"""Esperimento: la cattura DXGI (dxcam) affama la GIL e congela la UI Qt?

Misura il beat del main thread Qt mentre un thread esegue in loop:
  A) dxcam.grab() a 30 fps (path cattura DXGI)
  B) nulla (baseline)
Se i gap del beat esplodono solo con dxcam attivo → dxcam non rilascia
la GIL → il main thread Qt apparente "congelato" durante lo streaming.
"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

DURATION_S = 8.0


def measure(label: str, target) -> float:
    """Esegue `target` su un thread mentre misura il beat del main thread."""
    gaps: list[float] = []
    last = time.monotonic()
    stop = threading.Event()
    timer_held = threading.Event()

    def beat() -> None:
        nonlocal last
        now = time.monotonic()
        gaps.append(now - last)
        last = now

    def runner() -> None:
        timer_held.wait(0.5)
        target(stop)
        stop.set()

    app = QApplication.instance() or QApplication(sys.argv)
    timer = QTimer()
    timer.timeout.connect(beat)
    timer.start(50)
    th = threading.Thread(target=runner, daemon=True)
    th.start()

    t_end = time.monotonic() + DURATION_S
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.005)
        if stop.is_set():
            break
    timer.stop()
    th.join(timeout=1)

    gaps = gaps[2:]  # scarta il primo (avvio)
    max_gap = max(gaps) if gaps else 0.0
    avg_gap = sum(gaps) / len(gaps) if gaps else 0.0
    print(
        f"{label:20s} beat={len(gaps):4d}  avg={avg_gap * 1000:6.1f}ms  "
        f"MAX={max_gap * 1000:7.1f}ms"
    )
    return max_gap


def dxcam_loop(stop: threading.Event) -> None:
    try:
        import dxcam

        cam = dxcam.create(output_idx=0, output_color="RGB")
        if cam is None:
            print("dxcam non disponibile")
            return
        n = 0
        t0 = time.monotonic()
        while not stop.is_set():
            cam.grab()
            n += 1
            time.sleep(0.01)  # ~30 fps con grab istantaneo
        dt = time.monotonic() - t0
        print(f"  (dxcam: {n} grab in {dt:.1f}s = {n / dt:.1f} fps)")
    except Exception as e:
        print(f"  dxcam error: {e}")


def idle_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        time.sleep(0.03)


def main() -> int:
    print("— Baseline (thread idle) —")
    g0 = measure("baseline-idle", idle_loop)
    print("— Thread con dxcam.grab() a ~30fps —")
    g1 = measure("dxcam-30fps", dxcam_loop)
    print()
    print(f"baseline max={g0 * 1000:.0f}ms  dxcam max={g1 * 1000:.0f}ms")
    if g1 > 1.0:
        print("CONFERMATO: dxcam affama la GIL → UI Qt congelata durante lo streaming")
        return 1
    print("dxcam non affama la GIL (max gap < 1s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
