"""
NAT traversal utilities.

Provides STUN-based external address discovery and TURN relay
configuration helpers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ExternalEndpoint:
    """Discovered external IP:port."""

    ip: str
    port: int
    protocol: str = "udp"


@dataclass
class TURNServer:
    """TURN server configuration."""

    url: str  # e.g. "turn:turn.example.com:3478"
    username: str = ""
    credential: str = ""
    credential_type: str = "password"


async def discover_stun(
    stun_host: str = "stun.l.google.com",
    stun_port: int = 19302,
    timeout: float = 5.0,
) -> ExternalEndpoint | None:
    """Discover the external IP:port via a STUN server.

    Parameters
    ----------
    stun_host : str
        STUN server hostname.
    stun_port : int
        STUN server port (default 19302).
    timeout : float
        Timeout in seconds.

    Returns
    -------
    ExternalEndpoint or None
        The discovered external address, or ``None`` on failure.
    """
    # STUN binding request (RFC 5389)
    # Transaction ID (random 12 bytes)
    import random
    import struct
    transaction_id = bytes(random.randint(0, 255) for _ in range(12))

    # Message header: type=0x0001 (binding request), length=0, magic cookie=0x2112A442
    header = struct.pack("!HHI", 0x0001, 0x0000, 0x2112A442) + transaction_id

    try:
        loop = asyncio.get_event_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: STUNProtocol(header, timeout),
            remote_addr=(stun_host, stun_port),
        )

        try:
            result = await asyncio.wait_for(
                protocol.result_future, timeout=timeout
            )
            return result
        except TimeoutError:
            logger.warning("STUN request timed out")
            return None
        finally:
            transport.close()

    except Exception as e:
        logger.warning("STUN discovery failed: %s", e)
        return None


class STUNProtocol(asyncio.DatagramProtocol):
    """Simple STUN protocol handler for external address discovery."""

    def __init__(self, request: bytes, timeout: float) -> None:
        self._request = request
        self._transport: asyncio.DatagramTransport | None = None
        self.result_future: asyncio.Future[ExternalEndpoint] = asyncio.Future()

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self._transport = transport
        transport.sendto(self._request)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        import struct

        try:
            # Parse STUN response header
            msg_type, msg_len, magic_cookie = struct.unpack("!HHI", data[:8])
            if msg_type != 0x0101:  # binding response
                return

            # Parse attributes to find XOR-MAPPED-ADDRESS (0x0020)
            offset = 20  # skip header + transaction id (12 bytes after magic cookie)
            while offset < len(data):
                attr_type, attr_len = struct.unpack("!HH", data[offset:offset + 4])
                if attr_type == 0x0020:  # XOR-MAPPED-ADDRESS
                    value = data[offset + 4:offset + 4 + attr_len]
                    # First byte: reserved, second byte: address family
                    family = value[1]
                    # XOR with magic cookie high bits
                    port = struct.unpack("!H", value[2:4])[0] ^ 0x2112

                    if family == 0x01:  # IPv4
                        ip_bytes = bytes(
                            b ^ c for b, c in zip(value[4:8], struct.pack("!I", 0x2112A442))
                        )
                        ip = ".".join(str(b) for b in ip_bytes)
                    else:  # IPv6
                        ip = "::1"  # simplified for now

                    endpoint = ExternalEndpoint(ip=ip, port=port)
                    self.result_future.set_result(endpoint)
                    return

                offset += 4 + attr_len

        except Exception as e:
            if not self.result_future.done():
                self.result_future.set_exception(e)

    def error_received(self, exc: Exception) -> None:
        if not self.result_future.done():
            self.result_future.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if not self.result_future.done() and exc:
            self.result_future.set_exception(exc)
