"""
Connection manager UI.

Provides:
- ConnectionPanel — widget embedded in MainWindow's central area
- SessionStatusWidget — status display for the status bar
"""

from __future__ import annotations

import logging

from opendesk.core.device_registry import DeviceEntry

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Connection panel — embedded in MainWindow
# ---------------------------------------------------------------------------


class ConnectionPanel(QWidget):
    """Embedded panel for connecting to remote devices.

    Shows a list of known devices (from registry) with online/offline
    status, plus a manual entry section for new/unknown devices.

    Emits ``connection_requested(session_id, password)`` when the user
    initiates a connection.
    """

    connection_requested = Signal(str, str)  # session_id, password

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._devices: list[DeviceEntry] = []
        self._setup_ui()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 16)
        layout.setSpacing(12)

        # ── Title ──
        title = QLabel("Connessione remota")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(title)

        # ── Device list section ──
        list_label = QLabel("Dispositivi conosciuti:")
        list_label.setStyleSheet("font-size: 12px; font-weight: 600;")
        layout.addWidget(list_label)

        self._device_list = QListWidget()
        self._device_list.setMinimumHeight(100)
        self._device_list.itemClicked.connect(self._on_device_selected)
        self._device_list.itemDoubleClicked.connect(self._on_device_double_clicked)
        layout.addWidget(self._device_list, 1)

        # Connect button for selected device
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._connect_btn = QPushButton("Connetti")
        self._connect_btn.setEnabled(False)
        self._connect_btn.setObjectName("PrimaryButton")
        self._connect_btn.clicked.connect(self._on_connect)
        btn_row.addWidget(self._connect_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

        # ── Manual entry section (collapsible) ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("max-height: 1px;")
        layout.addWidget(sep)

        self._manual_toggle = QPushButton("➕ Connetti a nuovo dispositivo...")
        self._manual_toggle.setFlat(True)
        self._manual_toggle.setStyleSheet(
            "QPushButton { font-size: 12px; color: #2563eb; text-align: left; "
            "padding: 4px 0; }"
            "QPushButton:hover { color: #1d4ed8; }"
        )
        self._manual_toggle.clicked.connect(self._toggle_manual)
        layout.addWidget(self._manual_toggle)

        # Manual form (hidden by default)
        self._manual_form = QWidget(self)
        manual_layout = QVBoxLayout(self._manual_form)
        manual_layout.setContentsMargins(0, 4, 0, 0)
        manual_layout.setSpacing(8)

        fields = QHBoxLayout()
        fields.setSpacing(8)

        self._manual_id = QLineEdit()
        self._manual_id.setPlaceholderText("ID sessione (es. 123 456 789)")
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

        self._manual_connect_btn = QPushButton("Connetti")
        self._manual_connect_btn.setObjectName("PrimaryButton")
        self._manual_connect_btn.setEnabled(False)
        self._manual_connect_btn.clicked.connect(self._on_manual_connect)
        manual_layout.addWidget(self._manual_connect_btn)

        self._manual_id.textChanged.connect(self._on_manual_input_changed)
        self._manual_pwd.textChanged.connect(self._on_manual_input_changed)

        self._manual_form.setVisible(False)
        layout.addWidget(self._manual_form)

        layout.addStretch()

    # ── public API ──────────────────────────────────────────────────

    def update_device_list(self, devices: list[DeviceEntry]) -> None:
        """Update the displayed device list (called from MainWindow)."""
        self._devices = devices
        self._populate_device_list()

    # ── slots ───────────────────────────────────────────────────────

    def _toggle_manual(self) -> None:
        """Show/hide the manual connection form."""
        visible = not self._manual_form.isVisible()
        self._manual_form.setVisible(visible)
        self._manual_toggle.setText(
            "✕ Nascondi form manuale" if visible
            else "➕ Connetti a nuovo dispositivo..."
        )
        if visible:
            self._manual_id.setFocus()

    def _on_manual_input_changed(self) -> None:
        """Enable/disable manual connect button."""
        has_id = bool(self._manual_id.text().strip())
        has_pwd = bool(self._manual_pwd.text().strip())
        self._manual_connect_btn.setEnabled(has_id and has_pwd)

    def _on_manual_connect(self) -> None:
        """Connect using manually entered session ID + password."""
        session_id = self._manual_id.text().strip()
        password = self._manual_pwd.text().strip()
        if not session_id or not password:
            return
        self.connection_requested.emit(session_id, password)

    def _populate_device_list(self) -> None:
        """Populate the device list with online/offline indicators."""
        self._device_list.clear()

        if not self._devices:
            item = QListWidgetItem("Nessun dispositivo trovato.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._device_list.addItem(item)
            self._connect_btn.setEnabled(False)
            return

        for dev in self._devices:
            status = "🟢" if dev.online else "🔴"
            display = f"{status}  {dev.device_name}"
            if dev.online:
                display += "  — in linea"
            else:
                display += "  — offline"

            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, dev.device_id)
            item.setData(Qt.ItemDataRole.UserRole + 1, dev.session_id)
            item.setData(Qt.ItemDataRole.UserRole + 2, dev.trusted)

            if dev.trusted:
                item.setToolTip("Pre-autorizzato — connessione senza password")

            self._device_list.addItem(item)

    @Slot(QListWidgetItem)
    def _on_device_selected(self, item: QListWidgetItem) -> None:
        """Enable the connect button when a device is selected."""
        device_id = item.data(Qt.ItemDataRole.UserRole)
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = item.data(Qt.ItemDataRole.UserRole + 2) or False

        can_connect = bool(device_id and session_id)
        self._connect_btn.setEnabled(can_connect)

        if trusted and can_connect:
            self._on_connect()

    @Slot(QListWidgetItem)
    def _on_device_double_clicked(self, item: QListWidgetItem) -> None:
        """Double-click to connect."""
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        if session_id:
            self._on_connect()

    @Slot()
    def _on_connect(self) -> None:
        """Connect to the selected device."""
        item = self._device_list.currentItem()
        if item is None:
            return

        device_id = item.data(Qt.ItemDataRole.UserRole) or ""
        session_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        trusted = item.data(Qt.ItemDataRole.UserRole + 2) or False

        if not session_id:
            QMessageBox.warning(
                self, "Dispositivo offline",
                "Questo dispositivo non è attualmente connesso al relay.\n"
                "Riprova più tardi.",
            )
            return

        password = "" if trusted else self._prompt_password(device_id)
        if password is not None:
            self.connection_requested.emit(session_id, password)

    def _prompt_password(self, device_id: str) -> str | None:
        """Ask for the connection password via input dialog."""
        from PySide6.QtWidgets import QInputDialog

        pwd, ok = QInputDialog.getText(
            self, "Password richiesta",
            f"Inserisci la password per:\n{device_id[:8]}…",
            QLineEdit.EchoMode.Password,
        )
        return pwd if ok else None


# ---------------------------------------------------------------------------
# Session status widget
# ---------------------------------------------------------------------------


class SessionStatusWidget(QWidget):
    """Shows the status of the current session in the status bar area."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._indicator = QLabel("●")
        self._indicator.setStyleSheet("font-size: 16px;")
        layout.addWidget(self._indicator)

        self._label = QLabel("Disconnected")
        layout.addWidget(self._label)

    @Slot(str)
    def set_status(self, status: str, connected: bool = False) -> None:
        """Update the displayed status."""
        self._label.setText(status)
        color = "#22c55e" if connected else "#64748b"
        self._indicator.setStyleSheet(f"color: {color}; font-size: 16px;")
