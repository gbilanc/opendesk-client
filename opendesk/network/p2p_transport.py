"""WebRTC data-channel transport for direct peer-to-peer sessions.

The relay remains the signalling and fallback path; once ICE succeeds the
application messages flow directly between the two peers over SCTP/DTLS.

Only ``aiortc`` is used (already a project dependency).  Payloads are still
E2E-encrypted by ``RelayClient`` before reaching this module.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.rtcdatachannel import RTCDataChannel

from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# Drop media when the data channel has too much queued data.  The exact value
# is not critical: it only needs to be large enough to absorb a keyframe.
_MAX_MEDIA_BUFFERED_BYTES = 2 * 1024 * 1024

# Message types that benefit from an unordered/unreliable channel.
MEDIA_MESSAGE_TYPES = frozenset(
    {
        MessageType.VIDEO_FRAME,
        MessageType.VIDEO_TILE,
        MessageType.AUDIO_FRAME,
        MessageType.CAMERA_FRAME,
    }
)


def build_ice_servers(
    stun_server: str = "",
    turn_url: str = "",
    turn_username: str = "",
    turn_credential: str = "",
) -> list[RTCIceServer]:
    """Build aiortc ICE servers from the user settings."""
    servers: list[RTCIceServer] = []

    stun = stun_server.strip()
    if stun:
        if not stun.startswith(("stun:", "stuns:")):
            stun = f"stun:{stun}"
        servers.append(RTCIceServer(urls=[stun]))

    turn = turn_url.strip()
    if turn:
        if not turn.startswith(("turn:", "turns:")):
            turn = f"turn:{turn}"
        servers.append(
            RTCIceServer(
                urls=[turn],
                username=turn_username.strip() or None,
                credential=turn_credential or None,
            )
        )

    return servers


class P2PTransport:
    """A pair of WebRTC data channels (control + media).

    The host creates the channels and the offer; the client answers.  Calls
    are expected to run inside the asyncio loop that owns the
    ``RTCPeerConnection``.  ``RelayClient`` schedules cross-thread sends onto
    that loop.
    """

    def __init__(
        self,
        ice_servers: list[RTCIceServer] | None = None,
        on_message: Callable[[bytes], None] | None = None,
        on_state_change: Callable[[str], None] | None = None,
    ) -> None:
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._closed = False
        self._ready = False
        self._control: RTCDataChannel | None = None
        self._media: RTCDataChannel | None = None

        config = RTCConfiguration(iceServers=ice_servers or [])
        self._pc = RTCPeerConnection(config)
        self._pc.on("connectionstatechange", self._on_connection_state_change)
        self._pc.on("datachannel", self._on_data_channel)

    # ── properties ──────────────────────────────────────────────────

    @property
    def ready(self) -> bool:
        return self._ready and not self._closed

    @property
    def connection_state(self) -> str:
        return self._pc.connectionState

    # ── negotiation ─────────────────────────────────────────────────

    async def create_offer(self) -> str:
        """Create the data channels and return a complete local SDP offer.

        ``aiortc`` gathers ICE candidates inside ``setLocalDescription``, so
        the returned SDP is non-trickle and already contains candidates.
        """
        self._control = self._pc.createDataChannel("control")
        self._bind_channel(self._control)
        self._media = self._pc.createDataChannel(
            "media",
            ordered=False,
            maxRetransmits=0,
        )
        self._bind_channel(self._media)

        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        return self._pc.localDescription.sdp

    async def accept_offer(self, sdp: str) -> str:
        """Apply the remote offer and return a complete local SDP answer."""
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        return self._pc.localDescription.sdp

    async def accept_answer(self, sdp: str) -> None:
        """Apply the remote answer on the host side."""
        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    # ── data path ───────────────────────────────────────────────────

    def send(self, msg: Message, *, media: bool | None = None) -> bool:
        """Send a protocol message over the appropriate data channel.

        Must be called from the asyncio loop that owns the peer connection.
        Returns ``False`` when the channel is not open or media backpressure
        drops the message.
        """
        if not self.ready:
            return False

        if media is None:
            media = msg.type in MEDIA_MESSAGE_TYPES
        channel = self._media if media else self._control
        if channel is None or channel.readyState != "open":
            return False

        if media and channel.bufferedAmount > _MAX_MEDIA_BUFFERED_BYTES:
            logger.debug(
                "P2P media backpressure: buffered=%d, dropping type=%s",
                channel.bufferedAmount,
                msg.type.name,
            )
            return False

        try:
            channel.send(msg.encode())
            return True
        except Exception as exc:  # pragma: no cover - aiortc/transport errors
            logger.warning("P2P send failed (type=%s): %s", msg.type.name, exc)
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            asyncio.get_running_loop().create_task(self._pc.close())
        except RuntimeError:
            # Loop already closed (application shutdown); nothing to do.
            pass
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("P2P close failed: %s", exc)
        self._notify_state("closed")

    async def aclose(self) -> None:
        """Close the peer connection and wait for transport teardown."""
        if self._closed:
            return
        self._closed = True
        self._ready = False
        try:
            await self._pc.close()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("P2P async close failed: %s", exc)
        self._notify_state("closed")

    # ── callbacks ───────────────────────────────────────────────────

    def _bind_channel(self, channel: RTCDataChannel) -> None:
        if channel.label == "media":
            self._media = channel
        else:
            self._control = channel

        channel.on("open", self._check_ready)
        channel.on("close", self._on_channel_close)
        channel.on("message", self._on_channel_message)
        if channel.readyState == "open":
            self._check_ready()

    def _on_data_channel(self, channel: RTCDataChannel) -> None:
        self._bind_channel(channel)

    def _on_connection_state_change(self) -> None:
        state = self._pc.connectionState
        logger.info("P2P connection state: %s", state)
        if state == "connected":
            self._check_ready()
        elif state in ("failed", "closed"):
            was_ready = self._ready
            self._ready = False
            if state == "failed":
                self._notify_state("failed")
            elif was_ready:
                self._notify_state("closed")

    def _on_channel_close(self) -> None:
        if self._ready:
            self._ready = False
            self._notify_state("closed")

    def _check_ready(self) -> None:
        if self._ready or self._closed:
            return
        if self._control is None or self._control.readyState != "open":
            return
        if self._media is not None and self._media.readyState != "open":
            return
        self._ready = True
        logger.info("P2P data channels open")
        self._notify_state("connected")

    def _on_channel_message(self, data: bytes | str) -> None:
        if self._on_message is None:
            return
        try:
            payload = data.encode("utf-8") if isinstance(data, str) else data
            self._on_message(payload)
        except Exception:
            logger.exception("P2P message callback failed")

    def _notify_state(self, state: str) -> None:
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(state)
        except Exception:
            logger.exception("P2P state callback failed")
