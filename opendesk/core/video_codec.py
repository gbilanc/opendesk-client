"""
Video encoding and decoding using PyAV (FFmpeg bindings).

Provides H.264 encoding with adaptive quality, delta-frame support,
and bandwidth-aware bitrate control.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Lock, RLock

import av
import numpy as np

from opendesk.core.screen_capture import CapturedFrame

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quality presets
# ---------------------------------------------------------------------------


class QualityLevel(Enum):
    """Predefined quality / bandwidth trade-off levels."""

    LOW = auto()  # ~0.5 Mbps — good for slow connections
    MEDIUM = auto()  # ~2 Mbps — balanced
    HIGH = auto()  # ~8 Mbps — good image quality
    LOSSLESS = auto()  # ~20+ Mbps — near-lossless (LAN)


_QUALITY_BITRATE: dict[QualityLevel, int] = {
    QualityLevel.LOW: 500_000,
    QualityLevel.MEDIUM: 2_000_000,
    QualityLevel.HIGH: 8_000_000,
    QualityLevel.LOSSLESS: 20_000_000,
}


@dataclass
class EncoderConfig:
    """Configuration for the H.264 encoder."""

    width: int
    height: int
    fps: float = 30.0
    bitrate: int = 2_000_000  # bps
    quality: QualityLevel = QualityLevel.MEDIUM
    gop_size: int = 60  # keyframe interval (in frames)
    pixel_format: str = "yuv420p"


@dataclass
class EncodedPacket:
    """A single encoded video packet."""

    data: bytes
    pts: int  # presentation timestamp
    is_keyframe: bool
    width: int
    height: int
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


class VideoEncoder:
    """H.264 encoder wrapping PyAV.

    Usage::

        enc = VideoEncoder(width=1920, height=1080)
        packet = enc.encode(rgb_frame)
        enc.release()
    """

    def __init__(self, config: EncoderConfig | None = None) -> None:
        self._config = config or EncoderConfig(width=1280, height=720)
        self._lock = Lock()
        self._container: Any = None  # noqa: ANN401
        self._stream: Any = None  # noqa: ANN401
        self._pts: int = 0
        self._initialised = False
        self._actual_bitrate: int = self._config.bitrate
        self._force_next_keyframe: bool = False

    # ── properties ──────────────────────────────────────────────────

    @property
    def config(self) -> EncoderConfig:
        return self._config

    @property
    def actual_bitrate(self) -> int:
        return self._actual_bitrate

    @actual_bitrate.setter
    def actual_bitrate(self, value: int) -> None:
        """Dynamically adjust bitrate (rounded to reasonable bounds)."""
        self._actual_bitrate = max(100_000, min(50_000_000, value))
        if self._initialised:
            self._reinitialise()
        logger.debug("Bitrate adjusted to %d bps", self._actual_bitrate)

    def set_quality(self, level: QualityLevel) -> None:
        """Set a predefined quality level and adjust bitrate."""
        self._config.quality = level
        self.actual_bitrate = _QUALITY_BITRATE[level]

    # ── encoding ────────────────────────────────────────────────────

    def encode(self, frame: np.ndarray) -> list[EncodedPacket]:
        """Encode an RGB (H, W, 3) numpy array into H.264 packets.

        Parameters
        ----------
        frame : np.ndarray
            RGB uint8 image.

        Returns
        -------
        list[EncodedPacket]
            Encoded packets. On the first call, the encoder also emits
            SPS/PPS extradata; we merge those into the first keyframe
            packet so the receiver always gets a complete decodable unit.
        """
        self._ensure_initialised(frame.shape[1], frame.shape[0])

        # Force keyframe on the very first frame
        # NOTE: check _pts BEFORE calling _make_av_frame (which increments it).
        force_keyframe = self._pts == 0 or self._force_next_keyframe
        self._force_next_keyframe = False

        # Convert RGB → YUV420P
        yuv = self._rgb_to_yuv(frame)

        # Create a VideoFrame from the YUV planes
        av_frame = self._make_av_frame(yuv)

        if force_keyframe:
            av_frame.pict_type = 1  # PictureType.I (IDR / keyframe)

        raw_packets = list(self._stream.encode(av_frame))
        if not raw_packets:
            return []

        # PyAV may emit multiple packets for one input frame
        # (e.g. SPS/PPS + IDR on the first keyframe).
        # Combine them into one so the receiver never sees
        # a partial / undecodable H.264 payload.
        if len(raw_packets) > 1:
            has_keyframe = any(p.is_keyframe for p in raw_packets)
            combined = b"".join(bytes(p) for p in raw_packets)
            last = raw_packets[-1]
            return [EncodedPacket(
                data=combined,
                pts=last.pts or 0,
                is_keyframe=has_keyframe,
                width=self._config.width,
                height=self._config.height,
            )]

        return [self._packet_from_av(raw_packets[0])]

    def flush(self) -> list[EncodedPacket]:
        """Flush remaining packets from the encoder.

        Call this at the end of a stream.
        """
        packets: list[EncodedPacket] = []
        if self._stream is not None:
            for packet in self._stream.encode(None):
                packets.append(self._packet_from_av(packet))
        return packets

    def request_keyframe(self) -> None:
        """Force the next frame to be a keyframe."""
        self._force_next_keyframe = True

    def release(self) -> None:
        """Release encoder resources."""
        with self._lock:
            if self._container is not None:
                self._container.close()
                self._container = None
                self._stream = None
                self._initialised = False
            logger.info("Video encoder released")

    # ── internal ────────────────────────────────────────────────────

    def _ensure_initialised(self, width: int, height: int) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return

            self._container = av.open(
                "pipe:", mode="w", format="h264",  # no file, just in-memory
            )
            self._stream = self._container.add_stream("h264", rate=self._config.fps)
            self._stream.width = width
            self._stream.height = height
            self._stream.pix_fmt = "yuv420p"
            self._stream.bit_rate = self._actual_bitrate
            self._stream.gop_size = self._config.gop_size
            self._stream.max_b_frames = 0  # lower latency
            self._stream.options = {
                "preset": "ultrafast",  # low latency
                "tune": "zerolatency",
                "profile": "baseline",
            }
            self._config.width = width
            self._config.height = height
            self._initialised = True
            logger.info(
                "Encoder initialised: %dx%d @ %.1f fps, %d bps",
                width, height, self._config.fps, self._actual_bitrate,
            )

    def _reinitialise(self) -> None:
        """Re-create the encoder with new settings."""
        if self._container is not None:
            self._container.close()
        self._initialised = False
        self._pts = 0
        self._ensure_initialised(self._config.width, self._config.height)

    def _rgb_to_yuv(self, rgb: np.ndarray) -> np.ndarray:
        """Convert RGB (H, W, 3) uint8 to YUV420P planar.

        Uses OpenCV for fast conversion.
        """
        import cv2

        # OpenCV uses BGR internally, but cvtColor handles RGB→YUV
        yuv = cv2.cvtColor(rgb, cv2.COLOR_RGB2YUV)
        # YUV420 planar: full Y, quarter U, quarter V
        h, w = yuv.shape[:2]
        y = yuv[:, :, 0]
        u = yuv[::2, ::2, 1]
        v = yuv[::2, ::2, 2]
        # Pack planes contiguously
        return np.ascontiguousarray(np.concatenate([y.ravel(), u.ravel(), v.ravel()]))

    def _make_av_frame(self, yuv_planes: np.ndarray) -> Any:  # noqa: ANN401
        """Build an av.VideoFrame from a YUV420P byte array."""
        h, w = self._config.height, self._config.width
        frame = av.VideoFrame(w, h, "yuv420p")
        y_size = w * h
        u_size = (w // 2) * (h // 2)
        frame.planes[0].update(yuv_planes[:y_size])
        frame.planes[1].update(yuv_planes[y_size : y_size + u_size])
        frame.planes[2].update(yuv_planes[y_size + u_size :])
        frame.pts = self._pts
        self._pts += 1
        return frame

    def _packet_from_av(self, packet: Any) -> EncodedPacket:  # noqa: ANN401
        """Convert an av.Packet to an EncodedPacket."""
        return EncodedPacket(
            data=bytes(packet),
            pts=packet.pts or 0,
            is_keyframe=packet.is_keyframe,
            width=self._config.width,
            height=self._config.height,
        )

    def __enter__(self) -> VideoEncoder:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


class VideoDecoder:
    """H.264 decoder wrapping PyAV.

    Buffers pre-keyframe data (SPS/PPS extradata) until a keyframe
    arrives, then feeds the complete bitstream to the decoder.
    Recovers from decoder errors by recreating the codec context.

    Usage::

        dec = VideoDecoder()
        frame = dec.decode(packet.data, packet.width, packet.height, is_keyframe)
    """

    def __init__(self) -> None:
        self._codec: Any = None  # noqa: ANN401
        self._lock = RLock()  # reentrant — reset() is called from within decode()
        # Track decoder initialisation state
        self._needs_keyframe = True
        # Buffer for SPS/PPS / extradata received before the first keyframe
        self._buffer = b""

    def decode(
        self,
        data: bytes,
        width: int,
        height: int,
        is_keyframe: bool = False,
    ) -> np.ndarray | None:
        """Decode an H.264 packet into an RGB numpy array.

        Uses a persistent ``CodecContext`` to maintain SPS/PPS state
        across frames.  Pre-keyframe data (SPS/PPS extradata) is
        buffered and prepended to the first keyframe.

        Parameters
        ----------
        data : bytes
            Raw H.264 packet data (Annex B byte-stream).
        width, height : int
            Frame dimensions (for array allocation).
        is_keyframe : bool
            Whether the packet is a keyframe (IDR).  Used to decide
            whether to attempt decode or buffer.

        Returns
        -------
        np.ndarray or None
            RGB uint8 (H, W, 3) or ``None`` if not enough data yet.
        """
        with self._lock:
            # ── Determine the payload to decode ──
            if self._needs_keyframe:
                if is_keyframe:
                    # Explicit keyframe — prepend any buffered extradata
                    payload = self._buffer + data if self._buffer else data
                    self._buffer = b""
                else:
                    # Not (yet) a keyframe — buffer the data but do NOT
                    # attempt decode yet.  Previously we tried to decode
                    # the incomplete buffer anyway, which always failed
                    # and caused reset() — discarding the accumulated
                    # extradata.
                    self._buffer += data
                    return None
            else:
                payload = data

            # ── Persistent CodecContext ──
            # Once initialised with a keyframe, the same context is
            # reused for all subsequent frames so that SPS/PPS state
            # (extradata) is preserved.
            try:
                if self._codec is None:
                    self._codec = av.CodecContext.create("h264", "r")

                av_packet = av.Packet(payload)
                frames = self._codec.decode(av_packet)
                if not frames:
                    return None

                # Success — mark decoder as initialised
                self._needs_keyframe = False
                self._buffer = b""
                return frames[-1].to_rgb().to_ndarray()

            except Exception as e:
                logger.error("VideoDecoder failed: %s", e)
                self.reset()
                return None

    def reset(self) -> None:
        """Reset decoder state — requires a fresh keyframe next."""
        with self._lock:
            self._codec = None
            self._needs_keyframe = True
            self._buffer = b""
            logger.debug("VideoDecoder reset — awaiting keyframe")

    def release(self) -> None:
        with self._lock:
            self._codec = None
            self._buffer = b""
            self._needs_keyframe = True
