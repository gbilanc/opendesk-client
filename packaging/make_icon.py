"""Generate opendesk-host.ico (multi-size) from opendesk.svg using Qt + Pillow."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    from PIL import Image

    QGuiApplication(sys.argv)  # required by QtSvg/QPainter

    root = Path(__file__).resolve().parents[1]
    svg = root / "opendesk" / "ui" / "resources" / "opendesk.svg"
    out = Path(__file__).resolve().parent / "opendesk-host.ico"

    renderer = QSvgRenderer(str(svg))
    image = QImage(256, 256, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter)
    painter.end()

    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    buf = image.bits()
    try:
        data = bytes(buf)  # PySide6 >= 6.6 returns a memoryview
    except (TypeError, AttributeError):
        buf.setsize(image.sizeInBytes())  # older sip.voidptr
        data = bytes(buf)
    pil = Image.frombuffer("RGBA", (256, 256), data, "raw", "RGBA", 0, 1)
    pil.save(out, format="ICO", sizes=SIZES)
    print(f"[icon] wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
