"""Test mirato: PyAV (encode software x264) affama la GIL?

Beat QTimer sul main thread + thread che encoda 1280x800 a 30 fps
con lo stesso VideoCodec usato dalla pipeline.
"""

from __future__ import annotations

import sys
import threading
import time

import numpy as np

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

    stop = threading.Event()

    def encode_loop() -> None:
        from opendesk.core.video_codec import EncoderConfig, VideoEncoder

        try:
            enc = VideoEncoder(
                EncoderConfig(width=1280, height=800, fps=30, codec="h264", crf=14)
            )
        except Exception as e:
            print(f"encoder init failed: {e}")
            return
        frame = np.zeros((800, 1280, 3), dtype=np.uint8)
        n = 0
        t0 = time.monotonic()
        while not stop.is_set():
            frame[:, :, 0] = (n * 3) % 255  # cambia il frame: encode reale
            enc.encode(frame)
            n += 1
        dt = time.monotonic() - t0
        print(f"  (encode: {n} frame in {dt:.1f}s = {n / dt:.1f} fps)")
        enc.release()

    th = threading.Thread(target=encode_loop, daemon=True)
    th.start()

    t_end = time.monotonic() + DURATION_S
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.005)
    stop.set()
    th.join(timeout=5)
    timer.stop()

    gaps = gaps[2:]
    max_gap = max(gaps) * 1000 if gaps else 0
    avg = (sum(gaps) / len(gaps) * 1000) if gaps else 0
    print(f"beat={len(gaps)}  avg={avg:.1f}ms  MAX={max_gap:.1f}ms")
    if max_gap > 1000:
        print("CONFERMATO: encode PyAV affama la GIL → main thread Qt congelato")
        return 1
    print("PyAV rilascia la GIL (max gap < 1s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
