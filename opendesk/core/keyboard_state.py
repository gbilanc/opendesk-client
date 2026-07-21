"""
Caps Lock / keyboard modifier state detection.

Provides a cross-platform helper to check whether Caps Lock is
currently active.  Uses Xlib on X11, /sys/class/leds on Linux
Wayland, Win32 API on Windows.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform-specific checkers
# ---------------------------------------------------------------------------

_Checker = Callable[[], bool] | None
_checker: _Checker = None


def _check_x11() -> bool:
    """Check Caps Lock via Xlib (X11 / Linux).

    The Display connection is opened once and reused across calls
    to avoid blocking on a fresh X11 handshake every 500 ms.
    """
    # Reuse the Display across calls so we don't pay the handshake
    # cost every 500 ms when the caps-lock timer fires.
    if not hasattr(_check_x11, "_display"):
        try:
            from Xlib import display as xdisplay  # type: ignore[import-untyped]
            _check_x11._display = xdisplay.Display()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Xlib Display open failed, falling back", exc_info=True)
            return _check_sys_leds()

    try:
        d = _check_x11._display  # type: ignore[attr-defined]
        state = d.get_keyboard_control()._data["led_mask"]  # noqa: SLF001
        return bool(state & 1)
    except Exception:
        logger.debug("Xlib Caps Lock check failed, falling back", exc_info=True)
        return _check_sys_leds()


def _check_sys_leds() -> bool:
    """Check Caps Lock via /sys/class/leds (Linux, works on Wayland too).

    Reads the brightness file of the capslock LED.  This is the
    simplest cross-display-server approach on Linux.
    """
    try:
        base = Path("/sys/class/leds")
        if not base.exists():
            return False
        for entry in base.iterdir():
            if "capslock" in entry.name.lower():
                brightness = entry / "brightness"
                if brightness.exists():
                    val = brightness.read_text().strip()
                    return val == "1"
        return False
    except (OSError, PermissionError):
        logger.debug("/sys/class/leds Caps Lock check failed", exc_info=True)
        return False


def _check_win32() -> bool:
    """Check Caps Lock via Win32 API."""
    try:
        import ctypes
        return bool(ctypes.windll.user32.GetKeyState(0x14) & 0x0001)
    except Exception:
        logger.debug("Win32 Caps Lock check failed", exc_info=True)
        return False


def _check_subprocess() -> bool:
    """Fallback: parse ``xset -q`` output (X11 only)."""
    try:
        out = subprocess.check_output(
            ["xset", "-q"], stderr=subprocess.STDOUT, timeout=2, text=True,
        )
        for line in out.splitlines():
            if "Caps Lock" in line:
                return "on" in line.lower()
    except Exception:
        logger.debug("xset -q Caps Lock check failed", exc_info=True)
    return False


def _init_checker() -> _Checker:
    """Select the best Caps Lock checker for the current platform."""
    system = platform.system()
    if system == "Windows":
        return _check_win32

    # Linux — try /sys/class/leds first (works on both X11 and Wayland)
    try:
        base = Path("/sys/class/leds")
        if base.exists():
            for entry in base.iterdir():
                if "capslock" in entry.name.lower():
                    return _check_sys_leds
    except (OSError, PermissionError):
        pass

    # X11 fallback
    try:
        from Xlib import display  # noqa: F401
        return _check_x11
    except ImportError:
        pass

    # Last resort: subprocess
    try:
        subprocess.check_output(["xset", "-q"], stderr=subprocess.DEVNULL, timeout=1)
        return _check_subprocess
    except Exception:
        pass

    logger.warning("No Caps Lock detection available on this platform")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def caps_lock_active() -> bool:
    """Return ``True`` if Caps Lock is currently on."""
    global _checker  # noqa: PLW0603
    if _checker is None:
        _checker = _init_checker()
    if _checker is None:
        return False
    try:
        return _checker()
    except Exception:
        logger.debug("Caps Lock check failed", exc_info=True)
        return False
