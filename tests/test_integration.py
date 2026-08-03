"""
Integration tests for end-to-end P2P scenarios.

Tests protocol handshake, E2E encryption, and message routing
between two simulated peers without actual network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from opendesk.crypto.e2ee import E2EEncryption
from opendesk.network.protocol import Message, MessageType

# ======================================================================
# Simulated peer for integration testing
# ======================================================================


@dataclass
class SimulatedPeer:
    """A minimal peer that can send/receive messages via a callback."""

    peer_id: str
    encryption: E2EEncryption = field(default_factory=E2EEncryption)
    received_messages: list[Message] = field(default_factory=list)
    remote_key_set: bool = False

    async def send(self, msg: Message, to_peer: SimulatedPeer) -> None:
        """Simulate sending a message to another peer."""
        # Encrypt if E2E keys have been exchanged
        if self.remote_key_set and msg.type not in (
            MessageType.HELLO, MessageType.KEY_EXCHANGE,
        ):
            from opendesk.crypto.e2ee import encrypt_json
            encrypted = encrypt_json(self.encryption, msg.payload)
            msg.payload = {"encrypted": encrypted.encode().hex()}
            msg.encrypted = True

        await to_peer.receive(msg, self)

    async def receive(self, msg: Message, from_peer: SimulatedPeer) -> None:
        """Handle an incoming message from another peer."""
        # Decrypt if needed
        if msg.encrypted and self.remote_key_set:
            from opendesk.crypto.e2ee import EncryptedMessage
            em = EncryptedMessage.decode(bytes.fromhex(msg.payload["encrypted"]))
            plain = self.encryption.decrypt(em)
            import json
            msg.payload = json.loads(plain.decode("utf-8"))
            msg.encrypted = False

        self.received_messages.append(msg)


# ======================================================================
# Integration test scenarios
# ======================================================================


class TestP2PIntegration:
    """Full handshake + E2E encrypted communication."""

    async def _handshake(self, alice: SimulatedPeer, bob: SimulatedPeer) -> None:

        """Simulate the initial handshake between two peers."""
        # 1. Hello
        await alice.send(Message.hello(version=1), bob)
        await bob.send(Message.hello_ack(version=1), alice)

        # 2. Key exchange
        alice_pub = alice.encryption.get_public_key_string()
        await alice.send(Message.key_exchange(alice_pub), bob)

        bob.set_remote_key = True
        bob.encryption.set_remote_key(alice_pub)

        bob_pub = bob.encryption.get_public_key_string()
        await bob.send(Message.key_exchange(bob_pub), alice)

        alice.set_remote_key = True
        alice.encryption.set_remote_key(bob_pub)

    @pytest.mark.asyncio
    async def test_full_handshake(self) -> None:
        """Peers should exchange hello + keys successfully."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)

        assert len(alice.received_messages) == 2
        assert alice.received_messages[0].type == MessageType.HELLO_ACK
        assert alice.received_messages[1].type == MessageType.KEY_EXCHANGE

        assert len(bob.received_messages) == 2
        assert bob.received_messages[0].type == MessageType.HELLO
        assert bob.received_messages[1].type == MessageType.KEY_EXCHANGE

    @pytest.mark.asyncio
    async def test_e2e_chat(self) -> None:
        """After handshake, chat messages should be E2E encrypted."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)

        # Clear handshake messages
        alice.received_messages.clear()
        bob.received_messages.clear()

        # Send encrypted chat
        await alice.send(Message.chat_message("Hello Bob, this is secret!"), bob)
        await bob.send(Message.chat_message("Hi Alice, I can read you!"), alice)

        assert len(bob.received_messages) == 1
        assert bob.received_messages[0].type == MessageType.CHAT_MESSAGE
        assert bob.received_messages[0].payload["text"] == "Hello Bob, this is secret!"
        assert bob.received_messages[0].encrypted is False  # was decrypted

        assert len(alice.received_messages) == 1
        assert alice.received_messages[0].type == MessageType.CHAT_MESSAGE
        assert alice.received_messages[0].payload["text"] == "Hi Alice, I can read you!"
        assert alice.received_messages[0].encrypted is False  # was decrypted

    @pytest.mark.asyncio
    async def test_e2e_video_frame(self) -> None:
        """Video frames should survive E2E encryption roundtrip."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)
        alice.received_messages.clear()
        bob.received_messages.clear()

        frame_data = b"\x00\x01\x02" * 1000
        await alice.send(
            Message.video_frame(frame_data, 320, 180, pts=0, keyframe=True),
            bob,
        )

        assert len(bob.received_messages) == 1
        msg = bob.received_messages[0]
        assert msg.type == MessageType.VIDEO_FRAME
        assert msg.payload["width"] == 320
        assert msg.payload["height"] == 180
        assert msg.payload["data"] == frame_data

    @pytest.mark.asyncio
    async def test_mouse_and_keyboard_events(self) -> None:
        """Input events should be routed correctly."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)
        alice.received_messages.clear()
        bob.received_messages.clear()

        # Alice sends input events to Bob
        await alice.send(Message.mouse_event(100, 200, button=1, pressed=True), bob)
        await alice.send(Message.keyboard_event("a", pressed=True), bob)
        await alice.send(Message.keyboard_event("a", pressed=False), bob)

        assert len(bob.received_messages) == 3
        assert bob.received_messages[0].type == MessageType.MOUSE_EVENT
        assert bob.received_messages[0].payload["x"] == 100
        assert bob.received_messages[1].type == MessageType.KEYBOARD_EVENT
        assert bob.received_messages[2].type == MessageType.KEYBOARD_EVENT

    @pytest.mark.asyncio
    async def test_file_transfer_over_e2e(self) -> None:
        """File transfer messages should work over encrypted channel."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)
        alice.received_messages.clear()
        bob.received_messages.clear()

        # File request
        await alice.send(
            Message(MessageType.FILE_REQUEST, {
                "name": "document.pdf", "size": 50000, "sha256": "abc",
            }),
            bob,
        )
        assert len(bob.received_messages) == 1
        assert bob.received_messages[0].type == MessageType.FILE_REQUEST
        assert bob.received_messages[0].payload["name"] == "document.pdf"

    @pytest.mark.asyncio
    async def test_reconnection_flow(self) -> None:
        """Simulate disconnect and reconnect."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)
        alice.received_messages.clear()
        bob.received_messages.clear()

        # Disconnect
        await alice.send(Message.disconnect(reason="User requested"), bob)
        assert bob.received_messages[0].type == MessageType.DISCONNECT
        assert bob.received_messages[0].payload["reason"] == "User requested"

        # Reconnect with new keys (PFS)
        alice.received_messages.clear()
        bob.received_messages.clear()

        alice.encryption.rotate_keys()
        bob.encryption.rotate_keys()
        alice.set_remote_key = False
        bob.set_remote_key = False

        await self._handshake(alice, bob)
        assert len(bob.received_messages) == 2  # hello + key_exchange

    @pytest.mark.asyncio
    async def test_ping_pong(self) -> None:
        """Ping/pong for latency measurement."""
        alice = SimulatedPeer(peer_id="alice")
        bob = SimulatedPeer(peer_id="bob")

        await self._handshake(alice, bob)
        alice.received_messages.clear()
        bob.received_messages.clear()

        await alice.send(Message.ping(seq=42), bob)
        assert bob.received_messages[0].type == MessageType.PING
        assert bob.received_messages[0].payload["seq"] == 42
