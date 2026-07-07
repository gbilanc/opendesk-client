"""
Standalone relay server for OpenDesk.

Provides fallback connectivity when direct P2P (WebRTC) fails.
Peers connect to the relay, authenticate, and the relay forwards
messages between them.

Usage::

    python -m relay_server.server --port 8474
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from opendesk.crypto.auth import AuthManager, generate_session_id, hash_password

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_PORT = 8474
_PING_INTERVAL = 30  # seconds
_PEER_TIMEOUT = 120  # seconds without activity → disconnect


# ---------------------------------------------------------------------------
# Peer state
# ---------------------------------------------------------------------------


@dataclass
class RelayPeer:
    """A peer connected to the relay server."""

    peer_id: str
    writer: asyncio.StreamWriter
    reader: asyncio.StreamReader
    session_id: str = ""
    device_id: str = ""
    device_name: str = ""
    last_activity: float = field(default_factory=time.time)
    authenticated: bool = False
    paired_peer_id: str | None = None  # the other peer in a session


# ---------------------------------------------------------------------------
# Relay server
# ---------------------------------------------------------------------------


class RelayServer:
    """TCP relay server that forwards messages between peers.

    Peers connect, optionally authenticate via session ID, and are
    paired together to exchange messages.
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = _DEFAULT_PORT,
        auth_manager: AuthManager | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._auth = auth_manager or AuthManager()
        self._peers: dict[str, RelayPeer] = {}  # peer_id → peer
        self._sessions: dict[str, str] = {}  # session_id → host_peer_id
        self._server: asyncio.AbstractServer | None = None
        self._devices: dict[str, RelayPeer] = {}  # device_id → peer (known hosts)

    # ── startup / shutdown ──────────────────────────────────────────

    async def start(self) -> None:
        """Start the relay server and keep running until stopped."""
        self._server = await asyncio.start_server(
            self._handle_client,
            host=self._host,
            port=self._port,
        )
        addr = self._server.sockets[0].getsockname()
        logger.info("Relay server listening on %s:%d", addr[0], addr[1])

        # Start periodic cleanup
        asyncio.create_task(self._cleanup_loop())

        # Keep the server running until stop() is called
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the relay server."""
        # Disconnect all peers
        for peer in list(self._peers.values()):
            try:
                peer.writer.close()
            except Exception:
                pass
        self._peers.clear()
        self._sessions.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("Relay server stopped")

    # ── client handling ─────────────────────────────────────────────

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle an incoming peer connection."""
        peer_id = f"peer-{id(writer):x}"
        peer = RelayPeer(peer_id=peer_id, writer=writer, reader=reader)
        self._peers[peer_id] = peer

        addr = writer.get_extra_info("peername")
        logger.info("Peer connected: %s from %s", peer_id, addr)

        try:
            await self._peer_loop(peer)
        except (ConnectionError, asyncio.IncompleteReadError):
            logger.debug("Peer disconnected: %s", peer_id)
        except Exception as e:
            logger.error("Error handling peer %s: %s", peer_id, e)
        finally:
            self._remove_peer(peer_id)

    async def _peer_loop(self, peer: RelayPeer) -> None:
        """Main loop for an individual peer."""
        from opendesk.network.protocol import Message, MessageType

        while True:
            msg = await Message.from_reader(peer.reader)
            peer.last_activity = time.time()
            await self._handle_message(peer, msg)

    async def _handle_message(self, peer: RelayPeer, msg: Any) -> None:  # noqa: ANN401
        """Route an incoming message appropriately."""
        from opendesk.network.protocol import Message, MessageType

        msg_type = msg.type if isinstance(msg, Message) else MessageType(msg.get("type", 0))
        payload = msg.payload if isinstance(msg, Message) else msg.get("payload", {})

        if msg_type == MessageType.RELAY_REGISTER:
            await self._handle_register(peer, payload)
        elif msg_type == MessageType.RELAY_ROUTE:
            await self._handle_route(peer, payload)
        elif msg_type == MessageType.PING:
            await self._send(peer, Message.pong(payload.get("seq", 0)))
        elif msg_type == MessageType.DISCONNECT:
            raise ConnectionError("Peer requested disconnect")
        else:
            # Forward to paired peer if any
            if peer.paired_peer_id:
                paired = self._peers.get(peer.paired_peer_id)
                if paired:
                    await self._send(paired, msg)
            else:
                logger.warning("Unhandled message from %s: %s", peer.peer_id, msg_type)

    # ── message routing ─────────────────────────────────────────────

    async def _handle_register(self, peer: RelayPeer, payload: dict) -> None:
        """Register a peer with a session ID.

        - Empty session_id: relay generates a new one (legacy mode).
        - Existing session_id: join as client, pair with host.
        - New session_id: register as host for that session_id.
        """
        from opendesk.network.protocol import Message, MessageType

        # Store device identity if provided
        device_id = payload.get("device_id", "")
        device_name = payload.get("device_name", "")
        if device_id:
            peer.device_id = device_id
            peer.device_name = device_name
            self._devices[device_id] = peer
            logger.info("Device registered: %s (%s)", device_id, device_name)

        session_id = payload.get("session_id", "")
        if not session_id:
            # Legacy: relay generates session_id
            session_id = generate_session_id()
            self._sessions[session_id] = peer.peer_id
            peer.session_id = session_id
            await self._send(
                peer,
                Message(MessageType.RELAY_REGISTER, {"session_id": session_id}),
            )
            logger.info("Session created (legacy): %s for peer %s", session_id, peer.peer_id)
            self._broadcast_device_list()
            return

        host_id = self._sessions.get(session_id)
        if host_id is not None:
            # Session exists → join as client
            host_peer = self._peers.get(host_id)
            if host_peer is None:
                del self._sessions[session_id]
                await self._send(
                    peer,
                    Message(MessageType.RELAY_REGISTER, {"session_id": session_id, "mode": "host"}),
                )
                return

            # Pair the peers
            peer.session_id = session_id
            peer.paired_peer_id = host_id
            host_peer.paired_peer_id = peer.peer_id

            await self._send(
                peer,
                Message(MessageType.RELAY_REGISTER,
                        {"session_id": session_id, "paired": True, "mode": "client"}),
            )
            await self._send(
                host_peer,
                Message(MessageType.RELAY_PEER_LIST, {"peers": [peer.peer_id]}),
            )
            logger.info("Peers paired in session %s: %s ↔ %s", session_id, host_id, peer.peer_id)
            return

        # New session → register as host with this session_id
        self._sessions[session_id] = peer.peer_id
        peer.session_id = session_id
        await self._send(
            peer,
            Message(MessageType.RELAY_REGISTER, {"session_id": session_id, "mode": "host"}),
        )
        logger.info("Session registered: %s for peer %s", session_id, peer.peer_id)
        self._broadcast_device_list()

    async def _broadcast_device_list(self) -> None:
        """Send the current list of connected devices to all peers.

        Only peers with a device_id (known hosts) are included.
        """
        from opendesk.network.protocol import Message, MessageType

        devices = [
            {
                "device_id": p.device_id,
                "device_name": p.device_name or p.device_id[:8],
                "session_id": p.session_id,
            }
            for p in self._devices.values()
            if p.device_id and p.writer and not p.writer.is_closing()
        ]
        msg = Message.relay_device_list(devices)
        # Send to every connected peer
        tasks = [
            self._send(p, msg)
            for p in self._peers.values()
            if p.writer and not p.writer.is_closing()
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle_route(self, peer: RelayPeer, payload: dict) -> None:
        """Forward a message to the paired peer."""
        if not peer.paired_peer_id:
            return

        target = self._peers.get(peer.paired_peer_id)
        if target is None:
            return

        # Re-wrap and forward
        from opendesk.network.protocol import Message, MessageType
        inner_type = MessageType(payload.get("inner_type", 0))
        inner_payload = payload.get("inner_payload", {})
        inner_msg = Message(inner_type, inner_payload)
        await self._send(target, inner_msg)

    # ── utilities ───────────────────────────────────────────────────

    async def _send(self, peer: RelayPeer, msg: Any) -> None:  # noqa: ANN401
        """Send a message to a peer."""
        try:
            from opendesk.network.protocol import Message
            if isinstance(msg, Message):
                data = msg.encode()
            elif isinstance(msg, dict):
                data = json.dumps(msg).encode() + b"\n"
            else:
                data = bytes(msg)
            peer.writer.write(data)
            await peer.writer.drain()
        except Exception as e:
            logger.warning("Failed to send to %s: %s", peer.peer_id, e)
            self._remove_peer(peer.peer_id)

    def _remove_peer(self, peer_id: str) -> None:
        """Remove a peer and clean up associated state."""
        peer = self._peers.pop(peer_id, None)
        if peer is None:
            return

        # Remove from device registry if present
        was_device = False
        if peer.device_id and self._devices.get(peer.device_id) is peer:
            del self._devices[peer.device_id]
            was_device = True
            logger.info("Device went offline: %s (%s)", peer.device_id, peer.device_name)

        # Notify paired peer
        if peer.paired_peer_id:
            paired = self._peers.get(peer.paired_peer_id)
            if paired:
                paired.paired_peer_id = None
                from opendesk.network.protocol import Message, MessageType
                asyncio.ensure_future(
                    self._send(
                        paired,
                        Message(MessageType.ERROR, {"code": 410, "message": "Peer disconnected"}),
                    )
                )

        # Remove session if host
        if peer.session_id and self._sessions.get(peer.session_id) == peer_id:
            del self._sessions[peer.session_id]

        # Broadcast updated device list to remaining peers
        if was_device:
            asyncio.ensure_future(self._broadcast_device_list())

        logger.info("Peer removed: %s", peer_id)

    async def _cleanup_loop(self) -> None:
        """Periodically disconnect stale peers."""
        while True:
            await asyncio.sleep(_PING_INTERVAL)
            now = time.time()
            stale = [
                pid for pid, p in self._peers.items()
                if now - p.last_activity > _PEER_TIMEOUT
            ]
            for pid in stale:
                peer = self._peers.get(pid)
                if peer:
                    logger.info("Removing stale peer: %s", pid)
                    try:
                        peer.writer.close()
                    except Exception:
                        pass
                    self._remove_peer(pid)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the relay server from the command line."""
    parser = argparse.ArgumentParser(description="OpenDesk Relay Server")
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=_DEFAULT_PORT,
        help=f"Bind port (default: {_DEFAULT_PORT})",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    )

    server = RelayServer(host=args.host, port=args.port)

    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
        asyncio.run(server.stop())


if __name__ == "__main__":
    main()
