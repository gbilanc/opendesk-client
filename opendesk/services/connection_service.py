"""Servizio di connessione relay — wrapping di RelayClient.

Gestisce:
- Connessione/disconnessione dal relay (host/client)
- Ciclo di vita della sessione (ID, password, auth)
- Device registry e whitelist
- Riconnessione con backoff
"""

from __future__ import annotations

import logging
import uuid

import numpy as np

from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot

from opendesk.crypto.auth import AuthManager
from opendesk.core.device_registry import DeviceRegistry, DeviceEntry
from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayClient, RelayRole

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# ConnectionService
# ═══════════════════════════════════════════════════════════════════


class ConnectionService(QObject):
    """Servizio di connessione al relay.

    Signals emessi (da connettere in MainWindow o altri consumer)::

        connected(role: str, session_id: str)
        disconnected()
        peer_joined()
        auth_requested()
        auth_result(success: bool, message: str)
        frame_received(rgb: np.ndarray, width: int, height: int)
        message_received(msg: Message)
        device_list_received(devices: list[dict])
        error(error_msg: str)
    """

    # Segnali pubblici
    connected = Signal(str, str)
    disconnected = Signal()
    peer_joined = Signal()
    auth_requested = Signal()
    auth_result = Signal(bool, str)
    frame_received = Signal(np.ndarray, int, int)
    message_received = Signal(object)
    device_list_received = Signal(list)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # Auth / session
        self._auth = AuthManager()
        self._session_id: str = ""
        self._password: str = ""
        self._host_session_id: str = ""

        # Device identity
        self._device_id = self._settings.value("device/id", "")
        if not self._device_id:
            self._device_id = str(uuid.uuid4())
            self._settings.setValue("device/id", self._device_id)
        self._device_name = self._settings.value("device/name", "")
        if not self._device_name:
            self._device_name = f"Desktop-{self._device_id[:8]}"
            self._settings.setValue("device/name", self._device_name)

        # Relay client
        self._relay = RelayClient(self)
        self._relay.connected.connect(self._on_relay_connected)
        self._relay.disconnected.connect(self._on_relay_disconnected)
        self._relay.peer_joined.connect(self.peer_joined.emit)
        self._relay.auth_requested.connect(self.auth_requested.emit)
        self._relay.auth_result.connect(self.auth_result.emit)
        self._relay.frame_received.connect(self.frame_received.emit)
        self._relay.message_received.connect(self.message_received.emit)
        self._relay.error.connect(self._on_relay_error_sent)
        self._relay.device_list_received.connect(self._on_device_list_from_relay)

        # Device registry
        self._device_registry = DeviceRegistry()

        # Retry
        self._relay_retries = 0

    # ── properties ──────────────────────────────────────────────────

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
    def host_session_id(self) -> str:
        return self._host_session_id

    @property
    def auth_manager(self) -> AuthManager:
        return self._auth

    @property
    def relay(self) -> RelayClient:
        return self._relay

    @property
    def device_registry(self) -> DeviceRegistry:
        return self._device_registry

    @property
    def role(self) -> RelayRole | None:
        return self._relay.role

    @property
    def is_connected(self) -> bool:
        return self._relay.is_connected

    # ── session lifecycle ───────────────────────────────────────────

    def create_session(self, password: str) -> str:
        """Crea una nuova sessione locale e restituisce l'ID."""
        session = self._auth.create_session(password, one_time=False)
        self._session_id = session.session_id
        self._password = password
        logger.info("New session created: %s", self._session_id)
        return self._session_id

    def start_hosting(self) -> None:
        """Avvia l'hosting sul relay con la sessione corrente."""
        host, port = self._get_relay_config()
        self._host_session_id = self._session_id.replace(" ", "")
        logger.info("Starting host on relay %s:%s with session %s", host, port, self._host_session_id)
        # Passa gli ID dei dispositivi trusted per l'auto-auth
        trusted_ids = {d.device_id for d in self._device_registry.trusted()}
        self._relay.start_hosting(
            host, port, self._host_session_id, self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_device_ids=trusted_ids,
        )

    def join_session(self, peer_id: str, password: str) -> None:
        """Connetti come client a una sessione remota."""
        clean_id = peer_id.replace(" ", "")
        host, port = self._get_relay_config()
        logger.info("Joining session %s on relay %s:%s", clean_id, host, port)
        self._relay.join_session(
            host, port, clean_id, password,
            device_id=self._device_id,
        )

    def disconnect(self) -> None:
        """Disconnetti dal relay."""
        self._relay_retries = 0
        self._relay.disconnect()

    # ── relay config ────────────────────────────────────────────────

    def _get_relay_config(self) -> tuple[str, int]:
        host = self._settings.value("network/relay_host", "")
        if not host:
            host = "127.0.0.1"
        if host == "0.0.0.0":
            host = "127.0.0.1"
        try:
            port = int(self._settings.value("network/relay_port", 8474))
        except (ValueError, TypeError):
            logger.warning("Invalid relay port in settings, using default 8474")
            port = 8474
        return host, port

    # ── retry ───────────────────────────────────────────────────────

    def schedule_retry(self, status_callback) -> None:
        """Riprova la connessione con backoff esponenziale."""
        if self._relay_retries >= 5:
            status_callback("⚠ Relay unavailable — local session only")
            return
        delay = min(2 ** self._relay_retries * 2, 30)
        self._relay_retries += 1
        logger.info("Retrying relay in %ds (attempt %d/5)", delay, self._relay_retries)
        QTimer.singleShot(int(delay * 1000), lambda: self._retry_now(status_callback))

    def _retry_now(self, status_callback) -> None:
        # Guard: don't reconnect if the user explicitly disconnected
        if not self._host_session_id or self._relay.is_connected:
            return
        host, port = self._get_relay_config()
        status_callback("Reconnecting to relay...")
        trusted_ids = {d.device_id for d in self._device_registry.trusted()}
        self._relay.start_hosting(
            host, port, self._host_session_id, self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_device_ids=trusted_ids,
        )

    # ── relay event handlers → forward as signals ───────────────────

    @Slot(str, str)
    def _on_relay_connected(self, role: str, session_id: str) -> None:
        self._relay_retries = 0
        self.connected.emit(role, session_id)

    @Slot()
    def _on_relay_disconnected(self) -> None:
        self.disconnected.emit()

    @Slot(str)
    def _on_relay_error_sent(self, error_msg: str) -> None:
        self.error.emit(error_msg)

    @Slot(list)
    def _on_device_list_from_relay(self, devices: list[dict]) -> None:
        self._device_registry.merge_from_relay(devices)
        self.device_list_received.emit(devices)
