"""
Message protocol for OpenDesk.

Defines message types, serialization (MessagePack), and framing
for the OpenDesk remote desktop protocol.

Message format
--------------
All messages follow this structure::

    [ 4 bytes length (big-endian) ][ 1 byte type ][ payload (MessagePack) ]
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

import msgpack

from opendesk.crypto.e2ee import EncryptedMessage

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PROTOCOL_VERSION = 1
_MAX_MESSAGE_SIZE = 100 * 1024 * 1024  # 100 MB
_HEADER_FORMAT = "!I"  # 4 bytes unsigned int (network byte order)
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)

# ---------------------------------------------------------------------------
# Message types
# ---------------------------------------------------------------------------


class MessageType(IntEnum):
    """All supported message types."""

    # ── Signalling / handshake ──
    HELLO = 0x01  # Initial connection handshake
    HELLO_ACK = 0x02  # Handshake acceptance
    KEY_EXCHANGE = 0x03  # E2E public key exchange
    KEY_EXCHANGE_ACK = 0x04
    AUTH_REQUEST = 0x05  # Password auth challenge
    AUTH_RESPONSE = 0x06  # Password auth response
    AUTH_OK = 0x07  # Auth success
    AUTH_FAIL = 0x08  # Auth failure
    SESSION_INFO = 0x09  # Session metadata (resolution, capabilities)

    # ── Video ──
    VIDEO_FRAME = 0x10  # H.264 encoded frame
    VIDEO_REQUEST_KEYFRAME = 0x11  # Receiver requests keyframe

    # ── Input ──
    MOUSE_EVENT = 0x20  # Mouse movement / click
    KEYBOARD_EVENT = 0x21  # Key press / release
    TEXT_INPUT = 0x22  # Typed text

    # ── Clipboard ──
    CLIPBOARD_TEXT = 0x30  # Clipboard text content
    CLIPBOARD_IMAGE = 0x31  # Clipboard image (PNG bytes)
    CLIPBOARD_SYNC = 0x32  # Toggle clipboard sync on/off

    # ── File transfer ──
    FILE_REQUEST = 0x40  # Request to send a file
    FILE_ACCEPT = 0x41  # Receiver accepts
    FILE_REJECT = 0x42  # Receiver rejects
    FILE_CHUNK = 0x43  # File data chunk
    FILE_COMPLETE = 0x44  # File transfer complete
    FILE_ERROR = 0x45  # File transfer error
    FILE_PROGRESS = 0x46  # Transfer progress update

    # ── Audio ──
    AUDIO_FRAME = 0x50  # Opus-encoded audio packet

    # ── Chat ──
    CHAT_MESSAGE = 0x60  # Text chat message
    CHAT_TYPING = 0x61  # Typing indicator

    # ── Control ──
    PING = 0x70  # Keep-alive / latency measurement
    PONG = 0x71  # Ping response
    DISCONNECT = 0x72  # Graceful disconnection
    ERROR = 0x73  # Protocol error

    # ── Relay ──
    RELAY_REGISTER = 0x80  # Register with relay server
    RELAY_ROUTE = 0x81  # Route message through relay
    RELAY_PEER_LIST = 0x82  # List of connected peers


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single protocol message with typed payload."""

    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    encrypted: bool = False

    # ── serialisation ───────────────────────────────────────────────

    def encode(self) -> bytes:
        """Encode message to bytes for transport."""
        body = msgpack.packb(
            {
                "t": self.type.value,
                "p": self.payload,
                "e": self.encrypted,
            }
        )
        header = struct.pack(_HEADER_FORMAT, len(body))
        return header + body

    @classmethod
    def decode(cls, data: bytes) -> Message:
        """Decode message from raw bytes (wire format with header).

        Parameters
        ----------
        data : bytes
            Full wire-format message (header + body) as produced by
            :meth:`encode`.

        Returns
        -------
        Message
        """
        body_len = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])[0]
        body = data[_HEADER_SIZE : _HEADER_SIZE + body_len]
        obj = msgpack.unpackb(body)
        return cls(
            type=MessageType(obj["t"]),
            payload=obj.get("p", {}),
            encrypted=obj.get("e", False),
        )

    @classmethod
    async def from_reader(cls, reader: Any) -> Message:  # noqa: ANN401
        """Read and decode a message from an asyncio StreamReader."""
        header_data = b""
        while len(header_data) < _HEADER_SIZE:
            chunk = await reader.read(_HEADER_SIZE - len(header_data))
            if not chunk:
                raise ConnectionError("Connection closed while reading header")
            header_data += chunk

        body_len = struct.unpack(_HEADER_FORMAT, header_data)[0]
        if body_len > _MAX_MESSAGE_SIZE:
            raise ValueError(f"Message too large: {body_len} bytes")

        body_data = b""
        while len(body_data) < body_len:
            chunk = await reader.read(body_len - len(body_data))
            if not chunk:
                raise ConnectionError("Connection closed while reading body")
            body_data += chunk

        # Reconstruct full wire-format for decode()
        wire_data = header_data + body_data
        return cls.decode(wire_data)

    @staticmethod
    def write(writer: Any, msg: Message) -> None:  # noqa: ANN401
        """Write a message to an asyncio StreamWriter."""
        data = msg.encode()
        writer.write(data)

    # ── factory helpers ─────────────────────────────────────────────

    @classmethod
    def hello(cls, version: int = _PROTOCOL_VERSION) -> Message:
        return cls(MessageType.HELLO, {"version": version})

    @classmethod
    def hello_ack(cls, version: int = _PROTOCOL_VERSION) -> Message:
        return cls(MessageType.HELLO_ACK, {"version": version})

    @classmethod
    def key_exchange(cls, public_key_b64: str) -> Message:
        return cls(MessageType.KEY_EXCHANGE, {"public_key": public_key_b64})

    @classmethod
    def auth_request(cls, session_id: str) -> Message:
        return cls(MessageType.AUTH_REQUEST, {"session_id": session_id})

    @classmethod
    def auth_response(cls, password: str) -> Message:
        return cls(MessageType.AUTH_RESPONSE, {"password": password})

    @classmethod
    def auth_ok(cls) -> Message:
        return cls(MessageType.AUTH_OK, {})

    @classmethod
    def auth_fail(cls, reason: str = "Invalid credentials") -> Message:
        return cls(MessageType.AUTH_FAIL, {"reason": reason})

    @classmethod
    def video_frame(
        cls, data: bytes, width: int, height: int, pts: int, keyframe: bool = False,
    ) -> Message:
        return cls(
            MessageType.VIDEO_FRAME,
            {
                "data": data,
                "width": width,
                "height": height,
                "pts": pts,
                "keyframe": keyframe,
            },
        )

    @classmethod
    def mouse_event(
        cls, x: int, y: int, button: int | None = None,
        pressed: bool | None = None, absolute: bool = True,
    ) -> Message:
        return cls(
            MessageType.MOUSE_EVENT,
            {
                "x": x, "y": y, "button": button,
                "pressed": pressed, "absolute": absolute,
            },
        )

    @classmethod
    def keyboard_event(cls, key: str, pressed: bool) -> Message:
        return cls(
            MessageType.KEYBOARD_EVENT,
            {"key": key, "pressed": pressed},
        )

    @classmethod
    def clipboard_text(cls, text: str) -> Message:
        return cls(MessageType.CLIPBOARD_TEXT, {"text": text})

    @classmethod
    def chat_message(cls, text: str) -> Message:
        return cls(MessageType.CHAT_MESSAGE, {"text": text})

    @classmethod
    def ping(cls, seq: int = 0) -> Message:
        return cls(MessageType.PING, {"seq": seq})

    @classmethod
    def pong(cls, seq: int = 0) -> Message:
        return cls(MessageType.PONG, {"seq": seq})

    @classmethod
    def disconnect(cls, reason: str = "") -> Message:
        return cls(MessageType.DISCONNECT, {"reason": reason})

    @classmethod
    def error(cls, code: int, message: str) -> Message:
        return cls(MessageType.ERROR, {"code": code, "message": message})

    @classmethod
    def relay_register(
        cls, session_id: str = "",
    ) -> Message:
        """Register with a relay server or create a new session."""
        return cls(
            MessageType.RELAY_REGISTER,
            {"session_id": session_id},
        )

    @classmethod
    def relay_route(cls, inner_type: int, inner_payload: dict) -> Message:
        """Route a message through the relay to the paired peer."""
        return cls(
            MessageType.RELAY_ROUTE,
            {"inner_type": inner_type, "inner_payload": inner_payload},
        )
