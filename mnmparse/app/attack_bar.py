"""Auto-attack timer bar: a small window docked under the overlay (and its timer panel)
that can be pulled off it (see :mod:`mnmparse.app.docked_panel`).

The timing comes from :class:`mnmparse.swing.SwingTracker` (the viewer's own swings in the
Combat chat).  The bar fills at the learned delay and waits at full until the swing line
is read; only a read swing resets it.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QRectF, QSettings, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPainterPath, QPaintEvent, QPen
from PySide6.QtWidgets import QMenu

from mnmparse.app import probe
from mnmparse.app.docked_panel import DOCK_GAP, SNAP_DISTANCE, DockedPanel
from mnmparse.app.widgets import make_font, qcolor, token
from mnmparse.swing import HandState, SwingTracker

if TYPE_CHECKING:
    from mnmparse.app.overlay import OverlayWindow

SETTINGS_GROUP = "attack_bar"
FRAME_MS = 16  #: repaint interval while a bar is moving (about 60 frames a second)
WAIT_MS = 200  #: repaint interval while every bar waits at full (nothing moves)


class AttackBar(DockedPanel):
    """The auto-attack window (see module docstring)."""

    SETTINGS_GROUP = SETTINGS_GROUP
    TITLE = "PNUT M&M Auto Attack"

    def __init__(self, owner: "OverlayWindow", settings: QSettings, flags: Qt.WindowType) -> None:
        super().__init__(owner, settings, flags)
        self.tracker = SwingTracker(str(getattr(owner._cfg, "player_name", "") or ""))
        self._px = 13.0
        self._font = make_font(self._px * 0.85, weight=QFont.Weight.DemiBold)
        self._hands: list[HandState] = []
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # a coarse timer stutters at 16 ms
        self._timer.timeout.connect(self._tick)
        self.setToolTip(
            "Auto-attack timer, learned from your own swings in the Combat chat.\n"
            "Main hand, off hand and bow get their own bars. Unlock the overlay to drag this off it;\n"
            "double-click or right-click > Snap to overlay to dock it again."
        )
        self._resize_for(1)

    # -- public ----------------------------------------------------------------------------
    def observe(self, ev: Any) -> None:
        if self.tracker.observe(ev):
            self._tick()
            if self.isVisible():
                self._timer.setInterval(FRAME_MS)
                if not self._timer.isActive():
                    self._timer.start()

    def set_player_name(self, name: str) -> None:
        self.tracker.player_name = name or "You"

    def set_font_px(self, px: float) -> None:
        self._px = px
        self._font = make_font(px * 0.85, weight=QFont.Weight.DemiBold)
        self._resize_for(max(1, len(self._hands)))

    def sync(self) -> None:
        super().sync()
        if self.isVisible():
            self._timer.start()

    def add_menu_entries(self, menu: QMenu, event: Any) -> dict[Any, Any]:
        hide = menu.addAction("Hide auto-attack bar")
        # Settings > Overlay turns it back on
        return {hide: lambda: self._owner.set_attack_bar_enabled(False)}

    # -- timing ----------------------------------------------------------------------------
    def _tick(self) -> None:
        now = time.time()
        hands = self.tracker.hands(now)
        if len(hands) != len(self._hands):
            self._resize_for(max(1, len(hands)))
        self._hands = hands
        if not hands or all(h.idle(now) for h in hands):
            self._timer.stop()  # nothing moving: repaint once, wake on the next swing
            probe.pause("attack bar")
        elif all(h.due(now) for h in hands):
            self._timer.setInterval(WAIT_MS)  # full bars wait for their swing line: nothing moves
            probe.pause("attack bar")
        else:
            self._timer.setInterval(FRAME_MS)
            probe.frame("attack bar")
        self.update()

    def _resize_for(self, rows: int) -> None:
        row_h = int(round(self._px * 1.45))
        self.set_rows_height(8 + rows * row_h + (rows - 1) * 3)

    # -- painting --------------------------------------------------------------------------
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
        p.setFont(self._font)
        fm = QFontMetricsF(self._font)
        now = time.time()
        hands = self._hands or []
        rows = max(1, len(hands))
        row_h = (r.height() - 8 - (rows - 1) * 3) / rows
        label_w = max(fm.horizontalAdvance("off hand 9.9s"), 60.0) + 8
        for i in range(rows):
            top = r.top() + 4 + i * (row_h + 3)
            row = QRectF(r.left() + 8, top, r.width() - 16, row_h)
            hand = hands[i] if i < len(hands) else None
            p.setPen(qcolor(token("MUTED")))
            if hand is None:
                p.drawText(row, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                           "Auto attack: waiting for your swings")
                continue
            delay = f" {hand.delay:.1f}s" if hand.delay else ""
            p.drawText(QRectF(row.left(), row.top(), label_w, row.height()),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), f"{hand.label}{delay}")
            track = QRectF(row.left() + label_w, row.center().y() - row.height() * 0.28,
                           row.width() - label_w, row.height() * 0.56)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor("#ffffff", 0.08))
            p.drawRoundedRect(track, track.height() / 2, track.height() / 2)
            progress = hand.progress(now)
            if progress is None or hand.idle(now):
                continue
            due = progress >= 0.85  # the swing is about to land (or waits for its line at full)
            fill = QRectF(track.left(), track.top(), track.width() * min(1.0, max(0.0, progress)), track.height())
            p.setBrush(qcolor(token("SUCCESS") if due else token("ACCENT"), 0.95))
            p.drawRoundedRect(fill, track.height() / 2, track.height() / 2)
        p.end()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self._tick()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        super().hideEvent(event)
        self._timer.stop()


__all__ = ["AttackBar", "DOCK_GAP", "SNAP_DISTANCE"]
