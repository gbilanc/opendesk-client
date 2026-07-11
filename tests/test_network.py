"""
Tests for the network protocol and NAT traversal modules.
"""

from __future__ import annotations

from opendesk.network.protocol import Message, MessageType


class TestProtocol:
    def test_message_type_values(self) -> None:
        """All message types should have unique values."""
        values = [mt.value for mt in MessageType]
        assert len(values) == len(set(values)), "Duplicate message type values!"

    def test_hello_message(self) -> None:
        msg = Message.hello(version=1)
        assert msg.type == MessageType.HELLO
        assert msg.payload["version"] == 1
        assert not msg.encrypted

    def test_auth_messages(self) -> None:
        req = Message.auth_request("123 456 789", nonce="abc123nonce")
        assert req.type == MessageType.AUTH_REQUEST
        assert req.payload["session_id"] == "123 456 789"
        assert req.payload["nonce"] == "abc123nonce"

        resp = Message.auth_response("myhashvalue")
        assert resp.type == MessageType.AUTH_RESPONSE
        assert resp.payload["nonce_hash"] == "myhashvalue"

        ok = Message.auth_ok()
        assert ok.type == MessageType.AUTH_OK

        fail = Message.auth_fail("Wrong password")
        assert fail.type == MessageType.AUTH_FAIL
        assert fail.payload["reason"] == "Wrong password"

    def test_video_frame_message(self) -> None:
        msg = Message.video_frame(
            data=b"h264data",
            width=1280,
            height=720,
            pts=42,
            keyframe=True,
        )
        assert msg.type == MessageType.VIDEO_FRAME
        assert msg.payload["data"] == b"h264data"
        assert msg.payload["width"] == 1280
        assert msg.payload["height"] == 720
        assert msg.payload["pts"] == 42
        assert msg.payload["keyframe"] is True

    def test_mouse_event_message(self) -> None:
        msg = Message.mouse_event(x=100, y=200, button=1, pressed=True, absolute=True)
        assert msg.type == MessageType.MOUSE_EVENT
        assert msg.payload["x"] == 100
        assert msg.payload["y"] == 200
        assert msg.payload["button"] == 1
        assert msg.payload["pressed"] is True
        assert msg.payload["absolute"] is True

    def test_keyboard_event_message(self) -> None:
        msg = Message.keyboard_event(key="a", pressed=True)
        assert msg.type == MessageType.KEYBOARD_EVENT
        assert msg.payload["key"] == "a"
        assert msg.payload["pressed"] is True

    def test_chat_message(self) -> None:
        msg = Message.chat_message("Hello!")
        assert msg.type == MessageType.CHAT_MESSAGE
        assert msg.payload["text"] == "Hello!"

    def test_clipboard_message(self) -> None:
        msg = Message.clipboard_text("copied text")
        assert msg.type == MessageType.CLIPBOARD_TEXT
        assert msg.payload["text"] == "copied text"

    def test_ping_pong(self) -> None:
        ping = Message.ping(seq=1)
        assert ping.type == MessageType.PING
        assert ping.payload["seq"] == 1

        pong = Message.pong(seq=1)
        assert pong.type == MessageType.PONG
        assert pong.payload["seq"] == 1

    def test_disconnect_message(self) -> None:
        msg = Message.disconnect(reason="Session ended")
        assert msg.type == MessageType.DISCONNECT
        assert msg.payload["reason"] == "Session ended"

    def test_error_message(self) -> None:
        msg = Message.error(code=404, message="Not found")
        assert msg.type == MessageType.ERROR
        assert msg.payload["code"] == 404
        assert msg.payload["message"] == "Not found"

    def test_serialisation_roundtrip(self) -> None:
        """Encode then decode should yield an equivalent message."""
        original = Message.chat_message("Hello, world!")
        data = original.encode()
        restored = Message.decode(data)

        assert restored.type == original.type
        assert restored.payload == original.payload
        assert restored.encrypted == original.encrypted

    def test_serialisation_binary_payload(self) -> None:
        """Binary payloads (e.g. video data) should survive roundtrip."""
        original = Message.video_frame(
            data=b"\x00\x01\x02\xff\xfe" * 100,
            width=640,
            height=480,
            pts=10,
            keyframe=True,
        )
        data = original.encode()
        restored = Message.decode(data)

        assert restored.payload["data"] == original.payload["data"]
        assert restored.payload["width"] == 640

    def test_serialisation_empty_payload(self) -> None:
        msg = Message.auth_ok()
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.type == MessageType.AUTH_OK

    def test_hello_roundtrip(self) -> None:
        msg = Message.hello(version=1)
        data = msg.encode()
        restored = Message.decode(data)
        assert restored.type == MessageType.HELLO
        assert restored.payload["version"] == 1

    def test_factory_methods_exist(self) -> None:
        """All factory methods should be callable."""
        assert callable(Message.hello)
        assert callable(Message.hello_ack)
        assert callable(Message.key_exchange)
        assert callable(Message.auth_request)
        assert callable(Message.auth_response)
        assert callable(Message.auth_ok)
        assert callable(Message.auth_fail)
        assert callable(Message.video_frame)
        assert callable(Message.mouse_event)
        assert callable(Message.keyboard_event)
        assert callable(Message.clipboard_text)
        assert callable(Message.chat_message)
        assert callable(Message.ping)
        assert callable(Message.pong)
        assert callable(Message.disconnect)
        assert callable(Message.error)
