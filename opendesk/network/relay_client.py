"""
Relay-based P2P connection client.

Provides ``RelayClient``, a QObject that runs asyncio TCP connections
to the relay server in background threads.  Supports two independent
session types:

- **Host** (persistent) — registers a session_id, waits for clients,
  authenticates, streams video, receives input events.  This session
  runs in the background and stays alive even while the user connects
  as a client to another device.
- **Client** (on-demand) — joins an existing session by session_id
  or device_id, authenticates, receives video frames, sends input events.

Threading model:
  - ``RelayClient`` lives in the main (UI) thread, emits Qt signals.
  - Host and client asyncio event loops each run in their own daemon thread.
  - A single ``QTimer`` polls both inbox queues to deliver messages
    to the UI thread.
  - ``asyncio.run_coroutine_threadsafe()`` sends commands from UI → network thread.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Any, cast

import msgpack
import numpy as np
from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot

from opendesk.core.video_codec import VideoDecoder
from opendesk.crypto.challenge import (
    compute_response,
    generate_nonce,
    verify_response,
)
from opendesk.crypto.e2ee import E2EEncryption, EncryptedMessage
from opendesk.network.p2p_transport import (
    MEDIA_MESSAGE_TYPES,
    P2PTransport,
    build_ice_servers,
)
from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role
# ---------------------------------------------------------------------------


class RelayRole(Enum):
    HOST = auto()
    CLIENT = auto()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_MS = 16  # poll inbox every 16 ms (~60 Hz); i frame stale
# sono coalesced per ciclo, quindi il costo risveglio e' quasi nullo
# e la latenza di presentazione non puo' superare 16 ms
_CONNECT_TIMEOUT = 15.0  # max seconds to wait for TCP connection
_STOP_JOIN_TIMEOUT = 8.0  # max seconds to wait for session thread to stop
_SEND_DRAIN_TIMEOUT = 5.0  # max seconds to wait for TCP send buffer to drain
_SEND_MAX_CONSECUTIVE_TIMEOUTS = 3  # consecutive drain timeouts before disconnecting
_SEND_MAX_PENDING = 20  # max pending sends before backpressure kicks in
_PING_INTERVAL = 25.0  # s — keep-alive; il relay disconnette peer idle > 120s

# Sentinel pushed into the incoming queue when the relay reader ends.
_INCOMING_CLOSED = object()

# Signalling messages handled internally; they must not be forwarded to the UI.
_P2P_SIGNAL_TYPES = frozenset(
    {
        MessageType.P2P_OFFER,
        MessageType.P2P_ANSWER,
        MessageType.P2P_CLOSE,
    }
)


# ---------------------------------------------------------------------------
# Async relay session
# ---------------------------------------------------------------------------


class _RelaySession:
    """Asyncio session that runs in a background thread.

    Holds the TCP connection and event loop.  Incoming messages are
    pushed into ``inbox`` (a thread-safe queue).  Outgoing messages
    are sent via ``asyncio.run_coroutine_threadsafe()``.

    Each session carries a ``session_seq`` number so that stale inbox
    events from a previous session can be ignored.
    """

    def __init__(
        self,
        host: str,
        port: int,
        session_id: str,
        password: str,
        role: RelayRole,
        inbox: queue.Queue,
        device_id: str = "",
        device_name: str = "",
        session_seq: int = 0,
        trusted_devices: dict[str, str] | None = None,
        known_device_secrets: dict[str, str] | None = None,
        connection_mode: str = "remote_desktop",
        e2ee_enabled: bool = True,
        p2p_enabled: bool = False,
        ice_servers: list | None = None,
        relay_fallback: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.password = password.strip()
        self.role = role
        self.inbox = inbox
        self.device_id = device_id
        self.device_name = device_name
        self.session_seq = session_seq
        self.connection_mode = connection_mode
        self._e2ee_enabled = e2ee_enabled
        self._p2p_enabled = p2p_enabled
        self._ice_servers = ice_servers or []
        self._relay_fallback = relay_fallback
        self._authenticated = False
        self._p2p: P2PTransport | None = None
        self._p2p_ready = False
        self._p2p_negotiation_started = False
        self._pending_p2p_sends = 0
        self._incoming: asyncio.Queue | None = None
        # Per-device secrets of devices WE trust (host side): id → secret.
        self._trusted_devices: dict[str, str] = trusted_devices or {}
        self._trusted_device_ids: set[str] = set(self._trusted_devices)
        # Per-device secrets WE already know for peers (client side).
        self._known_device_secrets: dict[str, str] = known_device_secrets or {}
        # Secret used for the E2EE key-exchange proof.  Empty → session
        # password.  For trusted (password-less) sessions it is set to the
        # per-device secret so the proof doesn't depend on the password.
        self._proof_secret: str = ""

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = threading.Event()
        self._decoder: VideoDecoder | None = None
        self._auth_nonce: str = ""  # challenge nonce for challenge-response auth
        self._e2ee = E2EEncryption()
        self._e2ee_ready = False

        # Tile grid compositing (client side)
        self._reference_frame: np.ndarray | None = None
        self._frame_width: int = 0
        self._frame_height: int = 0
        self._last_keyframe_time: float = 0.0
        self._last_video_activity_time: float = 0.0
        self._last_keyframe_request_ts: float = 0.0  # anti-flood per richieste keyframe
        self._start_time: float = time.time()
        self._pending_sends: int = 0  # atomic counter for pending _send_async tasks
        self._send_lock = threading.Lock()
        self._drain_timeout_count: int = 0  # consecutive drain timeouts
        self._last_pong_time: float = 0.0  # risposta al PING del keep-alive

    async def _ping_loop(self) -> None:
        """Keep-alive: invia PING periodici per non essere scartati dal relay.

        Il relay considera stale un peer senza attività da > 120s
        (``_PEER_TIMEOUT``, vedi docs/manuale-relay-server.md §5.6) e lo
        disconnette.  Senza PING l'host viene kickato ogni ~2 minuti
        anche quando fermo.

        Il task sopravvive ai singoli fallimenti di invio (``except
        Exception``: ``CancelledError`` è una ``BaseException`` e non viene
        intercettata, quindi il ``cancel()`` dello shutdown propagherebbe
        comunque).  Se un PONG non arriva da troppo tempo il link è
        half-open: lo segnaliamo nel log.
        """
        while self._running.is_set():
            try:
                await asyncio.sleep(_PING_INTERVAL)
                if not self._running.is_set():
                    break
                await self._send_async(
                    Message.ping(seq=int(time.time())), bypass_backpressure=True
                )
                last_pong = self._last_pong_time
                if last_pong and (time.time() - last_pong) > 3 * _PING_INTERVAL:
                    # Link half-open: i PING partono ma non tornano i PONG.
                    # Il relay considera il peer attivo (riceve i nostri
                    # PING), quindi l'host resterebbe "paired" con noi per
                    # sempre e i nuovi join prenderebbero 409.  Usciamo
                    # noi: DISCONNECT best-effort (funziona se il verso
                    # client→relay è vivo) e chiusura socket.
                    logger.error(
                        "Keep-alive: nessun PONG da %.0fs — link half-open: "
                        "chiudo la sessione per liberare il pairing sul relay",
                        time.time() - last_pong,
                    )
                    try:
                        await self._send_async(
                            Message.disconnect("keep-alive timeout (half-open link)"),
                            bypass_backpressure=True,
                        )
                    except Exception:
                        pass
                    self.inbox.put(
                        ("error", "Connection half-open (no PONG from relay)", self.session_seq)
                    )
                    self.inbox.put(("disconnected", None, self.session_seq))
                    await self._close_writer()
                    return
            except Exception as e:  # noqa: BLE001 — non far morire il keep-alive
                logger.debug("ping_loop: invio PING fallito (%s), proseguo", e)

    # ── lifecycle ───────────────────────────────────────────────────

    async def _close_writer(self) -> None:
        """Chiude e rilascia il TCP writer (idempotente).

        Indispensabile a ogni uscita dai loop ``_run_host``/``_run_client``:
        senza, un break da ERROR (es. 409 "already paired") lascerebbe il
        socket aperto fino a fine-processo — il relay terrebbe il peer
        "connesso" e l'host resterebbe paired anche dopo la chiusura.
        """
        writer = self._writer
        if writer is None:
            return
        self._writer = None
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
        except Exception:
            pass

    def start(self) -> None:
        """Run the asyncio event loop in the current thread (blocking)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._incoming = asyncio.Queue()
        self._running.set()
        try:
            if self.role == RelayRole.HOST:
                self._loop.run_until_complete(self._run_host())
            else:
                self._loop.run_until_complete(self._run_client())
        finally:
            self._running.clear()
            self._loop.close()
            self._loop = None

    def stop(self) -> None:
        """Signal the event loop to stop."""
        if self._loop and self._running.is_set():
            asyncio.run_coroutine_threadsafe(self._stop_async(), self._loop)

    async def _stop_async(self) -> None:
        """Cleanly stop the network session.

        Closes the TCP writer first — this causes the reader to fail
        with ``ConnectionError`` / ``IncompleteReadError``, which
        makes ``_read_loop`` exit naturally via its exception handler,
        without needing to cancel tasks manually.
        """
        self._running.clear()
        if self._writer:
            # DISCONNECT esplicito PRIMA del close: avvisa il relay (che
            # rimuove il peer e scolla l'host con 410) anche nel caso in
            # cui il reader locale non vedesse EOF per tempo.  Best-effort:
            # una rete morta non deve bloccare lo shutdown dell'UI.
            try:
                await asyncio.wait_for(
                    self._send_async(
                        Message.disconnect("session closed"), bypass_backpressure=True
                    ),
                    timeout=2.0,
                )
            except Exception:
                pass
        await self._close_writer()
        if self._decoder:
            self._decoder.release()
            self._decoder = None
        # Small yield so shutdown tasks can propagate
        await asyncio.sleep(0)

    # ── sending ─────────────────────────────────────────────────────

    # Message types that are internal to the relay protocol and must be
    # sent directly (not wrapped in RELAY_ROUTE).
    #
    # KEY_EXCHANGE / KEY_EXCHANGE_ACK are NOT relay control types:
    # they are peer-to-peer messages that must be wrapped in RELAY_ROUTE
    # so the relay forwards them as opaque data to the paired peer.
    # Key exchange now happens AFTER authentication and is protected by a
    # password-based proof (HMAC of the public key), so a relay cannot
    # substitute a key without the session password.
    _RELAY_CONTROL_TYPES = frozenset(
        {
            MessageType.HELLO,
            MessageType.HELLO_ACK,
            MessageType.AUTH_REQUEST,
            MessageType.AUTH_RESPONSE,
            MessageType.AUTH_OK,
            MessageType.AUTH_FAIL,
            MessageType.SESSION_INFO,
            MessageType.PING,
            MessageType.PONG,
            MessageType.DISCONNECT,
            MessageType.ERROR,
            MessageType.RELAY_REGISTER,
            MessageType.RELAY_ROUTE,
            MessageType.RELAY_PEER_LIST,
            MessageType.RELAY_DEVICE_LIST,
            MessageType.RELAY_DEVICE_UPDATE,
        }
    )

    # Message types that should never be dropped by backpressure.
    # Everything except video frames/tiles is considered critical.
    _BACKPRESSURE_BYPASS = frozenset(
        {
            MessageType.HELLO,
            MessageType.HELLO_ACK,
            MessageType.KEY_EXCHANGE,
            MessageType.KEY_EXCHANGE_ACK,
            MessageType.AUTH_REQUEST,
            MessageType.AUTH_RESPONSE,
            MessageType.AUTH_OK,
            MessageType.AUTH_FAIL,
            MessageType.SESSION_INFO,
            MessageType.PING,
            MessageType.PONG,
            MessageType.DISCONNECT,
            MessageType.ERROR,
            MessageType.RELAY_REGISTER,
            MessageType.RELAY_ROUTE,
            MessageType.RELAY_PEER_LIST,
            MessageType.RELAY_DEVICE_LIST,
            MessageType.RELAY_DEVICE_UPDATE,
            MessageType.MOUSE_EVENT,
            MessageType.KEYBOARD_EVENT,
            MessageType.TEXT_INPUT,
            MessageType.CAPS_LOCK_STATE,
            MessageType.CLIPBOARD_TEXT,
            MessageType.CLIPBOARD_IMAGE,
            MessageType.CLIPBOARD_SYNC,
            MessageType.FILE_REQUEST,
            MessageType.FILE_ACCEPT,
            MessageType.FILE_REJECT,
            MessageType.FILE_CHUNK,
            MessageType.FILE_COMPLETE,
            MessageType.FILE_ERROR,
            MessageType.FILE_PROGRESS,
            MessageType.FILE_LIST_REQUEST,
            MessageType.FILE_LIST_RESPONSE,
            MessageType.FILE_DOWNLOAD_REQUEST,
            MessageType.FILE_DOWNLOAD_ACCEPT,
            MessageType.FILE_DOWNLOAD_REJECT,
            MessageType.AUDIO_FRAME,
            MessageType.CAMERA_FRAME,
            MessageType.CAMERA_START,
            MessageType.CHAT_MESSAGE,
            MessageType.CHAT_TYPING,
            MessageType.CHAT_OPEN,
            MessageType.VIDEO_REQUEST_KEYFRAME,
        }
    )

    # Peer-to-peer message types that are forwarded to the UI thread
    # via the generic "message" inbox event.  They need no special
    # handling in the network thread — we just suppress the
    # "unhandled message type" debug log to avoid flooding the log
    # with high-frequency types like AUDIO_FRAME and CAMERA_FRAME.
    _PEER_PASSTHROUGH = frozenset(
        {
            MessageType.MOUSE_EVENT,
            MessageType.KEYBOARD_EVENT,
            MessageType.TEXT_INPUT,
            MessageType.CAPS_LOCK_STATE,
            MessageType.CLIPBOARD_TEXT,
            MessageType.CLIPBOARD_IMAGE,
            MessageType.CLIPBOARD_SYNC,
            MessageType.FILE_REQUEST,
            MessageType.FILE_ACCEPT,
            MessageType.FILE_REJECT,
            MessageType.FILE_CHUNK,
            MessageType.FILE_COMPLETE,
            MessageType.FILE_ERROR,
            MessageType.FILE_PROGRESS,
            MessageType.FILE_LIST_REQUEST,
            MessageType.FILE_LIST_RESPONSE,
            MessageType.FILE_DOWNLOAD_REQUEST,
            MessageType.FILE_DOWNLOAD_ACCEPT,
            MessageType.FILE_DOWNLOAD_REJECT,
            MessageType.AUDIO_FRAME,
            MessageType.CAMERA_FRAME,
            MessageType.CAMERA_START,
            MessageType.CHAT_MESSAGE,
            MessageType.CHAT_TYPING,
            MessageType.CHAT_OPEN,
        }
    )

    def _key_exchange_message(self, message_type: MessageType) -> Message:
        """Build an ephemeral public-key message for E2E encryption setup.

        Include a *proof* = HMAC-SHA256(secret, public_key) so the peer can
        detect a malicious relay substituting a different key in transit.
        The secret is the session password, or the per-device secret for
        trusted (password-less) sessions (also sent as ``trust_secret`` so
        the peer can pin it).
        """
        public_key = self._e2ee.get_public_key_string()
        secret = self._proof_secret or self.password
        payload: dict[str, Any] = {
            "public_key": public_key,
            "device_id": self.device_id,
        }
        if secret:
            payload["proof"] = compute_response(public_key, secret)
        if self._proof_secret:
            # Trusted session: hand the per-device secret to the peer so a
            # device that doesn't know the session password can verify the
            # key.  The peer pins it (TOFU) and rejects later changes.
            payload["trust_secret"] = self._proof_secret
        return Message(message_type, payload)

    def _accept_remote_key(self, payload: dict[str, Any]) -> bool:
        """Accept the peer's public key and activate E2E encryption.

        Verifies the *proof* before trusting the key: a peer that knows the
        session password (or the per-device secret) can compute it, a relay
        that only forwards traffic cannot forge it for a substituted key.
        This binds the ephemeral public key to the authenticated channel.

        Password-less (trusted-device) sessions fall back to accepting the
        key unverified when no verifiable proof is available, otherwise the
        peer could never establish E2EE.
        """
        public_key = payload.get("public_key", "")
        proof = payload.get("proof", "")
        trust_secret = payload.get("trust_secret", "")
        sender_id = payload.get("device_id", "")
        if not isinstance(public_key, str) or not public_key:
            logger.warning(
                "Rejected E2E key exchange: missing or invalid public_key (type=%s)",
                type(public_key).__name__,
            )
            return False

        # Per-device secret takes precedence over the session password for
        # trusted (password-less) sessions.
        secret = trust_secret if isinstance(trust_secret, str) and trust_secret else self.password

        # Pin the peer's per-device secret and reject silent changes (a
        # relay that swaps the secret would otherwise be able to MITM).
        if trust_secret and sender_id:
            known = self._known_device_secrets.get(sender_id)
            if known and known != trust_secret:
                logger.warning(
                    "Rejected E2E key exchange: per-device secret for %s changed "
                    "(possible relay MITM)",
                    sender_id[:8],
                )
                return False

        verified = bool(proof) and verify_response(public_key, secret, proof)
        if not verified:
            if not self.password:
                # Password-less session (trusted-device bypass): the peer may
                # not be able to produce a password proof.  Accept the key
                # unverified rather than dropping the encrypted stream.
                logger.warning(
                    "E2E key proof non verificabile su sessione senza password — "
                    "accetto la chiave senza verifica (trusted device)",
                )
            else:
                logger.warning(
                    "Rejected E2E key exchange: invalid or missing password proof "
                    "(possible relay MITM)",
                )
                return False

        try:
            self._e2ee.set_remote_key(public_key)
        except Exception as e:
            logger.warning("Rejected E2E key exchange: %s", e)
            return False
        self._e2ee_ready = True
        if verified and trust_secret and sender_id:
            # Use the per-device secret for our own proof (e.g. the
            # KEY_EXCHANGE_ACK the client sends back) and pin it locally.
            self._proof_secret = trust_secret
            self._known_device_secrets[sender_id] = trust_secret
            if self.role == RelayRole.CLIENT:
                self.inbox.put(("trust_secret", (sender_id, trust_secret), self.session_seq))
        logger.info("E2E peer channel established (key verified=%s)", verified)
        return True

    def _encrypt_peer_message(self, msg: Message) -> Message:
        """Encrypt a peer payload while preserving its message type."""
        if not self._e2ee_enabled or not self._e2ee_ready:
            return msg
        plaintext = msgpack.packb(msg.payload, use_bin_type=True)
        encrypted = self._e2ee.encrypt(plaintext)
        return Message(
            msg.type,
            {"_e2ee": encrypted.encode()},
            encrypted=True,
        )

    def _decrypt_peer_message(self, msg: Message) -> Message:
        """Decrypt an E2E envelope received from the relay."""
        envelope = msg.payload.get("_e2ee")
        if envelope is None:
            return msg
        if not self._e2ee_ready or not isinstance(envelope, bytes):
            raise ValueError("Encrypted peer payload received before E2E setup")
        try:
            payload = msgpack.unpackb(
                self._e2ee.decrypt(EncryptedMessage.decode(envelope)),
                raw=False,
            )
        except Exception as e:
            raise ValueError("Unable to decrypt peer payload") from e
        if not isinstance(payload, dict):
            raise ValueError("Invalid decrypted peer payload")
        return Message(msg.type, payload)

    # ── direct P2P ──────────────────────────────────────────────────

    def _schedule_p2p_send(self, msg: Message) -> bool:
        """Queue a message for the P2P loop (thread-safe).

        This is called from the UI/network-worker thread while the data
        channel lives in the asyncio loop.  The pending counter mirrors the
        relay backpressure behaviour.
        """
        if not self._p2p_ready or self._p2p is None or self._loop is None:
            return False
        with self._send_lock:
            if self._pending_p2p_sends >= _SEND_MAX_PENDING:
                return False
            self._pending_p2p_sends += 1
        try:
            self._loop.call_soon_threadsafe(self._send_p2p_now, msg)
        except RuntimeError:
            with self._send_lock:
                self._pending_p2p_sends = max(0, self._pending_p2p_sends - 1)
            return False
        return True

    def _send_p2p_now(self, msg: Message) -> None:
        try:
            if self._p2p is not None and self._p2p_ready:
                self._p2p.send(msg)
        finally:
            with self._send_lock:
                self._pending_p2p_sends = max(0, self._pending_p2p_sends - 1)

    async def _send_peer_signalling(self, msg: Message) -> None:
        """Send a P2P signalling message through the relay.

        Once E2EE is ready the SDP is encrypted like any other peer payload,
        so the relay cannot read local IP addresses or DTLS fingerprints.
        """
        inner = self._encrypt_peer_message(msg)
        await self._send_async(
            Message.relay_route(inner.type.value, inner.payload),
            bypass_backpressure=True,
        )

    def _maybe_start_p2p(self) -> None:
        """Start the host-side P2P offer when authentication is complete."""
        if not self._p2p_enabled or self._p2p_negotiation_started:
            return
        if self.role != RelayRole.HOST or not self._authenticated:
            return
        if self._e2ee_enabled and not self._e2ee_ready:
            return
        self._p2p_negotiation_started = True
        asyncio.create_task(self._start_p2p_offer())

    async def _start_p2p_offer(self) -> None:
        try:
            self._p2p = P2PTransport(
                ice_servers=self._ice_servers,
                on_message=self._on_p2p_message,
                on_state_change=self._on_p2p_state_change,
            )
            sdp = await self._p2p.create_offer()
            await self._send_peer_signalling(Message.p2p_offer(sdp))
            logger.info("P2P offer sent to peer")
        except Exception as e:
            logger.warning("P2P offer failed, continuing on relay: %s", e)
            self._close_p2p()

    async def _handle_p2p_offer(self, payload: dict) -> None:
        if not self._p2p_enabled:
            return
        sdp = payload.get("sdp", "")
        if not isinstance(sdp, str) or not sdp:
            logger.warning("Ignoring P2P offer without SDP")
            return
        try:
            if self._p2p is None:
                self._p2p = P2PTransport(
                    ice_servers=self._ice_servers,
                    on_message=self._on_p2p_message,
                    on_state_change=self._on_p2p_state_change,
                )
            answer = await self._p2p.accept_offer(sdp)
            await self._send_peer_signalling(Message.p2p_answer(answer))
            logger.info("P2P answer sent to peer")
        except Exception as e:
            logger.warning("P2P offer handling failed, using relay: %s", e)
            await self._send_peer_signalling(Message.p2p_close(str(e)))
            self._close_p2p()

    async def _handle_p2p_answer(self, payload: dict) -> None:
        sdp = payload.get("sdp", "")
        if self._p2p is None or not isinstance(sdp, str) or not sdp:
            return
        try:
            await self._p2p.accept_answer(sdp)
            logger.info("P2P answer accepted")
        except Exception as e:
            logger.warning("P2P answer failed, continuing on relay: %s", e)
            await self._send_peer_signalling(Message.p2p_close(str(e)))
            self._close_p2p()

    def _on_p2p_message(self, data: bytes) -> None:
        try:
            msg = self._decrypt_peer_message(Message.decode(data))
        except Exception as e:
            logger.warning("Dropped P2P message: %s", e)
            return
        if self._incoming is not None:
            self._incoming.put_nowait(msg)

    def _on_p2p_state_change(self, state: str) -> None:
        if state == "connected":
            self._p2p_ready = True
            logger.info("P2P direct path active")
            self.inbox.put(("p2p_state", state, self.session_seq))
        elif state in ("failed", "closed"):
            self._p2p_ready = False
            logger.info("P2P path %s — falling back to relay", state)
            self.inbox.put(("p2p_state", state, self.session_seq))
            if not self._relay_fallback and self._running.is_set():
                self.inbox.put(
                    (
                        "error",
                        "P2P connection failed and relay fallback is disabled",
                        self.session_seq,
                    )
                )
                self._running.clear()

    def _close_p2p(self) -> None:
        self._p2p_ready = False
        if self._p2p is not None:
            self._p2p.close()
            self._p2p = None

    async def _aclose_p2p(self) -> None:
        """Close the P2P transport and wait for aiortc teardown."""
        self._p2p_ready = False
        transport = self._p2p
        self._p2p = None
        if transport is not None:
            await transport.aclose()

    async def _relay_reader_task(self) -> None:
        """Read relay messages and feed the common incoming queue."""
        try:
            async for msg in self._read_loop():
                if self._incoming is not None:
                    await self._incoming.put(msg)
        finally:
            if self._incoming is not None:
                self._incoming.put_nowait(_INCOMING_CLOSED)

    async def _next_incoming(self) -> Message | None:
        """Return the next relay/P2P message, or None when the session ends."""
        if self._incoming is None:
            return None
        while self._running.is_set():
            try:
                item = await asyncio.wait_for(self._incoming.get(), timeout=1.0)
            except TimeoutError:
                continue
            if item is _INCOMING_CLOSED:
                return None
            return item
        return None

    def send_message(self, msg: Message) -> bool:
        """Send a message over the relay connection (thread-safe).

        Peer-to-peer messages (video frames, input events, clipboard,
        file transfer, chat, audio) are automatically wrapped in
        ``RELAY_ROUTE`` so the relay server forwards them to the
        paired peer.  Relay-internal control messages are sent as-is.

        Returns True if the message was queued, False if backpressure
        kicked in (too many pending sends).  Control messages always
        succeed.
        """
        if self._loop and self._running.is_set():
            is_relay_control = msg.type in self._RELAY_CONTROL_TYPES
            bypass_backpressure = msg.type in self._BACKPRESSURE_BYPASS

            # Encrypt peer-to-peer messages once, before choosing the path.
            # Relay-internal control messages are sent as-is.
            if not is_relay_control:
                msg = self._encrypt_peer_message(msg)

                # Prefer the direct data channel when it is established.
                # Media is dropped on P2P backpressure instead of being sent
                # twice; non-media may still fall back to the relay.
                if self._p2p_ready:
                    if self._schedule_p2p_send(msg):
                        return True
                    if msg.type in MEDIA_MESSAGE_TYPES:
                        return False

                msg = Message.relay_route(
                    inner_type=msg.type.value,
                    inner_payload=msg.payload,
                )

            # Backpressure: only drop video frames/tiles when the send pipe is full
            if not bypass_backpressure:
                if self._pending_sends >= _SEND_MAX_PENDING:
                    logger.debug(
                        "Send backpressure: %d pending sends, dropping message type=%s",
                        self._pending_sends,
                        msg.type.name if hasattr(msg.type, "name") else msg.type,
                    )
                    return False
                with self._send_lock:
                    self._pending_sends += 1

            # Serializza sul thread chiamante (tipicamente NetworkWorker):
            # così l'event loop asyncio esegue solo write/drain e non resta
            # bloccato dal msgpack di frame/keyframe grandi.
            data = msg.encode()
            asyncio.run_coroutine_threadsafe(
                self._write_async(data, bypass_backpressure), self._loop
            )
            return True
        return False

    async def _send_async(self, msg: Message, bypass_backpressure: bool = False) -> None:
        await self._write_async(msg.encode(), bypass_backpressure)

    async def _write_async(self, data: bytes, bypass_backpressure: bool = False) -> None:
        if self._writer is None:
            if not bypass_backpressure:
                with self._send_lock:
                    self._pending_sends = max(0, self._pending_sends - 1)
            return
        try:
            self._writer.write(data)
            await asyncio.wait_for(
                self._writer.drain(),
                timeout=_SEND_DRAIN_TIMEOUT,
            )
            # Reset timeout counter on success
            self._drain_timeout_count = 0
        except TimeoutError:
            self._drain_timeout_count = getattr(self, "_drain_timeout_count", 0) + 1
            logger.warning(
                "Send drain timeout (%d/%d) — network congested or peer unresponsive",
                self._drain_timeout_count,
                _SEND_MAX_CONSECUTIVE_TIMEOUTS,
            )
            if self._drain_timeout_count >= _SEND_MAX_CONSECUTIVE_TIMEOUTS:
                logger.error(
                    "Send drain timed out %d times consecutively — closing connection",
                    self._drain_timeout_count,
                )
                if self._writer:
                    try:
                        self._writer.close()
                    except Exception:
                        pass
                    self._writer = None
                self.inbox.put(
                    ("error", "Connection stalled (send buffer full)", self.session_seq)
                )
                self.inbox.put(("disconnected", None, self.session_seq))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Send error: %s", e)
            self.inbox.put(("error", f"Send failed: {e}", self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))
        finally:
            if not bypass_backpressure:
                with self._send_lock:
                    self._pending_sends = max(0, self._pending_sends - 1)

    # ── host flow ───────────────────────────────────────────────────

    async def _run_host(self) -> None:
        logger.info("Host connecting to relay %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=_CONNECT_TIMEOUT,
            )
        except TimeoutError:
            err = f"Connection to relay {self.host}:{self.port} timed out ({_CONNECT_TIMEOUT}s)"
            logger.error(err)
            self.inbox.put(("error", err, self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))
            return
        except OSError as e:
            logger.error("Host connection failed: %s", e)
            self.inbox.put(("error", str(e), self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))
            return

        # Register session with device identity
        await self._send_async(
            Message.relay_register(
                session_id=self.session_id,
                device_id=self.device_id,
                device_name=self.device_name,
            )
        )

        ping_task = asyncio.create_task(self._ping_loop())
        reader_task = asyncio.create_task(self._relay_reader_task())
        try:
            while self._running.is_set():
                msg = await self._next_incoming()
                if msg is None:
                    break
                try:
                    msg = self._decrypt_peer_message(msg)
                except ValueError as e:
                    logger.warning("Dropped encrypted host message: %s", e)
                    continue
                if msg.type not in _P2P_SIGNAL_TYPES:
                    self.inbox.put(("message", msg, self.session_seq))

                t = msg.type
                if t == MessageType.RELAY_REGISTER:
                    self.inbox.put(("connected", ("host", self.session_id), self.session_seq))

                elif t == MessageType.RELAY_PEER_LIST:
                    self.inbox.put(("peer_joined", None, self.session_seq))
                    # New peer: reset the E2EE/P2P state.  The host session is
                    # persistent and handles peers sequentially, so keeping
                    # the previous peer's key or data channels would make the
                    # new peer unable to decrypt the stream (black screen).
                    self._close_p2p()
                    self._authenticated = False
                    self._p2p_negotiation_started = False
                    self._e2ee = E2EEncryption()
                    self._e2ee_ready = False
                    self._proof_secret = ""
                    # Authentication first, E2E key exchange after (see AUTH_RESPONSE).
                    # This ensures trusted devices can establish E2E even with
                    # different (or empty) passwords.
                    nonce = generate_nonce()
                    self._auth_nonce = nonce
                    await self._send_async(Message.auth_request(self.session_id, nonce=nonce))

                elif t == MessageType.RELAY_DEVICE_LIST:
                    devices = msg.payload.get("devices", [])
                    self.inbox.put(("device_list", devices, self.session_seq))

                elif t == MessageType.RELAY_DEVICE_UPDATE:
                    device = msg.payload.get("device", {})
                    online = msg.payload.get("online", False)
                    self.inbox.put(("device_update", (device, online), self.session_seq))

                elif t == MessageType.KEY_EXCHANGE_ACK:
                    if not self._accept_remote_key(msg.payload):
                        logger.warning(
                            "E2E key exchange ACK failed, continuing without encryption"
                        )
                    else:
                        self._maybe_start_p2p()

                elif t == MessageType.P2P_ANSWER:
                    await self._handle_p2p_answer(msg.payload)

                elif t == MessageType.P2P_CLOSE:
                    logger.info("Peer closed the P2P path — using relay")
                    self._close_p2p()

                elif t == MessageType.AUTH_RESPONSE:
                    client_hash = msg.payload.get("nonce_hash", "")
                    client_device_id = msg.payload.get("device_id", "")
                    client_connection_mode = msg.payload.get("connection_mode", "remote_desktop")
                    self.connection_mode = client_connection_mode

                    # Trusted device bypass: skip password verification
                    if client_device_id and client_device_id in self._trusted_device_ids:
                        logger.info(
                            "Trusted device '%s' authenticated without password",
                            client_device_id[:8],
                        )
                        # Use the per-device secret for the E2EE proof so the
                        # peer doesn't need to know the session password.
                        self._proof_secret = self._trusted_devices.get(client_device_id, "")
                        self._authenticated = True
                        await self._send_async(Message.auth_ok())
                        # E2E key exchange after authentication
                        if self._e2ee_enabled:
                            await self._send_async(
                                Message.relay_route(
                                    inner_type=MessageType.KEY_EXCHANGE.value,
                                    inner_payload=self._key_exchange_message(
                                        MessageType.KEY_EXCHANGE
                                    ).payload,
                                )
                            )
                        self._maybe_start_p2p()
                        self.inbox.put(
                            (
                                "auth_result",
                                (True, "Authenticated (trusted device)"),
                                self.session_seq,
                            )
                        )
                        continue

                    success = verify_response(self._auth_nonce, self.password, client_hash)
                    if success:
                        self._authenticated = True
                        await self._send_async(Message.auth_ok())
                        # E2E key exchange after authentication
                        if self._e2ee_enabled:
                            await self._send_async(
                                Message.relay_route(
                                    inner_type=MessageType.KEY_EXCHANGE.value,
                                    inner_payload=self._key_exchange_message(
                                        MessageType.KEY_EXCHANGE
                                    ).payload,
                                )
                            )
                        self._maybe_start_p2p()
                        self.inbox.put(("auth_result", (True, "Authenticated"), self.session_seq))
                        logger.debug("Host auth OK — connection_mode=%s", client_connection_mode)
                    else:
                        await self._send_async(Message.auth_fail("Invalid credentials"))
                        self.inbox.put(
                            ("auth_result", (False, "Invalid credentials"), self.session_seq)
                        )

                elif t == MessageType.VIDEO_REQUEST_KEYFRAME:
                    logger.info("Peer requested keyframe (host)")
                    self.inbox.put(("keyframe_requested", None, self.session_seq))

                elif t == MessageType.ERROR:
                    raw_err = msg.payload.get("message")
                    relay_err = raw_err if isinstance(raw_err, str) else None
                    if relay_err is None:
                        # Payload senza 'message' — logghiamo il payload completo per debug
                        logger.warning(
                            "Host received ERROR from relay without message field "
                            "(payload=%s, code=%s) — possible protocol mismatch",
                            {
                                k: (
                                    str(v)[:120]
                                    if isinstance(v, (bytes, str)) and len(str(v)) > 120
                                    else v
                                )
                                for k, v in msg.payload.items()
                            },
                            msg.payload.get("code", "N/A"),
                        )
                        relay_err = f"Relay error (code={msg.payload.get('code', 'N/A')})"
                    # "Peer disconnected" is expected when the remote client leaves.
                    # Treat it as a peer event, not a relay error.
                    if "Peer disconnected" in relay_err:
                        logger.info("Peer disconnected from our session")
                        self.inbox.put(("peer_disconnected", None, self.session_seq))
                    else:
                        logger.warning("Host received ERROR from relay: %s", relay_err)
                        self.inbox.put(("error", relay_err, self.session_seq))

                elif t == MessageType.DISCONNECT:
                    logger.debug("Host received DISCONNECT — breaking loop")
                    break

                elif t in self._PEER_PASSTHROUGH:
                    pass  # forwarded to UI via generic "message" inbox event
                elif t == MessageType.PONG:
                    self._last_pong_time = time.time()
                    logger.debug("Host keep-alive PONG received")

                elif t == MessageType.PING:
                    await self._send_async(Message.pong())

                else:
                    logger.debug("Host received unhandled message type %s", t)
        finally:
            ping_task.cancel()
            reader_task.cancel()
            await self._aclose_p2p()
            await self._close_writer()
        self.inbox.put(("disconnected", None, self.session_seq))
        logger.debug("Host session ended: disconnected event queued")

    # ── client flow ─────────────────────────────────────────────────

    async def _run_client(self) -> None:
        logger.info("Client connecting to relay %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=_CONNECT_TIMEOUT,
            )
        except TimeoutError:
            err = f"Connection to relay {self.host}:{self.port} timed out ({_CONNECT_TIMEOUT}s)"
            logger.error(err)
            self.inbox.put(("error", err, self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))
            return
        except OSError as e:
            logger.error("Client connection failed: %s", e)
            self.inbox.put(("error", str(e), self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))
            return

        # Look up device by ID (device_id) → relay pairs us with its session
        await self._send_async(
            Message(
                MessageType.RELAY_REGISTER,
                {"lookup_device": self.session_id},
            )
        )

        # Periodic task that requests a keyframe if none received.
        # Backoff esponenziale (3→6→12→…max 60s): senza limiti la richiesta
        # genera un keyframe 1080p a ogni tick — se il link è congestionato
        # il keyframe viene droppato dal backpressure, il client non lo
        # riceve e ne richiede un altro, mantenendo la congestione: immagine
        # congelata per sempre con sessione comunque connessa.
        _frame_count = 0  # track frames received since last watchdog check
        _watchdog_delay = 3.0

        async def _keyframe_watchdog():
            nonlocal _frame_count, _watchdog_delay
            while self._running.is_set():
                await asyncio.sleep(_watchdog_delay)
                if not self._running.is_set():
                    break
                last_activity = self._last_video_activity_time or self._start_time
                elapsed_since_activity = time.time() - last_activity
                if elapsed_since_activity > 5.0:
                    logger.warning(
                        "No video activity for %.0fs (frames received: %d) — "
                        "requesting keyframe (next attempt in %.0fs)",
                        elapsed_since_activity,
                        _frame_count,
                        _watchdog_delay * 2,
                    )
                    _frame_count = 0
                    self._last_video_activity_time = time.time()
                    await self._send_async(
                        Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                    )
                    _watchdog_delay = min(_watchdog_delay * 2, 60.0)
                else:
                    _watchdog_delay = 3.0

        watchdog_task = asyncio.create_task(_keyframe_watchdog())
        ping_task = asyncio.create_task(self._ping_loop())
        reader_task = asyncio.create_task(self._relay_reader_task())
        try:
            while self._running.is_set():
                msg = await self._next_incoming()
                if msg is None:
                    break
                try:
                    msg = self._decrypt_peer_message(msg)
                except ValueError as e:
                    logger.warning("Dropped encrypted client message: %s", e)
                    continue
                if msg.type not in _P2P_SIGNAL_TYPES:
                    self.inbox.put(("message", msg, self.session_seq))

                t = msg.type
                if t == MessageType.RELAY_REGISTER:
                    if msg.payload.get("paired"):
                        self.inbox.put(
                            ("connected", ("client", self.session_id), self.session_seq)
                        )
                    elif msg.payload.get("mode") == "host":
                        # Session doesn't exist — we were registered as host instead
                        logger.warning("Session %s not found on relay", self.session_id)
                        self.inbox.put(
                            ("error", f"Session {self.session_id} not found", self.session_seq)
                        )
                        self.inbox.put(("disconnected", None, self.session_seq))
                        break

                elif t == MessageType.P2P_OFFER:
                    await self._handle_p2p_offer(msg.payload)

                elif t == MessageType.P2P_CLOSE:
                    logger.info("Peer closed the P2P path — using relay")
                    self._close_p2p()

                elif t == MessageType.KEY_EXCHANGE:
                    if not self._e2ee_enabled:
                        logger.info("Peer offered E2E, but it is disabled locally")
                    elif self._accept_remote_key(msg.payload):
                        await self._send_async(
                            Message.relay_route(
                                inner_type=MessageType.KEY_EXCHANGE_ACK.value,
                                inner_payload=self._key_exchange_message(
                                    MessageType.KEY_EXCHANGE_ACK
                                ).payload,
                            )
                        )
                    else:
                        logger.warning("E2E key exchange failed, continuing without encryption")

                elif t == MessageType.AUTH_REQUEST:
                    nonce = msg.payload.get("nonce", "")
                    nonce_hash = compute_response(nonce, self.password) if nonce else ""
                    await self._send_async(
                        Message.auth_response(
                            nonce_hash,
                            device_id=self.device_id,
                            connection_mode=self.connection_mode,
                        )
                    )
                    self.inbox.put(("auth_requested", None, self.session_seq))

                elif t == MessageType.AUTH_OK:
                    self._authenticated = True
                    self.inbox.put(("auth_result", (True, "Authenticated"), self.session_seq))
                    if self.connection_mode == "remote_desktop":
                        # Richiedi subito un keyframe per avviare lo streaming
                        logger.info("Auth OK — requesting first keyframe from host")
                        await self._send_async(
                            Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                        )
                    else:
                        logger.info("Auth OK — file transfer mode, skipping keyframe request")

                elif t == MessageType.AUTH_FAIL:
                    reason = msg.payload.get("reason", "Authentication failed")
                    self.inbox.put(("auth_result", (False, reason), self.session_seq))
                    break

                elif t == MessageType.RELAY_DEVICE_LIST:
                    devices = msg.payload.get("devices", [])
                    self.inbox.put(("device_list", devices, self.session_seq))

                elif t == MessageType.RELAY_DEVICE_UPDATE:
                    device = msg.payload.get("device", {})
                    online = msg.payload.get("online", False)
                    self.inbox.put(("device_update", (device, online), self.session_seq))

                elif t == MessageType.VIDEO_FRAME:
                    payload = msg.payload
                    width = payload.get("width", 0)
                    height = payload.get("height", 0)
                    data = payload.get("data")
                    is_keyframe = payload.get("keyframe", False)
                    logger.debug(
                        "VIDEO_FRAME: %dx%d keyframe=%s len=%d",
                        width,
                        height,
                        is_keyframe,
                        len(data) if data else 0,
                    )
                    if data and width > 0 and height > 0:
                        if self._decoder is None:
                            self._decoder = VideoDecoder(codec="")
                            logger.info("VideoDecoder initialised (auto-detect)")
                        try:
                            rgb = self._decoder.decode(
                                data,
                                width,
                                height,
                                is_keyframe=is_keyframe,
                            )
                            if rgb is not None:
                                _frame_count += 1
                                # decode() restituisce un array fresco a ogni
                                # chiamata: il riferimento (mutato in-place
                                # dalla composizione tile) richiede una copia,
                                # ma l'inbox può prendere rgb direttamente.
                                self._reference_frame = rgb.copy()
                                self._frame_width = width
                                self._frame_height = height
                                now = time.time()
                                self._last_keyframe_time = now
                                self._last_video_activity_time = now
                                self.inbox.put(
                                    ("frame", (rgb, width, height), self.session_seq),
                                )
                                logger.debug(
                                    "Frame decoded: %dx%d (%s)",
                                    width,
                                    height,
                                    self._decoder.codec_name,
                                )
                            else:
                                logger.warning(
                                    "Frame decode returned None%s",
                                    " - decoder not ready" if not is_keyframe else "",
                                )
                                if not is_keyframe and self._decoder is not None:
                                    self._decoder.reset()
                                    await self._send_async(
                                        Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                                    )
                        except Exception as e:
                            logger.error("Frame decode error: %s", e)
                            self._decoder.reset()
                            await self._send_async(
                                Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                            )
                    else:
                        logger.warning("VIDEO_FRAME with empty data")

                elif t == MessageType.VIDEO_TILE:
                    payload = msg.payload
                    data = payload.get("data")
                    tx = payload.get("x", 0)
                    ty = payload.get("y", 0)
                    tw = payload.get("width", 0)
                    th = payload.get("height", 0)
                    logger.debug(
                        "🧩 VIDEO_TILE: %dx%d+%d+%d len=%d ref=%s",
                        tw,
                        th,
                        tx,
                        ty,
                        len(data) if data else 0,
                        "yes" if self._reference_frame is not None else "no",
                    )
                    if data and tw > 0 and th > 0 and self._reference_frame is not None:
                        try:
                            import cv2
                            import numpy as np

                            arr = np.frombuffer(data, dtype=np.uint8)
                            tile_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if (
                                tile_bgr is not None
                                and tile_bgr.shape[0] == th
                                and tile_bgr.shape[1] == tw
                            ):
                                tile_rgb = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2RGB)
                                # Composite onto reference frame
                                ref_h, ref_w = self._reference_frame.shape[:2]
                                self._last_video_activity_time = time.time()
                                if ty + th <= ref_h and tx + tw <= ref_w:
                                    self._reference_frame[ty : ty + th, tx : tx + tw] = tile_rgb
                                    # Invia all'UI SOLO la regione del tile:
                                    # niente copia full-frame del reference
                                    # (~6 MB a 1080p) né ricostruzione
                                    # QPixmap per ogni tile.
                                    self.inbox.put(
                                        (
                                            "tile",
                                            (
                                                tile_rgb,
                                                tx,
                                                ty,
                                                tw,
                                                th,
                                                self._frame_width,
                                                self._frame_height,
                                            ),
                                            self.session_seq,
                                        ),
                                    )
                            else:
                                logger.warning(
                                    "Tile decode mismatch: expected %dx%d, got %s",
                                    tw,
                                    th,
                                    tile_bgr.shape[:2] if tile_bgr is not None else "None",
                                )
                        except Exception as e:
                            logger.warning("Tile decode/composite error: %s", e)
                    elif self._reference_frame is None:
                        # Nessun reference frame su cui comporre: il keyframe
                        # iniziale (o l'ultimo riallineamento) è andato perso
                        # per backpressure.  Non droppare in silenzio: chiedi
                        # un nuovo keyframe all'host, con anti-flood.
                        now = time.time()
                        if now - self._last_keyframe_request_ts > 5.0:
                            self._last_keyframe_request_ts = now
                            logger.debug("Tile dropped - no reference frame, requesting keyframe")
                            await self._send_async(
                                Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                            )
                elif t == MessageType.VIDEO_REQUEST_KEYFRAME:
                    logger.debug("Keyframe requested by peer")
                    # Forward to host via inbox so the StreamService can
                    # force a keyframe on the encoder.
                    self.inbox.put(("keyframe_requested", None, self.session_seq))

                elif t == MessageType.ERROR:
                    raw_err = msg.payload.get("message")
                    relay_err = raw_err if isinstance(raw_err, str) else None
                    if relay_err is None:
                        logger.warning(
                            "Client received ERROR without message field (payload=%s, code=%s)",
                            {
                                k: (
                                    str(v)[:120]
                                    if isinstance(v, (bytes, str)) and len(str(v)) > 120
                                    else v
                                )
                                for k, v in msg.payload.items()
                            },
                            msg.payload.get("code", "N/A"),
                        )
                        relay_err = f"Relay error (code={msg.payload.get('code', 'N/A')})"
                    logger.debug("Client received ERROR: %s — breaking loop", relay_err)
                    self.inbox.put(("error", relay_err, self.session_seq))
                    break

                elif t == MessageType.DISCONNECT:
                    logger.debug("Client received DISCONNECT — breaking loop")
                    break

                elif t in self._PEER_PASSTHROUGH:
                    pass  # forwarded to UI via generic "message" inbox event
                elif t == MessageType.PONG:
                    self._last_pong_time = time.time()
                    logger.debug("Client keep-alive PONG received")

                elif t == MessageType.PING:
                    await self._send_async(Message.pong())

                else:
                    logger.debug("Client received unhandled message type %s", t)
        finally:
            watchdog_task.cancel()
            ping_task.cancel()
            reader_task.cancel()
            await self._aclose_p2p()
            await self._close_writer()
        self.inbox.put(("disconnected", None, self.session_seq))
        logger.debug("Client session ended: disconnected event queued")

    # ── I/O helpers ─────────────────────────────────────────────────

    async def _read_loop(self):  # noqa: ANN201
        """Yield messages from the TCP stream."""
        while self._running.is_set():
            try:
                msg = await Message.from_reader(self._reader)
                yield msg
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                # If _running was cleared by _stop_async() this is an
                # intentional shutdown — log at DEBUG, not WARNING.
                if self._running.is_set():
                    logger.warning("Connection lost: %s", e)
                else:
                    logger.debug("Connection closed (session stopped)")
                break
            except asyncio.CancelledError:
                logger.debug("Read cancelled (shutting down)")
                break

    def send_frame(
        self, data: bytes, width: int, height: int, pts: int, keyframe: bool = False
    ) -> bool:
        """Send an encoded (H.264) VIDEO_FRAME over the relay.

        Returns True if queued, False if dropped due to backpressure.
        """
        logger.debug(
            "send_frame: %dx%d keyframe=%s len=%d pts=%d",
            width,
            height,
            keyframe,
            len(data),
            pts,
        )
        msg = Message.video_frame(
            data=data, width=width, height=height, pts=pts, keyframe=keyframe
        )
        return self.send_message(msg)


# ---------------------------------------------------------------------------
# RelayClient — QObject for UI integration
# ---------------------------------------------------------------------------


class RelayClient(QObject):
    """High-level relay client for Qt applications.

    Supports independent host (persistent) and client (on-demand)
    sessions.  The host session runs in the background and stays
    alive even while the user connects as a client to another device.

    Usage::

        client = RelayClient()
        client.host_connected.connect(lambda sid: print(f"Hosting: {sid}"))
        client.client_connected.connect(lambda sid: print(f"Client: {sid}"))
        client.frame_received.connect(viewer.display_frame)

        client.start_hosting("relay.example.com", 8474, "123456789", "mypass")
        client.join_session("relay.example.com", 8474, "123456789", "mypass")
    """

    # ── Host signals ──
    host_connected = Signal(str, str)  # role ("host"), session_id
    host_disconnected = Signal()
    host_peer_joined = Signal()
    host_auth_result = Signal(bool, str)  # success, message
    host_keyframe_requested = Signal()  # remote peer needs a keyframe
    host_peer_disconnected = Signal()  # remote peer left our hosted session

    # ── Client signals ──
    client_connected = Signal(str, str)  # role ("client"), session_id
    client_disconnected = Signal()
    client_auth_requested = Signal()
    client_auth_result = Signal(bool, str)  # success, message
    frame_received = Signal(np.ndarray, int, int)  # rgb_data, width, height
    tile_received = Signal(np.ndarray, int, int, int, int)  # rgb, x, y, w, h
    client_keyframe_requested = Signal()  # remote host needs a keyframe

    # ── Shared signals (from both host and client) ──
    message_received = Signal(object)  # Message
    error = Signal(str)
    device_list_received = Signal(list)  # list[dict] — devices from relay
    trust_secret_received = Signal(str, str)  # peer device_id, per-device secret
    p2p_state_changed = Signal(str)  # "connected" | "failed" | "closed"
    main_keyframe_requested = Signal()  # legacy alias for host_keyframe_requested

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # ── Host session (persistent background) ──
        self._host_session: _RelaySession | None = None
        self._host_thread: threading.Thread | None = None
        self._host_inbox: queue.Queue = queue.Queue()
        self._host_seq: int = 0
        self._host_role: RelayRole | None = None
        self._e2ee_enabled = cast(
            bool,
            QSettings("OpenDesk", "OpenDesk").value(
                "security/e2ee",
                True,
                type=bool,
            ),
        )
        self._p2p_enabled = False
        self._stun_server = ""
        self._turn_url = ""
        self._turn_username = ""
        self._turn_credential = ""
        self._relay_fallback = True
        self._ice_servers: list = []
        self._load_p2p_settings()

        # ── Client session (on-demand foreground) ──
        self._session: _RelaySession | None = None
        self._thread: threading.Thread | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._current_seq: int = 0
        self._role: RelayRole | None = None

        # ── Single timer to poll both inboxes ──
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_inboxes)
        self._timer.start(_POLL_INTERVAL_MS)

    def _load_p2p_settings(self) -> None:
        """Read P2P/ICE settings from QSettings.

        Called at session start so changes made in the settings dialog are
        picked up without restarting the application.
        """
        settings = QSettings("OpenDesk", "OpenDesk")
        self._p2p_enabled = cast(bool, settings.value("network/p2p_enabled", False, type=bool))
        self._stun_server = cast(
            str,
            settings.value("network/stun_server", "stun:stun.l.google.com:19302", type=str),
        )
        self._turn_url = cast(str, settings.value("network/turn_url", "", type=str))
        self._turn_username = cast(str, settings.value("network/turn_username", "", type=str))
        self._turn_credential = cast(str, settings.value("network/turn_credential", "", type=str))
        self._relay_fallback = cast(bool, settings.value("network/enable_relay", True, type=bool))
        self._ice_servers = build_ice_servers(
            stun_server=self._stun_server,
            turn_url=self._turn_url,
            turn_username=self._turn_username,
            turn_credential=self._turn_credential,
        )

    # ── public API ──────────────────────────────────────────────────

    # ── drain helpers ──

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        """Discard any stale events left in a queue."""
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _drain_inbox(self) -> None:
        """Discard any stale events left from a previous client session."""
        self._drain_queue(self._inbox)

    def _drain_host_inbox(self) -> None:
        """Discard any stale events left from a previous host session."""
        self._drain_queue(self._host_inbox)

    # ── hosting ──

    def start_hosting(
        self,
        host: str,
        port: int,
        session_id: str,
        password: str,
        device_id: str = "",
        device_name: str = "",
        trusted_devices: dict[str, str] | None = None,
    ) -> None:
        """Connect to relay and register as host.

        Creates an independent persistent host session.  Does NOT
        affect any active client session.
        """
        self._stop_host_session()
        self._load_p2p_settings()
        self._drain_host_inbox()
        self._host_seq += 1
        self._host_role = RelayRole.HOST
        self._host_session = _RelaySession(
            host,
            port,
            session_id,
            password,
            RelayRole.HOST,
            self._host_inbox,
            device_id=device_id,
            device_name=device_name,
            session_seq=self._host_seq,
            trusted_devices=trusted_devices,
            e2ee_enabled=self._e2ee_enabled,
            p2p_enabled=self._p2p_enabled,
            ice_servers=self._ice_servers,
            relay_fallback=self._relay_fallback,
        )
        self._start_host_thread()

    def stop_hosting(self) -> None:
        """Stop the persistent host session if active."""
        self._stop_host_session()

    def is_hosting(self) -> bool:
        """Check if the host session is active and connected."""
        return self._host_thread is not None and self._host_thread.is_alive()

    # ── client connection ──

    def join_session(
        self,
        host: str,
        port: int,
        session_id: str,
        password: str,
        device_id: str = "",
        connection_mode: str = "remote_desktop",
        known_device_secrets: dict[str, str] | None = None,
    ) -> None:
        """Connect to relay and join a session as client.

        Does NOT stop the host session — the host session continues
        running in the background, accepting incoming connections.

        Parameters
        ----------
        connection_mode : str
            "remote_desktop" (default) for full remote desktop,
            "file_transfer" for file-transfer-only mode.
        """
        # Only stop a previous CLIENT session, NOT the host session
        self._stop_client_session()
        self._load_p2p_settings()
        self._drain_inbox()
        self._current_seq += 1
        self._role = RelayRole.CLIENT
        self._session = _RelaySession(
            host,
            port,
            session_id,
            password,
            RelayRole.CLIENT,
            self._inbox,
            device_id=device_id,
            session_seq=self._current_seq,
            known_device_secrets=known_device_secrets,
            connection_mode=connection_mode,
            e2ee_enabled=self._e2ee_enabled,
            p2p_enabled=self._p2p_enabled,
            ice_servers=self._ice_servers,
            relay_fallback=self._relay_fallback,
        )
        self._start_client_thread()

    # ── message sending ──

    def send_message(self, msg: Message) -> bool:
        """Send a protocol message.

        If the client session is active, sends through it;
        otherwise falls back to the host session.
        Returns True if queued, False if dropped.
        """
        if self._session and self._thread and self._thread.is_alive():
            return self._session.send_message(msg)
        elif self._host_session and self._host_thread and self._host_thread.is_alive():
            return self._host_session.send_message(msg)
        return False

    def send_frame(
        self, data: bytes, width: int, height: int, pts: int, keyframe: bool = False
    ) -> bool:
        """Send an encoded H.264 video frame over the relay (host only).

        Always sent through the host session.
        Returns True if queued, False if dropped.
        """
        if self._host_session:
            return self._host_session.send_frame(data, width, height, pts, keyframe=keyframe)
        return False

    def send_tile(self, data: bytes, x: int, y: int, width: int, height: int, pts: int) -> bool:
        """Send a JPEG-encoded tile update over the relay (host only).

        Always sent through the host session.
        Returns True if queued, False if dropped.
        """
        if self._host_session:
            msg = Message.video_tile(data, x, y, width, height, pts)
            return self._host_session.send_message(msg)
        return False

    def send_mouse_event(
        self,
        x: int,
        y: int,
        button: int | None = None,
        pressed: bool | None = None,
        absolute: bool = True,
    ) -> None:
        """Send a mouse event to the remote peer."""
        logger.debug(
            "RelayClient.send_mouse_event: x=%d y=%d button=%s pressed=%s abs=%s "
            "session=%s host_session=%s",
            x,
            y,
            button,
            pressed,
            absolute,
            self._session is not None,
            self._host_session is not None,
        )
        self.send_message(Message.mouse_event(x, y, button, pressed, absolute))

    def send_key_event(self, key: str, pressed: bool) -> None:
        """Send a keyboard event to the remote peer."""
        self.send_message(Message.keyboard_event(key, pressed))

    def send_caps_lock_state(self, active: bool) -> None:
        """Send the local Caps Lock state so the remote host can sync it."""
        self.send_message(Message.caps_lock_state(active))

    def disconnect(self) -> None:  # type: ignore[override]
        """Disconnect from the relay — stops both host and client sessions."""
        self._stop_host_session()
        self._stop_client_session()
        self._role = None
        self._host_role = None

    def disconnect_client(self) -> None:
        """Disconnect only the client session; host session stays alive."""
        self._stop_client_session()
        self._role = None

    # ── properties ──

    @property
    def is_connected(self) -> bool:
        """Check if any session (host or client) is active."""
        return (self._thread is not None and self._thread.is_alive()) or (
            self._host_thread is not None and self._host_thread.is_alive()
        )

    @property
    def role(self) -> RelayRole | None:
        """Return the role of the active client session, or None."""
        return self._role

    @property
    def host_role(self) -> RelayRole | None:
        """Return HOST if the host session is active, else None."""
        return self._host_role if self.is_hosting() else None

    @property
    def client_connection_mode(self) -> str:
        """Return the connection mode of the most recently connected client.

        ``"remote_desktop"`` (default) or ``"file_transfer"``.
        Only meaningful on the host side after a client has authenticated.
        Returns ``"remote_desktop"`` if no host session is active.
        """
        if self._host_session is not None:
            return getattr(self._host_session, "connection_mode", "remote_desktop")
        return "remote_desktop"

    # ── host internals ──────────────────────────────────────────

    def _start_host_thread(self) -> None:
        """Start the background thread for the host session."""
        session = self._host_session
        if session is None:
            return
        old_thread = self._host_thread
        if old_thread and old_thread.is_alive():
            logger.info(
                "Previous host thread still alive — creating new thread for session %s",
                getattr(session, "session_id", "?"),
            )
        self._host_thread = threading.Thread(target=session.start, daemon=True)
        self._host_thread.start()

    def _stop_host_session(self) -> None:
        """Stop the host session if any."""
        if self._host_session is None or self._host_thread is None:
            return
        session = self._host_session
        self._host_session = None
        session.stop()
        self._host_thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if self._host_thread.is_alive():
            logger.warning(
                "Host relay thread did not stop within %.1fs — forzo la "
                "chiusura del socket per sbloccarlo.",
                _STOP_JOIN_TIMEOUT,
            )
            self._force_close_socket(session)
            self._host_thread.join(timeout=2.0)
            if self._host_thread.is_alive():
                logger.warning(
                    "Host relay thread ancora vivo dopo la chiusura forzata "
                    "del socket (daemon: morirà col processo)."
                )
        self._host_thread = None

    # ── client internals ────────────────────────────────────────

    def _start_client_thread(self) -> None:
        """Start the background thread for the client session."""
        session = self._session
        if session is None:
            return
        old_thread = self._thread
        if old_thread and old_thread.is_alive():
            logger.info(
                "Previous client thread still alive — creating new thread for session %s",
                getattr(session, "session_id", "?"),
            )
        self._thread = threading.Thread(target=session.start, daemon=True)
        self._thread.start()

    def _stop_client_session(self) -> None:
        """Stop the client session if any."""
        if self._session is None or self._thread is None:
            return
        session = self._session
        self._session = None
        session.stop()
        self._thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if self._thread.is_alive():
            logger.warning(
                "Client relay thread did not stop within %.1fs — forzo la "
                "chiusura del socket per sbloccarlo.",
                _STOP_JOIN_TIMEOUT,
            )
            self._force_close_socket(session)
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                logger.warning(
                    "Client relay thread ancora vivo dopo la chiusura forzata "
                    "del socket (daemon: morirà col processo)."
                )
        self._thread = None

    @staticmethod
    def _force_close_socket(session: _RelaySession) -> None:
        """Chiude il socket TCP del session thread bloccato.

        Chiusura cross-thread deliberata: sblocca il ``select()`` del loop
        asyncio con un errore e permette al thread di terminare.  Senza
        questa, i vecchi thread relay restano zombie (in campo ne sono
        stati visti accumularsi 3+) e trattengono le connessioni al relay.
        """
        try:
            writer = getattr(session, "_writer", None)
            transport = writer.transport if writer is not None else None
            sock = transport.get_extra_info("socket") if transport else None
            if sock is not None:
                sock.close()
                logger.info("Socket sessione relay chiuso forzatamente")
        except Exception as e:
            logger.debug("force_close_socket fallito: %s", e)

    # ── inbox polling ───────────────────────────────────────────

    @Slot()
    def _poll_inboxes(self) -> None:
        """Process messages from both host and client network threads.

        Runs in the UI thread via QTimer.  Host events are routed to
        host-specific signals; client events to client-specific signals.
        Shared events (message, error, device_list) are emitted on the
        common signals from both inboxes.
        """
        self._poll_client_inbox()
        self._poll_host_inbox()

    def _poll_client_inbox(self) -> None:
        """Process client inbox events.

        Frame events are coalesced: only the most recent frame is kept
        per poll cycle.  Intermediate frames are discarded to avoid
        clogging the UI thread with stale frames that would never be
        displayed anyway.
        """
        t_start = time.time()
        frame_count = 0
        last_frame = None
        while not self._inbox.empty():
            try:
                event, data, seq = self._inbox.get_nowait()
            except queue.Empty:
                break

            # Skip stale events from a previous client session
            if seq != self._current_seq:
                continue

            if event == "frame":
                frame_count += 1
                last_frame = data  # only keep the latest frame
            else:
                self._route_client_event(event, data)

        if last_frame is not None:
            t0 = time.time()
            self._route_client_event("frame", last_frame)
            t_frame = (time.time() - t0) * 1000
            if t_frame > 10:  # only log if frame display takes >10ms
                logger.debug(
                    "Frame display took %.1fms (discarded %d stale frames, poll took %.1fms)",
                    t_frame,
                    frame_count - 1,
                    (time.time() - t_start) * 1000,
                )

    def _poll_host_inbox(self) -> None:
        """Process host inbox events.

        Frame events are coalesced (same logic as client inbox).
        """
        t0 = time.perf_counter()
        events = 0
        last_frame = None
        while not self._host_inbox.empty():
            try:
                event, data, seq = self._host_inbox.get_nowait()
                events += 1
            except queue.Empty:
                break

            # Skip stale events from a previous host session
            if seq != self._host_seq:
                continue

            if event == "frame":
                last_frame = data
            else:
                self._route_host_event(event, data)

        if last_frame is not None:
            self._route_host_event("frame", last_frame)

        dt_ms = (time.perf_counter() - t0) * 1000
        if dt_ms > 100:
            logger.warning(
                "SLOW _poll_host_inbox: %.0fms (%d events) — blocca il "
                "main thread e l'iniezione input remota",
                dt_ms,
                events,
            )

    def _route_client_event(self, event: str, data: Any) -> None:
        """Route a client inbox event to the appropriate signal."""
        if event == "connected":
            role_str, sid = data
            self.client_connected.emit(role_str, sid)
        elif event == "disconnected":
            self._session = None
            self._thread = None
            self.client_disconnected.emit()
        elif event == "auth_requested":
            self.client_auth_requested.emit()
        elif event == "auth_result":
            success, msg = data
            self.client_auth_result.emit(success, msg)
        elif event == "frame":
            rgb, w, h = data
            self.frame_received.emit(rgb, w, h)
        elif event == "tile":
            rgb, x, y, w, h, _fw, _fh = data
            self.tile_received.emit(rgb, x, y, w, h)
        elif event == "keyframe_requested":
            self.client_keyframe_requested.emit()
        elif event == "message":
            self.message_received.emit(data)
        elif event == "error":
            self.error.emit(data)
        elif event == "device_list":
            self.device_list_received.emit(data)
        elif event == "trust_secret":
            device_id, secret = data
            self.trust_secret_received.emit(device_id, secret)
        elif event == "p2p_state":
            self.p2p_state_changed.emit(data)
        else:
            logger.debug("Unhandled client event: %s", event)

    def _route_host_event(self, event: str, data: Any) -> None:
        """Route a host inbox event to the appropriate signal."""
        if event == "connected":
            role_str, sid = data
            self.host_connected.emit(role_str, sid)
        elif event == "disconnected":
            self._host_session = None
            self._host_thread = None
            self.host_disconnected.emit()
        elif event == "peer_joined":
            self.host_peer_joined.emit()
        elif event == "auth_result":
            success, msg = data
            self.host_auth_result.emit(success, msg)
        elif event == "peer_disconnected":
            self.host_peer_disconnected.emit()
        elif event == "keyframe_requested":
            self.host_keyframe_requested.emit()
            self.main_keyframe_requested.emit()
        elif event == "message":
            self.message_received.emit(data)
        elif event == "error":
            self.error.emit(data)
        elif event in ("device_list", "device_update"):
            self.device_list_received.emit(data)
        elif event == "p2p_state":
            self.p2p_state_changed.emit(data)
        else:
            logger.debug("Unhandled host event: %s", event)
