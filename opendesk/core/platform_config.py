"""
Platform configuration — centralised detection and defaults for all
supported operating systems and display servers.

Provides a single ``PlatformConfig`` dataclass that every component
(screen capture, input injection, codec, settings) uses instead of
scattered ``sys.platform`` / ``is_wayland()`` checks.

Each platform has its own capture method, input backend, codec
preferences, required system packages, and pip extras group.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from opendesk.utils.platform import current_platform, Platform, is_wayland

if TYPE_CHECKING:
    from opendesk.core.input_injection import InputBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Health issue dataclass
# ---------------------------------------------------------------------------


class HealthSeverity(Enum):
    CRITICAL = "critical"   # Feature non funzionante
    WARNING = "warning"     # Feature degradata / dep mancante
    INFO = "info"           # Informazione / suggerimento


@dataclass
class HealthIssue:
    """A detected problem or warning about the current platform setup."""

    severity: HealthSeverity
    component: str  # "capture", "input", "codec", "audio", "camera", "deps"
    message: str
    fix: str = ""  # Suggested fix (optional)

    def __str__(self) -> str:
        icon = {
            HealthSeverity.CRITICAL: "🔴",
            HealthSeverity.WARNING: "🟡",
            HealthSeverity.INFO: "ℹ️",
        }[self.severity]
        s = f"{icon} [{self.component}] {self.message}"
        if self.fix:
            s += f" — {self.fix}"
        return s


# ---------------------------------------------------------------------------
# Capture method enum
# ---------------------------------------------------------------------------


class CaptureMethod(Enum):
    """Preferred backend for screen capture."""

    AUTO = auto()
    MSS = auto()  # Cross-platform (DXGI / CoreGraphics / X11)
    PIPEWIRE = auto()  # Linux Wayland via GStreamer pipewiresrc
    PORTAL = auto()  # Linux Wayland via D-Bus portal + GStreamer
    DUMMY = auto()  # Test pattern for development


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _find_system_python_gi() -> str | None:
    """Find a system Python interpreter with GObject Introspection bindings.

    Needed by the PipeWire / Portal capture helper subprocess.
    """
    import shutil

    candidates = ["/usr/bin/python3", "/usr/bin/python"]
    for py in candidates:
        if not shutil.which(py):
            continue
        try:
            r = subprocess.run(
                [py, "-c",
                 "import gi; gi.require_version('Gst', '1.0');"
                 "gi.require_version('GstApp', '1.0');"
                 "from gi.repository import Gst; Gst.init(None);"
                 "print('ok')"],
                capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0:
                return py
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            continue
    return None


def _check_pipewire_element() -> bool:
    """Check if GStreamer pipewiresrc element is available."""
    system_python = _find_system_python_gi()
    if not system_python:
        return False
    try:
        r = subprocess.run(
            [system_python, "-c",
             "import gi; gi.require_version('Gst', '1.0');"
             "gi.require_version('GstApp', '1.0');"
             "from gi.repository import Gst; Gst.init(None);"
             "e = Gst.ElementFactory.make('pipewiresrc', None);"
             "exit(0 if e else 1)"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def _check_portal_available() -> bool:
    """Check if xdg-desktop-portal is running on the session bus."""
    try:
        import dbus_next  # noqa: F401
    except ImportError:
        return False
    try:
        r = subprocess.run(
            ["busctl", "--user", "list", "--no-pager"],
            capture_output=True, text=True, timeout=2,
        )
        if "org.freedesktop.portal.Desktop" in r.stdout:
            return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return False


def _check_x11_display() -> bool:
    """Check if an X11 display is available."""
    return "DISPLAY" in os.environ and bool(os.environ.get("DISPLAY"))


# ---------------------------------------------------------------------------
# PlatformConfig
# ---------------------------------------------------------------------------


@dataclass
class PlatformConfig:
    """Detected platform configuration with component-level defaults.

    This is the single source of truth for platform-specific behaviour.
    Create once at startup via ``PlatformConfig.detect()`` and pass
    to components that need it.
    """

    # ── Detection ──────────────────────────────────────────────────
    platform: Platform = Platform.UNKNOWN
    is_wayland: bool = False
    display_name: str = "Unknown"

    # ── Screen capture ─────────────────────────────────────────────
    capture_method: CaptureMethod = CaptureMethod.MSS
    capture_methods_available: list[CaptureMethod] = field(default_factory=list)
    has_portal: bool = False      # xdg-desktop-portal + dbus-next + gi
    has_pipewire: bool = False    # GStreamer pipewiresrc + gi
    has_x11: bool = False         # X11 display available (for MSS fallback)

    # ── Input injection ────────────────────────────────────────────
    input_backend_name: str = ""
    _input_backend_cls: type | None = None  # InputBackend subclass (lazy)

    # ── Codec / encoding ───────────────────────────────────────────
    codec_hint: str = "h264"      # preferred codec name
    default_pixel_format: str = "yuv444p"
    supports_hw_encoding: bool = False
    hw_encoders_available: list[str] = field(default_factory=list)

    # ── Dependencies ───────────────────────────────────────────────
    pip_extra: str = ""            # "wayland", "x11", "macos", or ""
    required_system_packages: list[str] = field(default_factory=list)

    # ── Features ───────────────────────────────────────────────────
    supports_clipboard_sync: bool = True
    supports_audio: bool = True
    supports_camera: bool = True
    supports_file_transfer: bool = True

    # ── Factory ────────────────────────────────────────────────────

    @classmethod
    def detect(cls) -> PlatformConfig:
        """Auto-detect the current platform and build a config.

        This is the main entry point — call once at startup.
        """
        plat = current_platform()
        wayland = is_wayland()

        cfg = cls(platform=plat, is_wayland=wayland)

        if plat == Platform.LINUX and wayland:
            cfg._detect_linux_wayland()
        elif plat == Platform.LINUX:
            cfg._detect_linux_x11()
        elif plat == Platform.WINDOWS:
            cfg._detect_windows()
        elif plat == Platform.MACOS:
            cfg._detect_macos()
        else:
            cfg.display_name = "Unknown"
            cfg.capture_method = CaptureMethod.MSS

        cfg._detect_codec_capabilities()
        cfg._resolve_input_backend()
        return cfg

    # ── Per-platform detection ─────────────────────────────────────

    def _detect_linux_wayland(self) -> None:
        """Configure for Linux + Wayland."""
        self.display_name = "Linux (Wayland)"
        self.pip_extra = "wayland"
        self.has_x11 = _check_x11_display()
        self.has_pipewire = _check_pipewire_element()
        self.has_portal = _check_portal_available() and self.has_pipewire

        self.required_system_packages = [
            "gstreamer1.0-pipewire",
            "python3-gi",
            "xdg-desktop-portal",
            "xdg-desktop-portal-gtk (or -kde, -wlr)",
            "ydotool (optional — absolute mouse)",
        ]

        # Capture: prefer PORTAL → PIPEWIRE → MSS (if XWayland)
        if self.has_portal:
            self.capture_method = CaptureMethod.PORTAL
            self.capture_methods_available = [
                CaptureMethod.PORTAL,
                CaptureMethod.PIPEWIRE,
            ]
            if self.has_x11:
                self.capture_methods_available.append(CaptureMethod.MSS)
        elif self.has_pipewire:
            self.capture_method = CaptureMethod.PIPEWIRE
            self.capture_methods_available = [CaptureMethod.PIPEWIRE]
            if self.has_x11:
                self.capture_methods_available.append(CaptureMethod.MSS)
        elif self.has_x11:
            self.capture_method = CaptureMethod.MSS
            self.capture_methods_available = [CaptureMethod.MSS]
        else:
            self.capture_method = CaptureMethod.DUMMY
            self.capture_methods_available = []

        # Wayland input requires evdev + optionally ydotool
        self.input_backend_name = "WaylandInputBackend (evdev + ydotool)"
        self.required_system_packages.append("evdev (Python package via pip)")

    def _detect_linux_x11(self) -> None:
        """Configure for Linux + X11."""
        self.display_name = "Linux (X11)"
        self.pip_extra = "x11"
        self.has_x11 = True

        self.required_system_packages = [
            "python-xlib (via pip extra)",
            "xtst (x11-utils / libxtst-dev)",
        ]

        # Capture: MSS via X11 (always works on X11)
        self.capture_method = CaptureMethod.MSS
        self.capture_methods_available = [CaptureMethod.MSS]

        # If pipewire is also available (unlikely on pure X11, but possible),
        # offer it as an alternative
        if _check_pipewire_element():
            self.capture_methods_available.append(CaptureMethod.PIPEWIRE)

        self.input_backend_name = "X11InputBackend (python-xlib + XTest)"

    def _detect_windows(self) -> None:
        """Configure for Windows."""
        self.display_name = "Windows"
        self.pip_extra = ""

        self.required_system_packages = [
            "Windows SDK (for C++ build tools, optional)",
        ]

        # Capture: MSS via DXGI
        self.capture_method = CaptureMethod.MSS
        self.capture_methods_available = [CaptureMethod.MSS]

        self.input_backend_name = "WindowsInputBackend (SendInput)"

    def _detect_macos(self) -> None:
        """Configure for macOS."""
        self.display_name = "macOS"
        self.pip_extra = "macos"

        self.required_system_packages = [
            "pyobjc-framework-Quartz (via pip extra)",
        ]

        # Capture: MSS via CoreGraphics
        self.capture_method = CaptureMethod.MSS
        self.capture_methods_available = [CaptureMethod.MSS]

        self.input_backend_name = "MacOSInputBackend (Quartz CGEvent)"

    # ── Codec detection ────────────────────────────────────────────

    def _detect_codec_capabilities(self) -> None:
        """Detect available codecs (SW + HW encoders)."""
        try:
            from opendesk.core.video_codec import (
                VideoEncoder, _try_open_codec, _candidates,
            )

            # HW encoders
            for name in _candidates(prefer_hw=True):
                if name in ("hevc", "h264"):
                    continue
                if _try_open_codec(name):
                    self.hw_encoders_available.append(name)

            self.supports_hw_encoding = len(self.hw_encoders_available) > 0

            # Preferred codec
            if self.supports_hw_encoding:
                self.codec_hint = self.hw_encoders_available[0]
            else:
                self.codec_hint = "h264"

            # Pixel format: yuv444p only available on some platforms/codecs
            if self.platform == Platform.WINDOWS:
                # Many Windows HW encoders don't support yuv444p
                self.default_pixel_format = "yuv420p"

        except ImportError:
            self.codec_hint = "h264"
            self.default_pixel_format = "yuv420p"

    # ── Input backend ──────────────────────────────────────────────

    def _resolve_input_backend(self) -> None:
        """Resolve the InputBackend class for this platform."""
        if self.platform == Platform.LINUX and self.is_wayland:
            try:
                from opendesk.core.input_injection import (  # noqa: F401
                    WaylandInputBackend,
                )
                self._input_backend_cls = WaylandInputBackend  # type: ignore[attr-defined]
            except ImportError:
                self._input_backend_cls = None
                self.input_backend_name = "❌ WaylandInputBackend (evdev missing)"

        elif self.platform == Platform.LINUX and not self.is_wayland:
            try:
                from opendesk.core.input_injection import X11InputBackend
                self._input_backend_cls = X11InputBackend
            except ImportError:
                self._input_backend_cls = None
                self.input_backend_name = "❌ X11InputBackend (python-xlib missing)"

        elif self.platform == Platform.WINDOWS:
            from opendesk.core.input_injection import WindowsInputBackend
            self._input_backend_cls = WindowsInputBackend

        elif self.platform == Platform.MACOS:
            try:
                from opendesk.core.input_injection import MacOSInputBackend
                self._input_backend_cls = MacOSInputBackend
            except ImportError:
                self._input_backend_cls = None
                self.input_backend_name = "❌ MacOSInputBackend (pyobjc missing)"

    # ── Public helpers ─────────────────────────────────────────────

    def create_input_backend(self):
        """Create an InputBackend instance for this platform.

        Returns None if the required dependencies are missing.
        """
        if self._input_backend_cls is None:
            return None
        return self._input_backend_cls()

    def summary_lines(self) -> list[str]:
        """Return a list of human-readable config lines for logging / UI."""
        lines = [
            f"Platform:       {self.display_name}",
            f"Capture:        {self.capture_method.name}",
            f"Available:      {', '.join(m.name for m in self.capture_methods_available) or 'none'}",
            f"Input backend:  {self.input_backend_name}",
            f"Codec:          {self.codec_hint}",
            f"HW encoders:    {', '.join(self.hw_encoders_available) or 'none'}",
            f"Pixel format:   {self.default_pixel_format}",
        ]
        if self.required_system_packages:
            lines.append(f"Required pkgs:  {', '.join(self.required_system_packages[:4])}")
        if self.pip_extra:
            lines.append(f"Pip extra:      {self.pip_extra}")
        return lines

    def log_summary(self) -> None:
        """Log the platform configuration at INFO level."""
        for line in self.summary_lines():
            logger.info("  %s", line)

    # ── Health checks ──────────────────────────────────────────────

    def check_health(self) -> list[HealthIssue]:
        """Run diagnostic checks and return all issues found.

        Checks cover capture backends, input injection, codec
        availability, and required dependencies for the current
        platform.
        """
        issues: list[HealthIssue] = []

        if self.platform == Platform.UNKNOWN:
            issues.append(HealthIssue(
                HealthSeverity.WARNING, "platform",
                "Sistema operativo non riconosciuto.",
            ))
            return issues

        self._check_capture_health(issues)
        self._check_input_health(issues)
        self._check_codec_health(issues)
        self._check_deps_health(issues)
        self._check_extra_features(issues)

        return issues

    def _check_capture_health(self, issues: list[HealthIssue]) -> None:
        """Check screen capture backend availability."""
        if self.platform == Platform.LINUX and self.is_wayland:
            if not self.capture_methods_available:
                issues.append(HealthIssue(
                    HealthSeverity.CRITICAL, "capture",
                    "Nessun backend di cattura disponibile su Wayland.",
                    "Installa xdg-desktop-portal, gstreamer1.0-pipewire, python3-gi, e XWayland.",
                ))
            elif self.capture_method == CaptureMethod.DUMMY:
                issues.append(HealthIssue(
                    HealthSeverity.CRITICAL, "capture",
                    "Nessun backend di cattura funzionante — usato backend DUMMY (nessun frame reale).",
                    "Verifica che XWayland o PipeWire + portal siano installati.",
                ))
            elif self.capture_method == CaptureMethod.MSS and not self.has_x11:
                issues.append(HealthIssue(
                    HealthSeverity.CRITICAL, "capture",
                    "MSS (X11) selezionato ma XWayland non disponibile.",
                    "Avvia l'app con XWayland o installa xdg-desktop-portal + PipeWire.",
                ))
            if not self.has_portal and not self.has_pipewire:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "capture",
                    "PipeWire non disponibile — usato fallback XWayland (MSS)." if self.has_x11
                    else "Né PipeWire né XWayland disponibili — la cattura schermo non funzionerà.",
                    "Installa gstreamer1.0-pipewire e python3-gi per cattura nativa Wayland.",
                ))
            elif not self.has_portal and self.has_pipewire:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "capture",
                    "Portal D-Bus non disponibile — PipeWire mostrerà il proprio dialog.",
                    "Installa dbus-next (pip) e xdg-desktop-portal + backend.",
                ))
            if not self.has_portal:
                if not shutil.which("busctl"):
                    pass  # already covered
                try:
                    import dbus_next  # noqa: F401
                except ImportError:
                    issues.append(HealthIssue(
                        HealthSeverity.WARNING, "capture",
                        "dbus-next non installato — il portal D-Bus non può essere usato.",
                        "Esegui: uv sync --extra wayland  (o pip install dbus-next)",
                    ))

        elif self.platform == Platform.LINUX and not self.is_wayland:
            if not _check_x11_display():
                issues.append(HealthIssue(
                    HealthSeverity.CRITICAL, "capture",
                    "Nessun display X11 trovato (variabile DISPLAY non impostata).",
                    "Avvia l'applicazione in una sessione X11.",
                ))

    def _check_input_health(self, issues: list[HealthIssue]) -> None:
        """Check input backend availability."""
        if self._input_backend_cls is None:
            if self.platform == Platform.LINUX and self.is_wayland:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "input",
                    "Input remoto non disponibile su Wayland — evdev non installato.",
                    "Esegui: uv sync --extra wayland  (o pip install evdev)",
                ))
            elif self.platform == Platform.LINUX and not self.is_wayland:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "input",
                    "Input remoto X11 non disponibile — python-xlib non installato.",
                    "Esegui: uv sync --extra x11  (o pip install python-xlib)",
                ))
            elif self.platform == Platform.MACOS:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "input",
                    "Input remoto macOS non disponibile — pyobjc non installato.",
                    "Esegui: uv sync --extra macos  (o pip install pyobjc-framework-Quartz)",
                ))
            else:
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "input",
                    "Nessun backend di input remoto disponibile per questa piattaforma.",
                ))

    def _check_codec_health(self, issues: list[HealthIssue]) -> None:
        """Check codec / encoding availability."""
        if not self.codec_hint:
            issues.append(HealthIssue(
                HealthSeverity.CRITICAL, "codec",
                "Nessun codec video disponibile — lo streaming non funzionerà.",
                "Verifica che PyAV sia installato correttamente e che libx264 sia presente.",
            ))

        # Check HW encoding availability per-platform
        if self.platform == Platform.MACOS and not self.hw_encoders_available:
            issues.append(HealthIssue(
                HealthSeverity.INFO, "codec",
                "Nessun HW encoder video trovato — usato software H.264.",
                "Su macOS il Videotoolbox dovrebbe essere disponibile. Verifica PyAV.",
            ))
        elif self.platform == Platform.WINDOWS and not self.hw_encoders_available:
            issues.append(HealthIssue(
                HealthSeverity.INFO, "codec",
                "Nessun HW encoder video trovato — usato software H.264.",
                "Installa driver NVIDIA/AMD o verifica che NVENC/AMF sia supportato.",
            ))

    def _check_deps_health(self, issues: list[HealthIssue]) -> None:
        """Check that required pip extras are installed."""
        if self.pip_extra and self.pip_extra not in ("",):
            # Check by trying to import the key package for this extra
            check_map = {
                "wayland": ("dbus_next", "evdev"),
                "x11": ("Xlib",),
                "macos": ("Quartz",),
            }
            pkgs = check_map.get(self.pip_extra, ())
            for pkg in pkgs:
                try:
                    __import__(pkg)
                except ImportError:
                    issues.append(HealthIssue(
                        HealthSeverity.WARNING, "deps",
                        f"Pacchetto '{pkg}' mancante — necessario per il supporto {self.pip_extra}.",
                        f"Esegui: uv sync --extra {self.pip_extra}",
                    ))

        # Check system packages on Linux
        if self.platform == Platform.LINUX:
            if not _find_system_python_gi():
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "deps",
                    "python3-gi (GObject Introspection) non trovato — serve per PipeWire.",
                    "Installa: sudo apt install python3-gi  (o equivalente per la tua distro)",
                ))
            if self.is_wayland and not shutil.which("pipewire"):
                issues.append(HealthIssue(
                    HealthSeverity.WARNING, "deps",
                    "PipeWire non trovato — necessario per cattura schermo su Wayland.",
                    "Installa: sudo apt install pipewire gstreamer1.0-pipewire  (o equivalente)",
                ))

    def _check_extra_features(self, issues: list[HealthIssue]) -> None:
        """Check optional features (audio, camera, clipboard sync)."""
        # Audio: check soundcard availability
        try:
            import soundcard  # noqa: F401
        except ImportError:
            if self.supports_audio:
                issues.append(HealthIssue(
                    HealthSeverity.INFO, "audio",
                    "Microfono non disponibile — pacchetto 'soundcard' non installato.",
                    "Esegui: uv sync --extra audio  (o pip install soundcard)",
                ))
                self.supports_audio = False

        # Camera: check OpenCV (already a dependency, but verify)
        try:
            import cv2  # noqa: F401
        except ImportError:
            if self.supports_camera:
                issues.append(HealthIssue(
                    HealthSeverity.INFO, "camera",
                    "Webcam non disponibile — OpenCV (opencv-python) non installato.",
                    "Installa: uv sync  (opencv-python è una dipendenza base)",
                ))
                self.supports_camera = False

    @property
    def has_critical_issues(self) -> bool:
        """True if at least one CRITICAL issue was found."""
        return any(
            i.severity == HealthSeverity.CRITICAL
            for i in self.check_health()
        )

    def health_summary_lines(self) -> list[str]:
        """Human-readable health report lines."""
        issues = self.check_health()
        if not issues:
            return ["✅ Nessun problema rilevato."]
        result = []
        for i in issues:
            icon = {
                HealthSeverity.CRITICAL: "🔴",
                HealthSeverity.WARNING: "🟡",
                HealthSeverity.INFO: "ℹ️",
            }[i.severity]
            line = f"{icon} [{i.component}] {i.message}"
            if i.fix:
                line += f"\n   → {i.fix}"
            result.append(line)
        return result

    def log_health(self) -> None:
        """Log all health issues."""
        issues = self.check_health()
        if not issues:
            logger.info("✅ Health check: nessun problema rilevato.")
            return
        for issue in issues:
            icon = {HealthSeverity.CRITICAL: "🔴", HealthSeverity.WARNING: "🟡", HealthSeverity.INFO: "ℹ️"}[issue.severity]
            msg = f"{icon} [{issue.component}] {issue.message}"
            if issue.fix:
                msg += f" → {issue.fix}"
            if issue.severity == HealthSeverity.CRITICAL:
                logger.error(msg)
            elif issue.severity == HealthSeverity.WARNING:
                logger.warning(msg)
            else:
                logger.info(msg)


# ---------------------------------------------------------------------------
# Cache global singleton (detect once, reuse everywhere)
# ---------------------------------------------------------------------------

_global_config: PlatformConfig | None = None


def get_platform_config() -> PlatformConfig:
    """Return the cached platform config, detecting if necessary."""
    global _global_config
    if _global_config is None:
        _global_config = PlatformConfig.detect()
        _global_config.log_summary()
        _global_config.log_health()
    return _global_config


def reset_platform_config() -> None:
    """Force re-detection on next call."""
    global _global_config
    _global_config = None
