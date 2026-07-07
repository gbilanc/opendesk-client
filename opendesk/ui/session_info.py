"""
Widget that displays the local session ID and password.

Shows a TeamViewer/AnyDesk-like "Your Session ID" panel at the top
of the main window, with copy buttons and a refresh action.
"""

from __future__ import annotations

import logging
import random
import string

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

from opendesk.crypto.auth import AuthManager

logger = logging.getLogger(__name__)


class SessionInfoWidget(QWidget):
    """Displays the device identity, session ID and password.

    Shows the persistent device ID (UUID), an editable device name,
    and the current session ID + password for incoming connections.
    """

    session_refreshed = Signal(str, str)  # session_id, password
    device_name_changed = Signal(str)  # new device name

    def __init__(
        self, auth_manager: AuthManager,
        device_id: str = "",
        device_name: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._auth = auth_manager
        self._session_id = ""
        self._password = ""
        self._device_id = device_id
        self._device_name = device_name
        self._name_editing = False
        self._setup_ui()
        self.refresh_session()

    # ── UI ──────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("SessionInfoWidget")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(12)

        # ── Device ID (persistent) ──
        device_label = QLabel("Dispositivo:")
        device_label.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #2563eb;"
        )
        layout.addWidget(device_label)

        self._device_name_label = QLabel(self._device_name)
        self._device_name_label.setObjectName("DeviceNameLabel")
        self._device_name_label.setStyleSheet(
            """
            QLabel#DeviceNameLabel {
                font-size: 14px;
                font-weight: 700;
                padding: 4px 8px;
                border: 1px solid transparent;
                border-radius: 4px;
            }
            QLabel#DeviceNameLabel:hover {
                border-color: palette(mid);
                background: rgba(37, 99, 235, 0.08);
            }
            """
        )
        self._device_name_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self._device_name_label.mousePressEvent = self._start_name_edit  # type: ignore[method-assign]
        layout.addWidget(self._device_name_label)

        self._device_id_label = QLabel(self._device_id[:8])
        self._device_id_label.setStyleSheet(
            "font-size: 11px; color: palette(shadow); padding: 0 4px;"
        )
        self._device_id_label.setToolTip(f"ID dispositivo: {self._device_id}")
        layout.addWidget(self._device_id_label)

        # ── Name editor (hidden by default) ──
        self._name_editor = QLineEdit(self._device_name)
        self._name_editor.setFixedHeight(30)
        self._name_editor.setMaximumWidth(200)
        self._name_editor.setVisible(False)
        self._name_editor.returnPressed.connect(self._finish_name_edit)
        self._name_editor.editingFinished.connect(self._finish_name_edit)
        layout.addWidget(self._name_editor)

        # ── Separator ──
        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.VLine)
        sep1.setStyleSheet("max-width: 1px;")
        layout.addWidget(sep1)

        # ── Session ID ──
        id_label = QLabel("ID sessione:")
        id_label.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #2563eb;"
        )
        layout.addWidget(id_label)

        self._id_display = QLabel("—")
        self._id_display.setObjectName("SessionIdDisplay")
        self._id_display.setStyleSheet(
            """
            QLabel#SessionIdDisplay {
                font-size: 22px;
                font-weight: 800;
                font-family: 'Courier New', 'Consolas', monospace;
                letter-spacing: 3px;
                padding: 2px 14px;
                border: 1px solid palette(mid);
                border-radius: 6px;
                min-width: 180px;
            }
            """
        )
        layout.addWidget(self._id_display)

        self._copy_id_btn = QPushButton("Copia ID")
        self._copy_id_btn.setFixedHeight(30)
        self._copy_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_id_btn.setStyleSheet(self._button_style())
        self._copy_id_btn.clicked.connect(self._copy_session_id)
        layout.addWidget(self._copy_id_btn)

        # ── Separator ──
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.VLine)
        sep2.setStyleSheet("max-width: 1px;")
        layout.addWidget(sep2)

        # ── Password ──
        pwd_label = QLabel("Password:")
        pwd_label.setStyleSheet(
            "font-size: 13px; font-weight: 700; color: #2563eb;"
        )
        layout.addWidget(pwd_label)

        self._pwd_display = QLabel("—")
        self._pwd_display.setObjectName("SessionPwdDisplay")
        self._pwd_display.setStyleSheet(
            """
            QLabel#SessionPwdDisplay {
                font-size: 16px;
                font-weight: 700;
                font-family: 'Courier New', 'Consolas', monospace;
                letter-spacing: 1px;
                padding: 2px 14px;
                border: 1px solid palette(mid);
                border-radius: 6px;
                min-width: 120px;
            }
            """
        )
        layout.addWidget(self._pwd_display)

        self._copy_pwd_btn = QPushButton("Copia Password")
        self._copy_pwd_btn.setFixedHeight(30)
        self._copy_pwd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_pwd_btn.setStyleSheet(self._button_style())
        self._copy_pwd_btn.clicked.connect(self._copy_password)
        layout.addWidget(self._copy_pwd_btn)

        # ── Spinge tutto a sinistra ──
        layout.addStretch(1)

        # ── Nuova sessione ──
        self._refresh_btn = QPushButton("🔄 Nuova sessione")
        self._refresh_btn.setFixedHeight(30)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setStyleSheet(
            """
            QPushButton {
                padding: 4px 14px;
                background: transparent;
                font-size: 12px;
                font-weight: 600;
            }
            QPushButton:hover {
                background: rgba(37, 99, 235, 0.12);
                border-color: #2563eb;
                color: #2563eb;
            }
            QPushButton:pressed {
                background: rgba(37, 99, 235, 0.20);
            }
            """
        )
        self._refresh_btn.clicked.connect(self.refresh_session)
        layout.addWidget(self._refresh_btn)

    @staticmethod
    def _button_style() -> str:
        return """
            QPushButton {
                padding: 4px 12px;
                font-size: 12px;
                font-weight: 600;
            }
        """

    # ── Properties ──────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def password(self) -> str:
        return self._password

    # ── Device name editing ────────────────────────────────────────

    @Slot()
    def set_device_name(self, name: str) -> None:
        """Update the displayed device name (from settings)."""
        self._device_name = name
        self._device_name_label.setText(name)
        self._name_editor.setText(name)

    def _start_name_edit(self, event: QMouseEvent | None = None) -> None:  # noqa: N802
        """Show the name editor in place of the label."""
        self._device_name_label.setVisible(False)
        self._name_editor.setText(self._device_name)
        self._name_editor.setVisible(True)
        self._name_editor.selectAll()
        self._name_editor.setFocus()

    @Slot()
    def _finish_name_edit(self) -> None:  # noqa: N802
        """Apply the new name and emit signal."""
        new_name = self._name_editor.text().strip()
        if not new_name:
            new_name = self._device_name
        self._device_name = new_name
        self._device_name_label.setText(new_name)
        self._device_name_label.setVisible(True)
        self._name_editor.setVisible(False)
        self.device_name_changed.emit(new_name)

    # ── Session lifecycle ───────────────────────────────────────────

    @Slot()
    def refresh_session(self) -> None:
        """Create a new session and update the display."""
        password = self._generate_password()
        session = self._auth.create_session(password, one_time=False)
        self._session_id = session.session_id
        self._password = password

        self._id_display.setText(self._session_id)
        self._pwd_display.setText(self._password)

        logger.info("New session: %s", self._session_id)
        self.session_refreshed.emit(self._session_id, self._password)

    @Slot()
    def _copy_session_id(self) -> None:
        """Copy session ID to the clipboard."""
        clipboard = QApplication.clipboard()
        clipboard.setText(self._session_id)
        self._flash_button(self._copy_id_btn, "Copiato!", self._button_style())
        logger.info("Session ID copied: %s", self._session_id)

    @Slot()
    def _copy_password(self) -> None:
        """Copy password to the clipboard."""
        clipboard = QApplication.clipboard()
        clipboard.setText(self._password)
        self._flash_button(self._copy_pwd_btn, "Copiato!", self._button_style())
        logger.info("Password copied")

    # ── Helpers ─────────────────────────────────────────────────────

    def _flash_button(self, btn: QPushButton, text: str, restore_style: str) -> None:
        """Briefly change button text, then restore after 1.5 s."""
        original = btn.text()
        btn.setText(text)
        btn.setEnabled(False)
        QTimer.singleShot(1500, lambda: self._restore_btn(btn, original, restore_style))

    def _restore_btn(self, btn: QPushButton, text: str, style: str) -> None:
        btn.setText(text)
        btn.setEnabled(True)

    @staticmethod
    def _generate_password() -> str:
        """Generate a random 8-character alphanumeric password."""
        alphabet = string.ascii_uppercase + string.digits
        return "".join(random.choices(alphabet, k=8))
