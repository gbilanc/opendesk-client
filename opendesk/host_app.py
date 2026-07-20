"""
OpenDesk Host — versione solo incoming.

Si connette al relay come host, mostra device ID e password in una
finestra compatta.  Accetta connessioni in ingresso con streaming,
input remoto, chat e file transfer.  Non permette connessioni in uscita.
"""

from __future__ import annotations

import logging
import queue
import secrets
import string
import time
import uuid

import numpy as np

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.device_registry import DeviceRegistry
from opendesk.core.file_transfer import FileTransferManager, TransferState
from opendesk.crypto.auth import AuthManager
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayClient, RelayRole
from opendesk.services.stream_service import StreamService
from opendesk.ui.chat_panel import ChatPanel
from opendesk.ui.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FT_POLL_INTERVAL = 200  # ms — poll file-transfer updates queue


# ═══════════════════════════════════════════════════════════════════════════
# HostService
# ═══════════════════════════════════════════════════════════════════════════


class HostService(QObject):
    """Servizio host-only: relay, streaming, file transfer, chat.

    Signals
    -------
    status_changed(text: str)
        Testo descrittivo dello stato corrente (connessione, auth, etc.).
    device_info_changed(device_id: str, password: str)
        Le credenziali di sessione sono cambiate (nuova sessione).
    peer_connected(peer_name: str)
        Un client remoto si e' autenticato.
    peer_disconnected()
        Il client remoto si e' disconnesso.
    streaming_started()
        Lo streaming screen capture e' iniziato.
    streaming_stopped()
        Lo streaming e' stato fermato.
    chat_message_received(text: str, is_remote: bool)
        Messaggio chat ricevuto dal/sul peer remoto.
    chat_open_requested(open: bool)
        Il peer remoto ha aperto/chiuso la chat.
    file_transfer_started()
        Un trasferimento file e' iniziato (per mostrare la UI).
    """

    status_changed = Signal(str)
    device_info_changed = Signal(str, str)
    peer_connected = Signal(str)
    peer_disconnected = Signal()
    streaming_started = Signal()
    streaming_stopped = Signal()
    chat_message_received = Signal(str, bool)
    chat_open_requested = Signal(bool)
    file_transfer_started = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # ── Device identity (persistent UUID) ──
        self._device_id = self._settings.value("device/id", "")
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
            self._settings.setValue("device/id", self._device_id)
        self._device_name = self._settings.value("device/name", "")
        if not self._device_name:
            self._device_name = f"Desktop-{self._device_id[:8]}"
            self._settings.setValue("device/name", self._device_name)

        # ── Auth / session ──
        self._auth = AuthManager()
        self._session_id: str = ""
        self._password: str = ""

        # ── Relay client (host only) ──
        self._relay = RelayClient(self)
        self._relay.host_connected.connect(self._on_host_connected)
        self._relay.host_disconnected.connect(self._on_host_disconnected)
        self._relay.host_peer_joined.connect(self._on_host_peer_joined)
        self._relay.host_peer_disconnected.connect(self._on_host_peer_disconnected)
        self._relay.host_auth_result.connect(self._on_host_auth_result)
        self._relay.host_keyframe_requested.connect(self._on_host_keyframe_requested)
        self._relay.message_received.connect(self._on_relay_message)
        self._relay.error.connect(self._on_relay_error)
        self._relay.device_list_received.connect(self._on_device_list)

        # ── Streaming ──
        self._stream: StreamService | None = None

        # ── File transfer ──
        self._file_transfer = FileTransferManager()
        self._ft_poll_timer = QTimer(self)
        self._ft_poll_timer.timeout.connect(self._poll_file_transfer)
        self._ft_poll_timer.start(_FT_POLL_INTERVAL)

        # ── Device registry (pre-authorization) ──
        self._device_registry = DeviceRegistry()

        # ── State ──
        self._peer_connected = False
        self._peer_device_id: str = ""
        self._host_retries = 0

    # ── properties ──────────────────────────────────────────────────────

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def device_name(self) -> str:
        return self._device_name

    @device_name.setter
    def device_name(self, value: str) -> None:
        self._device_name = value
        self._settings.setValue("device/name", value)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def password(self) -> str:
        return self._password

    @property
    def relay(self) -> RelayClient:
        return self._relay

    @property
    def stream(self) -> StreamService | None:
        return self._stream

    @property
    def file_transfer(self) -> FileTransferManager:
        return self._file_transfer

    @property
    def device_registry(self) -> DeviceRegistry:
        return self._device_registry

    @property
    def is_peer_connected(self) -> bool:
        return self._peer_connected

    @property
    def is_hosting(self) -> bool:
        return self._relay.is_hosting()

    # ── lifecycle ───────────────────────────────────────────────────────

    def create_session(self, password: str | None = None) -> tuple[str, str]:
        """Crea una nuova sessione.  Restituisce (session_id, password)."""
        if password is None:
            password = self._generate_password()
        session = self._auth.create_session(password, one_time=False)
        self._session_id = session.session_id
        self._password = password
        self.device_info_changed.emit(self._session_id, self._password)
        logger.info("New session created: %s", self._session_id)
        return self._session_id, self._password

    def start(self) -> None:
        """Connetti al relay e avvia l'hosting."""
        if not self._session_id:
            self.create_session()
        host, port = self._get_relay_config()
        clean_id = self._session_id.replace(" ", "")
        self.status_changed.emit(f"Connecting to relay {host}:{port}...")
        logger.info("Starting host on relay %s:%s session=%s", host, port, clean_id)
        trusted_ids = {d.device_id for d in self._device_registry.trusted()}
        self._relay.start_hosting(
            host, port, clean_id, self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_device_ids=trusted_ids,
        )

    def stop(self) -> None:
        """Ferma l'hosting e disconnetti dal relay."""
        self._stop_streaming()
        self._relay.disconnect()
        self._peer_connected = False
        self.status_changed.emit("Disconnected")

    def regenerate_session(self) -> None:
        """Genera nuova sessione (ID + password) e ri-avvia l'hosting."""
        self._relay.stop_hosting()
        self.create_session()
        self.start()

    # ── relay config ────────────────────────────────────────────────────

    def _get_relay_config(self) -> tuple[str, int]:
        host = self._settings.value("network/relay_host", "")
        if not host:
            host = "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        try:
            port = int(self._settings.value("network/relay_port", 8474))
        except (ValueError, TypeError):
            port = 8474
        return host, port

    # ── streaming ───────────────────────────────────────────────────────

    def _start_streaming(self) -> None:
        """Avvia screen capture + streaming verso il peer remoto."""
        if self._stream is not None and self._stream.is_streaming:
            return
        if self._stream is None:
            self._stream = StreamService(self._relay, self)
            self._stream.error.connect(self._on_stream_error)
            self._stream.input_unavailable.connect(self._on_input_unavailable)
        self._stream.start_streaming()
        self.streaming_started.emit()
        logger.info("Host streaming started")

    def _stop_streaming(self) -> None:
        """Ferma lo streaming."""
        if self._stream is not None:
            self._stream.stop_streaming()
            self.streaming_stopped.emit()
            logger.info("Host streaming stopped")

    # ── retry (exponential backoff) ─────────────────────────────────────

    def schedule_retry(self) -> None:
        """Riprova la connessione relay con backoff esponenziale."""
        if self._host_retries >= 5:
            self.status_changed.emit("⚠ Relay unavailable — local session only")
            return
        delay = min(2**self._host_retries * 2, 30)
        self._host_retries += 1
        logger.info("Retrying relay in %ds (attempt %d/5)", delay, self._host_retries)
        QTimer.singleShot(int(delay * 1000), self._retry_now)

    def _retry_now(self) -> None:
        if not self._session_id or self._relay.is_hosting():
            return
        host, port = self._get_relay_config()
        self.status_changed.emit("Reconnecting to relay...")
        trusted_ids = {d.device_id for d in self._device_registry.trusted()}
        self._relay.start_hosting(
            host, port,
            self._session_id.replace(" ", ""),
            self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_device_ids=trusted_ids,
        )

    # ── file transfer polling ──────────────────────────────────────────

    def _poll_file_transfer(self) -> None:
        """Polla la coda degli aggiornamenti file transfer (main thread)."""
        try:
            while True:
                event = self._file_transfer.updates.get_nowait()
                kind = event[0]

                if kind == "transfer":
                    self.file_transfer_started.emit()
                elif kind == "listing":
                    pass  # handled by FileBrowserDock internally
                elif kind == "status":
                    pass
        except queue.Empty:
            pass

    # ── host event handlers ─────────────────────────────────────────────

    @Slot(str, str)
    def _on_host_connected(self, role: str, session_id: str) -> None:
        self._host_retries = 0
        self.status_changed.emit("Connected to relay — waiting for incoming connection...")
        logger.info("Host connected to relay: %s", session_id)

    @Slot()
    def _on_host_disconnected(self) -> None:
        logger.info("Host disconnected from relay")
        self._peer_connected = False
        self.status_changed.emit("Disconnected from relay")
        if self._session_id:
            self.schedule_retry()

    @Slot()
    def _on_host_peer_joined(self) -> None:
        self.status_changed.emit("Remote peer joined — authenticating...")

    @Slot()
    def _on_host_peer_disconnected(self) -> None:
        logger.info("Remote peer disconnected")
        self._peer_connected = False
        self._peer_device_id = ""
        self._stop_streaming()
        self.peer_disconnected.emit()
        self.status_changed.emit("Remote client disconnected — waiting for connections...")

    @Slot(bool, str)
    def _on_host_auth_result(self, success: bool, message: str) -> None:
        if success:
            self._peer_connected = True
            self._start_streaming()
            self.peer_connected.emit(self._peer_device_id or "Remote")
            self.status_changed.emit("Remote peer authenticated — streaming...")
        else:
            self._peer_connected = False
            self.status_changed.emit(f"Authentication failed: {message}")

    @Slot()
    def _on_host_keyframe_requested(self) -> None:
        logger.debug("Keyframe requested by remote peer")

    @Slot(str)
    def _on_stream_error(self, error_msg: str) -> None:
        logger.error("Stream error: %s", error_msg)
        self.status_changed.emit(f"⚠ Stream error: {error_msg}")
        self._stop_streaming()

    @Slot(str)
    def _on_input_unavailable(self, error_msg: str) -> None:
        logger.warning("Input backend unavailable: %s", error_msg)
        self.status_changed.emit(f"⚠ Remote input disabled: {error_msg}")

    @Slot(str)
    def _on_relay_error(self, error_msg: str) -> None:
        if "Peer disconnected" in error_msg:
            logger.info("Peer disconnected (via error handler)")
            self._stop_streaming()
            self._peer_connected = False
            self.peer_disconnected.emit()
            self.status_changed.emit("Remote client disconnected — waiting for connections...")
            return
        logger.error("Relay error: %s", error_msg)
        self.status_changed.emit(f"⚠ Error: {error_msg}")

    @Slot(list)
    def _on_device_list(self, devices: list[dict]) -> None:
        """Aggiorna il device registry con la lista ricevuta dal relay."""
        self._device_registry.merge_from_relay(devices)

    @Slot(object)
    def _on_relay_message(self, msg: Message) -> None:
        """Gestisce tutti i messaggi in arrivo dal relay."""
        t = msg.type

        # ── Input injection ──
        if t == MessageType.MOUSE_EVENT and self._stream and self._stream.input_backend:
            self._stream.inject_mouse(msg)
        elif t == MessageType.KEYBOARD_EVENT and self._stream and self._stream.input_backend:
            self._stream.inject_keyboard(msg)

        # ── Chat ──
        elif t == MessageType.CHAT_MESSAGE:
            text = msg.payload.get("text", "")
            self.chat_message_received.emit(text, True)
        elif t == MessageType.CHAT_OPEN:
            is_open = msg.payload.get("open", False)
            self.chat_open_requested.emit(is_open)

        # ── Audio (playback dal peer remoto) ──
        elif t == MessageType.AUDIO_FRAME:
            data = msg.payload.get("data", b"")
            if data and self._stream:
                self._stream.play_audio_frame(data)

        # ── File transfer ──
        elif t == MessageType.FILE_REQUEST:
            job_id = self._file_transfer.handle_file_request(msg)
            if job_id:
                self._relay.send_message(Message.file_accept(job_id))
                self.file_transfer_started.emit()
        elif t == MessageType.FILE_CHUNK:
            self._file_transfer.handle_chunk(msg)
        elif t == MessageType.FILE_ACCEPT:
            job_id = msg.payload.get("job_id", "")
            job = self._file_transfer.get_job(job_id)
            if job:
                self._file_transfer.send_chunks(job, self._relay.send_message)
        elif t == MessageType.FILE_REJECT:
            job_id = msg.payload.get("job_id", "")
            reason = msg.payload.get("reason", "Rejected by remote")
            job = self._file_transfer.get_job(job_id)
            if job:
                job.state = TransferState.CANCELLED
                job.error = reason
        elif t in (MessageType.FILE_COMPLETE, MessageType.FILE_ERROR):
            job_id = msg.payload.get("job_id", "")
            job = self._file_transfer.get_job(job_id)
            if job:
                if t == MessageType.FILE_COMPLETE:
                    job.state = TransferState.COMPLETED
                else:
                    job.state = TransferState.FAILED
                    job.error = msg.payload.get("error", "Unknown error")
        elif t == MessageType.FILE_LIST_REQUEST:
            response = self._file_transfer.handle_list_request(msg)
            self._relay.send_message(response)
        elif t == MessageType.FILE_LIST_RESPONSE:
            self._file_transfer.handle_list_response(msg)
        elif t == MessageType.FILE_DOWNLOAD_REQUEST:
            self._file_transfer.handle_download_request(msg, self._relay.send_message)
        elif t == MessageType.FILE_DOWNLOAD_ACCEPT:
            self._file_transfer.handle_download_accept(msg)
        elif t == MessageType.FILE_DOWNLOAD_REJECT:
            self._file_transfer.handle_download_reject(msg)

        # ── Ignored ──
        elif t in (MessageType.CAMERA_FRAME, MessageType.CAMERA_START):
            # Siamo host: la webcam remota non viene mostrata (nessun viewer)
            pass
        elif t in (MessageType.CLIPBOARD_TEXT, MessageType.CLIPBOARD_IMAGE):
            # Clipboard sync non implementato in questa versione
            pass

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _generate_password() -> str:
        alphabet = string.ascii_uppercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(8))


# ═══════════════════════════════════════════════════════════════════════════
# HostWindow
# ═══════════════════════════════════════════════════════════════════════════


class HostWindow(QMainWindow):
    """Finestra principale compatta per OpenDesk Host (solo incoming).

    Mostra device ID, password e stato connessione.  Permette di
    avviare chat, file transfer e modificare le impostazioni.
    """

    WINDOW_TITLE = "OpenDesk Host"
    MIN_WIDTH = 420
    MIN_HEIGHT = 360

    # ── Design tokens ───────────────────────────────────────────────────
    _C_PRIMARY = "#2563eb"
    _C_PRIMARY_DARK = "#1e40af"
    _C_SUCCESS = "#22c55e"
    _C_WARNING = "#eab308"
    _C_DANGER = "#ef4444"
    _C_TEXT = "#0f172a"
    _C_TEXT_SECONDARY = "#64748b"
    _C_BORDER = "#e2e8f0"
    _C_SURFACE = "#ffffff"
    _FONT_MONO = "'Courier New', 'Consolas', monospace"

    def __init__(self, service: HostService) -> None:
        super().__init__()
        self._service = service

        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint)

        # ── Sub-windows (lazy) ──
        self._chat_panel: ChatPanel | None = None
        self._transfer_dock: QWidget | None = None

        # ── Build UI ──
        self._setup_central_widget()
        self._setup_menu()
        self._wire_service()

    # ═══════════════════════════════════════════════════════════════════════
    # UI setup
    # ═══════════════════════════════════════════════════════════════════════

    def _setup_central_widget(self) -> None:
        """Costruisce il widget centrale con design rinnovato."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 1. Header bar ──────────────────────────────────────────────
        header = QFrame()
        header.setObjectName("HostHeader")
        header.setFixedHeight(96)
        header.setStyleSheet(f"""
            QFrame#HostHeader {{
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 {self._C_PRIMARY_DARK},
                    stop:1 {self._C_PRIMARY}
                );
            }}
        """)
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(20, 10, 20, 10)
        header_layout.setSpacing(2)

        # Title
        title = QLabel("OpenDesk Host")
        title.setStyleSheet("font-size: 20px; font-weight: 800; color: #ffffff;")
        header_layout.addWidget(title)

        # Status row inside header
        status_row = QHBoxLayout()
        status_row.setSpacing(6)

        self._status_dot = QLabel("●")
        self._status_dot.setStyleSheet(f"font-size: 12px; color: {self._C_WARNING};")
        status_row.addWidget(self._status_dot)

        self._status_label = QLabel("Initializing...")
        self._status_label.setStyleSheet("font-size: 13px; font-weight: 500; color: rgba(255,255,255,0.85);")
        status_row.addWidget(self._status_label)
        status_row.addStretch()

        header_layout.addLayout(status_row)
        layout.addWidget(header)

        # ── 2. Content area ────────────────────────────────────────────
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(20, 20, 20, 16)
        content_layout.setSpacing(16)

        # ── 2a. Credentials card ───────────────────────────────────────
        card = QFrame()
        card.setObjectName("CredentialsCard")
        card.setStyleSheet(f"""
            QFrame#CredentialsCard {{
                background: {self._C_SURFACE};
                border: 1px solid {self._C_BORDER};
                border-radius: 10px;
            }}
        """)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 18, 20, 18)
        card_layout.setSpacing(4)

        # ── Your ID ──
        id_label = QLabel("YOUR ID")
        id_label.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {self._C_TEXT_SECONDARY}; letter-spacing: 1px;")
        card_layout.addWidget(id_label)

        id_row = QHBoxLayout()
        id_row.setSpacing(8)

        self._id_display = QLabel("—")
        self._id_display.setObjectName("HostIdDisplay")
        self._id_display.setToolTip(f"Device UUID: {self._service.device_id}\nUsa questo UUID per pre-autorizzare il dispositivo")
        self._id_display.setStyleSheet(f"""
            QLabel#HostIdDisplay {{
                font-size: 24px; font-weight: 800;
                font-family: {self._FONT_MONO};
                letter-spacing: 4px;
                color: {self._C_TEXT};
            }}
        """)
        id_row.addWidget(self._id_display)
        id_row.addStretch()

        self._copy_id_btn = QPushButton("📋  Copy ID")
        self._copy_id_btn.setFixedHeight(32)
        self._copy_id_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_id_btn.setStyleSheet(self._copy_button_style())
        self._copy_id_btn.clicked.connect(self._copy_session_id)
        id_row.addWidget(self._copy_id_btn)

        card_layout.addLayout(id_row)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"max-height: 1px; background: {self._C_BORDER}; margin: 8px 0;")
        card_layout.addWidget(sep)

        # ── Password ──
        pwd_label = QLabel("PASSWORD")
        pwd_label.setStyleSheet(f"font-size: 10px; font-weight: 700; color: {self._C_TEXT_SECONDARY}; letter-spacing: 1px;")
        card_layout.addWidget(pwd_label)

        pwd_row = QHBoxLayout()
        pwd_row.setSpacing(8)

        self._pwd_display = QLabel("—")
        self._pwd_display.setObjectName("HostPwdDisplay")
        self._pwd_display.setStyleSheet(f"""
            QLabel#HostPwdDisplay {{
                font-size: 18px; font-weight: 700;
                font-family: {self._FONT_MONO};
                letter-spacing: 3px;
                color: {self._C_TEXT};
            }}
        """)
        pwd_row.addWidget(self._pwd_display)
        pwd_row.addStretch()

        self._copy_pwd_btn = QPushButton("📋  Copy Pwd")
        self._copy_pwd_btn.setFixedHeight(32)
        self._copy_pwd_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_pwd_btn.setStyleSheet(self._copy_button_style())
        self._copy_pwd_btn.clicked.connect(self._copy_password)
        pwd_row.addWidget(self._copy_pwd_btn)

        card_layout.addLayout(pwd_row)

        content_layout.addWidget(card)

        # ── 2b. Device UUID info ───────────────────────────────────────
        uuid_info = QLabel(f"Device UUID: {self._service.device_id}")
        uuid_info.setStyleSheet(f"font-size: 10px; color: {self._C_TEXT_SECONDARY};")
        uuid_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        uuid_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        uuid_info.setToolTip("Usa questo UUID in Settings → Security per pre-autorizzare questo dispositivo")
        content_layout.addWidget(uuid_info)

        # ── 2c. Action buttons ─────────────────────────────────────────
        # Solo Settings: Chat e File Transfer si attivano automaticamente
        # quando il computer remoto li richiede.
        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(8)

        self._refresh_btn = QPushButton("🔄  New Session")
        self._refresh_btn.setFixedHeight(36)
        self._refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._refresh_btn.setStyleSheet(self._secondary_button_style())
        self._refresh_btn.clicked.connect(self._on_refresh_session)
        actions_layout.addWidget(self._refresh_btn)

        actions_layout.addStretch()

        self._settings_btn = QPushButton("⚙  Settings")
        self._settings_btn.setFixedHeight(36)
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setStyleSheet(self._secondary_button_style())
        self._settings_btn.clicked.connect(self._on_settings)
        actions_layout.addWidget(self._settings_btn)

        content_layout.addLayout(actions_layout)
        content_layout.addStretch()

        layout.addWidget(content, 1)

        self.setCentralWidget(central)

    # ── Button styles ───────────────────────────────────────────────────

    @staticmethod
    def _copy_button_style() -> str:
        return f"""
            QPushButton {{
                padding: 4px 14px;
                font-size: 12px;
                font-weight: 600;
                border: 1px solid #e2e8f0;
                border-radius: 6px;
                background: #f8fafc;
                color: #2563eb;
            }}
            QPushButton:hover {{
                background: #2563eb;
                color: #ffffff;
                border-color: #2563eb;
            }}
            QPushButton:pressed {{
                background: #1d4ed8;
            }}
            QPushButton:disabled {{
                background: #f1f5f9;
                color: #94a3b8;
                border-color: #e2e8f0;
            }}
        """

    @staticmethod
    def _secondary_button_style() -> str:
        return f"""
            QPushButton {{
                padding: 6px 18px;
                font-size: 13px;
                font-weight: 600;
                border: 1px solid #e2e8f0;
                border-radius: 8px;
                background: #ffffff;
                color: #475569;
            }}
            QPushButton:hover {{
                background: #f1f5f9;
                border-color: #2563eb;
                color: #2563eb;
            }}
            QPushButton:pressed {{
                background: #e2e8f0;
            }}
        """

    def _setup_menu(self) -> None:
        """Menu minimale (File > Quit, Help > About)."""
        menubar = self.menuBar()
        if menubar is None:
            return

        file_menu = menubar.addMenu("&File")
        act_quit = QAction("&Quit", self)
        act_quit.setShortcut("Ctrl+Q")
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        help_menu = menubar.addMenu("&Help")
        act_about = QAction("&About OpenDesk Host", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

    def _wire_service(self) -> None:
        """Collega i segnali del servizio ai gestori UI."""
        svc = self._service
        svc.status_changed.connect(self._on_status_changed)
        svc.device_info_changed.connect(self._on_device_info_changed)
        svc.peer_connected.connect(self._on_peer_connected)
        svc.peer_disconnected.connect(self._on_peer_disconnected)
        svc.chat_message_received.connect(self._on_chat_message)
        svc.chat_open_requested.connect(self._on_chat_open)
        svc.file_transfer_started.connect(self._on_file_transfer_event)

    # ── Slots: stato ─────────────────────────────────────────────────────

    @Slot(str)
    def _on_status_changed(self, status: str) -> None:
        self._status_label.setText(status)
        # Aggiorna colore pallino in base allo stato
        lower = status.lower()
        if "error" in lower or "fail" in lower or "unavailable" in lower:
            color = self._C_DANGER
        elif "connect" in lower or "authenticat" in lower or "join" in lower:
            color = self._C_WARNING
        elif "stream" in lower or "connected" in lower or "wait" in lower:
            color = self._C_SUCCESS
        else:
            color = self._C_WARNING
        self._status_dot.setStyleSheet(f"font-size: 12px; color: {color};")

    @Slot(str, str)
    def _on_device_info_changed(self, session_id: str, password: str) -> None:
        self._id_display.setText(self._format_session_id(session_id))
        self._pwd_display.setText(password)

    @Slot(str)
    def _on_peer_connected(self, peer_name: str) -> None:
        self.setWindowTitle("OpenDesk Host — Connected")

    @Slot()
    def _on_peer_disconnected(self) -> None:
        self.setWindowTitle(self.WINDOW_TITLE)

    # ── Slots: azioni ───────────────────────────────────────────────────

    @Slot()
    def _on_refresh_session(self) -> None:
        self._service.regenerate_session()

    # Chat e File Transfer si aprono SOLO su richiesta del computer remoto.
    # L'host non ha pulsanti per avviarli manualmente.

    @Slot()
    def _on_settings(self) -> None:
        dialog = SettingsDialog(
            device_registry=self._service.device_registry,
            parent=self,
        )
        dialog.exec()

    # ── Chat handlers ───────────────────────────────────────────────────

    @Slot(str, bool)
    def _on_chat_message(self, text: str, is_remote: bool) -> None:
        if self._chat_panel is None:
            self._chat_panel = ChatPanel(self)
            self._chat_panel.message_sent.connect(self._on_chat_message_sent)
        self._chat_panel.add_message("Remote", text, is_remote=True)
        if not self._chat_panel.isVisible():
            self._chat_panel.show()
            self._chat_panel.raise_()

    @Slot(bool)
    def _on_chat_open(self, is_open: bool) -> None:
        if is_open:
            if self._chat_panel is None:
                self._chat_panel = ChatPanel(self)
                self._chat_panel.message_sent.connect(self._on_chat_message_sent)
            if not self._chat_panel.isVisible():
                self._chat_panel.show()
                self._chat_panel.raise_()
                self._chat_panel.activateWindow()
        else:
            if self._chat_panel is not None and self._chat_panel.isVisible():
                self._chat_panel.hide()

    @Slot(str)
    def _on_chat_message_sent(self, text: str) -> None:
        self._service.relay.send_message(Message.chat_message(text))

    # ── File transfer handlers ──────────────────────────────────────────

    @Slot()
    def _on_file_transfer_event(self) -> None:
        """Mostra il dock file transfer quando il remoto invia un file."""
        dock = self._ensure_transfer_dock()
        if not dock.isVisible():
            dock.show()
            dock.raise_()
            dock.activateWindow()
            dock.set_connected(True)
            dock.set_status("Connected — file transfer ready")
            self._service.file_transfer.request_remote_listing(
                "/", self._service.relay.send_message,
            )

    def _ensure_transfer_dock(self) -> QWidget:
        """Crea il dock file transfer al primo uso (lazy)."""
        if self._transfer_dock is None:
            from opendesk.ui.file_transfer_ui import FileBrowserDock

            dock = FileBrowserDock(self)
            dock.file_upload_requested.connect(self._on_browser_upload)
            dock.file_download_requested.connect(self._on_browser_download)
            dock.remote_listing_requested.connect(self._on_browser_remote_listing)
            self._transfer_dock = dock
        return self._transfer_dock

    @Slot(list, str)
    def _on_browser_upload(self, paths: list[str], remote_dest: str = "/") -> None:
        if not paths:
            return
        self._service.file_transfer.send_files(
            paths, self._service.relay.send_message,
            remote_dest_path=remote_dest,
        )

    @Slot(list, str)
    def _on_browser_download(self, remote_paths: list[str], local_dest: str = "") -> None:
        if not remote_paths:
            return
        for rpath in remote_paths:
            self._service.file_transfer.request_download(
                rpath, self._service.relay.send_message,
                local_dest=local_dest,
            )

    @Slot(str)
    def _on_browser_remote_listing(self, path: str) -> None:
        self._service.file_transfer.request_remote_listing(
            path, self._service.relay.send_message,
        )

    # ── Copy ────────────────────────────────────────────────────────────

    def _copy_session_id(self) -> None:
        """Copia il session ID (senza spazi) negli appunti."""
        raw = self._service.session_id.replace(" ", "")
        QApplication.clipboard().setText(raw)
        self._flash_button(self._copy_id_btn, "Copied!")

    def _copy_password(self) -> None:
        QApplication.clipboard().setText(self._service.password)
        self._flash_button(self._copy_pwd_btn, "Copied!")

    def _flash_button(self, btn: QPushButton, text: str) -> None:
        original = btn.text()
        btn.setText(text)
        btn.setEnabled(False)
        QTimer.singleShot(1500, lambda: self._restore_btn(btn, original))

    def _restore_btn(self, btn: QPushButton, text: str) -> None:
        btn.setText(text)
        btn.setEnabled(True)

    @staticmethod
    def _format_session_id(session_id: str) -> str:
        """Formatta il session ID leggibile: rimuove spazi e ri-formatta."""
        raw = session_id.replace(" ", "")
        # Gruppi di 3 cifre
        blocks = [raw[i:i+3] for i in range(0, len(raw), 3)]
        return " ".join(blocks)

    # ── About ───────────────────────────────────────────────────────────

    @Slot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About OpenDesk Host",
            "<h3>OpenDesk Host v0.1.0</h3>"
            "<p>Remote desktop host — accepts incoming connections.</p>"
            "<p>Built with Python, PySide6.</p>"
            "<hr>"
            "<p style='font-size:12px;color:#64748b'>MIT License — 2026</p>",
        )

    # ── Close ───────────────────────────────────────────────────────────

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        """Conferma se una sessione remota e' attiva."""
        if self._service.is_peer_connected:
            reply = QMessageBox.question(
                self, "Confirm Quit",
                "A remote session is active.\nDisconnect and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return
        self._service.stop()
        if self._chat_panel:
            self._chat_panel.close()
        if self._transfer_dock:
            self._transfer_dock.close()
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════


def main_host() -> None:
    """Start the OpenDesk Host application."""
    import sys

    from opendesk.utils.logger import setup_logging

    setup_logging(level=logging.DEBUG)
    version = __import__("opendesk").__version__
    logger.info("Starting OpenDesk Host v%s", version)

    app = QApplication(sys.argv)
    app.setApplicationName("OpenDesk Host")
    app.setOrganizationName("OpenDesk")
    app.setApplicationVersion(version)
    app.setStyle("Fusion")

    # Applica il tema chiaro (riusa dalla app principale)
    from opendesk.app import load_stylesheet

    load_stylesheet(app, "light")

    # Crea servizio e finestra
    service = HostService()
    window = HostWindow(service)

    # Inizializza sessione e avvia
    service.create_session()
    window.show()
    service.start()

    sys.exit(app.exec())
