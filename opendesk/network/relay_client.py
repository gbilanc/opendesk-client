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


# ---------------------------------------------------------------------------
# Async relay session
# ---------------------------------------------------------------------------


class _RelaySession:
    """Asyncio session that runs in a background thread.

    Holds the TCP connection and event loop.  Incoming messages are
    pushed into ``inbox`` (a thread-safe queue).  Outgoing messages
    are sent via ``asyncio.run_coroutine_threadsafe()``.
    """

    def __init__(
        self,
        host: str,
        port: int,
        session_id: str,
        password: str,
        role: RelayRole,
        inbox: queue.Queue,
    ) -> None:
        self.host = host
        self.port = port
        self.session_id = session_id
        self.password = password
        self.role = role
        self.inbox = inbox

        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._running = threading.Event()
        self._decoder: VideoDecoder | None = None

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
        # Cancel all pending tasks to avoid
        # "Task was destroyed but it is pending" warnings.
        if self._loop and self._loop.is_running():
            current = asyncio.current_task(self._loop)
            for task in asyncio.all_tasks(self._loop):
                if task is not current:
                    task.cancel()
            # Yield control so cancelled tasks can process their cancellation
            await asyncio.sleep(0)
            self._loop.stop()

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
            self.inbox.put(("error", f"Send failed: {e}"))
            self.inbox.put(("disconnected", None))

    # ── host flow ───────────────────────────────────────────────────

    async def _run_host(self) -> None:
        logger.info("Host connecting to relay %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port
            )
        except OSError as e:
            logger.error("Host connection failed: %s", e)
            self.inbox.put(("error", str(e)))
            self.inbox.put(("disconnected", None))
            return

        # Register session
        await self._send_async(Message.relay_register(session_id=self.session_id))

        async for msg in self._read_loop():
            self.inbox.put(("message", msg))

            t = msg.type
            if t == MessageType.RELAY_REGISTER:
                self.inbox.put(("connected", ("host", self.session_id)))

            elif t == MessageType.RELAY_PEER_LIST:
                self.inbox.put(("peer_joined", None))
                # Request auth from client
                await self._send_async(Message.auth_request(self.session_id))

            elif t == MessageType.AUTH_RESPONSE:
                client_pw = msg.payload.get("password", "")
                success = client_pw == self.password
                if success:
                    await self._send_async(Message.auth_ok())
                    self.inbox.put(("auth_result", (True, "Authenticated")))
                else:
                    await self._send_async(Message.auth_fail("Invalid password"))
                    self.inbox.put(("auth_result", (False, "Invalid password")))

            elif t == MessageType.DISCONNECT:
                break

        self.inbox.put(("disconnected", None))

    # ── client flow ─────────────────────────────────────────────────

    async def _run_client(self) -> None:
        logger.info("Client connecting to relay %s:%s", self.host, self.port)
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.host, self.port
            )
        except OSError as e:
            logger.error("Client connection failed: %s", e)
            self.inbox.put(("error", str(e)))
            self.inbox.put(("disconnected", None))
            return

        # Join session
        await self._send_async(Message.relay_register(session_id=self.session_id))

        async for msg in self._read_loop():
            self.inbox.put(("message", msg))

            t = msg.type
            if t == MessageType.RELAY_REGISTER and msg.payload.get("paired"):
                self.inbox.put(("connected", ("client", self.session_id)))

            elif t == MessageType.AUTH_REQUEST:
                await self._send_async(Message.auth_response(self.password))
                self.inbox.put(("auth_requested", None))

            elif t == MessageType.AUTH_OK:
                self.inbox.put(("auth_result", (True, "Authenticated")))

            elif t == MessageType.AUTH_FAIL:
                reason = msg.payload.get("reason", "Authentication failed")
                self.inbox.put(("auth_result", (False, reason)))
                break

            elif t == MessageType.VIDEO_FRAME:
                payload = msg.payload
                width = payload.get("width", 0)
                height = payload.get("height", 0)
                data = payload.get("data")
                if data and width > 0 and height > 0:
                    if self._decoder is None:
                        self._decoder = VideoDecoder()
                    try:
                        rgb = self._decoder.decode(data, width, height)
                        if rgb is not None:
                            self.inbox.put(("frame", (rgb.copy(), width, height)))
                    except Exception as e:
                        logger.warning("Frame decode error: %s", e)

            elif t == MessageType.ERROR:
                err = msg.payload.get("message", "Unknown relay error")
                self.inbox.put(("error", err))
                break

            elif t == MessageType.DISCONNECT:
                break

        self.inbox.put(("disconnected", None))

    # ── I/O helpers ─────────────────────────────────────────────────

    async def _read_loop(self):  # noqa: ANN201
        """Yield messages from the TCP stream."""
        while self._running.is_set():
            try:
                msg = await Message.from_reader(self._reader)
                yield msg
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                logger.warning("Connection lost: %s", e)
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

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._session: _RelaySession | None = None
        self._thread: threading.Thread | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_inbox)
        self._timer.start(_POLL_INTERVAL_MS)
        self._role: RelayRole | None = None

    # ── public API ──────────────────────────────────────────────────

    def start_hosting(self, host: str, port: int, session_id: str, password: str) -> None:
        """Connect to relay and register as host."""
        self._stop_session()
        self._role = RelayRole.HOST
        self._session = _RelaySession(
            host, port, session_id, password, RelayRole.HOST, self._inbox,
        )
        self._start_thread()

    def join_session(self, host: str, port: int, session_id: str, password: str) -> None:
        """Connect to relay and join a session as client."""
        self._stop_session()
        self._role = RelayRole.CLIENT
        self._session = _RelaySession(
            host, port, session_id, password, RelayRole.CLIENT, self._inbox,
        )
        self._start_thread()

    def send_message(self, msg: Message) -> None:
        """Send a protocol message."""
        if self._session:
            self._session.send_message(msg)

    def send_frame(self, rgb: np.ndarray, width: int, height: int, pts: int, keyframe: bool = False) -> None:
        """Send a video frame (host only)."""
        if self._session:
            self._session.send_frame(rgb, width, height, pts, keyframe=keyframe)

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
        """Start the background thread for the session."""
        session = self._session
        if session is None:
            return
        self._thread = threading.Thread(target=session.start, daemon=True)
        self._thread.start()

    def _stop_session(self) -> None:
        """Stop the current session if any."""
        if self._session:
            self._session.stop()
            self._session = None
            self._thread = None

    @Slot()
    def _poll_inbox(self) -> None:
        """Process messages from the network thread (runs in UI thread)."""
        while not self._inbox.empty():
            try:
                event, data = self._inbox.get_nowait()
            except queue.Empty:
                break

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
