"""
Main application window.

Orchestrates the remote viewer, connection manager, toolbars,
and status bar.  Manages the overall connection lifecycle with
real P2P relay networking.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

from PySide6.QtCore import QObject, QSettings, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.keyboard_state import caps_lock_active
from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.core.input_injection import (
    InputBackend,
    MouseButton,
    KeyState,
    MouseEvent,
    KeyboardEvent,
    create_input_backend,
)
from opendesk.crypto.auth import AuthManager
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayClient, RelayRole
from opendesk.ui.chat_panel import ChatPanel
from opendesk.ui.connections import ConnectionDialog, SessionStatusWidget
from opendesk.ui.file_transfer_ui import FileTransferDock
from opendesk.ui.session_info import SessionInfoWidget
from opendesk.ui.settings_dialog import SettingsDialog
from opendesk.ui.viewer import RemoteViewer, ViewerToolbar

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window for the OpenDesk application."""

    WINDOW_TITLE = "OpenDesk"
    MIN_WIDTH = 1024
    MIN_HEIGHT = 680

    # ── Caps Lock indicator styles ──
    _CAPS_ON_STYLE = (
        "font-size: 12px; font-weight: 700; color: #dc2626;"
        " padding: 0 6px;"
    )
    _CAPS_OFF_STYLE = (
        "font-size: 12px; font-weight: 600; color: #94a3b8;"
        " padding: 0 6px;"
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(1280, 800)

        # Application state
        self._connected: bool = False
        self._fullscreen: bool = False
        self._peer_id: str = ""  # remote session ID when acting as client
        self._host_session_id: str = ""  # our own session ID when hosting

        # Session management
        self._auth_manager = AuthManager()

        # Relay / P2P networking
        self._relay = RelayClient(self)
        self._relay.connected.connect(self._on_relay_connected)
        self._relay.disconnected.connect(self._on_relay_disconnected)
        self._relay.peer_joined.connect(self._on_peer_joined)
        self._relay.auth_requested.connect(self._on_auth_requested)
        self._relay.auth_result.connect(self._on_auth_result)
        self._relay.frame_received.connect(self._on_frame_received)
        self._relay.message_received.connect(self._on_relay_message)
        self._relay.error.connect(self._on_relay_error)

        # Screen capture for hosting
        self._capture: ScreenCapture | None = None
        self._capture_thread: threading.Thread | None = None
        self._capture_running = False
        self._input_backend: InputBackend | None = None

        # Streaming timer (host): polls screen capture at target FPS
        self._stream_timer = QTimer(self)
        self._stream_timer.timeout.connect(self._capture_and_send_frame)

        # Settings
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # Build UI
        self._setup_actions()
        self._setup_menus()
        self._setup_toolbar()
        self._setup_viewer_toolbar()
        self._setup_statusbar()
        self._setup_docks()
        self._setup_central_widget()
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

        toolbar.addAction(self.act_connect)
        toolbar.addAction(self.act_disconnect)
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

        self.tabifyDockWidget(self._chat_panel, self._transfer_dock)

    def _setup_central_widget(self) -> None:
        """Build the central area with session info + remote viewer."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Session info bar (shows your ID + password like TeamViewer)
        self._session_info = SessionInfoWidget(self._auth_manager, central)
        self._session_info.session_refreshed.connect(self._on_session_refreshed)
        layout.addWidget(self._session_info)

        # session_refreshed was already emitted in __init__, catch up
        if self._session_info.session_id and self._session_info.password:
            self._on_session_refreshed(
                self._session_info.session_id,
                self._session_info.password,
            )

        # The remote viewer
        self._viewer = RemoteViewer(central)
        self._viewer.fullscreen_toggled.connect(self._on_toggle_fullscreen)
        self._viewer.remote_mouse_event.connect(self._on_remote_mouse_event)
        self._viewer.remote_key_event.connect(self._on_remote_key_event)
        layout.addWidget(self._viewer, 1)

        self.setCentralWidget(central)

    def _setup_fullscreen_shortcuts(self) -> None:
        """Register global shortcuts that work even in fullscreen."""
        self._fs_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fs_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._fs_shortcut.activated.connect(self._on_toggle_fullscreen)

        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc_shortcut.activated.connect(self._on_exit_fullscreen)

    def _setup_statusbar(self) -> None:
        """Configure the status bar."""
        status = QStatusBar(self)

        self._session_status = SessionStatusWidget()
        status.addPermanentWidget(self._session_status)

        self._status_text = QLabel("Ready")
        status.addWidget(self._status_text, 1)

        # ── Caps Lock indicator ──
        self._caps_lock_label = QLabel("⇪")
        self._caps_lock_label.setStyleSheet(MainWindow._CAPS_OFF_STYLE)
        self._caps_lock_label.setVisible(False)
        status.addPermanentWidget(self._caps_lock_label)

        self._caps_timer = QTimer(self)
        self._caps_timer.timeout.connect(self._update_caps_lock)
        self._caps_timer.start(500)

        self.setStatusBar(status)

    # ── Relay / Networking ──────────────────────────────────────────

    def _get_relay_config(self) -> tuple[str, int]:
        """Read relay host and port from saved settings."""
        host = self._settings.value("network/relay_host", "")
        port = int(self._settings.value("network/relay_port", 8474))
        # Default to localhost if not configured
        if not host:
            host = "127.0.0.1"
        # 0.0.0.0 is a bind address, not a connect target
        if host == "0.0.0.0":
            host = "127.0.0.1"
        return host, port

    @Slot(str, str)
    def _on_session_refreshed(self, session_id: str, password: str) -> None:
        """Called when a new local session is created — start hosting on relay."""
        self._host_session_id = session_id.replace(" ", "")  # strip spaces from ID
        host, port = self._get_relay_config()
        logger.info(
            "Starting host on relay %s:%s with session %s", host, port, session_id
        )
        self._status_text.setText(f"Hosting: {session_id}")
        self._relay.start_hosting(host, port, self._host_session_id, password)

    @Slot(str, str)
    def _on_relay_connected(self, role: str, session_id: str) -> None:
        """Relay connection established."""
        logger.info("Relay connected as %s: %s", role, session_id)
        if role == "host":
            self._status_text.setText(f"Waiting for connection... (ID: {session_id})")
        elif role == "client":
            self._status_text.setText(f"Connected to {session_id}, authenticating...")

    @Slot()
    def _on_relay_disconnected(self) -> None:
        """Relay connection lost."""
        logger.info("Relay disconnected")
        self._stop_streaming()
        if self._connected:
            self._set_connected(False)
            self._status_text.setText("Disconnected")
            self._peer_id = ""

    @Slot()
    def _on_peer_joined(self) -> None:
        """A remote peer joined our session (host only)."""
        logger.info("Remote peer joined our session")
        self._status_text.setText("Authenticating remote peer...")

    @Slot()
    def _on_auth_requested(self) -> None:
        """Authentication was requested by host (client only)."""
        logger.debug("Auth requested by host")

    @Slot(bool, str)
    def _on_auth_result(self, success: bool, message: str) -> None:
        """Authentication result."""
        if success:
            logger.info("Authentication successful")
            self._status_text.setText("Authentication successful")
            if self._relay.role == RelayRole.HOST:
                # Host: start screen capture and streaming
                self._start_host_streaming()
            else:
                # Client: connected, waiting for video
                self._set_connected(True)
                self._status_text.setText(f"Session active: {self._peer_id}")
        else:
            logger.warning("Authentication failed: %s", message)
            QMessageBox.warning(
                self, "Authentication Failed",
                f"Failed to authenticate with the remote computer:\n{message}",
            )
            self._relay.disconnect()

    @Slot(np.ndarray, int, int)
    def _on_frame_received(self, rgb_data: np.ndarray, width: int, height: int) -> None:
        """A video frame was received from the remote host (client only)."""
        self._viewer.display_frame(rgb_data, width, height)

    @Slot(object)
    def _on_relay_message(self, msg: Message) -> None:
        """Handle an incoming relay message."""
        # Route messages from remote peer
        if msg.type == MessageType.MOUSE_EVENT and self._input_backend:
            self._inject_mouse(msg)
        elif msg.type == MessageType.KEYBOARD_EVENT and self._input_backend:
            self._inject_keyboard(msg)
        elif msg.type == MessageType.CHAT_MESSAGE:
            text = msg.payload.get("text", "")
            self._chat_panel.add_message("Remote", text, is_remote=True)
        elif msg.type == MessageType.DISCONNECT:
            self._on_disconnect()

    @Slot(str)
    def _on_relay_error(self, error_msg: str) -> None:
        """Relay error occurred."""
        logger.error("Relay error: %s", error_msg)
        QMessageBox.critical(self, "Connection Error", error_msg)
        self._on_disconnect()

    # ── Screen capture and streaming (host) ─────────────────────────

    def _start_host_streaming(self) -> None:
        """Start screen capture and send frames to the client."""
        try:
            self._capture = ScreenCapture()
            self._input_backend = create_input_backend()
            self._set_connected(True)
            self._status_text.setText("Streaming to remote client...")

            # Start streaming timer at target FPS
            fps = int(self._settings.value("video/max_fps", 30))
            self._stream_timer.start(int(1000 / fps))
            self._capture_running = True
            logger.info("Screen capture started at %d FPS", fps)
        except Exception as e:
            logger.exception("Failed to start screen capture: %s", e)
            QMessageBox.critical(
                self, "Capture Error",
                f"Failed to start screen capture:\n{e}",
            )
            self._on_disconnect()

    def _stop_streaming(self) -> None:
        """Stop screen capture and streaming."""
        self._stream_timer.stop()
        self._capture_running = False
        if self._capture:
            self._capture.release()
            self._capture = None
        if self._input_backend:
            self._input_backend.release()
            self._input_backend = None

    @Slot()
    def _capture_and_send_frame(self) -> None:
        """Capture a single frame and send it via relay (called by timer)."""
        if not self._capture_running or self._capture is None:
            return
        try:
            frame = self._capture.capture_one(0)
            if frame is not None:
                self._relay.send_frame(
                    frame.data, frame.width, frame.height,
                    int(frame.timestamp * 1000),
                )
        except Exception as e:
            logger.warning("Capture error: %s", e)

    # ── Input injection (host) ──────────────────────────────────────

    def _inject_mouse(self, msg: Message) -> None:
        """Inject a mouse event from the remote client."""
        if self._input_backend is None:
            return
        payload = msg.payload
        x = payload.get("x", 0)
        y = payload.get("y", 0)
        button = payload.get("button")
        pressed = payload.get("pressed")
        absolute = payload.get("absolute", True)

        if button is not None:
            btn = MouseButton(button)
            state = KeyState.PRESSED if pressed else KeyState.RELEASED
            self._input_backend.move_mouse(x, y, absolute)
            self._input_backend.click_mouse(btn, state)
        else:
            self._input_backend.move_mouse(x, y, absolute)

    def _inject_keyboard(self, msg: Message) -> None:
        """Inject a keyboard event from the remote client."""
        if self._input_backend is None:
            return
        payload = msg.payload
        key = payload.get("key", "")
        pressed = payload.get("pressed", False)
        state = KeyState.PRESSED if pressed else KeyState.RELEASED
        if key:
            self._input_backend.key_event(key, state)

    # ── Input forwarding (client) ───────────────────────────────────

    @Slot(int, int, int, bool, bool)
    def _on_remote_mouse_event(
        self, x: int, y: int, button: int, pressed: bool, absolute: bool
    ) -> None:
        """Forward a local mouse event to the remote host."""
        if self._relay.is_connected and self._relay.role == RelayRole.CLIENT:
            self._relay.send_mouse_event(x, y, button, pressed, absolute)

    @Slot(str, bool)
    def _on_remote_key_event(self, key: str, pressed: bool) -> None:
        """Forward a local keyboard event to the remote host."""
        if self._relay.is_connected and self._relay.role == RelayRole.CLIENT:
            self._relay.send_key_event(key, pressed)

    # ── Slots: session ──────────────────────────────────────────────

    @Slot()
    def _on_connect(self) -> None:
        """Open the connection dialog."""
        dialog = ConnectionDialog(self)
        dialog.connection_requested.connect(self._on_connection_requested)
        dialog.exec()

    @Slot(str, str)
    def _on_connection_requested(self, peer_id: str, password: str) -> None:
        """Handle a connection request from the dialog.

        Connects to the relay as a client and joins the remote session.
        """
        logger.info("Connection requested: peer=%s", peer_id)
        self._peer_id = peer_id
        self._status_text.setText(f"Connecting to {peer_id}...")

        # Normalise session ID (remove spaces)
        clean_id = peer_id.replace(" ", "")
        host, port = self._get_relay_config()
        self._relay.join_session(host, port, clean_id, password)

    @Slot()
    def _on_disconnect(self) -> None:
        """Disconnect the current session."""
        if not self._connected and not self._relay.is_connected:
            return

        logger.info("Disconnecting session: %s", self._peer_id)
        self._stop_streaming()
        self._relay.disconnect()
        self._set_connected(False)
        self._status_text.setText("Disconnected")
        self._peer_id = ""

    # ── Slots: view ─────────────────────────────────────────────────

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
        self._session_info.hide()
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
        self._session_info.show()
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
        if self._relay.is_connected:
            self._relay.send_message(Message.chat_message(text))

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

    @Slot()
    def _update_caps_lock(self) -> None:
        """Poll Caps Lock state and update the status bar indicator."""
        active = caps_lock_active()
        if active:
            self._caps_lock_label.setStyleSheet(MainWindow._CAPS_ON_STYLE)
        else:
            self._caps_lock_label.setStyleSheet(MainWindow._CAPS_OFF_STYLE)
        self._caps_lock_label.setVisible(active)

    def _set_connected(self, connected: bool) -> None:
        """Update all UI state to reflect connection status."""
        self._connected = connected
        self._viewer.set_connection_active(connected)

        # Enable/disable actions
        self.act_connect.setEnabled(not connected)
        self.act_disconnect.setEnabled(connected)

        # Update session status widget
        if connected:
            display_id = self._peer_id or self._host_session_id
            self._session_status.set_status(
                f"Connected to {display_id}", connected=True
            )
            self.setWindowTitle(f"OpenDesk — {display_id}")
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

        self._stop_streaming()
        self._relay.disconnect()
        logger.info("Application closing")
        event.accept()
