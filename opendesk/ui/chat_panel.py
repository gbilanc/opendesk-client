"""
Chat panel dock widget for in-session text communication.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

_MAX_MESSAGES = 500


class ChatPanel(QDockWidget):
    """Dock widget for in-session chat."""

    message_sent = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Chat", parent)
        self.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.setMinimumWidth(280)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )

        # ── Central widget ──
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        # ── Message display ──
        self._display = QTextBrowser()
        self._display.setReadOnly(True)
        self._display.setOpenExternalLinks(False)
        self._display.setMinimumHeight(120)
        layout.addWidget(self._display, 1)

        # ── Input area ──
        input_layout = QHBoxLayout()
        input_layout.setSpacing(6)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a message...")
        self._input.returnPressed.connect(self._send_message)
        input_layout.addWidget(self._input, 1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setEnabled(False)
        self._send_btn.setObjectName("PrimaryButton")
        self._send_btn.clicked.connect(self._send_message)
        input_layout.addWidget(self._send_btn)

        layout.addLayout(input_layout)

        self.setWidget(central)

        # Connect input enable
        self._input.textChanged.connect(self._on_input_changed)

    # ── public API ──────────────────────────────────────────────────

    def add_message(self, sender: str, text: str, is_remote: bool = False) -> None:
        """Append a message to the chat history.

        Parameters
        ----------
        sender : str
            Display name of the sender.
        text : str
            Message text.
        is_remote : bool
            If ``True``, styles the message as coming from the remote peer.
        """
        timestamp = time.strftime("%H:%M")
        prefix = "←" if is_remote else "→"

        # Truncate if too many messages
        doc = self._display.document()
        if doc.blockCount() > _MAX_MESSAGES:
            # Remove first block
            cursor = self._display.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor)
            cursor.removeSelectedText()

        # Style differently for local vs remote
        if is_remote:
            html = (
                f'<div style="margin-bottom: 6px;">'
                f'<span style="color: #2563eb; font-weight: 600;">{sender}</span> '
                f'<span style="color: #94a3b8; font-size: 11px;">{timestamp}</span><br>'
                f'<span style="color: #0f172a;">{self._escape(text)}</span>'
                f'</div>'
            )
        else:
            html = (
                f'<div style="margin-bottom: 6px; text-align: right;">'
                f'<span style="color: #64748b; font-size: 11px;">{timestamp}</span> '
                f'<span style="color: #059669; font-weight: 600;">{sender}</span><br>'
                f'<span style="color: #0f172a;">{self._escape(text)}</span>'
                f'</div>'
            )

        self._display.append(html)

        # Auto-scroll to bottom
        scrollbar = self._display.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear(self) -> None:
        """Clear all messages."""
        self._display.clear()

    # ── internal ────────────────────────────────────────────────────

    @Slot()
    def _on_input_changed(self) -> None:
        self._send_btn.setEnabled(bool(self._input.text().strip()))

    @Slot()
    def _send_message(self) -> None:
        text = self._input.text().strip()
        if not text:
            return

        # Add locally
        self.add_message("Me", text, is_remote=False)
        self.message_sent.emit(text)
        self._input.clear()

    @staticmethod
    def _escape(text: str) -> str:
        """Escape HTML special characters."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
