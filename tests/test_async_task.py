"""Tests for the off-main-thread task helper and the session-hash flow."""

from __future__ import annotations

import sys
import threading

import pytest
from PySide6.QtCore import QEventLoop, QObject, QTimer
from PySide6.QtWidgets import QApplication

from opendesk.utils.async_task import run_async


@pytest.fixture(scope="session")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
        app.setStyle("Fusion")
    return app


def _run_loop_until(callback_holder: dict, timeout_ms: int = 5000) -> None:
    """Process Qt events until the callback sets ``callback_holder['done']``."""
    loop = QEventLoop()
    callback_holder["_loop"] = loop
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()


class TestRunAsync:
    def test_result_delivered_on_main_thread(self, qapp: QApplication) -> None:
        """The result callback runs on the thread that owns the parent."""
        parent = QObject()
        main_tid = threading.get_ident()
        seen: dict = {}

        def on_done(value: int) -> None:
            seen["value"] = value
            seen["tid"] = threading.get_ident()
            seen["_loop"].quit()

        run_async(lambda: 21 * 2, on_done, parent=parent)
        _run_loop_until(seen)

        assert seen["value"] == 42
        assert seen["tid"] == main_tid, "callback deve girare sul main thread"

    def test_error_callback(self, qapp: QApplication) -> None:
        """Exceptions are forwarded to on_error instead of crashing."""
        parent = QObject()
        seen: dict = {}

        def boom() -> None:
            raise ValueError("boom")

        def on_error(message: str) -> None:
            seen["error"] = message
            seen["_loop"].quit()

        run_async(boom, lambda _v: None, parent=parent, on_error=on_error)
        _run_loop_until(seen)

        assert "boom" in seen["error"]

    def test_worker_does_not_block_caller(self, qapp: QApplication) -> None:
        """run_async returns immediately while the work is still running."""
        parent = QObject()
        release = threading.Event()
        seen: dict = {}

        def slow() -> str:
            release.wait(timeout=2.0)
            return "done"

        def on_done(value: str) -> None:
            seen["value"] = value
            seen["_loop"].quit()

        run_async(slow, on_done, parent=parent)
        # If run_async blocked, this would never be reached before release.
        assert "value" not in seen
        release.set()
        _run_loop_until(seen)
        assert seen["value"] == "done"


class TestSessionHashFlow:
    def test_create_session_with_precomputed_hash(self) -> None:
        """A precomputed hash is used verbatim and verifies correctly."""
        from opendesk.crypto.auth import AuthManager, hash_password

        am = AuthManager()
        precomputed = hash_password("ABCD1234")
        session = am.create_session("ABCD1234", password_hash=precomputed)

        assert session.password_hash == precomputed
        assert am.verify_session(session.session_id, "ABCD1234")
        assert not am.verify_session(session.session_id, "WRONG")

    def test_refresh_session_does_not_register_pending_session(self, qapp: QApplication) -> None:
        """The widget only generates a password; MainWindow owns session creation.

        Regression: ``refresh_session`` used to call ``create_session`` too,
        hashing Argon2 on the main thread and leaving an orphan session.
        """
        from opendesk.crypto.auth import AuthManager
        from opendesk.ui.session_info import SessionInfoWidget

        auth = AuthManager()
        widget = SessionInfoWidget(device_id="uuid", device_name="PC")
        emitted: list[tuple[str, str]] = []
        widget.session_refreshed.connect(lambda sid, pwd: emitted.append((sid, pwd)))

        widget.refresh_session()

        assert len(auth._pending_sessions) == 0, "nessuna sessione orfana"
        assert len(emitted) == 1
        assert emitted[0][0] == ""
        assert emitted[0][1] == widget.password
