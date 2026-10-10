"""Clickable mode label with flowers or a small flame animation."""
from __future__ import annotations

import math
from typing import Any

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QPushButton

from mnmparse.app.morality import reduced_motion
from mnmparse.privacy import CASUAL_LABEL, ELITIST_LABEL, casual_enabled


class ModeButton(QPushButton):
    def __init__(self, cfg: Any, parent=None):
        super().__init__(parent)
        self._phase = 0.0
        self._fire_timer = QTimer(self)
        self._fire_timer.setInterval(120)
        self._fire_timer.timeout.connect(self._advance_fire)
        self.setMinimumHeight(44)
        self.set_config(cfg)

    def set_config(self, cfg: Any) -> None:
        self._casual = casual_enabled(cfg)
        self._animate = not self._casual and not reduced_motion(cfg)
        self.setText(CASUAL_LABEL if self._casual else f"🔥 {ELITIST_LABEL} 🔥")
        self.setAccessibleName("Casual Mode" if self._casual else ELITIST_LABEL)
        self.setStyleSheet(
            "QPushButton { padding: 6px 12px; border-radius: 8px; "
            + ("background: #203029; color: #e1f5dd; border: 1px solid #4e7455; }"
               if self._casual else
               "background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #49151b,stop:1 #923216); "
               "color: #ffe8b2; border: 1px solid #ff9a37; }")
            + "QPushButton:focus { border: 2px solid #f0b35b; }"
        )
        self._sync_animation()
        self.update()

    def _sync_animation(self) -> None:
        if self._animate and self.isVisible():
            self._fire_timer.start()
        else:
            self._fire_timer.stop()

    def sizeHint(self):  # noqa: N802
        hint = super().sizeHint()
        hint.setHeight(max(44, hint.height()))
        return hint

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def _advance_fire(self) -> None:
        self._phase = (self._phase + 0.35) % (2 * math.pi)
        self.update()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._sync_animation()

    def hideEvent(self, event):  # noqa: N802
        self._fire_timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):  # noqa: N802
        super().paintEvent(event)
        if self._casual:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(QRectF(self.rect()).adjusted(3, 3, -3, -3))
        painter.setPen(Qt.PenStyle.NoPen)
        base = self.height() - 2
        for index, x in enumerate(range(6, self.width() - 6, 18)):
            height = 7 + 2 * math.sin(self._phase + index * 1.7)
            flame = QPainterPath()
            flame.moveTo(x - 7, base)
            flame.cubicTo(x - 10, base - 5, x + 2, base - height + 4, x, base - height)
            flame.cubicTo(x + 9, base - 5, x + 8, base - 3, x + 7, base)
            flame.closeSubpath()
            painter.setBrush(QColor(255, 107, 29, 175))
            painter.drawPath(flame)
            painter.setBrush(QColor(255, 202, 67, 195))
            painter.drawEllipse(QRectF(x - 2, base - 4, 4, 6))
