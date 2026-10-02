"""Riproduttore controllato dello stallo UI host (RCA sezione 2, 2a iterazione).

Setup end-to-end REALE senza relay pubblico:
- mock relay (coppia host+client, relay trasparente di tutti i messaggi)
- HostService + HostWindow Qt reali ( DXGI/MSS capture reale, encoder reale)
- RelayClient reale in ruolo client (auth + streaming + input flood)

Fasi e misura:
  P0 baseline streaming
  P1 showMinimized() finestra host          (trigger sospetto #1)
  P2 showNormal()   finestra host           (quirk top-most → setWindowFlags)
  P3 flood input dal client (~60 msg/s)
  P4 close() → hide-to-tray + riapertura    (trigger sospetto #2)

Il beat QTimer (20ms) sul main thread misura i gap; stalli > 1s = FAIL.
Viene misurato anche il flusso frame host→client (wrapper su NetworkWorker).

Usage:  uv run python tests/repro_session_ui_stall.py
"""

from __future__ import annotations

import sys
import threading
import time

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from opendesk.host_app import HostService, HostWindow  # noqa: E402
from opendesk.network.protocol import Message  # noqa: E402
from opendesk.network.relay_client import RelayClient  # noqa: E402

PASSWORD = "repro-pass-123"
PHASE_S = 5.0
STALL_DUMP_FILE = "stall_dump.txt"


def _dump_on_stall(gap: float) -> None:
    """Scrive le finestre di campionamento raccolte durante lo stallo."""
    try:
        with _samples_lock:
            samples = list(_samples)
        print(f"[stall] gap={gap:.2f}s, samples={len(samples)}")
        with open(STALL_DUMP_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== STALL {gap:.2f}s =====\n")
            for ts, text in samples[-40:]:
                f.write(f"[{ts}]\n{text}\n")
    except Exception:
        pass


# Ring buffer di campionamento continuo dei thread (dal monitor thread)
_samples: list[tuple[str, str]] = []
_samples_lock = threading.Lock()
_sample_stop = threading.Event()


def _sample_loop() -> None:
    """Campiona lo stack di tutti i thread ogni 100ms (main thread incluso)."""
    import traceback

    main_tid = threading.main_thread().ident
    while not _sample_stop.is_set():
        try:
            frames = sys._current_frames()
            lines = []
            for tid, frame in frames.items():
                name = next(
                    (t.name for t in threading.enumerate() if t.ident == tid),
                    f"tid-{tid}",
                )
                marker = " <MAIN>" if tid == main_tid else ""
                if marker:
                    # Solo il main thread: le altre code sono note e in quiet
                    stack = "".join(traceback.format_stack(frame)[-4:])
                    lines.append(f"--- {name}{marker} ---\n{stack}")
            ts = time.strftime("%H:%M:%S") + f".{int((time.monotonic() % 1) * 1000):03d}"
            with _samples_lock:
                _samples.append((ts, "\n".join(lines)))
                if len(_samples) > 600:
                    del _samples[:300]
        except Exception:
            pass
        time.sleep(0.1)


def main() -> int:
    # Faulthandler: dump periodico SENZA GIL (thread C) — cattura chi la tiene
    import faulthandler

    fh_file = open("fh_dump.txt", "w", encoding="utf-8")
    faulthandler.dump_traceback_later(1.0, repeat=True, file=fh_file)
    faulthandler.enable(fh_file)  # cattura segfault (RCA: crash a close/tray?)

    # ── Settings: punta l'host al mock relay ──
    settings = QSettings("OpenDesk", "OpenDesk")
    saved_host = settings.value("network/relay_host", "")
    saved_port = settings.value("network/relay_port", 8474)

    # Relay REALE locale (opendesk-relay su 127.0.0.1:8474)
    relay_port = 8474
    if "--relay-port" in sys.argv:
        relay_port = int(sys.argv[sys.argv.index("--relay-port") + 1])
    settings.setValue("network/relay_host", "127.0.0.1")
    settings.setValue("network/relay_port", relay_port)
    print(f"[setup] relay reale locale su 127.0.0.1:{relay_port}")

    app = QApplication(sys.argv)

    # ── Beat probe sul main thread ──
    beat_lock = threading.Lock()
    beat_gaps: list[tuple[str, float]] = []
    phase_name = ["setup"]
    last_beat = time.monotonic()

    def beat() -> None:
        nonlocal last_beat
        now = time.monotonic()
        with beat_lock:
            beat_gaps.append((phase_name[0], now - last_beat))
            gap = now - last_beat
        last_beat = now
        if gap > 0.5:
            _dump_on_stall(gap)

    beat_timer = QTimer()
    beat_timer.timeout.connect(beat)
    beat_timer.start(20)

    threading.Thread(target=_sample_loop, name="StackSampler", daemon=True).start()

    # ── Host reale ──
    service = HostService()
    session_id, password = service.create_session(PASSWORD)
    window = HostWindow(service)
    window.show()

    peer_event = threading.Event()
    service.peer_connected.connect(lambda *a: peer_event.set())
    service.start()
    print("[setup] host avviato, attendo peer...")

    # ── Client reale (dopo che l'host è registrato sul relay) ──
    client = RelayClient()
    joined = False
    for attempt in range(3):
        time.sleep(2.0)  # margine per la registrazione host sul relay
        client.join_session(
            "127.0.0.1",
            relay_port,
            session_id.replace(" ", ""),  # il relay registra l'id senza spazi
            password,
            device_id="repro-client",
        )
        t_end = time.monotonic() + 10
        while not peer_event.is_set() and time.monotonic() < t_end:
            app.processEvents()
            time.sleep(0.01)
        if peer_event.is_set():
            joined = True
            break
        print(f"[setup] tentativo join #{attempt + 1} fallito, riprovo...")
        client.disconnect()
    if not joined:
        print("[FAIL] peer non connesso dopo 3 tentativi")
        return 1
    print("[setup] peer connesso — streaming attivo")

    # Attendi che la pipeline giri (pompa gli eventi Qt: il beat QTimer
    # richiede processEvents — un sleep puro sembrerebbe uno stallo!)
    t_end = time.monotonic() + 3.0
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)

    # Conta i pacchetti inviati host→client (wrapper sul NetworkWorker)
    frames_sent = 0
    frames_lock = threading.Lock()

    def wrap_send() -> None:
        nonlocal frames_sent
        stream = service._stream
        if stream is None or stream._pipeline is None:
            return
        nw = stream._pipeline._network_worker

        orig_frame = nw._send_frame

        def counting_frame(*a, **kw):
            nonlocal frames_sent
            with frames_lock:
                frames_sent += 1
            return orig_frame(*a, **kw)

        orig_tile = nw._send_tile

        def counting_tile(*a, **kw):
            nonlocal frames_sent
            with frames_lock:
                frames_sent += 1
            return orig_tile(*a, **kw)

        nw._send_frame = counting_frame
        nw._send_tile = counting_tile

    QTimer.singleShot(0, wrap_send)

    # ── Fasi ──
    phase_stats: dict[str, list[float]] = {}

    def run_phase(name: str, action=None, at_start=False) -> None:
        phase_name[0] = name
        phase_stats[name] = []
        with _samples_lock:
            _samples.clear()
        print(f"[fase] {name} ({PHASE_S:.0f}s)")
        if action is not None and at_start:
            QTimer.singleShot(0, action)
        t_end = time.monotonic() + PHASE_S
        while time.monotonic() < t_end:
            t_pe = time.perf_counter()
            app.processEvents()
            phase_stats[name].append(time.perf_counter() - t_pe)
            time.sleep(0.01)
        ps = phase_stats[name]
        print(
            f"  processEvents: n={len(ps)} max={max(ps) * 1000:.0f}ms "
            f"tot={sum(ps) * 1000:.0f}ms"
        )

    def do_minimize() -> None:
        window.showMinimized()

    def do_restore() -> None:
        window.showNormal()

    def input_flood() -> None:
        def flood_loop() -> None:
            t_end = time.monotonic() + PHASE_S
            i = 0
            while time.monotonic() < t_end:
                client.send_message(Message.mouse_event((i * 7) % 1280, (i * 11) % 800))
                i += 1
                time.sleep(1 / 60)

        threading.Thread(target=flood_loop, daemon=True).start()

    def do_close() -> None:
        window.close()  # closeEvent → hide to tray

    def do_reshow() -> None:
        window.show()

    run_phase("P0_baseline")
    run_phase("P1_minimize", do_minimize, at_start=True)
    run_phase("P2_restore", do_restore, at_start=True)
    run_phase("P3_input_flood", input_flood, at_start=True)
    run_phase("P4_close_tray", do_close, at_start=True)
    run_phase("P5_reshow", do_reshow, at_start=True)

    # ── Report ──
    with beat_lock:
        gaps = beat_gaps
    print("\n=== REPORT beat main thread ===")
    ok = True
    per_phase: dict[str, float] = {}
    for ph, g in gaps:
        per_phase[ph] = max(per_phase.get(ph, 0.0), g)
    for ph in sorted(per_phase):
        g = per_phase[ph]
        # "setup" include create_session (argon2, ~2s su main thread):
        # costo fisiologico di avvio, non uno stallo di sessione.
        stall = g >= 1.0 and ph != "setup"
        status = "STALL!" if stall else ("ok (startup)" if ph == "setup" else "ok")
        if stall:
            ok = False
        print(f"{ph:14s} max_gap={g * 1000:7.1f}ms  {status}")
    with frames_lock:
        print(f"frame/pacchetti inviati (dopo wrapper): {frames_sent}")
        if frames_sent == 0:
            ok = False
            print("NESSUN pacchetto inviato → fps 0.0 lato client")

    # ── Cleanup ──
    try:
        service.stop()
    except Exception:
        pass
    try:
        client.disconnect()
    except Exception:
        pass
    beat_timer.stop()
    faulthandler.cancel_dump_traceback_later()
    fh_file.close()
    settings.setValue("network/relay_host", saved_host)
    settings.setValue("network/relay_port", saved_port)
    print("\nRISULTATO:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
