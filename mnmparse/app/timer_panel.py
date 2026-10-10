"""Trigger timers and fading notifications docked above the auto-attack bar.

Each running timer is one row: a radial ring that empties as time runs out, the label and
a minutes:seconds counter.  The ring turns amber in the warning period and red in the last
five seconds; an ended trigger timer flashes "0:00" for a moment. Expired NPC timers stay
until dismissed, with restart and dismiss icons in place of the countdown. Right-click
a timer to cancel it (or all of them). The panel shows while timers run or recent triggers
are displayed (unless "always show" is on) and docks / undocks like the auto-attack bar
(:mod:`mnmparse.app.docked_panel`).
Accepted triggers without a countdown append a brief notification below the running
timers. Notifications fade away after four seconds; timed triggers show only their timer.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, QSettings, QSignalBlocker, QSize, Qt, QTimer
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QIcon, QPainter, QPainterPath, QPaintEvent, QPen, QPixmap,
    QResizeEvent, QWheelEvent,
)
from PySide6.QtWidgets import QMenu, QPushButton, QScrollBar

from mnmparse.app.docked_panel import DockedPanel
from mnmparse.app.widgets import make_font, qcolor, token
from mnmparse.triggers import ActiveTimer, Match, fill_placeholders

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow
    from mnmparse.app.triggers_runtime import TriggerRunner

FRAME_MS = 33
MAX_ROWS = 8
MAX_POPUPS = 4
POPUP_SECONDS = 4.0
POPUP_FADE_SECONDS = 1.0


@dataclass(frozen=True)
class TriggerPopup:
    label: str
    created_at: float
    color: str

    def opacity(self, now: float) -> float:
        return max(0.0, min(1.0, (POPUP_SECONDS - (now - self.created_at)) / POPUP_FADE_SECONDS))


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
        if getattr(timer, "keep_until_dismissed", False):
            return qcolor(timer.low_color or token("DANGER"))
        return qcolor(timer.low_color or token("DANGER"), 0.95 if int(now * 4) % 2 == 0 else 0.45)
    if left <= timer.low_s:
        return qcolor(timer.low_color or token("DANGER"))
    if timer.warn_s and left <= timer.warn_s:
        return qcolor(timer.warn_color or token("ACCENT"))
    return qcolor(timer.color or token("SUCCESS"))


@lru_cache(maxsize=8)
def _action_icon(action: str, color: str) -> QIcon:
    """Draw font-independent action glyphs at several resolutions for scaled overlays."""
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 96, 128):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(size / 24, size / 24)
        painter.setPen(QPen(qcolor(color), 2.2, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if action == "Restart":
            painter.drawArc(QRectF(5, 5, 14, 14), 345 * 16, -300 * 16)
            arrow = QPainterPath(QPointF(15.5, 2.8))
            arrow.lineTo(17, 7)
            arrow.lineTo(12.7, 5.6)
            painter.drawPath(arrow)
        else:
            painter.drawLine(QPointF(7, 7), QPointF(17, 17))
            painter.drawLine(QPointF(7, 17), QPointF(17, 7))
        painter.end()
        icon.addPixmap(pixmap)
    return icon


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
        self._expired_buttons: dict[str, tuple[QPushButton, QPushButton]] = {}
        self._scroll = QScrollBar(Qt.Orientation.Vertical, self)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self._scroll.setAccessibleName("Timer rows")
        self._scroll.setToolTip("Scroll to see more timers")
        self._scroll.setStyleSheet(
            "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.25); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        self._scroll.hide()
        self._scroll.valueChanged.connect(self.refresh)
        self._popups: list[TriggerPopup] = []
        self._anim = QTimer(self)
        self._anim.setInterval(FRAME_MS)
        self._anim.setTimerType(Qt.TimerType.PreciseTimer)
        self._anim.timeout.connect(self._frame)
        self.setToolTip("Running timers and recent triggers. Expired NPC timers can be restarted or dismissed. "
                        "Right-click a timer to cancel it.")
        self._resize_for(1)

    # -- wiring --------------------------------------------------------------------------
    def set_runner(self, runner: "TriggerRunner | None") -> None:
        if runner is self.runner:
            self.refresh()
            return
        if self.runner is not None:
            for signal, slot in ((self.runner.timers_changed, self.refresh), (self.runner.fired, self._trigger_fired)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.runner = runner
        self._popups.clear()
        if runner is not None:
            runner.timers_changed.connect(self.refresh)
            runner.fired.connect(self._trigger_fired)
        self.refresh()

    def _trigger_fired(self, match: Match) -> None:
        trigger = match.trigger
        if trigger.timer:
            return
        label = fill_placeholders(trigger.timer_label or trigger.name, match.values())
        self._popups.append(TriggerPopup(label, time.monotonic(), trigger.timer_color or token("ACCENT")))
        self._popups = self._popups[-MAX_POPUPS:]
        self.refresh()

    def _prune_popups(self) -> bool:
        now = time.monotonic()
        before = len(self._popups)
        self._popups = [popup for popup in self._popups if now - popup.created_at < POPUP_SECONDS]
        return len(self._popups) != before

    def timers(self) -> list[ActiveTimer]:
        return self.runner.board.ordered() if self.runner is not None else []

    def _visible_timers(self) -> list[ActiveTimer]:
        offset = self._scroll.value()
        return self.timers()[offset:offset + MAX_ROWS]

    def _needs_animation(self) -> bool:
        return bool(self._popups) or any(not (timer.ended and getattr(timer, "keep_until_dismissed", False))
                                        for timer in self.timers())

    def wants_visible(self) -> bool:
        return self.always_show or bool(self.timers()) or bool(self._popups)

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
        """Re-layout after a timer or notification changes."""
        self.sync()
        self.update()

    def sync(self) -> None:
        # The owner also calls sync when an overlay is re-shown. Expire notifications
        # before showing anything; their lifetime never pauses while the panel is hidden.
        self._prune_popups()
        with QSignalBlocker(self._scroll):
            self._scroll.setRange(0, max(0, len(self.timers()) - MAX_ROWS))
            self._scroll.setPageStep(MAX_ROWS)
        self._scroll.setVisible(len(self.timers()) > MAX_ROWS)
        self._resize_for(max(1, min(MAX_ROWS, len(self.timers())) + len(self._popups)))
        self._sync_expired_buttons()
        super().sync()
        self._position_controls()
        if self.isVisible() and self._needs_animation():
            self._anim.start()
        else:
            self._anim.stop()

    def _resize_for(self, rows: int) -> None:
        row_h = self._row_h()
        self.set_rows_height(10 + rows * row_h + (rows - 1) * 4)

    def _row_h(self) -> int:
        return int(round(self._px * 2.3))

    def _frame(self) -> None:
        if self._prune_popups():
            self.refresh()
        if not self.isVisible() or not self._needs_animation():
            self._anim.stop()
        self.update()

    def _button_widths(self) -> tuple[int, int, int]:
        side = min(self._row_h() - 4, max(18, round(self._px * 1.8)))
        return side, side, max(4, round(self._px * 0.35))

    def _row_rect(self, index: int) -> QRectF:
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        scroll_space = 14 if self._scroll.maximum() > 0 else 0
        return QRectF(r.left() + 8, r.top() + 5 + index * (self._row_h() + 4),
                      r.width() - 16 - scroll_space, self._row_h())

    def _sync_expired_buttons(self) -> None:
        expired = {timer.id: timer for timer in self._visible_timers()
                   if timer.ended and getattr(timer, "keep_until_dismissed", False)}
        for timer_id in self._expired_buttons.keys() - expired.keys():
            for button in self._expired_buttons.pop(timer_id):
                button.hide()
                button.deleteLater()
        for timer_id, timer in expired.items():
            if timer_id not in self._expired_buttons:
                restart, dismiss = QPushButton(self), QPushButton(self)
                restart.clicked.connect(lambda _checked=False, tid=timer_id: self._restart_timer(tid))
                dismiss.clicked.connect(lambda _checked=False, tid=timer_id: self._dismiss_timer(tid))
                self._expired_buttons[timer_id] = (restart, dismiss)
            restart, dismiss = self._expired_buttons[timer_id]
            restart.setToolTip(f"Restart {timer.label} using the same duration")
            dismiss.setToolTip(f"Dismiss {timer.label}")
            for button, action in ((restart, "Restart"), (dismiss, "Dismiss")):
                button.setIcon(_action_icon(action, token("TEXT")))
                button.setFont(self._f_label)
                button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                button.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setAccessibleName(f"{action} {timer.label}")
                button.setStyleSheet(
                    f"QPushButton {{ color: {token('TEXT')}; background: rgba(255,255,255,0.06); "
                    f"border: 1px solid {token('LINE')}; border-radius: 4px; padding: 0; "
                    f"font-size: {self._f_label.pixelSize()}px; font-weight: 600; min-height: 0; }}"
                    "QPushButton:hover { background: rgba(255,255,255,0.13); }"
                    "QPushButton:pressed { background: rgba(255,255,255,0.20); }"
                )
                button.show()
        restart_w, dismiss_w, gap = self._button_widths()
        needed = math.ceil(16 + self._row_h() - 6 + 8 + self._px * 3 + 6 + restart_w + dismiss_w + gap)
        self.setMinimumWidth(needed + (14 if self._scroll.maximum() > 0 else 0) if expired else 0)

    def _position_controls(self) -> None:
        restart_w, dismiss_w, gap = self._button_widths()
        button_h = restart_w
        icon_side = max(10, button_h - 4)
        for index, timer in enumerate(self._visible_timers()):
            buttons = self._expired_buttons.get(timer.id)
            if buttons is None:
                continue
            row = self._row_rect(index)
            top = round(row.center().y() - button_h / 2)
            right = math.floor(row.right())
            buttons[0].setGeometry(right - dismiss_w - gap - restart_w, top, restart_w, button_h)
            buttons[1].setGeometry(right - dismiss_w, top, dismiss_w, button_h)
            for button in buttons:
                button.setIconSize(QSize(icon_side, icon_side))
        self._scroll.setGeometry(self.width() - 14, 5, 8,
                                 max(1, len(self._visible_timers()) * (self._row_h() + 4) - 4))

    def _restart_timer(self, timer_id: str) -> None:
        if self.runner is not None:
            self.runner.restart_timer(timer_id)

    def _dismiss_timer(self, timer_id: str) -> None:
        if self.runner is not None:
            self.runner.cancel_timer(timer_id)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_expired_buttons"):
            self._position_controls()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        if self._scroll.maximum() > 0 and event.angleDelta().y():
            step = -1 if event.angleDelta().y() > 0 else 1
            self._scroll.setValue(self._scroll.value() + step)
            event.accept()
            return
        super().wheelEvent(event)

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
        timers = self._visible_timers()
        self._rows = []
        row_h = self._row_h()
        if not timers and not self._popups:
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
            row = self._row_rect(i)
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
            controls = self._expired_buttons.get(t.id)
            reserved_w = time_w
            if controls is not None:
                restart_w, dismiss_w, gap = self._button_widths()
                reserved_w = restart_w + dismiss_w + gap
            else:
                p.setFont(self._f_time)
                p.setPen(color if (t.ended or left <= t.low_s) else qcolor(token("TEXT")))
                p.drawText(QRectF(row.right() - time_w, row.top(), time_w, row.height()),
                           int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight), format_remaining(left))
            p.setFont(self._f_label)
            p.setPen(qcolor(token("MUTED") if t.ended else token("TEXT")))
            label_rect = QRectF(text_left, row.top(), max(0, row.right() - reserved_w - text_left - 6), row.height())
            p.drawText(label_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm_label.elidedText(t.label, Qt.TextElideMode.ElideRight, label_rect.width()))
            # a thin progress line under the row (easier to read at a glance than the ring alone)
            line = QRectF(text_left, row.bottom() - 3, (row.right() - text_left) * frac, 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(color.name(), 0.55))
            p.drawRoundedRect(line, 1, 1)
        popup_now = time.monotonic()
        for i, popup in enumerate(self._popups, start=len(timers)):
            row = self._row_rect(i)
            p.save()
            p.setOpacity(popup.opacity(popup_now))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor(popup.color, 0.15))
            p.drawRoundedRect(row, 5, 5)
            p.setBrush(qcolor(popup.color))
            p.drawRoundedRect(QRectF(row.left() + 2, row.top() + 6, 3, row.height() - 12), 1.5, 1.5)
            p.setFont(self._f_label)
            p.setPen(qcolor(token("TEXT")))
            label_rect = row.adjusted(12, 0, -8, 0)
            p.drawText(label_rect, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                       fm_label.elidedText(popup.label, Qt.TextElideMode.ElideRight, label_rect.width()))
            p.restore()
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

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        self.set_runner(None)
        self._anim.stop()
        super().closeEvent(event)


__all__ = ["TimerPanel", "format_remaining"]
