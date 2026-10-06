"""
Connection manager UI.

Provides:
- DeviceListModel — QAbstractListModel for device entries
- DeviceDelegate — QStyledItemDelegate with status badge rendering
- ConnectionPanel — widget embedded in MainWindow's central area
- SessionStatusWidget — status display for the status bar
"""

from __future__ import annotations

import logging

from PySide6.QtCore import (
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.device_registry import DeviceEntry
from opendesk.ui.widgets.empty_state_widget import EmptyStateWidget

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# Device list model
# ═══════════════════════════════════════════════════════════════════


class DeviceListModel(QAbstractListModel):
    """Model-View compliant model for a list of devices.

    Emits ``countChanged`` when the list is populated or cleared.
    """

    countChanged = Signal(int)  # noqa: N815
    deviceSelected = Signal(str, str, bool)  # device_id, session_id, trusted  # noqa: N815

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._devices: list[DeviceEntry] = []

    # ── QAbstractListModel interface ────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if parent.isValid():
            return 0
        return len(self._devices)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        dev = self._devices[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return dev.device_name
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"ID: {dev.device_id}"
            if dev.trusted:
                tip += "\nPre-autorizzato — connessione senza password"
            return tip
        if role == Qt.ItemDataRole.UserRole:
            return dev.device_id
        if role == Qt.ItemDataRole.UserRole + 1:
            return dev.session_id
        if role == Qt.ItemDataRole.UserRole + 2:
            return dev.trusted
        if role == Qt.ItemDataRole.UserRole + 3:
            return dev.online
        if role == Qt.ItemDataRole.UserRole + 4:
            return dev.device_name
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        dev = self._devices[index.row()]
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if not dev.session_id:
            flags &= ~Qt.ItemFlag.ItemIsEnabled
        return flags

    # ── public API ──────────────────────────────────────────────────

    def set_devices(self, devices: list[DeviceEntry]) -> None:
        """Sostituisce l'intera lista di dispositivi."""
        self.beginResetModel()
        self._devices = list(devices)
        self.endResetModel()
        self.countChanged.emit(len(self._devices))

    def device_at(self, row: int) -> DeviceEntry | None:
        """Restituisce il dispositivo all'indice ``row``."""
        if 0 <= row < len(self._devices):
            return self._devices[row]
        return None

    def row_of(self, device_id: str) -> int:
        """Cerca l'indice di un dispositivo per ID."""
        for i, d in enumerate(self._devices):
            if d.device_id == device_id:
                return i
        return -1


# ═══════════════════════════════════════════════════════════════════
# Device delegate — custom rendering
# ═══════════════════════════════════════════════════════════════════


class DeviceDelegate(QStyledItemDelegate):
    """Delegate per il rendering di ogni riga dispositivo.

    Disegna: pallino stato (🟢/🔴) | nome dispositivo | session ID |
    bottone "Connect" (solo per dispositivi online).
    """

    connect_clicked = Signal(QModelIndex)

    _STATUS_ONLINE = "#22c55e"
    _STATUS_OFFLINE = "#ef4444"
    _TEXT_PRIMARY = "#0f172a"
    _TEXT_MUTED = "#94a3b8"
    _ITEM_HEIGHT = 44
    _MARGIN = 8
    _BUTTON_WIDTH = 84
    _BUTTON_HEIGHT = 26

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        painter.save()

        from PySide6.QtWidgets import QStyle

        state = option.state
        # Background — use QStyle.State_* (Qt6 API)
        if state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor("#dbeafe"))
        elif state & QStyle.State_MouseOver:
            painter.fillRect(option.rect, QColor("#f8fafc"))
        else:
            painter.fillRect(option.rect, QColor("#ffffff"))

        # Draw bottom border
        painter.setPen(QPen(QColor("#f1f5f9"), 1))
        painter.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())

        rect = option.rect.adjusted(12, 0, -12, 0)
        y_center = rect.center().y()

        # Status dot
        online = index.data(Qt.ItemDataRole.UserRole + 3) or False
        dot_color = self._STATUS_ONLINE if online else self._STATUS_OFFLINE
        painter.setBrush(QBrush(QColor(dot_color)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(rect.left() + 4, y_center - 5, 10, 10)

        # Device name
        name = (
            index.data(Qt.ItemDataRole.UserRole + 4)
            or index.data(Qt.ItemDataRole.DisplayRole)
            or ""
        )
        # Reserve room on the right for the per-row Connect button (online only)
        reserve = self._BUTTON_WIDTH + 8 if online else 0
        painter.setPen(QColor(self._TEXT_PRIMARY))
        font = QFont("Segoe UI", 13, QFont.Weight.DemiBold)
        painter.setFont(font)
        name_rect = rect.adjusted(24, 4, -reserve, -18)
        painter.drawText(
            name_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
            name,
        )

        # Session ID (small, muted)
        session_id = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        if session_id:
            painter.setPen(QColor(self._TEXT_MUTED))
            font2 = QFont("Segoe UI", 11)
            painter.setFont(font2)
            id_rect = rect.adjusted(24, 18, -reserve, 0)
            painter.drawText(
                id_rect,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                session_id,
            )

        # Per-row Connect button (online devices only)
        if online:
            btn_rect = self._button_rect(option.rect)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor("#2563eb")))
            painter.drawRoundedRect(btn_rect, 6, 6)
            painter.setPen(QColor("#ffffff"))
            painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
            painter.drawText(btn_rect, Qt.AlignmentFlag.AlignCenter, "Connect")

        painter.restore()

    def _button_rect(self, item_rect: QRect) -> QRect:
        """Return the Connect button rectangle inside an item rect."""
        return QRect(
            item_rect.right() - 12 - self._BUTTON_WIDTH,
            item_rect.center().y() - self._BUTTON_HEIGHT // 2,
            self._BUTTON_WIDTH,
            self._BUTTON_HEIGHT,
        )

    def editorEvent(  # noqa: N802
        self, event, model, option: QStyleOptionViewItem, index: QModelIndex
    ) -> bool:
        """Emit ``connect_clicked`` when the per-row button is clicked."""
        if event.type() == QEvent.Type.MouseButtonRelease:
            online = index.data(Qt.ItemDataRole.UserRole + 3) or False
            if online and self._button_rect(option.rect).contains(event.position().toPoint()):
                self.connect_clicked.emit(index)
                return True
        return super().editorEvent(event, model, option, index)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802
        return QSize(200, self._ITEM_HEIGHT)


# ═══════════════════════════════════════════════════════════════════
# Connection panel
# ═══════════════════════════════════════════════════════════════════


class ConnectionPanel(QWidget):
    """Embedded panel for connecting to remote devices.

    Shows a list of known devices (from registry) with online/offline
    status, plus a manual entry section for new/unknown devices.

    Emits ``connection_requested(session_id, password)`` when the user
    initiates a connection.
    """

    connection_requested = Signal(str, str)  # session_id, password
    file_transfer_requested = Signal(str, str)  # device_id, password (programmatic API)
    disconnect_requested = Signal()  # disconnect current session

    def __init__(
        self,
        device_registry=None,
        local_device_id: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._model = DeviceListModel(self)
        self._registry = device_registry
        self._local_device_id = local_device_id
        self._setup_ui()
        self._setup_connections()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)

        # ── Title ──
        title = QLabel("Remote Connection")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # ── Device list (Model-View) ──
        list_label = QLabel("Known devices:")
        list_label.setStyleSheet("font-size: 12px; font-weight: 600;")
        layout.addWidget(list_label)

        self._list_view = QListView()
        self._list_view.setModel(self._model)
        self._delegate = DeviceDelegate(self)
        self._list_view.setItemDelegate(self._delegate)
        self._list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._list_view.setMinimumHeight(100)
        self._list_view.setFrameShape(QFrame.Shape.NoFrame)
        self._list_view.setMouseTracking(True)
        layout.addWidget(self._list_view, 1)

        # ── Empty state (shown when list is empty) ──
        self._empty_widget = EmptyStateWidget(
            icon="🖥️",
            title="No devices found",
            description="Devices connected to the relay will appear here.",
            action_text="",
        )
        self._empty_widget.setVisible(False)
        layout.addWidget(self._empty_widget)

        # ── Action buttons row ──
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        # ── Disconnect button ──
        btn_row.addStretch()

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        self._disconnect_btn.setStyleSheet("""
            QPushButton {
                background-color: #dc2626;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 6px 18px;
                font-weight: 600;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #b91c1c;
            }
            QPushButton:disabled {
                background-color: #94a3b8;
            }
        """)
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        btn_row.addWidget(self._disconnect_btn)

        layout.addLayout(btn_row)

        # ── Manual entry section ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("max-height: 1px; background: #e2e8f0;")
        layout.addWidget(sep)

        self._manual_toggle = QPushButton("➕ Connect to new device...")
        self._manual_toggle.setFlat(True)
        self._manual_toggle.setStyleSheet(
            "QPushButton { font-size: 12px; color: #2563eb; text-align: left; "
            "padding: 4px 0; }"
            "QPushButton:hover { color: #1d4ed8; }"
        )
        self._manual_toggle.clicked.connect(self._toggle_manual)
        layout.addWidget(self._manual_toggle)

        # ── Manual form ──
        self._manual_form = QWidget(self)
        manual_layout = QVBoxLayout(self._manual_form)
        manual_layout.setContentsMargins(0, 4, 0, 0)
        manual_layout.setSpacing(8)

        fields = QHBoxLayout()
        fields.setSpacing(8)

        self._manual_id = QLineEdit()
        self._manual_id.setPlaceholderText("Device ID (e.g. ABC-12345)")
        self._manual_id.setMinimumHeight(36)
        self._manual_id.setStyleSheet("font-size: 14px; font-weight: 600; letter-spacing: 2px;")
        fields.addWidget(self._manual_id, 2)

        self._manual_pwd = QLineEdit()
        self._manual_pwd.setPlaceholderText("Password")
        self._manual_pwd.setEchoMode(QLineEdit.EchoMode.Password)
        self._manual_pwd.setMinimumHeight(36)
        self._manual_pwd.setStyleSheet("font-size: 14px;")
        self._manual_pwd.returnPressed.connect(self._on_manual_connect)
        fields.addWidget(self._manual_pwd, 1)

        manual_layout.addLayout(fields)

        self._manual_connect_btn = QPushButton("Connect")
        self._manual_connect_btn.setProperty("class", "primary")
        self._manual_connect_btn.setEnabled(False)
        self._manual_connect_btn.clicked.connect(self._on_manual_connect)
        manual_layout.addWidget(self._manual_connect_btn)

        self._manual_id.textChanged.connect(self._on_manual_input_changed)
        self._manual_pwd.textChanged.connect(self._on_manual_input_changed)

        self._manual_form.setVisible(False)
        layout.addWidget(self._manual_form)

        layout.addStretch()

    def _setup_connections(self) -> None:
        self._delegate.connect_clicked.connect(self._on_row_connect)
        self._list_view.doubleClicked.connect(self._on_double_clicked)
        self._list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list_view.customContextMenuRequested.connect(self._on_context_menu)
        self._model.countChanged.connect(self._on_count_changed)

    # ── public API ──────────────────────────────────────────────────

    def update_device_list(self, devices: list[DeviceEntry]) -> None:
        """Update the displayed device list, excluding the local device."""
        filtered = [d for d in devices if d.device_id != self._local_device_id]
        self._model.set_devices(filtered)

    def set_connected(self, connected: bool) -> None:
        """Enable/disable Disconnect based on connection state."""
        self._disconnect_btn.setEnabled(connected)

    # ── context menu ───────────────────────────────────────────────

    @Slot()
    def _on_context_menu(self, pos) -> None:
        """Mostra menu contestuale per un dispositivo."""
        index = self._list_view.indexAt(pos)
        if not index.isValid():
            return

        device_id = index.data(Qt.ItemDataRole.UserRole) or ""
        trusted = index.data(Qt.ItemDataRole.UserRole + 2) or False

        menu = QMenu(self)
        if trusted:
            action_toggle = menu.addAction("❌ Revoca pre-autorizzazione")
        else:
            action_toggle = menu.addAction("✅ Pre-autorizza dispositivo")

        menu.addSeparator()
        action_copy_id = menu.addAction("📋 Copia UUID dispositivo")
        action_forget = menu.addAction("🧹 Dimentica segreti per-device")

        selected = menu.exec(self._list_view.viewport().mapToGlobal(pos))

        if selected == action_copy_id:
            from PySide6.QtWidgets import QApplication

            QApplication.clipboard().setText(device_id)
            return

        if selected == action_forget and self._registry:
            self._registry.forget_secrets(device_id)
            self._model.set_devices(
                [d for d in self._registry.all() if d.device_id != self._local_device_id]
            )
            return

        if selected == action_toggle and self._registry:
            self._registry.set_trusted(device_id, not trusted)
            # Aggiorna la visualizzazione dalla fonte ufficiale
            devices = self._registry.all()
            filtered = [d for d in devices if d.device_id != self._local_device_id]
            self._model.set_devices(filtered)

    @property
    def model(self) -> DeviceListModel:
        return self._model

    # ── slots ───────────────────────────────────────────────────────

    def _on_count_changed(self, count: int) -> None:
        """Toggle between list view and empty state."""
        has_devices = count > 0
        self._list_view.setVisible(has_devices)
        self._empty_widget.setVisible(not has_devices)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        """Double-click is disabled; connection only starts via the Connect button."""
        pass

    def _toggle_manual(self) -> None:
        visible = not self._manual_form.isVisible()
        self._manual_form.setVisible(visible)
        self._manual_toggle.setText(
            "✕ Hide manual form" if visible else "➕ Connect to new device..."
        )
        if visible:
            self._manual_id.setFocus()

    def _on_manual_input_changed(self) -> None:
        has_id = bool(self._manual_id.text().strip())
        has_pwd = bool(self._manual_pwd.text().strip())
        self._manual_connect_btn.setEnabled(has_id and has_pwd)

    def _on_manual_connect(self) -> None:
        device_id = self._manual_id.text().strip()
        password = self._manual_pwd.text().strip()
        if not device_id or not password:
            return
        self.connection_requested.emit(device_id, password)

    @Slot()
    def _on_disconnect(self) -> None:
        """Emit disconnect_requested signal."""
        self.disconnect_requested.emit()

    def _connect_device(self, device_id: str, session_id: str, trusted: bool) -> None:
        """Connect to a device, prompting for a password when needed."""
        if not session_id:
            QMessageBox.warning(
                self,
                "Device offline",
                "This device is not currently connected to the relay.\nPlease try again later.",
            )
            return
        password = "" if trusted else self._prompt_password(device_id)
        if password is not None:
            self.connection_requested.emit(device_id, password)

    @Slot(QModelIndex)
    def _on_row_connect(self, index: QModelIndex) -> None:
        """Connect to the device whose per-row button was clicked."""
        device_id = index.data(Qt.ItemDataRole.UserRole) or ""
        session_id = index.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = index.data(Qt.ItemDataRole.UserRole + 2) or False
        self._connect_device(device_id, session_id, trusted)

    def _prompt_password(self, device_id: str) -> str | None:
        pwd, ok = QInputDialog.getText(
            self,
            "Password Required",
            f"Enter the password for:\n{device_id[:8]}…",
            QLineEdit.EchoMode.Password,
        )
        return pwd.strip() if ok else None


# ═══════════════════════════════════════════════════════════════════
# Session status widget
# ═══════════════════════════════════════════════════════════════════


class SessionStatusWidget(QWidget):
    """Shows the status of the current session in the status bar."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._indicator = QLabel("●")
        self._indicator.setStyleSheet("font-size: 16px;")
        layout.addWidget(self._indicator)

        self._label = QLabel("Disconnected")
        self._label.setStyleSheet("font-size: 12px; color: #64748b;")
        layout.addWidget(self._label)

    @Slot(str)
    def set_status(self, status: str, connected: bool = False) -> None:
        """Update the displayed status."""
        self._label.setText(status)
        color = "#22c55e" if connected else "#64748b"
        self._indicator.setStyleSheet(f"color: {color}; font-size: 16px;")
