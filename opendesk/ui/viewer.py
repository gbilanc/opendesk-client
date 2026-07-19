"""
Remote screen viewer widget.

Uses ``QGraphicsView`` for smooth zoom, pan, and fullscreen support.
Displays decoded video frames from the remote peer and provides an
HUD overlay with FPS, quality, and latency information.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

import numpy as np
from PIL import Image

from PySide6.QtCore import (
    Qt,
    QRectF,
    QSize,
    QTimer,
    Signal,
    Slot,
    QPoint,
    QPointF,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_ZOOM = 0.1
_MAX_ZOOM = 5.0
_ZOOM_STEP = 1.15
_HUD_UPDATE_MS = 1000  # HUD refresh interval
_FRAME_TIMEOUT_MS = 10000  # 10 s without a frame → show warning

# ── Camera PiP overlay ──
_CAMERA_OVERLAY_WIDTH = 240
_CAMERA_OVERLAY_HEIGHT = 180
_CAMERA_OVERLAY_MARGIN = 12


# ---------------------------------------------------------------------------
# CameraOverlay — draggable PiP widget
# ---------------------------------------------------------------------------


class CameraOverlay(QWidget):
    """Picture-in-picture overlay for the remote webcam feed.

    Can be dragged by the user and has a close button.
    """

    closed = Signal()  # emitted when user clicks the close button

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(_CAMERA_OVERLAY_WIDTH, _CAMERA_OVERLAY_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)

        # Container widget for the rounded background
        self._container = QWidget(self)
        self._container.setGeometry(0, 0, _CAMERA_OVERLAY_WIDTH, _CAMERA_OVERLAY_HEIGHT)
        self._container.setStyleSheet(
            "background-color: #0f172a;"
            "border: 2px solid #1e293b;"
            "border-radius: 8px;"
        )

        # Video label
        self._video_label = QLabel(self._container)
        self._video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_label.setText("📷")
        self._video_label.setStyleSheet("background: transparent; font-size: 28px;")
        self._video_label.setGeometry(2, 2, _CAMERA_OVERLAY_WIDTH - 4, _CAMERA_OVERLAY_HEIGHT - 28)

        # Close button
        self._close_btn = QPushButton("✖", self._container)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setStyleSheet(
            "QPushButton {"
            "  background: #ef4444; color: white; border-radius: 11px;"
            "  font-size: 11px; font-weight: bold;"
            "  border: none;"
            "}"
            "QPushButton:hover { background: #dc2626; }"
        )
        self._close_btn.move(_CAMERA_OVERLAY_WIDTH - 26, 4)
        self._close_btn.clicked.connect(self._on_close)

        # Drag state
        self._dragging = False
        self._drag_offset = QPoint()

    # ── public API ──────────────────────────────────────────────────

    def set_pixmap(self, pixmap: QPixmap) -> None:
        """Set the camera frame pixmap, scaled to fit."""
        scaled = pixmap.scaled(
            _CAMERA_OVERLAY_WIDTH - 8,
            _CAMERA_OVERLAY_HEIGHT - 32,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._video_label.setPixmap(scaled)

    def clear_frame(self) -> None:
        """Reset to placeholder icon."""
        self._video_label.clear()
        self._video_label.setText("📷")

    # ── close ───────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """Hide the overlay and emit closed signal."""
        self.hide()
        self.closed.emit()

    # ── drag support ───────────────────────────────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_offset = event.globalPosition().toPoint() - self.parent().mapToGlobal(self.pos())
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            parent = self.parent()
            if parent:
                global_pos = event.globalPosition().toPoint() - self._drag_offset
                new_pos = parent.mapFromGlobal(global_pos)
                # Clamp within parent bounds
                pw, ph = parent.width(), parent.height()
                new_pos.setX(max(0, min(new_pos.x(), pw - _CAMERA_OVERLAY_WIDTH)))
                new_pos.setY(max(0, min(new_pos.y(), ph - _CAMERA_OVERLAY_HEIGHT)))
                self.move(new_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# RemoteViewer — main display widget
# ---------------------------------------------------------------------------


class RemoteViewer(QGraphicsView):
    """A ``QGraphicsView`` that displays the remote desktop stream.

    Features
    --------
    - Smooth zoom via scroll wheel
    - Pan by dragging (middle mouse button)
    - Fullscreen toggle
    - HUD overlay (FPS, quality, latency, resolution)
    - Quality indicator bar
    - Aspect-ratio-preserving scaling modes
    """

    # Signal emitted when the viewer requests a fullscreen toggle
    fullscreen_toggled = Signal()
    # Signal emitted with mouse/keyboard events for remote injection
    remote_mouse_event = Signal(int, int, int, bool, bool)  # x, y, button, pressed, abs
    remote_key_event = Signal(str, bool)  # key, pressed
    # Signal emitted when no frame has been received for a while
    frame_timeout = Signal()

    # ── Zoom modes ──────────────────────────────────────────────────

    class FitMode:
        """Scaling behaviour for remote content."""
        FIT_WINDOW = 0      # Scale to fit the viewport
        FIXED_RATIO = 1     # Original 1:1 pixel mapping
        CUSTOM_ZOOM = 2     # User-controlled zoom level

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # ── Scene setup ──
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform
            | QPainter.RenderHint.Antialiasing
        )
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontAdjustForAntialiasing, True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QBrush(QColor("#0f172a")))  # dark background

        # ── State ──
        self._pixmap_item: QGraphicsPixmapItem | None = None
        self._remote_resolution: tuple[int, int] = (1280, 720)
        self._fit_mode = self.FitMode.FIT_WINDOW
        self._zoom_level: float = 1.0
        self._connection_active: bool = False
        self._sharp_text: bool = True  # sharp text mode (default on)

        # ── Ring buffer for frame data (avoids per-frame allocation)
        self._frame_buffers: deque[bytearray] = deque(maxlen=3)
        self._buffer_idx: int = 0

        # ── HUD data ──
        self._fps: float = 0.0
        self._bitrate_kbps: float = 0.0
        self._latency_ms: float = 0.0
        self._frames_received: int = 0
        self._last_hud_update: float = time.time()
        self._last_frame_time: float = time.time()
        self._frame_count_since_hud: int = 0

        # ── HUD timer ──
        self._hud_timer = QTimer(self)
        self._hud_timer.timeout.connect(self._update_hud)
        self._hud_timer.start(_HUD_UPDATE_MS)

        # ── Frame timeout watchdog ──
        self._frame_timeout_timer = QTimer(self)
        self._frame_timeout_timer.setSingleShot(True)
        self._frame_timeout_timer.timeout.connect(self._on_frame_timeout)

        # ── Mouse event coalescing ──
        # Accumula eventi di movimento a ~30 fps invece di inviare
        # ogni singolo mouseMoveEvent (evita accodamento sulla rete).
        self._mouse_coalesce_timer = QTimer(self)
        self._mouse_coalesce_timer.setSingleShot(True)
        self._mouse_coalesce_timer.timeout.connect(self._send_pending_mouse)
        self._pending_mouse: tuple[int, int, int, bool, bool] | None = None
        self._MOUSE_COALESCE_MS = 33  # ~30 fps

        # ── Camera PiP overlay (top-right, draggable) ──
        self._camera_active: bool = False
        self._camera_overlay = CameraOverlay(self)
        self._camera_overlay.setVisible(False)
        self._camera_overlay.closed.connect(self._on_camera_overlay_closed)

        # Placeholder while disconnected
        self._show_placeholder()

    # ── Public API ──────────────────────────────────────────────────

    def set_sharp_text(self, enabled: bool) -> None:
        """Toggle sharp text mode.

        When enabled (default), removes smooth pixmap transformation so
        text remains crisp and pixel-accurate.  Bilinear interpolation
        is disabled in favour of nearest-neighbour scaling.
        """
        self._sharp_text = enabled
        self._apply_sharp_text()

    def _apply_sharp_text(self) -> None:
        """Apply or remove sharp-text rendering hints."""
        if self._sharp_text:
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        else:
            self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        if self._pixmap_item is not None:
            self._pixmap_item.setTransformationMode(
                Qt.TransformationMode.FastTransformation
                if self._sharp_text
                else Qt.TransformationMode.SmoothTransformation
            )

    def display_frame(self, rgb_data: np.ndarray | bytes, width: int, height: int) -> None:
        """Display a decoded video frame.

        Uses a ring buffer to avoid per-frame allocations.  At 1080p@30fps
        this saves ~180 MB/s of ``.copy().tobytes()`` overhead.
        """
        # Convert to QImage
        if isinstance(rgb_data, np.ndarray):
            if rgb_data.dtype != np.uint8:
                rgb_data = rgb_data.clip(0, 255).astype(np.uint8)
            if rgb_data.shape[2] == 3:
                h, w, _ = rgb_data.shape
                expected_size = w * h * 3

                # Get a pre-allocated buffer from the ring buffer
                if (
                    not self._frame_buffers
                    or len(self._frame_buffers[-1]) != expected_size
                ):
                    buf = bytearray(expected_size)
                else:
                    buf = self._frame_buffers.popleft()

                # Single copy: numpy → pre-allocated bytearray
                buf[:expected_size] = rgb_data.tobytes()
                self._frame_buffers.append(buf)
                img = QImage(buf, w, h, w * 3, QImage.Format.Format_RGB888)
                # Keep a reference so the buffer is not GC'd while QImage uses it
                img._np_buffer = buf
            else:
                return
        elif isinstance(rgb_data, bytes):
            img = QImage(rgb_data, width, height, width * 3, QImage.Format.Format_RGB888)
        else:
            return

        pixmap = QPixmap.fromImage(img)

        # Update or create scene pixmap
        if self._pixmap_item is None:
            self._pixmap_item = self._scene.addPixmap(pixmap)
            self._pixmap_item.setTransformationMode(
                Qt.TransformationMode.FastTransformation
                if self._sharp_text
                else Qt.TransformationMode.SmoothTransformation
            )
            self._remote_resolution = (width, height)
            self._apply_fit_mode()
        else:
            self._pixmap_item.setPixmap(pixmap)
            self._remote_resolution = (width, height)

        # Track FPS
        self._frame_count_since_hud += 1
        self._last_frame_time = time.time()

        # Reset the frame timeout watchdog
        if self._connection_active:
            self._frame_timeout_timer.start(_FRAME_TIMEOUT_MS)

    def set_connection_active(self, active: bool) -> None:
        """Mark connection state for UI updates."""
        self._connection_active = active
        if active:
            self._frame_timeout_timer.start(_FRAME_TIMEOUT_MS)
        else:
            self._frame_timeout_timer.stop()
            self.set_camera_active(False)
        self._show_placeholder()

    # ── Zoom / Fit ──────────────────────────────────────────────────

    def set_fit_mode(self, mode: int) -> None:
        """Change zoom mode."""
        self._fit_mode = mode
        self._apply_fit_mode()

    def zoom_in(self) -> None:
        """Zoom in by one step."""
        self._zoom_level = min(_MAX_ZOOM, self._zoom_level * _ZOOM_STEP)
        self._fit_mode = self.FitMode.CUSTOM_ZOOM
        self._apply_zoom()

    def zoom_out(self) -> None:
        """Zoom out by one step."""
        self._zoom_level = max(_MIN_ZOOM, self._zoom_level / _ZOOM_STEP)
        self._fit_mode = self.FitMode.CUSTOM_ZOOM
        self._apply_zoom()

    def zoom_to_fit(self) -> None:
        """Auto-fit the remote screen to the viewport."""
        self._fit_mode = self.FitMode.FIT_WINDOW
        self._apply_fit_mode()

    def zoom_to_original(self) -> None:
        """Show remote screen at 1:1 pixel mapping."""
        self._fit_mode = self.FitMode.FIXED_RATIO
        self._zoom_level = 1.0
        self._apply_zoom()

    @Slot()
    def toggle_fullscreen(self) -> None:
        """Emit fullscreen_toggled signal."""
        self.fullscreen_toggled.emit()

    # ── Event forwarding ───────────────────────────────────────────

    def forward_mouse_event(
        self, x: int, y: int, button: int, pressed: bool, absolute: bool = True
    ) -> None:
        """Forward a mouse event to the remote peer."""
        self.remote_mouse_event.emit(x, y, button, pressed, absolute)

    def forward_key_event(self, key: str, pressed: bool) -> None:
        """Forward a keyboard event to the remote peer."""
        self.remote_key_event.emit(key, pressed)

    # ── HUD ─────────────────────────────────────────────────────────

    def set_latency(self, ms: float) -> None:
        """Update measured round-trip latency."""
        self._latency_ms = ms

    def set_bitrate(self, kbps: float) -> None:
        """Update measured bitrate."""
        self._bitrate_kbps = kbps

    # ── Overridden events ───────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        """Zoom in/out with Ctrl+Scroll."""
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
            event.accept()
        else:
            super().wheelEvent(event)

    def _send_pending_mouse(self) -> None:
        """Invia l'ultima posizione mouse accumulata (coalescing)."""
        if self._pending_mouse is not None:
            self.remote_mouse_event.emit(*self._pending_mouse)
            self._pending_mouse = None

    def _flush_pending_mouse(self) -> None:
        """Forza l'invio immediato della posizione accumulata."""
        if self._mouse_coalesce_timer.isActive():
            self._mouse_coalesce_timer.stop()
        self._send_pending_mouse()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Forward mouse press (all buttons)."""
        btn = self._qt_button_to_remote(event.button())
        if btn is not None:
            # Flush pending movement first, then send click at correct pos
            self._flush_pending_mouse()
            scene_pos = self.mapToScene(event.pos())
            self.remote_mouse_event.emit(
                int(scene_pos.x()), int(scene_pos.y()),
                btn, True, True,
            )
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """Forward mouse release (all buttons)."""
        btn = self._qt_button_to_remote(event.button())
        if btn is not None:
            self._flush_pending_mouse()
            scene_pos = self.mapToScene(event.pos())
            self.remote_mouse_event.emit(
                int(scene_pos.x()), int(scene_pos.y()),
                btn, False, True,
            )
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        """Forward mouse movement with coalescing (~30 fps)."""
        if self._connection_active:
            scene_pos = self.mapToScene(event.pos())
            self._pending_mouse = (
                int(scene_pos.x()), int(scene_pos.y()),
                0, False, True,
            )
            if not self._mouse_coalesce_timer.isActive():
                self._mouse_coalesce_timer.start(self._MOUSE_COALESCE_MS)
        super().mouseMoveEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Re-apply fit mode on resize and reposition camera overlay."""
        super().resizeEvent(event)
        if self._fit_mode == self.FitMode.FIT_WINDOW:
            self._apply_fit_mode()
        self._reposition_camera_overlay()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        """Forward keyboard events and handle local shortcuts."""
        key = self._key_to_name(event.key())
        if key:
            self.remote_key_event.emit(key, True)
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event) -> None:  # noqa: N802
        """Forward keyboard release."""
        key = self._key_to_name(event.key())
        if key:
            self.remote_key_event.emit(key, False)
        super().keyReleaseEvent(event)

    # ── Internal ────────────────────────────────────────────────────

    def _on_frame_timeout(self) -> None:
        """Emitted when no frame arrives within the timeout window."""
        self.frame_timeout.emit()

    def _show_placeholder(self) -> None:
        """Show a placeholder when not connected."""
        pixmap = QPixmap(self._remote_resolution[0], self._remote_resolution[1])
        pixmap.fill(QColor("#1e293b"))

        # Draw centered text on the placeholder
        from PySide6.QtGui import QPainter
        painter = QPainter(pixmap)
        painter.setPen(QColor("#64748b"))
        font = QFont("Segoe UI", 18)
        painter.setFont(font)

        if self._connection_active:
            text = "Waiting for stream..."
        else:
            text = "Disconnected\nUse Session → Connect to start"

        painter.drawText(
            pixmap.rect(),
            Qt.AlignmentFlag.AlignCenter,
            text,
        )
        painter.end()

        if self._pixmap_item is None:
            self._pixmap_item = self._scene.addPixmap(pixmap)
        else:
            self._pixmap_item.setPixmap(pixmap)

    def _apply_fit_mode(self) -> None:
        """Apply the current fit mode."""
        if self._fit_mode == self.FitMode.FIT_WINDOW:
            self.fitInView(
                self._scene.sceneRect(),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
            self._zoom_level = 0.0  # managed by fitInView
        elif self._fit_mode == self.FitMode.FIXED_RATIO:
            self.resetTransform()
            self._zoom_level = 1.0
        else:
            self._apply_zoom()

    def _apply_zoom(self) -> None:
        """Apply the current zoom level."""
        self.resetTransform()
        self.scale(self._zoom_level, self._zoom_level)

    def _update_hud(self) -> None:
        """Recalculate and trigger HUD paint."""
        now = time.time()
        elapsed = now - self._last_hud_update
        if elapsed > 0:
            self._fps = self._frame_count_since_hud / elapsed
        self._frame_count_since_hud = 0
        self._last_hud_update = now
        self.viewport().update()  # trigger paintEvent for HUD

    def _qt_button_to_remote(self, qt_button) -> int | None:
        """Convert a Qt mouse button to remote button code.

        Returns
        -------
        int or None
            1 = left, 2 = middle, 3 = right, None if unsupported.
        """
        btn_map = {
            Qt.MouseButton.LeftButton: 1,
            Qt.MouseButton.MiddleButton: 2,
            Qt.MouseButton.RightButton: 3,
        }
        return btn_map.get(qt_button)

    def _key_to_name(self, qt_key: int) -> str | None:
        """Convert a Qt key code to a remote key name."""
        from PySide6.QtGui import Qt as QtKey

        # Common keys
        key_map = {
            QtKey.Key_Return: "return",
            QtKey.Key_Enter: "return",
            QtKey.Key_Tab: "tab",
            QtKey.Key_Backspace: "backspace",
            QtKey.Key_Delete: "delete",
            QtKey.Key_Escape: "escape",
            QtKey.Key_Home: "home",
            QtKey.Key_End: "end",
            QtKey.Key_PageUp: "pageup",
            QtKey.Key_PageDown: "pagedown",
            QtKey.Key_Up: "up",
            QtKey.Key_Down: "down",
            QtKey.Key_Left: "left",
            QtKey.Key_Right: "right",
            QtKey.Key_Space: "space",
            QtKey.Key_Control: "ctrl",
            QtKey.Key_Alt: "alt",
            QtKey.Key_Shift: "shift",
            QtKey.Key_Meta: "super",
            QtKey.Key_CapsLock: "capslock",
            QtKey.Key_F1: "f1",
            QtKey.Key_F2: "f2",
            QtKey.Key_F3: "f3",
            QtKey.Key_F4: "f4",
            QtKey.Key_F5: "f5",
            QtKey.Key_F6: "f6",
            QtKey.Key_F7: "f7",
            QtKey.Key_F8: "f8",
            QtKey.Key_F9: "f9",
            QtKey.Key_F10: "f10",
            QtKey.Key_F11: "f11",
            QtKey.Key_F12: "f12",
        }
        if qt_key in key_map:
            return key_map[qt_key]

        # Printable characters
        if 0x20 <= qt_key <= 0x7E:
            return chr(qt_key).lower()

        return None

    # ── Camera PiP overlay ────────────────────────────────────────────

    def set_camera_active(self, active: bool) -> None:
        """Show or hide the camera picture-in-picture overlay."""
        self._camera_active = active
        self._camera_overlay.setVisible(active)
        if not active:
            self._camera_overlay.clear_frame()

    def update_camera_frame(self, jpeg_data: bytes) -> None:
        """Update the camera PiP overlay with a new JPEG frame."""
        if not self._camera_active:
            return
        try:
            pixmap = QPixmap()
            if pixmap.loadFromData(jpeg_data, "JPEG"):
                self._camera_overlay.set_pixmap(pixmap)
        except Exception as e:
            logger.warning("Camera overlay update error: %s", e)

    def _on_camera_overlay_closed(self) -> None:
        """User closed the camera PiP overlay — deactivate."""
        self._camera_active = False

    def _reposition_camera_overlay(self) -> None:
        """Position the camera PiP at the top-right of the viewport."""
        if self._camera_overlay and self._camera_overlay.isVisible():
            # Only reposition if user hasn't manually moved it
            if not hasattr(self, '_camera_overlay_moved'):
                x = self.viewport().width() - _CAMERA_OVERLAY_WIDTH - _CAMERA_OVERLAY_MARGIN
                y = _CAMERA_OVERLAY_MARGIN
                self._camera_overlay.move(x, y)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        """Paint HUD overlay on top of the remote view."""
        if not self._connection_active:
            return

        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # ── HUD background ──
        hud_rect = QRectF(12, 12, 220, 100)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(15, 23, 42, 180))  # semi-transparent dark
        painter.drawRoundedRect(hud_rect, 8, 8)

        # ── HUD text ──
        painter.setPen(QColor("#e2e8f0"))
        font = QFont("Monospace", 11)
        painter.setFont(font)

        quality_color = "#22c55e" if self._fps >= 15 else (
            "#f59e0b" if self._fps >= 5 else "#ef4444"
        )

        lines = [
            f"FPS:   {self._fps:5.1f}",
            f"Res:   {self._remote_resolution[0]}×{self._remote_resolution[1]}",
            f"Lat:   {self._latency_ms:5.0f} ms",
            f"Bit:   {self._bitrate_kbps:5.0f} kbps",
        ]

        y = 36
        for i, line in enumerate(lines):
            if i == 0:
                painter.setPen(QColor(quality_color))
            else:
                painter.setPen(QColor("#e2e8f0"))
            painter.drawText(QPointF(22, y), line)
            y += 20

        # ── Quality bar ──
        bar_rect = QRectF(22, y + 4, 200, 4)
        painter.setBrush(QColor("#334155"))
        painter.drawRoundedRect(bar_rect, 2, 2)

        fill_width = max(4, int(200 * min(1.0, self._fps / 30.0)))
        fill_rect = QRectF(22, y + 4, fill_width, 4)
        painter.setBrush(QColor(quality_color))
        painter.drawRoundedRect(fill_rect, 2, 2)


# ---------------------------------------------------------------------------
# Viewer Toolbar
# ---------------------------------------------------------------------------


class ViewerToolbar(QToolBar):
    """Context toolbar for the remote viewer."""

    fullscreen_requested = Signal()
    zoom_in_requested = Signal()
    zoom_out_requested = Signal()
    fit_requested = Signal()
    sharp_toggled = Signal(bool)
    mic_toggled = Signal(bool)
    camera_toggled = Signal(bool)
    disconnect_requested = Signal()
    ctrl_alt_del_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Viewer Controls", parent)
        self.setMovable(False)
        self.setIconSize(QSize(18, 18))

        self._setup_buttons()

    def _setup_buttons(self) -> None:
        from PySide6.QtGui import QAction

        # Fit to window
        fit_act = QAction("⊞ Fit", self)
        fit_act.setToolTip("Fit remote screen to window")
        fit_act.triggered.connect(self.fit_requested)
        self.addAction(fit_act)

        # Zoom out
        zoom_out_act = QAction("−", self)
        zoom_out_act.setToolTip("Zoom out")
        zoom_out_act.triggered.connect(self.zoom_out_requested)
        self.addAction(zoom_out_act)

        # Zoom in
        zoom_in_act = QAction("+", self)
        zoom_in_act.setToolTip("Zoom in")
        zoom_in_act.triggered.connect(self.zoom_in_requested)
        self.addAction(zoom_in_act)

        self.addSeparator()

        # Sharp text toggle (checkable)
        self._sharp_act = QAction("⬡ Sharp", self)
        self._sharp_act.setToolTip("Toggle sharp text mode (nearest-neighbour scaling)")
        self._sharp_act.setCheckable(True)
        self._sharp_act.setChecked(True)
        self._sharp_act.triggered.connect(self._on_sharp_toggled)
        self.addAction(self._sharp_act)

        # Fullscreen
        fs_act = QAction("⛶ Fullscreen", self)
        fs_act.setToolTip("Toggle fullscreen (F11)")
        fs_act.triggered.connect(self.fullscreen_requested)
        self.addAction(fs_act)

        self.addSeparator()

        # Mic toggle
        self._mic_act = QAction("🎤 Mic", self)
        self._mic_act.setToolTip("Toggle microphone streaming")
        self._mic_act.setCheckable(True)
        self._mic_act.setChecked(False)
        self._mic_act.triggered.connect(self.mic_toggled)
        self.addAction(self._mic_act)

        # Camera toggle
        self._cam_act = QAction("📷 Camera", self)
        self._cam_act.setToolTip("Toggle webcam streaming")
        self._cam_act.setCheckable(True)
        self._cam_act.setChecked(False)
        self._cam_act.triggered.connect(self.camera_toggled)
        self.addAction(self._cam_act)

        self.addSeparator()

        # Ctrl+Alt+Del
        cad_act = QAction("✱ Ctrl+Alt+Del", self)
        cad_act.setToolTip("Send Ctrl+Alt+Del to the remote computer")
        cad_act.triggered.connect(self.ctrl_alt_del_requested)
        self.addAction(cad_act)

        # Disconnect
        disc_act = QAction("✕ Disconnect", self)
        disc_act.setToolTip("End current session")
        disc_act.triggered.connect(self.disconnect_requested)
        self.addAction(disc_act)

    def set_mic_checked(self, checked: bool) -> None:
        """Set mic toggle button state without emitting signal."""
        self._mic_act.setChecked(checked)

    def set_camera_checked(self, checked: bool) -> None:
        """Set camera toggle button state without emitting signal."""
        self._cam_act.setChecked(checked)

    def _on_sharp_toggled(self, checked: bool) -> None:
        """Forward sharp toggle to the signal."""
        self.sharp_toggled.emit(checked)


# ---------------------------------------------------------------------------
# ViewerWindow — standalone window for remote display
# ---------------------------------------------------------------------------


class ViewerWindow(QMainWindow):
    """Stand-alone window that shows the remote desktop stream.

    Created by MainWindow when a connection is established.
    Contains a RemoteViewer as its central widget plus a toolbar
    with zoom/fit/fullscreen controls and a disconnect button.
    """

    WINDOW_TITLE = "OpenDesk — Remote Desktop"
    MIN_WIDTH = 800
    MIN_HEIGHT = 600

    frame_timeout = Signal()

    def __init__(
        self,
        on_mouse_event: Callable | None = None,
        on_key_event: Callable | None = None,
        on_disconnect: Callable | None = None,
        on_mic_toggle: Callable | None = None,
        on_camera_toggle: Callable | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.WINDOW_TITLE)
        self.setMinimumSize(self.MIN_WIDTH, self.MIN_HEIGHT)
        self.resize(1280, 720)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        self._on_disconnect_cb = on_disconnect

        # ── Central viewer ──
        self._viewer = RemoteViewer(self)
        self.setCentralWidget(self._viewer)

        # ── Toolbar ──
        self._toolbar = ViewerToolbar(self)
        self._toolbar.fullscreen_requested.connect(self._toggle_fullscreen)
        self._toolbar.zoom_in_requested.connect(self._viewer.zoom_in)
        self._toolbar.zoom_out_requested.connect(self._viewer.zoom_out)
        self._toolbar.fit_requested.connect(self._viewer.zoom_to_fit)
        self._toolbar.sharp_toggled.connect(self._viewer.set_sharp_text)
        self._toolbar.mic_toggled.connect(self._on_mic_toggled)
        self._toolbar.camera_toggled.connect(self._on_camera_toggled)
        self._toolbar.ctrl_alt_del_requested.connect(self._on_ctrl_alt_del)
        self._toolbar.disconnect_requested.connect(self._on_disconnect_clicked)
        self.addToolBar(self._toolbar)

        # ── Status bar ──
        self._status = QStatusBar(self)
        self._status_label = QLabel("Connected")
        self._status.addWidget(self._status_label)
        self.setStatusBar(self._status)

        # Connect external callbacks
        self._on_mic_toggle_cb = on_mic_toggle
        self._on_camera_toggle_cb = on_camera_toggle

        # ── Connect signals → callbacks ──
        if on_mouse_event:
            self._viewer.remote_mouse_event.connect(on_mouse_event)
        if on_key_event:
            self._viewer.remote_key_event.connect(on_key_event)
        self._viewer.frame_timeout.connect(self._on_frame_timeout)

    # ── public API ──────────────────────────────────────────────────

    @property
    def viewer(self) -> RemoteViewer:
        """The embedded RemoteViewer widget."""
        return self._viewer

    @Slot()
    def _on_frame_timeout(self) -> None:
        """No frame received for a while — notify via signal."""
        self.frame_timeout.emit()

    def display_frame(self, rgb_data: np.ndarray, width: int, height: int) -> None:
        """Display a decoded frame in the viewer."""
        self._viewer.display_frame(rgb_data, width, height)

    def set_connection_active(self, active: bool, peer_name: str = "") -> None:
        """Update UI state for connection status."""
        self._viewer.set_connection_active(active)
        if active and peer_name:
            self._status_label.setText(f"Connected to {peer_name}")
            self.setWindowTitle(f"OpenDesk — {peer_name}")
        else:
            self._status_label.setText("Disconnected")
            self.setWindowTitle(self.WINDOW_TITLE)

    def set_sharp_text(self, enabled: bool) -> None:
        """Toggle sharp text mode in the viewer."""
        self._toolbar._sharp_act.setChecked(enabled)
        self._viewer.set_sharp_text(enabled)

    def set_mic_checked(self, checked: bool) -> None:
        """Sync mic toggle state from MainWindow without re-triggering."""
        self._toolbar.set_mic_checked(checked)

    def set_camera_checked(self, checked: bool) -> None:
        """Sync camera toggle state from MainWindow without re-triggering."""
        self._toolbar.set_camera_checked(checked)

    # ── slots ───────────────────────────────────────────────────────

    def _toggle_fullscreen(self) -> None:
        """Toggle fullscreen on this window."""
        if self.isFullScreen():
            self.showNormal()
            self._toolbar.show()
            self._status.show()
        else:
            self.showFullScreen()
            self._toolbar.hide()
            self._status.hide()

    def _on_mic_toggled(self, checked: bool) -> None:
        """Forward mic toggle to external callback."""
        if self._on_mic_toggle_cb:
            self._on_mic_toggle_cb(checked)

    def _on_camera_toggled(self, checked: bool) -> None:
        """Forward camera toggle to external callback."""
        if self._on_camera_toggle_cb:
            self._on_camera_toggle_cb(checked)

    def _on_ctrl_alt_del(self) -> None:
        """Send Ctrl+Alt+Del to the remote peer."""
        # Send Ctrl down, Alt down, Delete down, Delete up, Alt up, Ctrl up
        if hasattr(self._viewer, 'remote_key_event'):
            import itertools
            for key, pressed in [("ctrl", True), ("alt", True), ("delete", True),
                                  ("delete", False), ("alt", False), ("ctrl", False)]:
                self._viewer.remote_key_event.emit(key, pressed)

    def _on_disconnect_clicked(self) -> None:
        """User clicked disconnect."""
        if self._on_disconnect_cb:
            self._on_disconnect_cb()

    def closeEvent(self, event) -> None:  # noqa: N802
        """If user closes the window, trigger disconnect."""
        if self._on_disconnect_cb:
            self._on_disconnect_cb()
        self.hide()
        event.ignore()
