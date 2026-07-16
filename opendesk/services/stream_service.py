"""Servizio di streaming — capture, encode e invio frame.

Gestisce:
- ScreenCapture (PipeWire / MSS)
- VideoEncoder (H.264)
- InputBackend (remote input injection)
- Stream timer e bandwidth adaptation
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QSettings, QTimer, Signal, Slot

from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.core.video_codec import VideoEncoder, EncoderConfig, QualityLevel
from opendesk.core.input_injection import (
    InputBackend,
    MouseButton,
    KeyState,
    create_input_backend,
)
from opendesk.services.pipeline import (
    StreamingPipeline,
    PipelineConfig,
)
import cv2
import numpy as np

from opendesk.network.protocol import Message, MessageType
from opendesk.network.relay_client import RelayClient

logger = logging.getLogger(__name__)

# Bitrate defaults per quality level (mirrors video_codec._QUALITY_BITRATE)
_DEFAULT_BITRATES = {
    QualityLevel.LOW: 500_000,
    QualityLevel.MEDIUM: 2_000_000,
    QualityLevel.HIGH: 8_000_000,
    QualityLevel.LOSSLESS: 20_000_000,
}

# ═══════════════════════════════════════════════════════════════════
# Tile grid constants
# ═══════════════════════════════════════════════════════════════════

_TILE_SIZE = 128  # tile width/height in pixels (64 → 128 = 75% fewer tiles)
_TILE_THRESHOLD = 16  # pixel difference threshold for change detection
_TILE_MAX_CHANGED_RATIO = 0.30  # if more tiles changed, send full frame
_KEYFRAME_INTERVAL = 60  # send full keyframe every N frames (every ~2s at 30fps)
# JPEG quality per quality preset.
# JPEG is lossy but ~10× faster to encode than PNG and produces
# far smaller payloads for photographic / gradient-heavy content.
_TILE_JPEG_QUALITY: dict[QualityLevel, int] = {
    QualityLevel.LOW: 50,
    QualityLevel.MEDIUM: 65,
    QualityLevel.HIGH: 80,
    QualityLevel.LOSSLESS: 95,
}


# ═══════════════════════════════════════════════════════════════════
# StreamService
# ═══════════════════════════════════════════════════════════════════


class StreamService(QObject):
    """Servizio di streaming video con pipeline multi-thread.

    Usa ``StreamingPipeline`` (3 thread: capture, encode, network)
    per mantenere la UI fluida e massimizzare il frame rate.
    Periodicamente invia un full keyframe H.264 per risincronizzazione.

    Signals::

        frame_ready(rgb: np.ndarray, width: int, height: int)
        bitrate_changed(kbps: float)
        error(error_msg: str)
    """

    frame_ready = Signal(object, int, int)  # np.ndarray, width, height
    bitrate_changed = Signal(float)
    input_unavailable = Signal(str)  # non-fatal: input backend could not start
    error = Signal(str)

    def __init__(self, relay: RelayClient, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._relay = relay
        self._settings = QSettings("OpenDesk", "OpenDesk")

        # Capture (solo per input backend, non per streaming)
        self._input_backend: InputBackend | None = None

        # Pipeline multi-thread
        self._pipeline: StreamingPipeline | None = None

        # Timer per bandwidth adaptation (solo lettura contatori)
        self._bw_timer = QTimer(self)
        self._bw_timer.timeout.connect(self._update_bitrate)

        # Bandwidth estimation
        self._bw_measure_bytes: int = 0
        self._bw_measure_time: float = 0.0
        self._bw_estimated_kbps: float = 0.0

        # React when the remote peer requests a keyframe
        self._relay.host_keyframe_requested.connect(self._force_keyframe)

    # ── properties ──────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._pipeline is not None

    @property
    def input_backend(self) -> InputBackend | None:
        return self._input_backend

    @property
    def capture(self):
        return None  # pipeline gestisce la cattura internamente

    # ── streaming lifecycle ─────────────────────────────────────────

    def start_streaming(self) -> None:
        """Avvia la pipeline multi-thread (capture + encode + send)."""
        if self._pipeline is not None:
            logger.warning("Pipeline already running — ignoring start")
            return

        try:
            # Input backend (non bloccante)
            try:
                self._input_backend = create_input_backend()
            except Exception as e:
                logger.warning("Input backend unavailable — remote input disabled: %s", e)
                self._input_backend = None
                self.input_unavailable.emit(str(e))

            # Reset bandwidth
            self._bw_measure_bytes = 0
            self._bw_measure_time = time.time()
            self._bw_estimated_kbps = 0.0

            # Leggi impostazioni
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "HIGH")
            quality = getattr(QualityLevel, quality_name, QualityLevel.HIGH)
            resolution_scale = float(self._settings.value("video/resolution_scale", 1.0))

            # Crea configurazione della pipeline
            config = PipelineConfig(
                fps=fps,
                quality=quality,
                bitrate=_DEFAULT_BITRATES[quality],
                resolution_scale=resolution_scale,
                monitor_index=0,
            )

            # Callback per tracciare i byte inviati (bandwidth estimation)
            def _on_send(data: bytes) -> None:
                self._bw_measure_bytes += len(data)

            # Crea e avvia la pipeline
            self._pipeline = StreamingPipeline(config, self._relay.send_frame, self._relay.send_tile)
            self._pipeline.on_keyframe_sent = lambda data, w, h, pts, kf: _on_send(data)
            self._pipeline.on_tile_sent = lambda data, x, y, tw, th, pts: _on_send(data)
            self._pipeline.start()

            # Timer per bandwidth adaptation (1s)
            self._bw_timer.start(1000)

            logger.info(
                "Streaming started: pipeline 3-thread, %d FPS, %s, scale=%.2f",
                fps, quality_name, resolution_scale,
            )
        except Exception as e:
            logger.exception("Failed to start streaming: %s", e)
            self.error.emit(str(e))
            self.stop_streaming()

    def stop_streaming(self) -> None:
        """Ferma la pipeline e rilascia le risorse."""
        self._bw_timer.stop()
        if self._pipeline:
            self._pipeline.stop()
            self._pipeline = None
        if self._input_backend:
            self._input_backend.release()
            self._input_backend = None
        self._bw_measure_bytes = 0
        logger.info("Streaming stopped")

    @Slot()
    def _force_keyframe(self) -> None:
        """Force the next video frame to be a full keyframe (IDR).

        Called when the remote peer signals that it needs a fresh
        keyframe to re-sync the video decoder.
        """
        if self._pipeline:
            self._pipeline.request_keyframe()
            logger.info("Forcing full keyframe on next frame per peer request")

    # ── bandwidth adaptation ────────────────────────────────────────

    @Slot()
    def _update_bitrate(self) -> None:
        """Aggiorna il bitrate in base alla banda misurata.

        La pipeline usa i bitrate dalla config; per ora logghiamo
        la banda stimata.  In futuro si puo' aggiornare la config
        della pipeline a caldo.
        """
        now = time.time()
        elapsed = now - self._bw_measure_time
        if elapsed < 1.0 or self._bw_measure_bytes < 1024:
            return

        measured_kbps = (self._bw_measure_bytes * 8) / (elapsed * 1000)
        self._bw_measure_bytes = 0
        self._bw_measure_time = now

        if self._bw_estimated_kbps == 0:
            self._bw_estimated_kbps = measured_kbps
        else:
            self._bw_estimated_kbps = self._bw_estimated_kbps * 0.7 + measured_kbps * 0.3

        self.bitrate_changed.emit(self._bw_estimated_kbps)
        logger.debug("Estimated bandwidth: %.0f kbps", self._bw_estimated_kbps)

    # ── input injection ─────────────────────────────────────────────

    def inject_mouse(self, msg: Message) -> None:
        """Inietta un evento mouse (chiamato dal relay)."""
        if self._input_backend is None:
            return
        payload = msg.payload
        x = payload.get("x", 0)
        y = payload.get("y", 0)
        button = payload.get("button")
        pressed = payload.get("pressed")
        absolute = payload.get("absolute", True)

        if button:
            btn = MouseButton(button)
            state = KeyState.PRESSED if pressed else KeyState.RELEASED
            self._input_backend.move_mouse(x, y, absolute)
            self._input_backend.click_mouse(btn, state)
        else:
            self._input_backend.move_mouse(x, y, absolute)

    def inject_keyboard(self, msg: Message) -> None:
        """Inietta un evento tastiera (chiamato dal relay)."""
        if self._input_backend is None:
            return
        payload = msg.payload
        key = payload.get("key", "")
        pressed = payload.get("pressed", False)
        state = KeyState.PRESSED if pressed else KeyState.RELEASED
        if key:
            self._input_backend.key_event(key, state)
