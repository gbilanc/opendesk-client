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
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from opendesk.crypto.auth import AuthManager

logger = logging.getLogger(__name__)


class SessionInfoWidget(QWidget):
    """Displays the local session ID and password for incoming connections.

    Designed to sit between the toolbar and the remote viewer.
    Auto-adapts to light/dark theme via palette colors.
    """

    session_refreshed = Signal(str, str)  # session_id, password

    def __init__(
        self, auth_manager: AuthManager, parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._auth = auth_manager
        self._session_id = ""
        self._password = ""
        self._setup_ui()
        self.refresh_session()

    # ── UI ──────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        self.setObjectName("SessionInfoWidget")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(12)

        # ── Il tuo ID ──
        id_label = QLabel("Il tuo ID:")
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
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                background: rgba(255,255,255,0.6);
                color: #0f172a;
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
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setStyleSheet("background: #cbd5e1; max-width: 1px;")
        layout.addWidget(sep)

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
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                background: rgba(255,255,255,0.6);
                color: #0f172a;
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
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                background: transparent;
                font-size: 12px;
                font-weight: 600;
                color: #475569;
            }
            QPushButton:hover {
                background: rgba(37, 99, 235, 0.08);
                border-color: #2563eb;
                color: #2563eb;
            }
            QPushButton:pressed {
                background: rgba(37, 99, 235, 0.15);
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
                border: 1px solid #cbd5e1;
                border-radius: 6px;
                background: #ffffff;
                font-size: 12px;
                font-weight: 600;
                color: #2563eb;
            }
            QPushButton:hover {
                background: #eff6ff;
                border-color: #2563eb;
            }
            QPushButton:pressed {
                background: #dbeafe;
            }
            QPushButton:disabled {
                color: #94a3b8;
                background: #f8fafc;
                border-color: #e2e8f0;
            }
        """

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
