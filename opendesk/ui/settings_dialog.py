"""
Settings dialog for OpenDesk configuration.

Persists settings via QSettings.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QSettings, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.video_codec import QualityLevel

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG = "OpenDesk"
_APP = "OpenDesk"


class SettingsDialog(QDialog):
    """Application settings dialog with tabs."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        self._settings = QSettings(_ORG, _APP)

        self._setup_ui()
        self._load_settings()

    # ── UI setup ────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(16)
        layout.setContentsMargins(20, 20, 20, 20)

        # ── Tabs ──
        tabs = QTabWidget()

        # ── Tab 1: Video ──
        video_tab = QWidget()
        video_layout = QFormLayout(video_tab)
        video_layout.setSpacing(12)

        self._quality_combo = QComboBox()
        for q in QualityLevel:
            self._quality_combo.addItem(q.name.title(), q.value)
        self._quality_combo.setCurrentIndex(1)  # MEDIUM default
        video_layout.addRow("Quality:", self._quality_combo)

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(30)
        self._fps_spin.setSuffix(" fps")
        video_layout.addRow("Max FPS:", self._fps_spin)

        self._adaptive_fps = QCheckBox("Reduce FPS when screen is idle")
        self._adaptive_fps.setChecked(True)
        video_layout.addRow("", self._adaptive_fps)

        tabs.addTab(video_tab, "Video")

        # ── Tab 2: Network ──
        net_tab = QWidget()
        net_layout = QFormLayout(net_tab)
        net_layout.setSpacing(12)

        self._stun_server = QLineEdit()
        self._stun_server.setPlaceholderText("stun:stun.l.google.com:19302")
        net_layout.addRow("STUN Server:", self._stun_server)

        self._relay_host = QLineEdit()
        self._relay_host.setPlaceholderText("relay.example.com")
        net_layout.addRow("Relay Host:", self._relay_host)

        self._relay_port = QSpinBox()
        self._relay_port.setRange(1, 65535)
        self._relay_port.setValue(8474)
        net_layout.addRow("Relay Port:", self._relay_port)

        self._enable_relay = QCheckBox("Enable relay fallback")
        self._enable_relay.setChecked(True)
        net_layout.addRow("", self._enable_relay)

        tabs.addTab(net_tab, "Network")

        # ── Tab 3: Security ──
        sec_tab = QWidget()
        sec_layout = QFormLayout(sec_tab)
        sec_layout.setSpacing(12)

        self._require_auth = QCheckBox("Require password for incoming connections")
        self._require_auth.setChecked(True)
        sec_layout.addRow("", self._require_auth)

        self._e2ee_check = QCheckBox("Enable end-to-end encryption")
        self._e2ee_check.setChecked(True)
        self._e2ee_check.setEnabled(True)
        sec_layout.addRow("", self._e2ee_check)

        tabs.addTab(sec_tab, "Security")

        # ── Tab 4: Audio ──
        audio_tab = QWidget()
        audio_layout = QFormLayout(audio_tab)
        audio_layout.setSpacing(12)

        self._enable_audio = QCheckBox("Enable remote audio")
        self._enable_audio.setChecked(False)
        audio_layout.addRow("", self._enable_audio)

        tabs.addTab(audio_tab, "Audio")

        layout.addWidget(tabs)

        # ── Buttons ──
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Apply
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.Apply).clicked.connect(self._on_apply)
        layout.addWidget(buttons)

    # ── load / save ─────────────────────────────────────────────────

    def _load_settings(self) -> None:
        """Load current values from QSettings."""
        quality = self._settings.value("video/quality", "MEDIUM")
        idx = self._quality_combo.findText(quality.title())
        if idx >= 0:
            self._quality_combo.setCurrentIndex(idx)

        self._fps_spin.setValue(int(self._settings.value("video/max_fps", 30)))
        self._adaptive_fps.setChecked(
            self._settings.value("video/adaptive_fps", True, type=bool)
        )

        self._stun_server.setText(
            self._settings.value("network/stun_server", "stun:stun.l.google.com:19302")
        )
        self._relay_host.setText(
            self._settings.value("network/relay_host", "")
        )
        self._relay_port.setValue(
            int(self._settings.value("network/relay_port", 8474))
        )
        self._enable_relay.setChecked(
            self._settings.value("network/enable_relay", True, type=bool)
        )

        self._require_auth.setChecked(
            self._settings.value("security/require_auth", True, type=bool)
        )
        self._e2ee_check.setChecked(
            self._settings.value("security/e2ee", True, type=bool)
        )

        self._enable_audio.setChecked(
            self._settings.value("audio/enabled", False, type=bool)
        )

    def _save_settings(self) -> None:
        """Save current UI values to QSettings."""
        self._settings.setValue(
            "video/quality",
            self._quality_combo.currentText().upper(),
        )
        self._settings.setValue("video/max_fps", self._fps_spin.value())
        self._settings.setValue("video/adaptive_fps", self._adaptive_fps.isChecked())

        self._settings.setValue("network/stun_server", self._stun_server.text())
        self._settings.setValue("network/relay_host", self._relay_host.text())
        self._settings.setValue("network/relay_port", self._relay_port.value())
        self._settings.setValue("network/enable_relay", self._enable_relay.isChecked())

        self._settings.setValue("security/require_auth", self._require_auth.isChecked())
        self._settings.setValue("security/e2ee", self._e2ee_check.isChecked())

        self._settings.setValue("audio/enabled", self._enable_audio.isChecked())

        self._settings.sync()
        logger.info("Settings saved")

    # ── slots ───────────────────────────────────────────────────────

    @Slot()
    def _on_accept(self) -> None:
        self._save_settings()
        self.accept()

    @Slot()
    def _on_apply(self) -> None:
        self._save_settings()
