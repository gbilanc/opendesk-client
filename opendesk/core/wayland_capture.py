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
        - GObject Introspection (needed by the PipeWire helper subprocess)
        """
        if self._available is not None:
            return self._available

        try:
            import dbus_next  # noqa: F401
        except ImportError:
            logger.debug("Wayland screencast: dbus-next not installed")
            self._available = False
            return False

        # Also need gi bindings for the GStreamer helper subprocess
        from opendesk.core.platform_config import _find_system_python_gi
        if _find_system_python_gi() is None:
            logger.debug(
                "Wayland screencast: no system Python with gi bindings"
            )
            self._available = False
            return False

        import subprocess
        try:
            r = subprocess.run(
                ["busctl", "--user", "list", "--no-pager"],
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

    @staticmethod
    def _v(sig: str, value):  # noqa: ANN401
        """Shortcut for dbus-next Variant creation."""
        from dbus_next import Variant
        return Variant(sig, value)

    async def _wait_for_response(self, request_path: str, timeout: float = 30.0) -> tuple[int, dict]:
        """Wait for a portal ``Response`` signal on *request_path*.

        Returns a ``(response_code, results)`` tuple where *results*
        is a plain ``dict`` with Python-native values (Variants unwrapped).
        ``response_code``: 0 = success, 1 = cancelled, 2 = other error.
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def _handler(msg):
            if future.done():
                return
            if msg.interface == "org.freedesktop.portal.Request" and \
               msg.member == "Response" and \
               msg.path == request_path:
                code = msg.body[0] if msg.body else 2
                raw = msg.body[1] if len(msg.body) > 1 else {}
                # Unwrap Variant values to plain Python
                results = {}
                for k, v in raw.items():
                    results[k] = v.value if hasattr(v, "value") else v
                future.set_result((code, results))

        self._bus.add_message_handler(_handler)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"Timeout waiting for portal response on {request_path}"
            )
        finally:
            self._bus.remove_message_handler(_handler)

    async def _create_session(self) -> None:
        """Create a ScreenCast session via D-Bus.

        On GNOME 48 the portal uses the async request pattern:
        ``CreateSession`` returns a *request* path immediately;
        the real session handle arrives via a ``Response`` signal.
        """
        await self._ensure_bus()

        self._request_token += 1
        token = f"opendesk{self._request_token}"

        msg = self._make_msg(
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "CreateSession",
            "a{sv}",
            [{
                "session_handle_token": self._v("s", token),
            }],
        )
        response = await self._bus.call(msg)
        if not response.body:
            raise RuntimeError("CreateSession returned empty body")

        request_path = response.body[0]
        logger.debug("CreateSession request path: %s", request_path)

        # Wait for the Response signal — the session handle is in results
        code, results = await self._wait_for_response(request_path)
        if code != 0:
            raise RuntimeError(
                f"CreateSession rejected (response={code})"
            )

        session_handle = results.get("session_handle")
        if not session_handle:
            raise RuntimeError(
                f"CreateSession response missing session_handle: {results}"
            )

        self._session = WaylandCaptureSession(
            session_handle=str(session_handle),
        )
        logger.info("ScreenCast session created: %s", self._session.session_handle)

    async def _select_sources(self) -> None:
        """Select which monitor(s) to capture.

        Called on the main portal object with the session handle
        as the first argument.  Returns a request path and then
        waits for the ``Response`` signal before proceeding.
        """
        if self._session is None:
            raise RuntimeError("No session")

        msg = self._make_msg(
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "SelectSources",
            "oa{sv}",
            [
                self._session.session_handle,
                {
                    "types": self._v("u", 1),  # 1 = MONITOR
                    "multiple": self._v("b", False),
                },
            ],
        )
        response = await self._bus.call(msg)
        if not response.body:
            raise RuntimeError("SelectSources returned empty body")

        request_path = response.body[0]
        logger.debug("SelectSources request path: %s", request_path)

        # Wait for the Response signal on the request path
        code, results = await self._wait_for_response(request_path)
        if code != 0:
            raise RuntimeError(
                f"SelectSources rejected (response={code})"
            )
        logger.debug("ScreenCast sources selected")

    async def _start_session(self) -> None:
        """Start the screencast session and launch the GStreamer helper.

        Called on the main portal object.  The ``Start`` response
        contains ``streams`` — each stream has a PipeWire node ID
        that we pass to ``pipewiresrc`` via its ``path`` property.
        """
        if self._session is None:
            raise RuntimeError("No session")

        msg = self._make_msg(
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.ScreenCast",
            "Start",
            "osa{sv}",
            [
                self._session.session_handle,
                "",  # parent_window
                {},
            ],
        )
        response = await self._bus.call(msg)
        if not response.body:
            raise RuntimeError("ScreenCast Start returned empty body")
        request_handle = response.body[0]
        logger.debug("Start request handle: %s", request_handle)

        # Wait for the Start Response signal
        code, results = await self._wait_for_response(request_handle)
        if code != 0:
            raise RuntimeError(
                f"ScreenCast Start rejected (response={code})"
            )

        # Extract streams: a(sa{sv}) → list of (node_id, properties)
        streams = results.get("streams", [])
        if not streams:
            raise RuntimeError("Start returned no streams")

        # First stream: (uint32 node_id, dict properties)
        pw_node_id = streams[0][0] if isinstance(streams[0], (list, tuple)) else streams[0]
        properties = streams[0][1] if isinstance(streams[0], (list, tuple)) and len(streams[0]) > 1 else {}

        # Extract resolution from properties
        size = properties.get("size")
        if size and isinstance(size, (list, tuple)) and len(size) == 2:
            self._session.width, self._session.height = int(size[0]), int(size[1])

        self._session.pipewire_node = int(pw_node_id)
        logger.info(
            "PipeWire node: %d, resolution: %dx%d",
            self._session.pipewire_node,
            self._session.width,
            self._session.height,
        )

        # Launch the GStreamer helper — uses pipewiresrc path=<node_id>
        await self._launch_pipewire_helper()

    # ── GStreamer helper subprocess ─────────────────────────────────

    async def _launch_pipewire_helper(self) -> None:
        """Launch ``_pipewire_helper.py`` subprocess to capture frames.

        Passes the PipeWire node ID from the portal so GStreamer's
        ``pipewiresrc`` connects directly to the correct stream.
        """
        if self._session is None or self._session.pipewire_node <= 0:
            raise RuntimeError("No PipeWire node available")

        from opendesk.core.platform_config import _find_system_python_gi

        system_python = _find_system_python_gi()
        if not system_python:
            raise RuntimeError(
                "No system Python with GStreamer gi bindings found. "
                "Install python3-gi and gstreamer1.0-pipewire."
            )

        helper = Path(__file__).parent / "_pipewire_helper.py"
        node_id = self._session.pipewire_node
        logger.info("Launching PipeWire helper: %s with node=%d", helper, node_id)

        # Use pipewiresrc path=<node_id> to connect to the portal stream
        self._helper_process = subprocess.Popen(
            [
                system_python, str(helper),
                "--node-id", str(node_id),
                "--fps", "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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
            # Capture stderr before stopping the process
            err_output = ""
            if self._helper_process and self._helper_process.stderr:
                try:
                    err_output = self._helper_process.stderr.read().decode(errors="replace")
                except Exception:
                    pass
            self._stop_helper()
            if err_output:
                logger.warning("PipeWire helper stderr: %s", err_output)
            raise RuntimeError(
                f"PipeWire helper did not produce a frame header (got {len(header)} bytes). "
                "Check that xdg-desktop-portal and GStreamer pipewire plugin are installed."
            )

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
