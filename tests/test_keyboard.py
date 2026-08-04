"""Unit tests per la mappatura tastiera remota (viewer → backend).

Copre i bug storici del flusso tastiera:

1.  I simboli shiftati ("!", "@", ":") venivano scartati dal backend
    Wayland/evdev perché mancanti dalla mappa carattere → keycode.
2.  `ord(char)` nel backend Windows collideva con i VK di navigazione
    (0x21 = VK_PRIOR, 0x2E = VK_DELETE, ...) producendo tasti sbagliati.
3.  La cattura lato viewer lowercase() il carattere e scartava i
    caratteri non-ASCII (accentate) e diversi tasti (insert, print, ...).
"""

from __future__ import annotations

import pytest

from opendesk.core.input_injection import (
    WaylandInputBackend,
    WindowsInputBackend,
)
from opendesk.network.protocol import Message, MessageType

# ---------------------------------------------------------------------------
# WaylandInputBackend._key_to_evdev
# ---------------------------------------------------------------------------


class TestKeyToEvdev:
    def test_shifted_symbols_resolve_to_base_key(self) -> None:
        """I simboli shiftati non devono più essere scartati (erano DROPPED)."""
        b = WaylandInputBackend.__new__(WaylandInputBackend)
        cases = {
            "!",
            "@",
            "#",
            "$",
            "%",
            "^",
            "&",
            "*",
            "(",
            ")",
            ":",
            '"',
            "?",
            "{",
            "}",
            "|",
            "~",
            "_",
            "+",
            "<",
            ">",
        }
        for sym in cases:
            assert b._key_to_evdev(sym) != 0, f"'{sym}' dovrebbe risolvere"

    def test_symbols_map_to_expected_base_keys(self) -> None:
        """Verifica i mapping espliciti (base US: '!'→1, ':'→';', ...)."""
        b = WaylandInputBackend.__new__(WaylandInputBackend)
        from evdev import ecodes as e

        cases = {
            "!": e.KEY_1,
            "@": e.KEY_2,
            ":": e.KEY_SEMICOLON,
            "?": e.KEY_SLASH,
            "[": e.KEY_LEFTBRACE,
            "}": e.KEY_RIGHTBRACE,
            "~": e.KEY_GRAVE,
            "_": e.KEY_MINUS,
            "+": e.KEY_EQUAL,
            "<": e.KEY_COMMA,
            ">": e.KEY_DOT,
            '"': e.KEY_APOSTROPHE,
        }
        for sym, expected in cases.items():
            assert b._key_to_evdev(sym) == expected, f"'{sym}' → {expected}"

    def test_named_keys(self) -> None:
        b = WaylandInputBackend.__new__(WaylandInputBackend)
        from evdev import ecodes as e

        assert b._key_to_evdev("altgr") == e.KEY_RIGHTALT
        assert b._key_to_evdev("insert") == e.KEY_INSERT
        assert b._key_to_evdev("menu") == e.KEY_MENU
        assert b._key_to_evdev("f13") == e.KEY_F13
        assert b._key_to_evdev("print") == e.KEY_PRINT

    def test_unknown_key_returns_zero(self) -> None:
        b = WaylandInputBackend.__new__(WaylandInputBackend)
        assert b._key_to_evdev("ò") == 0  # senza layout, le accentate non mappano


# ---------------------------------------------------------------------------
# WindowsInputBackend._vk_from_key
# ---------------------------------------------------------------------------


class TestVkFromKey:
    def test_symbols_do_not_collide_with_navigation_vk(self) -> None:
        """Regressione: '!'→0x21 (VK_PRIOR), '.'→0x2E (VK_DELETE), ecc."""
        w = WindowsInputBackend.__new__(WindowsInputBackend)
        navigation = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
        for sym in "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~":
            vk = w._vk_from_key(sym)
            assert vk not in navigation, f"'{sym}' → VK 0x{vk:02X} collide con navigazione"

    def test_symbols_map_to_expected_vk(self) -> None:
        w = WindowsInputBackend.__new__(WindowsInputBackend)
        cases = {
            "!": 0x31,
            "@": 0x32,
            ":": 0xBA,
            ";": 0xBA,
            ".": 0xBE,
            ",": 0xBC,
            "-": 0xBD,
            "+": 0xBB,
            "[": 0xDB,
            "\\": 0xDC,
            "]": 0xDD,
            "`": 0xC0,
            "/": 0xBF,
            "?": 0xBF,
            "~": 0xC0,
            "'": 0xDE,
        }
        for sym, expected in cases.items():
            assert w._vk_from_key(sym) == expected, f"'{sym}' → 0x{expected:02X}"

    def test_letters_and_digits(self) -> None:
        w = WindowsInputBackend.__new__(WindowsInputBackend)
        assert w._vk_from_key("a") == 0x41
        assert w._vk_from_key("A") == 0x41  # case-insensitive come gli altri backend
        assert w._vk_from_key("z") == 0x5A
        assert w._vk_from_key("1") == 0x31
        assert w._vk_from_key("0") == 0x30

    def test_named_keys(self) -> None:
        w = WindowsInputBackend.__new__(WindowsInputBackend)
        assert w._vk_from_key("altgr") == 0xA5  # VK_RMENU
        assert w._vk_from_key("return") == 0x0D
        assert w._vk_from_key("f1") == 0x70
        assert w._vk_from_key("f12") == 0x7B
        assert w._vk_from_key("ctrl") == 0x11

    def test_unknown_returns_zero(self) -> None:
        w = WindowsInputBackend.__new__(WindowsInputBackend)
        assert w._vk_from_key("ò") == 0


# ---------------------------------------------------------------------------
# RemoteViewer._key_to_name (cattura lato client)
# ---------------------------------------------------------------------------


class TestKeyToName:
    def _viewer(self):
        from opendesk.ui.viewer import RemoteViewer

        return RemoteViewer.__new__(RemoteViewer)

    def test_previously_missing_keys(self) -> None:
        """insert/print/menu/altgr/F13+ erano scartati prima del fix."""
        from PySide6.QtCore import Qt

        v = self._viewer()
        assert v._key_to_name(Qt.Key_Insert) == "insert"
        assert v._key_to_name(Qt.Key_Print) == "print"
        assert v._key_to_name(Qt.Key_Menu) == "menu"
        assert v._key_to_name(Qt.Key_AltGr) == "altgr"
        assert v._key_to_name(Qt.Key_F13) == "f13"
        assert v._key_to_name(Qt.Key_F24) == "f24"

    def test_case_is_preserved(self) -> None:
        """Regressione: 'A' non deve più diventare 'a' via .lower()."""
        v = self._viewer()
        assert v._key_to_name(ord("A")) == "A"
        assert v._key_to_name(ord("a")) == "a"

    def test_latin1_accents_not_dropped(self) -> None:
        """Le accentate (> 0x7E) arrivano al protocollo invece di essere scartate."""
        v = self._viewer()
        assert v._key_to_name(ord("ò")) == "ò"
        assert v._key_to_name(ord("à")) == "à"

    def test_control_codes_not_sent(self) -> None:
        from PySide6.QtCore import Qt

        v = self._viewer()
        assert v._key_to_name(Qt.Key_Return) == "return"
        assert v._key_to_name(0x7F) is None  # DEL non stampabile
        assert v._key_to_name(0x00) is None  # control non inviato


# ---------------------------------------------------------------------------
# Protocollo: messaggio CAPS_LOCK_STATE
# ---------------------------------------------------------------------------


class TestCapsLockStateMessage:
    def test_factory_and_roundtrip(self) -> None:
        msg = Message.caps_lock_state(True)
        assert msg.type == MessageType.CAPS_LOCK_STATE
        parsed = Message.decode(msg.encode())
        assert parsed.payload.get("active") is True

    def test_factory_false(self) -> None:
        msg = Message.caps_lock_state(False)
        assert msg.payload == {"active": False}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
