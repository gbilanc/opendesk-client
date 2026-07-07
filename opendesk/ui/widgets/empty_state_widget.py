"""Placeholder per stati vuoti (nessun dato, errore, caricamento)."""

from __future__ import annotations

from enum import Enum, auto

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QPushButton, QSizePolicy, QFrame,
)


class EmptyStateWidget(QFrame):
    """Placeholder per liste vuote, errori e caricamento.

    Usage::

        empty = EmptyStateWidget(
            icon="📄",
            title="Nessun dispositivo",
            description="I dispositivi connessi appariranno qui.",
            action_text="Cerca dispositivi",
            on_action=lambda: print("refresh"),
        )
        stack.addWidget(empty)
        stack.setCurrentWidget(empty)
    """

    action_clicked = Signal()

    def __init__(
        self,
        icon: str = "",
        title: str = "",
        description: str = "",
        action_text: str = "",
        on_action=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setStyleSheet("EmptyStateWidget { background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(8)

        # ── Icon ──
        self._icon = QLabel(icon or "")
        self._icon.setStyleSheet("font-size: 42px; background: transparent;")
        self._icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._icon)

        # ── Title ──
        self._title = QLabel(title)
        self._title.setStyleSheet(
            "font-size: 16px; font-weight: 600; color: #0f172a; background: transparent;"
        )
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setWordWrap(True)
        layout.addWidget(self._title)

        # ── Description ──
        self._description = QLabel(description)
        self._description.setStyleSheet(
            "font-size: 13px; color: #64748b; background: transparent;"
        )
        self._description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._description.setWordWrap(True)
        layout.addWidget(self._description)

        # ── Action button ──
        self._action_btn = QPushButton(action_text)
        self._action_btn.setProperty("class", "primary")
        self._action_btn.setVisible(bool(action_text))
        self._action_btn.setFixedWidth(180)
        self._action_btn.clicked.connect(self.action_clicked.emit)
        if on_action:
            self.action_clicked.connect(on_action)
        layout.addWidget(self._action_btn, 0, Qt.AlignmentFlag.AlignCenter)

    # ── public API ──────────────────────────────────────────────────

    def configure(
        self,
        icon: str = "",
        title: str = "",
        description: str = "",
        action_text: str = "",
    ) -> None:
        """Aggiorna il contenuto del placeholder."""
        if icon:
            self._icon.setText(icon)
            self._icon.setVisible(True)
        if title:
            self._title.setText(title)
        if description:
            self._description.setText(description)
        if action_text:
            self._action_btn.setText(action_text)
            self._action_btn.setVisible(True)
        else:
            self._action_btn.setVisible(False)
