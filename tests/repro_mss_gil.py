"""Test: mss (GDI BitBlt) rilascia la GIL durante la cattura?"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

DURATION_S = 6.0


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

    import mss

    sct = mss.mss()
    mon = sct.monitors[1]
    stop = threading.Event()
    n = 0

    def grab_loop() -> None:
        nonlocal n
        t0 = time.monotonic()
        while not stop.is_set():
            raw = sct.grab(mon)
            buf = bytearray(raw.rgb)
            n += 1
        dt = time.monotonic() - t0
        print(f"  (mss: {n} grab in {dt:.1f}s = {n / dt:.1f} fps)")

    th = threading.Thread(target=grab_loop, daemon=True)
    th.start()

    t_end = time.monotonic() + DURATION_S
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.005)
    stop.set()
    th.join(timeout=2)
    timer.stop()

    gaps = gaps[2:]
    max_gap = max(gaps) * 1000 if gaps else 0
    print(f"beat={len(gaps)}  MAX={max_gap:.1f}ms")
    if max_gap > 1000:
        print("mss affama la GIL")
        return 1
    print("OK: mss rilascia la GIL")
    return 0


if __name__ == "__main__":
    sys.exit(main())
