"""
Test di regressione per il freeze della UI host su Windows.

RCA 2026-10-01: quando l'utente remoto chiudeva/minimizzava una finestra
qualsiasi sul PC controllato, la UI Qt dell'host si congelava.  Fix:
iniezione input spostata su un thread dedicato (InputInjectionWorker),
clipboard polling difensivo (backoff su letture lente), path idle DXGI
a prova di eccezione transitoria.

Questi test verificano i tre meccanismi di protezione.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

from opendesk.network.protocol import Message
from opendesk.services.stream_service import InputInjectionWorker, StreamService

# ── QApplication fixture (per StreamService/ClipboardSync) ───────────


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
    return app


class FakeBackend:
    """InputBackend minimo che registra le chiamate (thread-safe)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls: list[tuple] = []

    def move_mouse(self, x: int, y: int, absolute: bool = True) -> None:
        with self.lock:
            self.calls.append(("move", x, y))

    def click_mouse(self, button, state) -> None:
        with self.lock:
            self.calls.append(("click", button, state))

    def key_event(self, key, state) -> None:
        with self.lock:
            self.calls.append(("key", key, state))

    def scroll_mouse(self, dx: int, dy: int) -> None:
        with self.lock:
            self.calls.append(("scroll", dx, dy))

    def set_screen_size(self, w: int, h: int) -> None:
        with self.lock:
            self.calls.append(("screen", w, h))

    def release(self) -> None: ...


# ═══════════════════════════════════════════════════════════════════
# InputInjectionWorker
# ═══════════════════════════════════════════════════════════════════


class TestInputInjectionWorker:
    def test_fifo_order(self) -> None:
        """Gli eventi vengono iniettati in ordine (click = sequenza atomica)."""
        w = InputInjectionWorker()
        order: list[int] = []
        w.start()
        try:
            for i in range(20):
                w.submit(lambda i=i: order.append(i), desc="test")
            # Attendi svuotamento coda
            deadline = time.monotonic() + 5.0
            while len(order) < 20 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert order == list(range(20))
        finally:
            w.stop()

    def test_survives_exception(self) -> None:
        """Un'eccezione nell'iniezione non uccide il worker (e la UI non se ne accorge)."""
        w = InputInjectionWorker()
        done: list[bool] = []
        w.start()
        try:
            w.submit(lambda: 1 / 0, desc="boom")  # type: ignore[arg-type]
            w.submit(lambda: done.append(True), desc="after")
            deadline = time.monotonic() + 5.0
            while not done and time.monotonic() < deadline:
                time.sleep(0.01)
            assert done == [True]
            assert w.is_alive()
        finally:
            w.stop()

    def test_queue_full_drops_event(self) -> None:
        """Con coda piena l'evento viene scartato (con warning), non blocca il chiamante."""
        w = InputInjectionWorker()
        # Worker NON avviato: la coda si riempie
        submitted = sum(w.submit(lambda: None, desc="fill") for _ in range(2048))
        assert submitted <= 512  # _INPUT_QUEUE_MAX
        # Le submit oltre la capienza restituiscono False senza bloccare
        assert w.submit(lambda: None, desc="overflow") is False

    def test_stop_terminates_promptly(self) -> None:
        w = InputInjectionWorker()
        w.start()
        t0 = time.monotonic()
        w.stop(timeout=1.0)
        assert time.monotonic() - t0 < 2.0
        assert not w.is_alive()
        # Submit dopo stop → scartata
        assert w.submit(lambda: None, desc="late") is False

    def test_slow_injection_does_not_block_caller(self) -> None:
        """submit() ritorna subito anche se l'iniezione è lenta (API OS hung)."""
        w = InputInjectionWorker()
        w.start()
        try:
            release = threading.Event()
            w.submit(release.wait, desc="hung-api")  # type: ignore[arg-type]
            time.sleep(0.1)  # lascia partire il job
            t0 = time.perf_counter()
            w.submit(lambda: None, desc="quick")
            assert time.perf_counter() - t0 < 0.5  # non attende release.wait
        finally:
            release.set()
            w.stop(timeout=2.0)


# ═══════════════════════════════════════════════════════════════════
# StreamService → iniezione off main-thread
# ═══════════════════════════════════════════════════════════════════


class TestStreamServiceInputOffMainThread:
    def test_inject_mouse_delegates_to_worker(self, qapp: QApplication) -> None:
        """inject_mouse NON esegue inline: passa dal worker."""
        svc = StreamService.__new__(StreamService)  # evita init Qt completo
        # Inizializza solo ciò che serve
        svc._input_backend = FakeBackend()
        svc._input_worker = InputInjectionWorker()
        svc._input_worker.start()
        try:
            t0 = time.perf_counter()
            svc.inject_mouse(Message.mouse_event(100, 200))
            elapsed = time.perf_counter() - t0
            # Il main thread NON deve eseguire l'iniezione inline
            assert elapsed < 0.2
            # L'evento arriva comunque al backend
            backend: FakeBackend = svc._input_backend
            deadline = time.monotonic() + 5.0
            while not backend.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert backend.calls == [("move", 100, 200)]
        finally:
            svc._input_worker.stop()

    def test_inject_keyboard_delegates_to_worker(self, qapp: QApplication) -> None:
        svc = StreamService.__new__(StreamService)
        svc._input_backend = FakeBackend()
        svc._input_worker = InputInjectionWorker()
        svc._input_worker.start()
        try:
            svc.inject_keyboard(Message.keyboard_event("a", True))
            backend: FakeBackend = svc._input_backend
            deadline = time.monotonic() + 5.0
            while not backend.calls and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(backend.calls) == 1
            assert backend.calls[0][0] == "key"
            assert backend.calls[0][1] == "a"
        finally:
            svc._input_worker.stop()

    def test_inline_fallback_without_worker(self, qapp: QApplication) -> None:
        """Senza worker (streaming non attivo) l'evento non si perde: esegue inline."""
        svc = StreamService.__new__(StreamService)
        svc._input_backend = FakeBackend()
        svc._input_worker = None
        svc.inject_mouse(Message.mouse_event(5, 6))
        backend: FakeBackend = svc._input_backend
        assert backend.calls == [("move", 5, 6)]


# ═══════════════════════════════════════════════════════════════════
# ClipboardSync — backoff anti-freeze
# ═══════════════════════════════════════════════════════════════════


class TestClipboardBackoff:
    def test_slow_read_applies_backoff(self, qapp: QApplication) -> None:
        """Una lettura OLE lenta (>100ms) rallenta il polling invece di bloccare la UI."""
        from opendesk.core.clipboard_sync import (
            _BACKOFF_MAX_MS,
            _POLL_INTERVAL_MS,
            ClipboardSync,
        )

        sync = ClipboardSync()
        sync._send_fn = lambda msg: None  # noqa: ARG005 — abilita il polling
        sync._enabled = True

        class SlowMime:
            def hasText(self) -> bool:  # noqa: N802 — API Qt
                return False

            def hasImage(self) -> bool:  # noqa: N802
                return False

        def slow_mime_data():
            time.sleep(0.15)  # simula owner OLE lento/hung
            return SlowMime()

        sync._clipboard.mimeData = slow_mime_data  # type: ignore[method-assign]

        t0 = time.perf_counter()
        sync._poll_clipboard()
        elapsed = time.perf_counter() - t0
        # La lettura lenta è avvenuta (0.15s) — non è bloccata all'infinito
        assert elapsed >= 0.15
        assert sync._timer.interval() > _POLL_INTERVAL_MS
        assert sync._timer.interval() <= _BACKOFF_MAX_MS

        sync.stop()

    def test_backoff_escalates(self, qapp: QApplication) -> None:
        from opendesk.core.clipboard_sync import (
            _BACKOFF_MAX_MS,
            _POLL_INTERVAL_MS,
            ClipboardSync,
        )

        sync = ClipboardSync()
        sync._last_slow_poll = time.monotonic()  # appena vista una lettura lenta
        sync._backoff_ms = _POLL_INTERVAL_MS
        sync._apply_backoff()
        first = sync._backoff_ms
        assert first == _POLL_INTERVAL_MS * 4
        sync._apply_backoff()
        sync._apply_backoff()
        assert sync._backoff_ms == min(_BACKOFF_MAX_MS, first * 16)
        assert sync._backoff_ms <= _BACKOFF_MAX_MS
        sync.stop()
