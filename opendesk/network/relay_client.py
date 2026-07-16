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

        # Tile grid compositing (client side)
        self._reference_frame: np.ndarray | None = None
        self._frame_width: int = 0
        self._frame_height: int = 0
        self._last_keyframe_time: float = 0.0  # for watchdog

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

    # Message types that are internal to the relay protocol and must be
    # sent directly (not wrapped in RELAY_ROUTE).
    _RELAY_CONTROL_TYPES = frozenset({
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
    })

    def send_message(self, msg: Message) -> None:
        """Send a message over the relay connection (thread-safe).

        Peer-to-peer messages (video frames, input events, clipboard,
        file transfer, chat, audio) are automatically wrapped in
        ``RELAY_ROUTE`` so the relay server forwards them to the
        paired peer.  Relay-internal control messages are sent as-is.
        """
        if self._loop and self._running.is_set():
            # Wrap peer-to-peer messages in RELAY_ROUTE
            if msg.type not in self._RELAY_CONTROL_TYPES:
                msg = Message.relay_route(
                    inner_type=msg.type.value,
                    inner_payload=msg.payload,
                )
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

                elif t == MessageType.ERROR:
                    err = msg.payload.get("message", "Unknown relay error")
                    # "Peer disconnected" is expected when the remote client leaves.
                    # Treat it as a peer event, not a relay error.
                    if "Peer disconnected" in err:
                        logger.info("Peer disconnected from our session")
                        self.inbox.put(("peer_disconnected", None, self.session_seq))
                    else:
                        logger.warning("Host received ERROR from relay: %s", err)
                        self.inbox.put(("error", err, self.session_seq))

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

        # Periodic task that requests a keyframe if none received
        async def _keyframe_watchdog():
            while self._running.is_set():
                await asyncio.sleep(3.0)
                if self._last_keyframe_time > 0 and \
                   time.time() - self._last_keyframe_time > 5.0:
                    logger.info("No keyframe for 5s — requesting one")
                    self._last_keyframe_time = time.time()
                    await self._send_async(
                        Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                    )

        watchdog_task = asyncio.create_task(_keyframe_watchdog())

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
                            self._decoder = VideoDecoder(codec="h264")  # auto-detects HEVC if needed
                        try:
                            rgb = self._decoder.decode(
                                data, width, height, is_keyframe=is_keyframe,
                            )
                            if rgb is not None:
                                # Update reference frame for tile compositing
                                self._reference_frame = rgb.copy()
                                self._frame_width = width
                                self._frame_height = height
                                self._last_keyframe_time = time.time()
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
                            # Keep the old reference frame — setting it to None
                            # would cause ALL subsequent tiles to be skipped,
                            # and the viewer would show a gray placeholder after
                            # the frame timeout (10s).  The keyframe request
                            # below will cause the host to send a fresh keyframe
                            # which will update the reference frame on success.
                            await self._send_async(
                                Message(MessageType.VIDEO_REQUEST_KEYFRAME, {}),
                            )

                elif t == MessageType.VIDEO_TILE:
                    payload = msg.payload
                    data = payload.get("data")
                    tx = payload.get("x", 0)
                    ty = payload.get("y", 0)
                    tw = payload.get("width", 0)
                    th = payload.get("height", 0)
                    if data and tw > 0 and th > 0 and self._reference_frame is not None:
                        try:
                            import cv2
                            import numpy as np
                            arr = np.frombuffer(data, dtype=np.uint8)
                            tile_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                            if tile_bgr is not None and tile_bgr.shape[0] == th and tile_bgr.shape[1] == tw:
                                tile_rgb = cv2.cvtColor(tile_bgr, cv2.COLOR_BGR2RGB)
                                # Composite onto reference frame
                                ref_h, ref_w = self._reference_frame.shape[:2]
                                if ty + th <= ref_h and tx + tw <= ref_w:
                                    self._reference_frame[ty:ty+th, tx:tx+tw] = tile_rgb
                                    self.inbox.put(
                                        ("frame", (
                                            self._reference_frame.copy(),
                                            self._frame_width,
                                            self._frame_height,
                                        ), self.session_seq),
                                    )
                            else:
                                logger.warning(
                                    "Tile decode mismatch: expected %dx%d, got %s",
                                    tw, th,
                                    tile_bgr.shape[:2] if tile_bgr is not None else "None",
                                )
                        except Exception as e:
                            logger.warning("Tile decode/composite error: %s", e)
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
            watchdog_task.cancel()
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
    client_keyframe_requested = Signal()  # remote host needs a keyframe

    # ── Shared signals (from both host and client) ──
    message_received = Signal(object)  # Message
    error = Signal(str)
    device_list_received = Signal(list)  # list[dict] — devices from relay
    main_keyframe_requested = Signal()  # legacy alias for host_keyframe_requested

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

        # ── Host session (persistent background) ──
        self._host_session: _RelaySession | None = None
        self._host_thread: threading.Thread | None = None
        self._host_inbox: queue.Queue = queue.Queue()
        self._host_seq: int = 0
        self._host_role: RelayRole | None = None

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

    def start_hosting(self, host: str, port: int, session_id: str, password: str,
                      device_id: str = "", device_name: str = "",
                      trusted_device_ids: set[str] | None = None) -> None:
        """Connect to relay and register as host.

        Creates an independent persistent host session.  Does NOT
        affect any active client session.
        """
        self._stop_host_session()
        self._drain_host_inbox()
        self._host_seq += 1
        self._host_role = RelayRole.HOST
        self._host_session = _RelaySession(
            host, port, session_id, password, RelayRole.HOST, self._host_inbox,
            device_id=device_id, device_name=device_name,
            session_seq=self._host_seq,
            trusted_device_ids=trusted_device_ids,
        )
        self._start_host_thread()

    def stop_hosting(self) -> None:
        """Stop the persistent host session if active."""
        self._stop_host_session()

    def is_hosting(self) -> bool:
        """Check if the host session is active and connected."""
        return self._host_thread is not None and self._host_thread.is_alive()

    # ── client connection ──

    def join_session(self, host: str, port: int, session_id: str, password: str,
                     device_id: str = "") -> None:
        """Connect to relay and join a session as client.

        Does NOT stop the host session — the host session continues
        running in the background, accepting incoming connections.
        """
        # Only stop a previous CLIENT session, NOT the host session
        self._stop_client_session()
        self._drain_inbox()
        self._current_seq += 1
        self._role = RelayRole.CLIENT
        self._session = _RelaySession(
            host, port, session_id, password, RelayRole.CLIENT, self._inbox,
            device_id=device_id,
            session_seq=self._current_seq,
        )
        self._start_client_thread()

    # ── message sending ──

    def send_message(self, msg: Message) -> None:
        """Send a protocol message.

        If the client session is active, sends through it;
        otherwise falls back to the host session.
        """
        if self._session and self._thread and self._thread.is_alive():
            self._session.send_message(msg)
        elif self._host_session and self._host_thread and self._host_thread.is_alive():
            self._host_session.send_message(msg)

    def send_frame(self, data: bytes, width: int, height: int, pts: int, keyframe: bool = False) -> None:
        """Send an encoded H.264 video frame over the relay (host only).

        Always sent through the host session.
        """
        if self._host_session:
            self._host_session.send_frame(data, width, height, pts, keyframe=keyframe)

    def send_tile(self, data: bytes, x: int, y: int, width: int, height: int, pts: int) -> None:
        """Send a JPEG-encoded tile update over the relay (host only).

        Always sent through the host session.
        """
        if self._host_session:
            msg = Message.video_tile(data, x, y, width, height, pts)
            self._host_session.send_message(msg)

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
        return (self._thread is not None and self._thread.is_alive()) or \
               (self._host_thread is not None and self._host_thread.is_alive())

    @property
    def role(self) -> RelayRole | None:
        """Return the role of the active client session, or None."""
        return self._role

    @property
    def host_role(self) -> RelayRole | None:
        """Return HOST if the host session is active, else None."""
        return self._host_role if self.is_hosting() else None

    # ── host internals ──────────────────────────────────────────

    def _start_host_thread(self) -> None:
        """Start the background thread for the host session."""
        session = self._host_session
        if session is None:
            return
        old_thread = self._host_thread
        if old_thread and old_thread.is_alive():
            logger.info(
                "Previous host thread still alive — "
                "creating new thread for session %s",
                getattr(session, 'session_id', '?'),
            )
        self._host_thread = threading.Thread(target=session.start, daemon=True)
        self._host_thread.start()

    def _stop_host_session(self) -> None:
        """Stop the host session if any."""
        if self._host_session is None or self._host_thread is None:
            return
        self._host_session.stop()
        self._host_session = None
        self._host_thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if self._host_thread.is_alive():
            logger.warning(
                "Host relay thread did not stop within %.1fs — "
                "the old one will terminate when its TCP socket timeout fires.",
                _STOP_JOIN_TIMEOUT,
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
                "Previous client thread still alive — "
                "creating new thread for session %s",
                getattr(session, 'session_id', '?'),
            )
        self._thread = threading.Thread(target=session.start, daemon=True)
        self._thread.start()

    def _stop_client_session(self) -> None:
        """Stop the client session if any."""
        if self._session is None or self._thread is None:
            return
        self._session.stop()
        self._session = None
        self._thread.join(timeout=_STOP_JOIN_TIMEOUT)
        if self._thread.is_alive():
            logger.warning(
                "Client relay thread did not stop within %.1fs — "
                "the old one will terminate when its TCP socket timeout fires.",
                _STOP_JOIN_TIMEOUT,
            )
        self._thread = None

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
        """Process client inbox events."""
        while not self._inbox.empty():
            try:
                event, data, seq = self._inbox.get_nowait()
            except queue.Empty:
                break

            # Skip stale events from a previous client session
            if seq != self._current_seq:
                logger.debug(
                    "Skipping stale client event '%s' (seq %d, current %d)",
                    event, seq, self._current_seq,
                )
                continue

            self._route_client_event(event, data)

    def _poll_host_inbox(self) -> None:
        """Process host inbox events."""
        while not self._host_inbox.empty():
            try:
                event, data, seq = self._host_inbox.get_nowait()
            except queue.Empty:
                break

            # Skip stale events from a previous host session
            if seq != self._host_seq:
                logger.debug(
                    "Skipping stale host event '%s' (seq %d, current %d)",
                    event, seq, self._host_seq,
                )
                continue

            self._route_host_event(event, data)

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
        elif event == "keyframe_requested":
            self.client_keyframe_requested.emit()
        elif event == "message":
            self.message_received.emit(data)
        elif event == "error":
            self.error.emit(data)
        elif event == "device_list":
            self.device_list_received.emit(data)
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
        elif event == "device_list":
            self.device_list_received.emit(data)
        elif event == "device_update":
            self.device_list_received.emit(data)
        else:
            logger.debug("Unhandled host event: %s", event)
