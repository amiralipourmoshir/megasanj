"""Circular, center-cropped profile pictures on transparent backgrounds."""
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QPainter, QPainterPath, QPixmap


def circular_avatar(source, size):
    pixels = size * 2
    cropped = source.scaled(pixels, pixels, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    output = QPixmap(pixels, pixels)
    output.fill(Qt.transparent)
    painter = QPainter(output)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(QRectF(0, 0, pixels, pixels))
    painter.setClipPath(clip)
    painter.drawPixmap((pixels-cropped.width())//2, (pixels-cropped.height())//2, cropped)
    painter.end()
    output.setDevicePixelRatio(2)
    return output
