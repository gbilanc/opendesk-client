"""
Webcam capture for remote desktop.

Detects available cameras via OpenCV, captures frames, encodes as JPEG,
and streams to the remote peer. Runs in a background thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_WIDTH = 640
_DEFAULT_HEIGHT = 480
_DEFAULT_FPS = 15
_DEFAULT_JPEG_QUALITY = 75  # 1-100, higher = better quality, larger
_CAMERA_POLL_INTERVAL_S = 0.01  # sleep between frame polls (busy-wait mitigation)


class CameraState(Enum):
    """Camera state."""

    DISABLED = auto()
    IDLE = auto()
    CAPTURING = auto()
    ERROR = auto()


@dataclass
class CameraConfig:
    """Camera streaming configuration."""

    device_index: int = 0  # which camera device (0 = default)
    width: int = _DEFAULT_WIDTH
    height: int = _DEFAULT_HEIGHT
    fps: int = _DEFAULT_FPS
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY
    enabled: bool = False


# ---------------------------------------------------------------------------
# Camera device detection
# ---------------------------------------------------------------------------


def list_cameras(max_devices: int = 10) -> list[dict]:
    """Detect available cameras.

    Returns a list of dicts with ``index`` and ``name`` keys.
    Name may be empty if the backend doesn't provide it.
    """
    cameras = []
    for i in range(max_devices):
        try:
            cap = cv2.VideoCapture(i, cv2.CAP_ANY)
            if cap is None or not cap.isOpened():
                cap.release()
                continue
            # Try to read one frame to confirm the device works
            ret, _ = cap.read()
            if not ret:
                cap.release()
                continue
            # Get the camera name (may be empty)
            name = cap.getBackendName()
            if not name:
                name = cap.get(cv2.CAP_PROP_BRAND)
            cameras.append({"index": i, "name": name or f"Camera {i}"})
            cap.release()
        except Exception:
            continue
    return cameras


# ---------------------------------------------------------------------------
# Camera manager
# ---------------------------------------------------------------------------


class CameraManager:
    """Manages webcam capture and streaming.

    Uses OpenCV (``cv2.VideoCapture``) to capture frames from a webcam,
    encodes them as JPEG, and sends them to the remote peer via a
    callback function.

    Runs in a dedicated background thread so it doesn't interfere
    with the screen capture pipeline.
    """

    def __init__(self, config: CameraConfig | None = None) -> None:
        self._config = config or CameraConfig()
        self._state = CameraState.IDLE
        self._send_fn: Callable | None = None

        # Capture thread
        self._capture_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Actual capture resolution (may differ from config due to camera caps)
        self._actual_width: int = 0
        self._actual_height: int = 0

    # ── properties ──────────────────────────────────────────────────

    @property
    def config(self) -> CameraConfig:
        return self._config

    @property
    def state(self) -> CameraState:
        return self._state

    @property
    def is_capturing(self) -> bool:
        return self._capture_thread is not None and self._capture_thread.is_alive()

    @property
    def actual_resolution(self) -> tuple[int, int]:
        """The actual capture resolution (may differ from config)."""
        return (self._actual_width, self._actual_height)

    # ── lifecycle ───────────────────────────────────────────────────

    def start_capture(self, send_fn: Callable) -> None:
        """Start webcam capture in a background thread.

        Parameters
        ----------
        send_fn : callable
            Function to send camera frames to the remote peer.
            Called from the capture thread with a ``Message``.
        """
        if self.is_capturing:
            logger.warning("Camera capture already running")
            return

        self._send_fn = send_fn
        self._stop_event.clear()
        self._state = CameraState.CAPTURING

        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name="CameraCapture",
        )
        self._capture_thread.start()
        logger.info(
            "Camera capture started: device=%d, requested=%dx%d@%dfps",
            self._config.device_index,
            self._config.width,
            self._config.height,
            self._config.fps,
        )

    def stop_capture(self) -> None:
        """Stop webcam capture."""
        self._state = CameraState.IDLE
        self._stop_event.set()

        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3.0)
            if self._capture_thread.is_alive():
                logger.warning("Camera capture thread did not stop cleanly")
            self._capture_thread = None

        logger.info("Camera capture stopped")

    def toggle(self, send_fn: Callable | None = None) -> None:
        """Toggle camera capture on/off.

        If capturing → stop.  If idle → start with optional *send_fn*.
        """
        if self.is_capturing:
            self.stop_capture()
        elif send_fn is not None:
            self.start_capture(send_fn)
        else:
            logger.warning("Cannot toggle camera: no send_fn provided")

    # ── capture loop (runs in thread) ───────────────────────────────

    def _capture_loop(self) -> None:
        """Continuously capture webcam frames, encode as JPEG, and send."""
        cap: cv2.VideoCapture | None = None

        try:
            # Open camera
            cap = cv2.VideoCapture(self._config.device_index, cv2.CAP_ANY)
            if cap is None or not cap.isOpened():
                self._state = CameraState.ERROR
                logger.error("Failed to open camera device %d", self._config.device_index)
                return

            # Set requested resolution
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._config.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._config.height)
            cap.set(cv2.CAP_PROP_FPS, self._config.fps)

            # Read actual resolution
            self._actual_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            self._actual_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            actual_fps = cap.get(cv2.CAP_PROP_FPS)
            logger.info(
                "Camera opened: %dx%d @ %.1ffps",
                self._actual_width, self._actual_height, actual_fps,
            )

            frame_interval = 1.0 / max(self._config.fps, 1)
            frame_count = 0

            while not self._stop_event.is_set():
                t0 = time.perf_counter()

                # Read frame
                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("Camera read failed")
                    time.sleep(frame_interval)
                    continue

                # Encode as JPEG
                encode_param = [
                    cv2.IMWRITE_JPEG_QUALITY,
                    self._config.jpeg_quality,
                ]
                success, encoded = cv2.imencode(".jpg", frame, encode_param)
                if not success:
                    time.sleep(frame_interval)
                    continue

                # Send via callback
                if self._send_fn:
                    from opendesk.network.protocol import Message, MessageType
                    msg = Message(
                        MessageType.CAMERA_FRAME,
                        {
                            "data": encoded.tobytes(),
                            "width": self._actual_width,
                            "height": self._actual_height,
                            "pts": int(time.time() * 1000),
                            "format": "jpeg",
                        },
                    )
                    try:
                        self._send_fn(msg)
                    except Exception as e:
                        logger.warning("Camera send error: %s", e)

                frame_count += 1

                # Maintain target FPS
                elapsed = time.perf_counter() - t0
                sleep = max(0.0, frame_interval - elapsed)
                if sleep > 0.001:
                    time.sleep(sleep)

        except Exception as e:
            self._state = CameraState.ERROR
            logger.error("Camera capture error: %s", e)
        finally:
            if cap is not None:
                cap.release()
            self._state = CameraState.IDLE
            logger.info("Camera capture loop ended")

    # ── cleanup ─────────────────────────────────────────────────────

    def release(self) -> None:
        """Release all resources."""
        self.stop_capture()
        logger.info("Camera manager released")
