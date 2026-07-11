"""
Integration tests for the relay protocol.

Spins up a lightweight mock relay server, connects a host and a
client, and verifies the full authentication handshake including
the challenge-response mechanism.

The mock relay is deliberately minimal — it pairs the first two
connections and relays all messages between them.
"""

from __future__ import annotations

import asyncio
import logging
import threading

import pytest

from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)


async def _write_async(writer: asyncio.StreamWriter, msg: Message) -> None:
    """Write a message and drain the writer."""
    data = msg.encode()
    writer.write(data)
    await writer.drain()


class _PairedRelay:
    """Minimal relay: pairs first two connections, relays messages."""

    def __init__(self) -> None:
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._a_reader: asyncio.StreamReader | None = None
        self._a_writer: asyncio.StreamWriter | None = None
        self._b_reader: asyncio.StreamReader | None = None
        self._b_writer: asyncio.StreamWriter | None = None
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connected = asyncio.Event()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._a_writer is None:
            self._a_reader, self._a_writer = reader, writer
            writer.write(b"ok")
            await writer.drain()
            # Wait for peer B before forwarding
            await self._connected.wait()
            # Relay all messages from A → B
            while True:
                try:
                    msg = await Message.from_reader(reader)
                    await _write_async(self._b_writer, msg)
                except Exception:
                    break
        elif self._b_writer is None:
            self._b_reader, self._b_writer = reader, writer
            writer.write(b"ok")
            await writer.drain()
            self._connected.set()
            # Relay all messages from B → A
            while True:
                try:
                    msg = await Message.from_reader(reader)
                    await _write_async(self._a_writer, msg)
                except Exception:
                    break

    async def _run(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self._ready.set()
        async with self._server:
            await self._server.serve_forever()

    def start(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run())

    def start_thread(self) -> None:
        self._thread = threading.Thread(target=self.start, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._server:
            self._server.close()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)


async def _connect_and_read_ok(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect and read the initial 'ok' byte."""
    r, w = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=5.0,
    )
    data = await asyncio.wait_for(r.read(2), timeout=5.0)
    assert data == b"ok"
    return r, w


class TestRelayHandshake:
    """Integration test: two peers connect and authenticate via relay."""

    @pytest.mark.asyncio
    async def test_auth_handshake(self) -> None:
        """Host and client authenticate via challenge-response through relay."""
        from opendesk.crypto.challenge import generate_nonce, compute_response, verify_response

        PASSWORD = "testpass123"

        relay = _PairedRelay()
        relay.start_thread()
        port = relay.port

        # Both connect
        h_r, h_w = await _connect_and_read_ok("127.0.0.1", port)
        c_r, c_w = await _connect_and_read_ok("127.0.0.1", port)

        # Host → Client: AUTH_REQUEST with nonce
        nonce = generate_nonce()
        await _write_async(h_w, Message.auth_request("SID123", nonce=nonce))

        req = await asyncio.wait_for(Message.from_reader(c_r), timeout=5.0)
        assert req.type == MessageType.AUTH_REQUEST
        assert req.payload["nonce"] == nonce
        assert req.payload["session_id"] == "SID123"

        # Client → Host: AUTH_RESPONSE with nonce_hash
        client_hash = compute_response(nonce, PASSWORD)
        await _write_async(c_w, Message.auth_response(client_hash))

        resp = await asyncio.wait_for(Message.from_reader(h_r), timeout=5.0)
        assert resp.type == MessageType.AUTH_RESPONSE
        assert verify_response(nonce, PASSWORD, resp.payload["nonce_hash"])

        # Host → Client: AUTH_OK
        await _write_async(h_w, Message.auth_ok())
        ok = await asyncio.wait_for(Message.from_reader(c_r), timeout=5.0)
        assert ok.type == MessageType.AUTH_OK

        relay.stop()
        logger.info("Auth handshake integration test PASSED")

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(self) -> None:
        """Wrong password causes AUTH_FAIL."""
        from opendesk.crypto.challenge import generate_nonce, compute_response, verify_response

        PASSWORD = "realpass"
        WRONG = "wrongpass"

        relay = _PairedRelay()
        relay.start_thread()
        port = relay.port

        h_r, h_w = await _connect_and_read_ok("127.0.0.1", port)
        c_r, c_w = await _connect_and_read_ok("127.0.0.1", port)

        nonce = generate_nonce()
        await _write_async(h_w, Message.auth_request("SID", nonce=nonce))

        req = await asyncio.wait_for(Message.from_reader(c_r), timeout=5.0)
        wrong_hash = compute_response(req.payload["nonce"], WRONG)
        await _write_async(c_w, Message.auth_response(wrong_hash))

        resp = await asyncio.wait_for(Message.from_reader(h_r), timeout=5.0)
        assert resp.type == MessageType.AUTH_RESPONSE
        assert not verify_response(nonce, PASSWORD, resp.payload["nonce_hash"])

        await _write_async(h_w, Message.auth_fail("Bad password"))
        fail = await asyncio.wait_for(Message.from_reader(c_r), timeout=5.0)
        assert fail.type == MessageType.AUTH_FAIL

        relay.stop()
        logger.info("Wrong password rejection test PASSED")

    @pytest.mark.asyncio
    async def test_message_relay(self) -> None:
        """Non-auth messages are relayed correctly."""
        relay = _PairedRelay()
        relay.start_thread()
        port = relay.port

        h_r, h_w = await _connect_and_read_ok("127.0.0.1", port)
        c_r, c_w = await _connect_and_read_ok("127.0.0.1", port)

        # Send CHAT_MESSAGE from host → client
        await _write_async(h_w, Message.chat_message("Hello from host"))
        msg = await asyncio.wait_for(Message.from_reader(c_r), timeout=5.0)
        assert msg.type == MessageType.CHAT_MESSAGE
        assert msg.payload["text"] == "Hello from host"

        # Send PING from client → host
        await _write_async(c_w, Message.ping(seq=42))
        msg = await asyncio.wait_for(Message.from_reader(h_r), timeout=5.0)
        assert msg.type == MessageType.PING
        assert msg.payload["seq"] == 42

        relay.stop()
        logger.info("Message relay test PASSED")
