"""Session tab: loot, coin, crafting, kills, deaths and zones as collapsible categories.

:class:`SessionView` renders a :class:`~mnmparse.session.SessionSnapshot` as a tree: one
top-level row per category with its total, children with the detail (items by name with
who looted them, coin by player, slain mobs by type, deaths by player, crafted items, zones
in order).  Hovering any row shows the full detail.  Combat results (stuns, roots, ...) are
not shown here; they belong to the Overview tab.  Pure display: it computes nothing.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import time

from mnmparse.app import theme
from mnmparse.session import SessionSnapshot, filter_session, format_coin

__all__ = ["SessionView", "CATEGORY_COLORS"]

CATEGORY_COLORS: dict[str, str] = {
    "items": theme.KIND_COLORS.get("loot", theme.ACCENT),
    "coin": theme.KIND_COLORS.get("coin", "#e0c060"),
    "kills": theme.KIND_COLORS.get("kill", theme.ACCENT),
    "deaths": theme.DANGER,
    "crafts": theme.KIND_COLORS.get("craft", "#a5d6a7"),
    "rewards": theme.KIND_COLORS.get("reward", theme.ACCENT),
    "zones": theme.KIND_COLORS.get("zone", "#7fc8c8"),
    "mez": theme.KIND_COLORS.get("awaken", "#9fb3c8"),
    "personal": theme.KIND_COLORS.get("personal", "#7a8290"),
}

MAX_CHILDREN = 40


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


class SessionView(QWidget):
    """Category tree of the session; ``compact`` is the overlay density."""

    def __init__(self, compact: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.compact = compact
        self._font_px = 13.0 if compact else 14.0
        self._snap: SessionSnapshot | None = None
        self._view_mode = "group"
        self._player = ""
        self._expanded: dict[str, bool] = {"items": True}
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, compact)

        root = QVBoxLayout(self)
        root.setContentsMargins(0 if compact else 16, 0 if compact else 12, 0 if compact else 16, 0 if compact else 12)
        root.setSpacing(4 if compact else 10)

        self._tree = QTreeWidget(self)
        self._tree.setColumnCount(3)
        self._tree.setHeaderLabels(["Category", "Count", "Detail"])
        self._tree.setRootIsDecorated(True)
        self._tree.setIndentation(14 if compact else 18)
        self._tree.setUniformRowHeights(True)
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._tree.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, compact)
        self._tree.setStyleSheet(
            "QTreeWidget { background: transparent; border: none; }"
            "QTreeWidget::item { padding: 1px 0; }"
            f"QHeaderView::section {{ background: transparent; color: {theme.MUTED}; border: none; padding: 2px 6px; }}"
        )
        header = self._tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(False)
        header.setVisible(not compact)
        self._tree.itemExpanded.connect(lambda item: self._remember(item, True))
        self._tree.itemCollapsed.connect(lambda item: self._remember(item, False))
        root.addWidget(self._tree, 1)

        self._footer = QLabel("", self)
        self._footer.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self._footer.setWordWrap(True)
        root.addWidget(self._footer)
        self.set_font_scale(1.0)

    # ------------------------------------------------------------------
    def set_font_scale(self, scale: float) -> None:
        px = self._font_px * max(0.5, float(scale))
        self._tree.setFont(QFont(theme.app_font(max(9, int(round(px))))))
        self._footer.setFont(QFont(theme.app_font(max(8, int(round(px * 0.85))))))
        self._tree.viewport().update()

    def set_view_mode(self, mode: str, player: str = "") -> None:
        """``"group"`` shows everyone; ``"self"`` only the viewer's own loot, coin, kills, deaths, crafts."""
        self._view_mode = "self" if mode == "self" else "group"
        self._player = player or self._player
        self.set_snapshot(self._snap)

    def set_snapshot(self, snap: SessionSnapshot | None) -> None:
        """Replace the displayed session data (expansion state is kept per category)."""
        self._snap = snap
        if snap is not None and self._view_mode == "self":
            snap = filter_session(snap, self._player or "You")
        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.clear()
            if snap is None:
                self._footer.setText("")
                return
            self._add_items(snap)
            coin_detail = [(name, "", format_coin(c)) for name, c in snap.coin_by_looter[:MAX_CHILDREN]]
            if snap.coin_split:
                coin_detail.append(("Your splits", "", format_coin(snap.coin_split)))
            # The "Me" view's coin is what the viewer received (filter_session), not the gross loot.
            coin_title = "Coin received" if self._view_mode == "self" else "Coin looted"
            self._add_category("coin", coin_title, format_coin(snap.coin_total), coin_detail,
                               tooltip="\n".join(f"{n}: {format_coin(c)}" for n, c in snap.coin_by_looter) or "no coin yet")
            kills_detail = [(mob, f"{n}", "") for mob, n in snap.kills_by_target[:MAX_CHILDREN]]
            kills_tip = "\n".join(f"{k}: {n}" for k, n in snap.kills_by_killer) or "no kills yet"
            self._add_category("kills", "Kills", f"{snap.kills:,}", kills_detail, tooltip="By killer:\n" + kills_tip)
            self._add_category("deaths", "Deaths", f"{snap.deaths:,}",
                               [(name, f"{n}", "") for name, n in snap.deaths_by_player], tooltip="player deaths")
            crafts_tip = "\n".join(f"{n}: {c}" for n, c in snap.crafts_by_crafter) or "nothing crafted yet"
            outsiders = getattr(snap, "outsider_crafts", None) or []
            if outsiders:
                crafts_tip += "\n\nNearby, not in your group (not counted):\n" + "\n".join(f"{n}: {c}" for n, c in outsiders)
            self._add_category("crafts", "Crafted", f"{snap.crafts:,}",
                               [(item, f"{n}", "") for item, n in snap.crafts_by_item[:MAX_CHILDREN]],
                               tooltip=crafts_tip)
            rewards = getattr(snap, "rewards", None) or []
            if rewards:
                self._add_category(
                    "rewards", "Rewards", f"{len(rewards):,}",
                    [(e.item, "", e.source or "") for e in rewards[-MAX_CHILDREN:]],
                    tooltip="Quest hand-ins (not corpse loot, not in Items looted)",
                )
            self._add_category("zones", "Zones", f"{len(snap.zones)}", [(z, "", "") for z in snap.zones],
                               tooltip=" → ".join(snap.zones) if snap.zones else "no zone change seen")
            breaks = [(mob, "", time.strftime("%H:%M:%S", time.localtime(ts))) for ts, mob in snap.mez_breaks[-MAX_CHILDREN:]]
            self._add_category("mez", "Mez breaks", f"{len(snap.mez_breaks)}", breaks,
                               tooltip="\n".join(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  {mob}" for ts, mob in snap.mez_breaks[-25:])
                               or "no mesmerize broke")
            if snap.personal_included:
                detail = [(f"Skill {s}", f"{v}", "") for s, v in snap.skill_ups] + [
                    (f"Faction {f}", f"{'+' if d > 0 else ''}{d}", "") for f, d in snap.faction
                ] + [("Experience ticks", f"{snap.xp_ticks}", "")]
                self._add_category("personal", "Personal", f"{len(detail)}", detail, tooltip="viewer-only lines")
            self._footer.setText(
                f"{_mmss(snap.combat_seconds)} in combat over {snap.encounters} encounters  ·  "
                f"{snap.kills_per_hour:,.0f} kills/h  ·  {snap.items_per_hour:,.0f} items/h  ·  "
                f"{format_coin(int(snap.coin_per_hour))}/h"
            )
        finally:
            self._tree.setUpdatesEnabled(True)

    # ------------------------------------------------------------------
    def _add_items(self, snap: SessionSnapshot) -> None:
        """One expandable subtotal per person, followed by their complete item breakdown."""
        top = self._add_category("items", "Items looted", f"{snap.items:,}", [],
                                 tooltip=self._items_tooltip(snap))
        by_person: dict[str, list[tuple[str, int]]] = {}
        for item, looters in snap.item_looters.items():
            for name, count in looters:
                by_person.setdefault(name, []).append((item, count))
        for name, count in snap.items_by_looter:
            key = f"items/person/{name}"
            person = QTreeWidgetItem([name, f"{count:,}", ""])
            person.setData(0, Qt.ItemDataRole.UserRole, key)
            person.setForeground(0, QBrush(QColor(CATEGORY_COLORS["items"])))
            person.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            font = QFont(self._tree.font())
            font.setWeight(QFont.Weight.DemiBold)
            person.setFont(0, font)
            for item, quantity in sorted(by_person.get(name, []), key=lambda pair: (-pair[1], pair[0].casefold())):
                child = QTreeWidgetItem([item, f"{quantity:,}", ""])
                child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                child.setToolTip(0, f"{name} looted {quantity:,} × {item}")
                person.addChild(child)
            top.addChild(person)
            person.setExpanded(self._expanded.get(key, False))
        top.setExpanded(top.childCount() > 0 and self._expanded.get("items", True))

    def _add_category(
        self, key: str, title: str, total: str, children: list[tuple[str, str, str]], *, tooltip: str = ""
    ) -> QTreeWidgetItem:
        color = CATEGORY_COLORS.get(key, theme.TEXT)
        top = QTreeWidgetItem([title, total, ""])
        top.setData(0, Qt.ItemDataRole.UserRole, key)
        top.setForeground(0, QBrush(QColor(color)))
        top.setForeground(1, QBrush(QColor(theme.TEXT)))
        bold = QFont(self._tree.font())
        bold.setWeight(QFont.Weight.DemiBold)
        top.setFont(0, bold)
        top.setFont(1, bold)
        top.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if tooltip:
            for col in range(3):
                top.setToolTip(col, tooltip)
        for name, count, detail in children:
            child = QTreeWidgetItem([name, count, detail])
            child.setForeground(0, QBrush(QColor(theme.TEXT)))
            child.setForeground(1, QBrush(QColor(theme.MUTED)))
            child.setForeground(2, QBrush(QColor(theme.MUTED)))
            child.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            tip = f"{name}" + (f"  x{count}" if count else "") + (f"\n{detail}" if detail else "")
            for col in range(3):
                child.setToolTip(col, tip)
            top.addChild(child)
        self._tree.addTopLevelItem(top)
        top.setExpanded(bool(children) and self._expanded.get(key, False))
        return top

    def _remember(self, item: QTreeWidgetItem, expanded: bool) -> None:
        key = item.data(0, Qt.ItemDataRole.UserRole)
        if key:
            self._expanded[str(key)] = expanded

    @staticmethod
    def _looters_text(snap: SessionSnapshot, item: str) -> str:
        looters = snap.item_looters.get(item, [])
        return ", ".join(f"{name} x{n}" if n > 1 else name for name, n in looters[:6])

    @staticmethod
    def _items_tooltip(snap: SessionSnapshot) -> str:
        if not snap.items_by_name:
            return "no loot yet"
        lines = [f"{item} x{n}: {', '.join(f'{who} x{k}' if k > 1 else who for who, k in snap.item_looters.get(item, [])[:6])}"
                 for item, n in snap.items_by_name[:25]]
        if len(snap.items_by_name) > 25:
            lines.append(f"... and {len(snap.items_by_name) - 25} more")
        return "\n".join(lines)
