"""A passive edge vignette maintained for a confirmed player-stat warning."""
from __future__ import annotations

import math
import time

from PySide6.QtCore import QRect, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPaintEvent
from PySide6.QtWidgets import QWidget

from mnmparse.app.window_identity import window_title

EDGE_FRACTION = 0.12
PULSE_SECONDS = 1.6
LOW_HEALTH_COLOR = "#ed3030"
OVERBURDENED_COLOR = "#a4a4a4"


class LowHealthFlash(QWidget):
    """Never accepts input or focus; remains active until recovery calls ``stop``."""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus
                         | Qt.WindowType.WindowTransparentForInput)
        self.setWindowTitle(window_title("low-health"))
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._active = False
        self._started = 0.0
        self._reduced_motion = False
        self._color = QColor(LOW_HEALTH_COLOR)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)

    def flash(self, geometry: QRect, *, reduced_motion: bool = False) -> None:
        """Show or update the warning without restarting an existing pulse."""
        if geometry.isEmpty():
            return
        self.setGeometry(geometry)
        self._reduced_motion = bool(reduced_motion)
        if not self._active:
            self._active = True
            self._started = time.monotonic()
        if not self.isVisible():
            self.show()
        self.update()
        if self._reduced_motion:
            self._timer.stop()
        elif not self._timer.isActive():
            self._timer.start()

    @property
    def active(self) -> bool:
        return self._active

    def set_warning_color(self, color: str) -> None:
        """Change warning priority without restarting an existing pulse."""
        self._color = QColor(color)
        self.update()

    def stop(self) -> None:
        self._active = False
        self._timer.stop()
        self.hide()

    def _tick(self) -> None:
        if self._active and not self._reduced_motion:
            self.update()
        else:
            self._timer.stop()

    def _strength(self) -> float:
        if self._reduced_motion:
            return 0.40
        age = max(0.0, time.monotonic() - self._started)
        return 0.30 + 0.15 * (0.5 + 0.5 * math.cos(age * math.tau / PULSE_SECONDS))

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        if not self._active:
            return
        strength = self._strength()
        depth = max(1, round(min(self.width(), self.height()) * EDGE_FRACTION))
        painter = QPainter(self)
        width, height = self.width(), self.height()
        # Four smooth strips keep the center completely transparent. Their
        # overlap gives the corners a slightly stronger warning.
        for rect, start_x, start_y, end_x, end_y in (
            (QRectF(0, 0, width, depth), 0, 0, 0, depth),
            (QRectF(0, height - depth, width, depth), 0, height, 0, height - depth),
            (QRectF(0, 0, depth, height), 0, 0, depth, 0),
            (QRectF(width - depth, 0, depth, height), width, 0, width - depth, 0),
        ):
            gradient = QLinearGradient(start_x, start_y, end_x, end_y)
            for position, weight in ((0.0, 1.0), (0.18, 0.74), (0.55, 0.28), (1.0, 0.0)):
                color = QColor(self._color)
                color.setAlphaF(strength * weight)
                gradient.setColorAt(position, color)
            painter.fillRect(rect, gradient)
        painter.end()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop()
        super().closeEvent(event)
