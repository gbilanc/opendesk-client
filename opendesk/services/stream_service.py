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

_TILE_SIZE = 64  # tile width/height in pixels
_TILE_THRESHOLD = 16  # pixel difference threshold for change detection
_TILE_MAX_CHANGED_RATIO = 0.30  # if more tiles changed, send full frame
_KEYFRAME_INTERVAL = 60  # send full keyframe every N frames (every ~2s at 30fps)
# PNG compression level per quality preset (0=fast/no compression, 9=max).
# PNG is lossless — compression level only affects file size vs speed.
_TILE_PNG_COMPRESSION: dict[QualityLevel, int] = {
    QualityLevel.LOW: 1,
    QualityLevel.MEDIUM: 3,
    QualityLevel.HIGH: 6,
    QualityLevel.LOSSLESS: 9,
}


# ═══════════════════════════════════════════════════════════════════
# StreamService
# ═══════════════════════════════════════════════════════════════════


class StreamService(QObject):
    """Servizio di streaming video con tile grid differenziale.

    Usa una griglia di tile (default 64×64 px) per inviare solo le
    aree dello schermo che cambiano. Periodicamente invia un full
    keyframe H.264 per risincronizzazione.

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

        # Capture
        self._capture: ScreenCapture | None = None
        self._capture_running = False
        self._encoder: VideoEncoder | None = None
        self._input_backend: InputBackend | None = None

        # Timers
        self._stream_timer = QTimer(self)
        self._stream_timer.timeout.connect(self._capture_and_send)
        self._bw_timer = QTimer(self)
        self._bw_timer.timeout.connect(self._update_bitrate)

        # Bandwidth estimation
        self._bw_measure_bytes: int = 0
        self._bw_measure_time: float = 0.0
        self._bw_estimated_kbps: float = 0.0

        # React when the remote peer requests a keyframe
        self._relay.host_keyframe_requested.connect(self._force_keyframe)

        # Tile grid state
        self._prev_frame: np.ndarray | None = None
        self._frame_count: int = 0
        self._force_full_keyframe: bool = False

    # ── properties ──────────────────────────────────────────────────

    @property
    def is_streaming(self) -> bool:
        return self._capture_running

    @property
    def input_backend(self) -> InputBackend | None:
        return self._input_backend

    @property
    def capture(self) -> ScreenCapture | None:
        return self._capture

    # ── streaming lifecycle ─────────────────────────────────────────

    def _lazy_init_encoder(self, width: int, height: int) -> bool:
        """Create the encoder on first successful capture, if not already done."""
        if self._encoder is not None:
            return True
        try:
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "MEDIUM")
            quality = getattr(QualityLevel, quality_name, QualityLevel.MEDIUM)
            self._encoder = VideoEncoder(
                EncoderConfig(
                    width=width,
                    height=height,
                    fps=fps,
                    bitrate=_DEFAULT_BITRATES[quality],
                    quality=quality,
                )
            )
            logger.info("Encoder lazy-init: %dx%d @ %s", width, height, quality_name)
            return True
        except Exception as e:
            logger.warning("Encoder init failed: %s", e)
            return False

    def start_streaming(self) -> None:
        """Avvia la cattura schermo, encoding e streaming."""
        try:
            self._capture = ScreenCapture()
            self._capture_running = True

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

            # Tile grid state
            self._prev_frame = None
            self._frame_count = 0
            self._force_full_keyframe = False

            # Settings
            fps = int(self._settings.value("video/max_fps", 30))
            quality_name = self._settings.value("video/quality", "HIGH")
            quality = getattr(QualityLevel, quality_name, QualityLevel.HIGH)

            # Capture first frame — if it succeeds, init encoder immediately
            first = self._capture.capture_one(0)
            if first is not None:
                self._lazy_init_encoder(first.width, first.height)
            else:
                logger.warning(
                    "First capture returned None — encoder will be initialised "
                    "lazily on the first successful capture"
                )

            # Start timers (always, even if encoder not yet ready)
            self._stream_timer.start(int(1000 / fps))
            self._bw_timer.start(3000)
            logger.info("Streaming started at %d FPS (tile grid: %dx%d tiles)", fps,
                        _TILE_SIZE, _TILE_SIZE)
        except Exception as e:
            logger.exception("Failed to start streaming: %s", e)
            self.error.emit(str(e))
            self.stop_streaming()

    def stop_streaming(self) -> None:
        """Ferma cattura, encoding e timer."""
        if not self._capture_running and self._capture is None and self._encoder is None:
            return  # already stopped
        self._stream_timer.stop()
        self._bw_timer.stop()
        self._capture_running = False
        if self._capture:
            self._capture.release()
            self._capture = None
        if self._encoder:
            self._encoder.release()
            self._encoder = None
        if self._input_backend:
            self._input_backend.release()
            self._input_backend = None
        self._prev_frame = None
        self._bw_measure_bytes = 0
        self._frame_count = 0
        logger.info("Streaming stopped")

    @Slot()
    def _force_keyframe(self) -> None:
        """Force the next video frame to be a full keyframe (IDR).

        Called when the remote peer signals that it needs a fresh
        keyframe to re-sync the video decoder.
        """
        self._force_full_keyframe = True
        if self._encoder is not None:
            self._encoder.request_keyframe()
            logger.info("Forcing full keyframe on next frame per peer request")

    # ── capture / encoder ───────────────────────────────────────────

    @Slot()
    def _capture_and_send(self) -> None:
        """Cattura un frame, confronta con il precedente e invia
        solo i tile modificati. Periodicamente invia un full keyframe.
        """
        if not self._capture_running or self._capture is None:
            return
        try:
            frame = self._capture.capture_one(0)
            if frame is None:
                return

            # Lazy encoder initialisation
            if self._encoder is None:
                if not self._lazy_init_encoder(frame.width, frame.height):
                    return

            current = frame.data  # (H, W, 3) RGB
            h, w = current.shape[:2]
            pts = int(frame.timestamp * 1000)
            self._frame_count += 1

            # Decide whether to send a full keyframe or tiles
            send_full = (
                self._prev_frame is None
                or self._force_full_keyframe
                or self._frame_count >= _KEYFRAME_INTERVAL
            )

            if send_full:
                self._send_full_keyframe(current, w, h, pts)
                self._prev_frame = current.copy()
                self._frame_count = 0
                self._force_full_keyframe = False
                return

            # Tile grid: find and send changed tiles
            self._send_changed_tiles(current, w, h, pts)

        except Exception as e:
            logger.warning("Capture/encode error: %s", e)

    def _send_full_keyframe(self, rgb: np.ndarray, w: int, h: int, pts: int) -> None:
        """Send a full H.264 keyframe for sync."""
        if self._encoder is None:
            return
        self._encoder.request_keyframe()
        packets = self._encoder.encode(rgb)
        for pkt in packets:
            self._relay.send_frame(
                pkt.data, pkt.width, pkt.height, pts, keyframe=pkt.is_keyframe,
            )
            self._bw_measure_bytes += len(pkt.data)
        logger.debug("Full keyframe sent: %dx%d (%d bytes)", w, h,
                      sum(len(p.data) for p in packets))

    def _send_changed_tiles(self, current: np.ndarray, w: int, h: int, pts: int) -> None:
        """Compare current frame with previous, encode and send changed tiles.

        Uses full-frame diff (vectorised) before iterating tiles, avoiding
        the N×M per-tile astype() cost that dominated CPU usage.
        """
        prev = self._prev_frame
        if prev is None or prev.shape != current.shape:
            self._send_full_keyframe(current, w, h, pts)
            self._prev_frame = current.copy()
            return

        tile_size = _TILE_SIZE
        threshold = _TILE_THRESHOLD
        quality_name = self._settings.value("video/quality", "HIGH")
        quality = getattr(QualityLevel, quality_name, QualityLevel.HIGH)
        png_level = _TILE_PNG_COMPRESSION[quality]

        changed_tiles: list[tuple[bytes, int, int, int, int]] = []
        total_tiles = 0

        # ── 1. Full-frame diff (vectorised, single astype call) ──
        diff = np.abs(current.astype(np.int16) - prev.astype(np.int16))
        frame_changed = diff > threshold  # bool mask (H, W, 3)
        any_changed = np.any(frame_changed, axis=2)  # 2D bool mask (H, W)

        # ── 2. Iterate tiles on the bool mask only ──
        for y in range(0, h, tile_size):
            th = min(tile_size, h - y)
            for x in range(0, w, tile_size):
                tw = min(tile_size, w - x)
                total_tiles += 1

                tile_mask = any_changed[y:y + th, x:x + tw]
                change_ratio = tile_mask.sum() / tile_mask.size

                if change_ratio > 0.005:  # 0.5% of pixels changed
                    cur_tile = current[y:y + th, x:x + tw]
                    tile_bgr = cv2.cvtColor(cur_tile, cv2.COLOR_RGB2BGR)
                    success, encoded = cv2.imencode(
                        '.png', tile_bgr,
                        [cv2.IMWRITE_PNG_COMPRESSION, png_level],
                    )
                    if success:
                        changed_tiles.append((encoded.tobytes(), x, y, tw, th))

        # Update previous frame
        self._prev_frame = current.copy()

        # If too many tiles changed, send a full keyframe instead (more efficient)
        if total_tiles > 0:
            change_ratio = len(changed_tiles) / total_tiles
            if change_ratio > _TILE_MAX_CHANGED_RATIO:
                logger.debug(
                    "%.0f%% tiles changed — sending full keyframe instead",
                    change_ratio * 100,
                )
                self._send_full_keyframe(current, w, h, pts)
                self._frame_count = 0
                return

        # Send all changed tiles
        for tile_data, tx, ty, tw, th in changed_tiles:
            self._relay.send_tile(tile_data, tx, ty, tw, th, pts)
            self._bw_measure_bytes += len(tile_data)

        if changed_tiles:
            logger.debug(
                "Sent %d/%d tiles (%.1f KB)",
                len(changed_tiles), total_tiles,
                sum(len(t[0]) for t in changed_tiles) / 1024,
            )

    # ── bandwidth adaptation ────────────────────────────────────────

    @Slot()
    def _update_bitrate(self) -> None:
        """Aggiorna il bitrate in base alla banda misurata."""
        if not self._encoder or not self._capture_running:
            return

        now = time.time()
        elapsed = now - self._bw_measure_time
        if elapsed < 2.0 or self._bw_measure_bytes < 1024:
            return

        measured_kbps = (self._bw_measure_bytes * 8) / (elapsed * 1000)
        self._bw_measure_bytes = 0
        self._bw_measure_time = now

        if self._bw_estimated_kbps == 0:
            self._bw_estimated_kbps = measured_kbps
        else:
            self._bw_estimated_kbps = self._bw_estimated_kbps * 0.7 + measured_kbps * 0.3

        target_bitrate = int(self._bw_estimated_kbps * 1000 * 0.8)
        target_bitrate = max(100_000, min(50_000_000, target_bitrate))

        current = self._encoder.actual_bitrate
        if abs(target_bitrate - current) > current * 0.2:
            logger.info("Adaptive bitrate: %.0f kbps → %d kbps", self._bw_estimated_kbps, target_bitrate // 1000)
            self._encoder.actual_bitrate = target_bitrate
            self.bitrate_changed.emit(self._bw_estimated_kbps)

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
