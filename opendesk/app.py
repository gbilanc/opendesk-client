"""
Application entry point.

Initialises logging, creates the QApplication, loads the stylesheet,
and launches the main window.  Supports light/dark theme switching.
"""

from __future__ import annotations

import faulthandler
import logging
import os
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


def install_system_deps() -> None:
    """Print platform-specific system dependency installation guide.

    Exits after printing instructions — does not start the GUI.
    """
    import platform as _platform

    system = _platform.system()
    print("=" * 60)
    print("  OpenDesk — System Dependencies")
    print("=" * 60)
    print()

    if system == "Linux":
        print("Required system packages for Linux:")
        print()
        print("  Debian / Ubuntu / Mint:")
        print("    sudo apt-get install -y ffmpeg libx11-6 libxext6")
        print("    sudo apt-get install -y libxrender1 libxtst6")
        print()
        print("  For Wayland (optional, better screen capture):")
        print("    sudo apt-get install -y pipewire gstreamer1.0-pipewire")
        print("    sudo apt-get install -y python3-gi xdg-desktop-portal")
        print()
        print("  Fedora / RHEL:")
        print("    sudo dnf install -y ffmpeg libX11 libXext libXrender libXtst")
        print()
        print("  Arch Linux:")
        print("    sudo pacman -S --noconfirm ffmpeg libx11 libxext")
        print("    sudo pacman -S --noconfirm libxrender libxtst")
    elif system == "Darwin":
        print("Required system packages for macOS:")
        print()
        print("  ffmpeg (via Homebrew):")
        print("    brew install ffmpeg")
        print()
        print("  Everything else is bundled with the pip package.")
    elif system == "Windows":
        print("Required system packages for Windows:")
        print()
        print("  ffmpeg:")
        print("    Download from https://ffmpeg.org/download.html")
        print("    or install via winget: winget install ffmpeg")
        print()
        print("  Everything else is bundled with the pip package.")
        print("  Visual C++ Redistributable may be required:")
        print("    https://aka.ms/vcredist")

    print()
    print("After installing system packages, run:")
    print("  pip install --upgrade opendesk")
    print()
    sys.exit(0)


def _ensure_desktop_entry() -> None:
    """Create desktop / start-menu entry if missing (silent)."""
    import platform as _platform
    system = _platform.system()

    if system == "Linux":
        data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        apps_dir = data_home / "applications"
        icons_dir = data_home / "icons" / "hicolor" / "256x256" / "apps"
        desktop_file = apps_dir / "opendesk.desktop"

        if desktop_file.exists():
            return  # already installed

        apps_dir.mkdir(parents=True, exist_ok=True)
        icons_dir.mkdir(parents=True, exist_ok=True)

        # Find the icon from the installed package
        icon_src = (
            Path(__file__).parent / "ui" / "resources" / "opendesk.svg"
        )
        if icon_src.exists():
            import shutil
            shutil.copy2(icon_src, icons_dir / "opendesk.svg")

        # Find the executable path (pip-installed script)
        import shutil as _shutil
        exe_path = _shutil.which("opendesk") or _shutil.which("opendesk-host") or sys.executable

        desktop_content = f"""[Desktop Entry]
Type=Application
Name=OpenDesk
Comment=Remote Desktop Application
Icon=opendesk
Exec={exe_path}
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=opendesk
"""
        desktop_file.write_text(desktop_content)
        desktop_file.chmod(0o755)
        logger.debug("Created desktop entry: %s", desktop_file)


def _parse_cli_args() -> tuple[list[str], str | None, str | None]:
    """Parse CLI arguments consumed by the launcher.

    Returns (remaining argv, connect_session_id, connect_password).
    """
    remaining: list[str] = []
    connect_session_id: str | None = None
    connect_password: str | None = None
    i = 0
    while i < len(sys.argv):
        arg = sys.argv[i]
        if arg == "--install-system-deps":
            install_system_deps()
        elif arg == "--connect" and i + 2 < len(sys.argv):
            connect_session_id = sys.argv[i + 1]
            connect_password = sys.argv[i + 2]
            i += 3
            continue
        elif arg == "--log-level" and i + 1 < len(sys.argv):
            # handled below in main()
            remaining.append(arg)
            remaining.append(sys.argv[i + 1])
            i += 2
            continue
        elif arg.startswith("--log-level="):
            remaining.append(arg)
        else:
            remaining.append(arg)
        i += 1
    return remaining, connect_session_id, connect_password


def main(log_level: int | None = None) -> None:
    """Start the OpenDesk application.

    Parameters
    ----------
    log_level : int | None
        Optional logging level override (e.g. ``logging.WARNING``).
        If not provided, falls back to ``OPENDESK_LOG_LEVEL`` env var,
        then to ``logging.DEBUG``.
    """
    # Parse launcher-level CLI args before Qt processes argv
    sys.argv[:], connect_sid, connect_pwd = _parse_cli_args()

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

    # Ensure desktop entry on first run (silent if already present)
    _ensure_desktop_entry()

    app = QApplication(sys.argv)
    app.setApplicationName("OpenDesk")
    app.setOrganizationName("OpenDesk")
    app.setApplicationVersion(version)
    app.setWindowIcon(QIcon(str(_QSS_LIGHT.parent / "opendesk.svg")))

    app.setStyle("Fusion")
    load_stylesheet(app, "light")

    window = MainWindow()
    window.show()

    # Auto-connect if --connect was passed on the command line
    if connect_sid and connect_pwd:
        logger.info("CLI auto-connect: session=%s", connect_sid)
        window.connect_to(connect_sid, connect_pwd)

    sys.exit(app.exec())
