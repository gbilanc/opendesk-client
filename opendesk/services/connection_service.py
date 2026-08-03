"""Servizio di connessione relay — wrapping di RelayClient.

Gestisce:
- Connessione/disconnessione dal relay (host/client)
- Ciclo di vita della sessione (ID, password, auth)
- Device registry e whitelist
- Riconnessione con backoff (solo per la sessione host)

La sessione host è persistente: sopravvive anche quando l'utente
si connette come client a un altro device.
"""

from __future__ import annotations

import logging
import uuid

import numpy as np
from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot

from opendesk.core.device_registry import DeviceRegistry
from opendesk.crypto.auth import AuthManager
from opendesk.network.relay_client import RelayClient, RelayRole

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# ConnectionService
# ═══════════════════════════════════════════════════════════════════


class ConnectionService(QObject):
    """Servizio di connessione al relay.

    Mantiene una sessione host persistente che non viene terminata
    quando l'utente si connette come client a un altro device.

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
    peer_disconnected = Signal()  # remote peer left our hosted session
    auth_requested = Signal()
    auth_result = Signal(bool, str)  # legacy: fire per tutti i casi
    host_auth_result = Signal(bool, str)  # un client remoto si è autenticato a NOI (host)
    client_auth_result = Signal(bool, str)  # NOI ci siamo autenticati a un host remoto (client)
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

        # Relay client with independent host + client sessions
        self._relay = RelayClient(self)

        # ── Host signal wiring ──
        self._relay.host_connected.connect(self._on_host_connected)
        self._relay.host_disconnected.connect(self._on_host_disconnected)
        self._relay.host_peer_joined.connect(self._on_host_peer_joined)
        self._relay.host_peer_disconnected.connect(self._on_host_peer_disconnected)
        self._relay.host_auth_result.connect(self._on_host_auth_result)
        self._relay.host_keyframe_requested.connect(self._on_host_keyframe_requested)

        # ── Client signal wiring ──
        self._relay.client_connected.connect(self._on_client_connected)
        self._relay.client_disconnected.connect(self._on_client_disconnected)
        self._relay.client_auth_requested.connect(self._on_client_auth_requested)
        self._relay.client_auth_result.connect(self._on_client_auth_result)

        # ── Shared signals (from both host and client) ──
        self._relay.frame_received.connect(self.frame_received.emit)
        self._relay.message_received.connect(self.message_received.emit)
        self._relay.device_list_received.connect(self._on_device_list_from_relay)

        # ── Error routing: host vs client ──
        # We connect to error only for host errors; client errors are
        # handled separately via client_auth_result or client_disconnected.
        self._relay.error.connect(self._on_error)

        # Device registry
        self._device_registry = DeviceRegistry()

        # Retry (solo per la sessione host)
        self._host_retries = 0

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
        """Return the current CLIENT role if active, else None.

        ``RelayClient`` now supports independent host + client sessions.
        This property reflects the client session's role, so consumers
        that check ``role == RelayRole.CLIENT`` still work.
        """
        return self._relay.role

    @property
    def is_connected(self) -> bool:
        """Check if the CLIENT session is active and connected."""
        return self._relay.role == RelayRole.CLIENT and \
               self._relay.is_connected

    @property
    def is_hosting(self) -> bool:
        """Check if the HOST session is active and connected."""
        return self._relay.is_hosting()

    @property
    def client_connection_mode(self) -> str:
        """Connection mode of the client connected to our host session.

        ``"remote_desktop"`` (default) or ``"file_transfer"``.
        Only meaningful on the host side after a client authenticates.
        """
        return self._relay.client_connection_mode

    # ── session lifecycle ───────────────────────────────────────────

    def create_session(self, password: str) -> str:
        """Crea una nuova sessione locale e restituisce l'ID."""
        session = self._auth.create_session(password, one_time=False)
        self._session_id = session.session_id
        self._password = password
        logger.info("New session created: %s", self._session_id)
        return self._session_id

    def start_hosting(self) -> None:
        """Avvia l'hosting sul relay con la sessione corrente.

        La sessione host è persistente: non viene fermata quando
        l'utente si connette come client ad un altro device.
        """
        host, port = self._get_relay_config()
        self._host_session_id = self._session_id.replace(" ", "")
        logger.info(
            "Starting host on relay %s:%s with session %s",
            host, port, self._host_session_id,
        )
        # Passa gli ID dei dispositivi trusted per l'auto-auth
        trusted_ids = {d.device_id for d in self._device_registry.trusted()}
        self._relay.start_hosting(
            host, port, self._host_session_id, self._password,
            device_id=self._device_id,
            device_name=self._device_name,
            trusted_device_ids=trusted_ids,
        )

    def stop_hosting(self) -> None:
        """Ferma la sessione host."""
        self._host_retries = 0
        self._relay.stop_hosting()

    def join_session(self, peer_id: str, password: str,
                     connection_mode: str = "remote_desktop") -> None:
        """Connetti come client a una sessione remota.

        NON ferma la sessione host — l'hosting continua in background.

        Parameters
        ----------
        connection_mode : str
            "remote_desktop" (default) per desktop remoto completo,
            "file_transfer" per solo file transfer (senza streaming).
        """
        clean_id = peer_id.replace(" ", "")
        host, port = self._get_relay_config()
        logger.info(
            "Joining session %s on relay %s:%s (mode=%s)",
            clean_id, host, port, connection_mode,
        )
        self._relay.join_session(
            host, port, clean_id, password,
            device_id=self._device_id,
            connection_mode=connection_mode,
        )

    def disconnect(self) -> None:
        """Disconnetti completamente: ferma sia host che client."""
        self._host_retries = 0
        self._relay.disconnect()

    def disconnect_client(self) -> None:
        """Disconnetti solo la sessione client; l'hosting resta attivo."""
        self._relay.disconnect_client()

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

    # ── retry (solo per la sessione host) ───────────────────────────

    def schedule_retry(self, status_callback) -> None:
        """Riprova la connessione host con backoff esponenziale.

        La sessione client NON viene ritentata automaticamente
        (viene gestita dall'UI).
        """
        if self._host_retries >= 5:
            status_callback("⚠ Relay unavailable — local session only")
            return
        delay = min(2 ** self._host_retries * 2, 30)
        self._host_retries += 1
        logger.info("Retrying relay in %ds (attempt %d/5)", delay, self._host_retries)
        QTimer.singleShot(int(delay * 1000), lambda: self._retry_now(status_callback))

    def _retry_now(self, status_callback) -> None:
        # Guard: don't reconnect if hosting is already active
        if not self._host_session_id or self._relay.is_hosting():
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

    # ── host event handlers → forward as signals ────────────────────

    @Slot(str, str)
    def _on_host_connected(self, role: str, session_id: str) -> None:
        """Host session connected to relay."""
        self._host_retries = 0
        # Emit connected only if there's no active client session
        if self._relay.role != RelayRole.CLIENT:
            self.connected.emit(role, session_id)

    @Slot()
    def _on_host_disconnected(self) -> None:
        """Host session disconnected from relay."""
        logger.info("Host session disconnected from relay")
        # Schedule retry for host session
        if self._host_session_id:
            self.schedule_retry(lambda m: logger.info("Host retry: %s", m))
        # Emit disconnected only if there's no active client session
        if self._relay.role != RelayRole.CLIENT:
            self.disconnected.emit()

    @Slot()
    def _on_host_peer_joined(self) -> None:
        """A remote peer joined our hosted session."""
        self.peer_joined.emit()

    @Slot()
    def _on_host_peer_disconnected(self) -> None:
        """A remote peer disconnected from our hosted session."""
        logger.info("Remote peer disconnected from our session")
        self.peer_disconnected.emit()

    @Slot(bool, str)
    def _on_host_auth_result(self, success: bool, message: str) -> None:
        """Authentication result for a connecting client.

        A remote client successfully authenticated to OUR hosted session.
        """
        self.auth_result.emit(success, message)
        self.host_auth_result.emit(success, message)

    @Slot()
    def _on_host_keyframe_requested(self) -> None:
        """Remote peer requested a keyframe from our host stream.

        StreamService connects directly to RelayClient.host_keyframe_requested,
        so we just log here.
        """
        logger.debug(
            "Host keyframe request received (relayed by RelayClient.host_keyframe_requested)"
        )

    # ── client event handlers → forward as signals ──────────────────

    @Slot(str, str)
    def _on_client_connected(self, role: str, session_id: str) -> None:
        """Client session connected to relay."""
        self.connected.emit(role, session_id)

    @Slot()
    def _on_client_disconnected(self) -> None:
        """Client session disconnected from relay."""
        logger.info("Client session disconnected")
        self.disconnected.emit()

    @Slot()
    def _on_client_auth_requested(self) -> None:
        """Authentication requested by remote host."""
        self.auth_requested.emit()

    @Slot(bool, str)
    def _on_client_auth_result(self, success: bool, message: str) -> None:
        """Authentication result for the client connection.

        WE authenticated to a remote host's session.
        """
        self.auth_result.emit(success, message)
        self.client_auth_result.emit(success, message)

    # ── shared event handlers ──────────────────────────────────────

    @Slot(str)
    def _on_error(self, error_msg: str) -> None:
        """Handle errors from the relay (host or client).

        For host errors we forward to the error signal.
        Client errors are handled via client_auth_result/disconnected.
        """
        self.error.emit(error_msg)

    @Slot(list)
    def _on_device_list_from_relay(self, devices: list[dict]) -> None:
        self._device_registry.merge_from_relay(devices)
        self.device_list_received.emit(devices)
