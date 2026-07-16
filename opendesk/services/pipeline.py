"""Pipeline parallela di streaming: capture, encode e send su 3 thread.

Rimpiazza il timer singolo di ``StreamService._capture_and_send()``
con una pipeline multi-thread:

  [CaptureThread] ──frame_queue──► [EncoderThread] ──pkt_queue──► [NetThread]

Ogni thread è indipendente, quindi l'encoding non blocca mai la cattura
e l'invio non blocca l'encoding.  Le code hanno una dimensione massima
per evitare che un thread lento accumuli memoria indefinitamente.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from enum import Enum, auto
from typing import Callable

import cv2
import numpy as np

from opendesk.core.screen_capture import ScreenCapture, CapturedFrame
from opendesk.core.video_codec import VideoEncoder, EncoderConfig, QualityLevel, _QUALITY_CRF
from opendesk.network.protocol import Message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRAME_QUEUE_MAX = 3  # max frames in flight (back-pressure)
_PKT_QUEUE_MAX = 30   # max encoded packets queued for network


@dataclasses.dataclass
class PipelineConfig:
    """Runtime configuration for the streaming pipeline.

    Snapshot of settings read from QSettings at pipeline start.
    """
    fps: int = 30
    quality: QualityLevel = QualityLevel.HIGH
    bitrate: int = 8_000_000
    resolution_scale: float = 1.0
    monitor_index: int = 0
    codec: str = ""  # auto-detect if empty
    crf: int | None = None  # None = use bitrate, int = CRF mode
    pixel_format: str = "yuv420p"  # "yuv420p" or "yuv444p"
    encoder_preset: str = ""  # empty = auto by quality level


# ═══════════════════════════════════════════════════════════════════
# CaptureWorker
# ═══════════════════════════════════════════════════════════════════


class CaptureWorker(threading.Thread):
    """Thread che cattura lo schermo e invia frame all'encoder.

    Spara frame alla frequenza configurata, indipendentemente
    dalla velocità dell'encoder.  Se l'encoder è più lento,
    ``frame_queue`` si riempie fino a ``_FRAME_QUEUE_MAX`` e poi
    il worker salta frame (back-pressure naturale).

    In caso di errore critico chiama ``on_error`` (se fornito)
    per permettere al chiamante di arrestare la pipeline.
    """

    def __init__(
        self,
        config: PipelineConfig,
        frame_queue: queue.Queue,
        stop_event: threading.Event,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(name="CaptureWorker", daemon=True)
        self._config = config
        self._frame_queue = frame_queue
        self._stop_event = stop_event
        self._on_error = on_error
        self._capture: ScreenCapture | None = None

    def run(self) -> None:
        logger.info("CaptureWorker started")
        try:
            self._capture = ScreenCapture()
        except Exception as e:
            msg = f"ScreenCapture init failed: {e}"
            logger.error("CaptureWorker: %s", msg)
            if self._on_error:
                self._on_error(msg)
            return
        interval = 1.0 / max(self._config.fps, 1)

        consec_errors = 0
        max_consec_errors = 10

        while not self._stop_event.is_set():
            t0 = time.perf_counter()

            try:
                frame = self._capture.capture_one(self._config.monitor_index)
                if frame is None:
                    # PipeWire ancora in fase di startup
                    time.sleep(0.01)
                    continue

                # Successo — resetta contatore errori
                consec_errors = 0

                # ── Resolution scaling ──
                scale = self._config.resolution_scale
                data = frame.data
                if scale != 1.0:
                    h, w = data.shape[:2]
                    nw, nh = int(w * scale), int(h * scale)
                    if nw > 0 and nh > 0:
                        # INTER_AREA produces sharper results than INTER_LINEAR
                        # when downscaling — much better for text readability.
                        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
                        data = cv2.resize(data, (nw, nh), interpolation=interpolation)

                # ── Invia all'encoder (non bloccante) ──
                try:
                    self._frame_queue.put(
                        (data, frame.timestamp),
                        block=True,
                        timeout=0.1,
                    )
                except queue.Full:
                    # Encoder è in ritardo — salta questo frame
                    logger.debug("CaptureWorker: frame queue full, dropping frame")

            except Exception as e:
                consec_errors += 1
                if consec_errors >= max_consec_errors:
                    msg = f"CaptureWorker: {consec_errors} errori consecutivi — arresto"
                    logger.error(msg)
                    if self._on_error:
                        self._on_error(f"Screen capture fallita: {e}")
                    break
                if consec_errors == 1:
                    logger.warning("CaptureWorker error: %s", e)
                elif consec_errors <= 3:
                    logger.warning("CaptureWorker error (%d/10): %s", consec_errors, e)
                # dopo 3 errori logga solo in debug per non spammare
                elif consec_errors <= max_consec_errors:
                    logger.debug("CaptureWorker error (%d/10): %s", consec_errors, e)

            # Mantieni il frame rate target
            elapsed = time.perf_counter() - t0
            sleep = max(0.0, interval - elapsed)
            if sleep > 0.001:
                time.sleep(sleep)

        if self._capture:
            self._capture.release()
        logger.info("CaptureWorker stopped")

    def stop(self) -> None:
        self._stop_event.set()


# ═══════════════════════════════════════════════════════════════════
# EncoderWorker
# ═══════════════════════════════════════════════════════════════════


class EncoderWorker(threading.Thread):
    """Thread che prende frame dalla coda e li codifica in H.264/H.265.

    Supporta sia full keyframe che tile JPEG.
    Emette packet sulla ``pkt_queue`` per il NetworkWorker.

    Watchdog: se non arrivano frame per 5s (CaptureWorker morto),
    chiama ``on_error`` per arrestare la pipeline.
    """

    def __init__(
        self,
        config: PipelineConfig,
        frame_queue: queue.Queue,
        pkt_queue: queue.Queue,
        stop_event: threading.Event,
        on_send_full_keyframe: Callable | None = None,
        on_send_tile: Callable | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(name="EncoderWorker", daemon=True)
        self._config = config
        self._frame_queue = frame_queue
        self._pkt_queue = pkt_queue
        self._stop_event = stop_event
        self._on_send_full_keyframe = on_send_full_keyframe
        self._on_send_tile = on_send_tile
        self._on_error = on_error

        self._encoder: VideoEncoder | None = None
        self._prev_frame: np.ndarray | None = None
        self._frame_count: int = 0
        self._force_full_keyframe: bool = False

    def run(self) -> None:
        logger.info("EncoderWorker started")
        last_frame_time = time.monotonic()

        while not self._stop_event.is_set():
            try:
                data, timestamp = self._frame_queue.get(block=True, timeout=0.5)
                last_frame_time = time.monotonic()
            except queue.Empty:
                # Watchdog: se non arrivano frame per 5s, CaptureWorker e' morto
                if time.monotonic() - last_frame_time > 5.0:
                    msg = "CaptureWorker died — no frames for 5s"
                    logger.error("EncoderWorker: %s", msg)
                    if self._on_error:
                        self._on_error(msg)
                    break
                continue

            if data is None:
                break

            h, w = data.shape[:2]
            pts = int(timestamp * 1000)
            self._frame_count += 1

            try:
                # Lazy init encoder
                if self._encoder is None:
                    crf = self._config.crf
                    if crf is None:
                        crf = _QUALITY_CRF.get(self._config.quality)
                    # Build options with optional preset override
                    enc_opts = {"preset": self._config.encoder_preset} if self._config.encoder_preset else {"preset": "veryfast"}

                    self._encoder = VideoEncoder(
                        EncoderConfig(
                            width=w,
                            height=h,
                            fps=self._config.fps,
                            bitrate=self._config.bitrate,
                            quality=self._config.quality,
                            codec=self._config.codec,
                            crf=crf,
                            pixel_format=self._config.pixel_format,
                            options=enc_opts,
                        )
                    )
                    if self._encoder.codec_name:
                        logger.info("EncoderWorker: using %s CRF=%s",
                                    self._encoder.codec_name, crf or "(bitrate)")

                # Decidi se inviare full keyframe o tile
                send_full = (
                    self._prev_frame is None
                    or self._force_full_keyframe
                    or self._frame_count >= 60  # KEYFRAME_INTERVAL
                )

                if send_full:
                    self._do_full_keyframe(data, w, h, pts)
                    self._prev_frame = data.copy()
                    self._frame_count = 0
                    self._force_full_keyframe = False
                else:
                    self._do_tiles(data, w, h, pts)
            except Exception as e:
                logger.exception("EncoderWorker: encode error: %s", e)
                if self._encoder:
                    self._encoder.release()
                    self._encoder = None

        # Flush encoder residuals
        if self._encoder:
            for pkt in self._encoder.flush():
                self._pkt_queue.put(pkt)
            self._encoder.release()

        logger.info("EncoderWorker stopped")

    def request_keyframe(self) -> None:
        self._force_full_keyframe = True

    # ── internals ──────────────────────────────────────────────────

    def _do_full_keyframe(
        self, rgb: np.ndarray, w: int, h: int, pts: int
    ) -> None:
        if self._encoder is None:
            return
        self._encoder.request_keyframe()
        packets = self._encoder.encode(rgb)
        for pkt in packets:
            self._pkt_queue.put(pkt)
            if self._on_send_full_keyframe:
                self._on_send_full_keyframe(pkt.data, w, h, pts, pkt.is_keyframe)

    def _do_tiles(self, current: np.ndarray, w: int, h: int, pts: int) -> None:
        prev = self._prev_frame
        if prev is None or prev.shape != current.shape:
            self._do_full_keyframe(current, w, h, pts)
            self._prev_frame = current.copy()
            return

        tile_size = 128
        threshold = 16
        quality = self._config.quality
        jpeg_q = {
            QualityLevel.LOW: 50,
            QualityLevel.MEDIUM: 65,
            QualityLevel.HIGH: 80,
            QualityLevel.SHARP: 95,
            QualityLevel.LOSSLESS: 95,
        }[quality]

        # Full-frame diff (vectorised)
        diff = np.abs(current.astype(np.int16) - prev.astype(np.int16))
        any_changed = np.any(diff > threshold, axis=2)

        total_tiles = 0
        changed = 0
        # Pre-alloc list per tile data
        tiles: list[tuple[bytes, int, int, int, int]] = []

        for y in range(0, h, tile_size):
            th = min(tile_size, h - y)
            for x in range(0, w, tile_size):
                tw = min(tile_size, w - x)
                total_tiles += 1
                tile_mask = any_changed[y:y + th, x:x + tw]
                if tile_mask.sum() / tile_mask.size > 0.005:
                    cur_tile = current[y:y + th, x:x + tw]
                    tile_bgr = cv2.cvtColor(cur_tile, cv2.COLOR_RGB2BGR)
                    success, encoded = cv2.imencode(
                        ".jpg", tile_bgr,
                        [cv2.IMWRITE_JPEG_QUALITY, jpeg_q],
                    )
                    if success:
                        tiles.append((encoded.tobytes(), x, y, tw, th))
                        changed += 1

        self._prev_frame = current.copy()

        # Se troppi tile cambiati, meglio un full keyframe
        if total_tiles > 0 and changed / total_tiles > 0.30:
            self._do_full_keyframe(current, w, h, pts)
            self._frame_count = 0
            return

        for tile_data, tx, ty, tw, th in tiles:
            self._pkt_queue.put(("tile", tile_data, tx, ty, tw, th, pts))
            if self._on_send_tile:
                self._on_send_tile(tile_data, tx, ty, tw, th, pts)


# ═══════════════════════════════════════════════════════════════════
# NetworkWorker
# ═══════════════════════════════════════════════════════════════════


class NetworkWorker(threading.Thread):
    """Thread che invia i pacchetti codificati al relay.

    Svuota la ``pkt_queue`` e chiama le funzioni di invio del relay.
    """

    def __init__(
        self,
        pkt_queue: queue.Queue,
        stop_event: threading.Event,
        send_frame_fn: Callable,
        send_tile_fn: Callable,
    ) -> None:
        super().__init__(name="NetworkWorker", daemon=True)
        self._pkt_queue = pkt_queue
        self._stop_event = stop_event
        self._send_frame = send_frame_fn
        self._send_tile = send_tile_fn

    def run(self) -> None:
        logger.info("NetworkWorker started")
        while not self._stop_event.is_set():
            try:
                item = self._pkt_queue.get(block=True, timeout=0.5)
            except queue.Empty:
                continue

            if item is None:
                break

            try:
                if isinstance(item, tuple) and item[0] == "tile":
                    # Tile JPEG
                    _, data, x, y, tw, th, pts = item
                    self._send_tile(data, x, y, tw, th, pts)
                else:
                    # Full H.264 frame (EncodedPacket-like)
                    data = item.data
                    self._send_frame(
                        data,
                        item.width,
                        item.height,
                        item.pts,
                        keyframe=item.is_keyframe,
                    )
            except Exception as e:
                logger.warning("NetworkWorker send error: %s", e)

        logger.info("NetworkWorker stopped")


# ═══════════════════════════════════════════════════════════════════
# Pipeline orchestrator
# ═══════════════════════════════════════════════════════════════════


class StreamingPipeline:
    """Orchestratore della pipeline multi-thread.

    Usage::

        pipeline = StreamingPipeline(config, relay)
        pipeline.start()
        ...
        pipeline.stop()
    """

    def __init__(
        self,
        config: PipelineConfig,
        send_frame_fn: Callable,
        send_tile_fn: Callable,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._frame_queue: queue.Queue = queue.Queue(maxsize=_FRAME_QUEUE_MAX)
        self._pkt_queue: queue.Queue = queue.Queue(maxsize=_PKT_QUEUE_MAX)
        self._stop_event = threading.Event()

        self._capture_worker = CaptureWorker(
            config, self._frame_queue, self._stop_event,
            on_error=on_error,
        )
        self._encoder_worker = EncoderWorker(
            config,
            self._frame_queue,
            self._pkt_queue,
            self._stop_event,
            on_send_full_keyframe=self._on_send_full_keyframe,
            on_send_tile=self._on_send_tile,
            on_error=on_error,
        )
        self._network_worker = NetworkWorker(
            self._pkt_queue,
            self._stop_event,
            send_frame_fn,
            send_tile_fn,
        )

        # Callback hooks (collegati da StreamService)
        self.on_keyframe_sent: Callable | None = None
        self.on_tile_sent: Callable | None = None

    def start(self) -> None:
        """Avvia tutti e 3 i worker thread."""
        logger.info("Starting streaming pipeline (3 threads)")
        self._stop_event.clear()
        self._capture_worker.start()
        self._encoder_worker.start()
        self._network_worker.start()

    def stop(self) -> None:
        """Arresta tutti i worker (con drain code)."""
        logger.info("Stopping streaming pipeline")
        self._stop_event.set()
        # Svuota le code per sbloccare i thread in attesa
        self._drain_queue(self._frame_queue)
        self._drain_queue(self._pkt_queue)
        # Invia sentinelle None per sbloccare i get()
        self._frame_queue.put((None, 0))
        self._pkt_queue.put(None)
        # Join con timeout
        for t in (self._capture_worker, self._encoder_worker, self._network_worker):
            t.join(timeout=3.0)
            if t.is_alive():
                logger.warning("Worker %s did not stop cleanly", t.name)
        logger.info("Streaming pipeline stopped")

    def request_keyframe(self) -> None:
        """Forza un keyframe al prossimo frame encode."""
        self._encoder_worker.request_keyframe()

    def update_config(self, config: PipelineConfig) -> None:
        """Aggiorna la configurazione a caldo."""
        self._config = config

    @staticmethod
    def _drain_queue(q: queue.Queue) -> None:
        while not q.empty():
            try:
                q.get_nowait()
            except queue.Empty:
                break

    def _on_send_full_keyframe(
        self, data: bytes, w: int, h: int, pts: int, is_keyframe: bool
    ) -> None:
        if self.on_keyframe_sent:
            self.on_keyframe_sent(data, w, h, pts, is_keyframe)

    def _on_send_tile(
        self, data: bytes, x: int, y: int, tw: int, th: int, pts: int
    ) -> None:
        if self.on_tile_sent:
            self.on_tile_sent(data, x, y, tw, th, pts)
