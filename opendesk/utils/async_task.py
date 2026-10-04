"""Esecuzione di lavoro bloccante fuori dal main thread Qt.

``hash_password`` (Argon2) è CPU-bound e dura da decine a centinaia di
millisecondi: eseguirlo dentro uno ``Slot`` Qt congela la UI.  ``run_async``
sposta il lavoro su un thread e consegna il risultato al thread principale
tramite una connessione queued.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

logger = logging.getLogger(__name__)

# Riferimenti forti ai bridge in volo: senza di essi il wrapper Python del
# ``QObject`` potrebbe essere raccolto dal GC prima che l'evento queued
# venga consegnato, perdendo il risultato del task.
_pending: set[_TaskSignals] = set()


class _TaskSignals(QObject):
    """Bridge segnali worker → main thread."""

    done = Signal(object)
    error = Signal(str)


def run_async(
    fn: Callable[[], Any],
    on_done: Callable[[Any], None],
    *,
    parent: QObject,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Esegue ``fn`` su un thread e consegna il risultato sul main thread.

    Parameters
    ----------
    fn :
        Lavoro bloccante (non deve toccare la UI).
    on_done :
        Callback invocata sul thread di ``parent`` con il risultato di ``fn``.
    parent :
        Oggetto Qt a vita lunga: possiede il bridge dei segnali.
    on_error :
        Callback invocata sul thread di ``parent`` se ``fn`` solleva.
    """
    signals = _TaskSignals(parent)
    _pending.add(signals)

    def _deliver_done(result: Any) -> None:
        _pending.discard(signals)
        on_done(result)

    def _deliver_error(message: str) -> None:
        _pending.discard(signals)
        if on_error is not None:
            on_error(message)

    # QueuedConnection: il slot gira nel thread a cui appartiene ``signals``
    # (il main thread), indipendentemente da chi emette.
    signals.done.connect(_deliver_done, Qt.ConnectionType.QueuedConnection)
    signals.error.connect(_deliver_error, Qt.ConnectionType.QueuedConnection)

    def _worker() -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001 — l'errore torna alla UI
            logger.exception("run_async: task fallito")
            signals.error.emit(str(exc))
        else:
            signals.done.emit(result)

    threading.Thread(target=_worker, daemon=True, name="AsyncTask").start()
