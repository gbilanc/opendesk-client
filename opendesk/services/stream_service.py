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

from opendesk.core.audio_manager import AudioManager, AudioConfig, AudioDirection
from opendesk.core.camera_manager import CameraManager, CameraConfig
from opendesk.core.platform_config import get_platform_config
from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.core.video_codec import VideoEncoder, EncoderConfig, QualityLevel, _QUALITY_CRF
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
    QualityLevel.SHARP: 15_000_000,
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
    QualityLevel.SHARP: 95,
    QualityLevel.LOSSLESS: 98,
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

        # Audio manager (microphone capture + playback)
        self._audio_manager = AudioManager(
            AudioConfig(enabled=False)
        )
        self._audio_enabled: bool = False

        # Camera manager (webcam capture)
        self._camera_manager = CameraManager(
            CameraConfig(enabled=False)
        )
        self._camera_enabled: bool = False

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
    def audio_manager(self) -> AudioManager:
        """The AudioManager instance (capture + playback)."""
        return self._audio_manager

    @property
    def camera_manager(self) -> CameraManager:
        """The CameraManager instance (webcam capture)."""
        return self._camera_manager

    @property
    def audio_enabled(self) -> bool:
        return self._audio_enabled

    @audio_enabled.setter
    def audio_enabled(self, value: bool) -> None:
        self._audio_enabled = value

    @property
    def camera_enabled(self) -> bool:
        return self._camera_enabled

    @camera_enabled.setter
    def camera_enabled(self, value: bool) -> None:
        self._camera_enabled = value

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

            # Imposta la risoluzione dello schermo sul backend di input
            # (necessaria per il corretto scaling delle coordinate ABS su Wayland)
            if self._input_backend is not None:
                try:
                    from PySide6.QtWidgets import QApplication
                    screen = QApplication.primaryScreen()
                    if screen is not None:
                        size = screen.size()
                        self._input_backend.set_screen_size(
                            size.width(), size.height(),
                        )
                except Exception:
                    logger.debug("Could not query screen size for input backend")

            # Reset bandwidth
            self._bw_measure_bytes = 0
            self._bw_measure_time = time.time()
            self._bw_estimated_kbps = 0.0

            # Leggi impostazioni
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "HIGH")
            quality = getattr(QualityLevel, quality_name, QualityLevel.HIGH)
            resolution_scale = float(self._settings.value("video/resolution_scale", 1.0))

            # Codice video: preferisci HEVC HW se disponibile, altrimenti H.264
            codec = self._settings.value("video/codec", "")  # auto-detect
            prefer_hw = self._settings.value("video/hw_encoding", True, type=bool)
            if not codec:
                codec = VideoEncoder.default_codec(prefer_hw=prefer_hw)

            # Pixel format: default dal PlatformConfig
            cfg = get_platform_config()

            # CRF mode: mappa qualita' a CRF
            crf = _QUALITY_CRF.get(quality)

            # Pixel format: default dal PlatformConfig, poi da impostazioni
            pixel_format = self._settings.value("video/pixel_format", "")
            if not pixel_format:
                pixel_format = cfg.default_pixel_format
            if pixel_format not in ("yuv420p", "yuv444p"):
                pixel_format = cfg.default_pixel_format

            # Encoder preset: empty = auto by quality level
            encoder_preset = self._settings.value("video/encoder_preset", "")

            # Crea configurazione della pipeline
            config = PipelineConfig(
                fps=fps,
                quality=quality,
                bitrate=_DEFAULT_BITRATES[quality],
                resolution_scale=resolution_scale,
                monitor_index=0,
                codec=codec,
                crf=crf,
                pixel_format=pixel_format,
                encoder_preset=encoder_preset,
            )

            # Callback per tracciare i byte inviati (bandwidth estimation)
            def _on_send(data: bytes) -> None:
                self._bw_measure_bytes += len(data)

            # Callback per errori della pipeline (CaptureWorker / EncoderWorker)
            # Usa QTimer.singleShot per deferire al main thread Qt:
            # stop_streaming() chiama pipeline.stop() che fa join() sui thread
            # worker — se chiamato dal worker stesso causa RuntimeError.
            def _on_pipeline_error(msg: str) -> None:
                logger.error("Pipeline error: %s", msg)
                self.error.emit(msg)
                QTimer.singleShot(0, self.stop_streaming)

            # Crea e avvia la pipeline
            self._pipeline = StreamingPipeline(
                config,
                self._relay.send_frame,
                self._relay.send_tile,
                on_error=_on_pipeline_error,
            )
            self._pipeline.on_keyframe_sent = lambda data, w, h, pts, kf: _on_send(data)
            self._pipeline.on_tile_sent = lambda data, x, y, tw, th, pts: _on_send(data)
            self._pipeline.start()

            # Avvia cattura audio se abilitata (non blocca la pipeline in caso di errore)
            if self._audio_enabled:
                try:
                    self._start_audio_capture()
                except Exception as e:
                    logger.warning("Failed to start audio capture: %s", e)
                    self._audio_enabled = False

            # Avvia cattura webcam se abilitata (non blocca la pipeline in caso di errore)
            if self._camera_enabled:
                try:
                    self._start_camera_capture()
                except Exception as e:
                    logger.warning("Failed to start camera capture: %s", e)
                    self._camera_enabled = False

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
        """Ferma la pipeline, audio capture e rilascia le risorse."""
        self._bw_timer.stop()
        self._stop_audio_capture()
        self._stop_camera_capture()
        if self._pipeline:
            self._pipeline.stop()
            self._pipeline = None
        if self._input_backend:
            self._input_backend.release()
            self._input_backend = None
        self._bw_measure_bytes = 0
        logger.info("Streaming stopped")

    # ── audio capture (host side) ─────────────────────────────────────

    def _start_audio_capture(self) -> None:
        """Avvia la cattura del microfono in un thread separato."""
        if self._audio_manager.is_capturing:
            logger.debug("Audio capture already running")
            return
        self._audio_manager.direction = AudioDirection.MIC_ONLY
        # start_capture può lanciare eccezioni (es. codec Opus non disponibile)
        self._audio_manager.start_capture(self._relay.send_message)

    def _stop_audio_capture(self) -> None:
        """Ferma la cattura del microfono."""
        try:
            self._audio_manager.stop_capture()
        except Exception as e:
            logger.debug("Audio stop error (ignored): %s", e)

    def play_audio_frame(self, data: bytes) -> None:
        """Decodifica e riproduce un pacchetto audio ricevuto (lato client)."""
        try:
            self._audio_manager.play_audio_frame(data)
        except Exception as e:
            logger.debug("Audio playback error (ignored): %s", e)

    # ── camera capture (host side) ─────────────────────────────────────

    def _start_camera_capture(self) -> None:
        """Avvia la cattura della webcam in un thread separato."""
        if self._camera_manager.is_capturing:
            logger.debug("Camera capture already running")
            return
        self._camera_manager.start_capture(self._relay.send_message)

    def _stop_camera_capture(self) -> None:
        """Ferma la cattura della webcam."""
        try:
            self._camera_manager.stop_capture()
        except Exception as e:
            logger.debug("Camera stop error (ignored): %s", e)

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
            logger.warning("inject_mouse: input_backend is None, ignoring")
            return
        payload = msg.payload
        x = payload.get("x", 0)
        y = payload.get("y", 0)
        button = payload.get("button")
        pressed = payload.get("pressed")
        absolute = payload.get("absolute", True)

        logger.debug(
            "inject_mouse: x=%d y=%d button=%s pressed=%s abs=%s",
            x, y, button, pressed, absolute,
        )

        # button=0 dal movimento mouse (non un click)
        if button is not None and button > 0:
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
