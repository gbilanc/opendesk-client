"""
Cross-platform screen capture using ``mss`` (X11/Win/macOS)
and PipeWire (Wayland).

Provides frame differencing for bandwidth-efficient streaming and
automatic monitor enumeration.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from threading import Lock

import cv2
import mss
import numpy as np
from PIL import Image

from opendesk.core.platform_config import CaptureMethod, get_platform_config

logger = logging.getLogger(__name__)

# Sentinel per distinguere "posizione cursore mai osservata" da "cursore nascosto"
# (entrambi i casi non hanno coordinate).  Usato dal backend DXGI per decidere
# se emettere un frame anche a desktop fermo.
_UNSET_CURSOR = object()

# A desktop fermo Desktop Duplication non consegna frame: emettiamo comunque
# un frame GDI ogni _DXGI_IDLE_REFRESH_INTERVAL secondi (≈ l'_min_fps della
# policy adattiva) così l'idle-keyframe del client può riallineare il decoder
# e le richieste di keyframe vengono evase anche senza movimento.
_DXGI_IDLE_REFRESH_INTERVAL = 1.0


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


def _changed_mask(
    current: np.ndarray, previous: np.ndarray | None, threshold: int
) -> np.ndarray | None:
    """Boolean mask (H, W) of pixels that differ above *threshold*.

    Uses ``cv2.absdiff`` on uint8 instead of int16 arithmetic: one
    single-frame-sized temporary instead of 4× the frame size,
    which matters at 30 fps on full-HD/4K frames (RAM + GC churn).
    Returns ``None`` when there is no previous frame with a matching
    shape (the caller treats everything as "changed").
    """
    if previous is None or current.shape != previous.shape:
        return None
    diff = cv2.absdiff(current, previous)
    # cv2.max a due pass al posto di numpy ``.max(axis=2)``: la riduzione
    # "axis" di su array 3-canali è estremamente lenta in numpy ≥2.5
    # (~80 ms per frame full-HD), mentre cv2.max resta in pochi ms.
    d2 = cv2.max(diff[:, :, 0], diff[:, :, 1])
    return cv2.max(d2, diff[:, :, 2]) > threshold


def frame_diff_ratio(
    current: np.ndarray, previous: np.ndarray | None, threshold: int = 16
) -> float:
    """Fraction of pixels that differ above *threshold* (0.0…1.0)."""
    if previous is None or current.shape != previous.shape:
        return 1.0
    mask = _changed_mask(current, previous, threshold)
    assert mask is not None
    return float(mask.sum()) / mask.size


def compute_dirty_region(
    current: np.ndarray,
    previous: np.ndarray | None,
    threshold: int = 16,
) -> tuple[int, int, int, int] | None:
    """Bounding box of changed pixels (x0, y0, x1, y1).

    Reuses the same diff mask as ``frame_diff_ratio`` when called in
    sequence — caller should pre-compute the mask if both are needed.
    """
    if previous is None or current.shape != previous.shape:
        return (0, 0, current.shape[1], current.shape[0])
    mask = _changed_mask(current, previous, threshold)
    assert mask is not None
    coords = np.argwhere(mask)
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
        # State: None = unstarted, False = waiting for header, True = streaming
        self._header_ready: bool | None = None
        self._start_ts: float = 0.0
        self._pipewire_selected: bool = False  # true after first attempt

    # ── availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if GStreamer + pipewiresrc + GstApp are available on the system."""
        if self._available is not None:
            return self._available

        system_python = _find_system_python()
        if not system_python:
            logger.debug("PipeWire: no system Python with gi found")
            self._available = False
            return False

        import subprocess

        try:
            # Check GStreamer + pipewiresrc + GstApp (all needed by _pipewire_helper.py)
            r = subprocess.run(
                [
                    system_python,
                    "-c",
                    "import gi; gi.require_version('Gst', '1.0');"
                    "gi.require_version('GstApp', '1.0');"
                    "from gi.repository import Gst, GstApp; Gst.init(None);"
                    "e = Gst.ElementFactory.make('pipewiresrc', None);"
                    "exit(0 if e else 1)",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                self._available = True
                return True
            else:
                logger.debug("PipeWire: GStreamer check failed: %s", r.stderr.strip())
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug("PipeWire: GStreamer check exception: %s", e)

        logger.debug("PipeWire: GStreamer pipewiresrc not available")
        self._available = False
        return False

    # ── public API ──────────────────────────────────────────────────

    def start(self, monitor_index: int = 0) -> None:
        """Launch the GStreamer helper subprocess (non-blocking).

        Returns immediately after spawning.  The portal dialog may
        appear asynchronously — ``capture_one()`` will return None
        until the user approves the screen-selection dialog and the
        first frame header arrives.
        """
        if self._started:
            return
        self._started = True
        self._header_ready = False  # waiting
        self._start_ts = time.monotonic()
        self._pipewire_selected = True

        import subprocess
        from pathlib import Path

        helper = Path(__file__).parent / "_pipewire_helper.py"

        system_python = _find_system_python()
        if not system_python:
            logger.warning("PipeWire: system Python not found, cannot start")
            self._started = False
            self._header_ready = None  # failed
            return

        logger.info(
            "Starting PipeWire capture (monitor %d) via %s",
            monitor_index,
            helper,
        )

        self._helper_process = subprocess.Popen(
            [system_python, str(helper), "--monitor", str(monitor_index), "--fps", "30"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Make stdout non-blocking for header read attempts
        import fcntl

        if self._helper_process.stdout:
            fd = self._helper_process.stdout.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
        if self._helper_process.stderr:
            fd = self._helper_process.stderr.fileno()
            fl = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame | None:
        """Capture a single frame from the running subprocess.

        Returns None while waiting for portal approval (non-blocking).
        The caller should keep calling until frames arrive or a
        timeout is reached.
        """
        if not self._started:
            self.start(monitor_index)
            return None  # start() is non-blocking now

        # Still waiting for header?
        if self._header_ready is False:
            return self._try_read_header()

        # Failed (header_ready is None)
        if self._header_ready is None:
            return None

        # Streaming — read a frame
        if self._helper_process is None or self._helper_process.stdout is None:
            return None

        # Check if process is still alive
        poll = self._helper_process.poll()
        if poll is not None:
            err_text = ""
            try:
                if self._helper_process.stderr:
                    err_text = self._helper_process.stderr.read().decode(errors="replace")
            except Exception:
                pass
            logger.warning(
                "PipeWire helper exited with code %d: %s",
                poll,
                err_text.strip() or "(no output)",
            )
            self.release()
            return None

        # Lazy init frame buffer
        if not hasattr(self, "_frame_buf"):
            self._frame_buf = b""

        frame_size = self._resolved_w * self._resolved_h * 3
        needed = frame_size - len(self._frame_buf)
        try:
            chunk = self._helper_process.stdout.read(needed)
        except BlockingIOError:
            return None  # no data yet

        if chunk:
            self._frame_buf += chunk

        if len(self._frame_buf) < frame_size:
            return None  # still accumulating

        # Complete frame received
        data = self._frame_buf[:frame_size]
        self._frame_buf = self._frame_buf[frame_size:]  # keep overflow

        rgb = np.frombuffer(data, dtype=np.uint8).reshape(
            self._resolved_h,
            self._resolved_w,
            3,
        )
        return CapturedFrame(
            data=rgb.copy(),
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=(0, 0, self._resolved_w, self._resolved_h),
        )

    def _try_read_header(self) -> CapturedFrame | None:
        """Try to read the frame header non-blockingly.

        Accumulates partial reads until 8 bytes (width + height uint32 LE)
        are received.  Returns None until the header is complete or the
        helper fails.
        """
        import struct

        # Lazy init header buffer
        if not hasattr(self, "_header_buf"):
            self._header_buf = b""

        if self._helper_process is None or self._helper_process.stdout is None:
            self._header_ready = None
            return None

        # Check if helper died
        poll = self._helper_process.poll()
        if poll is not None:
            err_text = ""
            try:
                if self._helper_process.stderr:
                    err_text = self._helper_process.stderr.read().decode(errors="replace")
            except Exception:
                pass
            logger.error(
                "PipeWire helper exited with code %d: %s",
                poll,
                err_text.strip() or "(no output)",
            )
            self.release()
            self._header_ready = None
            return None

        # Timeout after 10 s
        if time.monotonic() - self._start_ts > 10.0:
            err_text = ""
            try:
                if self._helper_process.stderr:
                    err_text = self._helper_process.stderr.read().decode(errors="replace")
            except Exception:
                pass
            logger.error(
                "PipeWire: timed out waiting for portal approval (10 s). stderr: %s",
                err_text.strip() or "(no output)",
            )
            self.release()
            self._header_ready = None
            return None

        # Accumulate header bytes
        try:
            chunk = self._helper_process.stdout.read(8 - len(self._header_buf))
        except BlockingIOError:
            return None

        if chunk:
            self._header_buf += chunk

        if len(self._header_buf) < 8:
            return None

        self._resolved_w, self._resolved_h = struct.unpack("<II", self._header_buf[:8])
        self._header_buf = b""  # reset for next use
        self._header_ready = True
        logger.info(
            "PipeWire capture started: %dx%d",
            self._resolved_w,
            self._resolved_h,
        )
        return None

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

        import re
        import shutil
        import subprocess

        self._monitors = []

        if shutil.which("wlr-randr"):
            try:
                r = subprocess.run(
                    ["wlr-randr"],
                    capture_output=True,
                    text=True,
                    timeout=3,
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
                        self._monitors.append(
                            MonitorInfo(
                                index=idx,
                                name=current_name,
                                left=x,
                                top=y,
                                width=w,
                                height=h,
                                is_primary=idx == 0,
                            )
                        )
                        current_name = ""
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        if not self._monitors:
            self._monitors.append(
                MonitorInfo(
                    index=0,
                    name="Wayland Output",
                    left=0,
                    top=0,
                    width=1920,
                    height=1080,
                    is_primary=True,
                )
            )
        return self._monitors

    def release(self) -> None:
        """Stop the capture helper subprocess."""
        self._started = False
        self._header_ready = None
        self._pipewire_selected = False
        self._header_buf = b""
        self._frame_buf = b""
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

    @property
    def has_failed(self) -> bool:
        """True if PipeWire was tried and failed."""
        return self._pipewire_selected and self._header_ready is None and not self._started


# ---------------------------------------------------------------------------
# Backend auto-detection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Screen capture engine
# ---------------------------------------------------------------------------


class ScreenCapture:
    """Cross-platform screen capture engine.

    Uses ``PlatformConfig`` to select the best backend for the current
    platform.  The method can be overridden explicitly.

    Backend selection
    -----------------
    - **Wayland** → PORTAL (D-Bus + PipeWire) → PIPEWIRE → MSS (XWayland)
    - **X11** → MSS (via X11)
    - **Windows** → MSS (via DXGI)
    - **macOS** → MSS (via CoreGraphics)
    """

    def __init__(self, method: CaptureMethod | None = None) -> None:
        if method and method != CaptureMethod.AUTO:
            self._method = method
        else:
            self._method = get_platform_config().capture_method
        self._lock = Lock()
        self._sct: mss.mss | None = None
        self._pw: PipeWireCapture | None = None
        self._portal = None  # WaylandScreenCast — created lazily
        self._prev_frames: dict[int, np.ndarray] = {}
        # ── DXGI (Windows) ──
        self._dxgi: object | None = None  # dxcam camera, created lazily
        self._dxgi_monitor = -1
        # Ultima posizione globale del cursore già emessa in un frame.
        # _UNSET_CURSOR = mai osservata; None = cursore nascosto.
        self._last_cursor_gpos: object = _UNSET_CURSOR
        # Monotonic dell'ultimo frame emesso dal backend DXGI (per il
        # refresh periodico a desktop fermo).
        self._last_dxgi_frame_mono: float = 0.0
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
            return (
                pw.monitors()
                if pw.is_available()
                else [
                    MonitorInfo(
                        index=0,
                        name="Wayland Desktop",
                        left=0,
                        top=0,
                        width=1920,
                        height=1080,
                        is_primary=True,
                    )
                ]
            )
        sct = self._get_sct()
        return [
            MonitorInfo(
                index=i,
                name=m.get("name", f"Monitor {i}"),
                left=m["left"],
                top=m["top"],
                width=m["width"],
                height=m["height"],
                is_primary=m.get("is_primary", i == 0),
            )
            for i, m in enumerate(sct.monitors[1:])
        ]

    # ── single capture ──────────────────────────────────────────────

    def capture_one(self, monitor_index: int = 0) -> CapturedFrame | None:
        if self._method == CaptureMethod.PORTAL:
            return self._capture_portal(monitor_index)
        if self._method == CaptureMethod.DXGI:
            try:
                frame = self._capture_dxgi(monitor_index)
                if frame is not None:
                    return frame
                # None = nessun nuovo frame dal Desktop Duplication:
                # lo schermo non è cambiato, il chiamante ritenta.
                return None
            except Exception as e:
                logger.warning("DXGI capture failed (%s), falling back to MSS", e)
                self._method = CaptureMethod.MSS
        if self._method == CaptureMethod.PIPEWIRE:
            pw = self._get_pw()
            try:
                f = pw.capture_one(monitor_index)
                if f is not None:
                    return f
            except Exception as e:
                logger.warning("PipeWire capture failed: %s", e)
            # None means "still waiting" or "failed"; check which
            if pw.has_failed:
                logger.warning("PipeWire failed, falling back to MSS")
                self._method = CaptureMethod.MSS
            # else: still waiting for portal — return None, caller will retry
            return None
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
        elif self._method == CaptureMethod.DXGI:
            yield from self._loop_dxgi(monitor_index)
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
            self._release_dxgi()

    def __enter__(self) -> ScreenCapture:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    # ── internal: DXGI (Windows Desktop Duplication via dxcam) ─────

    def _get_dxgi(self, monitor_index: int):
        """Lazily create the dxcam camera for *monitor_index*.

        Returns None if dxcam is not installed or no output exists.
        """
        if self._dxgi is not None and self._dxgi_monitor == monitor_index:
            return self._dxgi
        if self._dxgi is not None:
            self._release_dxgi()
        try:
            import dxcam

            cam = dxcam.create(output_idx=monitor_index, output_color="RGB")
        except Exception as e:
            logger.warning("dxcam init failed: %s", e)
            return None
        if cam is None:
            logger.warning("dxcam.create() returned None (no output %d)", monitor_index)
            return None
        self._dxgi = cam
        self._dxgi_monitor = monitor_index
        return cam

    def _capture_dxgi(self, monitor_index: int = 0) -> CapturedFrame | None:
        cam = self._get_dxgi(monitor_index)
        if cam is None:
            raise RuntimeError("dxcam not available")
        # Geometria monitor da mss (solo enumerazione, nessuna cattura).
        sct = self._get_sct()
        mon = sct.monitors[monitor_index + 1]
        region = (mon["left"], mon["top"], mon["width"], mon["height"])

        # grab() returns None quando lo schermo non è cambiato.
        rgb = cam.grab()
        now = time.monotonic()
        if rgb is None:
            # Desktop fermo: Desktop Duplication non consegna frame e NON
            # include il cursore nel buffer.  Se l'unico cambiamento è il
            # puntatore, nessun frame arriverebbe all'encoder e il client
            # resterebbe su un'immagine congelata (percepita come stream
            # bloccato).  Rileviamo lo spostamento del cursore e, in quel
            # caso, catturiamo un frame via GDI (MSS) con il cursore
            # compositato: il diff frame invia da solo i tile delle aree
            # posizione-vecchia / posizione-nuova.  Inoltre emettiamo un
            # frame periodico (~1/s) anche senza movimento, così il flusso
            # resta vivo e il client può richiedere/ricevere keyframe.
            state = _cursor_state()
            gpos = state[0] if state is not None else None
            cursor_moved = gpos != self._last_cursor_gpos
            idle_refresh_due = now - self._last_dxgi_frame_mono >= _DXGI_IDLE_REFRESH_INTERVAL
            if cursor_moved or idle_refresh_due:
                self._last_cursor_gpos = gpos
                self._last_dxgi_frame_mono = now
                return self._capture_mss(monitor_index)
            return None

        rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
        rgb = draw_cursor_on_frame(rgb, region)
        state = _cursor_state()
        self._last_cursor_gpos = state[0] if state is not None else None
        self._last_dxgi_frame_mono = now
        return CapturedFrame(
            data=rgb,
            monitor_index=monitor_index,
            timestamp=time.time(),
            region=region,
        )

    def _loop_dxgi(self, monitor_index: int = 0) -> Iterator[CapturedFrame]:
        """DXGI capture loop con fps adattivo (stessa policy di _loop_mss)."""
        sct = self._get_sct()
        mon = sct.monitors[monitor_index + 1]
        diff = 1.0
        while True:
            t0 = time.perf_counter()
            cam = self._get_dxgi(monitor_index)
            if cam is None:
                yield from self._loop_mss(monitor_index)
                return
            rgb = cam.grab()
            if rgb is None:
                # Nessun nuovo frame: breve sleep per non bruciare CPU/GPU.
                time.sleep(0.005)
            else:
                rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
                prev = self._prev_frames.get(monitor_index)
                diff = frame_diff_ratio(rgb, prev, threshold=12)
                self._prev_frames[monitor_index] = rgb
                yield CapturedFrame(
                    data=rgb,
                    monitor_index=monitor_index,
                    timestamp=t0,
                    region=(mon["left"], mon["top"], mon["width"], mon["height"]),
                )
            elapsed = time.perf_counter() - t0
            sleep_needed = max(0.0, (1.0 / self._compute_fps(diff)) - elapsed)
            if sleep_needed > 0:
                time.sleep(sleep_needed)

    def _release_dxgi(self) -> None:
        if self._dxgi is not None:
            release = getattr(self._dxgi, "release", None)
            if callable(release):
                try:
                    release()
                except Exception as e:
                    logger.debug("dxcam release failed: %s", e)
            self._dxgi = None
            self._dxgi_monitor = -1

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
            frame = np.ascontiguousarray(buf[:, :, :3])
            frame = draw_cursor_on_frame(
                frame, (mon["left"], mon["top"], mon["width"], mon["height"])
            )
            return CapturedFrame(
                data=frame,
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
                data=rgb,
                monitor_index=monitor_index,
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
        consecutive_none = 0
        while True:
            t0 = time.perf_counter()
            frame = pw.capture_one(monitor_index)
            if frame is None:
                if pw.has_failed:
                    logger.warning("PipeWire failed, falling back to MSS")
                    yield from self._loop_mss(monitor_index)
                    return
                # Still waiting for portal — brief sleep, keep trying
                consecutive_none += 1
                if consecutive_none > 300:  # ~10 s at 30 fps
                    logger.warning("PipeWire timed out, falling back to MSS")
                    yield from self._loop_mss(monitor_index)
                    return
                time.sleep(0.01)
                continue
            consecutive_none = 0
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
        capture.  Has a 30-second timeout for the portal dialog.
        """
        if self._portal is not None:
            return self._portal

        import asyncio

        from opendesk.core.wayland_capture import WaylandScreenCast

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
                ok = future.result(timeout=30)
        else:
            ok = asyncio.run(wsc.setup())

        if not ok:
            raise RuntimeError("WaylandScreenCast setup failed (portal rejected or timed out)")

        self._portal = wsc
        return wsc

    def _capture_portal(self, monitor_index: int = 0) -> CapturedFrame:
        """Capture a single frame via the PORTAL backend.

        On any failure (timeout, no frames, helper crash) falls back
        transparently to PIPEWIRE.
        """
        try:
            wsc = self._get_portal()
        except Exception as e:
            logger.warning("PORTAL init failed: %s — falling back to PIPEWIRE", e)
            self._release_portal()
            self._method = CaptureMethod.PIPEWIRE
            return self.capture_one(monitor_index)

        rgb = wsc.capture_frame_sync()
        if rgb is None:
            logger.warning("PORTAL capture returned None — falling back to PIPEWIRE")
            self._release_portal()
            self._method = CaptureMethod.PIPEWIRE
            return self.capture_one(monitor_index)

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
# Cursor compositing (Windows)
# -------------------------------------------------------------------------
#
# DXGI Desktop Duplication (dxcam) e MSS (GDI BitBlt) NON compositano il
# cursore nel frame catturato: l'utente remoto non vede mai il puntatore
# muoversi sul proprio schermo.  A schermo statico (es. dopo la chiusura
# della finestra host) l'unico cambiamento visivo è il cursore: senza
# compositing l'immagine resta congelata sull'ultimo frame e il client
# percepisce la sessione come "bloccata" (input sembrato morto).
#
# Soluzione: disegnare il cursore sui pixel del frame prima dell'encode.
# Il diff frame dell'encoder individua automaticamente le aree (posizione
# vecchia + nuova) e invia i tile corrispondenti — nessuna modifica al
# protocollo né al decoder client.

_CURSOR_CACHE: dict[int, tuple[int, int, np.ndarray]] = {}  # hCursor → (w, h, rgba)


# GetCursorInfo / CURSOR_INFO
_CURSOR_SHOWING = 0x1  # CURSOR_SHOWING: il cursore è visibile


def _cursor_state() -> tuple[tuple[int, int], int] | None:
    """Posizione globale (x, y) e handle HCURSOR del cursore, se visibile.

    Windows-only; su altre piattaforme restituisce ``None``.
    """
    if not sys.platform.startswith("win"):
        return None
    import ctypes
    from ctypes import wintypes

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", wintypes.HANDLE),
            ("ptScreenPos", wintypes.POINT),
        ]

    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(ci)):
        return None
    if not (ci.flags & _CURSOR_SHOWING) or not ci.hCursor:
        return None
    return (ci.ptScreenPos.x, ci.ptScreenPos.y), ci.hCursor


def _cursor_rgba(hcursor: int) -> tuple[int, int, np.ndarray] | None:
    """Bitmap RGBA (h, w, 4) del cursore, con cache per handle.

    Usa GetIconInfo + GetDIBits (BGRA top-down); reso RGB con alpha.
    """
    cached = _CURSOR_CACHE.get(hcursor)
    if cached is not None:
        return cached
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32

        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        ii = ICONINFO()
        if not user32.GetIconInfo(wintypes.HICON(hcursor), ctypes.byref(ii)):
            return None
        try:
            # hbmColor può essere NULL per cursori monocromatici: usa mask
            use_mask = not ii.hbmColor
            hbmp = ii.hbmMask if use_mask else ii.hbmColor
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            if not gdi32.GetObjectW(wintypes.HANDLE(hbmp), 0, None):
                pass

            # GetObject via BITMAP struct per dimensioni
            class BITMAP(ctypes.Structure):
                _fields_ = [
                    ("bmType", ctypes.c_long),
                    ("bmWidth", ctypes.c_long),
                    ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long),
                    ("bmPlanes", wintypes.WORD),
                    ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p),
                ]

            bm = BITMAP()
            if not gdi32.GetObjectW(
                wintypes.HANDLE(hbmp), ctypes.sizeof(BITMAP), ctypes.byref(bm)
            ):
                return None
            w, h = bm.bmWidth, bm.bmHeight
            if w <= 0 or h <= 0 or w > 256 or h > 256:
                return None

            bmi.biWidth = w
            bmi.biHeight = -h  # top-down
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = 0  # BI_RGB
            buf = (ctypes.c_ubyte * (w * h * 4))()
            hdc = user32.GetDC(None)
            try:
                got = gdi32.GetDIBits(
                    wintypes.HDC(hdc),
                    wintypes.HBITMAP(hbmp),
                    0,
                    h,
                    buf,
                    ctypes.byref(bmi),
                    0,  # DIB_RGB_COLORS
                )
            finally:
                user32.ReleaseDC(None, hdc)
            if got != h:
                return None
            arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4).copy()
            if use_mask:
                # Monocromatico: AND mask (R plane) + XOR (G plane)
                and_mask = arr[:, :, 0]
                xor_mask = arr[:, :, 1]
                opaque = xor_mask > 0
                semi = (and_mask == 0) & ~opaque
                alpha = np.where(opaque, 255, np.where(semi, 255, 0)).astype(np.uint8)
                color = np.where(opaque[..., None], 255, 0).astype(np.uint8)
                arr = np.dstack([color] * 3 + [alpha])
            else:
                arr = arr[:, :, [2, 1, 0, 3]]  # BGRA → RGBA
            result = (h, w, arr)
            # Cache limitata: mantieni solo l'ultimo cursore
            _CURSOR_CACHE.clear()
            _CURSOR_CACHE[hcursor] = result
            return result
        finally:
            if ii.hbmMask:
                ctypes.windll.gdi32.DeleteObject(wintypes.HBITMAP(ii.hbmMask))
            if ii.hbmColor:
                ctypes.windll.gdi32.DeleteObject(wintypes.HBITMAP(ii.hbmColor))
    except Exception as e:
        logger.debug("cursor bitmap failed: %s", e)
        return None


def draw_cursor_on_frame(rgb: np.ndarray, region: tuple[int, int, int, int]) -> np.ndarray:
    """Composita il cursore di sistema sui pixel del frame RGB.

    Parameters
    ----------
    rgb : np.ndarray
        Frame RGB uint8 (H, W, 3) — viene modificato in place.
    region : tuple[int, int, int, int]
        ``(left, top, width, height)`` del monitor catturato.

    Returns
    -------
    np.ndarray
        Il frame (stesso oggetto) con il cursore disegnato, se visibile
        e dentro il monitor; altrimenti invariato.
    """
    if not rgb.flags.writeable:
        rgb = rgb.copy()
    state = _cursor_state()
    if state is None:
        return rgb
    (gx, gy), hcursor = state
    left, top, width, height = region
    fx, fy = gx - left, gy - top
    if fx < 0 or fy < 0 or fx >= width or fy >= height:
        return rgb  # cursore su un altro monitor
    shape = _cursor_rgba(hcursor)
    if shape is None:
        # Fallback: puntatore schematizzato (cerchio pieno + contorno)
        r = 6
        y0, y1 = max(0, fy - r), min(rgb.shape[0], fy + r + 1)
        x0, x1 = max(0, fx - r), min(rgb.shape[1], fx + r + 1)
        if y1 > y0 and x1 > x0:
            yy, xx = np.ogrid[y0:y1, x0:x1]
            dist2 = (yy - fy) ** 2 + (xx - fx) ** 2
            inside = dist2 <= r * r
            edge = (dist2 <= r * r) & (dist2 >= (r - 2) * (r - 2))
            patch = rgb[y0:y1, x0:x1]
            patch[inside] = (255, 255, 255)
            patch[edge] = (20, 20, 20)
        return rgb
    ch, cw, rgba = shape
    y0, x0 = max(0, fy), max(0, fx)
    y1 = min(rgb.shape[0], fy + ch)
    x1 = min(rgb.shape[1], fx + cw)
    if y1 <= y0 or x1 <= x0:
        return rgb
    sy0, sx0 = y0 - fy, x0 - fx
    patch = rgba[sy0 : sy0 + (y1 - y0), sx0 : sx0 + (x1 - x0)]
    alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
    dest = rgb[y0:y1, x0:x1]
    blended = (
        patch[:, :, :3].astype(np.float32) * alpha + dest.astype(np.float32) * (1.0 - alpha)
    ).astype(np.uint8)
    dest[:] = blended
    return rgb


# -------------------------------------------------------------------------
# Helper: find system Python with GStreamer gi bindings
# -------------------------------------------------------------------------


def _find_system_python() -> str | None:
    """Find a Python interpreter that has GStreamer + GstApp gi bindings.

    Delegates to ``platform_config._find_system_python_gi`` to avoid
    duplicating the detection logic.
    """
    from opendesk.core.platform_config import _find_system_python_gi

    return _find_system_python_gi()
