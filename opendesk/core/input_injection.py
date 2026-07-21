"""
Cross-platform remote input injection.

Abstracts mouse and keyboard input across:
- Linux X11 (python-xlib + XTest) ← primary on X11
- Linux Wayland (uinput via evdev, or ydotool) ← primary on Wayland
- Windows (SendInput, keybd_event, mouse_event via ctypes)
- macOS (CoreGraphics CGEvent via pyobjc)
"""

from __future__ import annotations

import ctypes
import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum, auto

from opendesk.utils.platform import current_platform, Platform, is_wayland
from opendesk.core.platform_config import get_platform_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Max ABS range for uinput absolute positioning (compositor maps to screen)
_ABS_MAX = 32767


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class MouseButton(IntEnum):
    LEFT = auto()
    MIDDLE = auto()
    RIGHT = auto()
    SCROLL_UP = auto()
    SCROLL_DOWN = auto()


class KeyState(IntEnum):
    PRESSED = auto()
    RELEASED = auto()
    TYPED = auto()


@dataclass
class MouseEvent:
    x: int
    y: int
    button: MouseButton | None = None
    state: KeyState = KeyState.PRESSED
    absolute: bool = True


@dataclass
class KeyboardEvent:
    key: str | int
    state: KeyState = KeyState.PRESSED
    modifiers: list[str] | None = None


# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------


class InputBackend(ABC):
    """Platform-specific input injection backend."""

    @abstractmethod
    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None: ...

    @abstractmethod
    def click_mouse(self, button: MouseButton, state: KeyState) -> None: ...

    @abstractmethod
    def scroll_mouse(self, dx: int, dy: int) -> None: ...

    @abstractmethod
    def key_event(self, key: str | int, state: KeyState) -> None: ...

    @abstractmethod
    def type_text(self, text: str) -> None: ...

    def set_screen_size(self, width: int, height: int) -> None:
        """Set the remote screen resolution (for coordinate scaling).

        Called by the stream service when the screen resolution becomes
        known.  Some backends (e.g. Wayland ABS uinput) need this to
        correctly scale pixel coordinates to the device range.
        """
        return

    def release(self) -> None:
        """Clean up backend resources."""
        return


# ---------------------------------------------------------------------------
# Linux X11 backend
# ---------------------------------------------------------------------------


class X11InputBackend(InputBackend):
    """Input backend for Linux X11 via XTest extension."""

    _KEY_MAP = {
        "return": "Return", "enter": "Return", "tab": "Tab",
        "escape": "Escape", "backspace": "BackSpace", "delete": "Delete",
        "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
        "space": "space", "ctrl": "Control_L", "alt": "Alt_L", "shift": "Shift_L",
        "super": "Super_L", "menu": "Menu", "capslock": "Caps_Lock",
    }

    def __init__(self) -> None:
        self._display: Any = None  # noqa: ANN401
        self._root: Any = None  # noqa: ANN401
        self._xtest: Any = None  # noqa: ANN401
        self._keysyms: dict[str, int] = {}
        self._shift_keycode: int | None = None
        self._setup()

    def _setup(self) -> None:
        try:
            from Xlib import X, XK, display
            from Xlib.ext import xtest
        except ImportError as exc:
            raise RuntimeError(f"python-xlib not available: {exc}") from exc

        self._display = display.Display()
        self._root = self._display.screen().root
        self._xtest = xtest

        for name in dir(XK):
            if name.startswith("XK_"):
                ks_name = name[3:]
                ks = getattr(XK, name)
                if not callable(ks):
                    self._keysyms[ks_name.lower()] = ks

        self._shift_keycode = self._keycode_from_name("Shift_L")

    def _keycode_from_name(self, name: str) -> int | None:
        keysym = self._keysyms.get(name.lower())
        if keysym is None:
            return None
        return self._display.keysym_to_keycode(keysym)

    def _resolve_key(self, key: str | int) -> int:
        if isinstance(key, int):
            return key
        key_lower = key.lower()
        x11_name = self._KEY_MAP.get(key_lower, key_lower)
        kc = self._keycode_from_name(x11_name)
        if kc is not None:
            return kc
        if len(key) == 1:
            kc = self._keycode_from_name(key)
            if kc is not None:
                return kc
        logger.warning("Unknown key '%s'", key)
        return 0

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        if absolute:
            self._root.warp_pointer(x, y)
        else:
            ptr = self._root.query_pointer()
            self._root.warp_pointer(ptr.root_x + x, ptr.root_y + y)
        self._display.sync()

    def click_mouse(self, button: MouseButton, state: KeyState) -> None:
        btn_map = {
            MouseButton.LEFT: 1, MouseButton.MIDDLE: 2, MouseButton.RIGHT: 3,
            MouseButton.SCROLL_UP: 4, MouseButton.SCROLL_DOWN: 5,
        }
        xbtn = btn_map.get(button, 1)
        if state == KeyState.PRESSED:
            self._xtest.fake_input(self._display, 4, xbtn)
        elif state == KeyState.RELEASED:
            self._xtest.fake_input(self._display, 5, xbtn)
        elif state == KeyState.TYPED:
            self._xtest.fake_input(self._display, 4, xbtn)
            self._xtest.fake_input(self._display, 5, xbtn)
        self._display.sync()

    def scroll_mouse(self, dx: int, dy: int) -> None:
        btn = {(-1, 0): MouseButton.SCROLL_UP, (1, 0): MouseButton.SCROLL_DOWN}.get((dx, dy))
        if btn:
            self.click_mouse(btn, KeyState.TYPED)

    def key_event(self, key: str | int, state: KeyState) -> None:
        kc = self._resolve_key(key)
        if kc == 0:
            return
        if state in (KeyState.PRESSED, KeyState.TYPED):
            self._xtest.fake_input(self._display, 2, kc)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            self._xtest.fake_input(self._display, 3, kc)
        self._display.sync()

    def type_text(self, text: str) -> None:
        from Xlib import XK
        for char in text:
            ks = XK.string_to_keysym(char)
            if ks == 0:
                continue
            kc = self._display.keysym_to_keycode(ks)
            if kc is None or kc == 0:
                continue
            need_shift = char.isupper() or char in "~!@#$%^&*()_+{}|:\"<>?"
            if need_shift and self._shift_keycode:
                self._xtest.fake_input(self._display, 2, self._shift_keycode)
            self._xtest.fake_input(self._display, 2, kc)
            self._xtest.fake_input(self._display, 3, kc)
            if need_shift and self._shift_keycode:
                self._xtest.fake_input(self._display, 3, self._shift_keycode)
        self._display.sync()

    def release(self) -> None:
        if self._display is not None:
            self._display.close()
            self._display = None


# ---------------------------------------------------------------------------
# Linux Wayland backend (uinput / evdev)
# ---------------------------------------------------------------------------


class WaylandInputBackend(InputBackend):
    """Input backend for Linux Wayland using uinput (via python-evdev).

    Creates a virtual uinput device that the compositor accepts as
    a trusted input source.

    **Mouse limitation**: Wayland compositors do not expose a
    ``warp_pointer`` protocol.  This backend converts absolute
    mouse coordinates into relative deltas using virtual cursor
    tracking.  The virtual position is estimated and may drift
    over time.  For accurate absolute positioning, install
    ``ydotool`` (``sudo apt install ydotool``) — the backend
    detects it automatically.

    When ``ydotool`` is unavailable, this backend tries to create
    a second uinput device with ``EV_ABS`` capability.  Compositors
    based on wlroots (Sway, Hyprland) and KDE respect absolute
    positioning via ``EV_ABS``.  GNOME ignores it, falling back
    to relative delta tracking with a warning.

    Requires:
        - ``python-evdev`` (``pip install evdev``)
        - ``uinput`` kernel module loaded
        - The user must have write access to ``/dev/uinput``
          (usually via the ``input`` group)
    """

    def __init__(self) -> None:
        self._ui: Any = None  # noqa: ANN401
        self._abs_ui: Any = None  # noqa: ANN401
        self._screen_width: int = 0
        self._screen_height: int = 0
        self._setup()

    def _setup(self) -> None:
        try:
            import evdev
            from evdev import UInput, ecodes as e
        except ImportError as exc:
            raise RuntimeError(
                "Wayland input requires python-evdev. "
                "Install it with: pip install evdev"
            ) from exc

        self._e = e
        self._virtual_x: int = 0
        self._virtual_y: int = 0
        self._virtual_inited: bool = False

        # Check for ydotool (alternative absolute positioning)
        self._ydotool = _find_ydotool()
        if self._ydotool:
            logger.info("Wayland: ydotool detected — absolute mouse supported")

        # Flag: true if we have a working ABS uinput device
        self._has_abs: bool = False

        # Check that /dev/uinput exists and we can write to it
        import os
        import stat
        uinput_path = "/dev/uinput"
        if not os.path.exists(uinput_path):
            raise RuntimeError(
                f"{uinput_path} does not exist. "
                "Load the uinput kernel module: sudo modprobe uinput"
            )
        uinput_stat = os.stat(uinput_path)
        if not os.access(uinput_path, os.W_OK):
            import pwd
            import grp
            group_info = grp.getgrgid(uinput_stat.st_gid) if uinput_stat.st_gid != 0 else None
            group_name = group_info.gr_name if group_info else "input"
            raise PermissionError(
                f"No write permission on {uinput_path}.\n"
                f"Add your user to the '{group_name}' group:\n"
                f"  sudo usermod -aG {group_name} $USER\n"
                f"Then log out and log back in."
            )

        # Check that the uinput module is actually loaded
        if not os.path.exists("/sys/module/uinput"):
            raise RuntimeError(
                "uinput kernel module not loaded. "
                "Run: sudo modprobe uinput"
            )

        try:
            # NOTE: EV_SYN / SYN_REPORT must NOT be included — the kernel
            # implicitly supports them for every device.  Enabling them
            # explicitly causes UI_SET_EVBIT to fail with EINVAL.
            capabilities = {
                e.EV_KEY: (
                    e.BTN_LEFT, e.BTN_MIDDLE, e.BTN_RIGHT,
                    e.KEY_A, e.KEY_B, e.KEY_C, e.KEY_D, e.KEY_E,
                    e.KEY_F, e.KEY_G, e.KEY_H, e.KEY_I, e.KEY_J,
                    e.KEY_K, e.KEY_L, e.KEY_M, e.KEY_N, e.KEY_O,
                    e.KEY_P, e.KEY_Q, e.KEY_R, e.KEY_S, e.KEY_T,
                    e.KEY_U, e.KEY_V, e.KEY_W, e.KEY_X, e.KEY_Y,
                    e.KEY_Z, e.KEY_1, e.KEY_2, e.KEY_3, e.KEY_4,
                    e.KEY_5, e.KEY_6, e.KEY_7, e.KEY_8, e.KEY_9,
                    e.KEY_0, e.KEY_SPACE, e.KEY_ENTER, e.KEY_BACKSPACE,
                    e.KEY_TAB, e.KEY_ESC, e.KEY_DELETE, e.KEY_HOME,
                    e.KEY_END, e.KEY_PAGEUP, e.KEY_PAGEDOWN,
                    e.KEY_UP, e.KEY_DOWN, e.KEY_LEFT, e.KEY_RIGHT,
                    e.KEY_LEFTSHIFT, e.KEY_LEFTCTRL, e.KEY_LEFTALT,
                    e.KEY_LEFTMETA, e.KEY_CAPSLOCK, e.KEY_MENU,
                    e.KEY_F1, e.KEY_F2, e.KEY_F3, e.KEY_F4,
                    e.KEY_F5, e.KEY_F6, e.KEY_F7, e.KEY_F8,
                    e.KEY_F9, e.KEY_F10, e.KEY_F11, e.KEY_F12,
                    e.KEY_COMMA, e.KEY_DOT, e.KEY_SEMICOLON,
                    e.KEY_APOSTROPHE, e.KEY_GRAVE, e.KEY_MINUS,
                    e.KEY_EQUAL, e.KEY_LEFTBRACE, e.KEY_RIGHTBRACE,
                    e.KEY_BACKSLASH, e.KEY_SLASH,
                    e.KEY_INSERT, e.KEY_PRINT, e.KEY_SCROLLLOCK,
                    e.KEY_PAUSE, e.KEY_SYSRQ,
                    e.KEY_NUMLOCK, e.KEY_KPSLASH, e.KEY_KPASTERISK,
                    e.KEY_KPMINUS, e.KEY_KPPLUS, e.KEY_KPENTER,
                    e.KEY_KP0, e.KEY_KP1, e.KEY_KP2, e.KEY_KP3,
                    e.KEY_KP4, e.KEY_KP5, e.KEY_KP6, e.KEY_KP7,
                    e.KEY_KP8, e.KEY_KP9, e.KEY_KPDOT,
                ),
                e.EV_REL: (e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL),
            }
            self._ui = UInput(capabilities, name="OpenDesk Virtual Input", version=0x1)
            logger.info("Wayland uinput REL device created")

            # ── Try to create a second uinput device with EV_ABS for absolute positioning ──
            if not self._ydotool:
                self._try_create_abs_device()

        except PermissionError:
            raise
        except OSError as e:
            raise RuntimeError(
                f"Failed to create uinput device: {e}\n"
                "Ensure the uinput kernel module is loaded: sudo modprobe uinput"
            ) from e

    def _key_to_evdev(self, key: str | int) -> int:
        from evdev import ecodes as e
        if isinstance(key, int):
            return key
        key_map = {
            "return": e.KEY_ENTER, "enter": e.KEY_ENTER, "tab": e.KEY_TAB,
            "escape": e.KEY_ESC, "backspace": e.KEY_BACKSPACE, "delete": e.KEY_DELETE,
            "home": e.KEY_HOME, "end": e.KEY_END,
            "pageup": e.KEY_PAGEUP, "pagedown": e.KEY_PAGEDOWN,
            "up": e.KEY_UP, "down": e.KEY_DOWN, "left": e.KEY_LEFT, "right": e.KEY_RIGHT,
            "space": e.KEY_SPACE, "ctrl": e.KEY_LEFTCTRL, "alt": e.KEY_LEFTALT,
            "shift": e.KEY_LEFTSHIFT, "super": e.KEY_LEFTMETA, "menu": e.KEY_MENU,
            "capslock": e.KEY_CAPSLOCK,
            "insert": e.KEY_INSERT, "print": e.KEY_PRINT,
            "scrolllock": e.KEY_SCROLLLOCK, "pause": e.KEY_PAUSE,
            "numlock": e.KEY_NUMLOCK,
            "f1": e.KEY_F1, "f2": e.KEY_F2, "f3": e.KEY_F3, "f4": e.KEY_F4,
            "f5": e.KEY_F5, "f6": e.KEY_F6, "f7": e.KEY_F7, "f8": e.KEY_F8,
            "f9": e.KEY_F9, "f10": e.KEY_F10, "f11": e.KEY_F11, "f12": e.KEY_F12,
            "f13": e.KEY_F13, "f14": e.KEY_F14, "f15": e.KEY_F15,
            "f16": e.KEY_F16, "f17": e.KEY_F17, "f18": e.KEY_F18,
            "f19": e.KEY_F19, "f20": e.KEY_F20,
            "f21": e.KEY_F21, "f22": e.KEY_F22, "f23": e.KEY_F23, "f24": e.KEY_F24,
        }
        lower_key = key.lower()
        if lower_key in key_map:
            return key_map[lower_key]
        if len(key) == 1 and "a" <= lower_key <= "z":
            return getattr(e, f"KEY_{lower_key.upper()}")
        if len(key) == 1 and "0" <= key <= "9":
            return getattr(e, f"KEY_{key}")
        sym_map = {
            ",": e.KEY_COMMA, ".": e.KEY_DOT, ";": e.KEY_SEMICOLON,
            "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE, "-": e.KEY_MINUS,
            "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
            "\\": e.KEY_BACKSLASH, "/": e.KEY_SLASH,
        }
        if key in sym_map:
            return sym_map[key]
        logger.warning("Unknown key '%s'", key)
        return 0

    def set_screen_size(self, width: int, height: int) -> None:
        """Set the screen resolution for ABS coordinate scaling."""
        self._screen_width = width
        self._screen_height = height
        logger.info(
            "Wayland: screen size set to %dx%d for ABS scaling",
            width, height,
        )

    def _try_create_abs_device(self) -> None:
        """Tenta di creare un secondo device uinput con EV_ABS.

        Alcuni compositori Wayland (wlroots, KDE) supportano il
        posizionamento assoluto via EV_ABS da uinput.  GNOME lo
        ignora.  Se la creazione fallisce, si usa il fallback REL.

        Nota: le coordinate vanno scalate in base alla risoluzione
        dello schermo (set_screen_size), altrimenti il compositore
        le interpreta nel range 0-_ABS_MAX invece che in pixel.
        """
        from evdev import UInput, AbsInfo
        try:
            abs_caps = {
                self._e.EV_KEY: (
                    self._e.BTN_LEFT, self._e.BTN_MIDDLE, self._e.BTN_RIGHT,
                ),
                self._e.EV_ABS: [
                    (self._e.ABS_X, AbsInfo(0, 0, _ABS_MAX, 0, 0, 0)),
                    (self._e.ABS_Y, AbsInfo(0, 0, _ABS_MAX, 0, 0, 0)),
                ],
            }
            self._abs_ui = UInput(
                abs_caps,
                name="OpenDesk Virtual Input (Absolute)",
                version=0x1,
            )
            self._has_abs = True
            logger.info(
                "Wayland: ABS uinput device created (max=%d) — "
                "absolute mouse positioning enabled (waiting for screen size)",
                _ABS_MAX,
            )
        except Exception as e:
            self._abs_ui = None
            self._has_abs = False
            logger.warning(
                "Wayland: could not create ABS uinput device (%s) — "
                "falling back to relative delta tracking. "
                "Install ydotool for accurate absolute positioning.",
                e,
            )

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        """Move the mouse cursor.

        On Wayland, absolute positioning is done via:
        1. ``ydotool`` (if installed) — most reliable
        2. ``EV_ABS`` on a secondary uinput device — works on wlroots/KDE
        3. Relative delta tracking with virtual cursor — drifts over time
        """
        if absolute and self._ydotool:
            # ydotool supports absolute positioning
            import subprocess
            subprocess.run(
                [self._ydotool, "mousemove", "--absolute", str(x), str(y)],
                capture_output=True, timeout=1,
            )
            self._virtual_x = x
            self._virtual_y = y
            return

        if absolute and self._has_abs:
            if self._screen_width > 0 and self._screen_height > 0:
                # Scala le coordinate pixel nel range ABS del device
                # Esempio: pixel x=500 su schermo 1920px → ABS_X = 500 * 32767 / 1920 ≈ 8533
                abs_x = int(x * _ABS_MAX / self._screen_width)
                abs_y = int(y * _ABS_MAX / self._screen_height)
                # Clamp per sicurezza
                abs_x = max(0, min(_ABS_MAX, abs_x))
                abs_y = max(0, min(_ABS_MAX, abs_y))
                self._abs_ui.write(self._e.EV_ABS, self._e.ABS_X, abs_x)
                self._abs_ui.write(self._e.EV_ABS, self._e.ABS_Y, abs_y)
                self._abs_ui.syn()
                self._virtual_x = x
                self._virtual_y = y
                return
            else:
                # Screen size non ancora nota → REL fallback
                logger.debug(
                    "Wayland ABS: screen size unknown, falling back to REL "
                    "for move to (%d, %d)", x, y,
                )

        if absolute:
            if not self._virtual_inited:
                logger.warning(
                    "Wayland: absolute mouse via relative deltas — "
                    "initial position assumed (0,0).  The first event "
                    "will be inaccurate.  Install ydotool (or use "
                    "wlroots/KDE compositor) for reliable positioning."
                )
                self._virtual_inited = True
            dx = x - self._virtual_x
            dy = y - self._virtual_y
            self._virtual_x = x
            self._virtual_y = y
        else:
            dx, dy = x, y
            self._virtual_x += dx
            self._virtual_y += dy

        self._ui.write(self._e.EV_REL, self._e.REL_X, dx)
        self._ui.write(self._e.EV_REL, self._e.REL_Y, dy)
        self._ui.syn()

    def click_mouse(self, button: MouseButton, state: KeyState) -> None:
        btn_map = {
            MouseButton.LEFT: self._e.BTN_LEFT,
            MouseButton.MIDDLE: self._e.BTN_MIDDLE,
            MouseButton.RIGHT: self._e.BTN_RIGHT,
        }
        btn = btn_map.get(button)
        if btn is None:
            return
        # Invia il click sul device ABS se disponibile (posizione già
        # impostata da move_mouse sullo stesso device), altrimenti sul
        # device REL.  Il cursore è condiviso tra tutti i device pointer.
        target = self._abs_ui if self._has_abs else self._ui
        if state == KeyState.PRESSED:
            target.write(self._e.EV_KEY, btn, 1)
        elif state == KeyState.RELEASED:
            target.write(self._e.EV_KEY, btn, 0)
        elif state == KeyState.TYPED:
            target.write(self._e.EV_KEY, btn, 1)
            target.write(self._e.EV_KEY, btn, 0)
        target.syn()

    def scroll_mouse(self, dx: int, dy: int) -> None:
        if dy != 0:
            self._ui.write(self._e.EV_REL, self._e.REL_WHEEL, dy)
        if dx != 0:
            self._ui.write(self._e.EV_REL, self._e.REL_HWHEEL, dx)
        self._ui.syn()

    def key_event(self, key: str | int, state: KeyState) -> None:
        kc = self._key_to_evdev(key)
        if kc == 0:
            return
        if state in (KeyState.PRESSED, KeyState.TYPED):
            self._ui.write(self._e.EV_KEY, kc, 1)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            self._ui.write(self._e.EV_KEY, kc, 0)
        self._ui.syn()

    def type_text(self, text: str) -> None:
        for char in text:
            kc = self._key_to_evdev(char)
            if kc == 0:
                continue
            need_shift = char.isupper() or char in "~!@#$%^&*()_+{}|:\"<>?"
            if need_shift:
                self._ui.write(self._e.EV_KEY, self._e.KEY_LEFTSHIFT, 1)
            self._ui.write(self._e.EV_KEY, kc, 1)
            self._ui.write(self._e.EV_KEY, kc, 0)
            if need_shift:
                self._ui.write(self._e.EV_KEY, self._e.KEY_LEFTSHIFT, 0)
            self._ui.syn()

    def release(self) -> None:
        if self._abs_ui is not None:
            try:
                self._abs_ui.close()
            except Exception:
                pass
            self._abs_ui = None
        if self._ui is not None:
            self._ui.close()
            self._ui = None


# ---------------------------------------------------------------------------
# Windows backend (ctypes / SendInput)
# ---------------------------------------------------------------------------


class WindowsInputBackend(InputBackend):
    """Input backend for Windows using ``SendInput`` (modern API).

    Uses:
    - ``SendInput`` for keyboard/mouse input (successore di
      ``mouse_event`` / ``keybd_event``, deprecati da Windows 8+)
    - ``SetCursorPos`` per spostamento assoluto del cursore
    - Fallback a ``mouse_event`` se ``SendInput`` fallisce

    UIPI (User Interface Privilege Isolation):
      Se il processo OpenDesk Host gira a un livello di integrità
      inferiore rispetto alla finestra di destinazione, l'input
      viene bloccato da Windows.  Soluzione: esegui l'host come
      amministratore, o abbassa l'integrity level della finestra
      target (non raccomandato).
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32

        # Strutture SendInput (INPUT, MOUSEINPUT, KEYBDINPUT)
        self._INPUT_TYPE_MOUSE = 0
        self._INPUT_TYPE_KEYBOARD = 1

        self._KEYEVENTF_KEYDOWN = 0x0000
        self._KEYEVENTF_KEYUP = 0x0002
        self._KEYEVENTF_SCANCODE = 0x0008

        self._MOUSEEVENTF_MOVE = 0x0001
        self._MOUSEEVENTF_ABSOLUTE = 0x8000
        self._MOUSEEVENTF_LEFTDOWN = 0x0002
        self._MOUSEEVENTF_LEFTUP = 0x0004
        self._MOUSEEVENTF_RIGHTDOWN = 0x0008
        self._MOUSEEVENTF_RIGHTUP = 0x0010
        self._MOUSEEVENTF_MIDDLEDOWN = 0x0020
        self._MOUSEEVENTF_MIDDLEUP = 0x0040
        self._MOUSEEVENTF_WHEEL = 0x0800
        self._MOUSEEVENTF_HWHEEL = 0x1000

        # Virtual key codes
        self._VK = {
            "return": 0x0D, "enter": 0x0D, "tab": 0x09,
            "escape": 0x1B, "backspace": 0x08, "delete": 0x2E,
            "home": 0x24, "end": 0x23,
            "pageup": 0x21, "pagedown": 0x22,
            "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
            "space": 0x20, "ctrl": 0x11, "alt": 0x12,
            "shift": 0x10, "super": 0x5B, "menu": 0x5D,
            "capslock": 0x14,
        }
        for i in range(1, 13):
            self._VK[f"f{i}"] = 0x6F + i

        self._SendInput = self._user32.SendInput
        self._SendInput.argtypes = [
            ctypes.c_uint,  # cInputs
            ctypes.c_void_p,  # pInputs (INPUT*)
            ctypes.c_int,  # cbSize
        ]
        self._SendInput.restype = ctypes.c_uint

        logger.info("Windows input backend initialised (SendInput)")

    # ── helpers ──────────────────────────────────────────────────────

    def _vk_from_key(self, key: str | int) -> int:
        if isinstance(key, int):
            return key
        lower = key.lower()
        if lower in self._VK:
            return self._VK[lower]
        if len(lower) == 1:
            return ord(lower.upper())
        return 0

    def _send_mouse_input(self, flags: int, data: int = 0, dx: int = 0, dy: int = 0) -> None:
        """Invia un evento mouse con ``SendInput``.

        Parameters
        ----------
        flags : int
            Combinazione di ``MOUSEEVENTF_*`` flags.
        data : int
            ``dwData``: movimento rotellina (positivo = su) o ``MOUSE_XBUTTON``.
        dx : int
            Coordinata X (o delta relativo se non ``MOUSEEVENTF_ABSOLUTE``).
        dy : int
            Coordinata Y (o delta relativo).
        """
        import ctypes
        from ctypes import wintypes

        # Definisce la struttura MOUSEINPUT
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_ulong),
                ("u", INPUT_UNION),
            ]

        inp = INPUT()
        inp.type = self._INPUT_TYPE_MOUSE
        inp.u.mi = MOUSEINPUT(dx, dy, data, flags, 0, None)

        self._SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _send_keyboard_input(self, vk: int, flags: int) -> None:
        """Invia un evento tastiera con ``SendInput``."""
        import ctypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_ulong),
                ("u", INPUT_UNION),
            ]

        inp = INPUT()
        inp.type = self._INPUT_TYPE_KEYBOARD
        inp.u.ki = KEYBDINPUT(vk, 0, flags, 0, None)

        self._SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    # ── public API ───────────────────────────────────────────────────

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        """Sposta il cursore nella posizione specificata.

        Usa ``SetCursorPos`` per spostamento assoluto (più affidabile
        di SendInput con flags MOUSE_MOVE + ABSOLUTE) e SendInput per
        movimento relativo.
        """
        if absolute:
            # SetCursorPos è più affidabile per movimenti assoluti
            self._user32.SetCursorPos(x, y)
        else:
            self._send_mouse_input(
                self._MOUSEEVENTF_MOVE, 0, x, y,
            )

    def click_mouse(self, button: MouseButton, state: KeyState) -> None:
        flag_map = {
            MouseButton.LEFT: (self._MOUSEEVENTF_LEFTDOWN, self._MOUSEEVENTF_LEFTUP),
            MouseButton.RIGHT: (self._MOUSEEVENTF_RIGHTDOWN, self._MOUSEEVENTF_RIGHTUP),
            MouseButton.MIDDLE: (self._MOUSEEVENTF_MIDDLEDOWN, self._MOUSEEVENTF_MIDDLEUP),
        }
        btns = flag_map.get(button)
        if btns is None:
            return
        down_flag, up_flag = btns

        if state in (KeyState.PRESSED, KeyState.TYPED):
            self._send_mouse_input(down_flag)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            self._send_mouse_input(up_flag)

    def scroll_mouse(self, dx: int, dy: int) -> None:
        if dy != 0:
            # wheel positivo = su (negativo il segno perché SendInput
            # usa convenzione opposta a mouse_event)
            self._send_mouse_input(self._MOUSEEVENTF_WHEEL, data=-dy * 120)
        if dx != 0:
            self._send_mouse_input(self._MOUSEEVENTF_HWHEEL, data=dx * 120)

    def key_event(self, key: str | int, state: KeyState) -> None:
        vk = self._vk_from_key(key)
        if vk == 0:
            return
        if state in (KeyState.PRESSED, KeyState.TYPED):
            self._send_keyboard_input(vk, self._KEYEVENTF_KEYDOWN)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            self._send_keyboard_input(vk, self._KEYEVENTF_KEYUP)

    def type_text(self, text: str) -> None:
        for char in text:
            vk = self._vk_from_key(char)
            if vk == 0:
                continue
            need_shift = char.isupper() or char in "~!@#$%^&*()_+{}|:\"<>?"
            if need_shift:
                self._send_keyboard_input(0x10, self._KEYEVENTF_KEYDOWN)  # VK_SHIFT
            self._send_keyboard_input(vk, self._KEYEVENTF_KEYDOWN)
            self._send_keyboard_input(vk, self._KEYEVENTF_KEYUP)
            if need_shift:
                self._send_keyboard_input(0x10, self._KEYEVENTF_KEYUP)


# ---------------------------------------------------------------------------
# macOS backend (CoreGraphics / pyobjc)
# ---------------------------------------------------------------------------


class MacOSInputBackend(InputBackend):
    """Input backend for macOS using CoreGraphics (``pyobjc``).

    Uses:
    - ``CGEventCreateMouseEvent`` + ``CGEventPost``
    - ``CGEventCreateKeyboardEvent`` + ``CGEventPost``
    """

    def __init__(self) -> None:
        try:
            import Quartz
            from Quartz import (
                CGEventCreateMouseEvent,
                CGEventCreateKeyboardEvent,
                CGEventPost,
                kCGHIDEventTap,
                kCGEventMouseMoved,
                kCGEventLeftMouseDown, kCGEventLeftMouseUp,
                kCGEventRightMouseDown, kCGEventRightMouseUp,
                kCGEventOtherMouseDown, kCGEventOtherMouseUp,
                kCGEventScrollWheel,
                kCGEventKeyDown, kCGEventKeyUp,
                kCGMouseButtonLeft, kCGMouseButtonRight, kCGMouseButtonCenter,
                CGEventSetIntegerValueField,
                kCGMouseEventDeltaX, kCGMouseEventDeltaY,
                CGWarpMouseCursorPosition,
                CGAssociateMouseAndMouseCursorPosition,
            )
            from Quartz.CoreGraphics import (
                CGMainDisplayID,
                CGDisplayPixelsWide,
                CGDisplayPixelsHigh,
            )
        except ImportError as exc:
            raise RuntimeError(
                "macOS input requires pyobjc: pip install pyobjc-framework-Quartz"
            ) from exc

        self._Quartz = Quartz
        self._kCGHIDEventTap = kCGHIDEventTap
        self._kCMouseButton = {
            MouseButton.LEFT: kCGMouseButtonLeft,
            MouseButton.RIGHT: kCGMouseButtonRight,
            MouseButton.MIDDLE: kCGMouseButtonCenter,
        }
        self._main_display = CGMainDisplayID()
        logger.info("macOS input backend initialised")

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        if absolute:
            CGWarpMouseCursorPosition((x, y))
        else:
            event = CGEventCreateMouseEvent(
                None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft
            )
            CGEventSetIntegerValueField(event, kCGMouseEventDeltaX, x)
            CGEventSetIntegerValueField(event, kCGMouseEventDeltaY, y)
            CGEventPost(kCGHIDEventTap, event)

    def click_mouse(self, button: MouseButton, state: KeyState) -> None:
        btn_type = self._kCMouseButton.get(button, kCGMouseButtonLeft)
        if button == MouseButton.LEFT:
            down, up = kCGEventLeftMouseDown, kCGEventLeftMouseUp
        elif button == MouseButton.RIGHT:
            down, up = kCGEventRightMouseDown, kCGEventRightMouseUp
        else:
            down, up = kCGEventOtherMouseDown, kCGEventOtherMouseUp

        if state in (KeyState.PRESSED, KeyState.TYPED):
            event = CGEventCreateMouseEvent(None, down, (0, 0), btn_type)
            CGEventPost(kCGHIDEventTap, event)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            event = CGEventCreateMouseEvent(None, up, (0, 0), btn_type)
            CGEventPost(kCGHIDEventTap, event)

    def scroll_mouse(self, dx: int, dy: int) -> None:
        event = CGEventCreateScrollWheelEvent(None, kCGEventScrollWheel, 2, dy, dx)
        CGEventPost(kCGHIDEventTap, event)

    def key_event(self, key: str | int, state: KeyState) -> None:
        from Quartz import (
            CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
            kCGEventKeyDown, kCGEventKeyUp,
        )
        keycode = self._key_to_macos(key)
        if keycode == 0:
            return
        if state in (KeyState.PRESSED, KeyState.TYPED):
            event = CGEventCreateKeyboardEvent(None, keycode, True)
            CGEventPost(kCGHIDEventTap, event)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            event = CGEventCreateKeyboardEvent(None, keycode, False)
            CGEventPost(kCGHIDEventTap, event)

    def type_text(self, text: str) -> None:
        for char in text:
            self.key_event(char, KeyState.TYPED)

    def _key_to_macos(self, key: str | int) -> int:
        if isinstance(key, int):
            return key
        # macOS virtual key codes
        key_map = {
            "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7,
            "c": 8, "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15,
            "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "5": 22,
            "6": 23, "7": 24, "8": 25, "9": 26, "0": 27,
            "space": 49, "return": 36, "enter": 36, "tab": 48,
            "backspace": 51, "delete": 117, "escape": 53,
            "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
            "up": 126, "down": 125, "left": 123, "right": 124,
            "shift": 56, "ctrl": 59, "alt": 58, "super": 55,
            "capslock": 57, "menu": 110,
        }
        lower = key.lower()
        if lower in key_map:
            return key_map[lower]
        if len(lower) == 1:
            return ord(lower.upper()) - 0x41 if lower.isalpha() else 0
        return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_ydotool() -> str | None:
    """Find ydotool binary for absolute mouse positioning on Wayland."""
    import shutil
    for name in ("ydotool", "ydotoold"):
        path = shutil.which(name)
        if path:
            return path
    return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_BACKENDS: dict[tuple[Platform, bool], type[InputBackend]] = {
    (Platform.LINUX, False): X11InputBackend,   # X11
    (Platform.LINUX, True): WaylandInputBackend,  # Wayland
    (Platform.WINDOWS, False): WindowsInputBackend,
    (Platform.MACOS, False): MacOSInputBackend,
}


def create_input_backend() -> InputBackend:
    """Create the appropriate input backend for the current platform.

    Uses ``PlatformConfig`` to select the backend for the detected
    platform, falling back to the legacy _BACKENDS dict if the
    platform config does not have a backend class.
    """
    cfg = get_platform_config()
    backend = cfg.create_input_backend()
    if backend is not None:
        logger.info("Input backend: %s (%s)", type(backend).__name__, cfg.display_name)
        return backend

    # Legacy fallback (config did not resolve a backend)
    plat = current_platform()
    wayland = is_wayland() if plat == Platform.LINUX else False
    key = (plat, wayland)
    backend_cls = _BACKENDS.get(key)

    if backend_cls is None:
        backend_cls = _BACKENDS.get((plat, False))
    if backend_cls is None:
        raise RuntimeError(f"Unsupported platform: {plat}")

    logger.info("Input backend (legacy): %s (%s)", backend_cls.__name__, plat.name)
    return backend_cls()
