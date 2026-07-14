"""
Cross-platform screen capture using ``mss`` (X11/Win/macOS)
and PipeWire (Wayland).

Provides frame differencing for bandwidth-efficient streaming and
automatic monitor enumeration.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from threading import Lock
from typing import Iterator

import mss
import numpy as np
from PIL import Image

from opendesk.utils.platform import current_platform, Platform, is_wayland

logger = logging.getLogger(__name__)


class CaptureMethod(Enum):
    """Preferred backend for screen capture."""

    AUTO = auto()  # Auto-detect
    MSS = auto()  # Cross-platform (DXGI / CoreGraphics / X11)
    PIPEWIRE = auto()  # Linux Wayland via PipeWire + xdg-desktop-portal (GStreamer subprocess)
    PORTAL = auto()  # Linux Wayland via D-Bus portal + GStreamer (reuses portal session)
    DUMMY = auto()  # Test pattern for development


@dataclass(frozen=True)
class MonitorInfo:
    """Describes a single monitor."""

    index: int
    name: str
    left: int
    top: int
    width: int
    height: int
    is_primary: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return (self.width, self.height)


@dataclass
class CapturedFrame:
    """A single captured frame with metadata."""

    data: np.ndarray  # RGB uint8 array (H, W, 3)
    monitor_index: int
    timestamp: float
    region: tuple[int, int, int, int]  # (left, top, width, height)

    @property
    def width(self) -> int:
        return self.region[2]

    @property
    def height(self) -> int:
        return self.region[3]


# ---------------------------------------------------------------------------
# Frame differencing
# ---------------------------------------------------------------------------


def frame_diff_ratio(
    current: np.ndarray, previous: np.ndarray | None, threshold: int = 16
) -> float:
    if previous is None or current.shape != previous.shape:
        return 1.0
    diff = np.abs(current.astype(np.int16) - previous.astype(np.int16))
    changed = np.any(diff > threshold, axis=2)
    return float(changed.sum()) / changed.size


def compute_dirty_region(
    current: np.ndarray, previous: np.ndarray | None, threshold: int = 16
) -> tuple[int, int, int, int] | None:
    if previous is None or current.shape != previous.shape:
        return (0, 0, current.shape[1], current.shape[0])
    diff = np.abs(current.astype(np.int16) - previous.astype(np.int16))
    changed = np.any(diff > threshold, axis=2)
    coords = np.argwhere(changed)
    if coords.size == 0:
        return None
    y0, x0 = coords.min(axis=0).tolist()
    y1, x1 = coords.max(axis=0).tolist()
    return (x0, y0, x1 + 1, y1 + 1)


# ---------------------------------------------------------------------------
# PipeWire / Wayland capture backend
# ---------------------------------------------------------------------------


class PipeWireCapture:
    """Wayland screen capture via GStreamer's ``pipewiresrc``.

    Uses GStreamer (via a subprocess with system Python) to capture the
    screen through ``pipewiresrc``, which internally shows the standard
    xdg-desktop-portal screen selection dialog to the user.

    Requires:
        - GStreamer with pipewire plugin (``gstreamer1.0-pipewire``)
        - ``xdg-desktop-portal`` + backend
        - PipeWire runtime
    """

    def __init__(self) -> None:
        self._available: bool | None = None
        self._monitors: list[MonitorInfo] = []
        self._helper_process: subprocess.Popen | None = None
        self._resolved_w: int = 0
        self._resolved_h: int = 0
        self._started: bool = False

    # ── availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if GStreamer + pipewiresrc are available on the system."""
        if self._available is not None:
            return self._available

        system_python = _find_system_python()
        if not system_python:
            logger.debug("PipeWire: no system Python with gi found")
            self._available = False
            return False

        import subprocess
        try:
            r = subprocess.run(
                [system_python, "-c",
                 "import gi; gi.require_version('Gst', '1.0');"
                 "from gi.repository import Gst; Gst.init(None);"
                 "e = Gst.ElementFactory.make('pipewiresrc', None);"
                 "exit(0 if e else 1)"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0:
                self._available = True
                return True
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

        logger.debug("PipeWire: GStreamer pipewiresrc not available")
        self._available = False
        return False

    # ── public API ──────────────────────────────────────────────────

    def start(self, monitor_index: int = 0) -> None:
        """Start the capture subprocess.

        This launches a GStreamer pipeline in a subprocess (using the
        system Python).  The portal will show a screen-selection dialog
        to the user.
        """
        if self._started:
            return
        self._started = True

        import subprocess
        from pathlib import Path

        helper = Path(__file__).parent / "_pipewire_helper.py"

        system_python = _find_system_python()
        if not system_python:
            logger.warning("PipeWire: system Python not found, cannot start")
            return

        logger.info(
            "Starting PipeWire capture (monitor %d) via %s",
            monitor_index, helper,
        )

        self._helper_process = subprocess.Popen(
            [system_python, str(helper),
             "--monitor", str(monitor_index),
             "--fps", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Read header (first 8 bytes = width, height as uint32 LE)
        # Timeout: if the portal dialog is not approved, fail after 30 s
        import struct
        import select
        header = b""
        deadline = time.monotonic() + 30.0
        while len(header) < 8 and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select([self._helper_process.stdout], [], [], min(remaining, 1.0))
            if r:
                chunk = self._helper_process.stdout.read(8 - len(header))
                if not chunk:
                    break
                header += chunk

        if len(header) < 8:
            logger.error("PipeWire: failed to read frame header (timeout or portal not approved)")
            stderr = self._helper_process.stderr.read() if self._helper_process.stderr else b""
            if stderr:
                logger.warning("PipeWire helper stderr: %s", stderr.decode(errors="replace"))
            self.release()
            return

        self._resolved_w, self._resolved_h = struct.unpack("<II", header)
        logger.info(
            "PipeWire capture started: %dx%d",
            self._resolved_w, self._resolved_h,
        )

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame | None:
        """Capture a single frame from the running subprocess."""
        if not self._started:
            self.start(monitor_index)

        if self._helper_process is None or self._helper_process.stdout is None:
            return None

        # Check if process is still alive
        if self._helper_process.poll() is not None:
            logger.warning(
                "PipeWire helper exited with code %d",
                self._helper_process.returncode,
            )
            stderr = self._helper_process.stderr.read() if self._helper_process.stderr else b""
            if stderr:
                logger.warning("PipeWire helper stderr: %s", stderr.decode(errors="replace"))
            self.release()
            return None

        frame_size = self._resolved_w * self._resolved_h * 3
        data = self._helper_process.stdout.read(frame_size)
        if len(data) < frame_size:
            logger.warning("PipeWire: incomplete frame read (%d < %d)", len(data), frame_size)
            return None

        rgb = np.frombuffer(data, dtype=np.uint8).reshape(self._resolved_h, self._resolved_w, 3)
        return CapturedFrame(
            data=rgb.copy(),
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=(0, 0, self._resolved_w, self._resolved_h),
        )

    def capture_loop(self, monitor_index: int = 0):
        """Generator that yields frames from the PipeWire subprocess."""
        self.start(monitor_index)
        while True:
            frame = self.capture_one(monitor_index)
            if frame is None:
                break
            yield frame

    def monitors(self) -> list[MonitorInfo]:
        if self._monitors:
            return self._monitors

        import subprocess
        import shutil
        import re

        self._monitors = []

        if shutil.which("wlr-randr"):
            try:
                r = subprocess.run(
                    ["wlr-randr"], capture_output=True, text=True, timeout=3,
                )
                current_name = ""
                for line in r.stdout.splitlines():
                    m = re.match(r'^(.+?)\s+"(.+?)"', line)
                    if m:
                        current_name = m.group(1)
                    m_size = re.search(r"(\d+)x(\d+) px", line)
                    m_pos = re.search(r"@ (\d+),(\d+)", line)
                    if m_size and current_name:
                        w, h = int(m_size.group(1)), int(m_size.group(2))
                        x, y = (int(m_pos.group(1)), int(m_pos.group(2))) if m_pos else (0, 0)
                        idx = len(self._monitors)
                        self._monitors.append(MonitorInfo(
                            index=idx, name=current_name,
                            left=x, top=y, width=w, height=h,
                            is_primary=idx == 0,
                        ))
                        current_name = ""
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if not self._monitors:
            self._monitors.append(MonitorInfo(
                index=0, name="Wayland Output",
                left=0, top=0, width=1920, height=1080, is_primary=True,
            ))
        return self._monitors

    def release(self) -> None:
        """Stop the capture helper subprocess."""
        self._started = False
        if self._helper_process:
            try:
                self._helper_process.terminate()
                self._helper_process.wait(timeout=3)
            except Exception:
                try:
                    self._helper_process.kill()
                except Exception:
                    pass
            self._helper_process = None
        self._resolved_w = 0
        self._resolved_h = 0


# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------


def _detect_capture_method() -> CaptureMethod:
    plat = current_platform()
    if plat == Platform.LINUX and is_wayland():
        # 1) Prefer D-Bus portal if available (avoids double dialog)
        try:
            from opendesk.core.wayland_capture import WaylandScreenCast
            wsc = WaylandScreenCast()
            if wsc.is_available():
                logger.info("Capture backend: PORTAL (Wayland D-Bus + GStreamer)")
                return CaptureMethod.PORTAL
        except Exception:
            pass
        # 2) Fall back to GStreamer pipewiresrc (shows its own portal dialog)
        pw = PipeWireCapture()
        if pw.is_available():
            logger.info("Capture backend: PIPEWIRE (GStreamer pipewiresrc)")
            return CaptureMethod.PIPEWIRE
        logger.info("Capture backend: MSS (XWayland fallback)")
    return CaptureMethod.MSS


# ---------------------------------------------------------------------------
# Screen capture engine
# ---------------------------------------------------------------------------


class ScreenCapture:
    """Cross-platform screen capture engine.

    Auto-detects the best backend:
    - Linux/Wayland → PipeWire (falls back to MSS/XWayland)
    - X11, Windows, macOS → MSS
    """

    def __init__(self, method: CaptureMethod | None = None) -> None:
        self._method = method if method and method != CaptureMethod.AUTO else _detect_capture_method()
        self._lock = Lock()
        self._sct: mss.mss | None = None
        self._pw: PipeWireCapture | None = None
        self._portal = None  # WaylandScreenCast — created lazily
        self._prev_frames: dict[int, np.ndarray] = {}
        self._fps_target: float = 30.0
        self._fps_adaptive: bool = True
        self._min_fps: float = 1.0
        self._idle_counter: int = 0
        logger.info("Screen capture: %s", self._method.name)

    @property
    def fps_target(self) -> float:
        return self._fps_target

    @fps_target.setter
    def fps_target(self, value: float) -> None:
        self._fps_target = max(1.0, min(60.0, value))

    @property
    def adaptive_fps(self) -> bool:
        return self._fps_adaptive

    @adaptive_fps.setter
    def adaptive_fps(self, enabled: bool) -> None:
        self._fps_adaptive = enabled

    @property
    def capture_method(self) -> CaptureMethod:
        return self._method

    # ── monitors ────────────────────────────────────────────────────

    def monitors(self) -> list[MonitorInfo]:
        if self._method == CaptureMethod.PIPEWIRE:
            return self._get_pw().monitors()
        if self._method == CaptureMethod.PORTAL:
            # PORTAL captures full desktop — single virtual monitor
            pw = self._get_pw()
            return pw.monitors() if pw.is_available() else [
                MonitorInfo(
                    index=0, name="Wayland Desktop",
                    left=0, top=0, width=1920, height=1080, is_primary=True,
                )
            ]
        sct = self._get_sct()
        return [
            MonitorInfo(
                index=i,
                name=m.get("name", f"Monitor {i}"),
                left=m["left"], top=m["top"],
                width=m["width"], height=m["height"],
                is_primary=m.get("is_primary", i == 0),
            )
            for i, m in enumerate(sct.monitors[1:])
        ]

    # ── single capture ──────────────────────────────────────────────

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame:
        if self._method == CaptureMethod.PORTAL:
            return self._capture_portal(monitor_index)
        if self._method == CaptureMethod.PIPEWIRE:
            pw = self._get_pw()
            try:
                f = pw.capture_one(monitor_index)
                if f is not None:
                    return f
            except Exception as e:
                logger.warning("PipeWire capture failed: %s", e)
            logger.warning("PipeWire failed, falling back to MSS")
            self._method = CaptureMethod.MSS
        try:
            return self._capture_mss(monitor_index)
        except Exception as e:
            raise RuntimeError(
                f"Screen capture failed: {e}\n"
                "On Wayland, install xdg-desktop-portal + PipeWire. "
                "On X11, ensure the display is accessible."
            ) from e

    # ── capture loop ────────────────────────────────────────────────

    def capture_loop(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        if self._method == CaptureMethod.PORTAL:
            yield from self._loop_portal(monitor_index)
        elif self._method == CaptureMethod.PIPEWIRE:
            yield from self._loop_pipewire(monitor_index)
        else:
            yield from self._loop_mss(monitor_index)

    # ── lifecycle ───────────────────────────────────────────────────

    def release(self) -> None:
        with self._lock:
            if self._sct is not None:
                self._sct.close()
                self._sct = None
            if self._pw is not None:
                self._pw.release()
                self._pw = None
            if self._portal is not None:
                self._release_portal()
            self._prev_frames.clear()

    def __enter__(self) -> ScreenCapture:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    # ── internal: MSS ───────────────────────────────────────────────

    def _get_sct(self) -> mss.mss:
        if self._sct is None:
            with self._lock:
                if self._sct is None:
                    self._sct = mss.mss()
        return self._sct

    def _capture_mss(self, monitor_index: int = 0) -> CapturedFrame:
        sct = self._get_sct()
        try:
            mon = sct.monitors[monitor_index + 1]
            raw = sct.grab(mon)
            buf = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
            return CapturedFrame(
                data=buf[:, :, :3],
                monitor_index=monitor_index,
                timestamp=time.time(),
                region=(mon["left"], mon["top"], mon["width"], mon["height"]),
            )
        except Exception as e:
            if "X11" in type(e).__name__ or "XProto" in type(e).__name__ or "X Error" in str(e):
                raise RuntimeError(
                    "Screen capture via X11 (MSS) failed on Wayland. "
                    "Install xdg-desktop-portal and PipeWire for native Wayland capture, "
                    "or run under X11."
                ) from e
            raise

    def _loop_mss(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        sct = self._get_sct()
        mon = sct.monitors[monitor_index + 1]
        while True:
            t0 = time.perf_counter()
            raw = sct.grab(mon)
            buf = np.frombuffer(raw.rgb, dtype=np.uint8).reshape(raw.height, raw.width, 3)
            rgb = buf[:, :, :3].copy()
            prev = self._prev_frames.get(monitor_index)
            diff = frame_diff_ratio(rgb, prev, threshold=12)
            self._prev_frames[monitor_index] = rgb
            yield CapturedFrame(
                data=rgb, monitor_index=monitor_index,
                timestamp=t0,
                region=(mon["left"], mon["top"], mon["width"], mon["height"]),
            )
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    # ── internal: PipeWire ──────────────────────────────────────────

    def _get_pw(self) -> PipeWireCapture:
        if self._pw is None:
            self._pw = PipeWireCapture()
        return self._pw

    def _loop_pipewire(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        pw = self._get_pw()
        pw.start(monitor_index)
        while True:
            t0 = time.perf_counter()
            frame = pw.capture_one(monitor_index)
            if frame is None:
                logger.warning("PipeWire ended, falling back to MSS")
                yield from self._loop_mss(monitor_index)
                return
            prev = self._prev_frames.get(monitor_index)
            diff = frame_diff_ratio(frame.data, prev, threshold=12)
            self._prev_frames[monitor_index] = frame.data
            yield frame
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    # ── internal: PORTAL (WaylandScreenCast D-Bus + GStreamer) ─────

    def _get_portal(self):
        """Lazy-init the WaylandScreenCast and set up the session.

        The D-Bus setup + portal dialog is synchronous-blocking because
        we run ``asyncio.run()`` internally.  Called once on first
        capture.
        """
        if self._portal is not None:
            return self._portal

        from opendesk.core.wayland_capture import WaylandScreenCast
        import asyncio

        wsc = WaylandScreenCast()
        if not wsc.is_available():
            raise RuntimeError("WaylandScreenCast not available")

        # Run async setup in a synchronous context
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # Already inside an event loop — delegate to a thread
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: asyncio.run(wsc.setup()))
                ok = future.result(timeout=60)
        else:
            ok = asyncio.run(wsc.setup())

        if not ok:
            raise RuntimeError("WaylandScreenCast setup failed")

        self._portal = wsc
        return wsc

    def _capture_portal(self, monitor_index: int = 0) -> CapturedFrame:
        """Capture a single frame via the PORTAL backend."""
        try:
            wsc = self._get_portal()
        except Exception as e:
            logger.warning("PORTAL init failed: %s — falling back to PIPEWIRE", e)
            self._method = CaptureMethod.PIPEWIRE
            return self.capture_one(monitor_index)

        rgb = wsc.capture_frame_sync()
        if rgb is None:
            # Helper may have exited — try MSS fallback
            logger.warning("PORTAL capture returned None")
            raise RuntimeError("PORTAL frame capture failed")

        w, h = wsc.width, wsc.height
        return CapturedFrame(
            data=rgb,
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=(0, 0, w, h),
        )

    def _loop_portal(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        """Continuous capture via PORTAL (WaylandScreenCast)."""
        try:
            wsc = self._get_portal()
        except Exception as e:
            logger.warning("PORTAL init failed: %s — falling back to PIPEWIRE", e)
            self._method = CaptureMethod.PIPEWIRE
            yield from self._loop_pipewire(monitor_index)
            return

        w = wsc.width
        h = wsc.height
        while True:
            t0 = time.perf_counter()
            rgb = wsc.capture_frame_sync()
            if rgb is None:
                logger.warning("PORTAL stream ended, falling back to PIPEWIRE")
                self._release_portal()
                self._method = CaptureMethod.PIPEWIRE
                yield from self._loop_pipewire(monitor_index)
                return

            prev = self._prev_frames.get(monitor_index)
            diff = frame_diff_ratio(rgb, prev, threshold=12)
            self._prev_frames[monitor_index] = rgb
            yield CapturedFrame(
                data=rgb,
                monitor_index=monitor_index,
                timestamp=t0,
                region=(0, 0, w, h),
            )
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    def _release_portal(self) -> None:
        """Shut down the portal session synchronously."""
        if self._portal is None:
            return
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(lambda: asyncio.run(self._portal.shutdown()))
                future.result(timeout=10)
        else:
            asyncio.run(self._portal.shutdown())
        self._portal = None

    # ── FPS helper ──────────────────────────────────────────────────

    def _compute_fps(self, diff: float) -> float:
        if not self._fps_adaptive:
            return self._fps_target
        if diff < 0.001:
            self._idle_counter += 1
        else:
            self._idle_counter = 0
        if self._idle_counter > 10:
            return self._min_fps
        if diff < 0.01:
            return max(self._min_fps, self._fps_target * 0.3)
        return self._fps_target


# ---------------------------------------------------------------------------
# Convenience screenshot
# ---------------------------------------------------------------------------

_global_capture: ScreenCapture | None = None


def screenshot(monitor_index: int = 0) -> Image.Image:
    """Take a single screenshot.

    Uses a cached ``ScreenCapture`` instance for repeated calls.
    Call ``release_screenshot_capture()`` to free resources.
    """
    global _global_capture
    if _global_capture is None:
        _global_capture = ScreenCapture()
    frame = _global_capture.capture_one(monitor_index)
    return Image.fromarray(frame.data)


def release_screenshot_capture() -> None:
    """Release the global screenshot capture instance."""
    global _global_capture
    if _global_capture is not None:
        _global_capture.release()
        _global_capture = None


# -------------------------------------------------------------------------
# Helper: find system Python with GStreamer gi bindings
# -------------------------------------------------------------------------


def _find_system_python() -> str | None:
    """Find a Python interpreter that has GStreamer gi bindings."""
    import shutil
    import subprocess

    candidates = ["/usr/bin/python3", "/usr/bin/python"]
    for py in candidates:
        if not shutil.which(py):
            continue
        try:
            r = subprocess.run(
                [py, "-c", "import gi; gi.require_version('Gst', '1.0');"
                 "from gi.repository import Gst; Gst.init(None);"
                 "print('ok')"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                return py
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue

    return None
