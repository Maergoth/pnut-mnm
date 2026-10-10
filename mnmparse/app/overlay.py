"""The translucent always-on-top DPS overlay (APP_SPEC section 8).

The overlay is **our own** frameless, translucent, topmost tool window and
nothing more (APP_SPEC section 1): the game window is never touched.  *Locked*
means it cannot be moved or resized; tabs, column sorting, the header toggle
and hover breakdowns keep working.  A separate *click-through* switch applies
``Qt.WindowTransparentForInput`` so the window ignores the mouse entirely.  The
window is shown with ``WA_ShowWithoutActivating`` and never activated or raised
afterwards.

Layout::

    +------------------------------------------------------------+
    | Encounter label            [ended]  00:42  12,345 dmg  293 dps |
    | Damage | Healing | Taken | Feed                             |
    | 1  You            4,120  98.1   41.2%  83%   120  [bar]     |
    | ...                                                         |
    +--------------------------------------------------------[grip]+

Unlocked: a hover toolbar (lock, opacity, font -/+, hide) floats at the
top-right, the header can be dragged to move the window and a size grip sits
bottom-right.  Locked: click-through, no chrome.  Geometry, opacity, tab,
sort, font scale and locked state persist in ``QSettings`` under the
``overlay`` group.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QHideEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
    QCloseEvent,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QDialog,
    QHBoxLayout,
    QMenu,
    QSizeGrip,
    QStackedWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from mnmparse.app.attack_bar import AttackBar
from mnmparse.app.timer_panel import TimerPanel
from mnmparse.app.respawn_timer_dialog import DEFAULT_RESPAWN_SECONDS, MAX_RESPAWN_SECONDS, RespawnTimerDialog
from mnmparse.app.window_identity import window_title
from mnmparse.app.session_view import SessionView
from mnmparse.app.widgets import (
    ElidedLabel,
    FeedView,
    MeterTable,
    SliderRow,
    WarningLatch,
    _cell_tooltip,
    capture_warning,
    fmt_int,
    fmt_mmss,
    fmt_rate,
    make_font,
    qcolor,
    token,
)

log = logging.getLogger(__name__)

try:  # models.py is written concurrently; the fallback mirrors APP_SPEC section 5
    from mnmparse.app.models import owner_row
    from mnmparse.app.models import self_rows as _self_rows
    from mnmparse.app.models import snapshot_rows_for_tab as _rows_for_tab
except Exception:  # pragma: no cover - only while models.py does not exist yet

    def _self_rows(snap: Any, tab: str, player_name: str) -> list[Any]:
        return []

    def _rows_for_tab(snap: Any, tab: str) -> list[Any]:
        rows = list(getattr(snap, "rows", []) or [])
        if tab == "overview":
            return rows
        if tab == "healing":
            return [r for r in rows if getattr(r, "heals", 0) > 0]
        if tab == "taken":
            return [r for r in rows if getattr(r, "taken", 0) > 0]
        return [r for r in rows if getattr(r, "damage", 0) > 0 or getattr(r, "swings", 0) > 0]


TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("damage", "Damage"),
    ("healing", "Healing"),
    ("taken", "Taken"),
    ("session", "Session"),
    ("feed", "Feed"),
)
"""``(key, title)`` of the tab strip, in order."""

DEFAULT_GEOMETRY = QRect(1880, 60, 560, 330)
MIN_SIZE = QSize(280, 140)
SETTINGS_GROUP = "overlay"
OVERLAY_BASE_PX = 13.0
FEED_LINES = 12
SNAPSHOT_THROTTLE_MS = 100  # ~10 Hz
HISTORY_LIMIT = 400  #: closed encounters kept for the encounter dropdown and zone summaries
MENU_ENCOUNTERS = 30  #: encounters listed in the dropdown
OPACITY_RANGE = (0.2, 1.0)
FONT_SCALE_RANGE = (0.6, 2.0)
FONT_SCALE_STEP = 0.1

# The flags APP_SPEC section 1 lists, plus WindowDoesNotAcceptFocus: Qt then creates the
# native window with WS_EX_NOACTIVATE, so a click on the UNLOCKED overlay (drag, sort,
# toolbar) never makes it the foreground window and the game keeps keyboard focus
# (APP_SPEC section 11: GetForegroundWindow() is never our overlay).  Mouse input still
# reaches our widgets; nothing is ever sent to the game.
OVERLAY_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool
    | Qt.WindowType.WindowDoesNotAcceptFocus
)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def clamp_to_screen(rect: QRect) -> QRect:
    """Return ``rect`` moved/shrunk so it lies on the screen that contains its centre (or the primary)."""
    screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
    if screen is None:
        return QRect(rect)
    area = screen.geometry()
    w = min(max(rect.width(), MIN_SIZE.width()), area.width())
    h = min(max(rect.height(), MIN_SIZE.height()), area.height())
    x = int(_clamp(rect.x(), area.left(), area.right() - w + 1))
    y = int(_clamp(rect.y(), area.top(), area.bottom() - h + 1))
    return QRect(x, y, w, h)


# --------------------------------------------------------------------------- small painted buttons
class _IconButton(QAbstractButton):
    """Flat painted toolbar button; ``kind`` in ``lock | minus | plus | close``."""

    def __init__(self, kind: str, tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(24, 22)
        self._hover = False

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        if self._hover or self.isDown():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(qcolor("#ffffff", 0.12 if self.isDown() else 0.08))
            p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 6, 6)
        color = qcolor(token("DANGER" if self.kind == "close" else "ACCENT" if self.kind == "lock" else "TEXT"))
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        c = r.center()
        if self.kind == "lock":
            body = QRectF(c.x() - 5, c.y() - 0.5, 10, 7)
            p.drawRoundedRect(body, 1.5, 1.5)
            shackle = QRectF(c.x() - 3.5, c.y() - 6.5, 7, 7)
            p.drawArc(shackle, 0 * 16, 180 * 16)
            p.setBrush(color)
            p.drawEllipse(QPointF(c.x(), c.y() + 3), 1.1, 1.1)
        elif self.kind == "close":
            p.drawLine(QPointF(c.x() - 4, c.y() - 4), QPointF(c.x() + 4, c.y() + 4))
            p.drawLine(QPointF(c.x() - 4, c.y() + 4), QPointF(c.x() + 4, c.y() - 4))
        else:
            p.setFont(make_font(12, weight=QFont.Weight.DemiBold))
            p.drawText(r, int(Qt.AlignmentFlag.AlignCenter), "A−" if self.kind == "minus" else "A+")
        p.end()


def add_group_entries(menu: QMenu, row: Any, emit: Any) -> dict[Any, Any]:
    """Right-click entries that count a person in or out of the group (or follow the chat
    again).  ``emit(name, state)`` is called with the choice; returns {action: handler}."""
    if row is None or getattr(row, "is_you", False) or getattr(row, "is_npc", False) or getattr(row, "is_enemy", False):
        return {}
    name = str(getattr(row, "name", "") or "")
    if not name:
        return {}
    menu.addSeparator()
    handlers: dict[Any, Any] = {}
    if getattr(row, "in_group", True):
        out = menu.addAction(f"Don't count {name} as my group")
        handlers[out] = lambda: emit(name, False)
    else:
        add = menu.addAction(f"Count {name} as my group")
        handlers[add] = lambda: emit(name, True)
    auto = menu.addAction(f"Let the chat decide for {name}")
    handlers[auto] = lambda: emit(name, None)
    return handlers


def add_pet_entries(menu: QMenu, row: Any, snap: Any, emit: Any) -> dict[Any, Any]:
    """Assign a combatant, or manage each pet already included in an owner's row."""
    if row is None or snap is None:
        return {}
    name = str(getattr(row, "name", "") or "")
    if not name:
        return {}
    pets = list(dict.fromkeys(getattr(row, "attributed_pets", ()) or ()))
    if getattr(row, "is_you", False) and not pets:
        return {}
    members = set(getattr(snap, "group_members", ()) or ())
    members.update(r.name for r in getattr(snap, "rows", ()) if
                   getattr(r, "in_group", True) and not any(getattr(r, k, False) for k in ("is_npc", "is_enemy", "is_pet")))
    menu.addSeparator()
    handlers: dict[Any, Any] = {}

    def choices(submenu: QMenu, pet: str, owner: str) -> None:
        eligible = members - {pet}
        if owner:
            eligible.add(owner)
        for member in sorted(eligible, key=str.casefold):
            action = submenu.addAction(member)
            action.setCheckable(True)
            action.setChecked(member == owner)
            handlers[action] = lambda pet=pet, member=member: emit(pet, member)
        if not eligible:
            submenu.addAction("No group members known yet").setEnabled(False)
        if owner:
            submenu.addSeparator()
            clear = submenu.addAction("Clear pet assignment")
            handlers[clear] = lambda pet=pet: emit(pet, None)

    if pets:
        submenu = menu.addMenu("Manage included pets")
        for pet in pets:
            choices(submenu.addMenu(pet), pet, name)
    else:
        owner = str(getattr(row, "pet_owner", "") or "")
        choices(menu.addMenu("Assign pet to group member"), name, owner)
    return handlers


COPY_TIP = "Copy this fight to the clipboard (one line; the format is in Settings > Export)"
COPIED_FLASH_S = 1.4


class _Toolbar(QWidget):
    """Hover toolbar (unlocked only): lock, opacity slider, font -/+, hide."""

    def __init__(self, owner: "OverlayWindow") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.lock_btn = _IconButton("lock", "Lock overlay (click-through)", self)
        self.opacity = SliderRow("Opacity", 20, 100, 85, formatter=lambda v: f"{v}%", compact=True, parent=self)
        self.minus_btn = _IconButton("minus", "Smaller text", self)
        self.plus_btn = _IconButton("plus", "Larger text", self)
        self.close_btn = _IconButton("close", "Hide overlay", self)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 6, 4)
        lay.setSpacing(4)
        lay.addWidget(self.lock_btn)
        lay.addSpacing(4)
        lay.addWidget(self.opacity)
        lay.addSpacing(4)
        lay.addWidget(self.minus_btn)
        lay.addWidget(self.plus_btn)
        lay.addSpacing(2)
        lay.addWidget(self.close_btn)
        self.adjustSize()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(qcolor("#ffffff", 0.14), 1))
        p.setBrush(qcolor(token("BG1"), 0.97))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)
        p.end()


class _Grip(QSizeGrip):
    """Size grip painted as three small dots (bottom-right, unlocked only)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeFDiagCursor)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(qcolor(token("MUTED"), 0.9))
        for dx, dy in ((10, 10), (10, 6), (6, 10), (10, 2), (6, 6), (2, 10)):
            p.drawEllipse(QRectF(dx, dy, 2.4, 2.4))
        p.end()


# --------------------------------------------------------------------------- header + tabs
class _HeaderBar(QWidget):
    """Painted header: encounter label, 'ended' pill, duration, total damage, raid dps.

    Dragging it moves the overlay window (unlocked only); a plain click toggles the
    self / group view.
    """

    def __init__(self, owner: "OverlayWindow") -> None:
        super().__init__(owner)
        self._owner = owner
        self._drag_offset: QPointF | None = None
        self._press_pos: QPointF | None = None
        self._dragged = False
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.label = ""
        self.duration = 0.0
        self.total = 0
        self.raid_dps = 0.0
        self.ended = False
        self.has_data = False
        self._reserved_right = 0  # width covered by the hover toolbar
        self._label_rect = QRectF()  # where the encounter name (the dropdown) is drawn
        self._copy_rect = QRectF()  # the copy-to-clipboard glyph (right edge)
        self._copy_hover = False
        self._press_on_copy = False
        self._mode_rect = QRectF()  # the solo / group glyph (left of the copy glyph)
        self._mode_hover = False
        self._press_on_mode = False
        self._base_tip = ""  # the header's own tooltip (the glyphs have theirs)
        self._copied_until = 0.0  # a check mark replaces the glyph until then (monotonic)
        self.set_font_px(OVERLAY_BASE_PX)

    def set_font_px(self, px: float) -> None:
        self._px = px
        self._f_label = make_font(px * 1.05, weight=QFont.Weight.DemiBold)
        self._f_num = make_font(px, tabular=True)
        self._f_num_bold = make_font(px * 1.05, weight=QFont.Weight.DemiBold, tabular=True)
        self._f_cap = make_font(px * 0.85)
        self._f_pill = make_font(px * 0.8, weight=QFont.Weight.DemiBold)
        self.setFixedHeight(int(round(px * 2.2)))
        self.update()

    def set_reserved_right(self, px: int) -> None:
        """Keep the right ``px`` free (the hover toolbar covers it); stats hide meanwhile."""
        if px != self._reserved_right:
            self._reserved_right = max(0, int(px))
            self.update()

    def set_data(self, *, label: str, duration: float, total: int, raid_dps: float, ended: bool, has_data: bool) -> None:
        self.label, self.duration, self.total, self.raid_dps = label, duration, total, raid_dps
        self.ended, self.has_data = ended, has_data
        self.update()

    def set_base_tip(self, tip: str) -> None:
        """The tooltip for the header itself (the copy and solo/group glyphs show their own)."""
        self._base_tip = tip

    def _mode_tip(self) -> str:
        if self._owner.view_mode == "self":
            return "Showing just you (your own damage, healing and taken). Click to show your group."
        return "Showing your group. Click to show just you (your own damage, healing and taken)."

    def event(self, e: QEvent) -> bool:  # noqa: D102 - Qt override
        if e.type() == QEvent.Type.ToolTip:
            pos = QPointF(e.pos())
            tip = COPY_TIP if self._on_copy(pos) else self._mode_tip() if self._on_mode(pos) else self._base_tip
            if tip:
                QToolTip.showText(e.globalPos(), tip, self)
            else:
                QToolTip.hideText()
                e.ignore()
            return True
        return super().event(e)

    def _on_mode(self, pos: QPointF) -> bool:
        return self.has_data and not self._mode_rect.isEmpty() and self._mode_rect.contains(pos)

    def flash_copied(self) -> None:
        """Show a check mark in place of the copy glyph for a moment."""
        self._copied_until = time.monotonic() + COPIED_FLASH_S
        self.update()
        QTimer.singleShot(int(COPIED_FLASH_S * 1000) + 50, self.update)

    def _on_copy(self, pos: QPointF) -> bool:
        return self.has_data and not self._copy_rect.isEmpty() and self._copy_rect.contains(pos)

    # drag to move ------------------------------------------------------
    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._on_copy(event.position()):
            self._press_on_copy = True
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._on_mode(event.position()):
            self._press_on_mode = True
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.globalPosition()
            self._dragged = False
            self._drag_offset = (
                None if self._owner.locked
                else event.globalPosition() - QPointF(self._owner.frameGeometry().topLeft())
            )
            event.accept()
            return
        super().mousePressEvent(event)

    def update_cursor(self, pos: QPointF | None = None) -> None:
        """Hand over the encounter name (a dropdown) and the copy glyph; move cursor only while unlocked."""
        if pos is not None and (self._label_rect.contains(pos) or self._on_copy(pos) or self._on_mode(pos)):
            shape = Qt.CursorShape.PointingHandCursor
        elif self._owner.locked:
            shape = Qt.CursorShape.ArrowCursor
        else:
            shape = Qt.CursorShape.SizeAllCursor
        if self.cursor().shape() != shape:
            self.setCursor(shape)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._press_pos is not None and (event.globalPosition() - self._press_pos).manhattanLength() > 4:
                self._dragged = True
            if self._dragged:
                target = (event.globalPosition() - self._drag_offset).toPoint()
                self._owner.move(target)  # our own window only
            event.accept()
            return
        copy_hover, mode_hover = self._on_copy(event.position()), self._on_mode(event.position())
        if (copy_hover, mode_hover) != (self._copy_hover, self._mode_hover):
            self._copy_hover, self._mode_hover = copy_hover, mode_hover
            self.update()
        self.update_cursor(event.position())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if self._copy_hover or self._mode_hover:
            self._copy_hover = self._mode_hover = False
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press_on_copy and event.button() == Qt.MouseButton.LeftButton:
            self._press_on_copy = False
            if self._on_copy(event.position()):
                self._owner.copy_current()
            event.accept()
            return
        if self._press_on_mode and event.button() == Qt.MouseButton.LeftButton:
            self._press_on_mode = False
            if self._on_mode(event.position()):
                self._owner.toggle_view_mode()
            event.accept()
            return
        if self._press_pos is not None and event.button() == Qt.MouseButton.LeftButton:
            if self._dragged:
                self._owner.persist_geometry()
            elif self._label_rect.contains(event.position()):
                self._owner.open_encounter_menu(self.mapToGlobal(QPoint(int(self._label_rect.left()), self.height())))
            else:
                self._owner.toggle_view_mode()
            self._press_pos = None
            self._drag_offset = None
            self._dragged = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # painting ----------------------------------------------------------
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        r = QRectF(self.rect())
        text = qcolor(token("TEXT"))
        muted = qcolor(token("MUTED"))
        accent = qcolor(token("ACCENT"))
        vcenter = int(Qt.AlignmentFlag.AlignVCenter)
        right = r.right() - 2 - self._reserved_right

        self._copy_rect = QRectF()
        self._mode_rect = QRectF()
        glyphs_w = 2 * (self._px * 0.95 + self._px * 0.7)
        min_label = self._px * 3.5  # an elided name plus its chevron stays clickable
        if self.has_data and right - glyphs_w >= r.left() + 2 + min_label:
            right = self._draw_copy(p, right, r)
            right = self._draw_mode(p, right, r)
        if not self.has_data or self._reserved_right:
            # no data, or the toolbar covers the stats: label only
            p.setFont(self._f_label)
            p.setPen(muted if (not self.has_data or self.ended) else text)
            fm = QFontMetricsF(self._f_label)
            label_rect = QRectF(r.left() + 2, r.top(), max(10.0, right - r.left() - 2), r.height())
            txt = self.label if self.has_data else "Waiting for combat…"
            self._draw_label(p, fm, label_rect, txt, muted if (not self.has_data or self.ended) else text)
            p.end()
            return

        # stats block, laid out right-to-left: [dur]  [total dmg]  [dps DPS]
        gap = self._px * 0.9

        def draw_right(txt: str, font: QFont, color: QColor) -> None:
            nonlocal right
            fm = QFontMetricsF(font)
            w = fm.horizontalAdvance(txt)
            p.setFont(font)
            p.setPen(color)
            p.drawText(QRectF(right - w, r.top(), w + 1, r.height()), vcenter | int(Qt.AlignmentFlag.AlignLeft), txt)
            right -= w

        dim = self.ended
        draw_right("dps", self._f_cap, muted)
        right -= 3
        draw_right(fmt_rate(self.raid_dps), self._f_num_bold, qcolor(token("MUTED")) if dim else accent)
        right -= gap
        draw_right("dmg", self._f_cap, muted)
        right -= 3
        draw_right(fmt_int(self.total), self._f_num, muted if dim else text)
        right -= gap
        draw_right(fmt_mmss(self.duration), self._f_num, muted)
        right -= gap

        if self.ended:
            pill_fm = QFontMetricsF(self._f_pill)
            pw = pill_fm.horizontalAdvance("ended") + 12
            ph = pill_fm.height() + 4
            pill = QRectF(right - pw, r.center().y() - ph / 2, pw, ph)
            p.setPen(QPen(qcolor("#ffffff", 0.14), 1))
            p.setBrush(qcolor("#ffffff", 0.06))
            p.drawRoundedRect(pill, ph / 2, ph / 2)
            p.setFont(self._f_pill)
            p.setPen(muted)
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), "ended")
            right -= pw + gap * 0.8

        # encounter label fills what is left on the left
        p.setFont(self._f_label)
        p.setPen(muted if dim else text)
        fm = QFontMetricsF(self._f_label)
        label_rect = QRectF(r.left() + 2, r.top(), max(10.0, right - r.left() - 2), r.height())
        self._draw_label(p, fm, label_rect, self.label, muted if dim else text)
        p.end()

    def _draw_copy(self, p: QPainter, right: float, r: QRectF) -> float:
        """The copy-to-clipboard glyph (two overlapping sheets; a check mark just after a
        copy) ending at ``right``; returns the right edge left for the rest."""
        size = self._px * 0.95
        box = QRectF(right - size, r.center().y() - size / 2, size, size)
        self._copy_rect = box.adjusted(-4, -4, 4, 4)  # a little easier to hit
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        if time.monotonic() < self._copied_until:
            pen = QPen(qcolor(token("SUCCESS")), max(1.6, size * 0.14))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPolyline([
                QPointF(box.left() + size * 0.12, box.top() + size * 0.55),
                QPointF(box.left() + size * 0.40, box.top() + size * 0.82),
                QPointF(box.left() + size * 0.90, box.top() + size * 0.20),
            ])
        else:
            color = qcolor(token("TEXT")) if self._copy_hover else qcolor(token("MUTED"), 0.85)
            p.setPen(QPen(color, max(1.1, size * 0.09)))
            w = size * 0.62
            radius = size * 0.12
            p.drawRoundedRect(QRectF(box.left() + size * 0.30, box.top(), w, w * 1.15), radius, radius)
            p.setBrush(qcolor(token("BG0"), 0.95))
            p.drawRoundedRect(QRectF(box.left() + size * 0.06, box.top() + size * 0.24, w, w * 1.15), radius, radius)
        p.restore()
        return box.left() - self._px * 0.7

    def _draw_mode(self, p: QPainter, right: float, r: QRectF) -> float:
        """The solo / group glyph ending at ``right`` (one person = just you, two = your
        group); returns the right edge left for the rest."""
        size = self._px * 0.95
        box = QRectF(right - size, r.center().y() - size / 2, size, size)
        self._mode_rect = box.adjusted(-4, -4, 4, 4)
        color = qcolor(token("TEXT")) if self._mode_hover else qcolor(token("MUTED"), 0.85)
        p.save()
        p.setPen(Qt.PenStyle.NoPen)

        def person(cx: float, top: float, scale: float, c: QColor) -> None:
            head = size * 0.20 * scale
            p.setBrush(c)
            p.drawEllipse(QPointF(cx, top + head), head, head)
            body = QRectF(cx - size * 0.34 * scale, top + head * 2.3, size * 0.68 * scale, size * 0.62 * scale)
            p.drawChord(body, 0, 180 * 16)

        if self._owner.view_mode == "self":
            person(box.center().x(), box.top() + size * 0.08, 1.0, color)
        else:
            back = QColor(color)
            back.setAlphaF(color.alphaF() * 0.6)
            person(box.left() + size * 0.66, box.top() + size * 0.02, 0.82, back)
            person(box.left() + size * 0.36, box.top() + size * 0.14, 0.9, color)
        p.restore()
        return box.left() - self._px * 0.7

    def _draw_label(self, p: QPainter, fm: QFontMetricsF, rect: QRectF, txt: str, color: QColor) -> None:
        """The encounter name with a small chevron: a click on it opens the encounter list."""
        chevron_w = self._px * 0.9
        avail = max(10.0, rect.width() - chevron_w - 4)
        elided = fm.elidedText(txt, Qt.TextElideMode.ElideRight, avail)
        p.setPen(color)
        p.drawText(QRectF(rect.left(), rect.top(), avail, rect.height()), int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), elided)
        x = rect.left() + fm.horizontalAdvance(elided) + 5
        cy = rect.center().y()
        pen = QPen(qcolor(token("MUTED")), 1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        w = chevron_w * 0.45
        p.drawPolyline([QPointF(x, cy - w / 2), QPointF(x + w, cy + w / 2), QPointF(x + 2 * w, cy - w / 2)])
        self._label_rect = QRectF(rect.left(), rect.top(), x + 2 * w + 4 - rect.left(), rect.height())


class _WarningPill(ElidedLabel):
    """A one-line capture warning under the overlay header; a click dismisses it."""

    def __init__(self, owner: "OverlayWindow") -> None:
        super().__init__("", owner, min_width=40)
        self._owner = owner
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("")
        self.hide()

    def set_font_px(self, px: float) -> None:
        self.setFont(make_font(px * 0.82, weight=QFont.Weight.DemiBold))
        accent = token("ACCENT")
        self.setStyleSheet(
            f"color: {accent}; background: {qcolor(accent, 0.12).name(QColor.NameFormat.HexArgb)}; "
            "border-radius: 6px; padding: 1px 8px;"
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._owner.dismiss_warning()
            event.accept()
            return
        super().mousePressEvent(event)


class _TabStrip(QWidget):
    """Thin painted tab strip; emits ``tab_clicked(key)``."""

    tab_clicked = Signal(str)

    def __init__(self, owner: "OverlayWindow") -> None:
        super().__init__(owner)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._current = "damage"
        self._hover = -1
        self._rects: list[QRectF] = []
        self.set_font_px(OVERLAY_BASE_PX)

    MIN_PAD = 4.0  #: smallest padding either side of a tab title (px)

    def set_font_px(self, px: float) -> None:
        self._px = px
        self._font = make_font(px * 0.92, weight=QFont.Weight.DemiBold)
        self.setFixedHeight(int(round(px * 1.85)))
        self.update()

    def set_current(self, key: str) -> None:
        self._current = key
        self.update()

    def needed_width(self) -> int:
        """Width at which every tab title still has :attr:`MIN_PAD` either side."""
        fm = QFontMetricsF(self._font)
        text = sum(fm.horizontalAdvance(title) for _key, title in TABS)
        return int(math.ceil(text + 2 * self.MIN_PAD * len(TABS)))

    def _layout_tabs(self) -> None:
        """Tab rectangles; the padding shrinks (down to :attr:`MIN_PAD`) before anything clips."""
        fm = QFontMetricsF(self._font)
        widths = [fm.horizontalAdvance(title) for _key, title in TABS]
        pad = self._px * 0.95
        available = float(self.width())
        if available > 0 and sum(widths) + 2 * pad * len(TABS) > available:
            pad = max(self.MIN_PAD, (available - sum(widths)) / (2 * len(TABS)))
        x = 0.0
        self._rects = []
        for w in widths:
            self._rects.append(QRectF(x, 0, w + 2 * pad, self.height()))
            x += w + 2 * pad

    def _index_at(self, pos: QPointF) -> int:
        for i, rc in enumerate(self._rects):
            if rc.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        i = self._index_at(event.position())
        if i != self._hover:
            self._hover = i
            self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            i = self._index_at(event.position())
            if i >= 0:
                self.tab_clicked.emit(TABS[i][0])
                event.accept()
                return
        super().mousePressEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        self._layout_tabs()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setFont(self._font)
        text = qcolor(token("TEXT"))
        muted = qcolor(token("MUTED"))
        accent = qcolor(token("ACCENT"))
        h = self.height()
        for i, ((key, title), rc) in enumerate(zip(TABS, self._rects)):
            active = key == self._current
            if i == self._hover and not active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(qcolor("#ffffff", 0.06))
                p.drawRoundedRect(rc.adjusted(2, 2, -2, -3), 6, 6)
            p.setPen(text if active else muted)
            p.drawText(rc, int(Qt.AlignmentFlag.AlignCenter), title)
            if active:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(accent)
                p.drawRoundedRect(QRectF(rc.left() + 8, h - 2.5, rc.width() - 16, 2.0), 1.0, 1.0)
        # baseline hairline
        p.setPen(QPen(qcolor(token("LINE")), 1))
        p.drawLine(QPointF(0, h - 0.5), QPointF(self.width(), h - 0.5))
        p.end()


# --------------------------------------------------------------------------- the overlay window
class OverlayWindow(QWidget):
    """Translucent always-on-top meter overlay (APP_SPEC section 8).

    Args:
        settings: the app's ``QSettings``; keys are stored under the ``overlay`` group.
        cfg: the loaded :class:`~mnmparse.config.Config` (overlay defaults are read via
            ``getattr`` so an older Config still works).
        parent: unused; the overlay is always a top-level window.

    Signals:
        locked_changed(bool): the click-through state changed (toolbar or :meth:`set_locked`).
        visibility_changed(bool): the window was shown or hidden.
        tab_changed(str): the current tab key changed (``damage``/``healing``/``taken``/``feed``).
    """

    locked_changed = Signal(bool)
    click_through_changed = Signal(bool)
    view_mode_changed = Signal(str)
    visibility_changed = Signal(bool)
    appearance_changed = Signal(float, float)  #: (opacity, font scale) changed
    attack_bar_changed = Signal(bool)  #: the auto-attack bar was switched on or off
    tab_changed = Signal(str)
    #: Copy the shown fight to the clipboard (the header's copy glyph or the right-click menu):
    #: the EncounterSnapshot; the app formats it (mnmparse.export) and calls flash_copied().
    copy_requested = Signal(object)
    #: Right-click a person > count them in / out of the group: ``(name, True | False | None)``
    #: (None: follow the chat again).  The app hands it to the engine's party roster.
    group_override_requested = Signal(str, object)
    pet_owner_requested = Signal(str, object)

    def __init__(self, settings: QSettings, cfg: Any, parent: QWidget | None = None) -> None:
        super().__init__(None)
        self._settings = settings
        self._cfg = cfg
        self.setObjectName("OverlayWindow")
        self.setWindowTitle(window_title("overlay"))
        self.setWindowFlags(OVERLAY_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # The overlay never becomes the active window (the game keeps focus), and Qt only
        # shows tooltips on active windows unless this is set: without it no hover works.
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setMouseTracking(True)
        self.setMinimumSize(MIN_SIZE)

        # state --------------------------------------------------------
        self._history: dict[str, Any] = {}  #: closed encounters by key, oldest first
        self._pinned: Any | None = None  #: an earlier encounter picked in the dropdown
        self._pin_live_key: str | None = None  #: the fight that was open when pinning (it does not unpin)
        self._live: Any | None = None  #: the newest snapshot from the engine
        self._snap: Any | None = None
        self._pending: Any | None = None
        self._pending_set = False
        self._ended = False
        self._locked = True
        self._click_through = False
        self._view_mode = "group"
        self._opacity = 0.85
        self._font_scale = 1.0
        self._tab = "damage"
        self._show_others = bool(getattr(cfg, "show_other_groups", False))
        self._suppress_visibility = False
        self._hovering = False

        # children -----------------------------------------------------
        self._header = _HeaderBar(self)
        self._warning = _WarningPill(self)
        self._warning_latch = WarningLatch()
        self._tabs = _TabStrip(self)
        self._stack = QStackedWidget(self)
        self._stack.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._stack.setAutoFillBackground(False)
        self._table = MeterTable(compact=True, parent=self._stack)
        self._feed = FeedView(max_lines=FEED_LINES, compact=True, parent=self._stack)
        self._session = SessionView(compact=True, parent=self._stack)
        self._stack.addWidget(self._table)
        self._stack.addWidget(self._feed)
        self._stack.addWidget(self._session)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 4, 10, 8)
        root.setSpacing(2)
        root.addWidget(self._header)
        root.addWidget(self._warning)
        root.addWidget(self._tabs)
        root.addWidget(self._stack, 1)

        # chrome: created last so it sits above the body in z-order
        self._grip = _Grip(self)
        self._toolbar = _Toolbar(self)
        self._toolbar.hide()
        self._grip.hide()

        # wiring -------------------------------------------------------
        self._tabs.tab_clicked.connect(self.set_tab)
        self._table.sort_changed.connect(self._on_sort_changed)
        self._toolbar.lock_btn.clicked.connect(lambda: self.set_locked(True))
        self._toolbar.close_btn.clicked.connect(self.hide)
        self._toolbar.minus_btn.clicked.connect(lambda: self.set_font_scale(self._font_scale - FONT_SCALE_STEP))
        self._toolbar.plus_btn.clicked.connect(lambda: self.set_font_scale(self._font_scale + FONT_SCALE_STEP))
        self._toolbar.opacity.value_changed.connect(lambda v: self.set_opacity(v / 100.0))

        self._throttle = QTimer(self)
        self._throttle.setSingleShot(True)
        self._throttle.setInterval(SNAPSHOT_THROTTLE_MS)
        self._throttle.timeout.connect(self._flush_snapshot)

        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.setInterval(400)
        self._geometry_timer.timeout.connect(self.persist_geometry)

        self._table.set_name_tooltip_provider(self._name_tooltip)
        for child in self.findChildren(QWidget):
            child.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self._timer_panel = TimerPanel(self, settings, OVERLAY_FLAGS)  # docked first: under the overlay
        self._attack_bar = AttackBar(self, settings, OVERLAY_FLAGS)  # then under the timer panel
        self._attack_bar.enabled = bool(
            settings.value("attack_bar/enabled", bool(getattr(cfg, "attack_bar", True)), type=bool)
        )

        self._load_settings()
        self._header.update_cursor()
        self._attack_bar.update_cursor()
        self._timer_panel.update_cursor()
        self._apply_fonts()
        self._apply_stylesheet()
        self._apply_input_flag()
        self._refresh_header()
        log.info(
            "overlay ready: geometry=%s locked=%s click_through=%s opacity=%.2f font=%.2f tab=%s view=%s",
            self.geometry(), self._locked, self._click_through, self._opacity, self._font_scale, self._tab, self._view_mode,
        )

    # ------------------------------------------------------------------ public API
    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def click_through(self) -> bool:
        return self._click_through

    @property
    def view_mode(self) -> str:
        """``"group"`` (actor rows) or ``"self"`` (the viewer's own sources)."""
        return self._view_mode

    def set_click_through(self, enabled: bool) -> None:
        """Ignore the mouse entirely (``Qt.WindowTransparentForInput``); no tabs, no tooltips."""
        enabled = bool(enabled)
        if enabled == self._click_through:
            return
        self._click_through = enabled
        self._apply_input_flag()
        self._settings.setValue(f"{SETTINGS_GROUP}/click_through", enabled)
        log.info("overlay click-through %s", "on" if enabled else "off")
        self.click_through_changed.emit(enabled)

    def set_view_mode(self, mode: str) -> None:
        """Switch between the group table and the viewer's own sources."""
        mode = "self" if mode == "self" else "group"
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._settings.setValue(f"{SETTINGS_GROUP}/view_mode", mode)
        self._session.set_view_mode(mode, str(getattr(self._cfg, "player_name", "") or ""))
        self._refresh_header()
        self._refresh_rows()
        self.view_mode_changed.emit(mode)

    def toggle_view_mode(self) -> None:
        self.set_view_mode("self" if self._view_mode == "group" else "group")

    @property
    def opacity(self) -> float:
        return self._opacity

    @property
    def font_scale(self) -> float:
        return self._font_scale

    @property
    def tab(self) -> str:
        return self._tab

    @property
    def meter(self) -> MeterTable:
        return self._table

    def set_show_other_groups(self, show: bool) -> None:
        """Whether fights nobody in the party took part in replace the meter."""
        show = bool(show)
        if show == self._show_others:
            return
        self._show_others = show
        self._refresh_filtered_selection()

    def _listed(self, snap: Any | None) -> bool:
        return snap is not None and (self._show_others or bool(getattr(snap, "ours", True)))

    def _refresh_filtered_selection(self) -> None:
        """Reconcile live, pinned and summary views immediately after filtering changes."""
        self._throttle.stop()
        self._pending, self._pending_set = None, False
        if self._pinned is not None and str(self._pinned.key).startswith("zone:"):
            self._pinned = self.zone_summary(self._pinned)
        elif self._pinned is not None:
            self._pinned = self._history.get(self._pinned.key, self._pinned)
            if self._live is not None and self._live.key == self._pinned.key:
                self._pinned = self._live
            if not self._listed(self._pinned):
                self._pinned = None
        if self._pinned is not None:
            self._snap = self._pinned
            self._ended = True
        else:
            self._pin_live_key = None
            self._snap = self._live if self._listed(self._live) else next(iter(self.history()), None)
            self._ended = self._snap is None or bool(getattr(self._snap, "closed", False))
        self._refresh_header()
        self._refresh_rows()

    def set_snapshot(self, snap: Any | None) -> None:
        """Queue a snapshot; applied at most every 100 ms (the newest wins).

        ``None`` means "no open encounter": the last snapshot stays on screen in
        the dimmed *ended* state until the next encounter opens.  Another group's
        fight (``snap.ours`` false) is ignored unless that is switched on.
        """
        if snap is not None:
            self._live = snap
            if getattr(snap, "closed", False):
                self._remember(snap)
            if not self._listed(snap):
                # A correction can make the displayed or queued fight unrelated.
                same_summary = (self._snap is not None and str(self._snap.key).startswith("zone:")
                                and self._visit_key(self._snap) == self._visit_key(snap))
                if same_summary or any(s is not None and s.key == snap.key for s in (self._snap, self._pending)):
                    self._refresh_filtered_selection()
                return
        if self._pinned is not None:
            new_fight = (
                snap is not None
                and not getattr(snap, "closed", False)
                and snap.key not in (self._pinned.key, self._pin_live_key)
            )
            if new_fight:
                self._pinned = None  # new combat began: back to the live fight
                self._pin_live_key = None
            else:
                return  # browsing an earlier fight: keep it on screen (the open fight's refreshes too)
        self._pending = snap
        self._pending_set = True
        if not self._throttle.isActive():
            self._throttle.start()

    # -- encounter history (dropdown + zone summaries) ---------------------------------
    def _remember(self, snap: Any) -> None:
        self._history.pop(snap.key, None)
        self._history[snap.key] = snap
        while len(self._history) > HISTORY_LIMIT:
            self._history.pop(next(iter(self._history)))

    def set_history(self, snaps: Sequence[Any] | None) -> None:
        """Prime the encounter list (oldest first, as ``Engine.history`` returns)."""
        for snap in snaps or ():
            self._remember(snap)

    def update_encounter(self, snap: Any | None) -> None:
        """A closed fight counted again (``Engine.encounter_updated``, same key): replace it in
        the encounter list and, when it is the one on screen, in the meter.  Never a new fight:
        the live display is left alone."""
        if snap is None:
            return
        key = snap.key
        if key in self._history:
            self._history[key] = snap  # same place in the list
        else:
            self._remember(snap)
            # keep the list in time order (a fight of another group that turned out to be ours)
            self._history = dict(sorted(self._history.items(), key=lambda kv: float(getattr(kv[1], "start", 0.0))))
        if self._pending_set and self._pending is not None and self._pending.key == key:
            self._pending = snap
        if self._live is not None and self._live.key == key:
            self._live = snap
        if self._pinned is not None and self._pinned.key == key:
            self._pinned = snap
        if self._snap is not None and self._snap.key == key:
            if not self._listed(snap):
                self._refresh_filtered_selection()
            else:
                self._snap = snap
                self._refresh_header()
                self._refresh_rows()
        elif self._snap is not None and str(self._snap.key).startswith("zone:") and self._visit_key(self._snap) == self._visit_key(snap):
            self._refresh_filtered_selection()

    def history(self) -> list[Any]:
        """Closed encounters, newest first."""
        return [s for s in reversed(self._history.values()) if self._listed(s)]

    def show_encounter(self, snap: Any | None) -> None:
        """Show an earlier encounter as if it had just ended; ``None`` follows the live fight.

        It stays until the next fight starts."""
        # A snapshot queued while the menu was open must not replace the choice.
        self._throttle.stop()
        self._pending, self._pending_set = None, False
        if snap is not None and str(snap.key).startswith("zone:"):
            snap = self.zone_summary(snap)
        elif snap is not None:
            snap = self._history.get(snap.key, snap)
            if self._live is not None and self._live.key == snap.key:
                snap = self._live
        if snap is None or not self._listed(snap):
            self._pinned = None
            self._pin_live_key = None
            self._snap = self._live if self._listed(self._live) else next(iter(self.history()), None)
            self._ended = bool(self._snap is None or getattr(self._snap, "closed", False))
        else:
            live = self._live
            self._pin_live_key = live.key if live is not None and not getattr(live, "closed", False) else None
            self._pinned = snap
            self._snap = snap
            self._ended = True
        self._refresh_header()
        self._refresh_rows()

    @property
    def pinned(self) -> Any | None:
        return self._pinned

    def _visit_key(self, snap: Any) -> tuple[str, float]:
        return (str(getattr(snap, "zone", "") or ""), float(getattr(snap, "zone_since", 0.0) or 0.0))

    def zone_summary(self, ref: Any | None = None) -> Any | None:
        """Every fight of ``ref``'s zone visit merged (the shown fight by default)."""
        from mnmparse.app.models import merge_snapshots

        ref = ref if ref is not None else self._snap
        if ref is None:
            return None
        visit = self._visit_key(ref)
        snaps = self._visit_snaps(ref)
        if not snaps:
            return None
        title = visit[0] or "this zone"
        return merge_snapshots(snaps, key=f"zone:{visit[0]}|{visit[1]:.0f}", label=title, zone=visit[0], zone_since=visit[1])

    def _visit_snaps(self, ref: Any) -> list[Any]:
        visit = self._visit_key(ref)
        snaps = {s.key: s for s in self._history.values() if self._visit_key(s) == visit and self._listed(s)}
        if self._listed(self._live) and self._visit_key(self._live) == visit:
            snaps[self._live.key] = self._live
        # Prefer stored/current snapshots over a menu reference captured before a correction.
        if (ref.key not in self._history and (self._live is None or ref.key != self._live.key)
                and self._listed(ref) and not str(ref.key).startswith("zone:")):
            snaps[ref.key] = ref
        return list(snaps.values())

    def _name_tooltip(self, row: Any) -> str:
        """Break down the displayed row for the selected tab and encounter."""
        key = "heals" if self._tab == "healing" else self._tab
        return _cell_tooltip(row, key)

    def menu_qss(self) -> str:
        return (
            f"QMenu {{ background: {token('BG1')}; color: {token('TEXT')}; border: 1px solid {token('LINE')}; padding: 4px; }}"
            f"QMenu::item {{ padding: 4px 18px 4px 10px; border-radius: 4px; }}"
            f"QMenu::item:selected {{ background: {token('BG2')}; }}"
            f"QMenu::item:disabled {{ color: {token('MUTED')}; }}"
            f"QMenu::separator {{ height: 1px; background: {token('LINE')}; margin: 4px 6px; }}"
        )

    def copy_current(self) -> None:
        """Ask for the shown fight (live or picked from the dropdown) to be copied."""
        if self._snap is not None:
            self.copy_requested.emit(self._snap)

    def flash_copied(self) -> None:
        """Confirm a copy: the header's copy glyph shows a check mark for a moment."""
        self._header.flash_copied()

    @staticmethod
    def _respawn_mob(row: Any) -> str:
        if row is None or getattr(row, "is_you", False) or getattr(row, "is_pet", False):
            return ""
        if not (getattr(row, "is_npc", False) or getattr(row, "is_enemy", False)):
            return ""
        return str(getattr(row, "name", "") or "").strip()

    def _respawn_names_at(self, global_pos: QPoint, row: Any) -> list[str]:
        name = self._respawn_mob(row)
        if name:
            return [name]
        pos = QPointF(self._header.mapFromGlobal(global_pos))
        if (self._snap is None or str(self._snap.key).startswith("zone:")
                or not self._header._label_rect.contains(pos)):
            return []
        names = {self._respawn_mob(actor) for actor in getattr(self._snap, "rows", ())}
        return sorted(names - {""}, key=str.casefold)

    def _respawn_durations(self) -> dict[str, int]:
        try:
            data = json.loads(str(self._settings.value("overlay/respawn_zone_durations", "{}")))
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {zone: value for zone, value in data.items()
                if type(value) is int and 0 < value <= MAX_RESPAWN_SECONDS}

    @staticmethod
    def _respawn_key(zone: str) -> str:
        return " ".join(zone.split()).casefold()

    def start_respawn_timer(self, name: str, zone: str | None = None) -> None:
        """Start one countdown and remember the confirmed duration for its zone."""
        runner = self._timer_panel.runner
        if runner is None or not name.strip():
            return
        if zone is None:
            zone = str(getattr(self._snap, "zone", "") or "")
        key = self._respawn_key(zone)
        seconds = self._respawn_durations().get(key, DEFAULT_RESPAWN_SECONDS)
        dialog = RespawnTimerDialog(name, seconds, self, remember_duration=bool(key))
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            seconds = dialog.duration()
        finally:
            dialog.deleteLater()
        if not 0 < seconds <= MAX_RESPAWN_SECONDS:
            return
        runner.start_one_time_timer(f"{name} respawn", seconds, keep_until_dismissed=True)
        if not key:
            return  # An unknown zone must not share a default with unrelated encounters.
        durations = self._respawn_durations()
        durations[key] = seconds
        self._settings.setValue("overlay/respawn_zone_durations", json.dumps(durations, ensure_ascii=False))
        self._settings.sync()

    def _add_respawn_entries(self, menu: QMenu, names: list[str]) -> dict[Any, Any]:
        if not names or self._timer_panel.runner is None:
            return {}
        zone = str(getattr(self._snap, "zone", "") or "")
        menu.addSeparator()
        if len(names) == 1:
            action = menu.addAction("Start respawn timer…")
            return {action: lambda: self.start_respawn_timer(names[0], zone)}
        submenu = menu.addMenu("Start respawn timer…")
        handlers = {}
        for name in names:
            action = submenu.addAction(name.replace("&", "&&"))
            handlers[action] = lambda name=name: self.start_respawn_timer(name, zone)
        return handlers

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        menu = QMenu(self)
        menu.setStyleSheet(self.menu_qss())
        copy = menu.addAction("Copy this fight to the clipboard")
        copy.setEnabled(self._snap is not None)
        mode = menu.addAction("Show your group" if self._view_mode == "self" else "Show just you")
        browse = menu.addAction("Earlier fights…")
        handlers = {copy: self.copy_current, mode: self.toggle_view_mode,
                    browse: lambda: self.open_encounter_menu(event.globalPos())}
        row = self._table.row_at(event.globalPos()) if self._tab not in ("feed", "session") and self._view_mode != "self" else None
        handlers.update(self._add_respawn_entries(menu, self._respawn_names_at(event.globalPos(), row)))
        handlers.update(add_group_entries(menu, row, self.group_override_requested.emit))
        handlers.update(add_pet_entries(menu, row, self._snap, self.pet_owner_requested.emit))
        chosen = menu.exec(event.globalPos())
        menu.deleteLater()
        action = handlers.get(chosen)
        if action is not None:
            action()

    def open_encounter_menu(self, global_pos: QPoint) -> None:
        """The encounter name's dropdown: the live fight, earlier fights by zone, zone summaries."""
        menu = QMenu(self)
        menu.setStyleSheet(self.menu_qss())
        menu.setToolTipsVisible(True)
        follow = menu.addAction("Live fight (follow)")
        follow.setCheckable(True)
        follow.setChecked(self._pinned is None)
        follow.setData(None)
        current_visit: tuple[str, float] | None = None
        for snap in self.history()[:MENU_ENCOUNTERS]:
            visit = self._visit_key(snap)
            if visit != current_visit:
                current_visit = visit
                menu.addSeparator()
                summary = menu.addAction(f"{visit[0] or 'Unknown zone'}: all fights")
                summary.setData(("zone", snap))
            when = time.strftime("%H:%M:%S", time.localtime(float(getattr(snap, "start", 0.0) or 0.0)))
            dps = float(getattr(snap, "raid_dps", 0.0) or 0.0)
            action = menu.addAction(
                f"   {when}  {snap.label}  ·  {fmt_mmss(float(getattr(snap, 'duration', 0.0) or 0.0))}  ·  {fmt_rate(dps)} dps"
            )
            action.setCheckable(True)
            action.setChecked(self._pinned is not None and self._pinned.key == snap.key)
            action.setData(("enc", snap))
        if not self.history():
            empty = menu.addAction("No earlier fights yet")
            empty.setEnabled(False)
        chosen = menu.exec(global_pos)
        menu.deleteLater()
        if chosen is None:
            return
        data = chosen.data()
        if data is None:
            self.show_encounter(None)
        elif data[0] == "zone":
            self.show_encounter(self.zone_summary(data[1]))
        else:
            self.show_encounter(data[1])

    # -- auto attack -----------------------------------------------------------------------
    @property
    def attack_bar(self) -> AttackBar:
        return self._attack_bar

    @property
    def timer_panel(self) -> TimerPanel:
        return self._timer_panel

    def set_trigger_runner(self, runner: Any) -> None:
        """Show the trigger timers of ``runner`` (``triggers_runtime.TriggerRunner``)."""
        self._timer_panel.set_runner(runner)

    def dock_anchor(self, panel: Any) -> QRect:
        """What ``panel`` docks under: the overlay, or for the auto-attack bar the timer
        panel when that is docked and showing."""
        timers = self._timer_panel
        if panel is self._attack_bar and timers.docked and timers.isVisible():
            return timers.frameGeometry()
        return self.frameGeometry()

    def panels_changed(self) -> None:
        """A panel appeared, vanished, changed height or (un)docked: re-stack the ones below."""
        if self._attack_bar.isVisible():
            self._attack_bar.follow()

    def observe_event(self, ev: Any) -> None:
        """Every parsed event (the auto-attack bar learns from your own swings)."""
        self._attack_bar.observe(ev)

    def set_config(self, cfg: Any) -> None:
        self._cfg = cfg
        self._attack_bar.set_player_name(str(getattr(cfg, "player_name", "") or ""))
        self.set_attack_bar_enabled(bool(getattr(cfg, "attack_bar", True)))

    def set_attack_bar_enabled(self, enabled: bool) -> None:
        """Show or hide the auto-attack bar (remembered between runs, like the other overlay state)."""
        enabled = bool(enabled)
        changed = enabled != self._attack_bar.enabled
        self._attack_bar.enabled = enabled
        self._settings.setValue("attack_bar/enabled", enabled)
        self._attack_bar.sync()
        if changed:
            self.attack_bar_changed.emit(enabled)

    def set_tab(self, tab: str) -> None:
        """Switch between ``damage``/``healing``/``taken``/``feed``."""
        if tab not in {k for k, _ in TABS}:
            log.warning("overlay: unknown tab %r", tab)
            return
        changed = tab != self._tab
        self._tab = tab
        self._tabs.set_current(tab)
        self._stack.setCurrentWidget(self._widget_for_tab(tab))
        self._refresh_rows()
        self._settings.setValue(f"{SETTINGS_GROUP}/tab", tab)
        if changed:
            self.tab_changed.emit(tab)

    def set_locked(self, locked: bool) -> None:
        """Lock = no moving or resizing and no hover toolbar; the mouse still works."""
        locked = bool(locked)
        if locked == self._locked:
            return
        self._locked = locked
        self._update_chrome()
        self._header.update_cursor()
        self._attack_bar.update_cursor()
        self._timer_panel.update_cursor()
        self._settings.setValue(f"{SETTINGS_GROUP}/locked", locked)
        log.info("overlay %s", "locked" if locked else "unlocked")
        self.locked_changed.emit(locked)

    def set_opacity(self, opacity: float) -> None:
        """Background alpha (0.2..1.0); text stays fully opaque."""
        value = _clamp(float(opacity), *OPACITY_RANGE)
        if abs(value - self._opacity) < 1e-6:
            return
        self._opacity = value
        self._toolbar.opacity.set_value(int(round(value * 100)))
        self._apply_stylesheet()
        self._settings.setValue(f"{SETTINGS_GROUP}/opacity", value)
        self.update()
        self._attack_bar.update()
        self._timer_panel.update()
        self.appearance_changed.emit(self._opacity, self._font_scale)

    def set_font_scale(self, scale: float) -> None:
        """Scale every font in the overlay (0.6..2.0, base 13 px)."""
        value = round(_clamp(float(scale), *FONT_SCALE_RANGE), 2)
        if abs(value - self._font_scale) < 1e-6:
            return
        self._font_scale = value
        self._apply_fonts()
        self._settings.setValue(f"{SETTINGS_GROUP}/font_scale", value)
        self.appearance_changed.emit(self._opacity, self._font_scale)

    def append_feed(self, text: str, kind: str, is_player_action: bool, is_player_target: bool | None = None) -> None:
        """Add a line to the Feed tab (keeps the last 12).

        ``is_player_target`` (parsed target == the player) colours hits on the player
        DANGER even when the line names the player instead of YOU; ``None`` falls back
        to the feed's own YOU/YOUR word scan.
        """
        self._feed.append(text, kind, is_player_action, time.time(), is_player_target=is_player_target)

    def persist_geometry(self) -> None:
        """Store the current geometry under ``overlay/geometry``."""
        if self.windowHandle() is None:
            return
        self._settings.setValue(f"{SETTINGS_GROUP}/geometry", self.geometry())

    def reset_geometry(self) -> None:
        """Move back to the default geometry (clamped onto a screen)."""
        self.setGeometry(clamp_to_screen(DEFAULT_GEOMETRY))
        self.persist_geometry()
        self._timer_panel.dock()  # panels dragged off-screen come back with the overlay
        self._attack_bar.dock()

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> None:
        s = self._settings
        g = f"{SETTINGS_GROUP}/"
        cfg = self._cfg
        self._locked = bool(s.value(g + "locked", bool(getattr(cfg, "overlay_locked", True)), type=bool))
        self._click_through = bool(s.value(g + "click_through", bool(getattr(cfg, "overlay_click_through", False)), type=bool))
        self._view_mode = "self" if str(s.value(g + "view_mode", "group")) == "self" else "group"
        self._opacity = _clamp(float(s.value(g + "opacity", float(getattr(cfg, "overlay_opacity", 0.85)), type=float)), *OPACITY_RANGE)
        self._font_scale = _clamp(float(s.value(g + "font_scale", float(getattr(cfg, "overlay_font_scale", 1.0)), type=float)), *FONT_SCALE_RANGE)
        tab = str(s.value(g + "tab", str(getattr(cfg, "overlay_tab", "damage") or "damage")))
        self._tab = tab if tab in {k for k, _ in TABS} else "damage"
        sort_key = str(s.value(g + "sort_key", "damage"))
        sort_desc = bool(s.value(g + "sort_desc", True, type=bool))
        self._table.set_sort(sort_key, sort_desc)
        geom = s.value(g + "geometry", None)
        rect = geom if isinstance(geom, QRect) and geom.isValid() else DEFAULT_GEOMETRY
        self.setGeometry(clamp_to_screen(rect))
        self._toolbar.opacity.set_value(int(round(self._opacity * 100)))
        self._tabs.set_current(self._tab)
        self._stack.setCurrentWidget(self._widget_for_tab(self._tab))
        self._session.set_view_mode(self._view_mode, str(getattr(cfg, "player_name", "") or ""))

    def _widget_for_tab(self, tab: str) -> QWidget:
        if tab == "feed":
            return self._feed
        if tab == "session":
            return self._session
        return self._table

    def set_session(self, snap: Any | None) -> None:
        """Replace the session (loot / kills / CC) data shown on the Session tab."""
        self._session.set_snapshot(snap)

    def _on_sort_changed(self, key: str, descending: bool) -> None:
        self._settings.setValue(f"{SETTINGS_GROUP}/sort_key", key)
        self._settings.setValue(f"{SETTINGS_GROUP}/sort_desc", bool(descending))

    # ------------------------------------------------------------------ appearance
    def set_status(self, status: dict[str, Any]) -> None:
        """Show a short warning while the chat is covered or hard to read (engine status)."""
        state = str(status.get("state", "running"))
        warning = capture_warning(status) if state in ("running", "paused") else None
        if self._warning_latch.update(warning is not None) and warning is not None:
            short, detail = warning
            self._warning.setText(f"!  {short}   x")
            self._warning.setToolTip(f"{detail}\n\nClick to hide until it happens again.")
            if self._warning.isHidden():
                self._warning.show()
        elif not self._warning.isHidden():
            self._warning.hide()

    def dismiss_warning(self) -> None:
        """Hide the warning until the problem clears and comes back."""
        self._warning_latch.dismiss()
        self._warning.hide()

    def warning_visible(self) -> bool:
        return not self._warning.isHidden()

    def _apply_fonts(self) -> None:
        px = OVERLAY_BASE_PX * self._font_scale
        self._header.set_font_px(px)
        self._tabs.set_font_px(px)
        self._warning.set_font_px(px)
        self._timer_panel.set_font_px(px)
        self._attack_bar.set_font_px(px)
        self._table.set_font_scale(self._font_scale)
        self._feed.set_font_scale(self._font_scale)
        self._session.set_font_scale(self._font_scale)
        # Nothing may clip: the minimum width follows the font size so every tab title and
        # the meter's name + main number always fit (the meter hides its optional columns
        # on its own as the overlay gets narrower).
        self.setMinimumWidth(self.needed_min_width())
        self.update()

    def needed_min_width(self) -> int:
        """Narrowest the overlay may get at the current font size."""
        margins = self.layout().contentsMargins() if self.layout() is not None else None
        side = (margins.left() + margins.right()) if margins is not None else 20
        return max(MIN_SIZE.width(), self._tabs.needed_width() + side, self._table.minimum_width() + side)

    def _apply_stylesheet(self) -> None:
        qss = ""
        try:
            from mnmparse.app import theme as _theme  # optional, written concurrently

            fn = getattr(_theme, "overlay_qss", None)
            if callable(fn):
                qss = str(fn(self._opacity) or "")
        except Exception as exc:  # noqa: BLE001 - never let styling break the overlay
            log.debug("overlay_qss unavailable: %s", exc)
        self.setStyleSheet(qss)

    def _apply_input_flag(self) -> None:
        was_visible = self.isVisible()
        self._suppress_visibility = True
        try:
            # Re-creating the native window hides it; re-show WITHOUT activating.
            self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, self._click_through)
            if was_visible:
                self.show()
            self._timer_panel.sync()
            self._attack_bar.sync()
        finally:
            self._suppress_visibility = False
        self._update_chrome()

    def _update_chrome(self) -> None:
        show = (not self._locked) and self._hovering
        self._toolbar.setVisible(show)
        self._grip.setVisible(not self._locked)
        self._position_chrome()
        self._header.set_reserved_right(self._toolbar.width() + 4 if show else 0)

    def _position_chrome(self) -> None:
        self._toolbar.adjustSize()
        self._toolbar.move(self.width() - self._toolbar.width() - 8, 4)
        self._grip.move(self.width() - self._grip.width() - 2, self.height() - self._grip.height() - 2)

    # ------------------------------------------------------------------ data
    def _flush_snapshot(self) -> None:
        if not self._pending_set:
            return
        snap, self._pending, self._pending_set = self._pending, None, False
        if snap is not None and not self._listed(snap):
            self._refresh_filtered_selection()
            return
        if snap is None:
            if self._snap is not None:
                self._ended = True
        else:
            self._snap = snap
            self._ended = bool(getattr(snap, "closed", False))
        self._refresh_header()
        self._refresh_rows()

    def _refresh_header(self) -> None:
        snap = self._snap
        if snap is None:
            self._header.set_data(label="", duration=0.0, total=0, raid_dps=0.0, ended=False, has_data=False)
            self._header.set_base_tip("")
            return
        label = str(getattr(snap, "label", "") or "")
        total = int(getattr(snap, "total_damage", 0) or 0)
        raid_dps = float(getattr(snap, "raid_dps", 0.0) or 0.0)
        if self._view_mode == "self":
            player = str(getattr(self._cfg, "player_name", "") or "You")
            me = owner_row(snap, player)
            label = f"{player} · self · {label}" if label else f"{player} · self"
            if me is not None:
                total = int(getattr(me, "damage", 0) or 0)
                raid_dps = float(getattr(me, "dps", 0.0) or 0.0)
        self._header.set_data(
            label=label,
            duration=float(getattr(snap, "duration", 0.0) or 0.0),
            total=total,
            raid_dps=raid_dps,
            ended=self._ended,
            has_data=True,
        )
        browsing = "\n(an earlier fight: the next fight replaces it)" if self._pinned is not None else ""
        self._header.set_base_tip(
            f"{label}{browsing}\nClick the name to browse earlier fights; right-click it to start a respawn timer. "
            f"Click elsewhere on this bar to switch to the "
            f"{'group table' if self._view_mode == 'self' else 'self view (your own sources)'}"
        )

    def _refresh_rows(self) -> None:
        if self._tab in ("feed", "session"):
            return
        if self._snap is None:
            rows: Sequence[Any] = []
        elif self._view_mode == "self":
            rows = _self_rows(self._snap, self._tab, str(getattr(self._cfg, "player_name", "") or ""))
        else:
            rows = _rows_for_tab(self._snap, self._tab)
        self._table.set_rows(rows, self._tab)

    # ------------------------------------------------------------------ Qt events
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 12.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setClipPath(path)
        p.fillPath(path, qcolor(token("BG0"), self._opacity))
        # slightly lighter header band
        band_h = self._tabs.geometry().bottom() + 4
        p.fillRect(QRectF(r.left(), r.top(), r.width(), band_h), qcolor("#ffffff", 0.035 * self._opacity + 0.015))
        p.setClipping(False)
        line = qcolor(token("LINE"))
        line.setAlphaF(max(line.alphaF(), 0.14))
        p.setPen(QPen(line, 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        p.end()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_chrome()
        if self.isVisible():
            self._geometry_timer.start()
            self._timer_panel.follow()
            self._attack_bar.follow()

    def moveEvent(self, event: Any) -> None:  # noqa: N802
        super().moveEvent(event)
        if self.isVisible():
            self._geometry_timer.start()
            self._timer_panel.follow()
            self._attack_bar.follow()

    def enterEvent(self, event: QEvent) -> None:  # noqa: N802
        self._hovering = True
        self._update_chrome()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        # QCursor.pos() is a read-only query; children (toolbar) keep the hover alive.
        if not self.rect().contains(self.mapFromGlobal(QCursor.pos())):
            self._hovering = False
            self._update_chrome()
        super().leaveEvent(event)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802
        super().showEvent(event)
        self._position_chrome()
        self._timer_panel.sync()
        self._attack_bar.sync()
        if not self._suppress_visibility:
            self.visibility_changed.emit(True)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        super().hideEvent(event)
        self._hovering = False
        if not self._suppress_visibility:
            self._toolbar.hide()
            self._attack_bar.hide()
            self._timer_panel.hide()
            self.visibility_changed.emit(False)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self.persist_geometry()
        if event.spontaneous():  # user closed it: just hide, the app owns the lifetime
            event.ignore()
            self.hide()
            return
        # A programmatic close is the app tearing down, not the user turning the overlay
        # off: the hide that follows must not report a visibility change (the app would
        # persist "overlay off" for the next launch).
        self._suppress_visibility = True
        self._attack_bar.close()
        self._timer_panel.close()
        super().closeEvent(event)

    # test / debugging hook: force the hover chrome (used by the visual check script)
    def _set_hovering(self, hovering: bool) -> None:
        self._hovering = bool(hovering)
        self._update_chrome()


__all__ = ["OverlayWindow", "TABS", "DEFAULT_GEOMETRY", "clamp_to_screen"]
