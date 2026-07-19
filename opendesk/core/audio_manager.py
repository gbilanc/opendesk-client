"""
Audio capture and playback for remote desktop.

Captures microphone input, encodes with Opus, and streams to the remote peer.
On the receiving side, decodes Opus packets and plays them.
"""

from __future__ import annotations

import logging
import threading
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


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AudioDirection(Enum):
    """Which audio direction is active."""

    NONE = auto()
    OUTPUT_ONLY = auto()  # hear remote computer (system audio loopback)
    MIC_ONLY = auto()  # speak to remote (microphone)
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

    Uses ``soundcard`` for microphone capture and speaker playback.
    Falls back gracefully if the library is not installed.
    """

    def __init__(self, config: AudioConfig | None = None) -> None:
        self._config = config or AudioConfig()
        self._opus = OpusCodec(
            self._config.sample_rate, self._config.channels
        )
        self._direction = AudioDirection.NONE
        self._send_fn: Callable | None = None

        # Capture thread
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Playback state
        self._playback_device = None  # reusable speaker context
        self._playback_lock = threading.Lock()

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

    @property
    def is_capturing(self) -> bool:
        """Whether microphone capture is currently running."""
        return self._capture_thread is not None and self._capture_thread.is_alive()

    # ── lifecycle (capture) ─────────────────────────────────────────

    def start_capture(self, send_fn: Callable) -> None:
        """Start microphone capture in a background thread.

        Parameters
        ----------
        send_fn : callable
            Function to send audio frames to the remote peer.
            Called from the capture thread with a ``Message``.
        """
        if self.is_capturing:
            logger.warning("Audio capture already running")
            return

        self._send_fn = send_fn
        self._config.enabled = True
        self._opus.setup_encoder()
        self._stop_event.clear()

        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="AudioCapture",
        )
        self._capture_thread.start()
        logger.info("Audio capture started")

    def stop_capture(self) -> None:
        """Stop microphone capture."""
        self._config.enabled = False
        self._stop_event.set()

        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3.0)
            if self._capture_thread.is_alive():
                logger.warning("Audio capture thread did not stop cleanly")
            self._capture_thread = None

        self._opus.release()
        logger.info("Audio capture stopped")

    # ── audio capture loop (runs in thread) ─────────────────────────

    def _capture_loop(self) -> None:
        """Continuously capture microphone audio and send to remote."""
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

            with mic.recorder(
                samplerate=self._config.sample_rate,
                channels=self._config.channels,
            ) as mic_rec:
                while not self._stop_event.is_set():
                    try:
                        pcm_data = mic_rec.record(numframes=_FRAME_SIZE)
                    except Exception as e:
                        logger.warning("Audio record error: %s", e)
                        time.sleep(_FRAME_DURATION_MS / 1000)
                        continue

                    if pcm_data is None or len(pcm_data) == 0:
                        time.sleep(_FRAME_DURATION_MS / 1000)
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
                        try:
                            self._send_fn(msg)
                        except Exception as e:
                            logger.warning("Audio send error: %s", e)

                    time.sleep(_FRAME_DURATION_MS / 1000)

        except Exception as e:
            logger.error("Audio capture error: %s", e)
        finally:
            logger.info("Audio capture loop ended")

    # ── playback ────────────────────────────────────────────────────

    def play_audio_frame(self, data: bytes) -> None:
        """Play a received audio frame (Opus packet).

        Safe to call from any thread.
        """
        if not self._config.enabled and self._direction != AudioDirection.NONE:
            # Allow playback even if capture is off, as long as direction allows
            pass

        try:
            import soundcard as sc
        except ImportError:
            return

        # Ensure decoder is set up
        if self._opus._decoder is None:
            self._opus.setup_decoder()

        pcm = self._opus.decode(data)
        if pcm is None:
            return

        with self._playback_lock:
            try:
                if self._playback_device is None:
                    self._playback_device = sc.default_speaker()

                self._playback_device.play(
                    pcm,
                    samplerate=self._config.sample_rate,
                    channels=self._config.channels,
                )
            except Exception as e:
                logger.warning("Audio playback error: %s", e)
                self._playback_device = None  # reset on error

    # ── cleanup ─────────────────────────────────────────────────────

    def release(self) -> None:
        """Release all resources."""
        self.stop_capture()
        self._playback_device = None
        logger.info("Audio manager released")
