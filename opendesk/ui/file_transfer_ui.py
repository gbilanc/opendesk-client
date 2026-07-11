"""
File transfer UI — lista trasferimenti con Model-View + delegate.

Provides:
- TransferListModel — QAbstractListModel for transfer jobs
- TransferDelegate — QStyledItemDelegate with progress bar
- FileTransferDock — standalone window for active/completed transfers
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import (
    Qt, QAbstractListModel, QModelIndex, QSize, Signal, Slot, QRect,
)
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPen, QBrush,
    QTextOption, QFontMetrics,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListView,
    QPushButton, QStyledItemDelegate, QStyleOptionViewItem,
    QVBoxLayout, QWidget, QAbstractItemView, QSizePolicy,
)

from opendesk.core.file_transfer import TransferJob, TransferState, TransferDirection

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════

_MAX_VISIBLE_ITEMS = 20
_ITEM_HEIGHT = 64
_PROGRESS_BAR_HEIGHT = 6

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
        self.countChanged.emit(self._jobs)

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
        # Background
        if option.state & QStyleOptionViewItem.StateFlag.State_Selected:
            painter.fillRect(rect, QColor("#f8fafc"))
        else:
            painter.fillRect(rect, QColor("#ffffff"))
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
# File transfer dock
# ═══════════════════════════════════════════════════════════════════


class FileTransferDock(QDialog):
    """Standalone window showing active/completed file transfers.

    Uses ``TransferListModel`` + ``TransferDelegate`` for proper
    Model-View separation.
    """

    cancel_requested = Signal(str)  # job_id
    pause_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("File Transfers")
        self.setMinimumSize(380, 350)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._model = TransferListModel(self)

        # ── Layout ──
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # Header
        header = QLabel("Transfers")
        header.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(header)

        # Transfer list (Model-View)
        self._list = QListView()
        self._list.setModel(self._model)
        self._list.setItemDelegate(TransferDelegate(self))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setMinimumHeight(100)
        layout.addWidget(self._list, 1)

        # Bottom: clear completed
        self._clear_btn = QPushButton("Clear completed")
        self._clear_btn.setStyleSheet("font-size: 12px;")
        self._clear_btn.clicked.connect(self._clear_completed)
        layout.addWidget(self._clear_btn)

    # ── public API ──────────────────────────────────────────────────

    def add_transfer(self, job: TransferJob) -> None:
        """Add or update a transfer in the list."""
        self._model.upsert(job)

    def remove_transfer(self, job_id: str) -> None:
        """Remove a transfer from the list."""
        self._model.remove(job_id)

    @property
    def model(self) -> TransferListModel:
        return self._model

    # ── internal ────────────────────────────────────────────────────

    @Slot()
    def _clear_completed(self) -> None:
        self._model.clear_completed()
