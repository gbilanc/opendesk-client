"""
Dual-pane file transfer UI with local/remote file browser.

Provides:
- ``FileSystemModel`` — QAbstractItemModel for remote filesystem browsing
- ``TransferListModel`` — QAbstractListModel for transfer jobs
- ``TransferDelegate`` — QStyledItemDelegate with progress bar
- ``FileBrowserDock`` — dual-pane window with local (left) and remote (right) browser
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from PySide6.QtCore import (
    Qt, QAbstractListModel, QAbstractItemModel, QModelIndex,
    QSize, Signal, Slot, QRect, QDir,
)
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QTextOption, QFontMetrics, QIcon,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListView,
    QPushButton, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy,
    QSplitter, QTreeView, QLineEdit, QFileSystemModel,
    QHeaderView, QStatusBar, QToolBar, QFileDialog,
    QMessageBox, QApplication,
)

from opendesk.core.file_transfer import TransferJob, TransferState, TransferDirection

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

_MAX_VISIBLE_ITEMS = 20
_ITEM_HEIGHT = 64
_PROGRESS_BAR_HEIGHT = 6

_ICON_FOLDER = "📁"
_ICON_FILE = "📄"
_ICON_IMAGE = "🖼️"
_ICON_VIDEO = "🎬"
_ICON_CODE = "💻"
_ICON_ARCHIVE = "📦"
_ICON_PARENT = "🔙"

# ═══════════════════════════════════════════════════════════════════
# Remote file system model
# ═══════════════════════════════════════════════════════════════════


class RemoteFileSystemModel(QAbstractItemModel):
    """Tree model for remote filesystem contents.

    Data is populated from ``FILE_LIST_RESPONSE`` messages received
    from the remote peer.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._root: dict = {
            "name": "/",
            "is_dir": True,
            "path": "/",
            "children": [],
        }
        self._path_map: dict[str, dict] = {"/": self._root}

    # ── QAbstractItemModel interface ────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if not parent.isValid():
            return len(self._root.get("children") or [])
        node = parent.internalPointer() if parent.internalPointer() else self._root
        if not isinstance(node, dict):
            return 0
        children = node.get("children")
        return 0 if children is None else len(children)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 4  # name, size, type, modified

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        parent_node = parent.internalPointer() if parent.isValid() else self._root
        if not isinstance(parent_node, dict):
            return QModelIndex()
        children = parent_node.get("children") or []
        if row < 0 or row >= len(children):
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        node = index.internalPointer()
        if node is None or not isinstance(node, dict):
            return QModelIndex()

        # Find parent by scanning
        parent_path = self._find_parent_path(node)
        if parent_path is None or parent_path == "/":
            return QModelIndex()

        parent_node = self._path_map.get(parent_path)
        if parent_node is None:
            return QModelIndex()

        # Find row of parent
        grandparent_path = self._find_parent_path(parent_node)
        grandparent = self._path_map.get(grandparent_path or "/", self._root)
        grandparent_children = grandparent.get("children") or []
        try:
            row = grandparent_children.index(parent_node)
        except ValueError:
            return QModelIndex()
        return self.createIndex(row, 0, parent_node)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = index.internalPointer()
        if not isinstance(node, dict):
            return None

        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            match col:
                case 0:
                    return node.get("name", "")
                case 1:
                    size = node.get("size", 0)
                    return self._format_size(size) if not node.get("is_dir") else ""
                case 2:
                    return "Directory" if node.get("is_dir") else "File"
                case 3:
                    mtime = node.get("mtime", 0)
                    if mtime:
                        from datetime import datetime
                        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                    return ""
                case _:
                    return ""

        if role == Qt.ItemDataRole.DecorationRole and col == 0:
            return self._icon_for_node(node)

        if role == Qt.ItemDataRole.ToolTipRole:
            return node.get("path", node.get("name", ""))

        if role == Qt.ItemDataRole.UserRole:
            return node

        # Foreground color for directories
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            if node.get("is_dir"):
                return QColor("#2563eb")

        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            headers = ["Name", "Size", "Type", "Modified"]
            if 0 <= section < len(headers):
                return headers[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        node = index.internalPointer()
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags

    # ── public API ──────────────────────────────────────────────────

    def set_directory_contents(self, path: str, entries: list[dict]) -> None:
        """Replace the contents of a directory with new entries."""
        node = self._path_map.get(path)
        if node is None:
            # Create intermediate nodes if needed
            path_obj = Path(path)
            parent_path = str(path_obj.parent)
            if parent_path not in self._path_map:
                # Walk up to find existing ancestor
                parts = path_obj.parts
                current = "/"
                for part in parts:
                    if not part or part == "/":
                        continue
                    candidate = str(Path(current) / part) if current != "/" else f"/{part}"
                    if candidate not in self._path_map:
                        break
                    current = candidate

            self._ensure_path(path)
            node = self._path_map.get(path)
            if node is None:
                return

        # Begin reset on parent
        parent_index = self._find_index_for_path(path)
        if parent_index.isValid():
            self.beginRemoveRows(parent_index, 0, max(0, len(node.get("children") or []) - 1))
        else:
            self.beginRemoveRows(QModelIndex(), 0, max(0, len(self._root.get("children") or []) - 1))

        old_children = node.get("children") or []
        for child in old_children:
            child_path = child.get("path", "")
            if child_path in self._path_map:
                del self._path_map[child_path]

        node["children"] = []
        if parent_index.isValid():
            self.endRemoveRows()
        else:
            self.endRemoveRows()

        # Add new entries
        children: list[dict] = []
        for entry in entries:
            name = entry.get("name", "")
            is_dir = entry.get("is_dir", False)
            child_path = path.rstrip("/") + "/" + name
            child_node: dict = {
                "name": name,
                "is_dir": is_dir,
                "size": entry.get("size", 0),
                "mtime": entry.get("mtime", 0.0),
                "path": child_path,
                "children": [] if is_dir else None,
            }
            children.append(child_node)
            if is_dir:
                self._path_map[child_path] = child_node

        # Sort: directories first, then files alphabetically
        children.sort(key=lambda c: (not c["is_dir"], c["name"].lower()))

        if parent_index.isValid():
            self.beginInsertRows(parent_index, 0, len(children) - 1)
        else:
            self.beginInsertRows(QModelIndex(), 0, len(children) - 1)

        node["children"] = children

        if parent_index.isValid():
            self.endInsertRows()
        else:
            self.endInsertRows()

    def clear(self) -> None:
        """Clear all remote data."""
        self.beginResetModel()
        self._root = {
            "name": "/",
            "is_dir": True,
            "path": "/",
            "children": [],
        }
        self._path_map = {"/": self._root}
        self.endResetModel()

    def node_at(self, index: QModelIndex) -> dict | None:
        """Get the node dict for a given index."""
        if not index.isValid():
            return None
        node = index.internalPointer()
        return node if isinstance(node, dict) else None

    def path_for_index(self, index: QModelIndex) -> str:
        """Get the full path for a given index."""
        node = self.node_at(index)
        if node:
            return node.get("path", node.get("name", ""))
        return ""

    # ── internal ────────────────────────────────────────────────────

    def _ensure_path(self, path: str) -> dict:
        """Ensure a path exists in the tree, creating intermediates."""
        if path in self._path_map:
            return self._path_map[path]

        if path == "/" or not path:
            return self._root

        path_obj = Path(path)
        parent_path = str(path_obj.parent)
        parent = self._ensure_path(parent_path)

        name = path_obj.name
        child = {
            "name": name,
            "is_dir": True,
            "path": path,
            "size": 0,
            "mtime": 0.0,
            "children": [],
        }
        parent.setdefault("children", []).append(child)
        self._path_map[path] = child
        return child

    def _find_index_for_path(self, path: str) -> QModelIndex:
        """Find the QModelIndex for a given path."""
        if path == "/" or not path:
            return QModelIndex()

        parts = Path(path).parts
        current = "/"
        parent = QModelIndex()

        for part in parts:
            if not part or part == "/":
                continue
            child_path = f"{current.rstrip('/')}/{part}" if current != "/" else f"/{part}"
            node = self._path_map.get(child_path)
            if node is None:
                return QModelIndex()
            # Find row
            parent_node = parent.internalPointer() if parent.isValid() else self._root
            parent_children = parent_node.get("children") or []
            try:
                row = parent_children.index(node)
            except ValueError:
                return QModelIndex()
            parent = self.createIndex(row, 0, node)
            current = child_path

        return parent

    def _find_parent_path(self, node: dict) -> str | None:
        """Find the parent path for a node."""
        node_path = node.get("path", "")
        if not node_path or node_path == "/":
            return None
        return str(Path(node_path).parent)

    @staticmethod
    def _icon_for_node(node: dict) -> str:
        if node.get("is_dir"):
            return _ICON_FOLDER
        name = node.get("name", "").lower()
        ext = Path(name).suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg"):
            return _ICON_IMAGE
        if ext in (".mp4", ".avi", ".mkv", ".mov", ".webm"):
            return _ICON_VIDEO
        if ext in (".py", ".js", ".ts", ".java", ".cpp", ".c", ".rs", ".go"):
            return _ICON_CODE
        if ext in (".zip", ".tar", ".gz", ".bz2", ".7z", ".rar"):
            return _ICON_ARCHIVE
        return _ICON_FILE

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ═══════════════════════════════════════════════════════════════════
# Transfer list model
# ═══════════════════════════════════════════════════════════════════


class TransferListModel(QAbstractListModel):
    """Model-View compliant model for a list of transfer jobs."""

    countChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._jobs: list[TransferJob] = []

    # ── QAbstractListModel interface ────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._jobs)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        job = self._jobs[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return job.file_info.name
        if role == Qt.ItemDataRole.UserRole:
            return job.id
        if role == Qt.ItemDataRole.UserRole + 1:
            return job.state
        if role == Qt.ItemDataRole.UserRole + 2:
            return job.progress
        if role == Qt.ItemDataRole.UserRole + 3:
            return job.file_info.size
        if role == Qt.ItemDataRole.UserRole + 4:
            return job.bytes_transferred
        if role == Qt.ItemDataRole.UserRole + 5:
            return job.direction
        if role == Qt.ItemDataRole.UserRole + 6:
            return job.error
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{job.file_info.name} — {job.state.name}"
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # ── public API ──────────────────────────────────────────────────

    def upsert(self, job: TransferJob) -> None:
        """Add a new job or update an existing one (by ID)."""
        for i, existing in enumerate(self._jobs):
            if existing.id == job.id:
                self._jobs[i] = job
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [])
                return
        # New job
        row = len(self._jobs)
        self.beginInsertRows(QModelIndex(), row, row)
        self._jobs.append(job)
        self.endInsertRows()
        self.countChanged.emit(len(self._jobs))

    def remove(self, job_id: str) -> bool:
        """Remove a job by ID."""
        for i, job in enumerate(self._jobs):
            if job.id == job_id:
                self.beginRemoveRows(QModelIndex(), i, i)
                self._jobs.pop(i)
                self.endRemoveRows()
                self.countChanged.emit(len(self._jobs))
                return True
        return False

    def clear_completed(self) -> None:
        """Remove all completed/failed/cancelled jobs."""
        completed_states = {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
        to_remove = [i for i, j in enumerate(self._jobs) if j.state in completed_states]
        for i in reversed(to_remove):
            self.beginRemoveRows(QModelIndex(), i, i)
            self._jobs.pop(i)
            self.endRemoveRows()
        if to_remove:
            self.countChanged.emit(len(self._jobs))

    def job_at(self, row: int) -> TransferJob | None:
        if 0 <= row < len(self._jobs):
            return self._jobs[row]
        return None

    @property
    def active_count(self) -> int:
        return sum(1 for j in self._jobs if j.state == TransferState.IN_PROGRESS)


# ═══════════════════════════════════════════════════════════════════
# Transfer delegate — custom rendering with progress bar
# ═══════════════════════════════════════════════════════════════════


class TransferDelegate(QStyledItemDelegate):
    """Delegate che disegna ogni transfer con nome, stato e barra progresso."""

    # Palette
    _BG_PROGRESS = "#e2e8f0"
    _FG_PROGRESS = "#2563eb"
    _FG_PROGRESS_DONE = "#22c55e"
    _FG_PROGRESS_ERROR = "#ef4444"
    _TEXT_NAME = "#0f172a"
    _TEXT_MUTED = "#94a3b8"
    _TEXT_STATE = {
        TransferState.PENDING: "#f59e0b",
        TransferState.IN_PROGRESS: "#2563eb",
        TransferState.COMPLETED: "#22c55e",
        TransferState.FAILED: "#ef4444",
        TransferState.CANCELLED: "#94a3b8",
        TransferState.PAUSED: "#f59e0b",
        TransferState.ACCEPTED: "#2563eb",
    }

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = option.rect
        # Background — use QStyle.State_Selected (Qt6 API)
        from PySide6.QtWidgets import QStyle
        selected = bool(option.state & QStyle.State_Selected)
        painter.fillRect(rect, QColor("#f8fafc") if selected else QColor("#ffffff"))
        # Bottom border
        painter.setPen(QPen(QColor("#f1f5f9"), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        content = rect.adjusted(12, 6, -12, -6)

        # ── Row 1: File name (left) + State badge (right) ──
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        state: TransferState = index.data(Qt.ItemDataRole.UserRole + 1)
        direction: TransferDirection = index.data(Qt.ItemDataRole.UserRole + 5)

        # Direction icon
        dir_icon = "↑" if direction == TransferDirection.SEND else "↓"

        # Name
        painter.setPen(QColor(self._TEXT_NAME))
        font_name = QFont("Segoe UI", 12, QFont.Weight.DemiBold)
        painter.setFont(font_name)
        name_rect = QRect(content.left(), content.top(), content.width() - 80, 20)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         f"{dir_icon}  {name}")

        # State
        state_color = self._TEXT_STATE.get(state, "#94a3b8")
        state_label = state.name.replace("_", " ").title()
        painter.setPen(QColor(state_color))
        font_state = QFont("Segoe UI", 10, QFont.Weight.Medium)
        painter.setFont(font_state)
        state_rect = QRect(content.right() - 80, content.top(), 80, 20)
        painter.drawText(state_rect, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                         state_label)

        # ── Row 2: Size info (left) + Progress bar (right) ──
        size_total = index.data(Qt.ItemDataRole.UserRole + 3) or 0
        bytes_done = index.data(Qt.ItemDataRole.UserRole + 4) or 0
        progress = index.data(Qt.ItemDataRole.UserRole + 2) or 0.0

        size_text = self._format_size(bytes_done) + " / " + self._format_size(size_total)
        painter.setPen(QColor(self._TEXT_MUTED))
        font_size = QFont("Segoe UI", 10)
        painter.setFont(font_size)
        size_rect = QRect(content.left(), content.top() + 22, content.width() - 6, 16)
        painter.drawText(size_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         size_text)

        # Progress bar
        bar_top = content.top() + 42
        bar_rect = QRect(content.left(), bar_top, content.width(), _PROGRESS_BAR_HEIGHT)
        painter.setBrush(QBrush(QColor(self._BG_PROGRESS)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(bar_rect, 3, 3)

        fill_w = int(bar_rect.width() * min(1.0, progress))
        if fill_w > 0:
            fill_color = self._FG_PROGRESS_DONE if state == TransferState.COMPLETED else (
                self._FG_PROGRESS_ERROR if state == TransferState.FAILED else self._FG_PROGRESS
            )
            painter.setBrush(QBrush(QColor(fill_color)))
            fill_rect = QRect(bar_rect.left(), bar_rect.top(), fill_w, bar_rect.height())
            painter.drawRoundedRect(fill_rect, 3, 3)

        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(300, _ITEM_HEIGHT)

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        return f"{size / (1024 * 1024 * 1024):.2f} GB"


# ═══════════════════════════════════════════════════════════════════
# File browser dock — main dual-pane widget
# ═══════════════════════════════════════════════════════════════════


class FileBrowserDock(QDialog):
    """Dual-pane file browser for local and remote filesystems.

    Supports:
    - Local filesystem navigation (left pane)
    - Remote filesystem navigation (right pane)
    - Upload: select local files → send to remote
    - Download: select remote files → receive to local
    - Active transfer list with progress
    """

    # Signals
    file_upload_requested = Signal(list)  # list of local file paths
    file_download_requested = Signal(list)  # list of remote paths
    remote_listing_requested = Signal(str)  # remote path to list
    navigate_local = Signal(str)  # local path to navigate to

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Transfer")
        self.setMinimumSize(900, 550)
        self.resize(1100, 650)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        # ── Models ──
        self._transfer_model = TransferListModel(self)
        self._local_model = QFileSystemModel(self)
        self._local_model.setRootPath(QDir.rootPath())
        self._local_model.setFilter(
            QDir.Filter.AllDirs | QDir.Filter.Files | QDir.Filter.NoDotAndDotDot
        )
        self._remote_model = RemoteFileSystemModel(self)

        # Current paths
        self._local_root = str(Path.home())
        self._remote_path = "/"

        self._build_ui()
        self._connect_signals()

    # ── UI construction ─────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Build the complete UI layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── Toolbar ──
        toolbar = self._build_toolbar()
        layout.addWidget(toolbar)

        # ── Dual-pane splitter ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setChildrenCollapsible(False)

        # Left: Local filesystem
        left_panel = self._build_pane(
            title="Local",
            model=self._local_model,
            is_local=True,
        )
        splitter.addWidget(left_panel)

        # Right: Remote filesystem
        right_panel = self._build_pane(
            title="Remote",
            model=self._remote_model,
            is_local=False,
        )
        splitter.addWidget(right_panel)

        splitter.setSizes([450, 450])
        layout.addWidget(splitter, 3)

        # ── Transfer list section ──
        transfers_section = self._build_transfers_section()
        layout.addWidget(transfers_section, 2)

        # ── Status bar ──
        self._status_bar = QStatusBar()
        self._status_bar.setStyleSheet("""
            QStatusBar {
                background: #f8fafc;
                border-top: 1px solid #e2e8f0;
                font-size: 12px;
                color: #64748b;
                padding: 2px 8px;
            }
        """)
        self._status_label = QLabel("Ready")
        self._status_bar.addWidget(self._status_label, 1)
        self._selection_label = QLabel("")
        self._status_bar.addPermanentWidget(self._selection_label)
        layout.addWidget(self._status_bar)

    def _build_toolbar(self) -> QWidget:
        """Build the action toolbar."""
        toolbar = QWidget()
        toolbar.setStyleSheet("""
            QWidget#fileBrowserToolbar {
                background: #ffffff;
                border-bottom: 1px solid #e2e8f0;
                padding: 4px;
            }
        """)
        toolbar.setObjectName("fileBrowserToolbar")

        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        # Upload button
        self._upload_btn = QPushButton("↑ Upload")
        self._upload_btn.setMinimumHeight(32)
        self._upload_btn.setStyleSheet("""
            QPushButton {
                background-color: #2563eb; color: white;
                border: none; border-radius: 6px;
                padding: 6px 18px; font-weight: 600; font-size: 13px;
            }
            QPushButton:hover { background-color: #1d4ed8; }
            QPushButton:disabled { background-color: #94a3b8; }
        """)
        self._upload_btn.setEnabled(False)
        layout.addWidget(self._upload_btn)

        # Download button
        self._download_btn = QPushButton("↓ Download")
        self._download_btn.setMinimumHeight(32)
        self._download_btn.setStyleSheet("""
            QPushButton {
                background-color: #059669; color: white;
                border: none; border-radius: 6px;
                padding: 6px 18px; font-weight: 600; font-size: 13px;
            }
            QPushButton:hover { background-color: #047857; }
            QPushButton:disabled { background-color: #94a3b8; }
        """)
        self._download_btn.setEnabled(False)
        layout.addWidget(self._download_btn)

        layout.addStretch()

        # Refresh remote button
        self._refresh_btn = QPushButton("🔄 Refresh")
        self._refresh_btn.setMinimumHeight(32)
        self._refresh_btn.setStyleSheet("""
            QPushButton {
                background: transparent; border: 1px solid #e2e8f0;
                border-radius: 6px; padding: 6px 16px;
                font-size: 13px; font-weight: 500;
            }
            QPushButton:hover { background: #f1f5f9; }
        """)
        layout.addWidget(self._refresh_btn)

        # Clear completed
        self._clear_btn = QPushButton("✕ Clear")
        self._clear_btn.setMinimumHeight(32)
        self._clear_btn.setStyleSheet("""
            QPushButton {
                background: transparent; border: 1px solid #e2e8f0;
                border-radius: 6px; padding: 6px 16px;
                font-size: 13px; font-weight: 500;
            }
            QPushButton:hover { background: #f1f5f9; }
        """)
        layout.addWidget(self._clear_btn)

        return toolbar

    def _build_pane(self, title: str, model, is_local: bool) -> QWidget:
        """Build one side of the dual-pane browser."""
        pane = QWidget()
        pane.setStyleSheet("""
            QWidget#filePane {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
            }
        """)
        pane.setObjectName("filePane")

        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Pane header ──
        header = QWidget()
        header.setStyleSheet("background: #f8fafc; border-radius: 8px 8px 0 0;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-size: 13px; font-weight: 700; color: #0f172a; background: transparent;")
        header_layout.addWidget(title_label)

        # Path bar
        path_bar = QLineEdit()
        path_bar.setStyleSheet("""
            QLineEdit {
                border: 1px solid #e2e8f0; border-radius: 4px;
                padding: 4px 8px; font-size: 12px; background: white;
            }
        """)
        header_layout.addWidget(path_bar, 1)

        # Home button
        home_btn = QPushButton("🏠")
        home_btn.setFixedSize(28, 28)
        home_btn.setStyleSheet("""
            QPushButton { border: none; border-radius: 4px; font-size: 14px; background: transparent; }
            QPushButton:hover { background: #e2e8f0; }
        """)
        header_layout.addWidget(home_btn)

        if is_local:
            self._local_path_bar = path_bar
            self._local_home_btn = home_btn
        else:
            self._remote_path_bar = path_bar
            self._remote_home_btn = home_btn
            path_bar.setPlaceholderText("/")

        layout.addWidget(header)

        # ── Tree view ──
        tree = QTreeView()
        tree.setModel(model)
        tree.setAnimated(True)
        tree.setIndentation(20)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        tree.setStyleSheet("""
            QTreeView {
                border: none; border-radius: 0px;
                font-size: 13px;
                outline: none;
            }
            QTreeView::item {
                padding: 4px 8px; min-height: 28px;
            }
            QTreeView::item:selected {
                background-color: #dbeafe; color: #0f172a;
            }
            QTreeView::item:hover {
                background-color: #f8fafc;
            }
        """)

        # Hide size/type/modified columns for local (use QFileSystemModel defaults)
        if is_local:
            tree.setRootIndex(self._local_model.index(self._local_root))
            # Show only filename column initially, hide others
            tree.setColumnWidth(0, 250)
            for col in range(1, self._local_model.columnCount()):
                tree.setColumnWidth(col, 80)

        # Configure column widths for remote model
        if not is_local:
            header_view = tree.header()
            header_view.setStretchLastSection(True)
            header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(1, 80)
            header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(2, 70)
            header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
            header_view.resizeSection(3, 120)

        tree.setObjectName(f"tree_{'local' if is_local else 'remote'}")
        layout.addWidget(tree, 1)

        if is_local:
            self._local_tree = tree
        else:
            self._remote_tree = tree

        return pane

    def _build_transfers_section(self) -> QWidget:
        """Build the active transfers section at the bottom."""
        section = QWidget()
        section.setStyleSheet("""
            QWidget#transferSection {
                background: #ffffff;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
            }
        """)
        section.setObjectName("transferSection")

        layout = QVBoxLayout(section)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)

        header = QLabel("Active Transfers")
        header.setStyleSheet("font-size: 13px; font-weight: 700; color: #0f172a; background: transparent;")
        layout.addWidget(header)

        self._transfer_list = QListView()
        self._transfer_list.setModel(self._transfer_model)
        self._transfer_list.setItemDelegate(TransferDelegate(self))
        self._transfer_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._transfer_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._transfer_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._transfer_list.setFrameShape(QFrame.Shape.NoFrame)
        self._transfer_list.setMinimumHeight(80)
        self._transfer_list.setMaximumHeight(150)
        self._transfer_list.setStyleSheet("""
            QListView { border: none; background: transparent; }
        """)
        layout.addWidget(self._transfer_list, 1)

        return section

    # ── signal connections ──────────────────────────────────────────

    def _connect_signals(self) -> None:
        """Connect internal signals."""
        # Local tree: double-click to enter directory / select files
        self._local_tree.doubleClicked.connect(self._on_local_double_clicked)
        local_sel = self._local_tree.selectionModel()
        if local_sel is not None:
            local_sel.selectionChanged.connect(self._on_local_selection_changed)

        # Remote tree: double-click to enter directory / select files
        self._remote_tree.doubleClicked.connect(self._on_remote_double_clicked)
        remote_sel = self._remote_tree.selectionModel()
        if remote_sel is not None:
            remote_sel.selectionChanged.connect(self._on_remote_selection_changed)

        # Path bars
        self._local_path_bar.returnPressed.connect(self._on_local_path_entered)
        self._remote_path_bar.returnPressed.connect(self._on_remote_path_entered)

        # Home buttons
        self._local_home_btn.clicked.connect(self._go_local_home)
        self._remote_home_btn.clicked.connect(self._go_remote_home)

        # Action buttons
        self._upload_btn.clicked.connect(self._on_upload_clicked)
        self._download_btn.clicked.connect(self._on_download_clicked)
        self._refresh_btn.clicked.connect(self._on_refresh_clicked)
        self._clear_btn.clicked.connect(self._clear_completed)

    # ── local filesystem handlers ───────────────────────────────────

    @Slot(QModelIndex)
    def _on_local_double_clicked(self, index: QModelIndex) -> None:
        """Double-click on local tree: enter directory or select file."""
        if self._local_model.isDir(index):
            self._local_tree.setRootIndex(index)
            path = self._local_model.filePath(index)
            self._local_path_bar.setText(path)
            self._local_root = path
            self.navigate_local.emit(path)

    @Slot()
    def _on_local_path_entered(self) -> None:
        """Navigate local path bar."""
        path = self._local_path_bar.text().strip()
        p = Path(path)
        if p.exists() and p.is_dir():
            index = self._local_model.index(str(p))
            self._local_tree.setRootIndex(index)
            self._local_root = str(p)

    @Slot()
    def _go_local_home(self) -> None:
        """Navigate local tree to home directory."""
        home = str(Path.home())
        self._local_path_bar.setText(home)
        index = self._local_model.index(home)
        self._local_tree.setRootIndex(index)
        self._local_root = home

    @Slot()
    def _on_local_selection_changed(self) -> None:
        """Update UI when local selection changes."""
        self._update_action_buttons()

    # ── remote filesystem handlers ──────────────────────────────────

    @Slot(QModelIndex)
    def _on_remote_double_clicked(self, index: QModelIndex) -> None:
        """Double-click on remote tree."""
        node = self._remote_model.node_at(index)
        if node and node.get("is_dir"):
            path = node.get("path", "")
            self._navigate_remote_to(path)
        # If it's a file, just select it (selection tracks it)

    def _navigate_remote_to(self, path: str) -> None:
        """Request remote directory listing and update path bar."""
        self._remote_path = path
        self._remote_path_bar.setText(path)
        self.remote_listing_requested.emit(path)

    @Slot()
    def _on_remote_path_entered(self) -> None:
        """Navigate remote path bar."""
        path = self._remote_path_bar.text().strip()
        if not path.startswith("/"):
            path = "/" + path
        self._navigate_remote_to(path)

    @Slot()
    def _go_remote_home(self) -> None:
        """Navigate remote to root."""
        self._navigate_remote_to("/")

    @Slot()
    def _on_remote_selection_changed(self) -> None:
        """Update UI when remote selection changes."""
        self._update_action_buttons()

    # ── action buttons ──────────────────────────────────────────────

    @Slot()
    def _on_upload_clicked(self) -> None:
        """Upload selected local files to remote."""
        indexes = self._local_tree.selectionModel().selectedIndexes()
        paths = set()
        for idx in indexes:
            if idx.column() == 0:  # Only first column
                path = self._local_model.filePath(idx)
                if not self._local_model.isDir(idx):
                    paths.add(path)

        if paths:
            self.file_upload_requested.emit(list(paths))
            self._status_label.setText(f"Uploading {len(paths)} file(s)...")

    @Slot()
    def _on_download_clicked(self) -> None:
        """Download selected remote files to local."""
        indexes = self._remote_tree.selectionModel().selectedIndexes()
        paths = set()
        for idx in indexes:
            if idx.column() == 0:
                node = self._remote_model.node_at(idx)
                if node and not node.get("is_dir"):
                    paths.add(node.get("path", ""))

        if paths:
            self.file_download_requested.emit(list(paths))
            self._status_label.setText(f"Downloading {len(paths)} file(s)...")

    @Slot()
    def _on_refresh_clicked(self) -> None:
        """Refresh the current remote directory."""
        self._navigate_remote_to(self._remote_path)

    @Slot()
    def _clear_completed(self) -> None:
        """Clear completed transfers."""
        self._transfer_model.clear_completed()

    # ── public API ──────────────────────────────────────────────────

    @property
    def transfer_model(self) -> TransferListModel:
        return self._transfer_model

    @property
    def remote_model(self) -> RemoteFileSystemModel:
        return self._remote_model

    def add_transfer(self, job: TransferJob) -> None:
        """Add or update a transfer in the list."""
        self._transfer_model.upsert(job)

    def remove_transfer(self, job_id: str) -> None:
        """Remove a transfer by ID."""
        self._transfer_model.remove(job_id)

    def set_remote_listing(self, path: str, entries: list[dict], error: str = "") -> None:
        """Update remote file listing."""
        if error:
            self._status_label.setText(f"⚠ Remote listing error: {error}")
            return
        self._remote_model.set_directory_contents(path, entries)
        self._status_label.setText(f"Remote: {path} ({len(entries)} items)")

    def set_status(self, message: str) -> None:
        """Set the status bar message."""
        self._status_label.setText(message)

    def set_connected(self, connected: bool) -> None:
        """Enable/disable remote-related controls based on connection."""
        self._refresh_btn.setEnabled(connected)
        self._download_btn.setEnabled(connected)
        self._remote_path_bar.setEnabled(connected)
        self._remote_home_btn.setEnabled(connected)
        if not connected:
            self._remote_model.clear()
            self._remote_path_bar.setText("")
            self._status_label.setText("Disconnected — remote browsing unavailable")
        else:
            self._navigate_remote_to("/")

    # ── internal ────────────────────────────────────────────────────

    def _update_action_buttons(self) -> None:
        """Enable/disable upload/download buttons based on selections."""
        # Guard: selectionModel might be None during initialization
        local_sel = self._local_tree.selectionModel()
        remote_sel = self._remote_tree.selectionModel()

        # Upload: needs local files selected
        local_indexes = local_sel.selectedIndexes() if local_sel else []
        has_local_files = any(
            not self._local_model.isDir(idx) and idx.column() == 0
            for idx in local_indexes
        )
        self._upload_btn.setEnabled(has_local_files)

        # Download: needs remote files selected
        remote_indexes = remote_sel.selectedIndexes() if remote_sel else []
        has_remote_files = False
        for idx in remote_indexes:
            if idx.column() == 0:
                node = self._remote_model.node_at(idx)
                if node and not node.get("is_dir"):
                    has_remote_files = True
                    break
        self._download_btn.setEnabled(has_remote_files and self._refresh_btn.isEnabled())

        # Update selection info
        local_count = sum(1 for idx in local_indexes if idx.column() == 0 and not self._local_model.isDir(idx))
        remote_count = sum(1 for idx in remote_indexes if idx.column() == 0)

        parts = []
        if local_count:
            parts.append(f"Local: {local_count} file(s)")
        if remote_count:
            parts.append(f"Remote: {remote_count} item(s)")
        self._selection_label.setText(" | ".join(parts) if parts else "")
