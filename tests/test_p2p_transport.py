"""Tests for the direct WebRTC data-channel transport."""

from __future__ import annotations

import asyncio

from opendesk.network.p2p_transport import (
    MEDIA_MESSAGE_TYPES,
    P2PTransport,
    build_ice_servers,
)
from opendesk.network.protocol import Message, MessageType


def test_p2p_signalling_messages_roundtrip() -> None:
    offer = Message.p2p_offer("v=0\r\n")
    answer = Message.p2p_answer("v=0\r\n")
    close = Message.p2p_close("done")

    assert offer.type == MessageType.P2P_OFFER
    assert answer.type == MessageType.P2P_ANSWER
    assert close.type == MessageType.P2P_CLOSE

    for msg in (offer, answer, close):
        decoded = Message.decode(msg.encode())
        assert decoded.type == msg.type
        assert decoded.payload == msg.payload


def test_build_ice_servers() -> None:
    servers = build_ice_servers(
        stun_server="stun.l.google.com:19302",
        turn_url="turn:turn.example.com:3478",
        turn_username="user",
        turn_credential="secret",
    )

    assert len(servers) == 2
    assert servers[0].urls == ["stun:stun.l.google.com:19302"]
    assert servers[1].urls == ["turn:turn.example.com:3478"]
    assert servers[1].username == "user"
    assert servers[1].credential == "secret"


def test_media_types() -> None:
    assert MessageType.VIDEO_FRAME in MEDIA_MESSAGE_TYPES
    assert MessageType.VIDEO_TILE in MEDIA_MESSAGE_TYPES
    assert MessageType.AUDIO_FRAME in MEDIA_MESSAGE_TYPES
    assert MessageType.CAMERA_FRAME in MEDIA_MESSAGE_TYPES
    assert MessageType.CHAT_MESSAGE not in MEDIA_MESSAGE_TYPES


def test_loopback_data_channel() -> None:
    async def scenario() -> None:
        host_messages: list[Message] = []
        client_messages: list[Message] = []

        host = P2PTransport(on_message=lambda data: host_messages.append(Message.decode(data)))
        client = P2PTransport(on_message=lambda data: client_messages.append(Message.decode(data)))

        try:
            offer = await host.create_offer()
            answer = await client.accept_offer(offer)
            await host.accept_answer(answer)

            for _ in range(100):
                if host.ready and client.ready:
                    break
                await asyncio.sleep(0.05)

            assert host.ready, "host data channels did not open"
            assert client.ready, "client data channels did not open"

            assert host.send(Message(MessageType.CHAT_MESSAGE, {"text": "hello"}))
            for _ in range(40):
                if client_messages:
                    break
                await asyncio.sleep(0.05)

            assert len(client_messages) == 1
            assert client_messages[0].type == MessageType.CHAT_MESSAGE
            assert client_messages[0].payload == {"text": "hello"}
        finally:
            await host.aclose()
            await client.aclose()

    asyncio.run(scenario())
