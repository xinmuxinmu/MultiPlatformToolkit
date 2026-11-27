"""Render the launcher SVG to a preview PNG and a Windows icon."""
from __future__ import annotations

import io
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> None:
    application = QGuiApplication.instance() or QGuiApplication([])
    assets = Path(__file__).resolve().parent / "assets"
    renderer = QSvgRenderer(str(assets / "toolkit.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid launcher SVG")

    def render(size: int) -> Image.Image:
        canvas = QImage(size * 4, size * 4, QImage.Format.Format_ARGB32_Premultiplied)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            renderer.render(painter)
        finally:
            painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if not canvas.save(buffer, "PNG"):
            raise RuntimeError("Could not render launcher icon")
        with Image.open(io.BytesIO(bytes(buffer.data()))) as image:
            return image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)

    sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
    frames = [render(size) for size in sizes]
    icon = assets / "toolkit.ico"
    frames[-1].save(
        icon, format="ICO", sizes=[(size, size) for size in sizes], append_images=frames[:-1]
    )
    render(512).save(assets / "toolkit.png")
    print(f"Created: {icon}")
    print(f"Created: {assets / 'toolkit.png'}")


if __name__ == "__main__":
    main()
