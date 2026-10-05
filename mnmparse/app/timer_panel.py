"""Trigger timers: a window docked under the overlay, above the auto-attack bar.

Each running timer is one row: a radial ring that empties as time runs out, the label and
a minutes:seconds counter.  The ring turns amber in the warning period and red in the last
five seconds; an ended timer flashes "0:00" for a moment.  Right-click a timer to cancel
it (or all of them).  The panel shows only while timers run (unless "always show" is on)
and docks / undocks like the auto-attack bar (:mod:`mnmparse.app.docked_panel`).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QMenu

from mnmparse.app.docked_panel import DockedPanel
from mnmparse.app.widgets import make_font, qcolor, token
from mnmparse.triggers import ActiveTimer

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow
    from mnmparse.app.triggers_runtime import TriggerRunner

FRAME_MS = 33
MAX_ROWS = 8


def format_remaining(seconds: float) -> str:
    """``75.2`` -> ``"1:16"`` (rounded up, so a timer reads 0:00 only when it has ended);
    an hour or more reads ``"1:02:05"``."""
    total = int(-(-max(0.0, seconds) // 1))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def timer_color(timer: ActiveTimer, now: float) -> QColor:
    """Resolve normal, warning, and low-duration colors, including the ended flash."""
    left = timer.remaining(now)
    if timer.ended:
        return qcolor(timer.low_color or token("DANGER"), 0.95 if int(now * 4) % 2 == 0 else 0.45)
    if left <= timer.low_s:
        return qcolor(timer.low_color or token("DANGER"))
    if timer.warn_s and left <= timer.warn_s:
        return qcolor(timer.warn_color or token("ACCENT"))
    return qcolor(timer.color or token("SUCCESS"))


class TimerPanel(DockedPanel):
    """The timer window (see module docstring)."""

    SETTINGS_GROUP = "timer_panel"
    TITLE = "PNUT M&M Timers"

    def __init__(self, owner: "OverlayWindow", settings: QSettings, flags: Qt.WindowType) -> None:
        super().__init__(owner, settings, flags)
        self.runner: "TriggerRunner | None" = None
        self.always_show = str(settings.value(f"{self.SETTINGS_GROUP}/always_show", "false")).lower() in ("true", "1")
        self._px = 13.0
        self._f_label = make_font(self._px * 0.95, weight=QFont.Weight.DemiBold)
        self._f_time = make_font(self._px * 1.05, weight=QFont.Weight.DemiBold, tabular=True)
        self._rows: list[tuple[QRectF, str]] = []  #: (row rect, timer id) from the last paint
        self._anim = QTimer(self)
        self._anim.setInterval(FRAME_MS)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._frame)
        self.setToolTip("Trigger timers. Right-click a timer to cancel it.")
        self._resize_for(1)

    # -- wiring --------------------------------------------------------------------------
    def set_runner(self, runner: "TriggerRunner | None") -> None:
        if self.runner is not None:
            try:
                self.runner.timers_changed.disconnect(self.refresh)
            except (RuntimeError, TypeError):
                pass
        self.runner = runner
        if runner is not None:
            runner.timers_changed.connect(self.refresh)
        self.refresh()

    def timers(self) -> list[ActiveTimer]:
        return self.runner.board.ordered() if self.runner is not None else []

    def wants_visible(self) -> bool:
        return self.always_show or bool(self.timers())

    def set_always_show(self, on: bool) -> None:
        self.always_show = bool(on)
        self._settings.setValue(f"{self.SETTINGS_GROUP}/always_show", self.always_show)
        self.refresh()

    def set_font_px(self, px: float) -> None:
        self._px = px
        self._f_label = make_font(px * 0.95, weight=QFont.Weight.DemiBold)
        self._f_time = make_font(px * 1.05, weight=QFont.Weight.DemiBold, tabular=True)
        self.refresh()

    def refresh(self) -> None:
        """Re-layout after timers started / ended (``TriggerRunner.timers_changed``)."""
        self._resize_for(min(MAX_ROWS, max(1, len(self.timers()))))
        self.sync()
        if self.isVisible() and self.timers():
            self._anim.start()
        self.update()

    def _resize_for(self, rows: int) -> None:
        row_h = self._row_h()
        self.set_rows_height(10 + rows * row_h + (rows - 1) * 4)

    def _row_h(self) -> int:
        return int(round(self._px * 2.3))

    def _frame(self) -> None:
        if not self.timers():
            self._anim.stop()
        self.update()

    # -- painting ------------------------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        opacity = float(getattr(self._owner, "opacity", 0.85))
        path = QPainterPath()
        path.addRoundedRect(r, 8, 8)
        p.fillPath(path, qcolor(token("BG0"), opacity))
        p.setPen(QPen(qcolor(token("LINE")), 1))
        p.drawPath(path)
        now = time.time()
        timers = self.timers()[:MAX_ROWS]
        self._rows = []
        row_h = self._row_h()
        if not timers:
            p.setFont(self._f_label)
            p.setPen(qcolor(token("MUTED")))
            p.drawText(r.adjusted(10, 0, -10, 0), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       "Timers: none running")
            p.end()
            return
        fm_time = QFontMetricsF(self._f_time)
        time_w = fm_time.horizontalAdvance("88:88") + 6
        fm_label = QFontMetricsF(self._f_label)
        for i, t in enumerate(timers):
            top = r.top() + 5 + i * (row_h + 4)
            row = QRectF(r.left() + 8, top, r.width() - 16, row_h)
            self._rows.append((row, t.id))
            left = t.remaining(now)
            frac = t.fraction(now)
            color = timer_color(t, now)
            # radial: a track ring and the remaining arc (12 o'clock, clockwise, emptying)
            d = row_h - 6
            ring = QRectF(row.left() + 1, row.center().y() - d / 2, d, d)
            width = max(2.5, d * 0.16)
            p.setPen(QPen(qcolor("#ffffff", 0.10), width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(ring.adjusted(width / 2, width / 2, -width / 2, -width / 2))
            pen = QPen(color, width)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            span = int(-360 * 16 * frac)
            if span:
                p.drawArc(ring.adjusted(width / 2, width / 2, -width / 2, -width / 2), 90 * 16, span)
            # label and counter
            text_left = ring.right() + 8
            p.setFont(self._f_time)
            p.setPen(color if (t.ended or left <= t.low_s) else qcolor(token("TEXT")))
            p.drawText(QRectF(row.right() - time_w, row.top(), time_w, row.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), format_remaining(left))
            p.setFont(self._f_label)
            p.setPen(qcolor(token("MUTED") if t.ended else token("TEXT")))
            label_rect = QRectF(text_left, row.top(), row.right() - time_w - text_left - 6, row.height())
            p.drawText(label_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm_label.elidedText(t.label, Qt.TextElideMode.ElideRight, label_rect.width()))
            # a thin progress line under the row (easier to read at a glance than the ring alone)
            line = QRectF(text_left, row.bottom() - 3, (row.right() - text_left) * frac, 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(color.name(), 0.55))
            p.drawRoundedRect(line, 1, 1)
        p.end()

    # -- interaction ---------------------------------------------------------------------
    def _timer_at(self, pos: QPointF) -> str | None:
        return next((tid for rect, tid in self._rows if rect.contains(pos)), None)

    def add_menu_entries(self, menu: QMenu, event: Any) -> dict[Any, Any]:
        handlers: dict[Any, Any] = {}
        runner = self.runner
        tid = self._timer_at(QPointF(event.pos()))
        if runner is not None and tid is not None:
            label = next((t.label for t in runner.board.timers if t.id == tid), "timer")
            cancel = menu.addAction(f"Cancel “{label}”")
            handlers[cancel] = lambda: runner.cancel_timer(tid)
        if runner is not None and runner.board.timers:
            cancel_all = menu.addAction("Cancel all timers")
            handlers[cancel_all] = runner.clear_timers
        always = menu.addAction("Always show this panel")
        always.setCheckable(True)
        always.setChecked(self.always_show)
        handlers[always] = lambda: self.set_always_show(not self.always_show)
        return handlers

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        super().hideEvent(event)
        self._anim.stop()


__all__ = ["TimerPanel", "format_remaining"]
