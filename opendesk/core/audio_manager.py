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
    """Simple Opus encoder/decoder using PyAV.

    Encoder uses ``av.open()`` + ``add_stream()`` (same approach as
    ``VideoEncoder``).  Decoder uses ``CodecContext``.
    """

    def __init__(self, sample_rate: int = _SAMPLE_RATE, channels: int = _CHANNELS) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._encoder_container = None
        self._encoder_stream = None
        self._decoder = None

    # ── encoder (stream-based API) ─────────────────────────────────

    def setup_encoder(self) -> None:
        """Create the Opus encoder via av.open + add_stream.

        Uses the same high-level approach as ``VideoEncoder`` to avoid
        PyAV compatibility issues with ``CodecContext.create()``.
        """
        self._encoder_container = av.open("pipe:", mode="w", format="opus")
        self._encoder_stream = self._encoder_container.add_stream(
            "libopus", rate=self._sample_rate
        )
        self._encoder_stream.bit_rate = 64000
        logger.info(
            "Opus encoder ready: %d Hz, %d ch",
            self._sample_rate, self._channels,
        )

    def encode(self, pcm: np.ndarray) -> bytes | None:
        """Encode PCM float32 array to Opus packet."""
        if self._encoder_stream is None:
            return None

        # Build an AudioFrame in fltp planar format
        frame = av.AudioFrame(
            format="fltp", layout="stereo", samples=len(pcm),
        )
        frame.sample_rate = self._sample_rate
        frame.pts = 0

        # Fill plane data using update() (the writable PyAV API)
        frame.planes[0].update(pcm[:, 0].tobytes())
        frame.planes[1].update(pcm[:, 1].tobytes())

        packets = self._encoder_stream.encode(frame)
        if packets:
            # Return the first (and usually only) packet
            return bytes(packets[0])

        # Flush residuals
        flush = self._encoder_stream.encode(None)
        if flush:
            return bytes(flush[0])
        return None

    # ── decoder (CodecContext-based) ───────────────────────────────

    def setup_decoder(self) -> None:
        """Create the Opus decoder via CodecContext."""
        self._decoder = av.CodecContext.create("libopus", "r")
        self._decoder.sample_rate = self._sample_rate
        self._decoder.layout = av.AudioLayout("stereo")
        logger.info(
            "Opus decoder ready: %d Hz, %d ch",
            self._sample_rate, self._channels,
        )

    def decode(self, data: bytes) -> np.ndarray | None:
        """Decode Opus packet to PCM float32 array (stereo, samples x 2)."""
        if self._decoder is None:
            return None

        packet = av.Packet(data)
        frames = self._decoder.decode(packet)
        if not frames:
            return None

        f = frames[0]
        fmt_name = f.format.name
        num_samples = f.samples
        num_channels = len(f.layout.channels)

        if fmt_name == "fltp" and len(f.planes) >= 2:
            # Planar float32: each plane has num_samples valid floats
            ch0 = np.frombuffer(bytes(f.planes[0]), dtype=np.float32)[:num_samples]
            ch1 = np.frombuffer(bytes(f.planes[1]), dtype=np.float32)[:num_samples]
            return np.column_stack([ch0, ch1])
        elif fmt_name == "s16" and len(f.planes) >= 1:
            # Interleaved s16: one plane with num_samples * num_channels int16 values
            total = num_samples * num_channels
            raw = np.frombuffer(bytes(f.planes[0]), dtype=np.int16)[:total]
            samples = raw.reshape(-1, num_channels).astype(np.float32) / 32768.0
            return samples
        else:
            logger.warning(
                "Unhandled decoder output format: %s, planes=%d, num_channels=%d",
                fmt_name, len(f.planes), num_channels,
            )
            return None

    # ── cleanup ────────────────────────────────────────────────────

    def release(self) -> None:
        """Release encoder container and decoder."""
        if self._encoder_container:
            try:
                self._encoder_container.close()
            except Exception:
                pass
            self._encoder_container = None
            self._encoder_stream = None
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
        # Workaround: ctypes.util.find_library on Windows searches PATH but
        # C:\Windows\System32 is often absent from PATH in some shells (e.g.
        # Git Bash). cffi (used by soundcard) calls find_library to locate
        # ole32.dll, so we ensure System32 is on PATH before importing.
        import platform as _platform
        if _platform.system() == "Windows":
            import os as _os
            _system32 = r"C:\Windows\System32"
            if _os.path.isdir(_system32) and _system32 not in _os.environ["PATH"]:
                _os.environ["PATH"] += _os.pathsep + _system32

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
