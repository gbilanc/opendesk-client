"""Badge colorato per stati (online/offline/transfer/etc.)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

# ── Palette stati ───────────────────────────────────────────────────
# (sfondo, testo, label)
_STATUS_PALETTE: dict[str, tuple[str, str, str]] = {
    "online":    ("#d1fae5", "#065f46", "Online"),
    "offline":   ("#fee2e2", "#991b1b", "Offline"),
    "connected": ("#dbeafe", "#1e40af", "Connesso"),
    "transfer":  ("#fef3c7", "#92400e", "Trasferimento"),
    "error":     ("#fee2e2", "#991b1b", "Errore"),
    "pending":   ("#f3f4f6", "#374151", "In attesa"),
    "active":    ("#d1fae5", "#065f46", "Attivo"),
    "idle":      ("#f3f4f6", "#374151", "Inattivo"),
}


class StatusBadge(QLabel):
    """Badge per stato con colori e testo predefiniti.

    Usage::

        badge = StatusBadge("online")
        badge.set_status("error")
    """

    def __init__(self, status: str = "pending", parent=None) -> None:
        super().__init__(parent)
        self._status = status.lower()
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(22)
        self.setMinimumWidth(70)
        self.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._update_style()

    # ── public API ──────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    def set_status(self, status: str) -> None:
        """Cambia lo stato e aggiorna colori."""
        self._status = status.lower()
        self._update_style()

    # ── internal ────────────────────────────────────────────────────

    def _update_style(self) -> None:
        bg, fg, text = _STATUS_PALETTE.get(self._status, ("#f3f4f6", "#374151", self._status))
        self.setText(text)
        self.setStyleSheet(
            f"StatusBadge {{"
            f"  background-color: {bg};"
            f"  color: {fg};"
            f"  font-size: 11px;"
            f"  font-weight: 600;"
            f"  padding: 1px 10px;"
            f"  border-radius: 11px;"
            f"}}"
        )
