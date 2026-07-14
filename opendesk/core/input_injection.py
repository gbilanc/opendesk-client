"""
Cross-platform remote input injection.

Abstracts mouse and keyboard input across:
- Linux X11 (python-xlib + XTest) ← primary on X11
- Linux Wayland (uinput via evdev, or ydotool) ← primary on Wayland
- Windows (SendInput, keybd_event, mouse_event via ctypes)
- macOS (CoreGraphics CGEvent via pyobjc)
"""

from __future__ import annotations

import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum, auto

from opendesk.utils.platform import current_platform, Platform, is_wayland

logger = logging.getLogger(__name__)


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

    Requires:
        - ``python-evdev`` (``pip install evdev``)
        - ``uinput`` kernel module loaded
        - The user must have write access to ``/dev/uinput``
          (usually via the ``input`` group)
    """

    def __init__(self) -> None:
        self._ui: Any = None  # noqa: ANN401
        self._setup()

    def _setup(self) -> None:
        try:
            import evdev
            from evdev import UInput, ecodes as e
        except ImportError as exc:
            raise RuntimeError(
                "Wayland input requires python-evdev: pip install evdev"
            ) from exc

        self._e = e
        self._virtual_x: int = 0
        self._virtual_y: int = 0
        self._virtual_inited: bool = False

        # Check for ydotool (alternative absolute positioning)
        self._ydotool = _find_ydotool()
        if self._ydotool:
            logger.info("Wayland: ydotool detected — absolute mouse supported")

        try:
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
                ),
                e.EV_REL: (e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL),
                e.EV_SYN: (e.SYN_REPORT,),
            }
            self._ui = UInput(capabilities, name="OpenDesk Virtual Input", version=0x1)
            logger.info("Wayland uinput device created")
        except PermissionError:
            logger.error(
                "Cannot create uinput device. "
                "Add user to the 'input' group: sudo usermod -aG input $USER"
            )
            raise

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
        }
        lower_key = key.lower()
        if lower_key in key_map:
            return key_map[lower_key]
        if len(key) == 1 and "a" <= lower_key <= "z":
            return getattr(e, f"KEY_{lower_key.upper()}")
        if len(key) == 1 and "0" <= key <= "9":
            return getattr(e, f"KEY_{key}")
        sym_map = {",": e.KEY_COMMA, ".": e.KEY_DOT, ";": e.KEY_SEMICOLON,
                   "'": e.KEY_APOSTROPHE, "`": e.KEY_GRAVE, "-": e.KEY_MINUS,
                   "=": e.KEY_EQUAL, "[": e.KEY_LEFTBRACE, "]": e.KEY_RIGHTBRACE,
                   "\\": e.KEY_BACKSLASH, "/": e.KEY_SLASH}
        if key in sym_map:
            return sym_map[key]
        logger.warning("Unknown key '%s'", key)
        return 0

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        """Move the mouse cursor.

        On Wayland, *absolute* positioning is emulated via relative
        deltas because compositors do not expose a warp-pointer API.

        If ``ydotool`` is installed, absolute positioning uses it
        for accurate cursor placement.  Otherwise, the virtual
        position is tracked and converted to relative deltas — this
        may drift over time.
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

        if absolute:
            if not self._virtual_inited:
                logger.info(
                    "Wayland absolute mouse: virtual cursor tracking enabled. "
                    "Initial position is (0,0).  Convert absolute to relative. "
                    "Install ydotool for accurate absolute positioning."
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
        if state == KeyState.PRESSED:
            self._ui.write(self._e.EV_KEY, btn, 1)
        elif state == KeyState.RELEASED:
            self._ui.write(self._e.EV_KEY, btn, 0)
        elif state == KeyState.TYPED:
            self._ui.write(self._e.EV_KEY, btn, 1)
            self._ui.write(self._e.EV_KEY, btn, 0)
        self._ui.syn()

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
        if self._ui is not None:
            self._ui.close()
            self._ui = None


# ---------------------------------------------------------------------------
# Windows backend (ctypes / SendInput)
# ---------------------------------------------------------------------------


class WindowsInputBackend(InputBackend):
    """Input backend for Windows using ctypes / SendInput API.

    Uses:
    - ``SendInput`` for keyboard/mouse input
    - ``SetCursorPos`` + ``mouse_event`` for mouse movement
    """

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._user32 = ctypes.windll.user32
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

        logger.info("Windows input backend initialised")

    def _vk_from_key(self, key: str | int) -> int:
        if isinstance(key, int):
            return key
        lower = key.lower()
        if lower in self._VK:
            return self._VK[lower]
        if len(lower) == 1:
            return ord(lower.upper())
        return 0

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        if absolute:
            # Convert to normalized coordinates (0..65535)
            sx, sy = ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
            nx = int(x * 65535 / max(sx, 1))
            ny = int(y * 65535 / max(sy, 1))
            self._user32.mouse_event(self._MOUSEEVENTF_MOVE | self._MOUSEEVENTF_ABSOLUTE, nx, ny, 0, 0)
        else:
            self._user32.mouse_event(self._MOUSEEVENTF_MOVE, x, y, 0, 0)

    def click_mouse(self, button: MouseButton, state: KeyState) -> None:
        flags = {
            (MouseButton.LEFT, KeyState.PRESSED): self._MOUSEEVENTF_LEFTDOWN,
            (MouseButton.LEFT, KeyState.RELEASED): self._MOUSEEVENTF_LEFTUP,
            (MouseButton.RIGHT, KeyState.PRESSED): self._MOUSEEVENTF_RIGHTDOWN,
            (MouseButton.RIGHT, KeyState.RELEASED): self._MOUSEEVENTF_RIGHTUP,
            (MouseButton.MIDDLE, KeyState.PRESSED): self._MOUSEEVENTF_MIDDLEDOWN,
            (MouseButton.MIDDLE, KeyState.RELEASED): self._MOUSEEVENTF_MIDDLEUP,
        }
        flag = flags.get((button, state))
        if flag is not None:
            self._user32.mouse_event(flag, 0, 0, 0, 0)
        elif state == KeyState.TYPED:
            self.click_mouse(button, KeyState.PRESSED)
            self.click_mouse(button, KeyState.RELEASED)

    def scroll_mouse(self, dx: int, dy: int) -> None:
        if dy != 0:
            self._user32.mouse_event(self._MOUSEEVENTF_WHEEL, 0, 0, -dy * 120, 0)

    def key_event(self, key: str | int, state: KeyState) -> None:
        vk = self._vk_from_key(key)
        if vk == 0:
            return
        if state in (KeyState.PRESSED, KeyState.TYPED):
            self._user32.keybd_event(vk, 0, self._KEYEVENTF_KEYDOWN, 0)
        if state in (KeyState.RELEASED, KeyState.TYPED):
            self._user32.keybd_event(vk, 0, self._KEYEVENTF_KEYUP, 0)

    def type_text(self, text: str) -> None:
        for char in text:
            vk = ord(char.upper()) if char.isalpha() else self._vk_from_key(char)
            if vk == 0:
                continue
            need_shift = char.isupper() or char in "~!@#$%^&*()_+{}|:\"<>?"
            if need_shift:
                self._user32.keybd_event(0x10, 0, self._KEYEVENTF_KEYDOWN, 0)  # VK_SHIFT
            self._user32.keybd_event(vk, 0, self._KEYEVENTF_KEYDOWN, 0)
            self._user32.keybd_event(vk, 0, self._KEYEVENTF_KEYUP, 0)
            if need_shift:
                self._user32.keybd_event(0x10, 0, self._KEYEVENTF_KEYUP, 0)


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
    """Create the appropriate input backend for the current platform."""
    plat = current_platform()
    wayland = is_wayland() if plat == Platform.LINUX else False
    key = (plat, wayland)
    backend_cls = _BACKENDS.get(key)

    if backend_cls is None:
        # Fallback to non-wayland
        backend_cls = _BACKENDS.get((plat, False))
    if backend_cls is None:
        raise RuntimeError(f"Unsupported platform: {plat}")

    logger.info("Input backend: %s (%s)", backend_cls.__name__, plat.name)
    return backend_cls()
