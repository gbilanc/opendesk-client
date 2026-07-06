"""
Main application window.

Orchestrates the remote viewer, connection manager, toolbars,
and status bar.  Manages the overall connection lifecycle.
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt, Slot, QSize
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from opendesk.ui.chat_panel import ChatPanel
from opendesk.ui.connections import ConnectionDialog, SessionStatusWidget
from opendesk.ui.file_transfer_ui import FileTransferDock
from opendesk.ui.settings_dialog import SettingsDialog
from opendesk.ui.viewer import RemoteViewer, ViewerToolbar

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window for the OpenDesk application."""

    WINDOW_TITLE = "OpenDesk"
    MIN_WIDTH = 1024
    MIN_HEIGHT = 680

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(1280, 800)

        # Application state
        self._connected: bool = False
        self._fullscreen: bool = False
        self._peer_id: str = ""

        # Build UI
        self._setup_actions()
        self._setup_menus()
        self._setup_toolbar()
        self._setup_viewer_toolbar()
        self._setup_docks()
        self._setup_central_widget()
        self._setup_statusbar()
        self._setup_fullscreen_shortcuts()

        logger.info("Main window initialised")

    # ── Initialisation ──────────────────────────────────────────────

    def _setup_actions(self) -> None:
        """Create reusable QAction objects."""
        # ── Session ──
        self.act_connect = QAction("&Connect...", self)
        self.act_connect.setShortcut(QKeySequence("Ctrl+N"))
        self.act_connect.setStatusTip("Connect to a remote computer")
        self.act_connect.triggered.connect(self._on_connect)

        self.act_disconnect = QAction("&Disconnect", self)
        self.act_disconnect.setShortcut(QKeySequence("Ctrl+D"))
        self.act_disconnect.setStatusTip("End current session")
        self.act_disconnect.setEnabled(False)
        self.act_disconnect.triggered.connect(self._on_disconnect)

        self.act_quit = QAction("&Quit", self)
        self.act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self.act_quit.triggered.connect(self.close)

        # ── View ──
        self.act_fullscreen = QAction("&Fullscreen", self)
        self.act_fullscreen.setShortcut(QKeySequence("F11"))
        self.act_fullscreen.setCheckable(True)
        self.act_fullscreen.triggered.connect(self._on_toggle_fullscreen)

        self.act_fit = QAction("Fit to &Window", self)
        self.act_fit.setShortcut(QKeySequence("Ctrl+0"))
        self.act_fit.triggered.connect(self._on_fit_view)

        self.act_zoom_in = QAction("Zoom &In", self)
        self.act_zoom_in.setShortcut(QKeySequence("Ctrl++"))
        self.act_zoom_in.triggered.connect(self._on_zoom_in)

        self.act_zoom_out = QAction("Zoom &Out", self)
        self.act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        self.act_zoom_out.triggered.connect(self._on_zoom_out)

        # ── Tools ──
        self.act_toggle_theme = QAction("Toggle &Dark Theme", self)
        self.act_toggle_theme.setShortcut(QKeySequence("Ctrl+T"))
        self.act_toggle_theme.setStatusTip("Switch between light and dark theme")
        self.act_toggle_theme.triggered.connect(self._on_toggle_theme)

        self.act_settings = QAction("&Settings...", self)
        self.act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self.act_settings.setStatusTip("Configure OpenDesk")
        self.act_settings.triggered.connect(self._on_settings)

        # ── Window ──
        self.act_show_chat = QAction("&Chat Panel", self)
        self.act_show_chat.setCheckable(True)
        self.act_show_chat.setChecked(True)
        self.act_show_chat.triggered.connect(self._on_toggle_chat)

        self.act_show_transfers = QAction("&File Transfers", self)
        self.act_show_transfers.setCheckable(True)
        self.act_show_transfers.setChecked(True)
        self.act_show_transfers.triggered.connect(self._on_toggle_transfers)

        # ── Help ──
        self.act_about = QAction("&About OpenDesk", self)
        self.act_about.triggered.connect(self._on_about)

    def _setup_menus(self) -> None:
        """Build the menu bar."""
        menubar = self.menuBar()
        assert menubar is not None

        # ── Session ──
        session_menu = menubar.addMenu("&Session")
        session_menu.addAction(self.act_connect)
        session_menu.addAction(self.act_disconnect)
        session_menu.addSeparator()
        session_menu.addAction(self.act_quit)

        # ── View ──
        view_menu = menubar.addMenu("&View")
        view_menu.addAction(self.act_fit)
        view_menu.addSeparator()
        view_menu.addAction(self.act_zoom_in)
        view_menu.addAction(self.act_zoom_out)
        view_menu.addSeparator()
        view_menu.addAction(self.act_fullscreen)
        view_menu.addSeparator()
        view_menu.addAction(self.act_toggle_theme)

        # ── Window ──
        window_menu = menubar.addMenu("&Window")
        window_menu.addAction(self.act_show_chat)
        window_menu.addAction(self.act_show_transfers)

        # ── Tools ──
        tools_menu = menubar.addMenu("&Tools")
        tools_menu.addAction(self.act_settings)

        # ── Help ──
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _setup_toolbar(self) -> None:
        """Create the main navigation toolbar."""
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setStyleSheet("""
            QToolBar {
                background: #ffffff;
                border-bottom: 1px solid #e2e8f0;
                padding: 4px 8px;
                spacing: 6px;
            }
            QToolButton {
                padding: 6px 14px;
                border-radius: 6px;
                border: 1px solid transparent;
                font-size: 13px;
            }
            QToolButton:hover {
                background: #f1f5f9;
                border-color: #e2e8f0;
            }
            QToolButton:pressed {
                background: #e2e8f0;
            }
        """)

        self.act_connect_tb = toolbar.addAction(self.act_connect)
        self.act_disconnect_tb = toolbar.addAction(self.act_disconnect)
        toolbar.addSeparator()
        toolbar.addAction(self.act_fit)
        toolbar.addAction(self.act_zoom_in)
        toolbar.addAction(self.act_zoom_out)
        toolbar.addSeparator()
        toolbar.addAction(self.act_fullscreen)

        self.addToolBar(toolbar)

    def _setup_viewer_toolbar(self) -> None:
        """Create a secondary, context-sensitive toolbar for the viewer."""
        self._viewer_toolbar = ViewerToolbar(self)
        self._viewer_toolbar.fullscreen_requested.connect(self._on_toggle_fullscreen)
        self._viewer_toolbar.zoom_in_requested.connect(self._on_zoom_in)
        self._viewer_toolbar.zoom_out_requested.connect(self._on_zoom_out)
        self._viewer_toolbar.fit_requested.connect(self._on_fit_view)
        self._viewer_toolbar.disconnect_requested.connect(self._on_disconnect)
        self.addToolBar(self._viewer_toolbar)

    def _setup_docks(self) -> None:
        """Create dock widgets (chat, file transfers)."""
        # ── Chat panel (right) ──
        self._chat_panel = ChatPanel(self)
        self._chat_panel.message_sent.connect(self._on_chat_message_sent)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._chat_panel)

        # ── File transfers (bottom) ──
        self._transfer_dock = FileTransferDock(self)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._transfer_dock)

        # Tabify file transfers with chat if they share the same area
        self.tabifyDockWidget(self._chat_panel, self._transfer_dock)

    def _setup_central_widget(self) -> None:
        """Build the central area with the remote viewer."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # The remote viewer
        self._viewer = RemoteViewer(central)
        self._viewer.fullscreen_toggled.connect(self._on_toggle_fullscreen)
        layout.addWidget(self._viewer, 1)

        self.setCentralWidget(central)

    def _setup_fullscreen_shortcuts(self) -> None:
        """Register global shortcuts that work even in fullscreen.

        F11 and Escape must work when menu/toolbar are hidden.
        QShortcut with Qt.ApplicationShortcut context ensures this.
        """
        # F11 toggle fullscreen (global, works even in fullscreen)
        self._fs_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fs_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._fs_shortcut.activated.connect(self._on_toggle_fullscreen)

        # Escape to exit fullscreen
        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc_shortcut.activated.connect(self._on_exit_fullscreen)

    def _setup_statusbar(self) -> None:
        """Configure the status bar."""
        status = QStatusBar(self)
        status.setStyleSheet("""
            QStatusBar {
                background: #f8fafc;
                border-top: 1px solid #e2e8f0;
                padding: 2px 12px;
                font-size: 12px;
                color: #64748b;
            }
        """)

        self._session_status = SessionStatusWidget()
        status.addPermanentWidget(self._session_status)

        self._status_label = QWidget()  # left-side status text
        from PySide6.QtWidgets import QLabel
        self._status_text = QLabel("Ready")
        self._status_text.setStyleSheet("font-size: 12px; color: #64748b;")
        status.addWidget(self._status_text, 1)

        self.setStatusBar(status)

    # ── Slots: session ──────────────────────────────────────────────

    @Slot()
    def _on_connect(self) -> None:
        """Open the connection dialog."""
        dialog = ConnectionDialog(self)
        dialog.connection_requested.connect(self._on_connection_requested)
        dialog.exec()

    @Slot(str, str)
    def _on_connection_requested(self, peer_id: str, password: str) -> None:
        """Handle a connection request from the dialog."""
        logger.info("Connection requested: peer=%s", peer_id)
        self._peer_id = peer_id
        self._status_text.setText(f"Connecting to {peer_id}...")

        # TODO: wire up actual P2P connection
        # For now, simulate connection
        self._set_connected(True)

    @Slot()
    def _on_disconnect(self) -> None:
        """Disconnect the current session."""
        if not self._connected:
            return

        logger.info("Disconnecting session: %s", self._peer_id)
        self._set_connected(False)
        self._status_text.setText("Disconnected")
        self._peer_id = ""

    # ── Slots: view ─────────────────────────────────────────────────

    @Slot()
    @Slot()
    def _on_toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode."""
        if self._fullscreen:
            self._on_exit_fullscreen()
        else:
            self._on_enter_fullscreen()

    def _on_enter_fullscreen(self) -> None:
        """Enter fullscreen."""
        self._fullscreen = True
        self.showFullScreen()
        self.menuBar().hide()
        self._viewer_toolbar.hide()
        for tb in self.findChildren(QToolBar):
            tb.hide()
        self.act_fullscreen.setChecked(True)
        self._status_text.setText("Fullscreen — press Esc or F11 to exit")

    def _on_exit_fullscreen(self) -> None:
        """Exit fullscreen and restore UI."""
        if not self._fullscreen:
            return
        self._fullscreen = False
        self.showNormal()
        self.menuBar().show()
        self._viewer_toolbar.show()
        for tb in self.findChildren(QToolBar):
            tb.show()
        self.act_fullscreen.setChecked(False)
        if self._connected:
            self._status_text.setText(f"Session active: {self._peer_id}")
        else:
            self._status_text.setText("Ready")

    @Slot()
    def _on_fit_view(self) -> None:
        """Fit the remote screen to the window."""
        self._viewer.zoom_to_fit()

    @Slot()
    def _on_zoom_in(self) -> None:
        """Zoom into the remote screen."""
        self._viewer.zoom_in()

    @Slot()
    def _on_zoom_out(self) -> None:
        """Zoom out of the remote screen."""
        self._viewer.zoom_out()

    # ── Slots: tools ───────────────────────────────────────────────

    @Slot()
    def _on_toggle_theme(self) -> None:
        """Toggle between light and dark theme."""
        from PySide6.QtWidgets import QApplication
        from opendesk.app import toggle_theme
        theme = toggle_theme(QApplication.instance())
        self.act_toggle_theme.setText(
            "Toggle &Light Theme" if theme == "dark" else "Toggle &Dark Theme"
        )
        logger.info("Theme switched to %s", theme)

    @Slot()
    def _on_settings(self) -> None:
        """Open the settings dialog."""
        dialog = SettingsDialog(self)
        dialog.exec()

    # ── Slots: window ──────────────────────────────────────────────

    @Slot()
    def _on_toggle_chat(self) -> None:
        """Show/hide the chat panel."""
        self._chat_panel.setVisible(self.act_show_chat.isChecked())

    @Slot()
    def _on_toggle_transfers(self) -> None:
        """Show/hide the file transfers panel."""
        self._transfer_dock.setVisible(self.act_show_transfers.isChecked())

    @Slot(str)
    def _on_chat_message_sent(self, text: str) -> None:
        """Handle a chat message sent by the local user."""
        logger.debug("Chat message sent: %s", text)
        # TODO: send via P2P connection

    # ── Slots: help ─────────────────────────────────────────────────

    @Slot()
    def _on_about(self) -> None:
        """Show the About dialog."""
        QMessageBox.about(
            self,
            "About OpenDesk",
            "<h3>OpenDesk v0.1.0</h3>"
            "<p>Multi-platform remote desktop application.</p>"
            "<p>Built with Python, PySide6, and WebRTC.</p>"
            "<hr>"
            "<p style='font-size:12px;color:#64748b'>"
            "MIT License — 2026</p>",
        )

    # ── Internal ────────────────────────────────────────────────────

    def _set_connected(self, connected: bool) -> None:
        """Update all UI state to reflect connection status."""
        self._connected = connected
        self._viewer.set_connection_active(connected)

        # Enable/disable actions
        self.act_connect.setEnabled(not connected)
        self.act_disconnect.setEnabled(connected)
        self.act_connect_tb.setEnabled(not connected)
        self.act_disconnect_tb.setEnabled(connected)

        # Update session status widget
        if connected:
            self._session_status.set_status(
                f"Connected to {self._peer_id}", connected=True
            )
            self._status_text.setText(f"Session active: {self._peer_id}")
            self.setWindowTitle(f"OpenDesk — {self._peer_id}")
        else:
            self._session_status.set_status("Disconnected")
            self._status_text.setText("Ready")
            self.setWindowTitle(self.WINDOW_TITLE)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Prompt for confirmation if a session is active."""
        if self._connected:
            reply = QMessageBox.question(
                self,
                "Confirm Disconnect",
                "A remote session is active.\nDisconnect and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        logger.info("Application closing")
        event.accept()
