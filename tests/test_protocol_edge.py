"""
Edge-case tests for the network protocol.

Covers large payloads, empty payloads, encoding corner cases,
and message framing.
"""

from __future__ import annotations

import struct

from opendesk.network.protocol import (
    _HEADER_FORMAT,
    _HEADER_SIZE,
    _MAX_MESSAGE_SIZE,
    Message,
    MessageType,
)


class TestProtocolEdgeCases:
    def test_empty_payload_roundtrip(self) -> None:
        """Messages with no payload should encode/decode correctly."""
        msg = Message(MessageType.PING, {"seq": 0})
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.type == MessageType.PING
        assert restored.payload == {"seq": 0}

    def test_large_payload(self) -> None:
        """Large binary payloads should survive roundtrip."""
        large_data = b"x" * 100_000  # 100 KB
        msg = Message(MessageType.VIDEO_FRAME, {
            "data": large_data,
            "width": 1920,
            "height": 1080,
            "pts": 12345,
            "keyframe": True,
        })
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.payload["data"] == large_data
        assert restored.payload["width"] == 1920

    def test_special_characters_in_payload(self) -> None:
        """Unicode and special characters should survive encoding."""
        msg = Message.chat_message("Hello 世界! Café ñoño 🎉")
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.payload["text"] == "Hello 世界! Café ñoño 🎉"

    def test_nested_payload(self) -> None:
        """Nested dicts in payload should work."""
        msg = Message(MessageType.RELAY_PEER_LIST, {
            "peers": [
                {"id": "peer1", "name": "Alice"},
                {"id": "peer2", "name": "Bob"},
            ],
            "metadata": {"version": 1, "protocol": "opendesk"},
        })
        data = msg.encode()
        restored = Message.decode(data)
        assert len(restored.payload["peers"]) == 2
        assert restored.payload["metadata"]["version"] == 1

    def test_all_numeric_types(self) -> None:
        """All numeric types should survive (int, float, bool)."""
        msg = Message(MessageType.MOUSE_EVENT, {
            "x": -100,
            "y": 200,
            "button": 1,
            "pressed": True,
            "absolute": False,
            "pressure": 0.75,
        })
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.payload["x"] == -100
        assert restored.payload["pressed"] is True
        assert restored.payload["pressure"] == 0.75

    def test_header_format(self) -> None:
        """Header should be exactly 4 bytes."""
        msg = Message.ping()
        data = msg.encode()
        header = data[:_HEADER_SIZE]
        assert len(header) == _HEADER_SIZE == 4
        body_len = struct.unpack(_HEADER_FORMAT, header)[0]
        assert body_len == len(data) - _HEADER_SIZE

    def test_max_message_size_not_exceeded(self) -> None:
        """Verify the max message size constant is reasonable."""
        assert _MAX_MESSAGE_SIZE == 100 * 1024 * 1024  # 100 MB

    def test_encrypted_flag(self) -> None:
        """Encrypted flag should survive encoding."""
        msg = Message(MessageType.CHAT_MESSAGE, {"text": "secret"})
        msg.encrypted = True
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.encrypted is True

    def test_multiple_messages_frame_correctly(self) -> None:
        """Multiple messages encoded sequentially should not overlap."""
        msgs = [
            Message.ping(seq=0),
            Message.chat_message("msg 0"),
            Message.keyboard_event("a", pressed=True),
        ]
        all_data = b"".join(m.encode() for m in msgs)

        # Decode them one by one
        restored = []
        offset = 0
        while offset < len(all_data):
            header = all_data[offset:offset + _HEADER_SIZE]
            body_len = struct.unpack(_HEADER_FORMAT, header)[0]
            chunk = all_data[offset:offset + _HEADER_SIZE + body_len]
            restored.append(Message.decode(chunk))
            offset += _HEADER_SIZE + body_len

        assert len(restored) == 3
        assert restored[0].type == MessageType.PING
        assert restored[1].type == MessageType.CHAT_MESSAGE
        assert restored[2].type == MessageType.KEYBOARD_EVENT

    def test_float_precision(self) -> None:
        """Float precision should be preserved."""
        msg = Message(MessageType.FILE_PROGRESS, {"progress": 0.123456789})
        data = msg.encode()
        restored = Message.decode(data)
        assert abs(restored.payload["progress"] - 0.123456789) < 1e-9

    def test_none_values(self) -> None:
        """None values in payload should survive."""
        msg = Message(MessageType.MOUSE_EVENT, {
            "x": 100, "y": 200, "button": None, "pressed": None,
        })
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.payload["button"] is None
        assert restored.payload["pressed"] is None

    def test_empty_dict_payload(self) -> None:
        """Empty dict payload should encode properly."""
        msg = Message(MessageType.AUTH_OK)
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.payload == {}
