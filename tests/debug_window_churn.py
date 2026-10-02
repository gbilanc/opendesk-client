"""Churn di FINESTRE REALI del desktop (come l'utente remoto): minimizza,
ripristina e ri-chiude le finestre top-level visibili, in rotazione.

Usage: uv run python tests/debug_window_churn.py [durata_s]
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
WM_SYSCOMMAND, SC_MINIMIZE, SC_RESTORE = 0x0112, 0x6008, 0x6000


def collect_windows() -> list[int]:
    res: list[int] = []

    @EnumWindowsProc
    def cb(h, _):
        if not user32.IsWindowVisible(h):
            return True
        if user32.IsIconic(h):
            return True
        buf = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(h, buf, 128)
        title = buf.value.strip()
        if not title or user32.GetWindowLongW(h, -8) == 0:  # GWL_HWNDPARENT
            return True
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(h, cls, 64)
        if cls.value in ("Shell_TrayWnd", "Windows.UI.Core.CoreWindow", "Progman"):
            return True
        res.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return res


def main() -> int:
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 60.0
    wins = collect_windows()
    print(f"{len(wins)} finestre trovate: {[w for w in wins]}")
    t_end = time.monotonic() + duration
    i = 0
    while time.monotonic() < t_end:
        for h in wins:
            if time.monotonic() >= t_end:
                break
            user32.PostMessageW(h, WM_SYSCOMMAND, SC_MINIMIZE, 0)
            time.sleep(0.6)
            user32.PostMessageW(h, WM_SYSCOMMAND, SC_RESTORE, 0)
            time.sleep(0.6)
            i += 1
    print(f"{i} cicli minimize/restore")
    return 0


if __name__ == "__main__":
    sys.exit(main())
