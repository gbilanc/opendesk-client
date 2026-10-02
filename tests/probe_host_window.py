"""Guida la finestra dell'host GIA' IN ESECUZIONE attraverso
minimize → restore (i messaggi Win32 che Qt gestisce sul main thread),
mentre il HangWatchdog nel processo host misura da solo gli stalli.

Usage: uv run python tests/probe_host_window.py
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

import time

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def find_host_window() -> int:
    hwnd = user32.FindWindowW(None, "OpenDesk Host")
    if hwnd:
        return hwnd
    found = []

    @EnumWindowsProc
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(h, buf, 256)
        if "OpenDesk" in buf.value and user32.IsWindowVisible(h):
            found.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else 0


def main() -> int:
    hwnd = find_host_window()
    if not hwnd:
        print("Finestra OpenDesk Host non trovata (forse nascosta in tray)")
        return 1
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    print(f"Finestra trovata: hwnd={hwnd:#x} title={buf.value!r}")

    WM_SYSCOMMAND = 0x0112
    SC_MINIMIZE = 0x6008
    SC_RESTORE = 0x6000

    print("t+0s   : SC_MINIMIZE (come click sul bottone riduci)")
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    time.sleep(12)

    print("t+12s  : SC_RESTORE")
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
    time.sleep(12)

    print("t+24s  : secondo ciclo minimize→restore rapido")
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    time.sleep(3)
    user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
    time.sleep(8)

    print("Probe completato — controlla il log host per i dump del watchdog")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
