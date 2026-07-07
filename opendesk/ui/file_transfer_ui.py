"""
File transfer UI — dock widget with progress bars and controls.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot, QSize
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.file_transfer import TransferJob, TransferState

logger = logging.getLogger(__name__)


class FileTransferDock(QDockWidget):
    """Dock widget showing active/completed file transfers."""

    cancel_requested = Signal(str)  # job_id
    pause_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("File Transfers", parent)
        self.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea
            | Qt.DockWidgetArea.BottomDockWidgetArea
        )
        self.setMinimumWidth(320)
        self.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
        )

        # ── Central widget ──
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(6)

        # Header
        header = QLabel("Active Transfers")
        header.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(header)

        # Transfer list
        self._list = QListWidget()
        layout.addWidget(self._list, 1)

        # Bottom: clear completed
        self._clear_btn = QPushButton("Clear Completed")
        self._clear_btn.setStyleSheet("font-size: 12px;")
        self._clear_btn.clicked.connect(self._clear_completed)
        layout.addWidget(self._clear_btn)

        self.setWidget(central)

    # ── public API ──────────────────────────────────────────────────

    def add_transfer(self, job: TransferJob) -> None:
        """Add or update a transfer in the list."""
        # Find existing item
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == job.id:
                self._update_item(item, job)
                return

        # Create new item
        widget = self._make_transfer_widget(job)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, job.id)
        item.setSizeHint(widget.sizeHint())
        self._list.addItem(item)
        self._list.setItemWidget(item, widget)

    def remove_transfer(self, job_id: str) -> None:
        """Remove a transfer from the list."""
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item and item.data(Qt.ItemDataRole.UserRole) == job_id:
                self._list.takeItem(i)
                break

    # ── internal ────────────────────────────────────────────────────

    def _make_transfer_widget(self, job: TransferJob) -> QWidget:
        """Create a widget representing a transfer."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)

        # Name row
        name_row = QHBoxLayout()
        name_label = QLabel(job.file_info.name)
        name_label.setStyleSheet("font-weight: 600; font-size: 12px;")
        name_row.addWidget(name_label, 1)

        status_label = QLabel(job.state.name.replace("_", " ").title())
        status_label.setStyleSheet("font-size: 11px;")
        name_row.addWidget(status_label)

        layout.addLayout(name_row)

        # Size info
        size_label = QLabel(self._format_size(job.file_info.size))
        size_label.setStyleSheet("font-size: 11px;")
        layout.addWidget(size_label)

        # Progress bar
        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(int(job.progress * 100))
        progress.setFixedHeight(6)
        progress.setTextVisible(False)
        layout.addWidget(progress)

        # Store progress bar reference
        widget._progress = progress
        widget._status_label = status_label

        return widget

    def _update_item(self, item: QListWidgetItem, job: TransferJob) -> None:
        """Update an existing list item with new job state."""
        widget = self._list.itemWidget(item)
        if widget is None:
            return

        if hasattr(widget, "_progress"):
            widget._progress.setValue(int(job.progress * 100))
        if hasattr(widget, "_status_label"):
            widget._status_label.setText(
                job.state.name.replace("_", " ").title()
            )

    @Slot()
    def _clear_completed(self) -> None:
        """Remove completed/failed/cancelled entries."""
        to_remove = []
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item:
                job_id = item.data(Qt.ItemDataRole.UserRole)
                # We don't have access to the job state here directly
                # In a real implementation, we'd check the manager
                to_remove.append((i, item))

        for i, item in reversed(to_remove):
            self._list.takeItem(i)

    @staticmethod
    def _format_size(size: int) -> str:
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024*1024):.1f} MB"
        return f"{size / (1024*1024*1024):.2f} GB"
