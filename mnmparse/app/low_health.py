"""A brief, passive red border for a confirmed personal low-health alert."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QRect, QTimer, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from mnmparse.app.window_identity import window_title


class LowHealthFlash(QWidget):
    """Never accepts input or activates the game; only its own border is painted."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setWindowTitle(window_title("low-health"))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._started = 0.0
        self._reduced_motion = False
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def flash(self, geometry: QRect, *, reduced_motion: bool = False) -> None:
        if geometry.isEmpty():
            return
        self.setGeometry(geometry)
        self._reduced_motion = reduced_motion
        self._started = time.monotonic()
        self.show()
        self.update()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        if time.monotonic() - self._started >= 1.4:
            self.stop()
        else:
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        age = max(0.0, time.monotonic() - self._started)
        strength = 0.55 if self._reduced_motion else 0.35 + 0.3 * (0.5 + 0.5 * math.cos(age * math.pi * 2))
        strength *= max(0.0, 1.0 - age / 1.4)
        painter = QPainter(self)
        width = min(24, max(8, self.width() // 80))
        # The center remains transparent, including small windowed games.
        for inset in range(width):
            color = QColor("#ed3030")
            color.setAlphaF(strength * (1.0 - inset / width))
            painter.setPen(color)
            painter.drawRect(self.rect().adjusted(inset, inset, -inset - 1, -inset - 1))
