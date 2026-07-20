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
    QListWidget,
    QListWidgetItem,
    QCheckBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from opendesk.core.video_codec import QualityLevel
from opendesk.core.device_registry import DeviceRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORG = "OpenDesk"
_APP = "OpenDesk"


class SettingsDialog(QDialog):
    """Application settings dialog with tabs."""

    def __init__(
        self,
        device_registry: DeviceRegistry | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self.setMinimumHeight(560)

        self._settings = QSettings(_ORG, _APP)
        self._registry = device_registry

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
        self._quality_combo.setCurrentIndex(3)  # SHARP default
        video_layout.addRow("Quality:", self._quality_combo)

        self._resolution_scale = QComboBox()
        self._resolution_scale.addItem("Full (1:1)", 1.0)
        self._resolution_scale.addItem("75%", 0.75)
        self._resolution_scale.addItem("50%", 0.5)
        self._resolution_scale.addItem("25%", 0.25)
        self._resolution_scale.setCurrentIndex(0)
        video_layout.addRow("Resolution:", self._resolution_scale)

        self._fps_spin = QSpinBox()
        self._fps_spin.setRange(1, 60)
        self._fps_spin.setValue(30)
        self._fps_spin.setSuffix(" fps")
        video_layout.addRow("Max FPS:", self._fps_spin)

        self._adaptive_fps = QCheckBox("Reduce FPS when screen is idle")
        self._adaptive_fps.setChecked(True)
        video_layout.addRow("", self._adaptive_fps)

        # ── Pixel format ──
        self._pixel_format = QComboBox()
        self._pixel_format.addItem("Full color (yuv444p — sharp text, recommended)", "yuv444p")
        self._pixel_format.addItem("Standard (yuv420p — less bandwidth)", "yuv420p")
        self._pixel_format.setCurrentIndex(0)
        video_layout.addRow("Pixel format:", self._pixel_format)

        # ── Sharp text in viewer ──
        self._sharp_text_viewer = QCheckBox("Sharp text mode in viewer (nearest-neighbour)")
        self._sharp_text_viewer.setChecked(True)
        video_layout.addRow("", self._sharp_text_viewer)

        # ── Codec / encoder ──
        codec_group = QGroupBox("Encoder")
        codec_form = QFormLayout(codec_group)
        codec_form.setSpacing(8)

        self._codec_combo = QComboBox()
        self._codec_combo.addItem("Auto (best available)", "")
        self._codec_combo.addItem("H.264 (SW)", "h264")
        self._codec_combo.addItem("H.265/HEVC (SW)", "hevc")
        # HW encoders (if available)
        from opendesk.core.video_codec import VideoEncoder
        for hw in VideoEncoder.available_hw_encoders():
            label = hw.replace("_", " ").upper()
            self._codec_combo.addItem(f"{label}", hw)
        self._codec_combo.setCurrentIndex(0)
        codec_form.addRow("Codec:", self._codec_combo)

        self._hw_check = QCheckBox("Prefer hardware acceleration")
        self._hw_check.setChecked(True)
        codec_form.addRow("", self._hw_check)

        # ── Preset ──
        self._encoder_preset = QComboBox()
        self._encoder_preset.addItem("Auto (by quality)", "")
        self._encoder_preset.addItem("ultrafast (low CPU)", "ultrafast")
        self._encoder_preset.addItem("veryfast", "veryfast")
        self._encoder_preset.addItem("faster", "faster")
        self._encoder_preset.addItem("fast (balanced)", "fast")
        self._encoder_preset.addItem("medium (better quality)", "medium")
        self._encoder_preset.addItem("slow (best quality)", "slow")
        self._encoder_preset.setCurrentIndex(0)
        codec_form.addRow("Preset:", self._encoder_preset)

        # Etichetta informativa sui codec HW disponibili
        hw_list = VideoEncoder.available_hw_encoders()
        if hw_list:
            hw_label = QLabel(
                f"<small>HW encoders detected: {', '.join(hw_list)}</small>"
            )
            hw_label.setStyleSheet("color: #22c55e;")
        else:
            hw_label = QLabel(
                "<small>No HW encoder detected — using software encoding</small>"
            )
            hw_label.setStyleSheet("color: #f59e0b;")
        codec_form.addRow("", hw_label)

        video_layout.addRow(codec_group)

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
        sec_layout = QVBoxLayout(sec_tab)
        sec_layout.setSpacing(12)

        # ── Auth group ──
        auth_group = QGroupBox("Accesso")
        auth_form = QFormLayout(auth_group)
        auth_form.setSpacing(8)

        self._require_auth = QCheckBox("Require password for incoming connections")
        self._require_auth.setChecked(True)
        auth_form.addRow("", self._require_auth)

        self._e2ee_check = QCheckBox("Enable end-to-end encryption")
        self._e2ee_check.setChecked(True)
        auth_form.addRow("", self._e2ee_check)

        sec_layout.addWidget(auth_group)

        # ── Authorized devices group ──
        auth_dev_group = QGroupBox("Dispositivi pre-autorizzati")
        auth_dev_layout = QVBoxLayout(auth_dev_group)

        desc = QLabel(
            "Devices in this list can connect without entering a password."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("font-size: 12px;")
        auth_dev_layout.addWidget(desc)

        self._trusted_list = QListWidget()
        self._trusted_list.setMinimumHeight(120)
        auth_dev_layout.addWidget(self._trusted_list)

        btn_row = QHBoxLayout()
        self._add_trusted_btn = QPushButton("Add device...")
        self._add_trusted_btn.clicked.connect(self._add_trusted_device)
        btn_row.addWidget(self._add_trusted_btn)
        self._remove_trusted_btn = QPushButton("Remove")
        self._remove_trusted_btn.clicked.connect(self._remove_trusted_device)
        btn_row.addWidget(self._remove_trusted_btn)
        btn_row.addStretch()
        auth_dev_layout.addLayout(btn_row)

        sec_layout.addWidget(auth_dev_group)
        sec_layout.addStretch()

        tabs.addTab(sec_tab, "Security")

        # ── Tab 4: General (Clipboard, Audio, etc.) ──
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.setSpacing(12)

        clipboard_group = QGroupBox("Clipboard")
        clipboard_form = QFormLayout(clipboard_group)
        clipboard_form.setSpacing(8)

        self._enable_clipboard_sync = QCheckBox("Sync clipboard with remote peer")
        self._enable_clipboard_sync.setChecked(False)
        clipboard_form.addRow("", self._enable_clipboard_sync)
        general_layout.addWidget(clipboard_group)

        audio_group = QGroupBox("Audio (Microphone)")
        audio_form = QFormLayout(audio_group)
        audio_form.setSpacing(8)

        self._enable_audio = QCheckBox("Stream microphone to remote peer")
        self._enable_audio.setChecked(False)
        audio_form.addRow("", self._enable_audio)
        general_layout.addWidget(audio_group)

        camera_group = QGroupBox("Camera (Webcam)")
        camera_form = QFormLayout(camera_group)
        camera_form.setSpacing(8)

        self._enable_camera = QCheckBox("Stream webcam to remote peer")
        self._enable_camera.setChecked(False)
        camera_form.addRow("", self._enable_camera)

        self._camera_device_combo = QComboBox()
        self._camera_device_combo.addItem("Default camera", 0)
        self._populate_camera_devices()
        camera_form.addRow("Device:", self._camera_device_combo)

        self._camera_quality_combo = QComboBox()
        self._camera_quality_combo.addItem("Low (320×240, 5 fps)", "low")
        self._camera_quality_combo.addItem("Medium (640×480, 10 fps)", "medium")
        self._camera_quality_combo.addItem("High (640×480, 15 fps)", "high")
        self._camera_quality_combo.setCurrentIndex(1)  # Medium default
        camera_form.addRow("Quality:", self._camera_quality_combo)

        general_layout.addWidget(camera_group)

        general_layout.addStretch()
        tabs.addTab(general_tab, "General")

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
        quality = self._settings.value("video/quality", "SHARP")
        idx = self._quality_combo.findText(quality.title())
        if idx >= 0:
            self._quality_combo.setCurrentIndex(idx)

        scale = float(self._settings.value("video/resolution_scale", 1.0))
        scale_idx = self._resolution_scale.findData(scale)
        if scale_idx >= 0:
            self._resolution_scale.setCurrentIndex(scale_idx)

        self._fps_spin.setValue(int(self._settings.value("video/max_fps", 30)))
        self._adaptive_fps.setChecked(
            self._settings.value("video/adaptive_fps", True, type=bool)
        )

        codec = self._settings.value("video/codec", "")
        codec_idx = self._codec_combo.findData(codec)
        if codec_idx >= 0:
            self._codec_combo.setCurrentIndex(codec_idx)
        self._hw_check.setChecked(
            self._settings.value("video/hw_encoding", True, type=bool)
        )

        pixel_fmt = self._settings.value("video/pixel_format", "yuv444p")
        pf_idx = self._pixel_format.findData(pixel_fmt)
        if pf_idx >= 0:
            self._pixel_format.setCurrentIndex(pf_idx)

        self._sharp_text_viewer.setChecked(
            self._settings.value("video/sharp_text_viewer", True, type=bool)
        )

        encoder_preset = self._settings.value("video/encoder_preset", "")
        ep_idx = self._encoder_preset.findData(encoder_preset)
        if ep_idx >= 0:
            self._encoder_preset.setCurrentIndex(ep_idx)

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

        self._enable_clipboard_sync.setChecked(
            self._settings.value("general/clipboard_sync", False, type=bool)
        )
        self._enable_audio.setChecked(
            self._settings.value("audio/enabled", False, type=bool)
        )

        self._enable_camera.setChecked(
            self._settings.value("camera/enabled", False, type=bool)
        )
        camera_device = int(self._settings.value("camera/device", 0))
        cam_idx = self._camera_device_combo.findData(camera_device)
        if cam_idx >= 0:
            self._camera_device_combo.setCurrentIndex(cam_idx)
        camera_quality = self._settings.value("camera/quality", "medium")
        cq_idx = self._camera_quality_combo.findData(camera_quality)
        if cq_idx >= 0:
            self._camera_quality_combo.setCurrentIndex(cq_idx)

        self._populate_trusted_devices()

    def _populate_camera_devices(self) -> None:
        """Populate the camera device combo with detected cameras."""
        try:
            from opendesk.core.camera_manager import list_cameras
            cameras = list_cameras()
            # Keep default entry at index 0
            for i, cam in enumerate(cameras):
                label = f"{cam['name']} (dev {cam['index']})"
                existing = self._camera_device_combo.findData(cam['index'])
                if existing < 0:
                    self._camera_device_combo.addItem(label, cam['index'])
        except Exception as e:
            logger.debug("Could not enumerate cameras: %s", e)

    def _populate_trusted_devices(self) -> None:
        """Populate the list of pre-authorized devices."""
        self._trusted_list.clear()
        if self._registry is None:
            self._trusted_list.addItem("(nessun registro dispositivi)")
            return

        trusted = self._registry.trusted()
        if not trusted:
            self._trusted_list.addItem("No pre-authorized devices")
            return

        for dev in trusted:
            text = f"{dev.device_name}  ({dev.device_id[:8]}…)"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, dev.device_id)
            self._trusted_list.addItem(item)

    @Slot()
    def _add_trusted_device(self) -> None:
        """Aggiunge manualmente un dispositivo alla lista trusted."""
        from PySide6.QtWidgets import QInputDialog, QLineEdit, QMessageBox

        if not self._registry:
            return

        raw, ok = QInputDialog.getText(
            self, "Aggiungi dispositivo pre-autorizzato",
            "Incolla l'UUID del dispositivo remoto.\n\n"
            "Il proprietario del dispositivo remoto deve:\n"
            "  1. Aprire OpenDesk → cliccare 'Copy' accanto a 'Device ID'\n"
            "     nella barra in alto\n"
            "  2. Condividere con te l'UUID copiato\n"
            "     (es. 550e8400-e29b-41d4-a716-446655440000)\n\n"
            "Oppure cerca per nome dispositivo o ID abbreviato (prime 8 cifre):",
            QLineEdit.EchoMode.Normal,
        )
        if not ok or not raw.strip():
            return
        raw = raw.strip()

        # Cerca nel registry (UUID esatto, prefisso, nome)
        matches = self._registry.find(raw)
        if matches:
            for dev in matches:
                if not dev.trusted:
                    self._registry.set_trusted(dev.device_id, True)
            self._populate_trusted_devices()
            n = len(matches)
            self._flash_status(f"✅ {n} dispositivo{'i' if n > 1 else ''} pre-autorizzato{'i' if n > 1 else ''}")
            return

        # Non trovato — chiedi conferma prima di creare un nuovo entry
        reply = QMessageBox.question(
            self, "Dispositivo sconosciuto",
            f"Nessun dispositivo '{raw}' trovato nel registry.\n"
            f"Vuoi creare un nuovo entry e pre-autorizzarlo comunque?\n\n"
            f"Assicurati di aver incollato l'UUID corretto (non l'ID sessione).",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._registry.upsert(
                raw,
                device_name=raw[:8],
                trusted=True,
                online=False,
            )
            self._populate_trusted_devices()

    # ── helpers ─────────────────────────────────────────────────────

    def _flash_status(self, msg: str) -> None:
        """Mostra un messaggio temporaneo nella parent dialog (se possibile)."""
        parent = self.parentWidget()
        if parent and hasattr(parent, 'statusBar'):
            sb = parent.statusBar()
            sb.showMessage(msg, 3000)

    @Slot()
    def _remove_trusted_device(self) -> None:
        """Remove the selected device from the trusted list."""
        item = self._trusted_list.currentItem()
        if item is None:
            return
        device_id = item.data(Qt.ItemDataRole.UserRole)
        if device_id and self._registry:
            self._registry.set_trusted(device_id, False)
            self._populate_trusted_devices()

    def _save_settings(self) -> None:
        """Save current UI values to QSettings."""
        self._settings.setValue(
            "video/quality",
            self._quality_combo.currentText().upper(),
        )
        self._settings.setValue(
            "video/resolution_scale",
            self._resolution_scale.currentData(),
        )
        self._settings.setValue("video/max_fps", self._fps_spin.value())
        self._settings.setValue("video/adaptive_fps", self._adaptive_fps.isChecked())
        self._settings.setValue(
            "video/codec",
            self._codec_combo.currentData(),
        )
        self._settings.setValue(
            "video/hw_encoding",
            self._hw_check.isChecked(),
        )
        self._settings.setValue(
            "video/pixel_format",
            self._pixel_format.currentData(),
        )
        self._settings.setValue(
            "video/sharp_text_viewer",
            self._sharp_text_viewer.isChecked(),
        )
        self._settings.setValue(
            "video/encoder_preset",
            self._encoder_preset.currentData(),
        )

        self._settings.setValue("network/stun_server", self._stun_server.text())
        self._settings.setValue("network/relay_host", self._relay_host.text())
        self._settings.setValue("network/relay_port", self._relay_port.value())
        self._settings.setValue("network/enable_relay", self._enable_relay.isChecked())

        self._settings.setValue("security/require_auth", self._require_auth.isChecked())
        self._settings.setValue("security/e2ee", self._e2ee_check.isChecked())

        self._settings.setValue("general/clipboard_sync", self._enable_clipboard_sync.isChecked())
        self._settings.setValue("audio/enabled", self._enable_audio.isChecked())
        self._settings.setValue("camera/enabled", self._enable_camera.isChecked())
        self._settings.setValue(
            "camera/device",
            self._camera_device_combo.currentData(),
        )
        self._settings.setValue(
            "camera/quality",
            self._camera_quality_combo.currentData(),
        )

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
