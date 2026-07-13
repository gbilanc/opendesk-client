"""
Relay-based P2P connection client.

Provides ``RelayClient``, a QObject that runs an asyncio TCP connection
to the relay server in a background thread.  Supports two roles:

- **Host** — registers a session_id, waits for a client, authenticates,
  streams video, receives input events.
- **Client** — joins an existing session by session_id, authenticates,
  receives video frames, sends input events.

Threading model:
  - ``RelayClient`` lives in the main (UI) thread, emits Qt signals.
  - An asyncio event loop runs in a daemon thread for TCP I/O.
  - A ``QTimer`` polls the inbox queue to deliver messages to the UI thread.
  - ``asyncio.run_coroutine_threadsafe()`` sends commands from UI → network thread.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Any

import numpy as np

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from opendesk.network.protocol import Message, MessageType
from opendesk.core.video_codec import VideoDecoder
from opendesk.crypto.challenge import generate_nonce, compute_response, verify_response

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

_POLL_INTERVAL_MS = 50  # poll inbox every 50 ms
_CONNECT_TIMEOUT = 15.0  # max seconds to wait for TCP connection
_STOP_JOIN_TIMEOUT = 8.0  # max seconds to wait for session thread to stop


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
        trusted_device_ids: set[str] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.password = password
        self.role = role
        self.inbox = inbox
        self.device_id = device_id
        self.device_name = device_name
        self.session_seq = session_seq
        self._trusted_device_ids: set[str] = trusted_device_ids or set()

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = threading.Event()
        self._decoder: VideoDecoder | None = None
        self._auth_nonce: str = ""  # challenge nonce for challenge-response auth

    # ── lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        """Run the asyncio event loop in the current thread (blocking)."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
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
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        if self._decoder:
            self._decoder.release()
            self._decoder = None
        # Small yield so shutdown tasks can propagate
        await asyncio.sleep(0)

    # ── sending ─────────────────────────────────────────────────────

    def send_message(self, msg: Message) -> None:
        """Send a message over the relay connection (thread-safe)."""
        if self._loop and self._running.is_set():
            asyncio.run_coroutine_threadsafe(self._send_async(msg), self._loop)

    async def _send_async(self, msg: Message) -> None:
        if self._writer is None:
            return
        try:
            data = msg.encode()
            self._writer.write(data)
            await self._writer.drain()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("Send error: %s", e)
            self.inbox.put(("error", f"Send failed: {e}", self.session_seq))
            self.inbox.put(("disconnected", None, self.session_seq))

    # ── host flow ───────────────────────────────────────────────────

    async def _run_host(self) -> None:
        logger.info("Host connecting to relay %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=_CONNECT_TIMEOUT,
            )
        except asyncio.TimeoutError:
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
        await self._send_async(Message.relay_register(
            session_id=self.session_id,
            device_id=self.device_id,
            device_name=self.device_name,
        ))

        gen = self._read_loop()
        try:
            async for msg in gen:
                self.inbox.put(("message", msg, self.session_seq))

                t = msg.type
                if t == MessageType.RELAY_REGISTER:
                    self.inbox.put(("connected", ("host", self.session_id), self.session_seq))

                elif t == MessageType.RELAY_PEER_LIST:
                    self.inbox.put(("peer_joined", None, self.session_seq))
                    # Challenge-response auth: generate nonce, send to client
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

                elif t == MessageType.AUTH_RESPONSE:
                    client_hash = msg.payload.get("nonce_hash", "")
                    client_device_id = msg.payload.get("device_id", "")

                    # Trusted device bypass: skip password verification
                    if client_device_id and client_device_id in self._trusted_device_ids:
                        logger.info(
                            "Trusted device '%s' authenticated without password",
                            client_device_id[:8],
                        )
                        await self._send_async(Message.auth_ok())
                        self.inbox.put((
                            "auth_result",
                            (True, "Authenticated (trusted device)"),
                            self.session_seq,
                        ))
                        continue

                    success = verify_response(self._auth_nonce, self.password, client_hash)
                    if success:
                        await self._send_async(Message.auth_ok())
                        self.inbox.put(("auth_result", (True, "Authenticated"), self.session_seq))
                        logger.debug("Host auth OK sent — continuing loop")
                    else:
                        await self._send_async(Message.auth_fail("Invalid credentials"))
                        self.inbox.put(("auth_result", (False, "Invalid credentials"), self.session_seq))

                elif t == MessageType.VIDEO_REQUEST_KEYFRAME:
                    logger.debug("Peer requested keyframe (host)")
                    self.inbox.put(("keyframe_requested", None, self.session_seq))

                elif t == MessageType.DISCONNECT:
                    logger.debug("Host received DISCONNECT — breaking loop")
                    break

                else:
                    logger.debug("Host received unhandled message type %s", t)
        finally:
            await gen.aclose()
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
        except asyncio.TimeoutError:
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
        await self._send_async(Message(
            MessageType.RELAY_REGISTER,
            {"lookup_device": self.session_id},
        ))

        gen = self._read_loop()
        try:
            async for msg in gen:
                self.inbox.put(("message", msg, self.session_seq))

                t = msg.type
                if t == MessageType.RELAY_REGISTER:
                    if msg.payload.get("paired"):
                        self.inbox.put(("connected", ("client", self.session_id), self.session_seq))
                    elif msg.payload.get("mode") == "host":
                        # Session doesn't exist — we were registered as host instead
                        logger.warning("Session %s not found on relay", self.session_id)
                        self.inbox.put(("error", f"Session {self.session_id} not found", self.session_seq))
                        self.inbox.put(("disconnected", None, self.session_seq))
                        break

                elif t == MessageType.AUTH_REQUEST:
                    nonce = msg.payload.get("nonce", "")
                    nonce_hash = compute_response(nonce, self.password) if nonce else ""
                    await self._send_async(Message.auth_response(
                        nonce_hash, device_id=self.device_id,
                    ))
                    self.inbox.put(("auth_requested", None, self.session_seq))

                elif t == MessageType.AUTH_OK:
                    self.inbox.put(("auth_result", (True, "Authenticated"), self.session_seq))

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
                        "VIDEO_FRAME received: %dx%d, keyframe=%s, data_len=%d",
                        width, height, is_keyframe, len(data) if data else 0,
                    )
                    if data and width > 0 and height > 0:
                        if self._decoder is None:
                            self._decoder = VideoDecoder()
                        try:
                            rgb = self._decoder.decode(
                                data, width, height, is_keyframe=is_keyframe,
                            )
                            if rgb is not None:
                                self.inbox.put(
                                    ("frame", (rgb.copy(), width, height), self.session_seq),
                                )
                                logger.debug(
                                    "Frame decoded successfully: %dx%d", width, height,
                                )
                            else:
                                logger.warning(
                                    "Frame decode returned None%s",
                                    " — decoder not ready yet" if not is_keyframe else "",
                                )
                                # If we're getting non-keyframes without a prior
                                # keyframe, ask the host to send one.
                                if not is_keyframe and self._decoder is not None:
                                    self._decoder.reset()
                                    await self._send_async(
                                        Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                                    )
                        except Exception as e:
                            logger.exception("Frame decode error: %s", e)
                            self._decoder.reset()
                            await self._send_async(
                                Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                            )
                elif t == MessageType.VIDEO_REQUEST_KEYFRAME:
                    logger.debug("Keyframe requested by peer")
                    # Forward to host via inbox so the StreamService can
                    # force a keyframe on the encoder.
                    self.inbox.put(("keyframe_requested", None, self.session_seq))

                elif t == MessageType.ERROR:
                    err = msg.payload.get("message", "Unknown relay error")
                    logger.debug("Client received ERROR: %s — breaking loop", err)
                    self.inbox.put(("error", err, self.session_seq))
                    break

                elif t == MessageType.DISCONNECT:
                    logger.debug("Client received DISCONNECT — breaking loop")
                    break

                else:
                    logger.debug("Client received unhandled message type %s", t)
        finally:
            await gen.aclose()
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

    def send_frame(self, data: bytes, width: int, height: int, pts: int, keyframe: bool = False) -> None:
        """Send an encoded (H.264) VIDEO_FRAME over the relay."""
        msg = Message.video_frame(data=data, width=width, height=height, pts=pts, keyframe=keyframe)
        self.send_message(msg)


# ---------------------------------------------------------------------------
# RelayClient — QObject for UI integration
# ---------------------------------------------------------------------------


class RelayClient(QObject):
    """High-level relay client for Qt applications.

    Usage::

        client = RelayClient()
        client.connected.connect(lambda role, sid: print(f"Connected as {role}: {sid}"))
        client.frame_received.connect(viewer.display_frame)

        client.start_hosting("relay.example.com", 8474, "123456789", "mypass")
        client.join_session("relay.example.com", 8474, "123456789", "mypass")
    """

    connected = Signal(str, str)  # role ("host" | "client"), session_id
    disconnected = Signal()
    peer_joined = Signal()
    auth_requested = Signal()
    auth_result = Signal(bool, str)  # success, message
    frame_received = Signal(np.ndarray, int, int)  # rgb_data, width, height
    message_received = Signal(object)  # Message
    error = Signal(str)
    device_list_received = Signal(list)  # list[dict] — devices from relay
    keyframe_requested = Signal()  # remote peer needs a keyframe

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session: _RelaySession | None = None
        self._thread: threading.Thread | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_inbox)
        self._timer.start(_POLL_INTERVAL_MS)
        self._role: RelayRole | None = None
        self._current_seq: int = 0

    # ── public API ──────────────────────────────────────────────────

    def _drain_inbox(self) -> None:
        """Discard any stale events left from a previous session."""
        while not self._inbox.empty():
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                break

    def start_hosting(self, host: str, port: int, session_id: str, password: str,
                      device_id: str = "", device_name: str = "",
                      trusted_device_ids: set[str] | None = None) -> None:
        """Connect to relay and register as host."""
        self._stop_session()
        self._drain_inbox()
        self._current_seq += 1
        self._role = RelayRole.HOST
        self._session = _RelaySession(
            host, port, session_id, password, RelayRole.HOST, self._inbox,
            device_id=device_id, device_name=device_name,
            session_seq=self._current_seq,
            trusted_device_ids=trusted_device_ids,
        )
        self._start_thread()

    def join_session(self, host: str, port: int, session_id: str, password: str,
                     device_id: str = "") -> None:
        """Connect to relay and join a session as client."""
        self._stop_session()
        self._drain_inbox()
        self._current_seq += 1
        self._role = RelayRole.CLIENT
        self._session = _RelaySession(
            host, port, session_id, password, RelayRole.CLIENT, self._inbox,
            device_id=device_id,
            session_seq=self._current_seq,
        )
        self._start_thread()

    def send_message(self, msg: Message) -> None:
        """Send a protocol message."""
        if self._session:
            self._session.send_message(msg)

    def send_frame(self, data: bytes, width: int, height: int, pts: int, keyframe: bool = False) -> None:
        """Send an encoded H.264 video frame over the relay (host only)."""
        if self._session:
            self._session.send_frame(data, width, height, pts, keyframe=keyframe)

    def send_mouse_event(
        self, x: int, y: int, button: int | None = None,
        pressed: bool | None = None, absolute: bool = True,
    ) -> None:
        """Send a mouse event to the remote peer."""
        self.send_message(Message.mouse_event(x, y, button, pressed, absolute))

    def send_key_event(self, key: str, pressed: bool) -> None:
        """Send a keyboard event to the remote peer."""
        self.send_message(Message.keyboard_event(key, pressed))

    def disconnect(self) -> None:
        """Disconnect from the relay."""
        self._stop_session()

    @property
    def is_connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def role(self) -> RelayRole | None:
        return self._role

    # ── internal ────────────────────────────────────────────────────

    def _start_thread(self) -> None:
        """Start the background thread for the session.

        If the previous thread is still alive (e.g. a stuck TCP
        connection), we leave it running as a daemon — it will
        terminate when the process exits or when the socket timeout
        fires.  We create a fresh thread for the new session.
        """
        session = self._session
        if session is None:
            return
        old_thread = self._thread
        if old_thread and old_thread.is_alive():
            logger.info(
                "Previous relay thread still alive — "
                "creating new thread for session %s",
                getattr(session, 'session_id', '?'),
            )
        self._thread = threading.Thread(target=session.start, daemon=True)
        self._thread.start()

    def _stop_session(self) -> None:
        """Stop the current session if any.

        Signals the event loop to stop and waits for the background
        thread to finish (with a timeout) so that no stale coroutines
        are left running when a new session starts.

        If the thread refuses to stop, we cannot safely terminate it
        (daemon threads cannot be killed).  Instead we mark it as
        stale — the next ``_start_thread()`` check will skip stale
        threads and let them die on their own.
        """
        if self._session is None or self._thread is None:
            return
        self._session.stop()
        self._session = None
        # Wait for the thread to finish
        self._thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if self._thread.is_alive():
            logger.warning(
                "Relay thread did not stop within %.1fs — "
                "connection may be stuck.  A new session will create "
                "a fresh thread; the old one will terminate when its "
                "TCP socket timeout fires.",
                _STOP_JOIN_TIMEOUT,
            )
        self._thread = None

    @Slot()
    def _poll_inbox(self) -> None:
        """Process messages from the network thread (runs in UI thread).

        Ignores events from stale sessions (e.g. a ``disconnected``
        event from a previous session that was still flushing its
        inbox after a new session was started).
        """
        while not self._inbox.empty():
            try:
                event, data, seq = self._inbox.get_nowait()
            except queue.Empty:
                break

            # Skip stale events from a previous session
            if seq != self._current_seq:
                logger.debug(
                    "Skipping stale event '%s' (seq %d, current %d)",
                    event, seq, self._current_seq,
                )
                continue

            if event == "connected":
                role_str, sid = data
                self.connected.emit(role_str, sid)
            elif event == "disconnected":
                self._session = None
                self._thread = None
                self.disconnected.emit()
            elif event == "peer_joined":
                self.peer_joined.emit()
            elif event == "auth_requested":
                self.auth_requested.emit()
            elif event == "auth_result":
                success, msg = data
                self.auth_result.emit(success, msg)
            elif event == "frame":
                rgb, w, h = data
                self.frame_received.emit(rgb, w, h)
            elif event == "message":
                self.message_received.emit(data)
            elif event == "error":
                self.error.emit(data)
            elif event == "keyframe_requested":
                self.keyframe_requested.emit()
            elif event == "device_list":
                self.device_list_received.emit(data)
