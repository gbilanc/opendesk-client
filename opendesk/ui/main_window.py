"""
Main application window.

Orchestrates the remote viewer, connection manager, toolbars,
and status bar.  Manages the overall connection lifecycle with
real P2P relay networking.
"""

from __future__ import annotations

import logging
import queue
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.clipboard_sync import ClipboardSync
from opendesk.core.file_transfer import FileTransferManager, TransferState
from opendesk.core.keyboard_state import caps_lock_active
from opendesk.core.platform_config import HealthSeverity, get_platform_config
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayRole
from opendesk.services.connection_service import ConnectionService
from opendesk.services.stream_service import StreamService
from opendesk.ui.chat_panel import ChatPanel
from opendesk.ui.connections import ConnectionPanel, SessionStatusWidget
from opendesk.ui.file_transfer_ui import FileBrowserDock
from opendesk.ui.session_info import SessionInfoWidget
from opendesk.ui.settings_dialog import SettingsDialog
from opendesk.ui.viewer import ViewerWindow

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level window for the OpenDesk application."""

    WINDOW_TITLE = "OpenDesk"
    MIN_WIDTH = 1024
    MIN_HEIGHT = 680

    # ── Caps Lock indicator styles ──
    _CAPS_ON_STYLE = "font-size: 12px; font-weight: 700; color: #dc2626;" " padding: 0 6px;"
    _CAPS_OFF_STYLE = "font-size: 12px; font-weight: 600; color: #94a3b8;" " padding: 0 6px;"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(1280, 800)

        # Application state
        self._connected: bool = False
        self._disconnecting: bool = False
        self._fullscreen: bool = False
        self._peer_id: str = ""  # remote session ID when acting as client
        self._file_transfer_mode: bool = False  # True when connecting for file-transfer-only

        # Settings
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # ── Services ──
        self._connection = ConnectionService(self)
        self._stream = StreamService(self._connection.relay, self)

        # Load audio/camera preferences from settings
        self._stream.audio_enabled = self._settings.value("audio/enabled", False, type=bool)
        self._stream.camera_enabled = self._settings.value("camera/enabled", False, type=bool)

        # Map service signals → existing handlers
        self._connection.connected.connect(self._on_relay_connected)
        self._connection.disconnected.connect(self._on_relay_disconnected)
        self._connection.peer_joined.connect(self._on_peer_joined)
        self._connection.peer_disconnected.connect(self._on_peer_disconnected)
        self._connection.auth_requested.connect(self._on_auth_requested)
        self._connection.host_auth_result.connect(self._on_host_auth_result)
        self._connection.client_auth_result.connect(self._on_client_auth_result)
        self._connection.frame_received.connect(self._on_frame_received)
        self._connection.message_received.connect(self._on_relay_message)
        self._connection.error.connect(self._on_relay_error)
        self._connection.device_list_received.connect(self._on_device_list_received)
        self._stream.error.connect(self._on_stream_error)
        self._stream.input_unavailable.connect(self._on_input_unavailable)

        # Backward-compatible aliases (delegate to services)
        self._relay = self._connection.relay
        self._auth_manager = self._connection.auth_manager
        self._device_id = self._connection.device_id
        self._device_name = self._connection.device_name
        self._host_session_id = ""  # kept locally for UI state
        self._device_registry = self._connection.device_registry

        # Device list from relay (cache)
        self._device_list_cache: list[dict] = []

        # Viewer window (separate window for remote desktop)
        self._viewer_window: ViewerWindow | None = None

        # File transfer manager
        self._file_transfer = FileTransferManager()
        self._file_transfer_send_fn = None  # set after connection

        # Poll the file-transfer updates queue from the background thread
        self._ft_update_timer = QTimer(self)
        self._ft_update_timer.timeout.connect(self._poll_file_transfer_updates)
        self._ft_update_timer.start(200)

        # Clipboard sync (instantiated but only started when enabled in settings)
        self._clipboard_sync = ClipboardSync(self)

        # Build UI
        self._setup_actions()
        self._setup_menus()
        self._setup_toolbar()
        self._setup_statusbar()

        # Mostra warning di piattaforma all'avvio (se ci sono criticità)
        QTimer.singleShot(500, self._check_platform_health_startup)
        self._setup_docks()
        self._setup_central_widget()
        self._setup_fullscreen_shortcuts()

        # Create initial session and start hosting
        # Use persisted password if available, otherwise generate one
        saved_pwd = self._settings.value("session/password", "")
        if not saved_pwd:
            import secrets
            import string

            alphabet = string.ascii_uppercase + string.digits
            saved_pwd = "".join(secrets.choice(alphabet) for _ in range(8))
            self._settings.setValue("session/password", saved_pwd)
        self._connection.create_session(saved_pwd)
        self._session_info.set_session(
            self._connection.session_id,
            self._connection.password,
        )
        self._host_session_id = self._connection.session_id.replace(" ", "")
        self._status_text.setText(f"Hosting: {self._connection.session_id.replace(' ', '')}")
        self._connection.start_hosting()

        logger.info("Main window initialised")

    # ── Initialisation ──────────────────────────────────────────────

    def _setup_actions(self) -> None:
        """Create reusable QAction objects."""
        # ── Session ──
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

        # ── Media toggles ──
        self.act_toggle_mic = QAction("🎤 Mic", self)
        self.act_toggle_mic.setCheckable(True)
        self.act_toggle_mic.setChecked(self._stream.audio_enabled)
        self.act_toggle_mic.setStatusTip("Toggle microphone streaming")
        self.act_toggle_mic.triggered.connect(self._on_toggle_mic)

        self.act_toggle_camera = QAction("📷 Camera", self)
        self.act_toggle_camera.setCheckable(True)
        self.act_toggle_camera.setChecked(self._stream.camera_enabled)
        self.act_toggle_camera.setStatusTip("Toggle webcam streaming")
        self.act_toggle_camera.triggered.connect(self._on_toggle_camera)

        # ── File ──
        self.act_send_file = QAction("&Send File...", self)
        self.act_send_file.setShortcut(QKeySequence("Ctrl+F"))
        self.act_send_file.setStatusTip("Send a file to the remote peer")
        self.act_send_file.setEnabled(False)
        self.act_send_file.triggered.connect(self._on_send_file)

        # ── Help ──
        self.act_about = QAction("&About OpenDesk", self)
        self.act_about.triggered.connect(self._on_about)

    def _setup_menus(self) -> None:
        """Build the menu bar."""
        menubar = self.menuBar()
        assert menubar is not None

        # ── Session ──
        session_menu = menubar.addMenu("&Session")
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

        # ── Tools ──
        tools_menu = menubar.addMenu("&Tools")
        tools_menu.addAction(self.act_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self.act_send_file)

        # ── Help ──
        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self.act_about)

    def _setup_toolbar(self) -> None:
        """Create the main navigation toolbar."""
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(20, 20))
        toolbar.setStyleSheet(
            """
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
        """
        )

        toolbar.addSeparator()
        toolbar.addAction(self.act_fit)
        toolbar.addAction(self.act_zoom_in)
        toolbar.addAction(self.act_zoom_out)
        toolbar.addSeparator()
        toolbar.addAction(self.act_fullscreen)
        toolbar.addSeparator()
        toolbar.addAction(self.act_toggle_mic)
        toolbar.addAction(self.act_toggle_camera)

        self.addToolBar(toolbar)

    def _setup_docks(self) -> None:
        """Create floating windows for chat and file transfers."""
        # ── Chat window ──
        self._chat_panel = ChatPanel(self)
        self._chat_panel.message_sent.connect(self._on_chat_message_sent)

        # ── File transfers window (created lazily on first use) ──
        self._transfer_dock: FileBrowserDock | None = None

    def _setup_central_widget(self) -> None:
        """Build the central area with session info + remote viewer."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Session info bar (shows your device ID + session)
        self._session_info = SessionInfoWidget(
            self._auth_manager,
            device_id=self._device_id,
            device_name=self._device_name,
            parent=central,
        )
        self._session_info.session_refreshed.connect(self._on_session_refreshed)
        self._session_info.device_name_changed.connect(self._on_device_name_changed)
        layout.addWidget(self._session_info)

        # Connection panel (device list + manual entry)
        self._connection_panel = ConnectionPanel(
            device_registry=self._device_registry,
            local_device_id=self._device_id,
            parent=central,
        )
        self._connection_panel.connection_requested.connect(self._on_connection_requested)
        self._connection_panel.file_transfer_requested.connect(self._on_file_transfer_requested)
        self._connection_panel.chat_toggled.connect(self._on_chat_toggled)
        self._connection_panel.disconnect_requested.connect(self._on_disconnect)
        layout.addWidget(self._connection_panel, 1)

        self.setCentralWidget(central)

    def _setup_fullscreen_shortcuts(self) -> None:
        """Register global shortcuts that work even in fullscreen."""
        self._fs_shortcut = QShortcut(QKeySequence("F11"), self)
        self._fs_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._fs_shortcut.activated.connect(self._on_toggle_fullscreen)

        self._esc_shortcut = QShortcut(QKeySequence("Escape"), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self._esc_shortcut.activated.connect(self._on_exit_fullscreen_in_viewer)

    def _on_exit_fullscreen_in_viewer(self) -> None:
        """Exit fullscreen in the viewer window if it's active."""
        if self._viewer_window and self._viewer_window.isFullScreen():
            self._viewer_window._toggle_fullscreen()

    # ── Platform health ────────────────────────────────────────────

    def _check_platform_health_startup(self) -> None:
        """Show a toast notification if critical issues are detected at startup."""
        from opendesk.ui.widgets.toast_notification import ToastNotification

        cfg = get_platform_config()
        issues = cfg.check_health()

        critical = [i for i in issues if i.severity == HealthSeverity.CRITICAL]
        warnings = [i for i in issues if i.severity == HealthSeverity.WARNING]

        if critical:
            msg = f"🔴 {len(critical)} problema{'i' if len(critical) > 1 else ''} critico{'i' if len(critical) > 1 else ''}: "
            msg += ", ".join(c.message.split(".")[0] for c in critical[:2])
            ToastNotification(self, msg, ToastNotification.Type.ERROR, duration_ms=6000).show()
        elif warnings:
            msg = f"🟡 {len(warnings)} avviso{'i' if len(warnings) > 1 else ''}: "
            msg += ", ".join(w.message.split(".")[0] for w in warnings[:2])
            ToastNotification(self, msg, ToastNotification.Type.WARNING, duration_ms=5000).show()

        # Aggiorna il widget health nella status bar
        if hasattr(self, "_health_widget"):
            self._health_widget._refresh()

    def _show_health_details(self) -> None:
        """Show a dialog with full health details."""
        from PySide6.QtWidgets import QMessageBox

        cfg = get_platform_config()
        issues = cfg.check_health()

        msg = f"<b>Piattaforma: {cfg.display_name}</b><br><br>"

        if not issues:
            msg += "✅ Nessun problema rilevato."
        else:
            for i in issues:
                icon = {
                    HealthSeverity.CRITICAL: "🔴",
                    HealthSeverity.WARNING: "🟡",
                    HealthSeverity.INFO: "ℹ️",
                }[i.severity]
                msg += f"{icon} <b>{i.component}</b>: {i.message}<br>"
                if i.fix:
                    msg += f"&nbsp;&nbsp;&nbsp;→ {i.fix}<br>"
                msg += "<br>"

        QMessageBox.information(self, "Stato piattaforma", msg)

    def _setup_statusbar(self) -> None:
        """Configure the status bar."""
        status = QStatusBar(self)

        # ── Health indicator (piattaforma) ──
        from opendesk.ui.widgets.health_status import HealthStatusWidget

        self._health_widget = HealthStatusWidget(self)
        self._health_widget.setToolTip("Stato piattaforma — click per dettagli")
        self._health_widget.mousePressEvent = lambda e: self._show_health_details()  # type: ignore[assignment]
        status.addPermanentWidget(self._health_widget)

        self._session_status = SessionStatusWidget()
        status.addPermanentWidget(self._session_status)

        self._status_text = QLabel("Ready")
        status.addWidget(self._status_text, 1)

        # ── Media status indicators ──
        self._mic_indicator = QLabel("Mic Off")
        self._mic_indicator.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #94a3b8; padding: 0 6px;"
        )
        self._mic_indicator.setVisible(False)
        status.addPermanentWidget(self._mic_indicator)

        self._camera_indicator = QLabel("Cam Off")
        self._camera_indicator.setStyleSheet(
            "font-size: 12px; font-weight: 600; color: #94a3b8; padding: 0 6px;"
        )
        self._camera_indicator.setVisible(False)
        status.addPermanentWidget(self._camera_indicator)

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

    @Slot(str, str)
    def _on_session_refreshed(self, session_id: str, password: str) -> None:
        """Called when user clicks 'Nuova sessione' — create new session and re-host."""
        self._connection.create_session(password)
        self._settings.setValue("session/password", password)
        self._session_info.set_session(
            self._connection.session_id,
            self._connection.password,
        )
        self._host_session_id = self._connection.session_id.replace(" ", "")
        logger.info("New session created: %s", self._connection.session_id)
        self._status_text.setText(f"Hosting: {self._host_session_id}")
        self._connection.start_hosting()

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
        """Relay connection lost (host or client session).

        Host and client sessions are now independent.  This handler
        checks ``is_hosting()`` to know which session disconnected:
        - If still hosting → only the client session dropped
        - If not hosting → the host session dropped (schedule retry)
        """
        was_hosting = self._connection.is_hosting
        logger.info(
            "Relay disconnected (was_hosting=%s, host_session=%s)",
            was_hosting,
            self._host_session_id,
        )
        self._clipboard_sync.stop()
        self.act_send_file.setEnabled(False)
        self._file_transfer_send_fn = None
        if self._transfer_dock is not None:
            self._transfer_dock.set_connected(False)
            self._transfer_dock.set_status("Disconnected — remote browsing unavailable")
        self._stop_streaming()
        self._set_connected(False)
        self._peer_id = ""

        if not was_hosting and self._host_session_id:
            # Host connection dropped unexpectedly — auto-retry
            self._status_text.setText("⚠ Relay disconnected — reconnecting...")
            self._connection.schedule_retry(lambda m: self._status_text.setText(m))
        else:
            self._status_text.setText("Disconnected")

    @Slot()
    def _on_peer_joined(self) -> None:
        logger.info("Remote peer joined our session")
        self._status_text.setText("Authenticating remote peer...")

    @Slot()
    def _on_peer_disconnected(self) -> None:
        """Remote peer (client) disconnected from our hosted session."""
        logger.info("Remote peer disconnected from our session")
        self._stop_streaming()
        self._set_connected(False)
        self._status_text.setText("Remote client disconnected — waiting for new connections")

    @Slot()
    def _on_auth_requested(self) -> None:
        logger.debug("Auth requested by host")

    @Slot(bool, str)
    def _on_host_auth_result(self, success: bool, message: str) -> None:
        """Authentication result for a REMOTE client connecting to US (host)."""
        if success:
            # Check if the client connected in file-transfer-only mode
            client_mode = self._connection.client_connection_mode
            is_ft_only = client_mode == "file_transfer"
            logger.info(
                "Remote client authenticated (mode=%s)",
                client_mode,
            )
            self._status_text.setText("Authentication successful")

            if not is_ft_only:
                # Full desktop mode — start streaming
                self._start_host_streaming()
            else:
                # File-transfer-only mode — skip streaming
                self._status_text.setText("File transfer session active")

            # Enable file transfer (always, regardless of mode)
            self.act_send_file.setEnabled(True)
            self._file_transfer_send_fn = self._send_file_message
            if self._transfer_dock is not None:
                self._transfer_dock.set_connected(True)

            # Start clipboard sync if enabled in settings
            if self._settings.value("general/clipboard_sync", False, type=bool):
                self._clipboard_sync.start(self._send_clipboard_message)

            # Request initial remote file listing
            if self._file_transfer_send_fn:
                self._file_transfer.request_remote_listing("/", self._file_transfer_send_fn)
        else:
            logger.warning("Client authentication failed: %s", message)
            self._status_text.setText("Client authentication failed")

    @Slot(bool, str)
    def _on_client_auth_result(self, success: bool, message: str) -> None:
        """Authentication result for US connecting to a remote host (client)."""
        if success:
            logger.info("We authenticated to remote host")
            self._status_text.setText("Authentication successful")
            self._set_connected(True, show_viewer=not self._file_transfer_mode)
            # Start frame timeout watchdog only after successful auth
            if self._viewer_window is not None:
                self._viewer_window.set_connection_active(True, self._peer_id)
            status_msg = (
                "File transfer session active"
                if self._file_transfer_mode
                else f"Session active: {self._peer_id}"
            )
            self._status_text.setText(status_msg)

            # Enable file transfer
            self.act_send_file.setEnabled(True)
            self._file_transfer_send_fn = self._send_file_message
            if self._transfer_dock is not None:
                self._transfer_dock.set_connected(True)

            # Start clipboard sync if enabled in settings
            if self._settings.value("general/clipboard_sync", False, type=bool):
                self._clipboard_sync.start(self._send_clipboard_message)

            # Request initial remote file listing
            if self._file_transfer_send_fn:
                self._file_transfer.request_remote_listing("/", self._file_transfer_send_fn)

            # If file-transfer-only, open the file transfer dock now
            if self._file_transfer_mode:
                self._show_file_transfer_dock()
                self._file_transfer_mode = False  # reset for next connection
        else:
            logger.warning("Authentication failed: %s", message)
            self._file_transfer_mode = False
            QMessageBox.warning(
                self,
                "Authentication Failed",
                f"Failed to authenticate with the remote computer:\n{message}",
            )
            self._connection.disconnect_client()

    @Slot(np.ndarray, int, int)
    def _on_frame_received(self, rgb_data: np.ndarray, width: int, height: int) -> None:
        if self._viewer_window is not None:
            self._viewer_window.display_frame(rgb_data, width, height)

    @Slot()
    def _on_frame_timeout(self) -> None:
        logger.warning("Frame timeout — no video frame received for 10 seconds")
        # Don't disconnect — the relay/p2p connection may still be
        # healthy; only the video stream is stalled.
        self._status_text.setText("⚠ No video frames — connection may be stalled")
        self._stop_streaming()

    @Slot(str)
    def _on_stream_error(self, error_msg: str) -> None:
        """Stream error (from StreamService).

        Log the error but do NOT disconnect — the relay connection
        stays alive so the peer doesn't get a spurious "Peer
        disconnected" error.  The host can retry streaming later.
        """
        logger.error("Stream error: %s", error_msg)
        self._status_text.setText(f"⚠ Streaming error: {error_msg}")
        self._stop_streaming()

    @Slot(str)
    def _on_input_unavailable(self, error_msg: str) -> None:
        """Non-fatal: input backend could not start (e.g. uinput missing).

        Shows a warning in the status bar but does NOT stop streaming.
        """
        logger.warning("Input backend unavailable: %s", error_msg)
        self._status_text.setText(f"⚠ Remote input disabled: {error_msg}")

    @Slot(object)
    def _on_relay_message(self, msg: Message) -> None:
        """Handle an incoming relay message."""
        if msg.type == MessageType.MOUSE_EVENT and self._stream.input_backend:
            self._inject_mouse(msg)
        elif msg.type == MessageType.KEYBOARD_EVENT and self._stream.input_backend:
            self._inject_keyboard(msg)
        elif msg.type == MessageType.CHAT_MESSAGE:
            text = msg.payload.get("text", "")
            self._chat_panel.add_message("Remote", text, is_remote=True)
        elif msg.type == MessageType.CHAT_OPEN:
            is_open = msg.payload.get("open", False)
            if is_open:
                if not self._chat_panel.isVisible():
                    self._chat_panel.show()
                    self._chat_panel.raise_()
                    self._chat_panel.activateWindow()
            else:
                if self._chat_panel.isVisible():
                    self._chat_panel.hide()
        elif msg.type == MessageType.AUDIO_FRAME:
            data = msg.payload.get("data", b"")
            if data and self._stream.audio_enabled:
                self._stream.play_audio_frame(data)
        elif msg.type == MessageType.CAMERA_FRAME:
            data = msg.payload.get("data", b"")
            if data and self._stream.camera_enabled:
                if self._viewer_window and self._viewer_window.isVisible():
                    self._viewer_window.viewer.update_camera_frame(data)
                    self._viewer_window.viewer.set_camera_active(True)
        elif msg.type == MessageType.CAMERA_START:
            enabled = msg.payload.get("enabled", False)
            if self._viewer_window:
                self._viewer_window.viewer.set_camera_active(enabled)
                if not enabled:
                    self._viewer_window.viewer.update_camera_frame(b"")
        elif msg.type in (MessageType.CLIPBOARD_TEXT, MessageType.CLIPBOARD_IMAGE):
            if self._clipboard_sync.enabled:
                self._clipboard_sync.receive_from_remote(msg)
        elif msg.type == MessageType.FILE_REQUEST:
            job_id = self._file_transfer.handle_file_request(msg)
            job = self._file_transfer.get_job(job_id) if job_id else None
            if job is None:
                return
            destination = job.file_info.path or str(Path.home() / "Downloads" / "OpenDesk")
            reply = QMessageBox.question(
                self,
                "Incoming file transfer",
                f"Accept '{job.file_info.name}' ({job.file_info.size:,} bytes)\n"
                f"Destination: {destination}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.Yes and self._file_transfer.accept_incoming(
                job.id
            ):
                self._relay.send_message(Message.file_accept(job.id))
            else:
                reason = job.error or "Rejected by local user"
                self._file_transfer.reject_incoming(job.id, reason)
                self._relay.send_message(Message.file_reject(job.id, reason))
        elif msg.type == MessageType.FILE_CHUNK:
            self._file_transfer.handle_chunk(msg)
            job_id = msg.payload.get("job_id", "")
            if job_id:
                job = self._file_transfer.get_job(job_id)
                if job and self._transfer_dock is not None:
                    self._transfer_dock.add_transfer(job)
        elif msg.type == MessageType.FILE_ACCEPT:
            job_id = msg.payload.get("job_id", "")
            job = self._file_transfer.get_job(job_id)
            if job and self._file_transfer_send_fn:
                self._file_transfer.send_chunks(job, self._file_transfer_send_fn)
        elif msg.type == MessageType.FILE_REJECT:
            job_id = msg.payload.get("job_id", "")
            reason = msg.payload.get("reason", "Rejected by remote")
            job = self._file_transfer.get_job(job_id)
            if job:
                job.state = TransferState.CANCELLED
                job.error = reason
                if self._transfer_dock is not None:
                    self._transfer_dock.add_transfer(job)
        elif msg.type == MessageType.FILE_COMPLETE:
            self._file_transfer.handle_file_complete(msg)
            job = self._file_transfer.get_job(msg.payload.get("job_id", ""))
            if job and self._transfer_dock is not None:
                self._transfer_dock.add_transfer(job)
        elif msg.type == MessageType.FILE_ERROR:
            job = self._file_transfer.get_job(msg.payload.get("job_id", ""))
            if job:
                job.state = TransferState.FAILED
                job.error = msg.payload.get("error", "Unknown error")
                if self._transfer_dock is not None:
                    self._transfer_dock.add_transfer(job)
        elif msg.type == MessageType.FILE_LIST_REQUEST:
            # Remote peer wants a directory listing — respond
            response = self._file_transfer.handle_list_request(msg)
            if self._file_transfer_send_fn:
                self._file_transfer_send_fn(response)
        elif msg.type == MessageType.FILE_LIST_RESPONSE:
            # Remote directory listing received
            self._file_transfer.handle_list_response(msg)
        elif msg.type == MessageType.FILE_DOWNLOAD_REQUEST:
            # Remote peer wants to download a file from us
            if self._file_transfer_send_fn:
                self._file_transfer.handle_download_request(msg, self._file_transfer_send_fn)
        elif msg.type == MessageType.FILE_DOWNLOAD_ACCEPT:
            # Remote peer accepted our download request
            self._file_transfer.handle_download_accept(msg)
        elif msg.type == MessageType.FILE_DOWNLOAD_REJECT:
            # Remote peer rejected our download request
            self._file_transfer.handle_download_reject(msg)
        elif msg.type == MessageType.DISCONNECT:
            self._on_disconnect()

    @Slot(str)
    def _on_relay_error(self, error_msg: str) -> None:
        """Relay error occurred (host or client).

        Note: "Peer disconnected" errors are now handled via the
        ``peer_disconnected`` signal and should NOT arrive here.
        This is a defensive fallback.
        """
        # Defensive fallback: "Peer disconnected" should be routed via
        # ``peer_disconnected`` signal at a lower level, but just in case
        # it leaks through, handle it gracefully.
        if "Peer disconnected" in error_msg:
            logger.info("Peer disconnected (via fallback error handler)")
            self._stop_streaming()
            self._set_connected(False)
            self._status_text.setText("Remote client disconnected — waiting for connections")
            return

        logger.error("Relay error: %s", error_msg)
        # If we have an active client session, this is likely a client error
        if self._connection.role == RelayRole.CLIENT:
            self._hide_viewer_window()
            QMessageBox.critical(self, "Connection Error", error_msg)
            self._connection.disconnect_client()
        elif self._connection.is_hosting:
            # Host error: informational — do NOT retry. The host session
            # is still alive and can accept new clients.
            self._status_text.setText(f"⚠ {error_msg}")
        else:
            self._status_text.setText(f"⚠ Error: {error_msg}")

    @Slot(list)
    def _on_device_list_received(self, devices: list[dict]) -> None:
        logger.debug("Device list received: %d devices", len(devices))
        self._device_list_cache = devices
        self._device_registry.merge_from_relay(devices)
        self._connection_panel.update_device_list(self._device_registry.all())

    @Slot(str)
    def _on_device_name_changed(self, new_name: str) -> None:
        self._connection.device_name = new_name
        logger.info("Device name changed to: %s", new_name)

    # ── Screen capture and streaming (host) ─────────────────────────

    def _show_viewer_window(self, peer_name: str = "") -> None:
        """Create and show the remote viewer window."""
        if self._viewer_window is None:
            self._viewer_window = ViewerWindow(
                on_mouse_event=self._on_remote_mouse_event,
                on_key_event=self._on_remote_key_event,
                on_disconnect=self._on_disconnect,
                on_mic_toggle=self._on_toggle_mic,
                on_camera_toggle=self._on_toggle_camera,
                parent=self,
            )
            self._viewer_window.frame_timeout.connect(self._on_frame_timeout)
            # Apply sharp text mode from settings
            sharp_text = self._settings.value("video/sharp_text_viewer", True, type=bool)
            self._viewer_window.set_sharp_text(sharp_text)
            # Sync mic/camera button state
            self._viewer_window.set_mic_checked(self._stream.audio_enabled)
            self._viewer_window.set_camera_checked(self._stream.camera_enabled)
        self._viewer_window.show()
        self._viewer_window.raise_()
        self._viewer_window.activateWindow()

    def _hide_viewer_window(self) -> None:
        """Hide the remote viewer window."""
        if self._viewer_window is not None:
            self._viewer_window.set_connection_active(False)
            self._viewer_window.hide()

    def _start_host_streaming(self) -> None:
        """Start screen capture and send frames to the client.

        Called on the HOST side when the remote peer authenticates.
        We update the UI to reflect the active session and start
        streaming screen frames TO the client.  Unlike the client
        side, we do NOT open a ViewerWindow here — the client is
        the one viewing our screen.
        """
        logger.info("Host streaming: auth succeeded, client connected")
        self._connected = True
        self.act_disconnect.setEnabled(True)
        self._session_status.set_status(
            "Streaming to client",
            connected=True,
        )
        self.setWindowTitle("OpenDesk — Streaming")
        self._status_text.setText("Streaming to remote client...")
        self._stream.start_streaming()

    def _stop_streaming(self) -> None:
        """Stop screen capture and streaming."""
        self._stream.stop_streaming()

    # ── Media toggles ──────────────────────────────────────────────────

    @Slot(bool)
    def _on_toggle_mic(self, checked: bool) -> None:
        """Toggle microphone streaming on/off."""
        self._stream.audio_enabled = checked
        if checked:
            self._stream._start_audio_capture()
            self._mic_indicator.setText("Mic On")
            self._mic_indicator.setStyleSheet(
                "font-size: 12px; font-weight: 700; color: #22c55e; padding: 0 6px;"
            )
        else:
            self._stream._stop_audio_capture()
            self._mic_indicator.setText("Mic Off")
            self._mic_indicator.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: #94a3b8; padding: 0 6px;"
            )
        self._mic_indicator.setVisible(True)
        # Sync viewer toolbar button state
        if self._viewer_window:
            self._viewer_window.set_mic_checked(checked)
        self.act_toggle_mic.setChecked(checked)
        logger.info("Microphone toggled: %s", "ON" if checked else "OFF")

    @Slot(bool)
    def _on_toggle_camera(self, checked: bool) -> None:
        """Toggle webcam streaming on/off."""
        self._stream.camera_enabled = checked
        # Update indicator
        if checked:
            self._stream._start_camera_capture()
            self._camera_indicator.setText("Cam On")
            self._camera_indicator.setStyleSheet(
                "font-size: 12px; font-weight: 700; color: #22c55e; padding: 0 6px;"
            )
        else:
            self._stream._stop_camera_capture()
            self._camera_indicator.setText("Cam Off")
            self._camera_indicator.setStyleSheet(
                "font-size: 12px; font-weight: 600; color: #94a3b8; padding: 0 6px;"
            )

        # Notify remote peer
        if self._connection.relay.is_connected:
            self._connection.relay.send_message(
                Message(MessageType.CAMERA_START, {"enabled": checked})
            )
        self._camera_indicator.setVisible(True)
        # Sync viewer toolbar button state and overlay visibility
        if self._viewer_window:
            self._viewer_window.set_camera_checked(checked)
            self._viewer_window.viewer.set_camera_active(checked)
        self.act_toggle_camera.setChecked(checked)
        logger.info("Camera toggled: %s", "ON" if checked else "OFF")

    # ── Input injection (host) ──────────────────────────────────────

    def _inject_mouse(self, msg: Message) -> None:
        """Inject a mouse event from the remote client."""
        self._stream.inject_mouse(msg)

    def _inject_keyboard(self, msg: Message) -> None:
        """Inject a keyboard event from the remote client."""
        self._stream.inject_keyboard(msg)

    # ── Input forwarding (client) ───────────────────────────────────

    @Slot(int, int, int, bool, bool)
    def _on_remote_mouse_event(
        self, x: int, y: int, button: int, pressed: bool, absolute: bool
    ) -> None:
        if self._relay.is_connected and self._relay.role == RelayRole.CLIENT:
            logger.debug(
                "Sending MOUSE_EVENT: x=%d y=%d button=%s pressed=%s abs=%s",
                x,
                y,
                button,
                pressed,
                absolute,
            )
            self._relay.send_mouse_event(x, y, button, pressed, absolute)

    @Slot(str, bool)
    def _on_remote_key_event(self, key: str, pressed: bool) -> None:
        if self._relay.is_connected and self._relay.role == RelayRole.CLIENT:
            self._relay.send_key_event(key, pressed)

    # ── Slots: session ──────────────────────────────────────────────

    def connect_to(self, peer_id: str, password: str) -> None:
        """Public method to connect to a remote session programmatically.

        Can be called from CLI auto-connect or external scripts.
        """
        logger.info("Programmatic connect: peer=%s", peer_id)
        self._file_transfer_mode = False
        self._peer_id = peer_id
        self._status_text.setText(f"Connecting to {peer_id}...")
        self._connection.join_session(peer_id, password)
        self._show_viewer_window(peer_name=peer_id)

    @Slot(str, str)
    def _on_connection_requested(self, peer_id: str, password: str) -> None:
        """Handle a connection request from the dialog."""
        logger.info("Connection requested: peer=%s", peer_id)
        self._file_transfer_mode = False
        self._peer_id = peer_id
        self._status_text.setText(f"Connecting to {peer_id}...")
        self._connection.join_session(peer_id, password)
        self._show_viewer_window(peer_name=peer_id)

    @Slot(str, str)
    def _on_file_transfer_requested(self, peer_id: str, password: str) -> None:
        """Handle a file-transfer-only connection request."""
        logger.info("File transfer requested: peer=%s", peer_id)
        self._file_transfer_mode = True
        self._peer_id = peer_id
        self._status_text.setText(f"Connecting to {peer_id} (file transfer)...")
        self._connection.join_session(peer_id, password, connection_mode="file_transfer")

    @Slot()
    def _on_disconnect(self) -> None:
        """Disconnect the current session.

        Behaviour depends on the active role:
        - If a CLIENT session is active (we're viewing a remote screen),
          only that session is disconnected; hosting persists.
        - If we're HOSTING with a remote client connected,
          stop hosting to disconnect the remote peer; the host session
          auto-retries to accept new connections.
        - If neither, this is a no-op.
        """
        if not self._connected and not self._relay.is_connected:
            return
        if self._disconnecting:
            return
        self._disconnecting = True
        try:
            logger.info("Disconnecting session")
            self._stop_streaming()

            if self._connection.role == RelayRole.CLIENT:
                # We're the client — disconnect from the remote host
                logger.info("Disconnecting client session: %s", self._peer_id)
                self._connection.disconnect_client()
            elif self._connection.is_hosting and self._connected:
                # We're hosting and a client was connected — stop hosting
                # so the relay disconnects the remote peer, then auto-retry
                # reconnects the host session for new clients.
                logger.info("Stopping host session to disconnect remote client")
                self._connection.stop_hosting()

            self._set_connected(False)
            self._status_text.setText("Disconnected")
            self._peer_id = ""
        finally:
            self._disconnecting = False

    # ── Slots: view ─────────────────────────────────────────────────

    @Slot()
    def _on_toggle_fullscreen(self) -> None:
        """Toggle fullscreen mode on the viewer window."""
        if self._viewer_window:
            self._viewer_window._toggle_fullscreen()

    @Slot()
    def _on_fit_view(self) -> None:
        """Fit the remote screen to the window."""
        if self._viewer_window:
            self._viewer_window.viewer.zoom_to_fit()

    @Slot()
    def _on_zoom_in(self) -> None:
        """Zoom into the remote screen."""
        if self._viewer_window:
            self._viewer_window.viewer.zoom_in()

    @Slot()
    def _on_zoom_out(self) -> None:
        """Zoom out of the remote screen."""
        if self._viewer_window:
            self._viewer_window.viewer.zoom_out()

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
    def _on_send_file(self) -> None:
        """Open a file dialog and send the selected file(s)."""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select files to send",
            "",
            "All files (*)",
        )
        if not paths or not self._file_transfer_send_fn:
            return
        self._file_transfer.send_files(paths, self._file_transfer_send_fn)
        logger.info("File transfer initiated: %d files", len(paths))

    @Slot()
    def _on_settings(self) -> None:
        """Open the settings dialog."""
        dialog = SettingsDialog(
            device_registry=self._device_registry,
            parent=self,
        )
        dialog.exec()

    # ── Slots: window ──────────────────────────────────────────────

    @Slot()
    def _on_chat_toggled(self) -> None:
        """Show/hide the chat window and notify the remote peer."""
        if self._chat_panel.isVisible():
            self._chat_panel.hide()
            self._send_chat_open_notification(False)
        else:
            self._chat_panel.show()
            self._chat_panel.raise_()
            self._chat_panel.activateWindow()
            self._send_chat_open_notification(True)

    def _send_chat_open_notification(self, is_open: bool) -> None:
        """Send CHAT_OPEN notification to the remote peer."""
        if self._relay.is_connected:
            self._relay.send_message(Message.chat_open(is_open))

    @Slot()
    def _ensure_transfer_dock(self) -> FileBrowserDock:
        """Create and return the file transfer dock (lazy init).

        The dock is created on first use to avoid potential segfaults
        from ``QFileSystemModel`` setup during ``MainWindow.__init__``.
        """
        if self._transfer_dock is None:
            self._transfer_dock = FileBrowserDock(self)
            self._transfer_dock.file_upload_requested.connect(self._on_browser_upload)
            self._transfer_dock.file_download_requested.connect(self._on_browser_download)
            self._transfer_dock.remote_listing_requested.connect(self._on_browser_remote_listing)
        return self._transfer_dock

    def _send_file_message(self, msg: Message) -> None:
        """Send a message via the relay (used as send_fn by FileTransferManager).

        This is a synchronous wrapper — ``RelayClient.send_message`` is
        thread-safe and uses ``run_coroutine_threadsafe`` internally.
        """
        self._relay.send_message(msg)

    def _send_clipboard_message(self, msg: Message) -> None:
        """Send a clipboard sync message over the relay."""
        if self._relay.is_connected:
            self._relay.send_message(msg)

    @Slot(str)
    def _on_chat_message_sent(self, text: str) -> None:
        logger.debug("Chat message sent: %s", text)
        if self._relay.is_connected:
            self._relay.send_message(Message.chat_message(text))

    # ── File transfer browser handlers ──────────────────────────────

    @Slot(list, str)
    def _on_browser_upload(self, paths: list[str], remote_dest: str = "/") -> None:
        """Upload selected local files to the remote peer.

        Parameters
        ----------
        remote_dest : str
            Remote directory where the file should be saved.
        """
        if not paths or not self._file_transfer_send_fn:
            return
        self._file_transfer.send_files(
            paths,
            self._file_transfer_send_fn,
            remote_dest_path=remote_dest,
        )
        logger.info("Upload requested: %d files to %s", len(paths), remote_dest)

    @Slot(list, str)
    def _on_browser_download(self, remote_paths: list[str], local_dest: str = "") -> None:
        """Download selected remote files to the local folder.

        Parameters
        ----------
        local_dest : str
            Local directory where the downloaded file should be saved.
        """
        if not remote_paths or not self._file_transfer_send_fn:
            return
        for rpath in remote_paths:
            self._file_transfer.request_download(
                rpath,
                self._file_transfer_send_fn,
                local_dest=local_dest,
            )
        logger.info("Download requested: %d files to %s", len(remote_paths), local_dest)

    @Slot(str)
    def _on_browser_remote_listing(self, path: str) -> None:
        """Request a remote directory listing."""
        if self._file_transfer_send_fn:
            self._file_transfer.request_remote_listing(path, self._file_transfer_send_fn)

    def _poll_file_transfer_updates(self) -> None:
        """Poll the FileTransferManager updates queue (runs on main thread via QTimer)."""
        try:
            while True:
                event = self._file_transfer.updates.get_nowait()
                kind = event[0]

                if kind == "transfer":
                    job_id = event[1]
                    job = self._file_transfer.get_job(job_id)
                    if job and self._transfer_dock is not None:
                        self._transfer_dock.add_transfer(job)

                elif kind == "listing":
                    path = event[1]
                    entries = event[2]
                    error = event[3]
                    if self._transfer_dock is not None:
                        self._transfer_dock.set_remote_listing(path, entries, error)

                elif kind == "status":
                    message = event[1]
                    if self._transfer_dock is not None:
                        self._transfer_dock.set_status(message)

        except queue.Empty:
            pass

    # ── Slots: help ─────────────────────────────────────────────────

    @Slot()
    def _on_about(self) -> None:
        """Show the About dialog."""
        QMessageBox.about(
            self,
            "About OpenDesk",
            "<h3>OpenDesk v1.0.0</h3>"
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

    def _show_file_transfer_dock(self) -> None:
        """Show the file transfer dock and bring it to front."""
        dock = self._ensure_transfer_dock()
        dock.show()
        dock.raise_()
        dock.activateWindow()
        # Refresh remote listing now that we're connected
        if self._file_transfer_send_fn:
            dock.set_connected(True)
            dock.set_status("Connected — browsing remote files...")
            self._file_transfer.request_remote_listing("/", self._file_transfer_send_fn)

    def _set_connected(self, connected: bool, show_viewer: bool = True) -> None:
        """Update all UI state to reflect connection status.

        Parameters
        ----------
        connected : bool
            Whether we are connected.
        show_viewer : bool
            Whether to show the remote desktop viewer window.
            Pass ``False`` for file-transfer-only connections.
        """
        self._connected = connected

        # Enable/disable actions
        self.act_disconnect.setEnabled(connected)

        # Show/hide viewer window
        if connected and self._viewer_window is None and show_viewer:
            self._show_viewer_window()
        elif not connected:
            self._hide_viewer_window()

        # Enable/disable Chat button based on connection state
        self._connection_panel.set_connected(connected)

        # Update session status widget
        if connected:
            display_id = self._peer_id or self._host_session_id
            self._session_status.set_status(f"Connected to {display_id}", connected=True)
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
        self._connection.disconnect()
        logger.info("Application closing")
        event.accept()
