"""
Full Wayland screen capture via xdg-desktop-portal D-Bus API.

Implements the complete ScreenCast portal protocol:
1. CreateSession -> selects monitor(s) -> Start -> receives PipeWire fd
2. Delegates actual frame capture to ``_pipewire_helper.py`` (GStreamer
   subprocess), passing the portal-issued fd so GStreamer reuses the
   existing session.

Requires:
    - ``dbus-next`` (pure Python D-Bus client)
    - ``xdg-desktop-portal`` + compositor-specific backend
    - PipeWire + GStreamer with pipewire plugin
    - A system Python with ``gi`` (GObject Introspection) bindings

Usage::

    capturer = WaylandScreenCast()
    if await capturer.setup():
        frame = await capturer.capture_frame()
        ...
    await capturer.shutdown()
"""

from __future__ import annotations

import asyncio
import logging
import os
import select
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class WaylandCaptureSession:
    """Active D-Bus screencast session state."""

    session_handle: str = ""
    pipewire_node: int = 0
    pipewire_fd: int = -1
    width: int = 0
    height: int = 0


class WaylandScreenCast:
    """Wayland screen capture via the xdg-desktop-portal ScreenCast API.

    This is the **real** Wayland capture path used by modern Linux
    desktop environments (GNOME, KDE, wlroots-based compositors).

    The flow is:
    1. Create D-Bus session with ScreenCast portal
    2. Select the monitor(s) to capture
    3. Start the session → receive a PipeWire node + fd
    4. Read frames from PipeWire stream
    """

    def __init__(self) -> None:
        self._session: WaylandCaptureSession | None = None
        self._bus = None
        self._request_token: int = 0
        self._available: bool | None = None
        # Subprocess-based GStreamer reader
        self._helper_process: subprocess.Popen | None = None
        self._helper_width: int = 0
        self._helper_height: int = 0

    # ── availability ────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if the system supports D-Bus screencast.

        Checks for:
        - ``dbus-next`` Python package
        - ``org.freedesktop.portal.Desktop`` on the session D-Bus
        """
        if self._available is not None:
            return self._available

        try:
            import dbus_next  # noqa: F401
        except ImportError:
            logger.debug("Wayland screencast: dbus-next not installed")
            self._available = False
            return False

        import subprocess
        try:
            r = subprocess.run(
                ["busctl", "list", "--no-pager"],
                capture_output=True, text=True, timeout=2,
            )
            if "org.freedesktop.portal.Desktop" in r.stdout:
                self._available = True
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        self._available = False
        return False

    # ── lifecycle ───────────────────────────────────────────────────

    async def setup(self) -> bool:
        """Initialise the screencast session.

        Returns ``True`` if the session is ready for frame capture.
        """
        if not self.is_available():
            logger.warning("Wayland screencast not available")
            return False

        try:
            await self._create_session()
            await self._select_sources()
            await self._start_session()
            logger.info("Wayland screencast session ready")
            return True
        except Exception as e:
            logger.error("Wayland screencast setup failed: %s", e)
            return False

    async def capture_frame(self) -> np.ndarray | None:
        """Capture a single frame from the PipeWire stream.

        Returns an RGB uint8 numpy array, or ``None`` on failure.
        """
        if self._session is None:
            return None

        try:
            return await self._read_pipewire_frame()
        except Exception as e:
            logger.warning("Wayland frame capture failed: %s", e)
            return None

    def capture_frame_sync(self) -> np.ndarray | None:
        """Synchronous variant for use in non-async contexts.

        Reads the next raw RGB frame from the GStreamer subprocess stdout.
        """
        if self._helper_process is None:
            return None
        if self._helper_height == 0 or self._helper_width == 0:
            return None

        frame_size = self._helper_width * self._helper_height * 3
        stdout = self._helper_process.stdout
        if stdout is None:
            return None

        data = stdout.read(frame_size)
        if len(data) < frame_size:
            logger.warning(
                "Wayland: incomplete frame %d / %d bytes",
                len(data), frame_size,
            )
            return None

        return np.frombuffer(data, dtype=np.uint8).reshape(
            self._helper_height, self._helper_width, 3,
        ).copy()

    @property
    def width(self) -> int:
        return self._helper_width

    @property
    def height(self) -> int:
        return self._helper_height

    async def shutdown(self) -> None:
        """Close the screencast session."""
        # Stop the GStreamer helper first
        self._stop_helper()

        if self._session and self._session.session_handle:
            try:
                msg = self._make_msg(
                    self._session.session_handle,
                    "org.freedesktop.portal.Session",
                    "Close",
                )
                await self._bus.call(msg)
            except Exception:
                pass

        if self._session and self._session.pipewire_fd >= 0:
            try:
                os.close(self._session.pipewire_fd)
            except Exception:
                pass

        self._session = None
        if self._bus:
            self._bus.disconnect()
            self._bus = None
        logger.info("Wayland screencast shutdown")

    def _stop_helper(self) -> None:
        """Terminate the GStreamer helper subprocess."""
        if self._helper_process is None:
            return
        try:
            self._helper_process.terminate()
            self._helper_process.wait(timeout=3)
        except Exception:
            try:
                self._helper_process.kill()
            except Exception:
                pass
        self._helper_process = None
        self._helper_width = 0
        self._helper_height = 0

    # ── internal D-Bus protocol ─────────────────────────────────────

    async def _ensure_bus(self) -> None:
        if self._bus is not None:
            return
        from dbus_next import BusType, Message
        from dbus_next.aio import MessageBus

        self._bus = await MessageBus(bus_type=BusType.SESSION).connect()

    def _make_msg(
        self, path: str, interface: str, member: str,
        signature: str = "", body: list | None = None,
    ) -> Any:  # noqa: ANN401
        from dbus_next import Message
        return Message(
            destination="org.freedesktop.portal.Desktop",
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )

    async def _create_session(self) -> None:
        """Create a ScreenCast session via D-Bus."""
        await self._ensure_bus()
        from dbus_next import Message

        self._request_token += 1
        token = f"opendesk{self._request_token}"
        sender_name = self._bus.unique_name[1:].replace(".", "_")

        request_path = f"/org/freedesktop/portal/desktop/request/{sender_name}/{token}"

        # Handle response via Signal
        def _on_signal(signal_name: str, args: list) -> None:
            if signal_name == "Response" and args:
                logger.debug("CreateSession response: %s", args)

        self._bus.on_signal(
            "org.freedesktop.portal.Request",
            "Response",
            _on_signal,
            path=request_path,
        )

        msg = self._make_msg(
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "CreateSession",
            "a{sv}",
            [{
                "session_handle_token": ("s", token),
            }],
        )
        response = await self._bus.call(msg)
        if response.body:
            self._session = WaylandCaptureSession(
                session_handle=response.body[0],
            )
            logger.info("ScreenCast session created: %s", self._session.session_handle)
        else:
            raise RuntimeError("Failed to create ScreenCast session")

    async def _select_sources(self) -> None:
        """Select which monitor(s) to capture."""
        if self._session is None:
            raise RuntimeError("No session")

        msg = self._make_msg(
            self._session.session_handle,
            "org.freedesktop.portal.ScreenCast",
            "SelectSources",
            "a{sv}",
            [{
                "types": ("u", 1),  # 1 = MONITOR (not WINDOW)
                "multiple": ("b", False),
            }],
        )
        await self._bus.call(msg)
        logger.debug("ScreenCast sources selected")

    async def _start_session(self) -> None:
        """Start the screencast session, receive PipeWire fd, launch helper."""
        if self._session is None:
            raise RuntimeError("No session")

        msg = self._make_msg(
            self._session.session_handle,
            "org.freedesktop.portal.ScreenCast",
            "Start",
            "a{sv}",
            [{"handle_token": ("s", "start")}],
        )
        response = await self._bus.call(msg)
        if not response.body:
            raise RuntimeError("Failed to start ScreenCast session")

        results = response.body[0]
        pw_node_id = results.get("pipewire_node_id", ("u", 0))[1]
        pw_fd = results.get("pipewire_fd", ("h", -1))[1]

        # Prefer the fd passed as ancillary data
        if hasattr(response, "unix_fds") and response.unix_fds:
            pw_fd = response.unix_fds[0]

        self._session.pipewire_node = pw_node_id
        self._session.pipewire_fd = pw_fd
        logger.info("PipeWire node: %d, fd: %d", pw_node_id, pw_fd)

        # ------------------------------------------------------------------
        # Launch the GStreamer helper subprocess with the portal-issued fd.
        # This reuses the existing portal session so the user does NOT need
        # to approve a second screen-selection dialog.
        # ------------------------------------------------------------------
        await self._launch_pipewire_helper()

    # ── GStreamer helper subprocess ─────────────────────────────────

    async def _launch_pipewire_helper(self) -> None:
        """Launch ``_pipewire_helper.py`` subprocess to capture frames.

        Passes the portal-issued PipeWire fd so GStreamer reuses the
        existing session instead of opening a new one.
        """
        if self._session is None or self._session.pipewire_fd < 0:
            raise RuntimeError("No PipeWire fd available")

        from opendesk.core.screen_capture import _find_system_python

        system_python = _find_system_python()
        if not system_python:
            raise RuntimeError(
                "No system Python with GStreamer gi bindings found. "
                "Install python3-gi and gstreamer1.0-pipewire."
            )

        helper = Path(__file__).parent / "_pipewire_helper.py"
        logger.info("Launching PipeWire helper: %s with fd=%d", helper, self._session.pipewire_fd)

        self._helper_process = subprocess.Popen(
            [
                system_python, str(helper),
                "--fd", str(self._session.pipewire_fd),
                "--fps", "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(self._session.pipewire_fd,),
        )

        # Read header: 8 bytes → width, height (uint32 LE)
        header = b""
        deadline = time.monotonic() + 30.0
        while len(header) < 8 and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            r, _, _ = select.select(
                [self._helper_process.stdout], [], [], min(remaining, 1.0),
            )
            if r:
                chunk = self._helper_process.stdout.read(8 - len(header))
                if not chunk:
                    break
                header += chunk

        if len(header) < 8:
            self._stop_helper()
            stderr = self._helper_process and self._helper_process.stderr
            if stderr:
                err = stderr.read().decode(errors="replace")
                logger.warning("PipeWire helper stderr: %s", err)
            raise RuntimeError("PipeWire helper did not produce a frame header")

        self._helper_width, self._helper_height = struct.unpack("<II", header)
        logger.info(
            "PipeWire helper stream: %dx%d",
            self._helper_width, self._helper_height,
        )

    async def _read_pipewire_frame(self) -> np.ndarray | None:
        """Read a single RGB frame from the GStreamer helper stdout."""
        # Offload to a thread so we don't block the event loop on I/O
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.capture_frame_sync)


# ---------------------------------------------------------------------------
# Fallback: subprocess-based Wayland capture
# ---------------------------------------------------------------------------


async def capture_wayland_subprocess() -> np.ndarray | None:
    """Fallback Wayland capture using ``grim`` + ``convert`` subprocess.

    Very slow (one screenshot at a time) but works everywhere.
    """
    import asyncio
    import shutil

    if not shutil.which("grim"):
        logger.debug("grim not found, trying import...")
        return None

    try:
        # grim outputs PNG to stdout
        proc = await asyncio.create_subprocess_exec(
            "grim", "-t", "png", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        png_data, stderr = await proc.communicate()
        if proc.returncode != 0 or not png_data:
            return None

        from PIL import Image
        import io
        img = Image.open(io.BytesIO(png_data))
        return np.array(img.convert("RGB"))

    except Exception as e:
        logger.debug("grim capture failed: %s", e)
        return None
