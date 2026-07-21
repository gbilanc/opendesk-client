"""Widget per mostrare lo stato di salute della piattaforma nella status bar."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QBrush, QPen, QFont
from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QWidget, QToolTip

from opendesk.core.platform_config import (
    get_platform_config,
    HealthSeverity,
    HealthIssue,
)


class HealthStatusWidget(QFrame):
    """Indicator nella status bar che mostra lo stato della piattaforma.

    Colori:
    - 🟢 Verde: tutto ok
    - 🟡 Giallo: warning (dipendenze opzionali mancanti)
    - 🔴 Rosso: critico (feature non funzionanti)

    Cliccando mostra i dettagli via tooltip.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setToolTip("Click per dettagli piattaforma")
        self._issues: list[HealthIssue] = []
        self._max_severity: HealthSeverity | None = None
        self._refresh()

    def _refresh(self) -> None:
        """Rileva issues e aggiorna stato."""
        cfg = get_platform_config()
        self._issues = cfg.check_health()
        # Determina la massima severità
        self._max_severity = None
        for i in self._issues:
            if i.severity == HealthSeverity.CRITICAL:
                self._max_severity = HealthSeverity.CRITICAL
                break
            if i.severity == HealthSeverity.WARNING:
                self._max_severity = HealthSeverity.WARNING
            elif self._max_severity is None:
                self._max_severity = HealthSeverity.INFO

        # Tooltip con dettagli
        if self._issues:
            lines = [f"{cfg.display_name} — stato piattaforma:"]
            for i in self._issues:
                icon = {
                    HealthSeverity.CRITICAL: "🔴",
                    HealthSeverity.WARNING: "🟡",
                    HealthSeverity.INFO: "ℹ️",
                }[i.severity]
                lines.append(f"{icon} {i.message[:60]}")
            self.setToolTip("\n".join(lines))
        else:
            self.setToolTip(f"{cfg.display_name} — ✅ tutto ok")

        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        """Disegna un pallino colorato."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        color = QColor("#22c55e")  # verde default
        if self._max_severity == HealthSeverity.CRITICAL:
            color = QColor("#ef4444")  # rosso
        elif self._max_severity == HealthSeverity.WARNING:
            color = QColor("#f59e0b")  # giallo

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(color.darker(130), 1))
        painter.drawEllipse(3, 3, 16, 16)

        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Click per refreshare immediatamente il controllo."""
        self._refresh()
        super().mousePressEvent(event)


class HealthSummaryDialog(QFrame):
    """Pannello compatto con la lista completa dei problemi di salute.

    Usato per mostrare i problemi all'avvio o su richiesta.
    Non è un QDialog perché deve integrarsi nella finestra esistente.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("""
            HealthSummaryDialog {
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 12px;
            }
            QLabel {
                color: #e2e8f0;
                font-size: 12px;
            }
        """)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        self._label = QLabel("Caricamento...")
        layout.addWidget(self._label)

    def show_summary(self) -> None:
        """Aggiorna e mostra il pannello."""
        cfg = get_platform_config()
        issues = cfg.check_health()

        if not issues:
            self._label.setText(
                f"<b>{cfg.display_name}</b><br>"
                "✅ Nessun problema rilevato."
            )
        else:
            lines = [f"<b>{cfg.display_name}</b> — problemi rilevati:<br>"]
            for i in issues:
                icon = {
                    HealthSeverity.CRITICAL: "🔴",
                    HealthSeverity.WARNING: "🟡",
                    HealthSeverity.INFO: "ℹ️",
                }[i.severity]
                lines.append(f"{icon} {i.message}")
                if i.fix:
                    lines.append(f"&nbsp;&nbsp;&nbsp;&rarr; {i.fix}")
            self._label.setText("<br>".join(lines))

        self.adjustSize()
