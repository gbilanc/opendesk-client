#!/usr/bin/env python3
"""Misurazione RAM di opendesk-host su Windows (step 1/11 del piano).

Usage:
    python tools/measure_ram.py --duration 300 --interval 2 --out ram_report.csv
    python tools/measure_ram.py --pid 12345 --duration 120

Registra ogni *interval* secondi la Working Set e il Private Bytes del
processo opendesk-host (o del PID specificato) e li salva in CSV.

Procedura di test raccomandata (eseguire su una macchina Windows reale):
  1. Avvia opendesk-host e misura 5 min senza connessioni  (baseline idle)
  2. Connetti un client e avvia lo streaming               (baseline stream)
  3. Riesegui dopo il deploy della patch e confronta i delta
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

try:
    import psutil  # pip install psutil
except ImportError:
    sys.exit("psutil richiesto: pip install psutil")


def find_opendesk_host_process() -> psutil.Process | None:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if (
                "opendesk" in name
                or "opendesk" in cmdline
                or "host_app" in cmdline
            ):
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, help="PID del processo da monitorare")
    parser.add_argument("--duration", type=int, default=300, help="Durata misura in s")
    parser.add_argument("--interval", type=float, default=2.0, help="Campionamento in s")
    parser.add_argument("--out", default="ram_report.csv")
    args = parser.parse_args()

    proc = (
        psutil.Process(args.pid)
        if args.pid
        else find_opendesk_host_process()
    )
    if proc is None:
        sys.exit("Processo opendesk-host non trovato (usa --pid)")

    print(f"Monitoraggio PID {proc.pid}: {proc.name()}")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["t", "working_set_mb", "private_mb", "threads"])
        t0 = time.time()
        while time.time() - t0 < args.duration:
            mem = proc.memory_info()
            writer.writerow([
                round(time.time() - t0, 1),
                round(mem.rss / 1024 / 1024, 1),
                round(mem.private / 1024 / 1024, 1),
                proc.num_threads(),
            ])
            f.flush()
            time.sleep(args.interval)
    print(f"Report scritto in {args.out}")


if __name__ == "__main__":
    main()
