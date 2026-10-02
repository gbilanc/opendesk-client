"""Client di carico per debug: join via relay pubblico + input flood.

Genera esattamente il traffico di un client reale (auth, keyframe
watchdog, mouse 60/s) SENZA UI.  Il video ricevuto viene scartato.

Usage: uv run python tests/debug_client_load.py <session_id> <password> [durata_s]
"""

from __future__ import annotations

import queue
import sys
import threading
import time

from opendesk.network.protocol import Message
from opendesk.network.relay_client import RelayClient


def main() -> int:
    session_id = sys.argv[1].replace(" ", "")
    password = sys.argv[2]
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0

    client = RelayClient()
    client.join_session("gibisoft.net", 8474, session_id, password, device_id="debug-load")

    # Polla l'inbox in un thread (scarta tutto: ci interessa solo generare carico)
    def drain() -> None:
        while not stop.is_set():
            try:
                while True:
                    client._inbox.get_nowait()
            except queue.Empty:
                pass
            time.sleep(0.05)

    stop = threading.Event()
    threading.Thread(target=drain, daemon=True).start()

    # Flood input ~60/s (come un client con mouse in movimento)
    t_end = time.monotonic() + duration
    i = 0
    while time.monotonic() < t_end:
        client.send_message(Message.mouse_event((i * 13) % 1280, (i * 7) % 800, absolute=True))
        i += 1
        time.sleep(1 / 60)

    stop.set()
    client.disconnect()
    print(f"inviati {i} eventi input")
    return 0


if __name__ == "__main__":
    sys.exit(main())
