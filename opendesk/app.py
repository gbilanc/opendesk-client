"""
Application entry point.

Initialises logging, creates the QApplication, loads the stylesheet,
and launches the main window.  Supports light/dark theme switching.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
from pathlib import Path

# Enable crash diagnostics — on segfault, prints a traceback to stderr
faulthandler.enable()

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPalette, QColor
from PySide6.QtWidgets import QApplication

from opendesk.utils.logger import parse_log_level, setup_logging
from opendesk.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

# Paths to QSS theme files
_QSS_LIGHT = Path(__file__).parent / "ui" / "resources" / "opendesk.qss"
_QSS_DARK = Path(__file__).parent / "ui" / "resources" / "dark.qss"

# Global reference to keep theme state
_current_theme: str = "light"


# ── QPalette colour schemes ──────────────────────────────────────

_LIGHT_PALETTE: dict[QPalette.ColorRole, str] = {
    QPalette.ColorRole.Window: "#ffffff",
    QPalette.ColorRole.WindowText: "#0f172a",
    QPalette.ColorRole.Base: "#ffffff",
    QPalette.ColorRole.AlternateBase: "#f8fafc",
    QPalette.ColorRole.ToolTipBase: "#ffffff",
    QPalette.ColorRole.ToolTipText: "#0f172a",
    QPalette.ColorRole.Text: "#0f172a",
    QPalette.ColorRole.Button: "#f1f5f9",
    QPalette.ColorRole.ButtonText: "#0f172a",
    QPalette.ColorRole.BrightText: "#dc2626",
    QPalette.ColorRole.Link: "#2563eb",
    QPalette.ColorRole.Highlight: "#2563eb",
    QPalette.ColorRole.HighlightedText: "#ffffff",
}

_DARK_PALETTE: dict[QPalette.ColorRole, str] = {
    QPalette.ColorRole.Window: "#1e293b",
    QPalette.ColorRole.WindowText: "#e2e8f0",
    QPalette.ColorRole.Base: "#0f172a",
    QPalette.ColorRole.AlternateBase: "#1e293b",
    QPalette.ColorRole.ToolTipBase: "#1e293b",
    QPalette.ColorRole.ToolTipText: "#e2e8f0",
    QPalette.ColorRole.Text: "#e2e8f0",
    QPalette.ColorRole.Button: "#334155",
    QPalette.ColorRole.ButtonText: "#e2e8f0",
    QPalette.ColorRole.BrightText: "#ef4444",
    QPalette.ColorRole.Link: "#60a5fa",
    QPalette.ColorRole.Highlight: "#3b82f6",
    QPalette.ColorRole.HighlightedText: "#ffffff",
}


def _apply_palette(app: QApplication, theme: str) -> None:
    """Apply a QPalette for the given theme."""
    palette = QPalette()
    colors = _DARK_PALETTE if theme == "dark" else _LIGHT_PALETTE
    for role, hex_color in colors.items():
        palette.setColor(role, QColor(hex_color))
    app.setPalette(palette)


def load_stylesheet(app: QApplication, theme: str = "light") -> None:
    """Load a QSS theme file and apply matching QPalette.

    Parameters
    ----------
    app : QApplication
        The application instance.
    theme : str
        ``"light"`` or ``"dark"``.
    """
    global _current_theme
    qss_path = _QSS_DARK if theme == "dark" else _QSS_LIGHT
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text())
    _apply_palette(app, theme)
    _current_theme = theme
    logger.debug("Theme '%s' applied (QSS + QPalette)", theme)


def toggle_theme(app: QApplication) -> str:
    """Switch between light and dark theme.

    Returns the new theme name.
    """
    new_theme = "dark" if _current_theme == "light" else "light"
    load_stylesheet(app, new_theme)
    return new_theme


def get_current_theme() -> str:
    """Return the current theme name."""
    return _current_theme


def main_release() -> None:
    """Start OpenDesk in release mode (WARNING+ messages only)."""
    main(log_level=logging.WARNING)


def main(log_level: int | None = None) -> None:
    """Start the OpenDesk application.

    Parameters
    ----------
    log_level : int | None
        Optional logging level override (e.g. ``logging.WARNING``).
        If not provided, falls back to ``OPENDESK_LOG_LEVEL`` env var,
        then to ``logging.DEBUG``.
    """
    # Parse --log-level from CLI before Qt processes argv
    cli_level: int | None = None
    for i, arg in enumerate(sys.argv[1:], start=1):
        if arg.startswith("--log-level="):
            cli_level = parse_log_level(arg.split("=", 1)[1])
            del sys.argv[i]
            break
        if arg == "--log-level" and i + 1 < len(sys.argv):
            cli_level = parse_log_level(sys.argv[i + 1])
            del sys.argv[i : i + 2]
            break

    effective_level = log_level if log_level is not None else cli_level
    setup_logging(level=effective_level)
    version = __import__("opendesk").__version__
    logger.info("Starting OpenDesk v%s", version)

    # Log platform configuration at startup
    from opendesk.core.platform_config import get_platform_config
    get_platform_config()

    app = QApplication(sys.argv)
    app.setApplicationName("OpenDesk")
    app.setOrganizationName("OpenDesk")
    app.setApplicationVersion(version)
    app.setWindowIcon(QIcon(str(_QSS_LIGHT.parent / "opendesk.svg")))

    app.setStyle("Fusion")
    load_stylesheet(app, "light")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
