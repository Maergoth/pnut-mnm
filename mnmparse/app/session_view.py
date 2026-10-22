"""Session tab: loot, coin, crafting, kills, deaths and zones as collapsible categories.

:class:`SessionView` renders a :class:`~mnmparse.session.SessionSnapshot` as a tree: one
top-level row per category with its total, children with the detail (items by name with
who looted them, coin by player, slain mobs by type, deaths by player, crafted items, zones
in order).  Hovering any row shows the full detail.  Combat results (stuns, roots, ...) are
not shown here; they belong to the Overview tab.  Pure display: it computes nothing.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


from mnmparse.app import theme
from mnmparse.session import SessionSnapshot, filter_session, format_coin

__all__ = ["SessionView", "CATEGORY_COLORS"]

CATEGORY_COLORS: dict[str, str] = {
    "experience": theme.ACCENT,
    "items": theme.KIND_COLORS.get("loot", theme.ACCENT),
    "coin": theme.KIND_COLORS.get("coin", "#e0c060"),
    "kills": theme.KIND_COLORS.get("kill", theme.ACCENT),
    "deaths": theme.DANGER,
    "crafts": theme.KIND_COLORS.get("craft", "#a5d6a7"),
    "rewards": theme.KIND_COLORS.get("reward", theme.ACCENT),
    "zones": theme.KIND_COLORS.get("zone", "#7fc8c8"),
    "personal": theme.KIND_COLORS.get("personal", "#7a8290"),
}

MAX_CHILDREN = 40


def _mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 3600:
        return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"
    return f"{total // 60:02d}:{total % 60:02d}"


class _SessionFooter(QLabel):
    activated = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__("", parent)
        self._action_enabled = False
        self._press: QPointF | None = None
        self._moved = False
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAccessibleName("Session totals and hourly rates")

    def set_action_enabled(self, enabled: bool) -> None:
        self._action_enabled = bool(enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.setAccessibleDescription("Click to start a new session." if enabled else "")
        if not enabled:
            self._press = None

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._press = None
        self._moved = False
        if self._action_enabled and event.button() == Qt.MouseButton.LeftButton:
            self._press = event.globalPosition()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is not None and (event.globalPosition() - self._press).manhattanLength() > 4:
            self._moved = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        pressed, self._press = self._press, None
        if (self._action_enabled and pressed is not None and not self._moved
                and event.button() == Qt.MouseButton.LeftButton
                and self.rect().contains(event.position().toPoint())
                and (event.globalPosition() - pressed).manhattanLength() <= 4):
            self.activated.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class SessionView(QWidget):
    """Category tree of the session; ``compact`` is the overlay density."""

    new_session_requested = Signal()

    def __init__(self, compact: bool, parent: QWidget | None = None, *,
                 show_new_session: bool = False) -> None:
        super().__init__(parent)
        self.compact = compact
        self._font_px = 13.0 if compact else 14.0
        self._snap: SessionSnapshot | None = None
        self._view_mode = "group"
        self._player = ""
        self._expanded: dict[str, bool] = {"items": True}
        self._new_session_allowed = bool(show_new_session)
        self._new_session_enabled = True
        self._footer_tip = ""
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

        self._footer = _SessionFooter(self)
        self._footer.setStyleSheet(f"color: {theme.MUTED}; background: transparent;")
        self._footer.setWordWrap(True)
        self._footer.activated.connect(self._request_new_session)
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(12)
        footer.addWidget(self._footer, 1)
        self._new_session_button: QPushButton | None = None
        if show_new_session and not compact:
            self._new_session_button = QPushButton("New session", self)
            self._new_session_button.setProperty("class", "primary")
            self._new_session_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._new_session_button.setToolTip("Archive the current live session and start a new one.")
            self._new_session_button.clicked.connect(self._request_new_session)
            footer.addWidget(self._new_session_button, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(footer)
        self._sync_new_session_action()
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
        self.set_snapshot(getattr(self, "_raw_snap", self._snap))

    def set_new_session_enabled(self, enabled: bool) -> None:
        """Only the owner of a live session may enable its optional footer action."""
        self._new_session_enabled = bool(enabled)
        self._sync_new_session_action()

    def _sync_new_session_action(self) -> None:
        enabled = self._new_session_allowed and self._new_session_enabled
        if self._new_session_button is not None:
            self._new_session_button.setEnabled(enabled)
        self._footer.set_action_enabled(enabled and bool(self._footer.text()))
        self._footer.setToolTip(self._footer_tip + ("\n\nClick to start a new session." if enabled and self._footer.text() else ""))

    def _request_new_session(self) -> None:
        if self._new_session_allowed and self._new_session_enabled:
            self.new_session_requested.emit()

    def set_config(self, cfg: Any) -> None:
        self._cfg = cfg
        self.set_snapshot(getattr(self, "_raw_snap", self._snap))

    def set_snapshot(self, snap: SessionSnapshot | None) -> None:
        """Replace the displayed session data (expansion state is kept per category)."""
        from mnmparse.privacy import project_session, casual_enabled
        if snap is not None and not getattr(snap, "privacy_mode", ""):
            self._raw_snap = snap
        snap = project_session(snap, getattr(self, "_cfg", None))
        self._snap = snap
        if snap is not None and self._view_mode == "self" and not casual_enabled(getattr(self, "_cfg", None)):
            snap = filter_session(snap, self._player or "You")
        self._tree.setUpdatesEnabled(False)
        try:
            self._tree.clear()
            if snap is None:
                self._footer.setText("")
                self._footer_tip = ""
                return
            self._add_items(snap)
            averages = getattr(snap, "group_averages", {})
            if averages:
                self._add_category("group-averages", f"Group averages ({snap.group_average_count})", "rounded",
                                   [(name.replace("_", " ").capitalize(), f"{value:g}", "per member")
                                    for name, value in averages.items()],
                                   tooltip="Known groups of three or more. Pets are counted with their owners.")
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
            self._add_experience(snap)
            if snap.personal_included:
                detail = [(f"Skill {s}", f"{v}", "") for s, v in snap.skill_ups] + [
                    (f"Faction {f}", f"{'+' if d > 0 else ''}{d}", "") for f, d in snap.faction
                ] + [("Experience ticks", f"{snap.xp_ticks}", "")]
                self._add_category("personal", "Personal", f"{len(detail)}", detail, tooltip="viewer-only lines")
            footer = (
                f"{_mmss(snap.combat_seconds)} in combat over {snap.encounters} encounters  ·  "
                f"Hourly: {snap.kills_per_hour:,.0f} kills  ·  {snap.items_per_hour:,.0f} items  ·  "
                f"{format_coin(int(snap.coin_per_hour))}"
            )
            status = snap.player_status
            rate = status.get("xp_points_per_hour")
            rate_tip = ""
            if rate is not None:
                if status.get("xp_rate_kind", "session") == "session":
                    footer += f"  ·  XP session ~{rate:,.1f}%"
                    rate_tip = "Session average recorded by an older version, in estimated level percentage points per hour."
                else:
                    footer += f"  ·  XP ~{rate:,.1f}%"
                    measured = _mmss(status.get("xp_rate_observed_seconds") or 0)
                    rate_tip = ("Rolling one-hour estimate in level percentage points per hour, from 30-second bar comparisons. "
                                f"Based on {measured} measured so far. "
                                "Idle before gains is excluded; breaks after gains lower the rate.")
            elif status.get("xp_gain_points") is not None:
                footer += "  ·  XP collecting…"
                rate_tip = ("Keep your XP bar visible for 30 seconds of stable comparisons to estimate XP/hour. "
                            "An old rate is hidden if unstable readings prevent a new comparison for over a minute.")
            self._footer.setText(footer)
            if snap.activity_rates.get("kind") == "rolling":
                session_tip = ("Rolling one-hour rates for kills, items and coin, matching XP's one-hour horizon. "
                               "New sessions use their shorter elapsed time. Breaks within the window lower the rates. "
                               "The category totals above still cover the whole session.")
            else:
                session_tip = (f"Recorded session averages over {_mmss(snap.elapsed)} total session time, "
                               "including idle time. This older archive has no rolling activity measurements.")
            self._footer_tip = session_tip + ("\n\n" + rate_tip if rate_tip else "")
        finally:
            self._tree.setUpdatesEnabled(True)
            self._sync_new_session_action()

    # ------------------------------------------------------------------
    def _add_experience(self, snap: SessionSnapshot) -> None:
        status = snap.player_status
        gain = status.get("xp_gain_points")
        percent = status.get("xp_percent")
        if gain is None and percent is None:
            return
        tip = ("Estimated from 30-second comparisons of your visible XP bar. "
               "Different levels require different XP amounts.")
        children = []
        if percent is not None:
            children.append(("Current bar", f"~{percent:.1f}%", "Toward next level"))
        observed = status.get("xp_observed_seconds")
        if observed:
            children.append(("Measured time", _mmss(observed), "Completed comparisons"))
        total = f"~{gain:.2f}%" if gain is not None and observed else "Collecting…"
        top = self._add_category("experience", "Experience (est.)", total,
                                 children, tooltip=tip)
        for index in range(top.childCount()):
            child = top.child(index)
            text = " · ".join(child.text(column) for column in range(3) if child.text(column))
            for column in range(3):
                child.setToolTip(column, text + "\n" + tip)

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
