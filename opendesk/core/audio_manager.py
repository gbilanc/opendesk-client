"""
Audio capture and playback for remote desktop.

Captures system audio output (what you hear) and microphone input,
encodes with Opus, and streams to the remote peer.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

import av
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SAMPLE_RATE = 48000
_CHANNELS = 2
_FRAME_DURATION_MS = 20  # Opus frame (20, 40, 60 ms)
_FRAME_SIZE = int(_SAMPLE_RATE * _FRAME_DURATION_MS / 1000)
_OPUS_APPLICATION = 2049  # OPUS_APPLICATION_AUDIO


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AudioDirection(Enum):
    """Which audio direction is active."""

    NONE = auto()
    OUTPUT_ONLY = auto()  # hear remote computer
    MIC_ONLY = auto()  # speak to remote
    BOTH = auto()


@dataclass
class AudioConfig:
    """Audio streaming configuration."""

    sample_rate: int = _SAMPLE_RATE
    channels: int = _CHANNELS
    frame_duration_ms: int = _FRAME_DURATION_MS
    bitrate: int = 64000  # Opus bitrate
    enabled: bool = False


# ---------------------------------------------------------------------------
# Opus wrapper (using PyAV's Opus codec)
# ---------------------------------------------------------------------------


class OpusCodec:
    """Simple Opus encoder/decoder using PyAV."""

    def __init__(self, sample_rate: int = _SAMPLE_RATE, channels: int = _CHANNELS) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._encoder = None
        self._decoder = None

    def setup_encoder(self) -> None:
        self._encoder = av.CodecContext.create("libopus", "w")
        self._encoder.sample_rate = self._sample_rate
        self._encoder.channels = self._channels
        self._encoder.frame_size = _FRAME_SIZE
        self._encoder.bit_rate = 64000
        logger.info("Opus encoder ready")

    def setup_decoder(self) -> None:
        self._decoder = av.CodecContext.create("libopus", "r")
        self._decoder.sample_rate = self._sample_rate
        self._decoder.channels = self._channels
        logger.info("Opus decoder ready")

    def encode(self, pcm: np.ndarray) -> bytes | None:
        """Encode PCM float32 array to Opus packet."""
        if self._encoder is None:
            return None
        frame = av.AudioFrame(format="fltp", layout="stereo", samples=len(pcm))
        frame.sample_rate = self._sample_rate
        frame.planes[0] = pcm[:, 0].tobytes()
        frame.planes[1] = pcm[:, 1].tobytes()
        frame.pts = 0

        packets = self._encoder.encode(frame)
        if packets:
            return bytes(packets[0])
        return None

    def decode(self, data: bytes) -> np.ndarray | None:
        """Decode Opus packet to PCM float32."""
        if self._decoder is None:
            return None
        packet = av.Packet(data)
        frames = self._decoder.decode(packet)
        if not frames:
            return None

        f = frames[0]
        # Convert planes to numpy array
        ch0 = np.frombuffer(f.planes[0], dtype=np.float32)
        ch1 = np.frombuffer(f.planes[1], dtype=np.float32)
        return np.column_stack([ch0, ch1])

    def release(self) -> None:
        self._encoder = None
        self._decoder = None


# ---------------------------------------------------------------------------
# Audio manager
# ---------------------------------------------------------------------------


class AudioManager:
    """Manages audio capture and playback.

    Uses platform-specific backends for audio capture (loopback)
    and playback.  Falls back to a stub on unsupported platforms.
    """

    def __init__(self, config: AudioConfig | None = None) -> None:
        self._config = config or AudioConfig()
        self._opus = OpusCodec(
            self._config.sample_rate, self._config.channels
        )
        self._direction = AudioDirection.NONE
        self._send_fn: Callable | None = None
        self._capture_task: asyncio.Task | None = None
        self._playback_stream = None

    # ── properties ──────────────────────────────────────────────────

    @property
    def config(self) -> AudioConfig:
        return self._config

    @property
    def direction(self) -> AudioDirection:
        return self._direction

    @direction.setter
    def direction(self, value: AudioDirection) -> None:
        self._direction = value
        logger.info("Audio direction set to %s", value.name)

    # ── lifecycle ───────────────────────────────────────────────────

    async def start(self, send_fn: Callable) -> None:
        """Start audio streaming.

        Parameters
        ----------
        send_fn : async callable
            Function to send audio frames to the remote peer.
        """
        self._send_fn = send_fn
        self._config.enabled = True

        if self._direction in (AudioDirection.OUTPUT_ONLY, AudioDirection.BOTH):
            self._opus.setup_encoder()
            self._capture_task = asyncio.create_task(self._capture_loop())

        if self._direction in (AudioDirection.OUTPUT_ONLY, AudioDirection.NONE):
            self._opus.setup_decoder()

        logger.info("Audio manager started")

    async def stop(self) -> None:
        """Stop audio streaming."""
        self._config.enabled = False

        if self._capture_task is not None:
            self._capture_task.cancel()
            self._capture_task = None

        self._opus.release()
        logger.info("Audio manager stopped")

    # ── audio capture loop ──────────────────────────────────────────

    async def _capture_loop(self) -> None:
        """Continuously capture system audio and send to remote."""
        try:
            import soundcard as sc  # optional dependency
        except ImportError:
            logger.warning(
                "soundcard not installed — audio capture disabled. "
                "Install with: pip install soundcard"
            )
            return

        try:
            mic = sc.default_microphone()
            logger.info("Audio capture device: %s", mic.name)

            with mic.recorder(samplerate=self._config.sample_rate, channels=self._config.channels) as mic_rec:
                while self._config.enabled:
                    # Capture a frame
                    pcm_data = mic_rec.record(numframes=_FRAME_SIZE)
                    if pcm_data is None or len(pcm_data) == 0:
                        await asyncio.sleep(_FRAME_DURATION_MS / 1000)
                        continue

                    # Convert to mono if needed, ensure float32
                    if len(pcm_data.shape) == 1:
                        pcm_data = pcm_data.reshape(-1, 1)

                    pcm_float = pcm_data.astype(np.float32)

                    # Opus encode
                    encoded = self._opus.encode(pcm_float)
                    if encoded and self._send_fn:
                        from opendesk.network.protocol import Message, MessageType
                        msg = Message(
                            MessageType.AUDIO_FRAME,
                            {"data": encoded, "pts": int(time.time() * 1000)},
                        )
                        await self._send_fn(msg)

                    await asyncio.sleep(_FRAME_DURATION_MS / 1000)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Audio capture error: %s", e)

    # ── playback ────────────────────────────────────────────────────

    async def play_audio_frame(self, data: bytes) -> None:
        """Play a received audio frame."""
        if not self._config.enabled:
            return

        try:
            import soundcard as sc
        except ImportError:
            return

        pcm = self._opus.decode(data)
        if pcm is None:
            return

        try:
            speaker = sc.default_speaker()
            speaker.play(pcm, samplerate=self._config.sample_rate, channels=self._config.channels)
        except Exception as e:
            logger.warning("Audio playback error: %s", e)

    # ── cleanup ─────────────────────────────────────────────────────

    def release(self) -> None:
        """Release resources."""
        if self._capture_task and not self._capture_task.done():
            self._capture_task.cancel()
