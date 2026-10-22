"""A short, mouse-transparent outline around a settings navigation destination."""
from __future__ import annotations

import math

from PySide6.QtCore import QElapsedTimer, QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QWidget

from mnmparse.app import theme
from mnmparse.app.morality import reduced_motion

HIGHLIGHT_MS = 1600


class SettingsHighlight(QWidget):
    """Paint above a destination without replacing its style, effect or layout."""

    def __init__(self, target: QWidget, cfg) -> None:
        super().__init__(target)
        self.setObjectName("SettingsHighlight")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._static = reduced_motion(cfg)
        self._elapsed = QElapsedTimer()
        self._elapsed.start()
        self._timer = QTimer(self)
        self._timer.setInterval(HIGHLIGHT_MS if self._static else 33)
        self._timer.timeout.connect(self._tick)
        target.installEventFilter(self)
        self.setGeometry(target.rect())
        self.show()
        self.raise_()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()
        self.deleteLater()

    def _tick(self) -> None:
        if self._elapsed.elapsed() >= HIGHLIGHT_MS:
            self.stop()
        elif not self._static:
            self.update()

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        elif event.type() == QEvent.Type.Hide:
            self.stop()
        return False

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = theme.qcolor(theme.ACCENT)
        progress = min(1.0, self._elapsed.elapsed() / HIGHLIGHT_MS)
        color.setAlpha(190 if self._static else round(130 + 90 * math.sin(math.pi * progress)))
        painter.setPen(QPen(color, 2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5), 6, 6)
