"""Render the editable SVG into desktop assets and an actual-size review sheet.

Run with the GUI dependencies installed: python tools/build_app_icon.py
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageDraw
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "dedupe" / "gui" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render(renderer: QSvgRenderer, size: int) -> Image.Image:
    import io

    canvas = QImage(size, size, QImage.Format.Format_ARGB32)
    canvas.fill(0)
    painter = QPainter(canvas)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    canvas.save(buffer, "PNG")
    return Image.open(io.BytesIO(bytes(data))).convert("RGBA")


def main() -> None:
    application = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(str(ASSETS / "samesame.svg"))
    if not renderer.isValid():
        raise RuntimeError("Invalid icon SVG")
    frames = {size: render(renderer, size) for size in SIZES}
    render(renderer, 512).save(ASSETS / "samesame.png")
    frames[256].save(
        ASSETS / "samesame.ico", sizes=[(size, size) for size in SIZES],
        append_images=[frames[size] for size in SIZES if size != 256],
    )

    sheet = Image.new("RGB", (800, 480), "#f6f1e6")
    draw = ImageDraw.Draw(sheet)
    draw.text((32, 20), "SameSame / shared cut", fill="#292d30")
    sheet.paste(frames[256], (272, 52), frames[256])
    draw.rectangle((0, 390, 799, 479), fill="#1c1f22")
    for index, size in enumerate((16, 20, 24, 32, 48, 64)):
        x = 76 + index * 128
        for center_y, label_y, color in ((350, 378, "#292d30"), (430, 468, "#f6f1e6")):
            sheet.paste(frames[size], (x - size // 2, center_y - size // 2), frames[size])
            draw.text((x - 16, label_y), f"{size} px", fill=color)
    output = ROOT / "docs" / "design"
    output.mkdir(parents=True, exist_ok=True)
    sheet.save(output / "icon-preview.png")
    # Keep the Qt application alive until all rendering is complete.
    del application


if __name__ == "__main__":
    main()
