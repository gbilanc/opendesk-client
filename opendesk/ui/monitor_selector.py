"""
Monitor selection widget.

Allows the user to select which remote monitor to view,
with preview thumbnails and names.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.screen_capture import MonitorInfo

logger = logging.getLogger(__name__)


class MonitorSelector(QDialog):
    """Dialog to select a monitor to view."""

    monitor_selected = Signal(int)  # monitor index

    def __init__(
        self,
        monitors: list[MonitorInfo],
        current_index: int = 0,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Monitor")
        self.setMinimumWidth(400)
        self.setModal(True)

        self._monitors = monitors
        self._selected_index = current_index

        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title = QLabel("Remote Monitors")
        title.setStyleSheet("font-size: 16px; font-weight: 600; color: #0f172a;")
        layout.addWidget(title)

        subtitle = QLabel(
            f"{len(self._monitors)} monitor(s) detected on the remote computer."
        )
        subtitle.setStyleSheet("font-size: 13px; color: #64748b;")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        # Monitor list
        self._list = QListWidget()
        self._list.setStyleSheet("""
            QListWidget {
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                padding: 4px;
            }
            QListWidget::item {
                padding: 12px;
                border-radius: 6px;
                margin: 2px 0;
            }
            QListWidget::item:hover {
                background: #f1f5f9;
            }
            QListWidget::item:selected {
                background: #dbeafe;
                color: #0f172a;
            }
        """)
        self._list.currentRowChanged.connect(self._on_row_changed)

        for i, mon in enumerate(self._monitors):
            size_str = f"{mon.width}×{mon.height}"
            primary_str = " (Primary)" if mon.is_primary else ""
            text = f"🖥  {mon.name}{primary_str}\n   {size_str} at ({mon.left},{mon.top})"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)

        layout.addWidget(self._list, 1)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self.reject)
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                padding: 8px 20px;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                background: #ffffff;
            }
            QPushButton:hover { background: #f8fafc; }
        """)
        btn_layout.addWidget(self._cancel_btn)

        self._select_btn = QPushButton("Show Monitor")
        self._select_btn.setStyleSheet("""
            QPushButton {
                padding: 8px 20px;
                border: none;
                border-radius: 8px;
                background: #2563eb;
                color: white;
                font-weight: 600;
            }
            QPushButton:hover { background: #1d4ed8; }
        """)
        self._select_btn.clicked.connect(self._on_select)
        btn_layout.addWidget(self._select_btn)

        layout.addLayout(btn_layout)

        # Select current monitor
        if 0 <= self._selected_index < self._list.count():
            self._list.setCurrentRow(self._selected_index)

    @Slot(int)
    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self._monitors):
            self._selected_index = row

    @Slot()
    def _on_select(self) -> None:
        self.monitor_selected.emit(self._selected_index)
        self.accept()


class MonitorSwitcherWidget(QWidget):
    """Compact monitor switcher for the toolbar."""

    monitor_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(4)

        label = QLabel("Monitor:")
        label.setStyleSheet("font-size: 12px; color: #64748b; font-weight: 500;")
        layout.addWidget(label)

        self._combo = QComboBox()
        self._combo.setMinimumWidth(120)
        self._combo.setStyleSheet("""
            QComboBox {
                padding: 4px 8px;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                font-size: 12px;
                min-height: 24px;
            }
        """)
        self._combo.currentIndexChanged.connect(self._on_combo_changed)
        layout.addWidget(self._combo)

    def set_monitors(self, monitors: list[MonitorInfo]) -> None:
        """Populate the combo with monitor names."""
        self._combo.blockSignals(True)
        self._combo.clear()
        for i, mon in enumerate(monitors):
            label = f"{mon.name} ({mon.width}×{mon.height})"
            self._combo.addItem(label, i)
        self._combo.blockSignals(False)

    def set_current(self, index: int) -> None:
        """Set the currently selected monitor."""
        if 0 <= index < self._combo.count():
            self._combo.setCurrentIndex(index)

    @Slot(int)
    def _on_combo_changed(self, index: int) -> None:
        if index >= 0:
            self.monitor_changed.emit(self._combo.itemData(index))
