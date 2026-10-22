"""Native NPC menu actions with small, font-independent wiki detail badges."""
from __future__ import annotations

import re
from collections.abc import Mapping, Set
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QAction, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QMenu, QWidget

from mnmparse.app import theme


def npc_title_key(name: str) -> str:
    return " ".join(str(name).replace("_", " ").split()).casefold()


def npc_menu_label(name: str, level: str = "") -> str:
    level = " ".join(str(level).split())
    if level.casefold() in ("unknown", "none", "n/a", "not available in wiki data"):
        level = ""
    level = re.sub(r"(?<=\d)\s*[-–]\s*(?=\d)", "–", level)
    label = f"Lv. {level}  {name}" if level else name
    # The game/wiki name remains literal; ampersands aren't menu mnemonics.
    return label.replace("&", "&&")


def _paint_sword(painter: QPainter) -> None:
    painter.setPen(QPen(theme.qcolor(theme.ACCENT), 2.0, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    blade = QPainterPath()
    blade.moveTo(8, 24)
    for x, y in ((24, 8), (31, 5), (28, 12), (12, 28), (8, 24)):
        blade.lineTo(x, y)
    painter.drawPath(blade)
    painter.drawLine(7, 22, 15, 30)
    painter.drawLine(10, 27, 5, 32)
    painter.drawLine(3, 30, 7, 34)
    # A four-point sparkle distinguishes loot from a plain combat sword.
    sparkle = QPainterPath()
    sparkle.moveTo(11, 3)
    for x, y in ((12.5, 7.5), (17, 9), (12.5, 10.5), (11, 15),
                 (9.5, 10.5), (5, 9), (9.5, 7.5), (11, 3)):
        sparkle.lineTo(x, y)
    painter.drawPath(sparkle)


def _paint_feather(painter: QPainter) -> None:
    painter.setPen(QPen(theme.qcolor(theme.ACCENT2), 2.0, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    feather = QPainterPath()
    feather.moveTo(9, 29)
    feather.cubicTo(5, 16, 15, 5, 32, 4)
    feather.cubicTo(33, 19, 22, 31, 9, 29)
    painter.drawPath(feather)
    painter.drawLine(5, 34, 27, 10)
    painter.drawLine(13, 25, 12, 17)
    painter.drawLine(19, 19, 18, 11)
    painter.drawLine(19, 19, 27, 18)
    painter.drawLine(13, 25, 23, 24)


class NotableNpcMenu(QMenu):
    """Keep standard QAction input/accessibility while painting right-side badges."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("notableNpcMenu")
        self.setToolTipsVisible(True)
        self._badge_size = 18
        self.set_badge_size(self._badge_size)

    def set_badge_size(self, size: int) -> None:
        self._badge_size = max(14, int(size))
        # Padding is part of native item sizing, so long names never run under
        # the badges. Status rows share the same alignment as selectable rows.
        self._badge_padding = self._badge_size * 2 + 24
        self.setStyleSheet(f"QMenu#notableNpcMenu::item {{ padding-right: {self._badge_padding}px; }}")
        self.update()

    def set_action_label(self, action: QAction, name: str, level: str) -> None:
        full = npc_menu_label(name, level).replace("&&", "&")
        screen = self.screen()
        # Leave room for both badges, native padding/frame and the small inset
        # Qt applies after a popup is resized. Bound the native action itself,
        # not just the widget: otherwise Qt can lay out badges offscreen.
        available = screen.availableGeometry().width() if screen is not None else 1920
        width = max(1, available - self._badge_padding - 36)
        fm = self.fontMetrics()
        label = full
        if fm.horizontalAdvance(full) > width:
            suffix = "  " + name
            if level and full.endswith(suffix):
                prefix = full[:-len(suffix)]
                prefix_width = width - fm.horizontalAdvance(suffix)
                if prefix_width >= fm.horizontalAdvance("Lv. …"):
                    label = fm.elidedText(prefix, Qt.TextElideMode.ElideMiddle, prefix_width) + suffix
                else:
                    label = fm.elidedText(full, Qt.TextElideMode.ElideRight, width)
            else:
                label = fm.elidedText(full, Qt.TextElideMode.ElideRight, width)
        action.setProperty("notableFullLabel", full if label != full else "")
        action.setText(label.replace("&", "&&"))

    @staticmethod
    def set_badges(action: QAction, *, unique_loot: bool, quests: bool) -> None:
        action.setProperty("notableUniqueLoot", bool(unique_loot))
        action.setProperty("notableQuests", bool(quests))
        hints = [action.property("notableFullLabel")] if action.property("notableFullLabel") else []
        if unique_loot:
            hints.append("Unique loot")
        if quests:
            hints.append("Offers or is required for quests")
        action.setToolTip("; ".join(hints))
        action.setStatusTip(action.toolTip())

    def badge_rects(self, action: QAction) -> tuple[QRectF, QRectF]:
        row = self.actionGeometry(action).intersected(self.rect())
        size = min(self._badge_size, max(1, row.height() - 4))
        y = row.center().y() - size / 2
        x = row.right() - 12 - size
        return (QRectF(x - size - 6, y, size, size), QRectF(x, y, size, size))

    def refresh_metadata(self, levels: Mapping[str, str], unique_loot: Set[str],
                         quests: Set[str]) -> None:
        """Refresh existing rows without replacing actions or moving hit targets."""
        # Text may widen a native menu. Never shrink away from the pointer while
        # it is open, and keep that widening within the current screen.
        self.setMinimumWidth(self.width())
        screen = self.screen()
        area = screen.availableGeometry() if screen is not None else None
        if area is not None:
            self.setMaximumWidth(max(1, area.width() - 8))
        for action in self.actions():
            name = action.data()
            if not isinstance(name, str):
                continue
            key = npc_title_key(name)
            self.set_action_label(action, name, levels.get(key, ""))
            self.set_badges(action, unique_loot=key in unique_loot, quests=key in quests)
        if area is not None:
            self.move(max(area.left(), min(self.x(), area.right() - self.width() + 1)), self.y())
        self.update()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        super().hideEvent(event)
        self.setMinimumWidth(0)
        self.setMaximumWidth(16777215)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for action in self.actions():
            if action.isSeparator() or not action.isVisible():
                continue
            for visible, rect, draw in (
                (action.property("notableUniqueLoot"), self.badge_rects(action)[0], _paint_sword),
                (action.property("notableQuests"), self.badge_rects(action)[1], _paint_feather),
            ):
                if visible:
                    painter.save()
                    painter.translate(rect.topLeft())
                    painter.scale(rect.width() / 40, rect.height() / 40)
                    draw(painter)
                    painter.restore()
        painter.end()
