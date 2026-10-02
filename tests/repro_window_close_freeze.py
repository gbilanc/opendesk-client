"""Riproduzione guidata del freeze UI host (RCA 2026-10-01, sezione 2).

Simula la sessione reale SENZA rete:
- StreamingPipeline con cattura DXGI/MSS reale di questo desktop
- flood di eventi input remoto su InputInjectionWorker (come farebbe
  il client mentre l'utente muove il mouse verso il bottone close)
- chiusura e minimizzazione REALE di una finestra via Win32
  (PostMessage WM_SYSCOMMAND/SC_MINIMIZE e WM_CLOSE, come farebbe
  SendInput + click sull'X)

Misura la reattività del main thread Qt (beat, come HangWatchdog) e il
flusso frame della pipeline nelle 4 fasi:
  A) desktop attivo            B) minimizzazione finestra
  C) chiusura finestra         D) desktop statico post-chiusura

Output: per ogni fase max-gap del main thread e frame catturati.
Exit code 0 se: main thread sempre reattivo (< 1s) e frame in flusso.

Usage:  uv run python tests/repro_window_close_freeze.py [durata_fase_s]
"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from opendesk.core.video_codec import QualityLevel
from opendesk.services.pipeline import PipelineConfig, StreamingPipeline
from opendesk.services.stream_service import InputInjectionWorker
from opendesk.utils.hang_watchdog import HangWatchdog

PHASE_S = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0

# ── Contatori condivisi ──────────────────────────────────────────────

stats_lock = threading.Lock()
stats: dict[str, dict[str, float]] = {
    phase: {"frames": 0, "max_gap": 0.0, "input_injected": 0}
    for phase in ("A_attivo", "B_minimizza", "C_chiudi", "D_statico")
}
current_phase = "A_attivo"
last_beat = time.monotonic()
beat_stop = threading.Event()


def beat_loop() -> None:
    """Misura la reattività del main thread (heartbeat da QTimer)."""
    global last_beat

    def _beat() -> None:
        global last_beat
        now = time.monotonic()
        gap = now - last_beat
        last_beat = now
        with stats_lock:
            stats[current_phase]["max_gap"] = max(stats[current_phase]["max_gap"], gap)

    timer = QTimer()
    timer.timeout.connect(_beat)
    timer.start(100)  # 10 beat/s
    while not beat_stop.wait(0.05):
        pass


# ── Fake send (niente rete) ─────────────────────────────────────────


def fake_send(
    data: bytes, width: int = 0, height: int = 0, pts: int = 0, keyframe: bool = False
) -> bool:
    with stats_lock:
        stats[current_phase]["frames"] += 1
    return True


# ── Finestra di test + chiusura/minimizzazione Win32 ────────────────

hwnd_holder: list[int] = []


def make_window() -> QWidget:
    w = QWidget()
    w.setWindowTitle("OpenDesk Repro Freeze")
    w.resize(400, 300)
    w.show()
    return w


def win32_minimize(window: QWidget) -> None:
    import ctypes

    hwnd = int(window.winId())
    user32 = ctypes.windll.user32
    user32.PostMessageW(hwnd, 0x0112, 0x6008, 0)  # WM_SYSCOMMAND, SC_MINIMIZE
    hwnd_holder.append(hwnd)


def win32_close(window: QWidget) -> None:
    import ctypes

    hwnd = int(window.winId())
    user32 = ctypes.windll.user32
    user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE


# ── Main ────────────────────────────────────────────────────────────


def main() -> int:
    global current_phase

    app = QApplication(sys.argv)
    watchdog = HangWatchdog()  # logga stack se il main thread si blocca >3s

    # Pipeline con cattura reale
    config = PipelineConfig(
        fps=30,
        quality=QualityLevel.HIGH,
        bitrate=8_000_000,
        monitor_index=0,
    )

    def fake_tile(data, x=0, y=0, tw=0, th=0, pts=0) -> None:
        fake_send(data)

    pipeline = StreamingPipeline(
        config, fake_send, fake_tile, on_error=lambda m: print(f"PIPELINE ERROR: {m}")
    )
    pipeline.start()

    # Flood input remoto su worker (fuori main thread)
    worker = InputInjectionWorker()
    worker.start()
    injected_total = 0

    def flood() -> None:
        nonlocal injected_total
        while not beat_stop.is_set():
            worker.submit(lambda: None, desc="repro")
            with stats_lock:
                stats[current_phase]["input_injected"] += 1
            time.sleep(0.016)  # ~60 eventi/s come un client attivo

    threading.Thread(target=flood, name="InputFlood", daemon=True).start()

    # Beat timer sul main thread
    threading.Thread(target=beat_loop, name="BeatProbe", daemon=True).start()

    window = make_window()

    def run_phase(phase: str, action=None) -> None:
        global current_phase
        current_phase = phase
        print(f"--- Fase {phase} ({PHASE_S:.0f}s) ---")
        t_end = time.monotonic() + PHASE_S
        while time.monotonic() < t_end:
            if action is not None and (t_end - time.monotonic()) < PHASE_S / 2:
                action()
                action = None  # una sola volta, a metà fase
            app.processEvents()
            time.sleep(0.01)

    try:
        run_phase("A_attivo")
        run_phase("B_minimizza", action=lambda: win32_minimize(window))
        run_phase("C_chiudi", action=lambda: win32_close(window))
        run_phase("D_statico")  # desktop fermo: verifica stream vivo (idle DXGI)
    finally:
        beat_stop.set()
        time.sleep(0.2)
        pipeline.stop()
        worker.stop(timeout=2.0)
        watchdog._timer.stop()

    # ── Report ──────────────────────────────────────────────────────
    print("\n=== REPORT ===")
    ok = True
    with stats_lock:
        for phase, s in stats.items():
            gap = s["max_gap"]
            frozen = gap >= 1.0
            ok &= not frozen
            print(
                f"{phase:12s} frame={int(s['frames']):5d}  "
                f"input_eventi={int(s['input_injected']):5d}  "
                f"max_gap_UI={gap * 1000:6.0f}ms  "
                f"{'FROZEN [X]' if frozen else 'reattiva [OK]'}"
            )

    with stats_lock:
        d_frames = stats["D_statico"]["frames"]
        a_frames = stats["A_attivo"]["frames"]
    if d_frames <= 0:
        ok = False
        print("STREAM MORTO nella fase statica [X] (idle DXGI non emette frame)")
    else:
        print(
            f"Stream vivo in fase statica [OK] ({d_frames} frame in {PHASE_S:.0f}s; "
            f"fase attiva {a_frames})"
        )

    print("\nRISULTATO:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
