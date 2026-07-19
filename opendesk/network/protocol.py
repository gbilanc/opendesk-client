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
    VIDEO_TILE = 0x12  # Tile-based incremental frame update

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
    FILE_LIST_REQUEST = 0x47  # Request remote directory listing
    FILE_LIST_RESPONSE = 0x48  # Remote directory listing response
    FILE_DOWNLOAD_REQUEST = 0x49  # Request to download a remote file
    FILE_DOWNLOAD_ACCEPT = 0x4A  # Accept download request
    FILE_DOWNLOAD_REJECT = 0x4B  # Reject download request

    # ── Audio ──
    AUDIO_FRAME = 0x50  # Opus-encoded audio packet

    # ── Camera (webcam) ──
    CAMERA_FRAME = 0x53  # JPEG-encoded webcam frame
    CAMERA_START = 0x54  # Webcam stream start/stop notification

    # ── Chat ──
    CHAT_MESSAGE = 0x60  # Text chat message
    CHAT_TYPING = 0x61  # Typing indicator
    CHAT_OPEN = 0x62  # Chat window open/close notification

    # ── Control ──
    PING = 0x70  # Keep-alive / latency measurement
    PONG = 0x71  # Ping response
    DISCONNECT = 0x72  # Graceful disconnection
    ERROR = 0x73  # Protocol error

    # ── Relay ──
    RELAY_REGISTER = 0x80  # Register with relay server
    RELAY_ROUTE = 0x81  # Route message through relay
    RELAY_PEER_LIST = 0x82  # List of connected peers
    RELAY_DEVICE_LIST = 0x83  # List of connected devices (id, name, session_id)
    RELAY_DEVICE_UPDATE = 0x84  # Device went online/offline


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

        body_buf = bytearray(body_len)
        bytes_read = 0
        while bytes_read < body_len:
            chunk = await reader.read(body_len - bytes_read)
            if not chunk:
                raise ConnectionError("Connection closed while reading body")
            body_buf[bytes_read : bytes_read + len(chunk)] = chunk
            bytes_read += len(chunk)

        # Reconstruct full wire-format for decode()
        wire_data = header_data + bytes(body_buf)
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
    def auth_request(cls, session_id: str, nonce: str = "") -> Message:
        """Request authentication from the remote peer.

        Parameters
        ----------
        session_id : str
            The session identifier.
        nonce : str
            Random challenge nonce (hex-encoded).  Used for
            challenge-response auth to avoid sending the password
            in plaintext over the wire.
        """
        return cls(MessageType.AUTH_REQUEST, {
            "session_id": session_id,
            "nonce": nonce,
        })

    @classmethod
    def auth_response(cls, nonce_hash: str, device_id: str = "",
                      connection_mode: str = "remote_desktop") -> Message:
        """Respond to an authentication challenge.

        Parameters
        ----------
        nonce_hash : str
            HMAC-SHA256(nonce, password) hex digest, proving
            knowledge of the shared secret without transmitting
            the password itself.
        device_id : str
            The client's device ID, used by the host to check
            for pre-authorized (trusted) devices.
        connection_mode : str
            "remote_desktop" (default) for full remote desktop,
            "file_transfer" for file-transfer-only mode (no streaming).
        """
        return cls(MessageType.AUTH_RESPONSE, {
            "nonce_hash": nonce_hash,
            "device_id": device_id,
            "connection_mode": connection_mode,
        })

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

    # ── file transfer factories ──

    @classmethod
    def file_request(cls, name: str, size: int, sha256: str = "",
                     job_id: str = "", dest_path: str = "") -> Message:
        """Request permission to send a file.

        Parameters
        ----------
        job_id : str
            The sender's local job ID, so the receiver can echo it back
            in FILE_ACCEPT and both sides agree on the same identifier.
        dest_path : str
            Remote directory path where the file should be saved.
        """
        return cls(MessageType.FILE_REQUEST, {
            "name": name, "size": size, "sha256": sha256,
            "job_id": job_id, "dest_path": dest_path,
        })

    @classmethod
    def file_accept(cls, job_id: str) -> Message:
        """Accept an incoming file transfer."""
        return cls(MessageType.FILE_ACCEPT, {"job_id": job_id})

    @classmethod
    def file_reject(cls, job_id: str, reason: str = "") -> Message:
        """Reject an incoming file transfer."""
        return cls(MessageType.FILE_REJECT, {"job_id": job_id, "reason": reason})

    @classmethod
    def file_chunk(
        cls, job_id: str, seq: int, data: bytes, is_last: bool = False,
    ) -> Message:
        """A chunk of file data."""
        return cls(MessageType.FILE_CHUNK, {
            "job_id": job_id, "seq": seq, "data": data, "is_last": is_last,
        })

    @classmethod
    def file_complete(cls, job_id: str) -> Message:
        """Signal that a file transfer completed successfully."""
        return cls(MessageType.FILE_COMPLETE, {"job_id": job_id})

    @classmethod
    def file_error(cls, job_id: str, error: str) -> Message:
        """Signal that a file transfer failed."""
        return cls(MessageType.FILE_ERROR, {"job_id": job_id, "error": error})

    @classmethod
    def file_list_request(cls, path: str = "/") -> Message:
        """Request a directory listing from the remote peer."""
        return cls(MessageType.FILE_LIST_REQUEST, {"path": path})

    @classmethod
    def file_list_response(
        cls, path: str, entries: list[dict], error: str = "",
    ) -> Message:
        """Respond with a directory listing.

        Parameters
        ----------
        path : str
            The directory path that was listed.
        entries : list[dict]
            List of entries, each with keys:
            - name (str)
            - is_dir (bool)
            - size (int, for files)
            - mtime (float, modification time)
        error : str
            Error message if listing failed.
        """
        return cls(MessageType.FILE_LIST_RESPONSE, {
            "path": path,
            "entries": entries,
            "error": error,
        })

    @classmethod
    def file_download_request(cls, remote_path: str) -> Message:
        """Request to download a file from the remote peer."""
        return cls(MessageType.FILE_DOWNLOAD_REQUEST, {
            "remote_path": remote_path,
        })

    @classmethod
    def file_download_accept(cls, job_id: str) -> Message:
        """Accept an incoming download request."""
        return cls(MessageType.FILE_DOWNLOAD_ACCEPT, {"job_id": job_id})

    @classmethod
    def file_download_reject(cls, job_id: str, reason: str = "") -> Message:
        """Reject an incoming download request."""
        return cls(MessageType.FILE_DOWNLOAD_REJECT, {
            "job_id": job_id, "reason": reason,
        })

    @classmethod
    def chat_message(cls, text: str) -> Message:
        return cls(MessageType.CHAT_MESSAGE, {"text": text})

    @classmethod
    def chat_open(cls, open: bool) -> Message:
        """Notify peer that the chat window was opened or closed."""
        return cls(MessageType.CHAT_OPEN, {"open": open})

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
        device_id: str = "",
        device_name: str = "",
    ) -> Message:
        """Register with a relay server or create a new session."""
        return cls(
            MessageType.RELAY_REGISTER,
            {
                "session_id": session_id,
                "device_id": device_id,
                "device_name": device_name,
            },
        )

    @classmethod
    def relay_route(cls, inner_type: int, inner_payload: dict) -> Message:
        """Route a message through the relay to the paired peer."""
        return cls(
            MessageType.RELAY_ROUTE,
            {"inner_type": inner_type, "inner_payload": inner_payload},
        )

    @classmethod
    def relay_device_list(cls, devices: list[dict]) -> Message:
        """Report the current list of connected devices (relay → peers)."""
        return cls(
            MessageType.RELAY_DEVICE_LIST,
            {"devices": devices},
        )

    @classmethod
    def video_tile(cls, data: bytes, x: int, y: int, width: int, height: int, pts: int) -> Message:
        """An incremental tile update (JPEG-encoded sub-region)."""
        return cls(
            MessageType.VIDEO_TILE,
            {
                "data": data,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "pts": pts,
            },
        )

    @classmethod
    def camera_frame(cls, data: bytes, width: int, height: int, pts: int,
                     fmt: str = "jpeg") -> Message:
        """A webcam video frame (JPEG-encoded by default)."""
        return cls(
            MessageType.CAMERA_FRAME,
            {
                "data": data,
                "width": width,
                "height": height,
                "pts": pts,
                "format": fmt,
            },
        )

    @classmethod
    def camera_start(cls, enabled: bool, width: int = 0, height: int = 0,
                     fps: int = 0) -> Message:
        """Notify peer that the webcam stream started or stopped."""
        return cls(
            MessageType.CAMERA_START,
            {
                "enabled": enabled,
                "width": width,
                "height": height,
                "fps": fps,
            },
        )

    @classmethod
    def relay_device_update(cls, device: dict, online: bool) -> Message:
        """Notify peers that a device went online or offline."""
        return cls(
            MessageType.RELAY_DEVICE_UPDATE,
            {"device": device, "online": online},
        )
