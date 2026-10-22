"""The shared horned NPC glyph used by the map and spawn timers."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPainter, QPainterPath, QPen, QPixmap

from mnmparse.app import theme


def monster_icon(color: str | None = None) -> QIcon:
    return QIcon(_colored_icon(color or theme.MUTED))


@lru_cache(maxsize=32)
def _colored_icon(color: str) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(size / 40, size / 40)
        painter.setPen(QPen(theme.qcolor(color), 2.6, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        face = QPainterPath()
        face.moveTo(10, 15)
        for x, y in ((8, 6), (16, 12), (24, 12), (32, 6), (30, 15),
                     (32, 21), (28, 31), (20, 35), (12, 31), (8, 21), (10, 15)):
            face.lineTo(x, y)
        painter.drawPath(face)
        painter.drawLine(13, 20, 17, 22)
        painter.drawLine(23, 22, 27, 20)
        painter.drawLine(16, 28, 24, 28)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
