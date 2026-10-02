"""Test: BitBlt(CAPTUREBLT) a 30fps + manipolazione finestre = freeze win32k?

Mentre un thread cattura lo schermo con mss (BitBlt CAPTUREBLT) a ~30fps,
un altro thread minimizza/ripristina finestre reali (animazioni DWM via
messaggi Win32).  Misura il beat del main thread Qt.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

DURATION_S = 10.0


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)

    gaps: list[float] = []
    last = time.monotonic()

    def beat() -> None:
        nonlocal last
        now = time.monotonic()
        gaps.append(now - last)
        last = now

    timer = QTimer()
    timer.timeout.connect(beat)
    timer.start(20)

    # Finestre reali create sul main thread (churn via messaggi Win32)
    wins = []
    for i in range(3):
        w = QWidget()
        w.setWindowTitle(f"churn-{i}")
        w.resize(300, 200)
        w.show()
        wins.append(int(w.winId()))

    user32 = ctypes.windll.user32
    WM_SYSCOMMAND, SC_MINIMIZE, SC_RESTORE = 0x0112, 0x6008, 0x6000

    stop = threading.Event()
    import mss

    def grab_loop() -> None:
        sct = mss.mss()
        mon = sct.monitors[1]
        n = 0
        while not stop.is_set():
            sct.grab(mon)
            n += 1
        print(f"  (mss: {n} grab)")

    def window_churn() -> None:
        """Minimizza/ripristina le finestre (animazioni DWM) via Win32."""
        i = 0
        while not stop.is_set():
            hwnd = wins[i % len(wins)]
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0)
            time.sleep(0.15)
            user32.PostMessageW(hwnd, WM_SYSCOMMAND, SC_RESTORE, 0)
            time.sleep(0.15)
            i += 1

    th1 = threading.Thread(target=grab_loop, daemon=True)
    th2 = threading.Thread(target=window_churn, daemon=True)
    th1.start()
    th2.start()

    t_end = time.monotonic() + DURATION_S
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.005)
    stop.set()
    th1.join(timeout=2)
    th2.join(timeout=2)
    timer.stop()

    gaps = gaps[2:]
    max_gap = max(gaps) * 1000 if gaps else 0
    print(f"beat={len(gaps)}  MAX={max_gap:.1f}ms")
    if max_gap > 1000:
        print(f"CONFERMATO: BitBlt CAPTUREBLT + churn finestre → gap {max_gap:.0f}ms")
        return 1
    print("Nessun freeze (max gap < 1s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
