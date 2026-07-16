"""
Video encoding and decoding using PyAV (FFmpeg bindings).

Provides H.264 / H.265 / HW-accelerated encoding with CRF or bitrate
rate control, adaptive quality, and delta-frame support.
"""

from __future__ import annotations

import logging
import subprocess  # noqa: S404 — only used for encoder probing
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from threading import Lock, RLock

import av
import numpy as np

from opendesk.core.screen_capture import CapturedFrame

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hardware encoder detection
# ---------------------------------------------------------------------------


def _candidates(prefer_hw: bool = True) -> list[str]:
    """Ordered list of encoder candidates to try.

    HW encoders are listed first; SW encoders are always available
    as fallback.  The caller tries each candidate with
    ``av.Container.add_stream()`` and catches errors.
    """
    hw = [
        "hevc_nvenc",
        "h264_nvenc",
        "hevc_amf",
        "h264_amf",
        "hevc_vaapi",
        "h264_vaapi",
        "hevc_qsv",
        "h264_qsv",
        "hevc_videotoolbox",
        "h264_videotoolbox",
    ]
    sw = ["hevc", "h264"]
    if prefer_hw:
        return hw + sw
    return sw


def _try_open_codec(name: str, fps: int = 30) -> bool:
    """Try to open *name* with actual encoding parameters.

    Creates a tiny in-memory stream with the given encoder and
    real parameters (width, height, pix_fmt).  If this succeeds
    the encoder is usable on this system.
    """
    try:
        container = av.open("pipe:", mode="w", format="h264" if "264" in name else "hevc")
        stream = container.add_stream(name, rate=fps)
        stream.width = 32
        stream.height = 32
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "veryfast"}
        # Try a dummy frame
        import numpy as np
        frame = av.VideoFrame.from_ndarray(
            np.zeros((32, 32, 3), dtype=np.uint8), format="rgb24",
        )
        list(stream.encode(frame))
        container.close()
        return True
    except Exception as e:
        logger.debug("Codec %s not available: %s", name, e)
        return False


# ---------------------------------------------------------------------------
# Quality presets
# ---------------------------------------------------------------------------


class QualityLevel(Enum):
    """Predefined quality / bandwidth trade-off levels.

    Higher quality = lower CRF value (0-51 scale where 0=lossless).
    For remote desktop with text, SHARP (CRF 18) is the sweet spot
    between readability and bandwidth.
    """

    LOW = auto()  # CRF 32 / ~0.5 Mbps — good for slow connections
    MEDIUM = auto()  # CRF 27 / ~2 Mbps — balanced
    HIGH = auto()  # CRF 23 / ~8 Mbps — good image quality
    SHARP = auto()  # CRF 18 / ~15 Mbps — sharp text, great for reading
    LOSSLESS = auto()  # CRF 16 / ~20+ Mbps — near-lossless (LAN)


# CRF values per quality level (used when crf_mode is True)
# Lower CRF = better quality = larger bitstream.
# For text readability, CRF 18 (SHARP) is recommended.
_QUALITY_CRF: dict[QualityLevel, int] = {
    QualityLevel.LOW: 32,
    QualityLevel.MEDIUM: 27,
    QualityLevel.HIGH: 23,
    QualityLevel.SHARP: 18,
    QualityLevel.LOSSLESS: 16,
}

# Bitrate values (used when crf_mode is False)
_QUALITY_BITRATE: dict[QualityLevel, int] = {
    QualityLevel.LOW: 500_000,
    QualityLevel.MEDIUM: 2_000_000,
    QualityLevel.HIGH: 8_000_000,
    QualityLevel.SHARP: 15_000_000,
    QualityLevel.LOSSLESS: 20_000_000,
}


@dataclass
class EncoderConfig:
    """Configuration for the video encoder.

    Supports H.264, H.265/HEVC, and hardware-accelerated encoders.
    Rate control can use CRF (constant quality) or bitrate mode.

    CRF mode (recommended):
        Sets ``crf`` to a value 0-51 (lower = better quality).
        Typical range: 18 (visually lossless) to 28 (good).
        The encoder dynamically allocates bits where needed.
        Much more efficient than fixed bitrate for varying content.

    Bitrate mode:
        Sets ``bitrate`` to a target bps.  The encoder will try to
        stay close to this bitrate, potentially sacrificing quality
        on complex scenes and wasting bits on simple ones.

    Pixel format:
        ``yuv420p`` — standard, widely compatible (chroma subsampled).
        ``yuv444p`` — full chroma resolution, much sharper text, but
        requires more bandwidth (~30% more).  Not all encoders support
        it; falls back to yuv420p automatically.
    """

    width: int
    height: int
    fps: float = 30.0
    bitrate: int = 2_000_000  # bps — used only when crf is None
    quality: QualityLevel = QualityLevel.MEDIUM
    codec: str = ""  # auto-detect if empty
    crf: int | None = None  # None = use bitrate, int = CRF mode
    gop_size: int = 60  # keyframe interval (in frames)
    pixel_format: str = "yuv420p"  # "yuv420p" or "yuv444p"
    options: dict[str, str] = field(default_factory=lambda: {
        "preset": "veryfast",
    })


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
    """Video encoder wrapping PyAV with support for H.264, H.265, HW.

    Usage::

        enc = VideoEncoder(EncoderConfig(width=1920, height=1080, crf=23))
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
        self._actual_crf: int | None = self._config.crf
        self._codec_name: str = ""
        self._force_next_keyframe: bool = False

    # ── properties ──────────────────────────────────────────────────

    @property
    def config(self) -> EncoderConfig:
        return self._config

    @property
    def codec_name(self) -> str:
        """The resolved codec name (e.g. ``hevc_nvenc``, ``h264``)."""
        return self._codec_name

    @property
    def actual_bitrate(self) -> int:
        return self._actual_bitrate

    @actual_bitrate.setter
    def actual_bitrate(self, value: int) -> None:
        """Dynamically adjust bitrate (rounded to reasonable bounds).

        Only effective when NOT using CRF mode.
        """
        self._actual_bitrate = max(100_000, min(50_000_000, value))
        if self._initialised and self._actual_crf is None:
            self._reinitialise()
        logger.debug("Bitrate adjusted to %d bps", self._actual_bitrate)

    def set_quality(self, level: QualityLevel) -> None:
        """Set a predefined quality level.

        In CRF mode (default) this sets the CRF value.
        In bitrate mode this sets the target bitrate.
        """
        self._config.quality = level
        if self._actual_crf is not None:
            self._actual_crf = _QUALITY_CRF[level]
            if self._initialised:
                self._reinitialise()
        else:
            self.actual_bitrate = _QUALITY_BITRATE[level]

    def set_encoder_preset(self, preset: str) -> None:
        """Change encoder speed/quality preset.

        Valid presets depend on the encoder:
        - SW x264/x265: ultrafast, veryfast, faster, fast, medium, slow, veryslow
        - NVENC: p1 (fastest) through p7 (slowest)
        - VAAPI: fast, medium, slow
        """
        self._config.options = {
            **self._config.options,
            "preset": preset,
        }
        if self._initialised:
            self._reinitialise()

    # ── encoding ────────────────────────────────────────────────────

    def encode(self, frame: np.ndarray) -> list[EncodedPacket]:
        """Encode an RGB (H, W, 3) numpy array into H.264/H.265 packets.

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
        force_keyframe = self._pts == 0 or self._force_next_keyframe
        self._force_next_keyframe = False

        # Create a VideoFrame from RGB — PyAV handles the RGB→YUV
        # conversion with the correct color matrices (ITU-R BT.601),
        # avoiding the quality loss from manual OpenCV conversion.
        av_frame = av.VideoFrame.from_ndarray(frame, format="rgb24")
        av_frame.pts = self._pts
        self._pts += 1

        if force_keyframe:
            av_frame.pict_type = 1  # PictureType.I (IDR / keyframe)

        raw_packets = list(self._stream.encode(av_frame))
        if not raw_packets:
            return []

        # PyAV may emit multiple packets for one input frame
        # (e.g. SPS/PPS + IDR on the first keyframe).
        # Combine them into one so the receiver never sees
        # a partial / undecodable payload.
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
            logger.info(
                "Video encoder (%s) released",
                self._codec_name or self._config.codec or "h264",
            )

    # ── internal ────────────────────────────────────────────────────

    def _choose_codec(self, codec_hint: str = "") -> str:
        """Resolve the best codec name.

        Tries candidates in order and returns the first one that
        opens successfully with ``av.Container.add_stream()``.
        This is the only reliable detection method.

        Priority:
        1. Explicit ``codec`` from config (if try-open succeeds)
        2. HW encoders in preference order
        3. Software H.264 as final fallback (HEVC SW is skipped —
           overkill for remote desktop, worse with small frames)
        """
        if codec_hint:
            hint = codec_hint.lower().strip()
            if hint in ("hevc", "h264"):
                # User explicitly requested HEVC or H.264
                return hint
            if _try_open_codec(hint):
                return hint
            logger.warning("Requested codec '%s' unavailable, auto-detecting", hint)

        # 1. HW encoders (try-open each)
        for name in _candidates(prefer_hw=True):
            if name in ("hevc", "h264"):
                continue  # skip SW in HW pass
            if _try_open_codec(name):
                logger.info("Using HW encoder: %s", name)
                return name

        # 2. SW H.264 (always available, best compatibility)
        return "h264"

    def _ensure_initialised(self, width: int, height: int) -> None:
        if self._initialised:
            return
        with self._lock:
            if self._initialised:
                return

            codec = self._choose_codec(self._config.codec)
            self._codec_name = codec

            # Determine output format name based on codec
            fmt = "h264" if "h264" in codec else "hevc"

            crf = self._actual_crf if self._actual_crf is not None else self._config.crf
            use_crf = crf is not None
            if use_crf:
                # Read the config-level CRF if not set dynamically
                if self._actual_crf is None:
                    self._actual_crf = crf
                self._actual_crf = max(0, min(51, self._actual_crf))

            is_hevc = "hevc" in codec
            profile = "main" if is_hevc else "baseline"

            self._container = av.open("pipe:", mode="w", format=fmt)
            self._stream = self._container.add_stream(codec, rate=self._config.fps)
            self._stream.width = width
            self._stream.height = height

            # ── Pixel format: prefer yuv444p for sharp text, fallback to yuv420p ──
            requested_pix_fmt = self._config.pixel_format
            try:
                self._stream.pix_fmt = requested_pix_fmt
            except Exception:
                logger.info(
                    "Pixel format %s not supported by %s, falling back to yuv420p",
                    requested_pix_fmt, codec,
                )
                self._stream.pix_fmt = "yuv420p"

            # ── Options base ──
            opts = dict(self._config.options)

            # Adjust preset based on quality level
            quality = self._config.quality
            if quality == QualityLevel.LOSSLESS:
                opts["preset"] = opts.get("preset", "medium")
            elif quality == QualityLevel.SHARP or quality == QualityLevel.HIGH:
                opts["preset"] = opts.get("preset", "fast")
            else:
                opts["preset"] = opts.get("preset", "veryfast")

            # Low-latency tuning
            if "h264" in codec and codec not in ("h264_nvenc",):
                opts["tune"] = "zerolatency"
                opts["profile"] = "baseline"
            elif is_hevc and codec in ("hevc",):
                opts["tune"] = "zerolatency"
            if codec in ("h264_nvenc", "hevc_nvenc"):
                opts["rc"] = "vbr"
                opts["cq"] = str(self._actual_crf or 23)
            if codec in ("h264_vaapi", "hevc_vaapi"):
                opts["rc_mode"] = "CQP"

            if use_crf:
                # CRF mode: constant quality, variable bitrate.
                # Non impostare bit_rate (PyAV vuole un int, None fallisce).
                # Il CRF nel dict options basta per libx264/libx265.
                self._stream.options = opts
                self._stream.options["crf"] = str(self._actual_crf)
                if is_hevc and codec in ("hevc",):
                    # libx265 uses x265-params for CRF
                    self._stream.options["x265-params"] = f"crf={self._actual_crf}"
            else:
                # Bitrate mode
                self._stream.bit_rate = self._actual_bitrate
                self._stream.options = opts

            self._stream.gop_size = self._config.gop_size
            self._stream.max_b_frames = 0  # lower latency

            self._config.width = width
            self._config.height = height
            self._initialised = True
            logger.info(
                "Encoder initialised: %s  %dx%d @ %.1f fps  pix_fmt=%s%s",
                codec, width, height, self._config.fps,
                self._stream.pix_fmt,
                f", crf={self._actual_crf}" if use_crf else
                f", bitrate={self._actual_bitrate:,} bps",
            )

    def _reinitialise(self) -> None:
        """Re-create the encoder with new settings."""
        if self._container is not None:
            self._container.close()
        self._initialised = False
        self._pts = 0
        self._ensure_initialised(self._config.width, self._config.height)

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

    # ── static helpers ──────────────────────────────────────────────

    @staticmethod
    def available_hw_encoders() -> list[str]:
        """Return list of available hardware encoder names (try-open)."""
        available = []
        for name in _candidates(prefer_hw=True):
            if name in ("hevc", "h264"):
                continue
            if _try_open_codec(name):
                available.append(name)
        return available

    @staticmethod
    def default_codec(prefer_hw: bool = True) -> str:
        """Return the best available encoder name (try-open).

        Prefers H.264 over SW HEVC for remote desktop (better
        compatibility, lower latency, better with small frames).
        HW HEVC encoders are preferred over SW H.264.
        """
        # 1. HW encoders (try-open each)
        for name in _candidates(prefer_hw=True):
            if name in ("hevc", "h264"):
                continue  # skip SW in HW pass
            if _try_open_codec(name):
                return name
        # 2. SW H.264 (always available, best compatibility)
        return "h264"


# ---------------------------------------------------------------------------
# Decoder — auto-detects codec from stream
# ---------------------------------------------------------------------------


class VideoDecoder:
    """H.264 / H.265 decoder wrapping PyAV.

    Auto-detects the codec from the packet stream (H.264 start code
    0x000001 or AVCC extradata); falls back to H.264 if detection
    fails.

    Buffers pre-keyframe data (SPS/PPS extradata) until a keyframe
    arrives, then feeds the complete bitstream to the decoder.
    Recovers from decoder errors by recreating the codec context.

    Usage::

        dec = VideoDecoder()
        frame = dec.decode(packet.data, packet.width, packet.height, is_keyframe)
    """

    def __init__(self, codec: str = "h264") -> None:
        """
        Parameters
        ----------
        codec : str
            Expected codec name (``"h264"``, ``"hevc"``).  If known
            ahead of time (e.g. from the encoder config), pass it
            here to avoid NAL-unit heuristics.
        """
        self._codec: Any = None  # noqa: ANN401
        self._codec_requested: str = codec
        self._codec_name: str = codec
        self._lock = RLock()
        self._needs_keyframe = True
        self._buffer = b""

    @property
    def codec_name(self) -> str:
        """The resolved codec name (e.g. ``h264``, ``hevc``)."""
        return self._codec_name

    def decode(
        self,
        data: bytes,
        width: int,
        height: int,
        is_keyframe: bool = False,
    ) -> np.ndarray | None:
        """Decode a video packet into an RGB numpy array.

        Uses a persistent ``CodecContext`` to maintain SPS/PPS state
        across frames.  Pre-keyframe data (SPS/PPS extradata) is
        buffered and prepended to the first keyframe.

        Parameters
        ----------
        data : bytes
            Raw video packet data (Annex B byte-stream or AVCC).
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
                    payload = self._buffer + data if self._buffer else data
                    self._buffer = b""
                else:
                    self._buffer += data
                    return None
            else:
                payload = data

            try:
                if self._codec is None:
                    # Use requested codec if set, otherwise auto-detect
                    # from NAL unit header.
                    codec_to_use = self._codec_requested
                    if not codec_to_use or codec_to_use not in ("h264", "hevc"):
                        codec_to_use = self._detect_codec(
                            payload if is_keyframe
                            else self._buffer[:16] + data[:16],
                        )
                        self._codec_requested = codec_to_use
                    self._codec_name = codec_to_use
                    self._codec = av.CodecContext.create(codec_to_use, "r")

                av_packet = av.Packet(payload)
                frames = self._codec.decode(av_packet)
                if not frames:
                    return None

                self._needs_keyframe = False
                self._buffer = b""
                return frames[-1].to_rgb().to_ndarray()

            except Exception as e:
                logger.error("VideoDecoder (%s) failed: %s", self._codec_name, e)
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
        """Release decoder resources."""
        with self._lock:
            self._codec = None
            self._buffer = b""
            self._needs_keyframe = True

    @staticmethod
    def _detect_codec(data: bytes) -> str:
        """Detect H.264 vs H.265 from a keyframe packet.

        Uses NAL unit type heuristics:
        - H.265 VPS (type 32 when shifted) has first byte 0x40.
          ``(0x40 >> 1) & 0x3F == 32``, ``0x40 & 0x1F == 0`` (H.264 type 0 = unspecified).
        - H.264 SPS (type 7) has first byte 0x67.
          ``0x67 & 0x1F == 7``, ``(0x67 >> 1) & 0x3F == 51`` (H.265 reserved).

        Conservative approach: only return ``"hevc"`` if we see VPS (type 32)
        or an IRAP slice (types 16-21).  Everything else is assumed H.264.
        """
        if len(data) < 5:
            return "h264"

        offset = 0
        if data[0] == 0 and data[1] == 0:
            if data[2] == 1:
                offset = 3
            elif len(data) > 3 and data[2] == 0 and data[3] == 1:
                offset = 4

        if offset == 0:
            return "h264"  # AVCC or unknown format

        nal_byte = data[offset]
        nal_h265 = (nal_byte >> 1) & 0x3F

        # VPS (type 32) is uniquely H.265 — H.264 has no equivalent.
        if nal_h265 == 32:
            logger.debug("Detected H.265 (VPS)")
            return "hevc"

        # IRAP slices (16-21) are H.265.  When interpreted as H.264
        # these become types 8-10 (PPS / AUD / End-of-Seq) which are
        # non-VCL and never appear as the FIRST NAL in a keyframe,
        # so this is safe.
        if 16 <= nal_h265 <= 21:
            logger.debug("Detected H.265 (IRAP type %d)", nal_h265)
            return "hevc"

        # Everything else → H.264 (conservative fallback)
        return "h264"
