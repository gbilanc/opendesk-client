#!/usr/bin/env python3
"""
cross_platform_installer.py

Installs OpenDesk Host (opendesk-host) cross-platform for Windows, Linux, and macOS.

Usage:
    python cross_platform_installer.py [--target-dir /path/to/install]

Options:
    --target-dir DIR       Directory where to install the application.
                          Defaults to the current directory.
    --skip-deps            Skip system dependency installation.
    --no-log               Suppress logging output.

Environment variables:
    OPENDESK_INSTALL_DIR    Directory for the opendesk package.
    OPENDESK_HOST_PORT      Relay port (default: 8474).
"""

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path


def detect_platform() -> platform.system():
    """Return the current OS name: 'windows', 'linux', or 'darwin'."""
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "darwin"
    return system


def run_cmd(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    print(f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, text=True)
    if check and result.returncode != 0:
        print(f"    ERROR: {result.stderr}")
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def install_system_deps_windows() -> None:
    """Install system dependencies on Windows."""
    print("Installing Windows system dependencies...")
    run_cmd(["choco", "install", "-y", "ffmpeg", "libx11-6", "libxext6", "libxrender1", "libxtst6"])


def install_system_deps_linux() -> None:
    """Install system dependencies on Linux."""
    print("Installing Linux system dependencies...")

    system = os.environ.get("OS", platform.system())
    if system == "Linux":
        if os.path.exists("/etc/apt/dpkg/lock-frontend"):
            print("  Waiting for apt to release.")
            run_cmd(["apt-get", "update"])
        else:
            print("  Installing via apt-get...")
            run_cmd(["sudo", "apt-get", "install", "-y", "ffmpeg", "libx11-6", "libxext6", "libxrender1", "libxtst6"])
    elif system == "Fedora":
        print("  Installing via dnf...")
        run_cmd(["sudo", "dnf", "install", "-y", "ffmpeg", "libX11", "libXext", "libXrender", "libXtst"])


def install_system_deps_darwin() -> None:
    """Install system dependencies on macOS."""
    print("Installing macOS system dependencies...")
    run_cmd(["brew", "install", "ffmpeg"])


def install_opendesk() -> None:
    """Install the opendesk package using uv (falls back to pip).

    On PEP 668 systems (Debian/Ubuntu 23.04+, Mint 21.3+) the system pip
    refuses to install packages (externally-managed-environment), so we
    prefer ``uv`` when available.
    """
    print("Installing opendesk package via uv/pip...")
    uv = _shutil_which("uv")
    if uv:
        run_cmd([uv, "pip", "install", "opendesk"])
    else:
        run_cmd([sys.executable, "-m", "pip", "install", "opendesk"])


def _shutil_which(name: str) -> str | None:
    """Resolve an executable in PATH without importing shutil at module level."""
    import shutil

    return shutil.which(name)


def setup_host_app() -> None:
    """Create the desktop entry file for the host application."""
    print("Setting up desktop entry...")

    system = detect_platform()
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
    """Crea l'entry XDG autostart (Linux): avvia l'host a icona al login."""
    if detect_platform() != "linux":
        return

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
        description="Install OpenDesk Host cross-platform for Windows, Linux, and macOS."
    )
    parser.add_argument(
        "--target-dir",
        default=Path(__file__).parent,
        help="Directory where to install the application (default: current directory).",
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

    target_dir = Path(args.target_dir)
    target_dir = target_dir.resolve()

    print(f"OpenDesk Host — Cross-Platform Installer")
    print(f"Target directory: {target_dir}\n")

    system = detect_platform()
    print(f"Detected platform: {system}\n")

    # Determine which system deps to install
    skip_deps = args.skip_deps
    if not skip_deps:
        if system == "Windows":
            install_system_deps_windows()
        elif system == "Linux":
            install_system_deps_linux()
        elif system == "Darwin":
            install_system_deps_darwin()

    # Install the opendesk package
    install_opendesk()

    # Set up desktop entry
    setup_host_app()

    # Avvia automaticamente al login, ridotto a icona
    setup_autostart()

    print("\nInstallation complete!")
    print(f"Run: {target_dir}/opendesk-host --log-level=WARNING")
    print(f"Config: Set OPENDESK_LOG_LEVEL=WARNING if needed.\n")


if __name__ == "__main__":
    main()
