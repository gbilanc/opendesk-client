"""
Clipboard synchronisation between local and remote peers.

Monitors the local clipboard for changes and broadcasts text/image
content to the remote peer.  Supports text and image (PNG) formats.
"""

from __future__ import annotations

import io
import logging
import time

from PIL import Image

from PySide6.QtCore import QMimeData, QTimer, Signal, QObject
from PySide6.QtGui import QClipboard, QGuiApplication

from opendesk.network.protocol import Message, MessageType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_POLL_INTERVAL_MS = 500  # check clipboard every 500 ms
_MAX_IMAGE_SIZE = 10 * 1024 * 1024  # 10 MB max image transfer


# ---------------------------------------------------------------------------
# Clipboard sync engine
# ---------------------------------------------------------------------------


class ClipboardSync(QObject):
    """Monitors the local clipboard and syncs changes to a remote peer.

    Signals
    -------
    text_received(str)
        Emitted when text is received from the remote peer.
    image_received(QImage)
        Emitted when an image is received from the remote peer.
    sync_toggled(bool)
        Emitted when sync is enabled/disabled.
    """

    text_received = Signal(str)
    image_received = Signal(object)  # QImage
    sync_toggled = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._enabled: bool = False
        self._last_text: str = ""
        self._last_image_hash: int = 0
        self._send_fn = None

        # Track async tasks to avoid untracked fire-and-forget leaks
        self._pending_tasks: set = set()

        # Local clipboard (Qt's built-in clipboard)
        self._clipboard: QClipboard = QGuiApplication.clipboard()

        # Polling timer (Qt doesn't have clipboard change signals on all platforms)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll_clipboard)

    # ── public API ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def start(self, send_fn) -> None:
        """Enable clipboard syncing.

        Parameters
        ----------
        send_fn : async callable
            Function to send a Message to the remote peer.
        """
        self._enabled = True
        self._send_fn = send_fn
        self._timer.start(_POLL_INTERVAL_MS)
        self.sync_toggled.emit(True)
        logger.info("Clipboard sync started")

    def stop(self) -> None:
        """Disable clipboard syncing."""
        self._enabled = False
        self._timer.stop()
        # Cancel pending tasks
        for task in self._pending_tasks:
            if not task.done():
                task.cancel()
        self._pending_tasks.clear()
        self.sync_toggled.emit(False)
        logger.info("Clipboard sync stopped")

    def toggle(self) -> bool:
        """Toggle sync on/off. Returns the new state."""
        if self._enabled:
            self.stop()
        elif self._send_fn is not None:
            self.start(self._send_fn)
        # If _send_fn is None, toggle() can't enable — caller must use start()
        return self._enabled

    async def receive_from_remote(self, msg: Message) -> None:
        """Handle clipboard data received from the remote peer."""
        if not self._enabled:
            return

        msg_type = msg.type
        payload = msg.payload

        if msg_type == MessageType.CLIPBOARD_TEXT:
            text = payload.get("text", "")
            self._last_text = text
            self.text_received.emit(text)

            # Set local clipboard (temporarily disable polling to avoid loop)
            self._timer.stop()
            self._clipboard.setText(text)
            self._timer.start(_POLL_INTERVAL_MS)

        elif msg_type == MessageType.CLIPBOARD_IMAGE:
            img_data = payload.get("data", b"")
            if len(img_data) > _MAX_IMAGE_SIZE:
                logger.warning("Clipboard image too large: %d bytes", len(img_data))
                return

            from PySide6.QtGui import QImage

            image = QImage()
            if image.loadFromData(img_data):
                self._timer.stop()
                self._clipboard.setImage(image)
                self._timer.start(_POLL_INTERVAL_MS)
                self.image_received.emit(image)

        elif msg_type == MessageType.CLIPBOARD_SYNC:
            enabled = payload.get("enabled", True)
            if enabled:
                self.start(self._send_fn)
            else:
                self.stop()

    # ── internal ────────────────────────────────────────────────────

    def _poll_clipboard(self) -> None:
        """Check for local clipboard changes and broadcast them."""
        if not self._enabled or self._send_fn is None:
            return

        # Clean up completed tasks
        self._pending_tasks = {t for t in self._pending_tasks if not t.done()}

        mime: QMimeData | None = self._clipboard.mimeData()
        if mime is None:
            return

        # Check text
        if mime.hasText():
            text = mime.text()
            if text and text != self._last_text:
                self._last_text = text
                import asyncio
                task = asyncio.ensure_future(
                    self._send_fn(Message.clipboard_text(text))
                )
                self._pending_tasks.add(task)
                logger.debug("Clipboard text synced: %d chars", len(text))

        # Check image (less frequently - skip every other poll)
        elif mime.hasImage() and int(time.time() * 2) % 2 == 0:
            import hashlib

            image = self._clipboard.image()
            if image.isNull():
                return

            # Simple hash to detect change
            h = hash((image.width(), image.height(), image.pixel(0, 0)))
            if h == self._last_image_hash:
                return
            self._last_image_hash = h

            # Convert to PNG bytes
            from PySide6.QtCore import QBuffer, QIODevice

            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buf, "PNG")
            img_data = bytes(buf.data())

            if len(img_data) <= _MAX_IMAGE_SIZE:
                msg = Message(
                    MessageType.CLIPBOARD_IMAGE,
                    {"data": img_data},
                )
                import asyncio
                task = asyncio.ensure_future(self._send_fn(msg))
                self._pending_tasks.add(task)
                logger.debug("Clipboard image synced: %d bytes", len(img_data))
