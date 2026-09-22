#!/usr/bin/env python3
"""
cross_platform_installer_linux.py

Installs OpenDesk Host on Linux.

Usage:
    python cross_platform_installer_linux.py [--skip-deps] [--no-log]

Options:
    --skip-deps    Skip system dependency installation.
    --no-log       Suppress logging output.

Environment variables:
    OPENDESK_INSTALL_DIR    Directory where to install the application.
"""

import argparse
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True)
    if check and result.returncode != 0:
        print(f"    ERROR: {result.stderr}")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def install_system_deps() -> None:
    """Install system dependencies on Linux."""
    print("Installing Linux system dependencies...")

    # Check for apt-get
    if os.path.exists("/etc/apt/dpkg/lock-frontend"):
        print("  Waiting for apt to release...")
        run_cmd(["apt-get", "update"])
    else:
        print("  Installing via apt-get...")
        run_cmd(["sudo", "apt-get", "install", "-y", "ffmpeg", "libx11-6", "libxext6", "libxrender1", "libxtst6"])


def install_opendesk() -> None:
    """Install the opendesk Python package via pip."""
    print("Installing opendesk package via pip...")
    run_cmd([sys.executable, "-m", "pip", "install", "opendesk"])


def setup_host_app() -> None:
    """Create the desktop entry file for the host application."""
    print("Setting up desktop entry...")

    system = "linux"
    data_home = Path.home() / ".local" / "share"
    apps_dir = data_home / "applications"
    icons_dir = data_home / "icons" / "hicolor" / "256x256" / "apps"

    apps_dir.mkdir(parents=True, exist_ok=True)
    icons_dir.mkdir(parents=True, exist_ok=True)

    icon_src = Path(__file__).parent / "opendesk" / "ui" / "resources" / "opendesk.svg"
    if icon_src.exists():
        (icons_dir / "opendesk.svg").write_text(icon_src.read_text())

    desktop_file = apps_dir / "opendesk-host.desktop"
    desktop_content = """[Desktop Entry]
Type=Application
Name=OpenDesk Host
Comment=Remote Desktop Application (incoming-only)
Icon=opendesk
Exec=opendesk-host --minimized
Terminal=false
Categories=Network;RemoteAccess;
StartupWMClass=opendesk
"""

    with open(desktop_file, "w") as f:
        f.write(desktop_content)
    desktop_file.chmod(0o755)
    print(f"  Created: {desktop_file}")


def setup_autostart() -> None:
    """Crea l'entry XDG autostart: avvia l'host (ridotto a icona) al login."""
    print("Setting up autostart...")

    autostart_dir = Path.home() / ".config" / "autostart"
    autostart_dir.mkdir(parents=True, exist_ok=True)

    autostart_file = autostart_dir / "opendesk-host.desktop"
    autostart_content = """[Desktop Entry]
Type=Application
Name=OpenDesk Host
Comment=Avvia OpenDesk Host ridotto a icona nella system tray
Icon=opendesk
Exec=opendesk-host --minimized
Terminal=false
X-GNOME-Autostart-enabled=true
X-KDE-autostart-after=panel
Categories=Network;RemoteAccess;
"""

    with open(autostart_file, "w") as f:
        f.write(autostart_content)
    autostart_file.chmod(0o755)
    print(f"  Created: {autostart_file}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install OpenDesk Host on Linux."
    )
    parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Skip system dependency installation.",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Suppress logging output.",
    )
    args = parser.parse_args()

    print("OpenDesk Host — Linux Installer\n")

    # Determine which system deps to install
    skip_deps = args.skip_deps

    if not skip_deps:
        install_system_deps()

    # Install the opendesk package
    install_opendesk()

    # Set up desktop entry
    setup_host_app()

    # Avvia automaticamente al login, ridotto a icona
    setup_autostart()

    print("\nInstallation complete!")
    print(f"Run: {Path(__file__).parent}/opendesk-host --log-level=WARNING\n")


if __name__ == "__main__":
    main()
