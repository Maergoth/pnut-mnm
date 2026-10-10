"""Reusable Qt widgets for PNUT M&M (APP_SPEC section 7).

* :class:`MeterTable` -- the DPS/HPS/damage-taken meter used by the overlay
  (``compact=True``) and the Live/History pages.  A ``QTableView`` over a
  ``QAbstractTableModel`` with a numeric sort proxy and a delegate that paints
  a gradient bar behind every row, the rank number and the coloured actor name.
* :class:`FeedView` -- colour-coded combat message list with kind filter,
  search and "stick to bottom unless the user scrolled up" behaviour.
* :class:`ToggleSwitch` -- animated iOS-style on/off button.
* :class:`SliderRow` -- ``label + slider + value`` form row.
* :class:`StatusChip` -- small pill with a coloured state dot.

Everything paints with the tokens from :mod:`mnmparse.app.theme`; a tiny
fallback keeps the module importable while the theme is being written.
"""

from __future__ import annotations

import html
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QSortFilterProxyModel,
    Qt,
    Property,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QShowEvent,
    QTextCursor,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QLayoutItem,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- theme access
try:  # the theme module is written concurrently; prefer it, fall back to the spec tokens
    from mnmparse.app import theme as _theme
except Exception:  # pragma: no cover - only while theme.py does not exist yet
    _theme = None  # type: ignore[assignment]


class _FallbackTheme:
    """The APP_SPEC section 4 tokens, used only when ``theme.py`` is unavailable."""

    BG0 = "#0f1117"
    BG1 = "#161a23"
    BG2 = "#1e2430"
    LINE = "rgba(255,255,255,0.08)"
    TEXT = "#e8e6e3"
    MUTED = "#9aa3b2"
    ACCENT = "#f0b35b"
    ACCENT2 = "#5bc0eb"
    DANGER = "#e5484d"
    SUCCESS = "#46c37b"
    YOU = "#ffd166"
    NPC = "#d9655b"
    PET = "#c084fc"
    PARTY = ("#5bc0eb", "#7ee787", "#ff9f68", "#c792ea", "#f78fb3", "#4dd0e1", "#ffd54f", "#a5d6a7")
    KIND_COLORS = {
        "melee_hit": TEXT,
        "ability_hit": TEXT,
        "damage_effect": TEXT,
        "melee_miss": MUTED,
        "ability_miss": MUTED,
        "resist": MUTED,
        "fizzle": MUTED,
        "heal": SUCCESS,
        "kill": ACCENT,
        "cast": ACCENT2,
        "interrupt": ACCENT2,
        "status": "#b8c0cc",
        "cannot_attack": "#d08770",
        "experience": "#c3e88d",
        "unknown": "#6b7280",
        "marker": "#6b7280",
    }

    @staticmethod
    def actor_color(name: str, *, is_you: bool, is_npc: bool, is_pet: bool) -> str:
        if is_you:
            return _FallbackTheme.YOU
        if is_pet:
            return _FallbackTheme.PET
        if is_npc:
            return _FallbackTheme.NPC
        h = 0
        for ch in name.casefold():
            h = (h * 31 + ord(ch)) & 0xFFFFFFFF
        return _FallbackTheme.PARTY[h % len(_FallbackTheme.PARTY)]


def token(name: str) -> Any:
    """Return theme token ``name`` (``"BG0"``, ``"ACCENT"`` ...) with a spec fallback."""
    if _theme is not None and hasattr(_theme, name):
        return getattr(_theme, name)
    return getattr(_FallbackTheme, name)


def qcolor(spec: str, alpha: float | None = None) -> QColor:
    """Build a :class:`QColor` from a hex / ``rgba(...)`` token, optionally overriding alpha."""
    spec = spec.strip()
    if spec.startswith("rgba(") or spec.startswith("rgb("):
        parts = [p.strip() for p in spec[spec.index("(") + 1 : spec.rindex(")")].split(",")]
        r, g, b = (int(float(parts[i])) for i in range(3))
        a = float(parts[3]) if len(parts) > 3 else 1.0
        c = QColor(r, g, b)
        c.setAlphaF(a if a <= 1.0 else a / 255.0)
    else:
        c = QColor(spec)
    if alpha is not None:
        c.setAlphaF(max(0.0, min(1.0, alpha)))
    return c


FONT_FAMILIES: tuple[str, ...] = ("Segoe UI Variable Display", "Segoe UI", "Segoe UI Variable Text")


def make_font(px: float, *, weight: QFont.Weight = QFont.Weight.Normal, tabular: bool = False) -> QFont:
    """Return the app font at ``px`` pixels; ``tabular`` enables tabular digits when Qt allows."""
    f = QFont()
    f.setFamilies(list(FONT_FAMILIES))
    f.setPixelSize(max(6, int(round(px))))
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    if tabular:
        _set_tabular(f)
    return f


def _set_tabular(f: QFont) -> None:
    """Enable the ``tnum`` OpenType feature (Qt 6.7+); silently ignored on older Qt."""
    set_feature = getattr(f, "setFeature", None)
    if set_feature is None:
        return
    tag_cls = getattr(QFont, "Tag", None)
    candidates: list[Any] = []
    if tag_cls is not None:
        from_string = getattr(tag_cls, "fromString", None)
        if from_string is not None:
            try:
                tag = from_string("tnum")
                if tag is not None:
                    candidates.append(tag)
            except Exception:  # noqa: BLE001 - API shape varies between PySide builds
                pass
        try:
            candidates.append(tag_cls("tnum"))
        except Exception:  # noqa: BLE001
            pass
    candidates.append("tnum")
    for cand in candidates:
        try:
            set_feature(cand, 1)
            return
        except Exception:  # noqa: BLE001
            continue


# --------------------------------------------------------------------------- number formatting
def fmt_int(value: float | int) -> str:
    """``12345 -> "12,345"``."""
    try:
        return f"{int(round(value)):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_rate(value: float) -> str:
    """Per-second rate: one decimal below 1000, thousands separators above."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "0.0"
    if math.isnan(v) or math.isinf(v):
        return "0.0"
    if abs(v) < 1000:
        return f"{v:,.1f}"
    return f"{v:,.0f}"


def fmt_pct(fraction: float, *, decimals: int = 1) -> str:
    """``0.4234 -> "42.3%"``."""
    try:
        return f"{100.0 * float(fraction):.{decimals}f}%"
    except (TypeError, ValueError):
        return "0%"


def fmt_hit_pct(value: float) -> str:
    """Hit percentage as produced by ``stats`` (already 0..100); negative = unknown ("—")."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "0%"
    return "—" if value < 0 else f"{value:.0f}%"


def hit_pct_or_unknown(row: Any) -> float:
    """``row.hit_pct``, or -1 when the chat does not show this actor's misses."""
    return float(getattr(row, "hit_pct", 0.0) or 0.0) if getattr(row, "misses_shown", True) else -1.0


def actor_display_name(row: Any) -> str:
    """Presentation label, keeping ``name`` available for selections and actions."""
    return str(getattr(row, "display_name", None) or getattr(row, "name", ""))


def fmt_mmss(seconds: float) -> str:
    """``125.4 -> "02:05"`` (hours roll into the minutes field)."""
    try:
        s = max(0, int(seconds))
    except (TypeError, ValueError):
        s = 0
    return f"{s // 60:02d}:{s % 60:02d}"


# --------------------------------------------------------------------------- meter columns
@dataclass(frozen=True)
class _Column:
    """One meter column: ``key`` is the sort key the app persists."""

    key: str
    title: str
    align: Qt.AlignmentFlag
    width_sample: str  # text used to size fixed columns
    value: Callable[[Any, float], Any]  # (row, share_in_view) -> sortable number / name
    text: Callable[[Any], str]  # sortable value -> display text
    stretch: bool = False
    #: When the meter is too narrow, columns are hidden highest ``drop`` first; 0 = always shown.
    drop: int = 0


_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter


def _max_heal(row: Any) -> int:
    """Max heal when the model provides one, else the max hit (ActorRow has no max_heal)."""
    return int(getattr(row, "max_heal", None) or getattr(row, "max_hit", 0) or 0)


def _columns_for(metric: str) -> list[_Column]:
    """The meter columns of ``metric``.

    ``drop`` orders what a narrow meter hides first (rank, then the least important
    numbers); the name and the metric's own number always stay.
    """
    rank = _Column("rank", "#", _CENTER, "88", lambda r, s: 0, lambda v: str(v), drop=9)
    name = _Column("name", "Name", _LEFT, "", lambda r, s: actor_display_name(r), lambda v: str(v), stretch=True)

    def share(drop: int) -> _Column:
        return _Column("share", "%", _RIGHT, "100.0%", lambda r, s: float(s), fmt_pct, drop=drop)

    if metric == "overview":
        return [
            rank,
            name,
            _Column("dps", "DPS", _RIGHT, "8,888.8", lambda r, s: float(r.dps), fmt_rate),
            _Column("hps", "HPS", _RIGHT, "8,888.8", lambda r, s: float(r.hps), fmt_rate, drop=1),
            _Column("utility", "Utility", _RIGHT, "888", lambda r, s: int(getattr(r, "utility", 0) or 0), fmt_int, drop=2),
            _Column("deaths", "Deaths", _RIGHT, "88", lambda r, s: int(getattr(r, "deaths", 0) or 0),
                    lambda v: fmt_int(v) if v else "", drop=3),
            _Column("taken", "Taken", _RIGHT, "888,888", lambda r, s: int(getattr(r, "taken", 0) or 0), fmt_int, drop=4),
        ]
    if metric == "healing":
        return [
            rank,
            name,
            _Column("heals", "Heals", _RIGHT, "888,888", lambda r, s: int(r.heals), fmt_int),
            _Column("hps", "HPS", _RIGHT, "8,888.8", lambda r, s: float(r.hps), fmt_rate, drop=1),
            share(2),
            _Column("max", "Max", _RIGHT, "88,888", lambda r, s: _max_heal(r), fmt_int, drop=3),
        ]
    if metric == "taken":
        return [
            rank,
            name,
            _Column("taken", "Taken", _RIGHT, "888,888", lambda r, s: int(r.taken), fmt_int),
            _Column("dtps", "DTPS", _RIGHT, "8,888.8", lambda r, s: float(r.dtps), fmt_rate, drop=1),
            share(2),
        ]
    return [
        rank,
        name,
        _Column("damage", "Damage", _RIGHT, "888,888", lambda r, s: int(r.damage), fmt_int),
        _Column("dps", "DPS", _RIGHT, "8,888.8", lambda r, s: float(r.dps), fmt_rate, drop=1),
        share(3),
        _Column("hit_pct", "Hit%", _RIGHT, "100%", lambda r, s: hit_pct_or_unknown(r), fmt_hit_pct, drop=4),
        _Column("max", "Max", _RIGHT, "88,888", lambda r, s: int(r.max_hit), fmt_int, drop=2),
    ]


METRICS: tuple[str, ...] = ("overview", "damage", "healing", "taken")


METRIC_VALUE_KEY: dict[str, str] = {"overview": "dps", "damage": "damage", "healing": "heals", "taken": "taken"}
"""Default sort column (and bar value) per metric."""


def metric_value(row: Any, metric: str) -> float:
    """The number the bar represents for ``row`` under ``metric``."""
    if metric == "overview":
        return float(getattr(row, "dps", 0) or 0)
    if metric == "healing":
        return float(getattr(row, "heals", 0) or 0)
    if metric == "taken":
        return float(getattr(row, "taken", 0) or 0)
    return float(getattr(row, "damage", 0) or 0)


ROW_ROLE = Qt.ItemDataRole.UserRole + 1  #: the ActorRow object
SORT_ROLE = Qt.ItemDataRole.UserRole + 2  #: numeric (or name) sort value
FRACTION_ROLE = Qt.ItemDataRole.UserRole + 3  #: bar fraction 0..1
RANK_ROLE = Qt.ItemDataRole.UserRole + 4  #: 1-based rank by metric value
COLOR_ROLE = Qt.ItemDataRole.UserRole + 5  #: actor colour hex


TEXT_SORT_KEYS: frozenset[str] = frozenset({"name", "rank"})
"""Column keys whose first header click sorts ascending; every other column starts descending."""


def initial_sort_order(column_key: str) -> Qt.SortOrder:
    """The order a column gets on its first header click (``Qt.InitialSortOrderRole``)."""
    return Qt.SortOrder.AscendingOrder if column_key in TEXT_SORT_KEYS else Qt.SortOrder.DescendingOrder


class _MeterModel(QAbstractTableModel):
    """Table model over ``ActorRow`` objects; updated in place so sorting/selection survive."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._metric = "damage"
        self._columns: list[_Column] = _columns_for("damage")
        self._rows: list[Any] = []
        #: Optional ``row -> html`` for the Name column (the overlay's zone-wide summary).
        self.name_tooltip: Callable[[Any], str | None] | None = None
        self._values: list[float] = []
        self._shares: list[float] = []
        self._ranks: list[int] = []
        self._max_value = 0.0

    # -- introspection ---------------------------------------------------
    @property
    def columns(self) -> list[_Column]:
        return self._columns

    @property
    def metric(self) -> str:
        return self._metric

    def column_index(self, key: str) -> int:
        for i, col in enumerate(self._columns):
            if col.key == key:
                return i
        return -1

    def row_object(self, row: int) -> Any:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    # -- updates ---------------------------------------------------------
    def set_metric(self, metric: str) -> bool:
        """Switch the column set; returns True when the columns changed (model reset)."""
        if metric == self._metric:
            return False
        self.beginResetModel()
        self._metric = metric
        self._columns = _columns_for(metric)
        self._recompute()
        self.endResetModel()
        return True

    def set_rows(self, rows: Sequence[Any]) -> None:
        new_rows = list(rows)
        old_n, new_n = len(self._rows), len(new_rows)
        if new_n > old_n:
            self.beginInsertRows(QModelIndex(), old_n, new_n - 1)
            self._rows = new_rows
            self._recompute()
            self.endInsertRows()
        elif new_n < old_n:
            self.beginRemoveRows(QModelIndex(), new_n, old_n - 1)
            self._rows = new_rows
            self._recompute()
            self.endRemoveRows()
        else:
            self._rows = new_rows
            self._recompute()
        if new_n and old_n:
            common = min(old_n, new_n)
            self.dataChanged.emit(self.index(0, 0), self.index(common - 1, len(self._columns) - 1))

    def _recompute(self) -> None:
        metric = self._metric
        values = [metric_value(r, metric) for r in self._rows]
        total = sum(v for v in values if v > 0)
        if metric == "damage":
            shares = [float(getattr(r, "share", 0.0) or 0.0) for r in self._rows]
            if total > 0 and not any(shares):
                shares = [v / total for v in values]
        else:
            shares = [(v / total if total > 0 else 0.0) for v in values]
        order = sorted(range(len(values)), key=lambda i: (-values[i], str(self._rows[i].name).casefold()))
        ranks = [0] * len(values)
        for pos, i in enumerate(order):
            ranks[i] = pos + 1
        self._values, self._shares, self._ranks = values, shares, ranks
        self._max_value = max(values) if values else 0.0

    # -- QAbstractTableModel ---------------------------------------------
    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and 0 <= section < len(self._columns):
            if role == Qt.ItemDataRole.DisplayRole:
                return self._columns[section].title
            if role == Qt.ItemDataRole.InitialSortOrderRole:
                # QHeaderView asks this on the first click of a section: numbers sort
                # biggest-first (the top DPS is what the click is for), names A-Z.
                return initial_sort_order(self._columns[section].key)
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        r, c = index.row(), index.column()
        if not (0 <= r < len(self._rows) and 0 <= c < len(self._columns)):
            return None
        row = self._rows[r]
        col = self._columns[c]
        if role == ROW_ROLE:
            return row
        if role == RANK_ROLE:
            return self._ranks[r]
        if role == FRACTION_ROLE:
            return (self._values[r] / self._max_value) if self._max_value > 0 else 0.0
        if role == COLOR_ROLE:
            return _row_color(row)
        if role in (Qt.ItemDataRole.DisplayRole, SORT_ROLE):
            value: Any = self._ranks[r] if col.key == "rank" else col.value(row, self._shares[r])
            if role == SORT_ROLE:
                return value.casefold() if isinstance(value, str) else value
            return col.text(value)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(col.align)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col.key == "name" and self.name_tooltip is not None:
                try:
                    tip = self.name_tooltip(row)
                except Exception:  # noqa: BLE001 - a tooltip must never break the table
                    log.debug("name tooltip failed", exc_info=True)
                    tip = None
                if tip:
                    return tip
            return _cell_tooltip(row, col.key)
        return None


OUTSIDER_ALPHA = 0.55  #: how faded a player outside the group is drawn


def is_outsider(row: Any) -> bool:
    """A player (or pet) on the group's side who is not in the group: another group nearby,
    or a party member's pet the chat does not tie to its owner."""
    return (
        row is not None
        and not getattr(row, "in_group", True)
        and not getattr(row, "is_enemy", False)
        and not getattr(row, "is_npc", False)
    )


def _row_color(row: Any) -> str:
    color = getattr(row, "color", None)
    if color:
        return str(color)
    return str(
        token("actor_color")(
            str(row.name),
            is_you=bool(getattr(row, "is_you", False)),
            is_npc=bool(getattr(row, "is_npc", False)),
            is_pet=bool(getattr(row, "is_pet", False)),
        )
    )


_TIP_STYLE = "font-family:'Segoe UI'; font-size:12px;"


def _tip_table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(f"<th align='{'left' if i == 0 else 'right'}' style='padding:0 8px 2px 0;color:#9aa3b2'>{h}</th>" for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(f"<td align='{'left' if i == 0 else 'right'}' style='padding:0 8px 0 0'>{v}</td>" for i, v in enumerate(r)) + "</tr>"
        for r in rows
    )
    return f"<table cellspacing='0'><tr>{head}</tr>{body}</table>"


def _cell_tooltip(row: Any, key: str) -> str:
    """Hover breakdown for a meter cell: skills under damage columns, heal detail under
    heal columns, crowd-control detail under the CC column, the general summary elsewhere."""
    g = lambda name, default=0: getattr(row, name, default) or default  # noqa: E731
    title = f"<b>{html.escape(actor_display_name(row))}</b>"
    pets = getattr(row, "attributed_pets", ()) or ()
    if pets:
        title += f"<br><i>Includes {html.escape(', '.join(pets))}</i>"
    if getattr(row, "pet_owner", ""):
        title += f"<br><i>Pet of {html.escape(row.pet_owner)}</i>"
    if is_outsider(row):
        title += ("<br><i>Not in your group: not counted in the group total. "
                  "Right-click to count them (a party member's pet, say).</i>")
    elif getattr(row, "is_enemy", False) and not getattr(row, "is_npc", False):
        title += "<br><i>Enemy (fought your group)</i>"
    if key in ("damage", "dps", "hit_pct", "max", "share"):
        skills = list(g("skills", []) or [])
        lines = [
            [s.skill, f"{s.hits}/{s.hits + s.misses}", f"{s.total:,}", f"{s.max_hit:,}", f"{s.avg:,.1f}"]
            for s in skills[:12]
        ]
        swings = int(g("swings"))
        if getattr(row, "misses_shown", True):
            melee = f"Melee swings {swings}: {int(g('hits'))} hit, {int(g('misses'))} missed ({float(g('hit_pct')):.0f}%) "
        else:
            melee = f"Melee hits {int(g('hits'))} <i>(your combat chat does not show other players' misses, so their hit rate is unknown)</i> "
        summary = (
            f"Damage {int(g('damage')):,} &middot; {float(g('dps')):,.1f} DPS &middot; share {100 * float(g('share')):.1f}%<br>"
            + melee
            + f"&middot; max {int(g('max_hit')):,} &middot; avg {float(g('avg_hit')):,.1f}"
        )
        table = _tip_table(["Skill", "Hits", "Total", "Max", "Avg"], lines) if lines else ""
        estimated = int(g("estimated"))
        if estimated:
            summary += f"<br><i>{estimated} unreadable number{'s' if estimated != 1 else ''} filled with the zone average (Dummy Fix)</i>"
        return f"<div style=\"{_TIP_STYLE}\">{title}<br>{summary}{'<br>' + table if table else ''}</div>"
    if key in ("heals", "hps"):
        spells = list(g("heal_skills", []) or [])
        lines = [[s.skill, f"{s.hits}", f"{s.total:,}", f"{s.max_hit:,}", f"{s.avg:,.1f}"] for s in spells[:12]]
        summary = (
            f"Healing done {int(g('heals')):,} &middot; {float(g('hps')):,.1f} HPS &middot; biggest heal {int(g('max_heal')):,}<br>"
            f"Healing received {int(g('healed')):,}"
        )
        table = _tip_table(["Spell", "Casts", "Total", "Max", "Avg"], lines) if lines else ""
        return f"<div style=\"{_TIP_STYLE}\">{title}<br>{summary}{'<br>' + table if table else ''}</div>"
    if key == "deaths":
        killed_by = dict(g("killed_by", {}) or {})
        n = int(g("deaths"))
        if not n:
            return f"<div style=\"{_TIP_STYLE}\">{title}<br>Did not die in this fight</div>"
        lines = [[killer, f"{count}"] for killer, count in killed_by.items()]
        table = _tip_table(["Killed by", "Times"], lines) if lines else ""
        return f"<div style=\"{_TIP_STYLE}\">{title}<br>Died {n} time{'s' if n != 1 else ''}{'<br>' + table if table else ''}</div>"
    if key in ("taken", "dtps"):
        sources = list(g("taken_from", []) or [])
        lines = [[s.skill, f"{s.hits}", f"{s.total:,}", f"{s.max_hit:,}"] for s in sources[:12]]
        prevented = int(g("prevented"))
        summary = (
            f"Damage taken {int(g('taken')):,} &middot; {float(g('dtps')):,.1f} per second &middot; "
            f"prevented by blocks/absorbs {prevented:,} &middot; healing received {int(g('healed')):,}"
        )
        table = _tip_table(["Source", "Hits", "Total", "Max"], lines) if lines else ""
        return f"<div style=\"{_TIP_STYLE}\">{title}<br>{summary}{'<br>' + table if table else ''}</div>"
    if key in ("utility", "cc"):
        types = dict(g("cc_types", {}) or {})
        debuffs = dict(g("debuffs", {}) or {})
        skills = dict(g("cc_skills", {}) or {})
        skills.update({k: v for k, v in dict(g("debuff_skills", {}) or {}).items()})
        landed, tries, aggro = int(g("cc")), int(g("cc_attempts")), int(g("aggro"))
        rate = f" ({100 * landed / tries:.0f}%)" if tries else ""
        lines = [[f"CC: {cat}", f"{n}"] for cat, n in types.items()]
        lines += [[f"Debuff: {cat}", f"{n}"] for cat, n in debuffs.items()]
        taunts = int(g("taunts"))
        if aggro:
            lines.append(["Aggro: taunts" if taunts == aggro else "Aggro gained", f"{aggro}"])
        if taunts and taunts != aggro:
            lines.append(["  of which taunts", f"{taunts}"])
        table = _tip_table(["Effect", "Landed"], lines) if lines else ""
        by_skill = _tip_table(["Ability", "Landed"], [[name, f"{n}"] for name, n in list(skills.items())[:10]]) if skills else ""
        summary = (
            f"Utility {int(g('utility'))}: crowd control {landed} of {tries} tries{rate}, "
            f"debuffs {sum(debuffs.values())}, aggro {aggro}"
        )
        parts = [p for p in (table, by_skill) if p]
        return f"<div style=\"{_TIP_STYLE}\">{title}<br>{summary}{'<br>' + '<br>'.join(parts) if parts else ''}</div>"
    return _row_tooltip(row)


def zone_tooltip(row: Any, *, zone: str, fights: int, combat_s: float, of: int | None = None) -> str:
    """Hover card for a name: that person over every fight of the zone visit.

    ``fights`` is how many fights the person took part in (``of`` the visit's total)."""
    g = lambda name, default=0: getattr(row, name, default) or default  # noqa: E731
    where = zone or "this zone"
    total = f" of {of}" if of is not None and of != fights else ""
    head = (
        f"<b>{html.escape(actor_display_name(row))}</b> &middot; {html.escape(where)}<br>"
        f"in {fights}{total} fight{'s' if (of or fights) != 1 else ''}, {fmt_mmss(combat_s)} in combat"
    )
    lines = [
        ["Damage", f"{int(g('damage')):,}", f"{float(g('dps')):,.1f} DPS"],
        ["Healing", f"{int(g('heals')):,}", f"{float(g('hps')):,.1f} HPS"],
        ["Taken", f"{int(g('taken')):,}", f"{int(g('prevented')):,} prevented"],
        ["Utility", f"{int(g('utility'))}", f"{int(g('cc'))} CC, {sum(dict(g('debuffs', {}) or {}).values())} debuffs, {int(g('aggro'))} aggro"],
        ["Melee", f"{int(g('hits'))}/{int(g('swings'))}",
         (f"{float(g('hit_pct')):.0f}% hit" if getattr(row, "misses_shown", True) else "hit rate unknown")
         + f", max {int(g('max_hit')):,}"],
    ]
    skills = [[s.skill, f"{s.hits}", f"{s.total:,}", f"{s.max_hit:,}"] for s in list(g("skills", []) or [])[:8]]
    parts = [_tip_table(["", "Total", ""], lines)]
    if skills:
        parts.append(_tip_table(["Top abilities", "Hits", "Total", "Max"], skills))
    return f"<div style=\"{_TIP_STYLE}\">{head}<br>{'<br>'.join(parts)}</div>"


def _row_tooltip(row: Any) -> str:
    g = lambda k, d=0: getattr(row, k, d)  # noqa: E731
    return (
        f"{actor_display_name(row)}\n"
        f"Damage {fmt_int(g('damage'))}  ({fmt_rate(g('dps'))} dps)\n"
        f"Taken {fmt_int(g('taken'))}  Heals {fmt_int(g('heals'))}  Healed {fmt_int(g('healed'))}\n"
        f"Swings {g('swings')}  Hits {g('hits')}  Misses {g('misses')}  Max {fmt_int(g('max_hit'))}"
    )


class _MeterProxy(QSortFilterProxyModel):
    """Numeric sort (names case-insensitively), ties broken by name for a stable order."""

    def lessThan(self, left: QModelIndex | QPersistentModelIndex, right: QModelIndex | QPersistentModelIndex) -> bool:  # noqa: N802
        a = left.data(SORT_ROLE)
        b = right.data(SORT_ROLE)
        if isinstance(a, str) or isinstance(b, str):
            sa, sb = str(a or ""), str(b or "")
            if sa != sb:
                return sa < sb
        else:
            try:
                fa, fb = float(a or 0), float(b or 0)
            except (TypeError, ValueError):
                fa, fb = 0.0, 0.0
            if fa != fb:
                return fa < fb
        # tie-break: name, flipped so that "descending" still lists ties alphabetically
        model = self.sourceModel()
        na = str(getattr(model.data(left, ROW_ROLE), "name", "")).casefold()
        nb = str(getattr(model.data(right, ROW_ROLE), "name", "")).casefold()
        if self.sortOrder() == Qt.SortOrder.DescendingOrder:
            return na > nb
        return na < nb


class _MeterHeader(QHeaderView):
    """Flat, transparent header with muted upper-case labels and a small sort triangle."""

    def __init__(self, columns_provider: Callable[[], list[_Column]], parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._columns = columns_provider
        self._hover = -1
        # Painted with an explicit font: a style-sheet ``font-size`` rule on an ancestor
        # (e.g. theme.overlay_qss) would otherwise pin ``self.font()`` and defeat scaling.
        self.label_font: QFont = make_font(11, weight=QFont.Weight.DemiBold)
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(True)
        self.setHighlightSections(False)
        self.setStretchLastSection(False)
        self.setDefaultAlignment(_LEFT)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QHeaderView { background: transparent; border: none; } QHeaderView::section { background: transparent; border: none; }")

    def set_label_font(self, font: QFont) -> None:
        """Set the font used to paint the column titles."""
        self.label_font = QFont(font)
        self.viewport().update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        idx = self.logicalIndexAt(event.position().toPoint())
        if idx != self._hover:
            self._hover = idx
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        self._hover = -1
        self.viewport().update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        super().paintEvent(event)
        # bottom hairline under the whole header
        p = QPainter(self.viewport())
        p.setPen(QPen(qcolor(token("LINE")), 1))
        y = self.viewport().height() - 1
        p.drawLine(0, y, self.viewport().width(), y)
        p.end()

    def paintSection(self, painter: QPainter, rect: QRect, logical_index: int) -> None:  # noqa: N802
        cols = self._columns()
        if not (0 <= logical_index < len(cols)):
            return
        col = cols[logical_index]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        sorted_here = self.sortIndicatorSection() == logical_index
        if logical_index == self._hover:
            painter.fillRect(rect, qcolor("#ffffff", 0.05))
        color = qcolor(token("ACCENT")) if sorted_here else qcolor(token("MUTED"))
        painter.setPen(color)
        painter.setFont(self.label_font)
        label = col.title if col.title in ("#", "%") else col.title.upper()
        pad = 6
        text_rect = QRectF(rect).adjusted(pad, 0, -pad, 0)
        arrow_w = 8.0
        if sorted_here:
            if col.align & Qt.AlignmentFlag.AlignRight:
                text_rect.adjust(0, 0, -(arrow_w + 3), 0)
            elif col.align & Qt.AlignmentFlag.AlignLeft:
                text_rect.adjust(arrow_w + 3, 0, 0, 0)
        fm = QFontMetricsF(self.label_font)
        elided = fm.elidedText(label, Qt.TextElideMode.ElideRight, text_rect.width())
        painter.drawText(text_rect, int(col.align), elided)
        if sorted_here:
            tw = fm.horizontalAdvance(elided)
            if col.align & Qt.AlignmentFlag.AlignRight:
                ax = text_rect.right() + 3
            elif col.align & Qt.AlignmentFlag.AlignLeft:
                ax = text_rect.left() - arrow_w - 3
            else:
                ax = text_rect.center().x() + tw / 2 + 3
            cy = rect.center().y() + 0.5
            path = QPainterPath()
            if self.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder:
                path.moveTo(ax, cy - 2)
                path.lineTo(ax + arrow_w, cy - 2)
                path.lineTo(ax + arrow_w / 2, cy + 3)
            else:
                path.moveTo(ax, cy + 3)
                path.lineTo(ax + arrow_w, cy + 3)
                path.lineTo(ax + arrow_w / 2, cy - 2)
            path.closeSubpath()
            painter.fillPath(path, color)
        painter.restore()


class _MeterDelegate(QStyledItemDelegate):
    """Paints the per-row gradient bar (sliced per cell, seamless) and the cell text."""

    def __init__(self, view: "_MeterView", compact: bool) -> None:
        super().__init__(view)
        self._view = view
        self._compact = compact
        self.font_px = 13.0 if compact else 14.0
        self.set_font_px(self.font_px)

    def set_font_px(self, px: float) -> None:
        self.font_px = px
        self._text_font = make_font(px, tabular=True)
        self._bold_font = make_font(px, weight=QFont.Weight.DemiBold, tabular=True)
        self._muted_font = make_font(px * 0.92, tabular=True)
        self._outsider_font = make_font(px, tabular=True)
        self._outsider_font.setItalic(True)

    @property
    def row_height(self) -> int:
        return int(round(self.font_px * (1.8 if self._compact else 2.15)))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        return QSize(40, self.row_height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> None:
        model = index.model()
        col_idx = index.column()
        columns = self._view.columns()
        if not (0 <= col_idx < len(columns)):
            return
        col = columns[col_idx]
        row_obj = index.data(ROW_ROLE)
        frac = float(index.data(FRACTION_ROLE) or 0.0)
        rank = int(index.data(RANK_ROLE) or 0)
        color = qcolor(str(index.data(COLOR_ROLE) or token("TEXT")))
        outsider = is_outsider(row_obj)
        if outsider:  # outside the group: drawn faded (not counted in the totals)
            color.setAlphaF(OUTSIDER_ALPHA)
        cell = QRectF(option.rect)
        vp_w = self._view.viewport().width()
        row_rect = QRectF(0.0, cell.top(), float(vp_w), cell.height())
        hovered = index.row() == self._view.hovered_row
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setClipRect(cell)

        # hover lightening (6 %)
        if hovered:
            painter.fillRect(row_rect, qcolor("#ffffff", 0.06))

        # gradient bar
        inset_y = 2.0 if self._compact else 3.0
        bar_rect = QRectF(row_rect.left() + 2.0, row_rect.top() + inset_y, 0.0, row_rect.height() - 2 * inset_y)
        bar_w = max(0.0, frac) * (row_rect.width() - 4.0)
        radius = 5.0 if self._compact else 6.0
        if bar_w >= 2.0:
            bar_rect.setWidth(bar_w)
            grad = QLinearGradient(bar_rect.topLeft(), bar_rect.topRight())
            fade = OUTSIDER_ALPHA if outsider else 1.0
            c0 = QColor(color)
            c0.setAlphaF(0.45 * fade)
            c1 = QColor(color)
            c1.setAlphaF(0.20 * fade)
            grad.setColorAt(0.0, c0)
            grad.setColorAt(1.0, c1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(bar_rect, radius, radius)
            if rank == 1:
                outline = QColor(color)
                outline.setAlphaF(0.75)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.setPen(QPen(outline, 1.0))
                painter.drawRoundedRect(bar_rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)

        # selection = subtle outline around the whole row
        if selected:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(qcolor(token("ACCENT"), 0.6), 1.0))
            painter.drawRoundedRect(row_rect.adjusted(1.5, 1.5, -1.5, -1.5), radius, radius)

        # text
        pad = 7.0
        text_rect = cell.adjusted(pad, 0, -pad, 0)
        if col.key == "rank":
            painter.setFont(self._muted_font)
            painter.setPen(qcolor(token("MUTED")))
        elif col.key == "name":
            is_you = bool(getattr(row_obj, "is_you", False))
            painter.setFont(self._bold_font if is_you else self._outsider_font if outsider else self._text_font)
            painter.setPen(color)
        else:
            painter.setFont(self._text_font)
            painter.setPen(qcolor(token("MUTED")) if outsider else qcolor(token("TEXT")))
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if col.key == "name" and bool(getattr(row_obj, "is_pet", False)):
            owner = getattr(row_obj, "pet_owner", "")
            text = f"{text}  · {owner}'s pet" if owner else f"{text}  ·pet"
        fm = QFontMetricsF(painter.font())
        painter.drawText(text_rect, int(col.align), fm.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width()))
        painter.restore()
        _ = model  # keep the reference explicit for readers


class _MeterView(QTableView):
    """The table view: transparent, no grid, row hover tracking."""

    def __init__(self, columns_provider: Callable[[], list[_Column]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._columns_provider = columns_provider
        self.hovered_row = -1
        #: Called after the viewport changed size (the owner re-fits its columns).
        self.on_viewport_resized: Callable[[], None] | None = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setFrameShape(QTableView.Shape.NoFrame)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setAlternatingRowColors(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalHeader().setVisible(False)
        self.setStyleSheet(
            "QTableView { background: transparent; border: none; selection-background-color: transparent; "
            "selection-color: palette(text); outline: 0; }"
            "QTableView::item { background: transparent; border: none; padding: 0px; }"
            "QTableView::item:selected { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )

    def columns(self) -> list[_Column]:
        return self._columns_provider()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        idx = self.indexAt(event.position().toPoint())
        row = idx.row() if idx.isValid() else -1
        if row != self.hovered_row:
            self.hovered_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802
        if self.hovered_row != -1:
            self.hovered_row = -1
            self.viewport().update()
        super().leaveEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        # QAbstractScrollArea delivers the *viewport's* resize events here.
        super().resizeEvent(event)
        if self.on_viewport_resized is not None:
            self.on_viewport_resized()
        self.viewport().update()  # bars span the viewport width


class MeterTable(QWidget):
    """The DPS meter table (APP_SPEC section 7).

    Args:
        compact: overlay density (smaller font, tighter rows, no scrollbar).
        parent: Qt parent.

    Signals:
        sort_changed(str, bool): the user clicked a header; ``(column_key, descending)``.
        row_selected(object): the selected ``ActorRow`` (or ``None`` when cleared).
    """

    sort_changed = Signal(str, bool)
    row_selected = Signal(object)

    def __init__(self, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._compact = compact
        self._font_scale = 1.0
        self._base_px = 13.0 if compact else 14.0
        self._sort_key = "damage"
        self._sort_desc = True
        self._selected_name: str | None = None
        self._applying_sort = False

        self._model = _MeterModel(self)
        self._proxy = _MeterProxy(self)
        self._proxy.setSourceModel(self._model)
        self._proxy.setSortRole(SORT_ROLE)
        self._proxy.setDynamicSortFilter(True)

        self._view = _MeterView(lambda: self._model.columns, self)
        self._header = _MeterHeader(lambda: self._model.columns, self._view)
        self._view.setHorizontalHeader(self._header)
        self._delegate = _MeterDelegate(self._view, compact)
        self._view.setItemDelegate(self._delegate)
        self._view.setModel(self._proxy)
        self._view.on_viewport_resized = self._fit_columns
        self._view.setSortingEnabled(True)
        self._view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if compact else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._header.sortIndicatorChanged.connect(self._on_sort_indicator)
        self._view.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self._view.clicked.connect(self._on_clicked)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._view)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._apply_fonts()
        self._apply_sort()

    # -- public API ------------------------------------------------------
    @property
    def view(self) -> QTableView:
        """The underlying view (pages may tweak scroll policy etc.)."""
        return self._view

    @property
    def metric(self) -> str:
        return self._model.metric

    @property
    def sort(self) -> tuple[str, bool]:
        """``(column_key, descending)`` currently applied."""
        return self._sort_key, self._sort_desc

    def set_rows(self, rows: Sequence[Any], metric: str = "damage") -> None:
        """Replace the rows; ``metric`` is ``"damage" | "healing" | "taken"``."""
        metric = metric if metric in METRIC_VALUE_KEY else "damage"
        columns_changed = self._model.set_metric(metric)
        self._model.set_rows(rows)
        if columns_changed:
            self._apply_column_widths()
            self._apply_sort()
        self._restore_selection()
        self._view.viewport().update()

    def set_name_tooltip_provider(self, provider: Callable[[Any], str | None] | None) -> None:
        """Tooltip for the Name column (``None`` falls back to the row summary)."""
        self._model.name_tooltip = provider

    def set_sort(self, column_key: str, descending: bool) -> None:
        """Sort by ``column_key`` (falls back to the metric column when unknown)."""
        self._sort_key = column_key
        self._sort_desc = bool(descending)
        self._apply_sort()

    def set_font_scale(self, scale: float) -> None:
        """Scale every font and the row height by ``scale`` (0.6..2.0)."""
        self._font_scale = max(0.6, min(2.0, float(scale)))
        self._apply_fonts()

    def row_at(self, global_pos: QPoint) -> Any | None:
        """The ``ActorRow`` under ``global_pos`` (screen coordinates), or ``None``."""
        viewport = self._view.viewport()
        local = viewport.mapFromGlobal(global_pos)
        if not viewport.rect().contains(local):
            return None
        idx = self._view.indexAt(local)
        if not idx.isValid():
            return None
        return self._model.row_object(self._proxy.mapToSource(idx).row())

    def selected_row(self) -> Any | None:
        """The currently selected ``ActorRow`` or ``None``."""
        sel = self._view.selectionModel().selectedRows()
        if not sel:
            return None
        src = self._proxy.mapToSource(sel[0])
        return self._model.row_object(src.row())

    def clear_selection(self) -> None:
        self._selected_name = None
        self._view.clearSelection()

    def row_count(self) -> int:
        return self._model.rowCount()

    # -- internals -------------------------------------------------------
    def _apply_fonts(self) -> None:
        px = self._base_px * self._font_scale
        self._delegate.set_font_px(px)
        self._header.set_label_font(make_font(px * 0.82, weight=QFont.Weight.DemiBold))
        self._header.setFixedHeight(int(round(px * 1.7)))
        vh = self._view.verticalHeader()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        h = self._delegate.row_height
        vh.setMinimumSectionSize(h)  # before the default: the default is clamped to the minimum
        vh.setDefaultSectionSize(h)
        for r in range(vh.count()):
            vh.resizeSection(r, h)
        self._apply_column_widths()
        self._view.viewport().update()

    def _column_width(self, col: _Column) -> int:
        """Fixed width of a non-stretch column at the current font size."""
        fm = QFontMetricsF(make_font(self._base_px * self._font_scale, tabular=True))
        hfm = QFontMetricsF(self._header.label_font)
        # room for the label, the sort triangle (8 px + 3 px gap) and the cell padding
        w = max(fm.horizontalAdvance(col.width_sample), hfm.horizontalAdvance(col.title.upper()) + 11 + 4) + 16
        return int(math.ceil(w))

    def name_min_width(self) -> int:
        """Narrowest the Name column may get before more columns are hidden."""
        return int(math.ceil((84 if self._compact else 110) * self._font_scale))

    def minimum_width(self) -> int:
        """Width that shows the name and the main number of every metric tab.

        Narrower than the full column set: :meth:`_fit_columns` hides the optional
        columns (``drop`` > 0) as the meter gets narrower, so nothing is ever clipped.
        """
        need = 0
        for metric in METRICS:
            fixed = sum(self._column_width(c) for c in _columns_for(metric) if not c.stretch and c.drop == 0)
            need = max(need, fixed)
        return need + self.name_min_width() + 8

    def _apply_column_widths(self) -> None:
        for i, col in enumerate(self._model.columns):
            if col.stretch:
                self._header.setSectionResizeMode(i, QHeaderView.ResizeMode.Stretch)
                continue
            self._header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
            self._header.resizeSection(i, self._column_width(col))
        self._header.setMinimumSectionSize(20)
        self._fit_columns()

    def _fit_columns(self) -> None:
        """Hide optional columns (highest ``drop`` first) until the name column has room."""
        columns = self._model.columns
        available = self._view.viewport().width()
        if available <= 0:
            return
        widths = {i: self._column_width(c) for i, c in enumerate(columns) if not c.stretch}
        hidden: set[int] = set()
        optional = sorted((i for i, c in enumerate(columns) if c.drop > 0), key=lambda i: -columns[i].drop)
        need = sum(widths.values()) + self.name_min_width()
        for i in optional:
            if need <= available:
                break
            hidden.add(i)
            need -= widths[i]
        for i in range(len(columns)):
            if self._view.isColumnHidden(i) != (i in hidden):
                self._view.setColumnHidden(i, i in hidden)

    def minimum_height(self, rows: int = 3) -> int:
        """Height that shows the header and ``rows`` full rows."""
        return self._header.height() + rows * self._delegate.row_height + 4

    def hidden_columns(self) -> list[str]:
        """Keys of the columns currently hidden for lack of width."""
        return [c.key for i, c in enumerate(self._model.columns) if self._view.isColumnHidden(i)]


    def _apply_sort(self) -> None:
        col = self._model.column_index(self._sort_key)
        if col < 0:
            self._sort_key = METRIC_VALUE_KEY.get(self._model.metric, "damage")
            self._sort_desc = True
            col = self._model.column_index(self._sort_key)
        order = Qt.SortOrder.DescendingOrder if self._sort_desc else Qt.SortOrder.AscendingOrder
        self._applying_sort = True
        try:
            self._view.sortByColumn(col, order)
            self._header.setSortIndicator(col, order)
        finally:
            self._applying_sort = False

    def _on_sort_indicator(self, section: int, order: Qt.SortOrder) -> None:
        if self._applying_sort:
            return
        cols = self._model.columns
        if not (0 <= section < len(cols)):
            return
        self._sort_key = cols[section].key
        self._sort_desc = order == Qt.SortOrder.DescendingOrder
        log.debug("meter sort -> %s %s", self._sort_key, "desc" if self._sort_desc else "asc")
        self.sort_changed.emit(self._sort_key, self._sort_desc)

    def _on_selection_changed(self, *_: Any) -> None:
        row = self.selected_row()
        name = getattr(row, "name", None)
        if name != self._selected_name:
            self._selected_name = name
            self.row_selected.emit(row)

    def _on_clicked(self, index: QModelIndex) -> None:
        row = self.selected_row()
        if row is not None and getattr(row, "name", None) == self._selected_name:
            self.row_selected.emit(row)

    def _restore_selection(self) -> None:
        if self._selected_name is None:
            return
        for r in range(self._proxy.rowCount()):
            idx = self._proxy.index(r, 0)
            row = idx.data(ROW_ROLE)
            if getattr(row, "name", None) == self._selected_name:
                sm = self._view.selectionModel()
                if not sm.isRowSelected(r, QModelIndex()):
                    sm.blockSignals(True)
                    self._view.selectRow(r)
                    sm.blockSignals(False)
                return


# --------------------------------------------------------------------------- feed view
HIT_KINDS: frozenset[str] = frozenset({"melee_hit", "ability_hit"})


@dataclass
class FeedEntry:
    """One feed line kept by :class:`FeedView` for re-filtering."""

    text: str
    kind: str
    is_player_action: bool
    ts: float
    is_player_target: bool | None = None  #: parsed target == player; ``None`` = unknown (word scan)


def feed_color(text: str, kind: str, is_player_action: bool, is_player_target: bool | None = None) -> str:
    """Hex colour for a feed line following the KIND_COLORS rules of APP_SPEC section 4.

    ``is_player_target`` comes from the parsed event (target is YOU or the configured
    player name); when ``None`` the text is scanned for the words YOU/YOUR.  Delegates
    to ``theme.feed_color`` when the theme provides it.
    """
    targets_you = _targets_you(text) if is_player_target is None else bool(is_player_target)
    theme_fn = getattr(_theme, "feed_color", None) if _theme is not None else None
    if callable(theme_fn):
        try:
            return str(theme_fn(kind, is_player_action=is_player_action, is_player_target=targets_you))
        except TypeError:  # older signature; fall through to the local rules
            pass
    if kind in HIT_KINDS:
        if is_player_action:
            return str(token("YOU"))
        if targets_you:
            return str(token("DANGER"))
        return str(token("TEXT"))
    colors: dict[str, str] = token("KIND_COLORS")
    return str(colors.get(kind, colors.get("unknown", "#6b7280")))


def _targets_you(text: str) -> bool:
    words = text.replace(",", " ").replace(".", " ").replace("!", " ").split()
    return any(w in ("YOU", "YOUR") for w in words)


class FeedView(QWidget):
    """Colour-coded combat message feed.

    Args:
        max_lines: how many lines are kept (``cfg.feed_max_lines``; 12 for the overlay).
        compact: overlay density (smaller font, no scrollbar, no timestamps).
        parent: Qt parent.
    """

    def __init__(self, max_lines: int = 500, compact: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._compact = compact
        self._max_lines = max(1, int(max_lines))
        self._entries: deque[FeedEntry] = deque(maxlen=self._max_lines)
        #: per entry: does it have a block in the document (it passed the filter when added)
        self._shown: deque[bool] = deque(maxlen=self._max_lines)
        self._stale = False  #: entries arrived while hidden: rebuild when shown
        self._filter: set[str] | None = None
        self._search = ""
        self._font_scale = 1.0
        self._base_px = 13.0 if compact else 14.0

        self._edit = QTextEdit(self)
        self._edit.setReadOnly(True)
        self._edit.setFrameShape(QTextEdit.Shape.NoFrame)
        self._edit.setUndoRedoEnabled(False)
        self._edit.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self._edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self._edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._edit.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff if compact else Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._edit.viewport().setAutoFillBackground(False)
        self._edit.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._edit.setFocusPolicy(Qt.FocusPolicy.NoFocus if compact else Qt.FocusPolicy.ClickFocus)
        self._edit.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction if compact else Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._edit.document().setDocumentMargin(4 if compact else 8)
        self._edit.setStyleSheet(
            "QTextEdit { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            "QScrollBar::handle:vertical { background: rgba(255,255,255,0.18); border-radius: 3px; min-height: 20px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self._edit)
        self._apply_font()

    # -- public API ------------------------------------------------------
    def append(
        self,
        text: str,
        kind: str,
        is_player_action: bool,
        ts: float | None = None,
        is_player_target: bool | None = None,
    ) -> None:
        """Add a line; keeps at most ``max_lines`` and auto-scrolls unless the user scrolled up.

        ``is_player_target`` is the parsed "target is the player" flag; ``None`` makes the
        colouring fall back to scanning the text for YOU/YOUR.
        """
        entry = FeedEntry(
            str(text),
            str(kind),
            bool(is_player_action),
            float(ts if ts is not None else time.time()),
            None if is_player_target is None else bool(is_player_target),
        )
        dropped_shown = len(self._entries) == self._entries.maxlen and bool(self._shown and self._shown[0])
        self._entries.append(entry)
        visible = self._visible(entry)
        self._shown.append(visible)
        if not self.isVisible():
            self._stale = True  # nobody sees the document: bring it up to date when shown
            return
        if self._compact:
            self._rebuild()  # a dozen lines: rebuilding is cheap and keeps the overlay simple
            return
        if not (dropped_shown or visible):
            return
        # One block in and (at capacity) one block out.  Re-rendering all 500 lines on every
        # message cost the GUI thread 20-70 ms a line and made the overlay animations stutter.
        stick = self._at_bottom()
        bar = self._edit.verticalScrollBar()
        value, removed = bar.value(), 0.0
        if dropped_shown:
            if not stick:  # keep what the reader looks at in place when the top line goes
                doc = self._edit.document()
                removed = doc.documentLayout().blockBoundingRect(doc.firstBlock()).height()
            self._remove_first_block()
        if visible:
            self._append_html(self._render(entry))
        if stick:
            self._scroll_to_bottom()
        else:
            bar.setValue(max(0, int(round(value - removed))))

    def set_filter(self, kinds: Iterable[str] | None) -> None:
        """Show only the given kinds (``None`` = everything)."""
        self._filter = None if kinds is None else set(kinds)
        self._rebuild()

    def set_search(self, needle: str) -> None:
        """Show only lines containing ``needle`` (case-insensitive) and highlight it."""
        self._search = (needle or "").strip()
        self._rebuild()

    def set_max_lines(self, max_lines: int) -> None:
        self._max_lines = max(1, int(max_lines))
        self._entries = deque(self._entries, maxlen=self._max_lines)
        self._shown = deque(self._shown, maxlen=self._max_lines)
        self._rebuild()

    def set_font_scale(self, scale: float) -> None:
        self._font_scale = max(0.6, min(2.0, float(scale)))
        self._apply_font()
        self._rebuild()

    def clear(self) -> None:
        self._entries.clear()
        self._shown.clear()
        self._stale = False
        self._edit.clear()

    def entries(self) -> list[FeedEntry]:
        """A copy of the kept entries (oldest first)."""
        return list(self._entries)

    def all_text(self) -> str:
        """Plain text of every kept line (for "Copy all")."""
        return "\n".join(f"{_hms(e.ts)}  {e.text}" for e in self._entries)

    # -- internals -------------------------------------------------------
    def _apply_font(self) -> None:
        self._edit.setFont(make_font(self._base_px * self._font_scale, tabular=True))

    def _visible(self, e: FeedEntry) -> bool:
        if self._filter is not None and e.kind not in self._filter:
            return False
        if self._search and self._search.casefold() not in e.text.casefold():
            return False
        return True

    def _render(self, e: FeedEntry) -> str:
        color = feed_color(e.text, e.kind, e.is_player_action, e.is_player_target)
        body = html.escape(e.text)
        if self._search:
            body = _highlight(body, html.escape(self._search), str(token("ACCENT")))
        ts = "" if self._compact else f'<span style="color:{token("MUTED")};">{_hms(e.ts)}</span>&nbsp; '
        px = max(6, int(round(self._base_px * self._font_scale)))
        families = ", ".join(f"'{f}'" for f in FONT_FAMILIES)
        return (
            f'<div style="color:{color}; font-size:{px}px; font-family:{families}; white-space:pre-wrap;">'
            f"{ts}{body}</div>"
        )

    def _at_bottom(self) -> bool:
        bar = self._edit.verticalScrollBar()
        return bar.value() >= bar.maximum() - 2

    def _scroll_to_bottom(self) -> None:
        bar = self._edit.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _append_html(self, fragment: str) -> None:
        # ``insertHtml`` merges a ``<div>`` fragment into the block the cursor is in, so
        # every entry starts its own block explicitly (``setHtml`` in ``_rebuild`` does
        # that by itself; this path must match it or lines run together).
        # A cursor of its own: moving the edit's cursor would scroll to the end and drop the
        # reader's selection on every line.
        doc = self._edit.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if not doc.isEmpty():
            cursor.insertBlock()
        cursor.insertHtml(fragment)

    def _remove_first_block(self) -> None:
        doc = self._edit.document()
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        if doc.blockCount() > 1:
            cursor.movePosition(QTextCursor.MoveOperation.NextBlock, QTextCursor.MoveMode.KeepAnchor)
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()

    def _rebuild(self) -> None:
        stick = self._compact or self._at_bottom()
        self._shown = deque((self._visible(e) for e in self._entries), maxlen=self._max_lines)
        parts = [self._render(e) for e, shown in zip(self._entries, self._shown) if shown]
        self._edit.setHtml("".join(parts))
        self._stale = False
        if stick:
            self._scroll_to_bottom()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if self._stale:
            self._rebuild()
            self._scroll_to_bottom()


def _hms(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (OverflowError, OSError, ValueError):
        return "--:--:--"


def _highlight(escaped: str, needle: str, color: str) -> str:
    """Wrap case-insensitive occurrences of ``needle`` in a highlight span."""
    out: list[str] = []
    low = escaped.casefold()
    n = needle.casefold()
    i = 0
    while True:
        j = low.find(n, i)
        if j < 0:
            out.append(escaped[i:])
            break
        out.append(escaped[i:j])
        out.append(f'<span style="background:{color}; color:#0f1117; border-radius:3px;">{escaped[j:j + len(needle)]}</span>')
        i = j + len(needle)
    return "".join(out)


# --------------------------------------------------------------------------- toggle switch
class ToggleSwitch(QAbstractButton):
    """Animated iOS-style switch (checkable button, 44x24)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._offset = 0.0
        self._anim = QPropertyAnimation(self, b"offset", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.toggled.connect(self._animate)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def _get_offset(self) -> float:
        return self._offset

    def _set_offset(self, value: float) -> None:
        self._offset = float(value)
        self.update()

    offset = Property(float, _get_offset, _set_offset)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(44, 24)

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self._anim.stop()
        self._offset = 1.0 if checked else 0.0
        self.update()

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        h = r.height()
        on = qcolor(token("ACCENT"))
        off = qcolor(token("BG2"))
        t = self._offset
        track = QColor(
            int(off.red() + (on.red() - off.red()) * t),
            int(off.green() + (on.green() - off.green()) * t),
            int(off.blue() + (on.blue() - off.blue()) * t),
        )
        if not self.isEnabled():
            track.setAlphaF(0.5)
        p.setPen(QPen(qcolor(token("LINE")), 1))
        p.setBrush(track)
        p.drawRoundedRect(r, h / 2, h / 2)
        knob_d = h - 6
        x = r.left() + 3 + (r.width() - 6 - knob_d) * t
        knob = QRectF(x, r.top() + 3, knob_d, knob_d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#0f1117") if t > 0.5 else qcolor(token("TEXT")))
        p.drawEllipse(knob)
        if self.hasFocus():
            p.setPen(QPen(qcolor(token("ACCENT"), 0.5), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), h / 2, h / 2)
        p.end()


# --------------------------------------------------------------------------- slider row
class SliderRow(QWidget):
    """``label  [=====o=====]  value`` form row.

    Args:
        label: caption on the left.
        minimum, maximum, value: integer slider range and initial value.
        formatter: turns the integer into the value text (default ``str``).
        compact: tighter layout for the overlay toolbar.

    Signals:
        value_changed(int): emitted whenever the slider moves.
    """

    value_changed = Signal(int)

    def __init__(
        self,
        label: str,
        minimum: int,
        maximum: int,
        value: int,
        *,
        formatter: Callable[[int], str] | None = None,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fmt = formatter or str
        self._label = QLabel(label, self)
        self._slider = QSlider(Qt.Orientation.Horizontal, self)
        self._slider.setRange(int(minimum), int(maximum))
        self._slider.setValue(int(value))
        self._slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self._slider.setFocusPolicy(Qt.FocusPolicy.NoFocus if compact else Qt.FocusPolicy.ClickFocus)
        self._value = QLabel(self._fmt(int(value)), self)
        self._value.setAlignment(_RIGHT)
        self._value.setFont(make_font(12 if compact else 13, tabular=True))
        self._value.setMinimumWidth(max(34, QFontMetricsF(self._value.font()).horizontalAdvance(self._fmt(int(maximum))) + 6))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6 if compact else 10)
        lay.addWidget(self._label)
        lay.addWidget(self._slider, 1)
        lay.addWidget(self._value)
        if compact:
            self._label.setFont(make_font(12))
            self._slider.setFixedWidth(90)
        self._label.setStyleSheet(f"color: {token('MUTED')}; background: transparent;")
        self._value.setStyleSheet(f"color: {token('TEXT')}; background: transparent;")
        self._slider.setStyleSheet(
            "QSlider { background: transparent; }"
            "QSlider::groove:horizontal { height: 4px; border-radius: 2px; background: rgba(255,255,255,0.14); }"
            f"QSlider::sub-page:horizontal {{ background: {token('ACCENT')}; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; "
            f"background: {token('TEXT')}; border: 1px solid rgba(0,0,0,0.4); }}"
            f"QSlider::handle:horizontal:hover {{ background: #ffffff; }}"
        )
        self._slider.valueChanged.connect(self._on_changed)

    @property
    def slider(self) -> QSlider:
        return self._slider

    def value(self) -> int:
        return self._slider.value()

    def set_value(self, value: int, *, emit: bool = False) -> None:
        """Move the slider without emitting ``value_changed`` unless ``emit``."""
        if not emit:
            self._slider.blockSignals(True)
        self._slider.setValue(int(value))
        if not emit:
            self._slider.blockSignals(False)
        self._value.setText(self._fmt(self._slider.value()))

    def _on_changed(self, value: int) -> None:
        self._value.setText(self._fmt(value))
        self.value_changed.emit(value)


# --------------------------------------------------------------------------- status chip
_CHIP_STATES: dict[str, str] = {"ok": "SUCCESS", "warn": "ACCENT", "bad": "DANGER", "idle": "MUTED"}


class StatusChip(QLabel):
    """Small rounded pill: coloured dot + text. ``set_state("ok"|"warn"|"bad"|"idle", text)``."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = "idle"
        self._text = text
        self.setFont(make_font(12, tabular=True))
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setStyleSheet(
            f"QLabel {{ background: {token('BG2')}; color: {token('TEXT')}; border: 1px solid rgba(255,255,255,0.08); "
            "border-radius: 8px; padding: 2px 9px 2px 8px; }"
        )
        self.set_state("idle", text)

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str, text: str | None = None) -> None:
        """Set the dot colour (``ok``/``warn``/``bad``/``idle``) and optionally the text."""
        if state not in _CHIP_STATES:
            log.warning("StatusChip: unknown state %r", state)
            state = "idle"
        self._state = state
        if text is not None:
            self._text = text
        dot = token(_CHIP_STATES[state])
        self.setText(f'<span style="color:{dot}; font-size:9px;">&#9679;</span>&nbsp; {html.escape(self._text)}')
        self.setToolTip(self._text)


# --------------------------------------------------------------------------- layout helpers
class FlowLayout(QLayout):
    """A layout that lays its items out left to right and wraps to the next line.

    Used for rows of chips/tabs whose number is not known in advance (zone tabs, feed
    filters): instead of forcing the window wider or clipping, items flow onto more
    lines when the width is short.
    """

    def __init__(self, parent: QWidget | None = None, *, h_spacing: int = 6, v_spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h = int(h_spacing)
        self._v = int(v_spacing)
        self.setContentsMargins(0, 0, 0, 0)

    # -- QLayout API -----------------------------------------------------------------
    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def clear(self) -> None:
        """Remove and delete every widget in the layout."""
        while self._items:
            item = self._items.pop()
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    # -- internals -------------------------------------------------------------------
    def _arrange(self, rect: QRect, *, test_only: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y = area.x(), area.y()
        line_height = 0
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            if hint.width() > area.width() > 0:  # a long item gets the whole line, never more
                hint = QSize(max(item.minimumSize().width(), area.width()), hint.height())
            next_x = x + hint.width() + self._h
            if next_x - self._h > area.right() + 1 and line_height > 0:
                x = area.x()
                y += line_height + self._v
                next_x = x + hint.width() + self._h
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


class ElidedLabel(QLabel):
    """A single-line label that elides its text instead of forcing the layout wider.

    A plain ``QLabel`` reports its full text as its minimum width, so one long encounter
    label can push a whole page past the window edge.  This one lets the layout give it
    any width and shows ``…`` where the text does not fit (the full text is the tooltip).
    """

    def __init__(self, text: str = "", parent: QWidget | None = None, *, min_width: int = 48) -> None:
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(max(0, int(min_width)))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt override
        self._full = str(text or "")
        self.setToolTip(self._full)
        self._update_elision()

    def text(self) -> str:
        """The full (unelided) text."""
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(self.minimumWidth(), hint.height())

    def sizeHint(self) -> QSize:  # noqa: N802
        hint = super().sizeHint()
        return QSize(max(self.minimumWidth(), self.fontMetrics().horizontalAdvance(self._full) + 2), hint.height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._update_elision()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange, QEvent.Type.PolishRequest):
            self._update_elision()

    def _update_elision(self) -> None:
        width = max(0, self.contentsRect().width())
        shown = self._full if width == 0 else self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, width)
        if shown != super().text():
            super().setText(shown)


# --------------------------------------------------------------------------- capture warning
#: A dismissed warning comes back only after the problem was gone for this long.
WARNING_CLEAR_S = 20.0


def capture_warning(status: dict[str, Any]) -> tuple[str, str] | None:
    """``(short, long)`` warning text for an engine status, or ``None`` when all is well.

    Two problems are worth interrupting the player for: a game panel covering the Combat
    chat (the tracker skips those frames, so lines scrolling by are lost) and chat text
    that mostly does not read (a moved window, a changed font, something translucent over
    it).  A third state is only a note: the chat is scrolled up (it does not show its
    newest line), so the lines arriving meanwhile are read once it is scrolled down again.
    """
    if status.get("occluded_recent"):
        return (
            "Chat covered: lines may be missing",
            "Something is covering the Combat chat (a game panel opened over it). Lines that "
            "scroll by while it is covered are lost; close the panel or move the chat window.",
        )
    if status.get("garbled"):
        pct = float(status.get("unreadable_pct", 0.0) or 0.0)
        return (
            "Messages not recognized",
            f"{pct:.0f}% of recent messages could not be fully parsed or needed an estimated amount. "
            "Check the affected text in Feed, and confirm that the crop matches the Combat chat "
            "(Settings > Crop). Poor OCR and unsupported messages can both cause this warning.",
        )
    if status.get("scrolled_back"):
        return (
            "Chat scrolled up",
            "The Combat chat does not show its newest line (it is scrolled up, or out of step for a "
            "moment). Lines that arrive meanwhile are read when it shows the newest line again, with "
            "estimated times; the open fight does not time out until then.",
        )
    return None


class WarningLatch:
    """Show-until-dismissed logic shared by the window banner and the overlay pill.

    ``update(active)`` returns whether the warning should be visible: a dismissal holds
    until the problem has been gone for :data:`WARNING_CLEAR_S`, then a new occurrence
    shows again.
    """

    def __init__(self, clear_s: float = WARNING_CLEAR_S) -> None:
        self.clear_s = clear_s
        self.dismissed = False
        self._last_active = 0.0

    def update(self, active: bool, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if active:
            self._last_active = now
            return not self.dismissed
        if self.dismissed and now - self._last_active >= self.clear_s:
            self.dismissed = False
        return False

    def dismiss(self) -> None:
        self.dismissed = True


__all__ = [
    "METRICS",
    "zone_tooltip",
    "WarningLatch",
    "capture_warning",
    "ElidedLabel",
    "FeedEntry",
    "FeedView",
    "FlowLayout",
    "MeterTable",
    "METRIC_VALUE_KEY",
    "SliderRow",
    "StatusChip",
    "ToggleSwitch",
    "actor_display_name",
    "feed_color",
    "fmt_int",
    "fmt_mmss",
    "fmt_pct",
    "fmt_rate",
    "initial_sort_order",
    "make_font",
    "metric_value",
    "qcolor",
    "token",
]
