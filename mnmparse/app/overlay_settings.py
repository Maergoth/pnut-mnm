"""A shared hover-only settings shortcut for persistent overlay windows."""
from __future__ import annotations

from collections.abc import Callable
import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QToolButton, QWidget

from mnmparse.app import theme


class OverlaySettingsGear(QToolButton):
    """Stay inside ``host`` and reveal its settings shortcut only on hover.

    ``sync`` also handles an owner's click-through setting changing without a
    mouse event. While the host is visible, an application event filter observes
    descendant hover events, including children added later. Hidden hosts retain
    only their own local filter, without polling or observing other windows.
    """

    def __init__(self, host: QWidget, callback: Callable[[], None], *, label: str,
                 input_enabled: Callable[[], bool] | None = None) -> None:
        super().__init__(host)
        self._host = host
        self._callback = callback
        self._input_enabled = input_enabled or (lambda: True)
        self._syncing = False
        self._watching_application = False
        self.setObjectName("OverlaySettingsGear")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAutoRaise(True)
        description = f"Open {str(label).strip() or 'overlay'} settings"
        self.setToolTip(description)
        self.setAccessibleName(description)
        self.setAccessibleDescription("Open this window's settings in PNUT.")
        self.clicked.connect(self._activate)
        self.hide()
        host.installEventFilter(self)
        self.sync()

    def _watch_application(self, watching: bool) -> None:
        if watching == self._watching_application:
            return
        app = QApplication.instance()
        if app is not None:
            if watching:
                app.installEventFilter(self)
            else:
                app.removeEventFilter(self)
        self._watching_application = watching

    def _can_show(self) -> bool:
        host = self._host
        try:
            return (host.isVisible() and host.isEnabled() and not host.window().isMinimized()
                    and bool(self._input_enabled())
                    and host.rect().contains(host.mapFromGlobal(QCursor.pos())))
        except RuntimeError:
            return False  # A host or input owner can be in Qt teardown.

    def sync(self) -> None:
        """Recheck hover, visibility and click-through without polling."""
        if self._syncing:
            return
        self._syncing = True
        try:
            self._watch_application(self._host.isVisible() and not self._host.window().isMinimized())
            visible = self._can_show()
            # Short bars keep their natural height; fit the floating shortcut
            # inside them instead of imposing its usual 30px outer footprint.
            width, height = self._host.width(), self._host.height()
            extent = min(22, max(1, min(width, height) - 2))
            self.setFixedSize(extent, extent)
            self.move(min(4, max(0, (width - extent) // 2)),
                      min(4, max(0, (height - extent) // 2)))
            self.setEnabled(visible)
            self.setVisible(visible)
            if visible:
                self.raise_()
        finally:
            self._syncing = False

    def _activate(self) -> None:
        self.sync()
        if self.isVisible() and self.isEnabled():
            self._callback()

    def eventFilter(self, watched, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if watched is self._host:
            if kind in {QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseMove,
                        QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Move,
                        QEvent.Type.Resize, QEvent.Type.WindowStateChange,
                        QEvent.Type.EnabledChange}:
                self.sync()
        elif kind in {QEvent.Type.Enter, QEvent.Type.Leave, QEvent.Type.MouseMove}:
            if isinstance(watched, QWidget) and self._host.isAncestorOf(watched):
                self.sync()
        return False

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        active = self.underMouse() or self.isDown()
        scale = self.width() / 22
        painter.setBrush(QColor(theme.BG1 if self.isDown() else theme.BG2))
        painter.setPen(QPen(QColor(theme.ACCENT if active else theme.MUTED), 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(.5, .5, -.5, -.5), 5 * scale, 5 * scale)
        painter.translate(QPointF(self.width() / 2, self.height() / 2))
        painter.scale(scale, scale)
        gear = QPainterPath()
        for index in range(32):
            angle = index * math.tau / 32
            radius = 6.8 if index % 4 in (1, 2) else 5.1
            point = QPointF(math.cos(angle) * radius, math.sin(angle) * radius)
            gear.moveTo(point) if index == 0 else gear.lineTo(point)
        gear.closeSubpath()
        gear.addEllipse(QRectF(-2.3, -2.3, 4.6, 4.6))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.ACCENT if active else theme.TEXT))
        painter.drawPath(gear)


__all__ = ["OverlaySettingsGear"]
