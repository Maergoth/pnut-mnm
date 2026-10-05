"""Main-window pages: Live, History, Feed, Settings and About.

Every page is a plain :class:`QWidget` constructed as
``Page(engine, cfg, settings, parent=None)``.  The shell (``main.py``) feeds
them through a small set of methods:

* :meth:`LivePage.set_snapshot` / :meth:`LivePage.set_metric`
* :meth:`HistoryPage.add_encounter` / :meth:`HistoryPage.set_history`
* :meth:`FeedPage.append`
* :meth:`SettingsPage.load` and the :attr:`SettingsPage.config_changed` signal
* :class:`AboutPage` only needs the configuration.

All pages tolerate a ``None`` snapshot and an empty history.  Nothing in here
touches the game: the pages only talk to our own :class:`Engine` object and to
files under the project folder.
"""

from __future__ import annotations

import bisect
import csv
import dataclasses
import json
import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QPoint, QPointF, QRectF, QSettings, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QMouseEvent,
    QPainter,
    QPen,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSizePolicy,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

import mnmparse.app as _app_pkg
from mnmparse.app import theme
from mnmparse.app.crop_picker import CropPicker, button_qss, css_color, qcolor
from mnmparse.app.models import ActorRow, EncounterSnapshot, SkillRow, build_snapshot, merge_snapshots, owner_row, snapshot_rows_for_tab
from mnmparse.app.widgets import ElidedLabel, FeedView, FlowLayout, MeterTable, SliderRow, StatusChip, ToggleSwitch
from mnmparse.config import (
    CAPTURE_BACKENDS,
    DEFAULT_CONFIG_PATH,
    OCR_ENGINES,
    PREPROCESS_MODES,
    Config,
    project_path,
    save_config,
)

if TYPE_CHECKING:
    from mnmparse.app.engine import Engine

log = logging.getLogger(__name__)

__all__ = [
    "AboutPage",
    "FEED_GROUPS",
    "FeedPage",
    "HistoryPage",
    "LivePage",
    "METRIC_TABS",
    "SettingsPage",
    "export_csv",
    "export_json",
    "exports_dir",
]

#: Metric tabs shared by the Live and History meters: ``(key, label)``.
METRIC_TABS: tuple[tuple[str, str], ...] = (
    ("overview", "Overview"),
    ("damage", "Damage"),
    ("healing", "Healing"),
    ("taken", "Taken"),
)

#: Feed filter chips: ``(label, kinds)``.  Together they cover every ``grammar.KINDS`` entry.
FEED_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Hits", ("melee_hit", "ability_hit", "ability_partial", "env_damage")),
    ("Misses", ("melee_miss", "ability_miss", "resist", "fizzle")),
    ("Heals", ("heal",)),
    ("Kills", ("kill",)),
    ("Casts", ("cast", "interrupt")),
    ("Utility", ("cc", "cc_fade", "debuff", "aggro", "awaken")),
    ("Loot", ("loot", "coin", "coin_split", "reward", "vendor", "craft")),
    ("Status", ("status", "cannot_attack", "zone", "chat")),
    ("Personal", ("personal", "experience", "level_up", "consider")),
    ("Unknown", ("unknown", "marker")),
)

#: Minimum and maximum width of the caption column in the Settings forms (logical px); the
#: column grows to fit the longest caption and captions wrap beyond the maximum.
_FORM_LABEL_WIDTH = 220
_FORM_LABEL_MAX_WIDTH = 340

#: Tabs the overlay can start on (``Config.overlay_tab``).
_OVERLAY_TABS: tuple[str, ...] = ("overview", "damage", "healing", "taken", "session", "feed")

def _cc_types_text(types: dict[str, int]) -> str:
    """``"2 stun, 1 mez"`` for the details pane (``"-"`` when nothing landed)."""
    parts = [f"{count} {cat}" for cat, count in types.items() if count]
    return ", ".join(parts) if parts else "-"


def _utility_types_text(row: Any) -> str:
    """``"2 stun, 1 condemned, 3 aggro"`` combining CC types, debuff types and aggro."""
    parts = [f"{n} {cat}" for cat, n in (getattr(row, "cc_types", None) or {}).items() if n]
    parts += [f"{n} {cat}" for cat, n in (getattr(row, "debuffs", None) or {}).items() if n]
    aggro = int(getattr(row, "aggro", 0) or 0)
    if aggro:
        parts.append(f"{aggro} aggro")
    return ", ".join(parts) if parts else "-"


#: ActorRow fields written to CSV (every numeric field, in this order).
_CSV_NUMERIC_FIELDS: tuple[str, ...] = (
    "damage",
    "dps",
    "taken",
    "dtps",
    "heals",
    "hps",
    "healed",
    "swings",
    "hits",
    "misses",
    "hit_pct",
    "max_hit",
    "avg_hit",
    "share",
    "max_heal",
    "cc",
    "cc_attempts",
    "utility",
    "aggro",
    "prevented",
)

_SAFETY_TEXT = (
    "This application is completely passive and out-of-process. It reads pixels only, "
    "through Windows Graphics Capture (the same Windows API OBS uses for window capture); "
    "the desktop compositor hands it a copy of the already-rendered game frame. It never "
    "opens the game process, reads its memory, injects DLLs or hooks, sends keystrokes, "
    "mouse input or window messages, and never moves, resizes or focuses the game window. "
    "The only Win32 calls are the read-only ones needed to find the window. The overlay is "
    "our own translucent window (frameless, topmost, shown without activating, never takes "
    "keyboard focus); it never interacts with the game. With the default WGC backend the "
    "capture never includes other windows, so the overlay cannot be fed to the OCR; the "
    "\"mss\" fallback grabs the desktop rectangle, so keep the overlay off the Combat chat "
    "when using it. Everything the tool writes goes under the project folder (logs/, "
    "config.json, assets/ui/ for a few rendered UI images); window layout and switches are "
    "kept in the registry under HKCU\\Software\\mnmparse\\MnM Parser (QSettings)."
)

_TERMS_TEXT = (
    "Read this before you run the tool. As of October 2026 the Monsters & Memories Master User "
    "Agreement (account2.monstersandmemories.com/policy/mau) forbids, among other things, software "
    "that \"intercepts, collects, reads, or 'mines' information generated or stored by the Platform\" "
    "and any interception of the game's network protocol, and it states that the game may monitor "
    "your computer's memory for unauthorized third-party programs. The Play Nice Policy bans "
    "automation and \"extracting game data through unauthorized methods\". Neither document mentions "
    "screen capture or OCR by name.\n\n"
    "What that means for this tool: it does nothing the agreement names explicitly; it never reads "
    "the game's memory, network traffic or files, never automates anything, and never modifies game "
    "behaviour. From the game's point of view it is indistinguishable from a screenshot or an OBS "
    "window capture. The \"reads or mines information generated by the Platform\" clause is broad "
    "enough that Niche Worlds Cult could decide a screen-reading parser is unauthorized. A "
    "packet-based parser for this game was withdrawn in 2026 after the developers objected, and the "
    "developers have said they do not want DPS meters shaping the game's culture.\n\n"
    "Whether to run it is your decision and your risk. Keep it private, do not discuss parses in "
    "game, and stop using it if the developers state that OCR parsers are not permitted. The "
    "maintainer of this project does not endorse violating the agreement."
)


# ----------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------


def _mmss(seconds: float) -> str:
    """``125.3`` -> ``"02:05"``."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _num(value: float | int) -> str:
    """Thousands-separated integer text."""
    return f"{int(round(value)):,}"


def _share(value: float) -> str:
    """``0.42`` (a 0..1 fraction) -> ``"42.0%"``."""
    return f"{value * 100:.1f}%"


def _pct(value: float) -> str:
    """``87.5`` (already a percentage) -> ``"87.5%"``."""
    return f"{value:.1f}%"


def _cfg_get(cfg: Config, name: str, default: Any) -> Any:  # noqa: ANN401
    """Read an optional :class:`Config` field (the overlay fields may be absent)."""
    return getattr(cfg, name, default)


def _page_qss() -> str:
    """Stylesheet for the page-local object names (panels, chips, titles)."""
    line = css_color(theme.LINE)
    return f"""
    QFrame#Panel {{
        background: {theme.BG1};
        border: 1px solid {line};
        border-radius: 12px;
    }}
    QFrame#SubPanel {{
        background: {theme.BG2};
        border: 1px solid {line};
        border-radius: 8px;
    }}
    QLabel#Title {{ font-size: 20px; font-weight: 600; color: {theme.TEXT}; }}
    QLabel#Heading {{ font-size: 15px; font-weight: 600; color: {theme.TEXT}; }}
    QLabel#Muted {{ color: {theme.MUTED}; }}
    QLabel#Stat {{ font-size: 18px; font-weight: 600; color: {theme.TEXT}; }}
    QLabel#Problems {{ color: {theme.DANGER}; }}
    QLabel#Ok {{ color: {theme.SUCCESS}; }}
    QLabel#Link {{ color: {theme.ACCENT2}; }}
    QPushButton#Segment {{
        background: transparent; color: {theme.MUTED}; border: 1px solid {line};
        border-radius: 8px; padding: 4px 14px; min-height: 22px;
    }}
    QPushButton#Segment:hover {{ color: {theme.TEXT}; background: {theme.BG2}; }}
    QPushButton#Segment:checked {{
        color: {theme.BG0}; background: {theme.ACCENT}; border-color: {theme.ACCENT}; font-weight: 600;
    }}
    QPushButton#ZoneTab {{
        background: transparent; color: {theme.MUTED}; border: 1px solid {line};
        border-radius: 7px; padding: 2px 10px; min-height: 18px; font-size: 12px;
    }}
    QPushButton#ZoneTab:hover {{ color: {theme.TEXT}; background: {theme.BG2}; }}
    QPushButton#ZoneTab:checked {{
        color: {theme.BG0}; background: {theme.ACCENT}; border-color: {theme.ACCENT}; font-weight: 600;
    }}
    QFrame#StatChip {{ background: {theme.BG2}; border: 1px solid {line}; border-radius: 7px; }}
    {button_qss()}
    QPushButton#Danger {{
        background: transparent; color: {theme.DANGER}; border: 1px solid {theme.DANGER};
        border-radius: 8px; padding: 5px 14px; min-height: 22px;
    }}
    QPushButton#Danger:hover {{ background: rgba(229, 72, 77, 40); }}
    QPushButton#Danger:disabled {{ color: {theme.MUTED}; border-color: {line}; }}
    """


def _panel(parent: QWidget | None = None, *, sub: bool = False) -> QFrame:
    """A rounded dark-glass panel frame."""
    frame = QFrame(parent)
    frame.setObjectName("SubPanel" if sub else "Panel")
    return frame


def _label(text: str = "", name: str | None = None, parent: QWidget | None = None) -> QLabel:
    lab = QLabel(text, parent)
    if name:
        lab.setObjectName(name)
    return lab


def _tabular(widget: QWidget) -> None:
    """Ask for tabular digits on ``widget`` (Qt 6.7+; harmless elsewhere)."""
    widget.setFont(theme.tabular_font(widget.font()))


def _percent_text(value: int) -> str:
    return f"{value}%"


def _hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return slug[:limit] or "encounter"


# ----------------------------------------------------------------------------------
# Exports
# ----------------------------------------------------------------------------------


def exports_dir(cfg: Config) -> Path:
    """``<log_dir>/exports`` resolved against the project root (created on demand)."""
    path = project_path(cfg.log_dir) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _export_stem(snap: EncounterSnapshot) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(snap.start))
    return f"encounter_{stamp}_{_slug(snap.label)}"


def export_csv(snap: EncounterSnapshot, path: Path) -> Path:
    """Write one row per actor with every numeric :class:`ActorRow` field.

    Returns:
        The written path.
    """
    header = ["encounter", "name", *_CSV_NUMERIC_FIELDS, "is_you", "is_npc", "is_pet"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in snap.rows:
            values: list[Any] = [snap.label, row.name]
            values.extend(getattr(row, name) for name in _CSV_NUMERIC_FIELDS)
            values.extend(int(flag) for flag in (row.is_you, row.is_npc, row.is_pet))
            writer.writerow(values)
    log.info("Exported %d actor rows to %s", len(snap.rows), path)
    return path


def export_json(snap: EncounterSnapshot, path: Path) -> Path:
    """Write ``dataclasses.asdict(snap)`` as pretty JSON.

    Returns:
        The written path.
    """
    data = dataclasses.asdict(snap)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log.info("Exported encounter %s to %s", snap.key, path)
    return path


# ----------------------------------------------------------------------------------
# Shared building blocks
# ----------------------------------------------------------------------------------


class _Segmented(QWidget):
    """A row of mutually exclusive checkable buttons (metric tabs)."""

    changed = Signal(str)

    def __init__(self, items: Sequence[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._keys = [key for key, _ in items]
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = FlowLayout(self, h_spacing=6, v_spacing=6)  # wraps instead of clipping when narrow
        for index, (key, label) in enumerate(items):
            button = _SegmentButton(label, self)
            button.setChecked(index == 0)
            self._group.addButton(button, index)
            layout.addWidget(button)
        self._group.idClicked.connect(self._on_clicked)

    def current(self) -> str:
        """Key of the checked button."""
        return self._keys[max(0, self._group.checkedId())]

    def set_current(self, key: str) -> None:
        """Check ``key`` without emitting :attr:`changed` (unknown keys are ignored)."""
        if key not in self._keys:
            return
        button = self._group.button(self._keys.index(key))
        if button is not None:
            button.setChecked(True)

    def _on_clicked(self, index: int) -> None:
        self.changed.emit(self._keys[index])


class _EncounterHeader(QWidget):
    """Label, duration, total damage, raid DPS and a live/ended chip for one snapshot.

    Wide: one row ``[title / subtitle] [stats] [chip] [actions]``.  When that does not
    fit, the stats move to a second row, so the header can get narrow without clipping
    any text (the title and subtitle elide, with the full text as their tooltip).
    """

    TITLE_MIN_WIDE = 180  #: room the title keeps in the one-row arrangement
    HYSTERESIS = 24  #: extra width needed to go back to one row (no flicker at the edge)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)
        self._row1 = QHBoxLayout()
        self._row1.setSpacing(18)
        self._row2 = QHBoxLayout()
        self._row2.setSpacing(18)
        self._row2.addStretch(1)
        outer.addLayout(self._row1)
        outer.addLayout(self._row2)

        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self._title = ElidedLabel("No encounter yet", min_width=80)
        self._title.setObjectName("Title")
        self._subtitle = ElidedLabel("Start capture and fight something.", min_width=80)
        self._subtitle.setObjectName("Muted")
        title_col.addWidget(self._title)
        title_col.addWidget(self._subtitle)
        self._row1.addLayout(title_col, 1)

        self._stats_box = QWidget(self)
        stats_layout = QHBoxLayout(self._stats_box)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(18)
        self._stats: dict[str, QLabel] = {}
        for key, caption in (("duration", "Duration"), ("damage", "Total damage"), ("dps", "Raid DPS")):
            col = QVBoxLayout()
            col.setSpacing(0)
            value = _label("-", "Stat")
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            _tabular(value)
            cap = _label(caption, "Muted")
            cap.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            col.addWidget(value)
            col.addWidget(cap)
            stats_layout.addLayout(col)
            self._stats[key] = value

        self._chip = StatusChip("idle", self)
        self._chip.set_state("idle", "idle")
        self._row1.addWidget(self._chip, 0, Qt.AlignmentFlag.AlignVCenter)
        self._actions: list[QWidget] = []
        self._wide: bool | None = None
        self._arrange(True)

    # -- layout ------------------------------------------------------------------------
    def add_action(self, widget: QWidget) -> None:
        """Append a button (e.g. Reset encounter) to the end of the first row."""
        self._actions.append(widget)
        self._row1.addWidget(widget, 0, Qt.AlignmentFlag.AlignVCenter)
        self._update_mode()

    def is_wide(self) -> bool:
        """``True`` while everything sits on one row."""
        return bool(self._wide)

    def _arrange(self, wide: bool) -> None:
        if wide == self._wide:
            return
        self._wide = wide
        self._row1.removeWidget(self._stats_box)
        self._row2.removeWidget(self._stats_box)
        if wide:
            self._row1.insertWidget(1, self._stats_box, 0, Qt.AlignmentFlag.AlignVCenter)
        else:
            self._row2.insertWidget(0, self._stats_box, 0, Qt.AlignmentFlag.AlignLeft)
        self.updateGeometry()

    def _wide_need(self) -> int:
        spacing = self._row1.spacing()
        actions = sum(a.sizeHint().width() + spacing for a in self._actions if not a.isHidden())
        return (
            self.TITLE_MIN_WIDE + spacing + self._stats_box.sizeHint().width() + spacing
            + self._chip.sizeHint().width() + actions
        )

    def _update_mode(self) -> None:
        width = self.width()
        if width <= 0:
            return
        need = self._wide_need()
        self._arrange(width >= need if self._wide else width >= need + self.HYSTERESIS)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        """Always the two-row minimum, so a parent may shrink the header and the stats wrap."""
        spacing = self._row1.spacing()
        row1 = self._title.minimumSizeHint().width() + spacing + self._chip.minimumSizeHint().width()
        row1 += sum(a.minimumSizeHint().width() + spacing for a in self._actions if not a.isHidden())
        width = max(row1, self._stats_box.minimumSizeHint().width())
        return QSize(width, super().minimumSizeHint().height())

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_mode()

    # -- data --------------------------------------------------------------------------
    def set_snapshot(self, snap: EncounterSnapshot | None) -> None:
        """Render ``snap`` (``None`` clears the header)."""
        if snap is None:
            self._title.setText("No encounter yet")
            self._subtitle.setText("Start capture and fight something.")
            for value in self._stats.values():
                value.setText("-")
            self._chip.set_state("idle", "idle")
            self._update_mode()
            return
        self._title.setText(snap.label or "unknown")
        killed = f"  ·  killed: {', '.join(snap.killed)}" if snap.killed else ""
        summary = getattr(snap, "encounters", 1) > 1 or str(snap.key).startswith("zone:")
        if summary:
            # A zone summary: every encounter of the visit added up (duration = time in combat).
            n = getattr(snap, "encounters", 1)
            since = getattr(snap, "zone_since", 0.0) or snap.start
            day = "" if time.strftime("%x", time.localtime(since)) == time.strftime("%x") else time.strftime("%b %d ", time.localtime(since))
            self._subtitle.setText(f"{n} encounter{'s' if n != 1 else ''}  ·  since {day}{_hms(since)}{killed}")
            self._stats["duration"].setText(_mmss(snap.duration))
        else:
            started = _hms(snap.start)
            zone = f"  ·  {snap.zone}" if getattr(snap, "zone", "") else ""
            self._subtitle.setText(f"started {started}{zone}  ·  {snap.event_count} events{killed}")
            self._stats["duration"].setText(_mmss(snap.duration))
        self._stats["damage"].setText(_num(snap.total_damage))
        self._stats["dps"].setText(f"{snap.raid_dps:,.1f}")
        if not snap.closed:
            self._chip.set_state("ok", "live")
        elif summary:
            self._chip.set_state("idle", "zone")
        else:
            self._chip.set_state("idle", "ended")
        self._update_mode()


class _StatChip(QFrame):
    """``Caption value`` in a small rounded box (one entry of the details summary)."""

    def __init__(self, caption: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StatChip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(8)
        cap = QLabel(caption, self)
        cap.setObjectName("Muted")
        layout.addWidget(cap)
        self.value = ElidedLabel("-", self, min_width=24)
        self.value.setStyleSheet("font-weight: 600;")
        _tabular(self.value)
        layout.addWidget(self.value, 1)


class _DetailsPanel(QFrame):
    """Details for the selected actor: name, a wrapping row of stat chips, the skills table.

    The chips flow onto more lines when the panel is narrow.  Everything sits in one
    vertical scroll area (the skills table is sized to its rows), so a short panel
    scrolls instead of squeezing the summary into overlapping text, and the meter above
    keeps its room.
    """

    _SKILL_COLUMNS = ("Skill", "Hits", "Total", "Max", "Avg", "Hit%")
    #: ``(key, caption)`` of the summary chips, in display order.
    _CHIPS: tuple[tuple[str, str], ...] = (
        ("damage", "Damage"),
        ("dps", "DPS"),
        ("share", "Share"),
        ("hits", "Hits"),
        ("max_hit", "Max hit"),
        ("heals", "Healing"),
        ("taken", "Taken"),
        ("utility", "Utility"),
    )
    MIN_HEIGHT = 96  #: the name row and one line of chips

    def __init__(self, parent: QWidget | None = None, *, horizontal: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        frame = QVBoxLayout(self)
        frame.setContentsMargins(2, 2, 2, 2)  # keep the panel's rounded border visible
        frame.setSpacing(0)
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        content = QWidget()
        content.setObjectName("DetailsContent")
        content.setAutoFillBackground(False)
        content.setStyleSheet("QWidget#DetailsContent { background: transparent; }")
        outer = QVBoxLayout(content)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(8)
        self._scroll.setWidget(content)
        frame.addWidget(self._scroll)
        self.setMinimumHeight(self.MIN_HEIGHT)
        if not horizontal:
            self.setMinimumWidth(300)

        self._last_row: ActorRow | None = None
        top = QHBoxLayout()
        top.setSpacing(12)
        self._name = ElidedLabel("Select an actor", min_width=60)
        self._name.setObjectName("Heading")
        self._name.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self._role = ElidedLabel("Click a row in the meter to see its breakdown.", min_width=40)
        self._role.setObjectName("Muted")
        top.addWidget(self._name, 0)
        top.addWidget(self._role, 1)
        outer.addLayout(top)

        self._chips_box = QWidget(content)
        self._flow = FlowLayout(self._chips_box, h_spacing=8, v_spacing=6)
        self._cells: dict[str, QLabel] = {}
        self._chip_widgets: dict[str, _StatChip] = {}
        for key, caption in self._CHIPS:
            chip = _StatChip(caption, self._chips_box)
            self._flow.addWidget(chip)
            self._cells[key] = chip.value
            self._chip_widgets[key] = chip
        outer.addWidget(self._chips_box)

        self._skills = QTableWidget(0, len(self._SKILL_COLUMNS), content)
        self._skills.setHorizontalHeaderLabels(self._SKILL_COLUMNS)
        self._skills.verticalHeader().setVisible(False)
        self._skills.setShowGrid(False)
        self._skills.setAlternatingRowColors(False)
        self._skills.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._skills.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._skills.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._skills.setFrameShape(QFrame.Shape.NoFrame)
        self._skills.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._skills.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # the panel scrolls
        header = self._skills.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        _tabular(self._skills)
        outer.addWidget(self._skills)
        outer.addStretch(1)
        self._fit_skills_height()

    def set_actor(self, row: ActorRow | None) -> None:
        """Show ``row`` (``None`` resets the panel).

        Called on every snapshot (up to 10 Hz); unchanged rows are skipped and the
        skills table is only rebuilt when the skill breakdown itself changed, so it
        keeps its scroll position during a long fight.
        """
        last = self._last_row
        if row == last and (row is None) == (last is None):
            return
        self._last_row = None if row is None else dataclasses.replace(row)
        if row is None:
            self._skills.setRowCount(0)
            self._name.setText("Select an actor")
            self._name.setStyleSheet("")
            self._role.setText("Click a row in the meter to see its breakdown.")
            for cell in self._cells.values():
                cell.setText("-")
            self._fit_skills_height()
            return
        self._name.setText(row.name)
        self._name.setStyleSheet(f"color: {row.color};")
        if row.is_you:
            kind = "you"
        elif row.is_pet:
            kind = f"{row.pet_owner}'s pet" if row.pet_owner else "pet"
        elif row.is_npc or getattr(row, "is_enemy", False):
            kind = "enemy"
        elif getattr(row, "in_group", True):
            kind = "your group"
        else:
            kind = "outside your group (not in the totals)"
        pets = f"  ·  includes pets: {', '.join(row.attributed_pets)}" if row.attributed_pets else ""
        self._role.setText(f"{kind}  ·  {len(row.skills)} skills{pets}")
        utility = int(getattr(row, "utility", 0) or 0)
        prevented = int(getattr(row, "prevented", 0) or 0)
        values = {
            "damage": _num(row.damage),
            "dps": f"{row.dps:,.1f}",
            "share": _share(row.share),
            "hits": (f"{_num(row.hits)} of {_num(row.swings)}  ·  {_pct(row.hit_pct)}" if row.swings else _num(row.hits))
            if getattr(row, "misses_shown", True) else f"{_num(row.hits)}  ·  hit rate unknown",
            "max_hit": f"{_num(row.max_hit)}  ·  avg {row.avg_hit:,.1f}",
            "heals": f"{_num(row.heals)}  ·  {row.hps:,.1f} HPS",
            "taken": f"{_num(row.taken)}  ·  {_num(prevented)} prevented" if prevented else _num(row.taken),
            "utility": f"{_num(utility)}  ·  {_utility_types_text(row)}" if utility else "0",
        }
        tips = {
            "hits": f"{_num(row.hits)} hits and {_num(row.misses)} misses out of {_num(row.swings)} swings",
            "max_hit": "Largest single hit, and the average damaging hit",
            "heals": f"{_num(row.heals)} healing done ({row.hps:,.1f} per second)",
            "taken": "Damage taken after mitigation; prevented = blocked plus absorbed",
            "utility": "Crowd control landed + debuffs landed + aggro gained",
        }
        for key, text in values.items():
            self._cells[key].setText(text)
            if key in tips:
                self._chip_widgets[key].setToolTip(tips[key])
        if last is None or last.name != row.name or last.skills != row.skills:
            self._fill_skills(row.skills, misses_shown=bool(getattr(row, "misses_shown", True)))

    def _fit_skills_height(self) -> None:
        """Size the skills table to its rows (one empty row at least); the panel scrolls."""
        header = self._skills.horizontalHeader().sizeHint().height()
        rows = max(1, self._skills.rowCount()) * self._skills.verticalHeader().defaultSectionSize()
        self._skills.setFixedHeight(header + rows + 2 * self._skills.frameWidth() + 2)

    def _fill_skills(self, skills: Iterable[SkillRow], *, misses_shown: bool = True) -> None:
        rows = list(skills)
        self._skills.setRowCount(len(rows))
        for r, skill in enumerate(rows):
            attempts = skill.hits + skill.misses
            hit_pct = (skill.hits / attempts * 100.0) if attempts else 100.0
            hit_text = f"{hit_pct:.0f}%" if misses_shown else "—"  # other players' misses are not shown
            cells = (
                skill.skill,
                _num(skill.hits),
                _num(skill.total),
                _num(skill.max_hit),
                f"{skill.avg:,.1f}",
                hit_text,
            )
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c > 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                else:
                    item.setToolTip(text)
                self._skills.setItem(r, c, item)
        self._fit_skills_height()


class _MeterPane(QWidget):
    """Metric tabs + :class:`MeterTable` + :class:`_DetailsPanel` for one snapshot.

    Used by both the Live and the History page.
    """

    metric_changed = Signal(str)

    def __init__(
        self,
        settings_key: str,
        settings: QSettings,
        parent: QWidget | None = None,
        *,
        details_below: bool = False,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._settings_key = settings_key
        self._snap: EncounterSnapshot | None = None
        self._selected_name: str | None = None
        self._details_below = details_below

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.tabs = _Segmented(METRIC_TABS, self)
        layout.addWidget(self.tabs)

        orientation = Qt.Orientation.Vertical if details_below else Qt.Orientation.Horizontal
        self._splitter = QSplitter(orientation, self)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(10)

        table_panel = _panel()
        table_layout = QVBoxLayout(table_panel)
        table_layout.setContentsMargins(8, 8, 8, 8)
        self.table = MeterTable(compact=False)
        self.table.setMinimumWidth(self.table.minimum_width())  # optional columns hide below this
        self.table.setMinimumHeight(self.table.minimum_height(3))  # the meter always shows three rows
        table_layout.addWidget(self.table)
        self._splitter.addWidget(table_panel)

        self.details = _DetailsPanel(horizontal=details_below)
        self._splitter.addWidget(self.details)
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 2)
        layout.addWidget(self._splitter, 1)

        self.tabs.changed.connect(self._on_tab)
        self.table.row_selected.connect(self._on_row_selected)
        self.table.sort_changed.connect(self._on_sort_changed)
        self._restore()
        self._splitter.splitterMoved.connect(lambda *_: self.save_layout())

    # -- persistence -----------------------------------------------------------------
    def _restore(self) -> None:
        self._settings.beginGroup(self._settings_key)
        try:
            tab = str(self._settings.value("metric", "damage"))
            sort_key = self._settings.value("sort_key", None)
            descending = self._settings.value("sort_desc", True)
            sizes = self._settings.value("details_split", None)
        finally:
            self._settings.endGroup()
        self.tabs.set_current(tab)
        if sort_key:
            desc = str(descending).lower() in ("true", "1")
            self.table.set_sort(str(sort_key), desc)
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([int(v) for v in sizes])
            except (TypeError, ValueError):
                log.debug("Ignoring bad splitter sizes %r", sizes)
        else:
            self._splitter.setSizes([300, 280] if self._details_below else [740, 400])

    def _store(self, key: str, value: Any) -> None:  # noqa: ANN401
        self._settings.beginGroup(self._settings_key)
        try:
            self._settings.setValue(key, value)
        finally:
            self._settings.endGroup()

    # -- public ----------------------------------------------------------------------
    def current_metric(self) -> str:
        """The current metric tab key."""
        return self.tabs.current()

    def set_metric(self, tab: str) -> None:
        """Switch to ``tab`` and re-render (no-op for unknown keys)."""
        if tab not in {key for key, _ in METRIC_TABS}:
            return
        self.tabs.set_current(tab)
        self._store("metric", tab)
        self._render()

    def set_snapshot(self, snap: EncounterSnapshot | None) -> None:
        """Render ``snap`` (``None`` clears the meter and the details)."""
        self._snap = snap
        self._render()

    def snapshot(self) -> EncounterSnapshot | None:
        return self._snap

    # -- internals -------------------------------------------------------------------
    def _render(self) -> None:
        metric = self.tabs.current()
        rows = snapshot_rows_for_tab(self._snap, metric) if self._snap is not None else []
        self.table.set_rows(rows, metric)
        selected = next((r for r in rows if r.name == self._selected_name), None)
        if selected is None:
            current = self.table.selected_row()
            selected = current if isinstance(current, ActorRow) else None
        if selected is None and rows:
            selected = next((r for r in rows if r.is_you), rows[0])
        self.details.set_actor(self._owner_details(selected))

    def _owner_details(self, row: ActorRow | None) -> ActorRow | None:
        if row is None or self._snap is None or row.is_pet:
            return row
        return owner_row(self._snap, row.name)

    def _on_tab(self, key: str) -> None:
        self._store("metric", key)
        self._render()
        self.metric_changed.emit(key)

    def _on_row_selected(self, row: object) -> None:
        if isinstance(row, ActorRow):
            self._selected_name = row.name
            self.details.set_actor(self._owner_details(row))
        else:
            self.details.set_actor(None)

    def _on_sort_changed(self, key: str, descending: bool) -> None:
        self._store("sort_key", key)
        self._store("sort_desc", descending)

    def save_layout(self) -> None:
        """Persist the meter/details split (also done whenever the user drags it)."""
        self._store("details_split", self._splitter.sizes())


# ----------------------------------------------------------------------------------
# Live page: every encounter, grouped under collapsible zone headers (like ACT)
# ----------------------------------------------------------------------------------

#: Title used for encounters that started before any zone line was seen.
UNKNOWN_ZONE_LABEL = "Unknown zone"
#: Encounters without a known zone that start further apart than this form separate groups.
UNKNOWN_ZONE_GAP_S = 1800.0
#: An imported encounter is a fight already listed (the same session imported twice, or a
#: .log and its .jsonl, which split fights a little differently) when the two overlap for at
#: least this share of the shorter one and share a player; a friend's log from another
#: group at the same time shares no player and is kept.
IMPORT_OVERLAP_SHARE = 0.5
IMPORT_DUPLICATE_S = 2.0  #: starts this close count as overlapping even for one-second fights
IMPORT_LOOKBACK_S = 900.0  #: no listed fight is longer than this


def zone_title(zone: str) -> str:
    """Display name of a snapshot's zone (``""`` reads "Unknown zone")."""
    return zone or UNKNOWN_ZONE_LABEL


class _SegmentButton(QPushButton):
    """A ``Segment`` button whose size hint has room for its bold (checked) text.

    The stylesheet makes the checked segment bold, but Qt computes the size hint from the
    widget's regular font, so a checked "Overview" was wider than its button and clipped.
    """

    def __init__(self, text: str, parent: QWidget | None = None, *, object_name: str = "Segment") -> None:
        super().__init__(text, parent)
        self.setObjectName(object_name)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        bold = QFont(self.font())
        bold.setWeight(QFont.Weight.DemiBold)
        regular = self.fontMetrics().horizontalAdvance(self.text())
        extra = QFontMetrics(bold).horizontalAdvance(self.text()) - regular
        return QSize(hint.width() + max(0, extra) + 4, hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()


def _scaled_font(font: QFont, factor: float, *, weight: QFont.Weight | None = None) -> QFont:
    """``font`` scaled by ``factor`` whether it is specified in pixels or points."""
    out = QFont(font)
    if out.pixelSize() > 0:
        out.setPixelSize(max(8, int(round(out.pixelSize() * factor))))
    elif out.pointSizeF() > 0:
        out.setPointSizeF(max(6.0, out.pointSizeF() * factor))
    if weight is not None:
        out.setWeight(weight)
    return out


@dataclasses.dataclass
class _Visit:
    """One stay in a zone: the encounters fought between entering it and leaving it."""

    key: str
    zone: str
    since: float  #: when the zone was entered (the first encounter's start when unknown)
    keys: list[str]  #: encounter keys, oldest first
    last_start: float


def _day(ts: float) -> str:
    return time.strftime("%b %d", time.localtime(ts))


def _is_today(ts: float) -> bool:
    return _day(ts) == _day(time.time())


class _EncounterDelegate(QStyledItemDelegate):
    """Zone header rows and encounter rows of the Live tree.

    Zone: ``Zone name  ·  N encounters`` over ``entered 11:42  ·  12:01 in combat  ·  ...``.
    Encounter: ``11:42:31   targets`` over ``00:38  ·  31,000 dmg  ·  you 400.0 DPS``; the open
    fight has a green dot.
    """

    LINE2_ROLE = Qt.ItemDataRole.UserRole + 1
    ZONE_ROLE = Qt.ItemDataRole.UserRole + 2  #: True on zone header rows
    LIVE_ROLE = Qt.ItemDataRole.UserRole + 3
    OPEN_ROLE = Qt.ItemDataRole.UserRole + 4  #: zone header expanded
    ROW_HEIGHT = 46
    ZONE_HEIGHT = 50
    ARROW_W = 22  #: the chevron column of zone rows (a click there opens / closes the zone)
    CHILD_INDENT = 18

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        height = self.ZONE_HEIGHT if index.data(self.ZONE_ROLE) else self.ROW_HEIGHT
        return QSize(max(0, option.rect.width()), height)

    def paint(  # noqa: D102
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        zone = bool(index.data(self.ZONE_ROLE))
        rect = option.rect.adjusted(2 if zone else 2 + self.CHILD_INDENT, 2, -2, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if zone or selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(theme.BG2) if (selected or hovered) else QColor(255, 255, 255, 10))
            painter.drawRoundedRect(rect, 8, 8)
        if selected:
            painter.setBrush(qcolor(theme.ACCENT))
            painter.drawRoundedRect(QRectF(rect.left(), rect.top() + 8, 3, rect.height() - 16), 1.5, 1.5)
        text_rect = rect.adjusted(12, 4, -8, -4)
        if zone:
            # chevron: right when closed, down when open
            cx, cy = rect.left() + 13.0, rect.center().y()
            pen = QPen(qcolor(theme.MUTED))
            pen.setWidthF(1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            if index.data(self.OPEN_ROLE):
                points = [QPointF(cx - 4, cy - 2), QPointF(cx, cy + 2.5), QPointF(cx + 4, cy - 2)]
            else:
                points = [QPointF(cx - 2, cy - 4.5), QPointF(cx + 2.5, cy), QPointF(cx - 2, cy + 4.5)]
            painter.drawPolyline(points)
            text_rect = text_rect.adjusted(self.ARROW_W - 8, 0, 0, 0)
        half = text_rect.height() // 2
        line1 = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        line2 = str(index.data(self.LINE2_ROLE) or "")
        base = QFont(painter.font())
        bold = QFont(base)
        bold.setWeight(QFont.Weight.DemiBold)
        line1_rect = text_rect.adjusted(0, 0, 0, -half)
        if index.data(self.LIVE_ROLE):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(theme.SUCCESS))
            cy = line1_rect.center().y()
            painter.drawEllipse(QRectF(line1_rect.left(), cy - 3.5, 7, 7))
            line1_rect = line1_rect.adjusted(13, 0, 0, 0)
        painter.setFont(_scaled_font(bold, 1.06) if zone else bold)
        painter.setPen(qcolor(theme.ACCENT2 if zone else theme.TEXT))
        elided = painter.fontMetrics().elidedText(line1, Qt.TextElideMode.ElideRight, line1_rect.width())
        painter.drawText(line1_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided)
        painter.setFont(base)
        painter.setPen(qcolor(theme.MUTED))
        line2_rect = text_rect.adjusted(0, half, 0, 0)
        elided2 = painter.fontMetrics().elidedText(line2, Qt.TextElideMode.ElideRight, line2_rect.width())
        painter.drawText(line2_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, elided2)
        painter.restore()


class _EncounterTree(QTreeWidget):
    """The Live tree: no native branch column; a zone opens / closes from its chevron
    (or a double click), a plain click selects it (its summary)."""

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.position().toPoint())
            if item is not None and item.parent() is None:
                rect = self.visualItemRect(item)
                if event.position().x() - rect.left() <= _EncounterDelegate.ARROW_W + 6:
                    item.setExpanded(not item.isExpanded())
        super().mousePressEvent(event)


class LivePage(QWidget):
    """Every encounter (live, closed and imported), grouped under zone headers.

    Left: a tree with one collapsible header per zone visit (newest first, time stamped,
    with the number of encounters and the combat time), the encounters of that visit
    under it, and the open fight pinned at the top with a green dot.  Selecting a header
    shows the summary of every encounter of that visit (like ACT's "All" entry);
    selecting an encounter shows that fight.  Right: header, metric tabs, meter and actor
    details for the selection, which follows the newest fight until the user picks
    something else.  ``Import log…`` re-parses a ``combat_*.log`` / ``events_*.jsonl`` file
    into the same tree (the same fight imported twice is listed once) and hands its
    session to the Session page through :attr:`imported`.
    """

    metric_changed = Signal(str)
    imported = Signal(object)  #: ImportResult of a file the user imported (Session page listens)
    #: Right-click > Copy: a fight or a zone summary (EncounterSnapshot) for the clipboard;
    #: the app formats it (mnmparse.export).
    copy_requested = Signal(object)
    #: Right-click a person in the meter > count them in / out of the group (see
    #: overlay.add_group_entries): ``(name, True | False | None)``.
    group_override_requested = Signal(str, object)
    pet_owner_requested = Signal(str, object)

    def __init__(
        self, engine: Engine, cfg: Config, settings: QSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cfg = cfg
        self._settings = settings
        self._snaps: dict[str, EncounterSnapshot] = {}
        self._import_sources: dict[str, tuple[Any, Any]] = {}  #: key -> (Stats, Encounter), for ownership corrections
        self._sources: dict[str, str] = {}
        self._intervals: list[tuple[float, float, str]] = []  #: (start, end, key), sorted
        self._live_key: str | None = None
        self._selection: tuple[str, str] | None = None  #: ("zone", visit key) or ("enc", encounter key)
        self._follow = True
        self._expanded: dict[str, bool] = {}
        self._visits: list[_Visit] = []
        self._visit_of: dict[str, str] = {}
        self._summary_cache: dict[str, tuple[tuple[str, ...], EncounterSnapshot]] = {}
        self._batching = False
        self._stale = False  #: snapshots arrived while the page was off screen (see _on_screen)
        self._imported_count = 0
        self._show_others = bool(getattr(cfg, "show_other_groups", False))
        self.setStyleSheet(_page_qss())

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(0)
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.setHandleWidth(12)
        layout.addWidget(self._splitter, 1)

        # Left: the encounter tree and export.
        list_panel = _panel()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(14, 12, 14, 12)
        list_layout.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(_label("Encounters", "Heading"))
        self._count = ElidedLabel("none yet")
        self._count.setObjectName("Muted")
        top.addWidget(self._count, 1)
        self._import_btn = QPushButton("Import log…")
        self._import_btn.setObjectName("Chip")
        self._import_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._import_btn.setToolTip(
            "Re-parse a combat_*.log or events_*.jsonl file into this list (fights already listed are "
            "skipped); its session appears in the Session page's source selector"
        )
        self._import_btn.clicked.connect(self._on_import)
        top.addWidget(self._import_btn)
        list_layout.addLayout(top)
        self._tree = _EncounterTree()
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(1)
        self._tree.setItemDelegate(_EncounterDelegate(self._tree))
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(0)
        self._tree.setUniformRowHeights(False)
        self._tree.setAnimated(False)
        self._tree.setMouseTracking(True)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._tree.setFrameShape(QFrame.Shape.NoFrame)
        self._tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._tree.setStyleSheet("QTreeWidget { background: transparent; } QTreeWidget::item { border: none; }")
        self._tree.itemSelectionChanged.connect(self._on_selection)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._tree_menu)
        self._tree.itemExpanded.connect(lambda item: self._remember_expanded(item, True))
        self._tree.itemCollapsed.connect(lambda item: self._remember_expanded(item, False))
        list_layout.addWidget(self._tree, 1)
        export_row = QHBoxLayout()
        export_row.setSpacing(8)
        self._csv = QPushButton("Export CSV")
        self._json = QPushButton("Export JSON")
        for button in (self._csv, self._json):
            button.setObjectName("Chip")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setEnabled(False)
        self._csv.clicked.connect(lambda: self._export("csv"))
        self._json.clicked.connect(lambda: self._export("json"))
        export_row.addWidget(self._csv)
        export_row.addWidget(self._json)
        export_row.addStretch(1)
        list_layout.addLayout(export_row)
        self._status = _label("", "Muted")
        self._status.setWordWrap(True)
        list_layout.addWidget(self._status)
        self._splitter.addWidget(list_panel)

        # Right: header + Reset, then metric tabs, the meter and the details under it.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(12)
        header_panel = _panel()
        header_layout = QHBoxLayout(header_panel)
        header_layout.setContentsMargins(18, 12, 18, 12)
        header_layout.setSpacing(18)
        self._header = _EncounterHeader()
        header_layout.addWidget(self._header, 1)
        self._reset = QPushButton("Reset encounter")
        self._reset.setObjectName("Danger")
        self._reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reset.setToolTip("Close the open encounter now.")
        self._reset.clicked.connect(self._on_reset)
        self._header.add_action(self._reset)
        right_layout.addWidget(header_panel)
        self._pane = _MeterPane("live", settings, details_below=True)
        self._pane.metric_changed.connect(self.metric_changed)
        right_layout.addWidget(self._pane, 1)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._restore_splitter()
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        self._show(None)
        self._update_count()

    # -- data in -----------------------------------------------------------------------
    def set_snapshot(self, snap: EncounterSnapshot | None) -> None:
        """Show the open encounter (or its final, closed snapshot); ``None`` is ignored."""
        if snap is None:
            return
        key = snap.key
        was_live = self._live_key == key
        is_new = key not in self._snaps
        flipped = not is_new and self._snaps[key].ours != snap.ours  # someone from the group joined in
        self._store(key, snap)
        if snap.closed:
            if was_live:
                self._live_key = None
            structural = is_new or was_live
        else:
            structural = is_new or not was_live
            self._live_key = key
        if not self._on_screen():
            # Nobody sees the list: keep the data and redraw once when the page shows again
            # (refreshing it several times a second while the window sat in the tray cost the
            # GUI thread 20 ms a snapshot and made the overlay animations stutter).
            self._stale = True
            return
        self._apply(key, structural or flipped)

    def add_encounter(self, snap: EncounterSnapshot | None, source: str | None = None) -> bool:
        """Insert (or refresh) one closed encounter; returns ``False`` for a duplicate.

        Imported encounters (``source`` = file name) that match a listed fight in time are
        skipped, so importing a session twice, or its .log and its .jsonl, lists it once.
        """
        if snap is None:
            return False
        key = snap.key if source is None else f"{source}:{snap.key}"
        if source is not None and key not in self._snaps and self._duplicate_of(snap) is not None:
            return False
        is_new = key not in self._snaps
        flipped = not is_new and self._snaps[key].ours != snap.ours
        self._store(key, snap)
        if source is not None:
            self._sources[key] = source
        was_live = self._live_key == key
        if was_live:
            self._live_key = None
        if not self._on_screen():
            self._stale = True
            return True
        self._apply(key, is_new or was_live or flipped)
        return True

    def update_encounter(self, snap: EncounterSnapshot | None) -> None:
        """Replace a listed fight's numbers with ``snap`` (same key): the engine counted it again
        because the party roster learned someone who fought in it.  A fight that was another
        group's and is now the group's appears in the list."""
        if snap is None:
            return
        key = snap.key
        old = self._snaps.get(key)
        if old is None:
            self.add_encounter(snap)
            return
        self._store(key, snap)
        visit = self._visit_of.get(key)
        if visit is not None:
            self._summary_cache.pop(visit, None)  # the zone summary holds the old numbers
        else:
            self._summary_cache.clear()
        if not self._on_screen():
            self._stale = True
            return
        self._apply(key, old.ours != snap.ours)

    def _on_screen(self) -> bool:
        """True while the page can be seen (shown, and its window not minimised)."""
        if self._batching:
            return True  # importing: _rebuild runs once at the end anyway
        win = self.window()
        return self.isVisible() and not (win is not None and win.isMinimized())

    def refresh_if_stale(self) -> None:
        """Bring the list up to date after snapshots arrived while it was not on screen."""
        if getattr(self, "_stale", False) and self._on_screen():
            self._stale = False
            self._rebuild()

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self.refresh_if_stale()

    def set_history(self, history: Sequence[EncounterSnapshot] | None) -> None:
        """Replace the list with ``history`` (oldest first, as :meth:`Engine.history` returns)."""
        self._batching = True
        try:
            self._snaps.clear()
            self._import_sources.clear()
            self._sources.clear()
            self._intervals.clear()
            self._summary_cache.clear()
            self._live_key = None
            self._imported_count = 0
            for snap in history or ():
                self.add_encounter(snap)
        finally:
            self._batching = False
        self._rebuild()

    # -- public ------------------------------------------------------------------------
    def set_pet_owner(self, pet: str, owner: str | None) -> None:
        """Rebuild imported encounters from their events after a saved owner correction."""
        changed = False
        for key, (stats, enc) in self._import_sources.items():
            stats.roster.set_pet_owner(pet, owner)
            if pet not in stats.canonical_map(enc).values():
                continue
            snap = build_snapshot(stats, enc, self._cfg.player_name)
            if snap != self._snaps.get(key):
                self._store(key, snap)
                changed = True
        if changed:
            self._summary_cache.clear()
            self._rebuild()

    def selected(self) -> EncounterSnapshot | None:
        """The snapshot shown on the right (an encounter, or a zone summary)."""
        return self._pane.snapshot()

    def select_key(self, key: str) -> bool:
        """Select the encounter row for ``key``; ``False`` if it is not listed."""
        item = self._find_item("enc", key)
        if item is None:
            return False
        self._tree.setCurrentItem(item)
        return True

    def select_zone(self, index: int = 0) -> bool:
        """Select the zone header ``index`` (0 = the newest visit)."""
        if not 0 <= index < self._tree.topLevelItemCount():
            return False
        self._tree.setCurrentItem(self._tree.topLevelItem(index))
        return True

    def zone_headers(self) -> list[tuple[str, int]]:
        """``(zone title, encounters)`` per header, top to bottom."""
        out: list[tuple[str, int]] = []
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            out.append((str(item.data(0, Qt.ItemDataRole.UserRole + 10)), item.childCount()))
        return out

    def listed_keys(self) -> list[str]:
        """Encounter keys in tree order (headers excluded)."""
        keys: list[str] = []
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            for j in range(top.childCount()):
                keys.append(str(top.child(j).data(0, Qt.ItemDataRole.UserRole)))
        return keys

    def set_metric(self, tab: str) -> None:
        """Select the ``"overview"`` / ``"damage"`` / ``"healing"`` / ``"taken"`` tab."""
        self._pane.set_metric(tab)

    def current_metric(self) -> str:
        """The selected metric tab key."""
        return self._pane.current_metric()

    def save_layout(self) -> None:
        """Persist the splitter and meter-pane layout."""
        self._settings.setValue("live/list_split", self._splitter.sizes())
        self._pane.save_layout()

    def set_config(self, cfg: Config) -> None:
        """Adopt a saved configuration (whether other groups' fights are listed)."""
        self._cfg = cfg
        show = bool(getattr(cfg, "show_other_groups", False))
        if show != self._show_others:
            self._show_others = show
            self._rebuild()

    def _listed(self, snap: EncounterSnapshot) -> bool:
        return self._show_others or bool(getattr(snap, "ours", True))

    # -- bookkeeping ---------------------------------------------------------------------
    def _store(self, key: str, snap: EncounterSnapshot) -> None:
        if key not in self._snaps:
            bisect.insort(self._intervals, (float(snap.start), float(snap.end), key))
        self._snaps[key] = snap

    def _duplicate_of(self, snap: EncounterSnapshot) -> str | None:
        """A listed encounter that is the same fight as ``snap`` (see IMPORT_OVERLAP_SHARE)."""
        players = {r.name for r in snap.rows if not r.is_npc}
        hi = bisect.bisect_right(self._intervals, (float(snap.end) + IMPORT_DUPLICATE_S, float("inf"), ""))
        for start, end, key in reversed(self._intervals[:hi]):
            if start < snap.start - IMPORT_LOOKBACK_S:
                break
            other = self._snaps.get(key)
            if other is None:
                continue
            overlap = min(end, snap.end) - max(start, snap.start)
            shorter = max(1.0, min(end - start, snap.end - snap.start))
            near = abs(start - snap.start) <= IMPORT_DUPLICATE_S
            if not (near or overlap >= IMPORT_OVERLAP_SHARE * shorter):
                continue
            if not players or players & {r.name for r in other.rows if not r.is_npc}:
                return key
        return None

    def _group_visits(self) -> list[_Visit]:
        """Zone visits, oldest first: consecutive encounters in the same zone stay."""
        visits: list[_Visit] = []
        for key in sorted(self._snaps, key=lambda k: (self._snaps[k].start, k)):
            snap = self._snaps[key]
            if not self._listed(snap):
                continue
            zone = snap.zone or ""
            since = float(snap.zone_since or 0.0) if zone else 0.0
            current = visits[-1] if visits else None
            same = current is not None and current.zone == zone and (
                (zone and abs(current_since_key(current) - since) < 1.0)
                or (not zone and snap.start - current.last_start <= UNKNOWN_ZONE_GAP_S)
            )
            if same:
                current.keys.append(key)
                current.last_start = snap.start
            else:
                anchor = since if (zone and since > 0) else snap.start
                visits.append(_Visit(f"{zone}|{since if zone else snap.start:.0f}", zone, anchor, [key], snap.start))
        return visits

    def _remember_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        if item.parent() is None:
            self._expanded[str(item.data(0, Qt.ItemDataRole.UserRole))] = expanded
            item.setData(0, _EncounterDelegate.OPEN_ROLE, expanded)

    # -- tree ----------------------------------------------------------------------------
    def _apply(self, key: str, structural: bool) -> None:
        if structural:
            self._rebuild()
            return
        item = self._find_item("enc", key)
        if item is not None:
            self._fill_encounter(item, key)
            parent = item.parent()
            visit = self._visit_by_key(self._visit_of.get(key))
            if parent is not None and visit is not None:
                self._fill_zone(parent, visit)
        if self._selection == ("enc", key):
            self._show(self._snaps.get(key))
        elif self._selection is not None and self._selection[0] == "zone" and self._visit_of.get(key) == self._selection[1]:
            self._show(self._summary(self._visit_by_key(self._selection[1])))
        self._update_count()

    def _visit_by_key(self, visit_key: str | None) -> _Visit | None:
        return next((v for v in self._visits if v.key == visit_key), None)

    def _rebuild(self) -> None:
        """Re-fill the tree and restore (or follow) the selection."""
        if self._batching:
            return
        self._visits = self._group_visits()
        self._visit_of = {k: v.key for v in self._visits for k in v.keys}
        live = self._live_key if self._live_key in self._snaps else None
        self._tree.blockSignals(True)
        try:
            self._tree.clear()
            newest = self._visits[-1].key if self._visits else None
            for visit in reversed(self._visits):
                top = QTreeWidgetItem()
                top.setData(0, Qt.ItemDataRole.UserRole, visit.key)
                top.setData(0, _EncounterDelegate.ZONE_ROLE, True)
                top.setData(0, Qt.ItemDataRole.UserRole + 10, zone_title(visit.zone))
                self._fill_zone(top, visit)
                keys = list(reversed(visit.keys))
                if live in keys:
                    keys.remove(live)
                    keys.insert(0, live)
                for key in keys:
                    child = QTreeWidgetItem()
                    child.setData(0, Qt.ItemDataRole.UserRole, key)
                    self._fill_encounter(child, key)
                    top.addChild(child)
                self._tree.addTopLevelItem(top)
                expanded = self._expanded.get(visit.key, visit.key == newest)
                top.setExpanded(expanded)
                top.setData(0, _EncounterDelegate.OPEN_ROLE, expanded)
            target = self._target_item()
            if target is not None:
                parent = target.parent()
                if parent is not None and not parent.isExpanded():
                    parent.setExpanded(True)
                    parent.setData(0, _EncounterDelegate.OPEN_ROLE, True)
                self._tree.setCurrentItem(target)
        finally:
            self._tree.blockSignals(False)
        self._on_selection()
        self._update_count()

    def _target_item(self) -> QTreeWidgetItem | None:
        """What to select after a rebuild: the newest fight when following, else the old pick."""
        first_visit = self._tree.topLevelItem(0) if self._tree.topLevelItemCount() else None
        newest = first_visit.child(0) if first_visit is not None and first_visit.childCount() else None
        if self._follow or self._selection is None:
            if self._selection is not None and self._selection[0] == "zone" and first_visit is not None:
                if first_visit.data(0, Qt.ItemDataRole.UserRole) == self._selection[1]:
                    return first_visit
            return newest or first_visit
        item = self._find_item(*self._selection)
        return item if item is not None else (newest or first_visit)

    def _find_item(self, kind: str, key: str) -> QTreeWidgetItem | None:
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            if kind == "zone":
                if top.data(0, Qt.ItemDataRole.UserRole) == key:
                    return top
                continue
            for j in range(top.childCount()):
                child = top.child(j)
                if child.data(0, Qt.ItemDataRole.UserRole) == key:
                    return child
        return None

    def _fill_zone(self, item: QTreeWidgetItem, visit: _Visit) -> None:
        snaps = [self._snaps[k] for k in visit.keys if k in self._snaps]
        n = len(snaps)
        combat = sum(s.duration for s in snaps)
        damage = sum(s.total_damage for s in snaps)
        your_damage = sum(row.damage for s in snaps if (row := owner_row(s, self._cfg.player_name or "You")) is not None)
        stamp = _hms(visit.since) if _is_today(visit.since) else f"{_day(visit.since)} {_hms(visit.since)}"
        verb = "entered" if visit.zone else "from"
        item.setText(0, f"{zone_title(visit.zone)}  ·  {n} encounter{'s' if n != 1 else ''}")
        you = f"  ·  you {your_damage / max(1.0, combat):,.1f} DPS" if your_damage else ""
        item.setData(
            0,
            _EncounterDelegate.LINE2_ROLE,
            f"{verb} {stamp}  ·  {_mmss(combat)} in combat  ·  {_num(damage)} dmg{you}",
        )
        item.setData(0, _EncounterDelegate.LIVE_ROLE, self._live_key in visit.keys)
        item.setToolTip(0, f"{zone_title(visit.zone)}: select for the summary of all {n} encounters")

    def _fill_encounter(self, item: QTreeWidgetItem, key: str) -> None:
        snap = self._snaps[key]
        source = self._sources.get(key)
        live = key == self._live_key
        you = owner_row(snap, self._cfg.player_name or "You")
        your_dps = f"you {you.dps:,.1f} DPS" if you is not None else "no own damage"
        item.setText(0, f"{_hms(snap.start)}   {snap.label}")
        head = "live  ·  " if live else ""
        tail = f"  ·  {source}" if source else ""
        item.setData(
            0,
            _EncounterDelegate.LINE2_ROLE,
            f"{head}{_mmss(snap.duration)}  ·  {_num(snap.total_damage)} dmg  ·  {your_dps}{tail}",
        )
        item.setData(0, _EncounterDelegate.LIVE_ROLE, live)
        origin = f"\nimported from {source}" if source else ""
        item.setToolTip(
            0,
            f"{snap.label}\n{snap.event_count} events, killed: {', '.join(snap.killed) or 'none'}"
            f"\nzone: {zone_title(snap.zone)}{origin}",
        )

    # -- selection -------------------------------------------------------------------------
    def _on_selection(self) -> None:
        items = self._tree.selectedItems()
        if not items:
            self._selection = None
            self._show(None)
            return
        item = items[0]
        key = str(item.data(0, Qt.ItemDataRole.UserRole))
        first_visit = self._tree.topLevelItem(0)
        if item.parent() is None:
            self._selection = ("zone", key)
            self._follow = False
            self._show(self._summary(self._visit_by_key(key)))
        else:
            self._selection = ("enc", key)
            self._follow = first_visit is not None and first_visit.childCount() > 0 and item is first_visit.child(0)
            self._show(self._snaps.get(key))

    def snapshot_for_item(self, item: QTreeWidgetItem | None) -> EncounterSnapshot | None:
        """The fight of an encounter row, or the summary of a zone header."""
        if item is None:
            return None
        key = str(item.data(0, Qt.ItemDataRole.UserRole))
        if item.parent() is None:
            return self._summary(self._visit_by_key(key))
        return self._snaps.get(key)

    def _tree_menu(self, pos: QPoint) -> None:
        item = self._tree.itemAt(pos)
        snap = self.snapshot_for_item(item)
        if snap is None:
            return
        menu = QMenu(self)
        label = "Copy zone summary to clipboard" if item.parent() is None else "Copy fight to clipboard"
        copy = menu.addAction(label)
        chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
        menu.deleteLater()
        if chosen is copy:
            self.copy_requested.emit(snap)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        """Right-click anywhere on the right-hand side: copy what it shows."""
        snap = self.selected()
        if snap is None:
            super().contextMenuEvent(event)
            return
        from mnmparse.app.overlay import add_group_entries, add_pet_entries

        menu = QMenu(self)
        zone = getattr(snap, "encounters", 1) > 1 or str(getattr(snap, "key", "")).startswith("zone:")
        copy = menu.addAction("Copy zone summary to clipboard" if zone else "Copy fight to clipboard")
        handlers = {copy: lambda: self.copy_requested.emit(snap)}
        handlers.update(add_group_entries(menu, self._pane.table.row_at(event.globalPos()),
                                          self.group_override_requested.emit))
        handlers.update(add_pet_entries(menu, self._pane.table.row_at(event.globalPos()), snap,
                                        self.pet_owner_requested.emit))
        chosen = menu.exec(event.globalPos())
        menu.deleteLater()
        action = handlers.get(chosen)
        if action is not None:
            action()

    def _summary(self, visit: _Visit | None) -> EncounterSnapshot | None:
        """The merged snapshot of every encounter of ``visit`` (closed part cached)."""
        if visit is None:
            return None
        closed = tuple(k for k in visit.keys if k != self._live_key and k in self._snaps)
        cached = self._summary_cache.get(visit.key)
        title = zone_title(visit.zone)
        if cached is None or cached[0] != closed:
            base = merge_snapshots(
                [self._snaps[k] for k in closed], key=f"zone:{visit.key}", label=title,
                zone=visit.zone, zone_since=visit.since,
            )
            self._summary_cache[visit.key] = (closed, base)
        else:
            base = cached[1]
        live = self._snaps.get(self._live_key) if self._live_key in visit.keys else None
        if live is None:
            return base if closed else None
        if not closed:
            return merge_snapshots([live], key=f"zone:{visit.key}", label=title, zone=visit.zone, zone_since=visit.since)
        return merge_snapshots([base, live], key=f"zone:{visit.key}", label=title, zone=visit.zone, zone_since=visit.since)

    def _show(self, snap: EncounterSnapshot | None) -> None:
        self._header.set_snapshot(snap)
        self._pane.set_snapshot(snap)
        self._reset.setEnabled(self._live_key is not None and self._live_key in self._snaps)
        self._csv.setEnabled(snap is not None)
        self._json.setEnabled(snap is not None)

    def _update_count(self) -> None:
        closed = sum(1 for snap in self._snaps.values() if snap.closed and self._listed(snap))
        parts: list[str] = []
        if closed:
            parts.append(f"{closed} closed")
        live = self._snaps.get(self._live_key) if self._live_key else None
        if live is not None and self._listed(live):
            parts.append("1 live")
        if self._visits:
            parts.append(f"{len(self._visits)} zone{'s' if len(self._visits) != 1 else ''}")
        hidden = sum(1 for snap in self._snaps.values() if not self._listed(snap))
        self._count.setText("  ·  ".join(parts) if parts else "none yet")
        self._count.setToolTip(
            f"{hidden} fight{'s' if hidden != 1 else ''} of other groups nearby hidden (Settings > General)"
            if hidden else ""
        )

    # -- actions ---------------------------------------------------------------------------
    def _on_reset(self) -> None:
        log.info("Reset encounter requested from the Live page")
        try:
            self._engine.reset_encounter()
        except Exception:  # noqa: BLE001 - the engine must never take the GUI down
            log.exception("reset_encounter failed")

    def _on_import(self) -> None:
        from mnmparse.importer import import_file
        from mnmparse.vocab import GLOBAL

        start_dir = str(project_path(self._cfg.log_dir))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import combat log", start_dir, "Combat logs (*.log *.jsonl);;All files (*)"
        )
        if not paths:
            return
        self.import_files(paths, vocab=GLOBAL)

    def import_files(self, paths: Sequence[str], *, vocab: Any = None) -> tuple[int, int]:
        """Import ``paths``; returns ``(encounters added, duplicates skipped)``.

        The files are imported together as one stretch of play (:func:`importer.import_files`:
        the party roster goes on from file to file, and the lines a restart of the app logged
        twice are left out); when that fails, each file on its own, so one bad file costs only
        itself.
        """
        from mnmparse.importer import import_file, import_files

        ownership = getattr(self._engine, "pet_owners", None)
        options: dict[str, Any] = dict(
            player_name=self._cfg.player_name,
            encounter_timeout_s=self._cfg.encounter_timeout_s,
            include_personal=bool(getattr(self._cfg, "include_personal", False)),
            vocab=vocab,
            dummy_fix=bool(getattr(self._cfg, "dummy_fix", False)),
            pet_owners=ownership() if callable(ownership) else {},
        )
        added = skipped = 0
        failures: list[str] = []
        results: list[Any] = []
        together = False
        if len(paths) > 1:
            try:
                results = import_files(list(paths), **options)
                together = True
            except Exception:  # noqa: BLE001 - fall back to one file at a time
                log.exception("importing %d files together failed; importing them one by one", len(paths))
                results = []
        if not together:
            for name in paths:
                try:
                    results.append(import_file(name, **options))
                except Exception as exc:  # noqa: BLE001 - a bad file must not take the page down
                    log.exception("import failed: %s", name)
                    failures.append(f"{Path(name).name}: {exc}")
        self._batching = True
        try:
            for result in results:
                sources = {f"{enc.start:.3f}": enc for enc in result.stats.history}
                for snap in result.encounters:
                    if self.add_encounter(snap, source=result.name):
                        added += 1
                        if snap.key in sources:
                            self._import_sources[f"{result.name}:{snap.key}"] = (result.stats, sources[snap.key])
                    else:
                        skipped += 1
                self.imported.emit(result)
        finally:
            self._batching = False
        self._imported_count += added
        self._rebuild()
        if failures:
            self._set_status("; ".join(failures), ok=False)
        else:
            dup = f" ({skipped} already listed)" if skipped else ""
            self._set_status(f"Imported {added} encounter{'s' if added != 1 else ''}{dup}.", ok=True)
        return added, skipped

    def _on_splitter_moved(self, _pos: int, _index: int) -> None:
        self._settings.setValue("live/list_split", self._splitter.sizes())

    def _restore_splitter(self) -> None:
        sizes = self._settings.value("live/list_split", None)
        if isinstance(sizes, list) and len(sizes) == 2:
            try:
                self._splitter.setSizes([max(280, int(sizes[0])), max(360, int(sizes[1]))])
                return
            except (TypeError, ValueError):
                log.debug("Ignoring bad live splitter sizes %r", sizes)
        self._splitter.setSizes([340, 800])

    def _set_status(self, text: str, *, ok: bool) -> None:
        self._status.setObjectName("Ok" if ok else "Problems")
        self._status.setText(text)
        self._status.style().unpolish(self._status)
        self._status.style().polish(self._status)

    def _export(self, fmt: str) -> None:
        snap = self._pane.snapshot()
        if snap is None:
            return
        try:
            path = exports_dir(self._cfg) / f"{_export_stem(snap)}.{fmt}"
            if fmt == "csv":
                export_csv(snap, path)
            else:
                export_json(snap, path)
        except OSError as exc:
            log.warning("Export failed: %s", exc)
            self._set_status(f"Export failed: {exc}", ok=False)
        else:
            self._set_status(f"Saved {path.name} in {path.parent}", ok=True)


def current_since_key(visit: _Visit) -> float:
    """The zone-entry time a visit was grouped by (its key's number)."""
    try:
        return float(visit.key.rsplit("|", 1)[1])
    except (IndexError, ValueError):
        return visit.since


# ----------------------------------------------------------------------------------
# Feed page
# ----------------------------------------------------------------------------------


class FeedPage(QWidget):
    """Full-height message feed with per-group filter chips, a search box and Copy all."""

    def __init__(
        self, engine: Engine, cfg: Config, settings: QSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._cfg = cfg
        self._max_lines = int(_cfg_get(cfg, "feed_max_lines", 500))
        self._lines: list[tuple[float, str, str]] = []
        self.setStyleSheet(_page_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        bar = FlowLayout(h_spacing=8, v_spacing=8)
        self._chips: list[tuple[QPushButton, tuple[str, ...]]] = []
        kind_colors: dict[str, str] = getattr(theme, "KIND_COLORS", {})
        for label, kinds in FEED_GROUPS:
            chip = QPushButton(label)
            chip.setObjectName("Chip")
            chip.setCheckable(True)
            chip.setChecked(True)
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            color = theme.YOU if label == "Hits" else kind_colors.get(kinds[0], theme.ACCENT)
            chip.setStyleSheet(
                f"QPushButton#Chip:checked {{ color: {color}; border-color: {color}; "
                f"background: {css_color(QColor(color), alpha=0.14)}; }}"
            )
            chip.toggled.connect(self._apply_filter)
            bar.addWidget(chip)
            self._chips.append((chip, kinds))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search…")
        self._search.setClearButtonEnabled(True)
        self._search.setMinimumWidth(220)
        self._search.textChanged.connect(self._on_search)
        bar.addWidget(self._search)
        self._copy = QPushButton("Copy all")
        self._copy.setObjectName("Chip")
        self._copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy.clicked.connect(self.copy_all)
        bar.addWidget(self._copy)
        layout.addLayout(bar)

        panel = _panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(8, 8, 8, 8)
        self.view = FeedView(max_lines=self._max_lines)
        panel_layout.addWidget(self.view)
        layout.addWidget(panel, 1)

        self._status = _label("No messages yet.", "Muted")
        layout.addWidget(self._status)

    # -- public ----------------------------------------------------------------------
    def append(
        self, text: str, kind: str, is_player_action: bool, ts: float, is_player_target: bool | None = None
    ) -> None:
        """Add one logged message (same arguments as :meth:`FeedView.append`)."""
        self._lines.append((ts, kind, text))
        if len(self._lines) > self._max_lines:
            del self._lines[: len(self._lines) - self._max_lines]
        self.view.append(text, kind, is_player_action, ts, is_player_target=is_player_target)
        if self.isVisible():  # a hidden page re-lays itself out for nothing
            self._update_status()

    def _update_status(self) -> None:
        if self._lines:
            self._status.setText(f"{len(self._lines)} messages kept  ·  last {_hms(self._lines[-1][0])}")

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._update_status()

    def copy_all(self) -> None:
        """Copy every kept line as ``[HH:MM:SS] text`` to the clipboard."""
        text = "\n".join(f"[{_hms(ts)}] {line}" for ts, _kind, line in self._lines)
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
        self._status.setText(f"Copied {len(self._lines)} lines to the clipboard.")

    def active_kinds(self) -> set[str] | None:
        """The kinds currently shown, or ``None`` when every chip is checked."""
        if all(chip.isChecked() for chip, _ in self._chips):
            return None
        kinds: set[str] = set()
        for chip, group in self._chips:
            if chip.isChecked():
                kinds.update(group)
        return kinds

    # -- internals -------------------------------------------------------------------
    def _apply_filter(self) -> None:
        self.view.set_filter(self.active_kinds())

    def _on_search(self, text: str) -> None:
        self.view.set_search(text)


# ----------------------------------------------------------------------------------
# Settings page
# ----------------------------------------------------------------------------------


def _field_lines(fields: dict[str, str], tail: str = "", per_line: int = 6) -> str:
    """``Fields: {a} {b} ...`` broken into lines of ``per_line`` fields (plus ``tail``)."""
    names = [f"{{{k}}}" for k in fields]
    rows = [" ".join(names[i:i + per_line]) for i in range(0, len(names), per_line)]
    lines = ["Fields: " + rows[0], *rows[1:]] if rows else []
    if tail:
        lines.append(tail)
    return "\n".join(lines)


def _export_preview(fmt: Any, player: str = "") -> str:
    """``fmt`` applied to a made-up fight (Settings > Export > Preview)."""
    from types import SimpleNamespace

    from mnmparse.export import format_snapshot

    def row(name: str, dps: float, share: float, hps: float = 0.0, taken: int = 0, utility: int = 0) -> Any:
        return SimpleNamespace(name=name, dps=dps, damage=int(dps * 48), share=share, max_hit=int(dps * 3),
                               hit_pct=74.0, hps=hps, heals=int(hps * 48), taken=taken, utility=utility,
                               is_npc=False)

    rows = [row(player or "You", 24.1, 0.39, 2.1, 420, 3), row("Brannoc", 19.6, 0.32, 0.0, 910, 6),
            row("Tamsin", 11.3, 0.18, 14.2, 60, 1), row("Wenna", 6.8, 0.11, 0.0, 130, 4)]
    snap = SimpleNamespace(label="a skeletal knight", zone="Wyrmsbane Tomb", duration=48.0,
                           start=time.time() - 60, total_damage=sum(r.damage for r in rows),
                           raid_dps=61.8, killed=["a skeletal knight"], encounters=1, rows=rows)
    return format_snapshot(snap, fmt) or "(nothing to show)"


class SettingsPage(QWidget):
    """The configuration form.  Save writes ``config.json`` and emits :attr:`config_changed`."""

    config_changed = Signal(object)
    #: Emitted when the user presses "Reset position" in the Overlay section.
    overlay_reset_requested = Signal()
    #: An Overlay-section control changed: ``(key, value)`` with key one of "visible",
    #: "locked", "click_through", "opacity", "font_scale", "tab", "attack_bar".  These apply
    #: to the overlay at once (no Save needed); :meth:`sync_overlay` mirrors the overlay back.
    overlay_setting_changed = Signal(str, object)
    #: The ▶ next to Export > Sound: play this built-in sound (the app owns the audio).
    sound_preview_requested = Signal(str)
    #: Download the latest wiki maps into the local cache without blocking Settings.
    map_download_requested = Signal()
    #: Check GitHub for an app update and download it in the background.
    app_update_requested = Signal()
    #: Restart to install an app update that is ready.
    app_restart_requested = Signal()

    def __init__(
        self, engine: Engine, cfg: Config, settings: QSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._settings = settings
        self._cfg = cfg
        self._loading = False
        self._captions: list[tuple[QWidget, QLabel, str]] = []
        self.setStyleSheet(_page_qss())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        self._content = QVBoxLayout(content)
        self._content.setContentsMargins(20, 18, 20, 18)
        self._content.setSpacing(12)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        self._build_app_updates()
        self._build_map_downloads()
        self._build_status()
        self._build_general()
        self._build_capture()
        self._build_ocr()
        self._build_crop()
        self._build_overlay()
        self._build_encounter()
        self._build_export()
        contact = QLabel("If you have any questions, contact @Maergoth in discord")
        contact.setObjectName("Muted")
        contact.setWordWrap(True)
        contact.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._content.addWidget(contact)
        self._fit_captions()
        self._content.addStretch(1)

        # Sticky footer: problems + Save / Revert.
        footer = _panel()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(18, 10, 18, 10)
        footer_layout.setSpacing(12)
        self._problems = _label("", "Problems")
        self._problems.setWordWrap(True)
        footer_layout.addWidget(self._problems, 1)
        self._status = _label("", "Muted")
        footer_layout.addWidget(self._status)
        self._revert = QPushButton("Revert")
        self._revert.setObjectName("Chip")
        self._revert.setFixedWidth(100)
        self._revert.clicked.connect(self.revert)
        footer_layout.addWidget(self._revert)
        self._save = QPushButton("Save")
        self._save.setObjectName("Primary")
        self._save.setFixedWidth(120)
        self._save.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save.clicked.connect(self.save)
        footer_layout.addWidget(self._save)
        footer_wrap = QVBoxLayout()
        footer_wrap.setContentsMargins(20, 8, 20, 18)  # a gap so clipped scroll content never touches the footer
        footer_wrap.addWidget(footer)
        outer.addLayout(footer_wrap)

        self._connect_form_signals()
        self.load(cfg)

    def _connect_form_signals(self) -> None:
        """Re-validate (problems text, Save enabled) whenever any field changes."""
        for edit in (self.player_name, self.log_dir, self.window_title):
            edit.textChanged.connect(self._on_form_changed)
        for combo in (self.capture_backend, self.overlay_tab):
            combo.currentIndexChanged.connect(self._on_form_changed)
        for spin in (self.feed_max_lines, self.fps, self.encounter_timeout, self.log_break, self.log_max):
            spin.valueChanged.connect(self._on_form_changed)
        for toggle in (self.start_on_launch, self.minimize_to_tray, self.include_personal, self.show_other_groups,
                       self.overlay_enabled, self.overlay_locked, self.overlay_click_through):
            toggle.toggled.connect(self._on_form_changed)
        # OCR engine/scale/preprocess and the crop picker are connected where they are built
        # (they also push the live config into the picker).

    # -- section builders ------------------------------------------------------------
    def _build_app_updates(self) -> None:
        self._app_update_ready = False
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        layout.addWidget(_label("App updates", "Heading"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.app_update_button = QPushButton("Update")
        self.app_update_button.setObjectName("Chip")
        self.app_update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.app_update_button.clicked.connect(self._request_app_update)
        row.addWidget(self.app_update_button)
        self.app_update_on_startup = QCheckBox("On startup")
        self.app_update_on_startup.setToolTip(
            "Download app updates from GitHub on startup and offer to restart when ready."
        )
        self.app_update_on_startup.setChecked(
            self._settings.value("app/update_on_startup", False, type=bool)
        )
        self.app_update_on_startup.toggled.connect(
            lambda enabled: self._settings.setValue("app/update_on_startup", enabled)
        )
        row.addWidget(self.app_update_on_startup)
        row.addStretch(1)
        layout.addLayout(row)
        self.app_update_status = _label("", "Muted")
        self.app_update_status.setTextFormat(Qt.TextFormat.PlainText)
        self.app_update_status.setWordWrap(True)
        self.app_update_status.hide()
        layout.addWidget(self.app_update_status)
        self._content.addWidget(panel)

    def _request_app_update(self) -> None:
        if self._app_update_ready:
            self.app_restart_requested.emit()
        else:
            self.app_update_requested.emit()

    def set_app_update_status(self, message: str, running: bool, ready: bool = False) -> None:
        """Offer a restart only after an app update is ready to install."""
        self._app_update_ready = ready
        self.app_update_button.setText("Restart to update" if ready else "Update")
        self.app_update_button.setEnabled(not running)
        self.app_update_status.setText(message)
        self.app_update_status.setVisible(bool(message))

    def _build_map_downloads(self) -> None:
        panel = _panel()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)
        layout.addWidget(_label("Map updates", "Heading"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.map_download_button = QPushButton("Download latest maps")
        self.map_download_button.setObjectName("Chip")
        self.map_download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.map_download_button.clicked.connect(self.map_download_requested.emit)
        row.addWidget(self.map_download_button)
        self.map_download_source = QLabel(
            'from <a href="https://monstersandmemories.miraheze.org/wiki/Category:Zones">wiki</a>'
        )
        self.map_download_source.setOpenExternalLinks(True)
        row.addWidget(self.map_download_source)
        self.map_download_on_startup = QCheckBox("On startup")
        self.map_download_on_startup.setChecked(
            self._settings.value("map/download_on_startup", False, type=bool)
        )
        self.map_download_on_startup.toggled.connect(
            lambda enabled: self._settings.setValue("map/download_on_startup", enabled)
        )
        row.addWidget(self.map_download_on_startup)
        row.addStretch(1)
        layout.addLayout(row)
        tip = _label(
            "Save space by placing the map over the chat window PNUT is logging. "
            "This works with the default window capture (WGC); desktop capture (mss) "
            "needs the chat to remain uncovered.",
            "Muted",
        )
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.map_download_status = _label("", "Muted")
        self.map_download_status.setTextFormat(Qt.TextFormat.PlainText)
        self.map_download_status.setWordWrap(True)
        self.map_download_status.hide()
        layout.addWidget(self.map_download_status)
        self._content.addWidget(panel)

    def set_map_download_status(self, message: str, running: bool) -> None:
        """Show map download progress and prevent duplicate requests."""
        self.map_download_button.setEnabled(not running)
        self.map_download_status.setText(message)
        self.map_download_status.setVisible(bool(message))

    def _section(self, title: str, blurb: str = "") -> QFormLayout:
        panel = _panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 14, 18, 14)
        panel_layout.setSpacing(8)
        panel_layout.addWidget(_label(title, "Heading"))
        if blurb:
            hint = _label(blurb, "Muted")
            hint.setWordWrap(True)
            panel_layout.addWidget(hint)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        form.setHorizontalSpacing(24)
        form.setVerticalSpacing(10)
        panel_layout.addLayout(form)
        self._content.addWidget(panel)
        return form

    def _row(self, form: QFormLayout, caption: str, field: QWidget | QHBoxLayout, hint: str = "") -> None:
        """Add a form row: a caption (plus an optional muted ``hint`` line) and the field.

        Captions share one column width (see :meth:`_fit_captions`) and wrap instead of
        clipping, so a long caption can never be cut off.
        """
        box = QWidget()
        box_layout = QVBoxLayout(box)
        box_layout.setContentsMargins(0, 0, 0, 0)
        box_layout.setSpacing(1)
        box_layout.addStretch(1)  # centred by stretches: an aligned layout cut long hints short
        label = QLabel(caption)
        label.setWordWrap(True)
        box_layout.addWidget(label)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("Muted")
            hint_label.setWordWrap(True)
            hint_label.setFont(_scaled_font(hint_label.font(), 0.9))
            box_layout.addWidget(hint_label)
        box_layout.addStretch(1)
        self._captions.append((box, label, caption))
        form.addRow(box, field)

    def _fit_captions(self) -> None:
        """Give every caption the width of the longest one (bounded; longer ones wrap)."""
        if not self._captions:
            return
        metrics = QFontMetrics(self._captions[0][1].font())
        widest = max(metrics.horizontalAdvance(text) for _box, _label, text in self._captions)
        width = max(_FORM_LABEL_WIDTH, min(widest + 8, _FORM_LABEL_MAX_WIDTH))
        for box, _label, _text in self._captions:
            box.setFixedWidth(width)

    @staticmethod
    def _line_edit(placeholder: str = "", width: int = 360) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedWidth(width)
        return edit

    @staticmethod
    def _combo(items: Sequence[str], width: int = 180) -> QComboBox:
        combo = QComboBox()
        combo.addItems(list(items))
        combo.setFixedWidth(width)
        return combo

    @staticmethod
    def _dspin(lo: float, hi: float, step: float, decimals: int = 1, suffix: str = "") -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(lo, hi)
        spin.setSingleStep(step)
        spin.setDecimals(decimals)
        spin.setSuffix(suffix)
        spin.setFixedWidth(120)
        return spin

    #: Engine state -> what the Status section says.
    _STATE_TEXT = {
        "stopped": "Stopped",
        "starting": "Starting",
        "no_window": "Waiting for the game window",
        "running": "Reading the Combat chat",
        "paused": "Paused",
    }

    def _build_status(self) -> None:
        form = self._section(
            "Status",
            "Live capture diagnostics (updated every second). A warning bar appears at the top of "
            "the window, and a small one on the overlay, when the chat is covered or hard to read.",
        )
        self._status_values: dict[str, QLabel] = {}
        for key, caption, hint in (
            ("state", "Capture", ""),
            ("window", "Game window", "Found read-only by its title; never touched"),
            ("fps", "Capture rate", "Frames read per second"),
            ("ocr", "OCR time", "Time to read one frame"),
            ("messages", "Messages logged", "Since capture started"),
            ("occluded", "Covered frames skipped", "A game panel was over the chat; lines scrolling by meanwhile are lost"),
            ("unreadable", "Unreadable recent lines", "Share of the last 40 lines that matched no known message"),
            ("replays", "Re-read lines skipped", "Old chat lines shown again (scrolled back, re-rendered) and not counted twice"),
        ):
            value = QLabel("-")
            value.setObjectName("Muted")
            _tabular(value)
            self._status_values[key] = value
            self._row(form, caption, value, hint)
        self.set_engine_state(getattr(self._engine, "state", "stopped") or "stopped")

    def set_engine_state(self, state: str) -> None:
        """Show the engine state in the Status section."""
        values = getattr(self, "_status_values", None)
        if not values:
            return
        values["state"].setText(self._STATE_TEXT.get(state, state))
        if state == "stopped":
            for key in ("window", "fps", "ocr"):
                values[key].setText("-")

    def set_status(self, status: dict[str, Any]) -> None:
        """Show one engine status dictionary (``Engine.status``) in the Status section."""
        values = getattr(self, "_status_values", None)
        if not values:
            return
        state = str(status.get("state", ""))
        if state:
            self.set_engine_state(state)
        values["window"].setText("found" if status.get("window_found") else "not found")
        fps = float(status.get("fps", 0.0) or 0.0)
        values["fps"].setText(f"{fps:.1f} fps (set to {float(getattr(self._cfg, 'fps', 0.0) or 0.0):g})")
        ocr = float(status.get("ocr_ms", 0.0) or 0.0)
        values["ocr"].setText(f"{ocr:.0f} ms" if ocr > 0 else "-")
        values["messages"].setText(f"{int(status.get('messages', 0) or 0):,}")
        values["occluded"].setText(f"{int(status.get('occluded', 0) or 0):,}")
        values["unreadable"].setText(f"{float(status.get('unreadable_pct', 0.0) or 0.0):.0f}%")
        values["replays"].setText(f"{int(status.get('replays', 0) or 0):,}")

    def _build_general(self) -> None:
        form = self._section("General")
        self.player_name = self._line_edit("Your character name (so You/YOU is attributed)")
        self.start_on_launch = ToggleSwitch()
        self.minimize_to_tray = ToggleSwitch()
        self.feed_max_lines = QSpinBox()
        self.feed_max_lines.setRange(50, 10000)
        self.feed_max_lines.setSingleStep(50)
        self.feed_max_lines.setFixedWidth(120)
        log_row = QHBoxLayout()
        log_row.setSpacing(8)
        self.log_dir = self._line_edit("logs")
        browse = QPushButton("Browse…")
        browse.setObjectName("Chip")
        browse.clicked.connect(self._browse_log_dir)
        log_row.addWidget(self.log_dir)
        log_row.addWidget(browse)
        log_row.addStretch(1)
        self._row(form, "Player name", self.player_name)
        self._row(form, "Start capture on launch", self.start_on_launch, "Begin reading the game as soon as the app opens")
        self._row(form, "Minimize to tray on close", self.minimize_to_tray)
        self.include_personal = ToggleSwitch()
        self._row(form, "Count personal lines in Session", self.include_personal, "Skill-ups, faction, XP and consider lines, which only you see")
        self.show_other_groups = ToggleSwitch()
        self._row(form, "Show other groups' fights", self.show_other_groups,
                  "When off, only fights involving you, your pets, or known group members are listed. "
                  "Nearby players may still appear in those fights. Combat chat always shows nearby activity.")
        self._row(form, "Log directory", log_row)
        self.log_break = self._dspin(1.0, 1440.0, 5.0, 0, " min")
        self.log_max = self._dspin(1.0, 500.0, 1.0, 0, " MB")
        self._row(form, "New log file after idle", self.log_break, "Minutes without a line before a new combat_*.log starts")
        self._row(form, "New log file at size", self.log_max, "A log file never grows past this")
        self._row(form, "Feed lines kept", self.feed_max_lines)

    def _build_capture(self) -> None:
        form = self._section(
            "Capture",
            "The game window is captured read-only through Windows Graphics Capture (never "
            "includes other windows). \"mss\" grabs the desktop rectangle instead and sees "
            "whatever is on top of the game, including the overlay if it covers the chat.",
        )
        self.window_title = self._line_edit("Monsters and Memories")
        self.capture_backend = self._combo(CAPTURE_BACKENDS)
        self.fps = self._dspin(0.5, 30.0, 0.5, 1, " fps")
        self._row(form, "Window title", self.window_title)
        self._row(form, "Backend", self.capture_backend)
        self._row(form, "Capture rate", self.fps, "Frames read per second; 6 keeps up with a busy chat")

    def _build_ocr(self) -> None:
        form = self._section("OCR", "Windows OCR with max-channel preprocessing at 1x is the measured sweet spot.")
        self.ocr_engine = self._combo(OCR_ENGINES)
        self.ocr_scale = self._dspin(0.5, 4.0, 0.25, 2, "x")
        self.preprocess = self._combo(PREPROCESS_MODES)
        self._row(form, "Engine", self.ocr_engine)
        self._row(form, "Upscale", self.ocr_scale)
        self._row(form, "Preprocess", self.preprocess)
        for combo in (self.ocr_engine, self.preprocess):
            combo.currentIndexChanged.connect(self._sync_picker_config)
        self.ocr_scale.valueChanged.connect(self._sync_picker_config)

    def _build_crop(self) -> None:
        panel = _panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 14, 18, 14)
        panel_layout.setSpacing(8)
        panel_layout.addWidget(_label("Crop", "Heading"))
        hint = _label(
            "The rectangle of the Combat chat text in game-window pixels. Capture a frame, "
            "drag the rectangle over the text, then Test OCR to check what is read.",
            "Muted",
        )
        hint.setWordWrap(True)
        panel_layout.addWidget(hint)
        self.crop_picker = CropPicker(self._engine, self._cfg)
        self.crop_picker.setMinimumHeight(420)
        self.crop_picker.crop_changed.connect(self._on_form_changed)
        panel_layout.addWidget(self.crop_picker)
        self._content.addWidget(panel)

    def _build_overlay(self) -> None:
        form = self._section(
            "Overlay",
            "Our own translucent always-on-top window. These controls change the overlay right away "
            "and follow changes made on the overlay itself. Locked means it cannot be moved or resized "
            "(tabs, sorting and hover breakdowns keep working). Ignore the mouse makes it fully "
            "click-through.",
        )
        self.overlay_enabled = ToggleSwitch()
        self.overlay_locked = ToggleSwitch()
        self.overlay_click_through = ToggleSwitch()
        self.attack_bar = ToggleSwitch()
        self.attack_bar.toggled.connect(self._on_form_changed)
        percent = _percent_text
        self.overlay_opacity = SliderRow("", 20, 100, 85, formatter=percent)
        self.overlay_opacity.setFixedWidth(360)
        self.overlay_opacity.value_changed.connect(self._on_form_changed)
        self.overlay_font_scale = SliderRow("", 60, 200, 100, formatter=percent)  # = the overlay's A-/A+ range
        self.overlay_font_scale.setFixedWidth(360)
        self.overlay_font_scale.value_changed.connect(self._on_form_changed)
        self.overlay_tab = self._combo(_OVERLAY_TABS)
        reset = QPushButton("Reset position")
        reset.setObjectName("Chip")
        reset.setToolTip("Put the overlay back at its default place on the primary screen.")
        reset.clicked.connect(self._reset_overlay_position)
        self._row(form, "Show overlay", self.overlay_enabled)
        self._row(form, "Locked", self.overlay_locked, "No moving or resizing; tabs and hover breakdowns still work")
        self._row(form, "Ignore the mouse", self.overlay_click_through, "Click-through: clicks reach the game, tabs and tooltips stop working")
        self._row(form, "Background opacity", self.overlay_opacity)
        self._row(form, "Font scale", self.overlay_font_scale)
        self._row(form, "Tab", self.overlay_tab)
        self._row(form, "Auto-attack bar", self.attack_bar,
                  "Swing timer under the overlay, learned from your own swings; unlock the overlay to drag it off")
        self._row(form, "Position", reset)
        live = (
            (self.overlay_enabled.toggled, "visible", lambda v: bool(v)),
            (self.overlay_locked.toggled, "locked", lambda v: bool(v)),
            (self.overlay_click_through.toggled, "click_through", lambda v: bool(v)),
            (self.attack_bar.toggled, "attack_bar", lambda v: bool(v)),
            (self.overlay_opacity.value_changed, "opacity", lambda v: int(v) / 100.0),
            (self.overlay_font_scale.value_changed, "font_scale", lambda v: int(v) / 100.0),
            (self.overlay_tab.currentTextChanged, "tab", lambda v: str(v)),
        )
        for signal, key, convert in live:
            signal.connect(lambda value, k=key, c=convert: self._emit_overlay_setting(k, c(value)))

    #: overlay setting key -> (Config field, form widget attribute)
    _OVERLAY_FIELDS = {
        "visible": "overlay_enabled",
        "locked": "overlay_locked",
        "click_through": "overlay_click_through",
        "opacity": "overlay_opacity",
        "font_scale": "overlay_font_scale",
        "tab": "overlay_tab",
        "attack_bar": "attack_bar",
    }

    def _emit_overlay_setting(self, key: str, value: Any) -> None:
        if not self._loading:
            self.overlay_setting_changed.emit(key, value)

    def sync_overlay(self, **values: Any) -> None:
        """Show the overlay's current state in the form without re-applying it.

        Also moves the Revert baseline, so changes made on the overlay are not reported
        as unsaved edits.
        """
        changes: dict[str, Any] = {}
        self._loading = True
        try:
            for key, value in values.items():
                field_name = self._OVERLAY_FIELDS.get(key)
                if field_name is None or value is None:
                    continue
                if key in ("opacity", "font_scale"):
                    getattr(self, field_name).set_value(int(round(float(value) * 100)))
                    value = round(float(value), 2)
                elif key == "tab":
                    self._select(self.overlay_tab, str(value))
                else:
                    getattr(self, field_name).setChecked(bool(value))
                changes[field_name] = value
        finally:
            self._loading = False
        known = {f.name for f in fields(Config)}
        changes = {k: v for k, v in changes.items() if k in known}
        if changes:
            self._cfg = dataclasses.replace(self._cfg, **changes)

    def _build_encounter(self) -> None:
        form = self._section("Encounter")
        self.dummy_fix = ToggleSwitch()
        self.dummy_fix.toggled.connect(self._on_form_changed)
        self.encounter_timeout = self._dspin(1.0, 300.0, 1.0, 0, " s")
        self._row(form, "Timeout without damage", self.encounter_timeout, "Seconds of no hits or swings before the encounter closes")
        self._row(
            form,
            "Dummy Fix",
            self.dummy_fix,
            "A hit or heal whose number could not be read counts as this zone's average for the same attack "
            "or spell (marked in the hover breakdown). Off: such lines are left out.",
        )

    _SORT_TITLES = {"damage": "Damage", "healing": "Healing", "taken": "Damage taken", "utility": "Utility"}

    def _build_export(self) -> None:
        from mnmparse.export import ACTOR_FIELDS, LINE_FIELDS, PRESETS
        from mnmparse.triggers import BUILTIN_SOUNDS

        form = self._section(
            "Export",
            "One line per fight for the clipboard, to paste into a chat. Right-click a fight in Live, "
            "or click the copy mark in the overlay's header, to copy one by hand.",
        )
        self.export_auto = ToggleSwitch()
        self._row(form, "Copy each finished fight", self.export_auto, "Fights your group took part in")
        self.export_sound = self._combo(["None", *BUILTIN_SOUNDS], 160)
        play = QPushButton("▶")
        play.setObjectName("Chip")
        play.setFixedWidth(36)
        play.setCursor(Qt.CursorShape.PointingHandCursor)
        play.setToolTip("Play it (volume and output device: the Triggers page)")
        play.clicked.connect(self._preview_export_sound)
        sound_row = QHBoxLayout()
        sound_row.setSpacing(8)
        sound_row.addWidget(self.export_sound)
        sound_row.addWidget(play)
        sound_row.addStretch(1)
        self._row(form, "Sound when copied", sound_row)
        self.export_preset = self._combo([*PRESETS, "Custom"], 180)
        self._row(form, "Format", self.export_preset, "Editing a field below makes it Custom")
        self.export_line = self._line_edit("{title} [{duration}] {dps} DPS - {actors}", 460)
        self._row(form, "Line", self._with_help(
            self.export_line,
            _field_lines(LINE_FIELDS, "A format spec works too, e.g. {dps:.0f}"),
            "\n".join(f"{{{k}}}: {v}" for k, v in LINE_FIELDS.items()),
        ))
        self.export_actor = self._line_edit("{name} {dps}", 460)
        self._row(form, "Each person", self._with_help(
            self.export_actor,
            _field_lines(ACTOR_FIELDS),
            "\n".join(f"{{{k}}}: {v}" for k, v in ACTOR_FIELDS.items()),
        ))
        self.export_separator = self._line_edit(", ", 120)
        self._row(form, "Between people", self.export_separator)
        self.export_sort = self._combo(list(self._SORT_TITLES.values()), 160)
        self.export_max = QSpinBox()
        self.export_max.setRange(1, 40)
        self.export_max.setFixedWidth(90)
        order_row = QHBoxLayout()
        order_row.setSpacing(8)
        order_row.addWidget(self.export_sort)
        order_row.addWidget(QLabel("at most"))
        order_row.addWidget(self.export_max)
        order_row.addWidget(QLabel("people"))
        order_row.addStretch(1)
        self._row(form, "Order by", order_row, "Enemies are never listed")
        self.export_preview = QLabel("")
        self.export_preview.setObjectName("Muted")
        self.export_preview.setWordWrap(True)
        self.export_preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.export_preview.setFixedWidth(460)
        preview_box = QWidget()
        preview_layout = QHBoxLayout(preview_box)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(self.export_preview)
        preview_layout.addStretch(1)
        self._row(form, "Preview", preview_box, "A made-up fight")
        for edit in (self.export_line, self.export_actor, self.export_separator):
            edit.textChanged.connect(self._on_export_edited)
        self.export_sort.currentIndexChanged.connect(self._on_export_edited)
        self.export_max.valueChanged.connect(self._on_export_edited)
        self.export_preset.currentIndexChanged.connect(self._on_export_preset)
        self.export_auto.toggled.connect(self._on_form_changed)
        self.export_sound.currentIndexChanged.connect(self._on_form_changed)
        self.player_name.textChanged.connect(self._refresh_export_preview)  # the preview shows your name

    @staticmethod
    def _with_help(field: QWidget, text: str, tooltip: str = "") -> QWidget:
        """``field`` with a muted, wrapping help line under it (as wide as the field)."""
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(field)
        help_label = QLabel(text)  # explicit lines (word wrap in a form row can be cut short)
        help_label.setObjectName("Muted")
        help_label.setFont(_scaled_font(help_label.font(), 0.9))
        if tooltip:
            help_label.setToolTip(tooltip)
            field.setToolTip(tooltip)
        lay.addWidget(help_label)
        return box

    def _export_format(self) -> Any:
        from mnmparse.export import ExportFormat

        sort = next((k for k, v in self._SORT_TITLES.items() if v == self.export_sort.currentText()), "damage")
        return ExportFormat(self.export_line.text(), self.export_actor.text(), self.export_separator.text(),
                            sort, int(self.export_max.value()))

    def _set_export_format(self, fmt: Any) -> None:
        self.export_line.setText(fmt.line)
        self.export_actor.setText(fmt.actor)
        self.export_separator.setText(fmt.separator)
        self._select(self.export_sort, self._SORT_TITLES.get(fmt.sort, "Damage"))
        self.export_max.setValue(int(fmt.max_actors))

    def _on_export_preset(self) -> None:
        from mnmparse.export import PRESETS

        if self._loading or getattr(self, "_export_syncing", False):
            return
        preset = PRESETS.get(self.export_preset.currentText())
        if preset is None:
            return
        self._export_syncing = True
        try:
            self._set_export_format(preset)
        finally:
            self._export_syncing = False
        self._on_export_edited()

    def _on_export_edited(self) -> None:
        from mnmparse.export import preset_for

        fmt = self._export_format()
        if not getattr(self, "_export_syncing", False):
            self._export_syncing = True
            try:
                self._select(self.export_preset, preset_for(fmt))
            finally:
                self._export_syncing = False
        self._refresh_export_preview()
        self._on_form_changed()

    def _refresh_export_preview(self) -> None:
        self.export_preview.setText(_export_preview(self._export_format(), self.player_name.text().strip()))
        # a wrapped label keeps its old height unless asked again (the form row does not)
        self.export_preview.setMinimumHeight(0)
        self.export_preview.setMinimumHeight(self.export_preview.heightForWidth(self.export_preview.width()))

    def _preview_export_sound(self) -> None:
        name = self.export_sound.currentText()
        if name and name != "None":
            self.sound_preview_requested.emit(name)

    # -- public ----------------------------------------------------------------------
    def load(self, cfg: Config) -> None:
        """Fill the form from ``cfg`` (also the baseline for Revert)."""
        self._cfg = cfg
        self._loading = True
        try:
            self.player_name.setText(cfg.player_name)
            self.start_on_launch.setChecked(bool(_cfg_get(cfg, "start_capture_on_launch", False)))
            self.minimize_to_tray.setChecked(bool(_cfg_get(cfg, "minimize_to_tray", True)))
            self.include_personal.setChecked(bool(_cfg_get(cfg, "include_personal", False)))
            self.show_other_groups.setChecked(bool(_cfg_get(cfg, "show_other_groups", False)))
            self.dummy_fix.setChecked(bool(_cfg_get(cfg, "dummy_fix", False)))
            self.attack_bar.setChecked(bool(_cfg_get(cfg, "attack_bar", True)))
            self.feed_max_lines.setValue(int(_cfg_get(cfg, "feed_max_lines", 500)))
            self.log_break.setValue(float(_cfg_get(cfg, "log_break_minutes", 60.0)))
            self.log_max.setValue(float(_cfg_get(cfg, "log_max_mb", 5.0)))
            self.log_dir.setText(cfg.log_dir)
            self.window_title.setText(cfg.window_title)
            self._select(self.capture_backend, cfg.capture_backend)
            self.fps.setValue(float(cfg.fps))
            self._select(self.ocr_engine, cfg.ocr_engine)
            self.ocr_scale.setValue(float(cfg.ocr_scale))
            self._select(self.preprocess, cfg.preprocess)
            self.crop_picker.set_crop(tuple(int(v) for v in cfg.crop))
            self.crop_picker.set_config(cfg)
            self.overlay_enabled.setChecked(bool(_cfg_get(cfg, "overlay_enabled", False)))
            self.overlay_locked.setChecked(bool(_cfg_get(cfg, "overlay_locked", True)))
            self.overlay_click_through.setChecked(bool(_cfg_get(cfg, "overlay_click_through", False)))
            self.overlay_opacity.set_value(int(round(float(_cfg_get(cfg, "overlay_opacity", 0.85)) * 100)))
            self.overlay_font_scale.set_value(
                int(round(float(_cfg_get(cfg, "overlay_font_scale", 1.0)) * 100))
            )
            self._select(self.overlay_tab, str(_cfg_get(cfg, "overlay_tab", "damage")))
            self.encounter_timeout.setValue(float(cfg.encounter_timeout_s))
            from mnmparse.export import format_from_config, preset_for

            fmt = format_from_config(cfg)
            self.export_auto.setChecked(bool(_cfg_get(cfg, "export_auto", True)))
            self._select(self.export_sound, str(_cfg_get(cfg, "export_sound", "Dink") or "None"))
            self._set_export_format(fmt)
            self._select(self.export_preset, preset_for(fmt))
            self._refresh_export_preview()
        finally:
            self._loading = False
        self._validate()

    def form_config(self) -> Config:
        """Build a :class:`Config` from the current form values (not saved)."""
        values: dict[str, Any] = {
            "player_name": self.player_name.text().strip(),
            "start_capture_on_launch": self.start_on_launch.isChecked(),
            "minimize_to_tray": self.minimize_to_tray.isChecked(),
            "include_personal": self.include_personal.isChecked(),
            "show_other_groups": self.show_other_groups.isChecked(),
            "dummy_fix": self.dummy_fix.isChecked(),
            "attack_bar": self.attack_bar.isChecked(),
            "feed_max_lines": int(self.feed_max_lines.value()),
            "log_break_minutes": float(self.log_break.value()),
            "log_max_mb": float(self.log_max.value()),
            "log_dir": self.log_dir.text().strip() or "logs",
            "window_title": self.window_title.text().strip(),
            "capture_backend": self.capture_backend.currentText(),
            "fps": float(self.fps.value()),
            "ocr_engine": self.ocr_engine.currentText(),
            "ocr_scale": float(self.ocr_scale.value()),
            "preprocess": self.preprocess.currentText(),
            "crop": tuple(int(v) for v in self.crop_picker.crop()),
            "overlay_enabled": self.overlay_enabled.isChecked(),
            "overlay_locked": self.overlay_locked.isChecked(),
            "overlay_click_through": self.overlay_click_through.isChecked(),
            "overlay_opacity": self.overlay_opacity.value() / 100.0,
            "overlay_font_scale": self.overlay_font_scale.value() / 100.0,
            "overlay_tab": self.overlay_tab.currentText(),
            "encounter_timeout_s": float(self.encounter_timeout.value()),
            "export_auto": self.export_auto.isChecked(),
            "export_sound": "" if self.export_sound.currentText() == "None" else self.export_sound.currentText(),
        }
        fmt = self._export_format()
        values.update({
            "export_line": fmt.line,
            "export_actor": fmt.actor,
            "export_separator": fmt.separator,
            "export_sort": fmt.sort,
            "export_max_actors": fmt.max_actors,
        })
        known = {f.name for f in fields(Config)}
        unknown = sorted(set(values) - known)
        if unknown:
            log.debug("Config has no field(s) %s; those settings are not persisted", unknown)
        return dataclasses.replace(self._cfg, **{k: v for k, v in values.items() if k in known})

    @staticmethod
    def problems_for(cfg: Config) -> list[str]:
        """``Config.problems()`` plus the page-level checks the form needs.

        An empty window title passes ``Config.problems`` but can never match a window
        (``find_game_window`` returns ``None``), so capture would retry forever.
        """
        problems = list(cfg.problems())
        if not str(cfg.window_title or "").strip():
            problems.append("Window title must not be empty (the game's window title, e.g. \"Monsters and Memories\")")
        return problems

    def save(self) -> None:
        """Validate, write ``config.json``, push to the engine and emit :attr:`config_changed`."""
        cfg = self.form_config()
        problems = self.problems_for(cfg)
        self._show_problems(problems)
        if problems:
            self._status.setText("Not saved: fix the problems first.")
            return
        try:
            save_config(cfg, DEFAULT_CONFIG_PATH)
        except OSError as exc:
            log.warning("Could not save config: %s", exc)
            self._show_problems([f"Could not write {DEFAULT_CONFIG_PATH}: {exc}"])
            return
        self._cfg = cfg
        self.crop_picker.set_config(cfg)
        try:
            self._engine.update_config(cfg)
        except Exception:  # noqa: BLE001 - keep the GUI alive whatever the engine does
            log.exception("engine.update_config failed")
        self._status.setText(f"Saved {DEFAULT_CONFIG_PATH.name} at {time.strftime('%H:%M:%S')}")
        self.config_changed.emit(cfg)

    def revert(self) -> None:
        """Discard unsaved edits (reload the last saved/loaded configuration)."""
        self.load(self._cfg)
        self._status.setText("Reverted.")

    # -- internals -------------------------------------------------------------------
    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        index = combo.findText(value)
        if index < 0:
            combo.addItem(value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def _browse_log_dir(self) -> None:
        start = str(project_path(self.log_dir.text().strip() or "logs"))
        chosen = QFileDialog.getExistingDirectory(self, "Choose the log directory", start)
        if chosen:
            self.log_dir.setText(chosen)
            self._on_form_changed()

    def _sync_picker_config(self) -> None:
        if self._loading:
            return
        self.crop_picker.set_config(self.form_config())
        self._on_form_changed()

    def _on_form_changed(self) -> None:
        if not self._loading:
            self._validate()

    def _validate(self) -> list[str]:
        problems = self.problems_for(self.form_config())
        self._show_problems(problems)
        return problems

    def _show_problems(self, problems: Sequence[str]) -> None:
        self._problems.setText("\n".join(problems))
        self._problems.setVisible(bool(problems))
        self._save.setEnabled(not problems)

    def _reset_overlay_position(self) -> None:
        self._settings.beginGroup("overlay")
        try:
            self._settings.remove("geometry")
        finally:
            self._settings.endGroup()
        log.info("Overlay position reset requested")
        self.overlay_reset_requested.emit()
        self._status.setText("Overlay position reset.")


# ----------------------------------------------------------------------------------
# About page
# ----------------------------------------------------------------------------------


class AboutPage(QWidget):
    """Version, safety posture, terms of service and links to the logs folder / config.json."""

    def __init__(
        self,
        engine: Engine | None,
        cfg: Config,
        settings: QSettings | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.setStyleSheet(_page_qss())
        app_name = getattr(_app_pkg, "APP_NAME", "PNUT M&M")
        version = getattr(_app_pkg, "APP_VERSION", "dev")

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        head = _panel()
        head_layout = QVBoxLayout(head)
        head_layout.setContentsMargins(18, 14, 18, 14)
        head_layout.addWidget(_label(app_name, "Title"))
        head_layout.addWidget(_label(_app_pkg.APP_TAGLINE, "Muted"))
        head_layout.addWidget(
            _label(f"version {version}  ·  Monsters & Memories combat parser, screen-read edition", "Muted")
        )
        layout.addWidget(head)

        layout.addWidget(self._text_panel("Safety posture", _SAFETY_TEXT))
        layout.addWidget(self._text_panel("Terms of service", _TERMS_TEXT))

        links = _panel()
        links_layout = QVBoxLayout(links)
        links_layout.setContentsMargins(18, 14, 18, 14)
        links_layout.setSpacing(6)
        links_layout.addWidget(_label("Files", "Heading"))
        logs_path = project_path(cfg.log_dir)
        links_layout.addWidget(self._link("Open the logs folder", logs_path, is_dir=True))
        links_layout.addWidget(self._link("Open config.json", DEFAULT_CONFIG_PATH))
        layout.addWidget(links)
        layout.addStretch(1)

    def _text_panel(self, title: str, text: str) -> QFrame:
        panel = _panel()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 14, 18, 14)
        panel_layout.setSpacing(8)
        panel_layout.addWidget(_label(title, "Heading"))
        for paragraph in text.split("\n\n"):
            body = QLabel(paragraph)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            panel_layout.addWidget(body)
        return panel

    def _link(self, caption: str, path: Path, *, is_dir: bool = False) -> QLabel:
        label = QLabel(f'<a href="{QUrl.fromLocalFile(str(path)).toString()}" style="color:{theme.ACCENT2}">{caption}</a>'
                       f'  <span style="color:{theme.MUTED}">{path}</span>')
        label.setObjectName("Link")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        label.setCursor(Qt.CursorShape.PointingHandCursor)

        def _open(_href: str) -> None:
            target = path
            if is_dir:
                target.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                log.info("%s does not exist yet", target)
                return
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

        label.linkActivated.connect(_open)
        return label


# ---------------------------------------------------------------------------
# Session page (loot, coin, crafting, kills, deaths, crowd control for the run)
# ---------------------------------------------------------------------------


class SessionPage(QWidget):
    """Everything the group produced outside the damage numbers, for the whole run.

    Driven by ``Engine.session`` snapshots (see :mod:`mnmparse.session`); the view itself
    computes nothing.  Imported log files (Live > Import log) appear in the source
    selector next to the live session.  Export writes the shown snapshot as JSON under
    ``logs/exports/``.
    """

    def __init__(
        self, engine: Engine, cfg: Config, settings: QSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        from mnmparse.app.session_view import SessionView

        self._engine = engine
        self._cfg = cfg
        self._live: Any | None = None
        self._imported: dict[str, Any] = {}
        self.setStyleSheet(_page_qss())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(_label("Session", "Heading"))
        self._source = QComboBox()
        self._source.addItem("Live session", None)
        self._source.setMinimumWidth(220)
        self._source.currentIndexChanged.connect(self._on_source_changed)
        top.addWidget(self._source)
        self._mine = ToggleSwitch()
        self._mine.setToolTip("Only your own loot, coin, kills, deaths and crafts")
        self._mine.toggled.connect(self._on_mine_toggled)
        top.addSpacing(12)
        top.addWidget(_label("Mine only", "Muted"))
        top.addWidget(self._mine)
        self._since = _label("", "Muted")
        top.addWidget(self._since)
        top.addStretch(1)
        self._export = QPushButton("Export JSON")
        self._export.clicked.connect(self._on_export)
        self._reset = QPushButton("Reset session")
        self._reset.clicked.connect(self._on_reset)
        top.addWidget(self._export)
        top.addWidget(self._reset)
        layout.addLayout(top)

        self._view = SessionView(compact=False, parent=self)
        layout.addWidget(self._view, 1)
        self._status = _label("", "Muted")
        layout.addWidget(self._status)

        getter = getattr(engine, "session_snapshot", None)
        if callable(getter):
            try:
                self.set_session(getter())
            except Exception:  # noqa: BLE001
                log.debug("initial session snapshot unavailable", exc_info=True)

    # -- data in -----------------------------------------------------------------------
    def set_session(self, snap: Any | None) -> None:
        """Replace the live session snapshot (shown when "Live session" is selected)."""
        self._live = snap
        if self._source.currentData() is None:
            self._display(snap)

    def add_imported_session(self, name: str, snap: Any | None) -> None:
        """Add (or refresh) an imported file's session and show it."""
        if snap is None:
            return
        self._imported[name] = snap
        index = self._source.findData(name)
        if index < 0:
            self._source.addItem(f"Imported: {name}", name)
            index = self._source.count() - 1
        self._source.setCurrentIndex(index)
        self._display(snap)

    def shown(self) -> Any | None:
        """The snapshot currently displayed."""
        key = self._source.currentData()
        return self._live if key is None else self._imported.get(str(key))

    # -- internals ---------------------------------------------------------------------
    def _display(self, snap: Any | None) -> None:
        self._view.set_snapshot(snap)
        if snap is not None:
            started = time.strftime("%b %d %H:%M:%S", time.localtime(float(getattr(snap, "started", 0.0) or 0.0)))
            self._since.setText(f"since {started}")
        else:
            self._since.setText("")
        self._reset.setEnabled(self._source.currentData() is None)

    def _on_source_changed(self, _index: int) -> None:
        self._display(self.shown())

    def _on_mine_toggled(self, checked: bool) -> None:
        self._view.set_view_mode("self" if checked else "group", self._cfg.player_name or "You")

    def _on_reset(self) -> None:
        reset = getattr(self._engine, "reset_session", None)
        if callable(reset):
            reset()
            self._status.setText("Session counters reset.")

    def _on_export(self) -> None:
        snap = self.shown()
        if snap is None:
            self._status.setText("Nothing to export yet.")
            return
        try:
            out_dir = Path(project_path(self._cfg.log_dir)) / "exports"
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            label = self._source.currentData() or "live"
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label))[:40]
            path = out_dir / f"session_{safe}_{stamp}.json"
            payload = dataclasses.asdict(snap)
            payload["kills_per_hour"] = round(snap.kills_per_hour, 2)
            payload["items_per_hour"] = round(snap.items_per_hour, 2)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            self._status.setText(f"Exported {path}")
        except Exception as exc:  # noqa: BLE001
            log.exception("session export failed")
            self._status.setText(f"Export failed: {exc}")


# The Triggers page lives in its own module (it imports the helpers above).
from mnmparse.app.triggers_page import TriggersPage  # noqa: E402,F401
