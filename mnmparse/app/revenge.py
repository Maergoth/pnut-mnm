"""Opt-in PvP reminder list. Never compares teammates or exposes raw combat chat."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QCloseEvent, QCursor, QHideEvent, QMouseEvent, QMoveEvent, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from PySide6.QtWidgets import QSizePolicy

from mnmparse.app import theme
from mnmparse.app.crop_picker import button_qss
from mnmparse.app.widgets import ElidedLabel, OverlaySizeGrip
from mnmparse.app.window_identity import window_title
from mnmparse.config import project_path
from mnmparse.grammar import is_you
from mnmparse.revenge_csv import CsvEntry, KINDS, RevengeCsvError, format_csv, parse_csv, parse_names, validate_entries
from mnmparse.storage import atomic_json

PROMPT_SECONDS = 30.0
MAX_SAVED_NAMES = 1000
MAX_NAME_LENGTH = 32767  # QLineEdit's default technical limit, not a name format rule.
MAX_STORAGE_BYTES = 8 * 1024 * 1024
MAX_TIMESTAMP = 253402300799
_ATTACKS = frozenset({"melee_hit", "ability_hit", "ability_partial", "melee_miss", "ability_miss"})
_NAME = re.compile(r"[A-Za-z]{1,64}\Z", re.ASCII)


def plausible_attacker(name: object, player_name: str = "") -> bool:
    return (isinstance(name, str) and bool(_NAME.fullmatch(name))
            and name.casefold() not in {"a", "an", "the", "you", "yourself", player_name.strip().casefold()})


def _valid_saved_name(name: object) -> bool:
    """Manual/storage names are unrestricted text; detection has separate rules."""
    return isinstance(name, str) and bool(name.strip()) and len(name) <= MAX_NAME_LENGTH


def _contains_name(text: str, name: str) -> bool:
    """Match literal, folded names without treating substrings as people."""
    start = text.find(name)
    while start >= 0:
        end = start + len(name)
        before = text[start - 1] if start else ""
        after = text[end] if end < len(text) else ""
        if not (before.isalnum() or before == "_" or after.isalnum() or after == "_"):
            return True
        start = text.find(name, start + 1)
    return False


@dataclass(frozen=True)
class AttackerCandidate:
    name: str
    appeared_at: float
    expires_at: float


@dataclass(frozen=True)
class RevengeEntry:
    name: str
    added_at: float | None
    last_activity: float | None
    kind: str = "Player"


def _entry_key(name: str, kind: str = "Player") -> str | tuple[str, str]:
    return name.casefold() if kind == "Player" else ("Guild", name.casefold())


def _valid_time(value: object) -> bool:
    if value is None:
        return True
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= MAX_TIMESTAMP
    except OverflowError:
        return False


def _activity_time(entry: RevengeEntry) -> float | None:
    known = [value for value in (entry.added_at, entry.last_activity) if value is not None]
    return max(known) if known else None


def _activity_caption(entry: RevengeEntry) -> tuple[str, str]:
    value = _activity_time(entry)
    if value is None:
        return "Date unknown", "This older saved name has no recorded activity date."
    try:
        date = datetime.fromtimestamp(value)
    except (ValueError, OverflowError, OSError):
        return "Date unavailable", "This saved activity date cannot be displayed on this system."
    return date.strftime("%b %d, %Y"), "Last activity: " + date.strftime("%Y-%m-%d %H:%M:%S") + " (local time)"


class RevengeController(QObject):
    """Store names only after an explicit + action; a prompt expires after 30 s.

    Candidate detection is a name heuristic, not an authoritative PvP classifier.
    The feature requires an explicit PvP opt-in. Automatic recruitment requires
    a known identity; saved-name sightings do not. Source text and damage values
    are never kept.
    """

    prompts_changed = Signal()
    saved_changed = Signal()
    error = Signal(str)
    pop_out_requested = Signal()
    alert_requested = Signal(str)  #: saved display name only, never combat source text

    def __init__(self, cfg: Any, path: str | Path | None = None, parent: QObject | None = None,
                 *, clock: Callable[[], float] = time.time,
                 monotonic_clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self._cfg = cfg
        self.path = Path(path) if path is not None else project_path("revenge.json")
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._alert_last_seen: dict[str, float] = {}
        self._alerts_available = self.alerts_available
        self._alert_delivery_cutoff: float | None = None
        self._seen: set[str] = set()
        self._pending: dict[str, AttackerCandidate] = {}
        self._saved: dict[str | tuple[str, str], RevengeEntry] = {}
        self._dirty = False
        self._last_error = ""
        self._presentation_available = self.available
        self._delivery_cutoff: float | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.expire)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(2000)
        self._save_timer.timeout.connect(self.flush_pending_changes)
        self._activity_refresh = QTimer(self)
        self._activity_refresh.setSingleShot(True)
        self._activity_refresh.setInterval(200)
        self._activity_refresh.timeout.connect(self.saved_changed.emit)
        self._display_timer = QTimer(self)
        self._display_timer.setInterval(60_000)
        self._display_timer.timeout.connect(self.saved_changed.emit)
        self._load()
        if self.available:
            self._display_timer.start()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "revenge_enabled", False))

    @property
    def available(self) -> bool:
        return self.enabled

    @property
    def alerts_available(self) -> bool:
        return self.enabled and bool(getattr(self._cfg, "revenge_alert_enabled", True))

    @property
    def pending_candidates(self) -> tuple[AttackerCandidate, ...]:
        return tuple(self._pending.values()) if self.available else ()

    @property
    def saved_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.saved_entries)

    @property
    def saved_entries(self) -> tuple[RevengeEntry, ...]:
        return self.entries_for()

    def entries_for(self, kind: str | None = None) -> tuple[RevengeEntry, ...]:
        if not self.available:
            return ()
        days = getattr(self._cfg, "revenge_days", 30)
        count = getattr(self._cfg, "revenge_entries", 100)
        days = min(3650, max(0, days)) if type(days) is int else 30
        count = min(MAX_SAVED_NAMES, max(0, count)) if type(count) is int else 100
        cutoff = self._clock() - days * 86400
        entries = [entry for entry in self._saved.values()
                   if (kind is None or entry.kind == kind)
                   and (not days or _activity_time(entry) is None or _activity_time(entry) >= cutoff)]
        entries.sort(key=lambda entry: (_activity_time(entry) is None,
                                       -(_activity_time(entry) or 0), entry.name.casefold(), entry.kind))
        return tuple(entries[:count] if count else entries)

    @property
    def saved_total(self) -> int:
        return len(self._saved) if self.available else 0

    @property
    def last_error(self) -> str:
        return self._last_error

    def set_config(self, cfg: Any) -> None:
        self._cfg = cfg
        if self.available and not self._presentation_available:
            # Engine messages can already be queued on the GUI thread. Older
            # disabled attacks must not appear when the feature is enabled.
            self._delivery_cutoff = self._clock()
        self._presentation_available = self.available
        if self.alerts_available and not self._alerts_available:
            self._alert_delivery_cutoff = self._clock()
        self._alerts_available = self.alerts_available
        if not self.alerts_available:
            self._alert_last_seen.clear()
        if self.available and not self._display_timer.isActive():
            self._display_timer.start()
        elif not self.available:
            self._display_timer.stop()
        if not self.available:
            # A disabled prompt must not appear when the feature is re-enabled.
            self._pending.clear()
            self._timer.stop()
            self._activity_refresh.stop()
        self.prompts_changed.emit()
        self.saved_changed.emit()

    @Slot(object, object)
    def on_message(self, message: Any, event: Any) -> None:
        """QObject receiver for worker emissions; AutoConnection runs it on the GUI thread."""
        if bool(getattr(message, "backlog", False)):
            return
        if self._delivery_cutoff is not None:
            try:
                timestamp = float(getattr(event, "ts", float("nan")))
            except (TypeError, ValueError, OverflowError):
                timestamp = float("nan")
            if not math.isfinite(timestamp) or timestamp <= self._delivery_cutoff:
                attacker = self._eligible_attacker(event)
                if attacker is not None:
                    self._seen.add(attacker.casefold())
                return
        self.observe(event)
        if self._alert_delivery_cutoff is not None:
            try:
                timestamp = float(getattr(event, "ts", float("nan")))
            except (TypeError, ValueError, OverflowError):
                timestamp = float("nan")
            if not math.isfinite(timestamp) or timestamp <= self._alert_delivery_cutoff:
                return
        self.observe_saved(event, text=getattr(message, "text", ""))

    def observe_saved(self, event_or_text: Any, now: float | None = None, *, text: str = "") -> tuple[str, ...]:
        """Alert once on appearance, then only after this name has gone quiet.

        Matching uses every saved name, independently of display limits and
        automatic recruitment. Repeated combat lines refresh the quiet interval
        without restarting the visible notification or sounding again.
        """
        if not self.enabled or not self._saved:
            return ()
        event = event_or_text
        source = event if isinstance(event, str) else text or getattr(event, "text", "")
        source = source.casefold() if isinstance(source, str) else ""
        participants = {name.casefold() for field in ("actor", "raw_actor", "target")
                        if isinstance(name := getattr(event, field, None), str)}
        keys = [key for key, entry in self._saved.items() if entry.kind == "Player"
                and (key in participants or _contains_name(source, entry.name.casefold()))]
        if not keys:
            return ()
        activity_now = self._clock()
        for key in keys:
            self._record_activity(key, activity_now)
        if not self.alerts_available:
            return ()
        now = self._monotonic_clock() if now is None else now
        if type(now) not in (int, float) or not math.isfinite(now):
            return ()
        cooldown = getattr(self._cfg, "revenge_alert_cooldown", 30.0)
        if type(cooldown) not in (int, float) or not math.isfinite(cooldown) or not 0 <= cooldown <= 3600:
            cooldown = 30.0
        alerts = []
        for key in keys:
            previous = self._alert_last_seen.get(key)
            self._alert_last_seen[key] = now
            if previous is None or now - previous >= cooldown:
                name = self._saved[key].name
                alerts.append(name)
                self.alert_requested.emit(name)
        return tuple(alerts)

    def _record_activity(self, key: str, now: float | None) -> None:
        entry = self._saved[key]
        if _valid_time(now) and now is not None and (entry.last_activity is None or now > entry.last_activity):
            self._saved[key] = RevengeEntry(entry.name, entry.added_at, float(now), entry.kind)
            self._dirty = True
            # Coalesce sustained combat into bounded two-second checkpoints.
            if not self._save_timer.isActive():
                self._save_timer.start()
            if not self._activity_refresh.isActive():
                self._activity_refresh.start()

    def _eligible_attacker(self, event: Any) -> str | None:
        player = str(getattr(self._cfg, "player_name", "") or "").strip()
        if not self.enabled or not player or getattr(event, "kind", "") not in _ATTACKS:
            return None
        target = getattr(event, "target", None)
        if not isinstance(target, str) or not (target.casefold() == player.casefold() or is_you(target)):
            return None
        attacker = getattr(event, "actor", None) or getattr(event, "raw_actor", None)
        if not plausible_attacker(attacker, player) or is_you(getattr(event, "raw_actor", "") or ""):
            return None
        return attacker

    def observe(self, event_or_text: Any, now: float | None = None) -> bool:
        """Observe one attack. Returns True only when a visible candidate is added."""
        if not self.enabled:
            return False
        player = str(getattr(self._cfg, "player_name", "") or "").strip()
        if not player:
            return False
        now = self._clock() if now is None else now
        event = event_or_text
        if isinstance(event, str):
            from mnmparse.parser import parse_line
            event = parse_line(event, now, player)
        attacker = self._eligible_attacker(event)
        if attacker is None:
            return False
        key = attacker.casefold()
        if key in self._saved:
            self._seen.add(key)
            self._record_activity(key, now)
            return False
        if key in self._seen:
            return False
        self._seen.add(key)
        if not self.available:
            return False
        self.expire(now)
        self._pending[key] = AttackerCandidate(attacker, now, now + PROMPT_SECONDS)
        self._timer.start()
        self.prompts_changed.emit()
        return True

    def expire(self, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        expired = [key for key, entry in self._pending.items() if now >= entry.expires_at]
        for key in expired:
            del self._pending[key]
        if not self._pending:
            self._timer.stop()
        if expired or self._pending:
            self.prompts_changed.emit()

    def save_attacker(self, name: str, *, from_prompt: bool = False, kind: str = "Player") -> bool:
        if not self.available:
            return False
        if kind not in KINDS or from_prompt and kind != "Player":
            self._report_error("Choose a Player or Guild list.")
            return False
        if not _valid_saved_name(name):
            self._report_error("Enter a name to add to the revenge list.")
            return False
        name = name.strip()
        key = _entry_key(name, kind)
        candidate = self._pending.get(key) if kind == "Player" else None
        if from_prompt and (candidate is None or self._clock() >= candidate.expires_at):
            self.expire()
            return False
        if len(self._saved) >= MAX_SAVED_NAMES and key not in self._saved:
            self._report_error("Revenge list is full. Remove a saved name before adding another.")
            return False
        # Explicit manual Add accepts arbitrary text; + requires a live candidate.
        now = self._clock()
        if not _valid_time(now) or now is None:
            self._report_error("The current clock could not be recorded. Check the system clock and try again.")
            return False
        previous = self._saved.get(key)
        entry = RevengeEntry(previous.name if previous else candidate.name if candidate else name,
                             previous.added_at if previous else float(now),
                             max(previous.last_activity or 0, float(now)) if previous else float(now), kind)
        updated = {**self._saved, key: entry}
        if not self._persist(updated):
            return False
        self._saved = updated
        self._set_clean()
        if kind == "Player":
            self._seen.add(key)
            self._pending.pop(key, None)
        self.expire()
        self.saved_changed.emit()
        self.prompts_changed.emit()
        return True

    def remove_saved(self, name: str, *, kind: str = "Player") -> bool:
        key = _entry_key(name, kind)
        if not self.available or kind not in KINDS or key not in self._saved:
            return False
        updated = {existing_key: value for existing_key, value in self._saved.items() if existing_key != key}
        if not self._persist(updated):
            return False
        self._saved = updated
        if kind == "Player":
            self._alert_last_seen.pop(name.casefold(), None)
        self._set_clean()
        self.saved_changed.emit()
        return True

    def export_csv(self, kind: str = "Player") -> str:
        """Export the chosen complete list, including entries hidden by limits."""
        if not self.available:
            return ""
        if kind not in KINDS:
            self._report_error("Choose a Player or Guild list.")
            return ""
        entries = sorted((entry for entry in self._saved.values() if entry.kind == kind),
                         key=lambda entry: (_activity_time(entry) is None, -(_activity_time(entry) or 0), entry.name.casefold()))
        try:
            return format_csv(CsvEntry(entry.name, entry.kind) for entry in entries)
        except RevengeCsvError as exc:
            self._report_error(str(exc))
            return ""

    def import_csv(self, text: str, kind: str = "Player") -> bool:
        try:
            entries = parse_csv(text, default_kind=kind)
        except RevengeCsvError as exc:
            self._report_error(str(exc))
            return False
        return self.import_entries(entries, kind=kind)

    def import_entries(self, entries: Any, *, kind: str = "Player") -> bool:
        """Validate everything, then merge only the explicitly chosen list once."""
        if not self.available:
            return False
        try:
            if kind not in KINDS:
                raise RevengeCsvError("Choose a Player or Guild list.")
            checked = validate_entries(entries)
            selected = [entry for entry in checked if entry.kind == kind]
            if not selected:
                raise RevengeCsvError(f"No {kind.lower()} entries to add.")
        except (RevengeCsvError, TypeError) as exc:
            self._report_error(str(exc))
            return False
        now = self._clock()
        if not _valid_time(now) or now is None:
            self._report_error("The current clock could not be recorded. Check the system clock and try again.")
            return False
        updated = dict(self._saved)
        selected_keys = set()
        for entry in selected:
            name = entry.name.strip()
            key = _entry_key(name, kind)
            selected_keys.add(key)
            if key not in updated:
                updated[key] = RevengeEntry(name, float(now), float(now), kind)
        if len(updated) > MAX_SAVED_NAMES:
            self._report_error("Revenge list is full. Remove saved entries before importing more.")
            return False
        if not self._persist(updated):
            return False
        self._saved = updated
        self._set_clean()
        if kind == "Player":
            self._seen.update(selected_keys)
            for key in selected_keys:
                self._pending.pop(key, None)
            self.expire()
            self.prompts_changed.emit()
        self.saved_changed.emit()
        return True

    def new_session(self) -> None:
        self._seen.clear()
        self._pending.clear()
        self._alert_last_seen.clear()
        if self.available:
            self._delivery_cutoff = self._clock()
            self._alert_delivery_cutoff = self._delivery_cutoff
        self._timer.stop()
        self.prompts_changed.emit()

    def _report_error(self, message: str) -> None:
        self._last_error = message
        self.error.emit(message)

    def _load(self) -> None:
        # A restored archive must not be overwritten by an older queued activity
        # checkpoint. Loading valid data replaces the entire in-memory snapshot.
        self._set_clean()
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > MAX_STORAGE_BYTES:
                raise ValueError("Oversized revenge list")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("schema")) is not int:
                raise ValueError("Invalid revenge list")
            if data["schema"] == 1:
                names = data.get("names")
                if (not isinstance(names, list) or len(names) > MAX_SAVED_NAMES
                        or any(not _valid_saved_name(name) for name in names)):
                    raise ValueError("Invalid legacy revenge list")
                # Keep unknown dates visible under day filters; a later real
                # attack fills the date without inventing historical activity.
                saved = {name.casefold(): RevengeEntry(name, None, None) for name in names}
            elif data["schema"] == 2:
                rows = data.get("entries")
                if not isinstance(rows, list) or len(rows) > MAX_SAVED_NAMES:
                    raise ValueError("Invalid revenge entries")
                saved = {}
                for row in rows:
                    kind = row.get("kind", "Player") if isinstance(row, dict) else None
                    if (not isinstance(row, dict) or set(row) not in ({"name", "added_at", "last_activity"},
                                                                     {"name", "added_at", "last_activity", "kind"})
                            or kind not in KINDS
                            or not _valid_saved_name(row.get("name"))
                            or not _valid_time(row["added_at"]) or not _valid_time(row["last_activity"])
                            or _entry_key(row["name"], kind) in saved):
                        raise ValueError("Invalid revenge entry")
                    saved[_entry_key(row["name"], kind)] = RevengeEntry(row["name"], row["added_at"], row["last_activity"], kind)
            else:
                raise ValueError("Unsupported revenge schema")
            self._saved = saved
            self._alert_last_seen.clear()
            self._last_error = ""
        except (OSError, ValueError, RecursionError):
            self._report_error("The revenge list could not be read. The existing file has been preserved.")

    def _set_clean(self) -> None:
        self._dirty = False
        self._save_timer.stop()
        self._activity_refresh.stop()

    def flush_pending_changes(self) -> bool:
        self._save_timer.stop()
        if not self._dirty:
            return True
        if not self._persist(self._saved):
            return False
        self._set_clean()
        self.saved_changed.emit()
        return True

    def _persist(self, saved: dict[str | tuple[str, str], RevengeEntry]) -> bool:
        try:
            payload = {"schema": 2, "entries": [
                {"name": entry.name, "added_at": entry.added_at, "last_activity": entry.last_activity,
                 **({"kind": entry.kind} if entry.kind != "Player" else {})}
                for entry in sorted(saved.values(), key=lambda entry: (entry.name.casefold(), entry.kind))]}
            # Match atomic_json's exact UTF-8 representation. A successful save
            # must also be readable by the loader and backup's resource bounds.
            if len((json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")) > MAX_STORAGE_BYTES:
                self._report_error("Revenge list exceeds the 8 MB storage limit. Your previous list is intact; remove some entries before adding more.")
                return False
            atomic_json(self.path, payload)
        except (OSError, ValueError):
            self._report_error("Revenge list could not be saved. Your previous list is intact; try adding the name again.")
            return False
        if self._last_error:
            self._last_error = ""
            self.error.emit("")
        return True


def _hint(controller: RevengeController) -> str:
    if not controller.enabled:
        return "Revenge is a PvP-only feature, off by default. Enable it in Options."
    return ""


def _scrub_row(row: QWidget) -> None:
    """Remove text immediately, before Qt's deferred deletion/accessibility pass."""
    for label in row.findChildren(QLabel):
        label.setText("")
        label.setToolTip("")
    for button in row.findChildren(QPushButton):
        button.setAccessibleName("")
        button.setToolTip("")


class RevengePrompts(QFrame):
    """Stable keyboard-accessible + buttons for the current 30-second candidates."""

    def __init__(self, controller: RevengeController, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.setStyleSheet(button_qss())
        self._rows: dict[str, tuple[QWidget, QLabel]] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.heading = QLabel("Revenge · recent PvP attackers")
        layout.addWidget(self.heading)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_scroll.setMaximumHeight(180)
        rows = QWidget()
        self._row_layout = QVBoxLayout(rows)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._row_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.rows_scroll.setWidget(rows)
        layout.addWidget(self.rows_scroll)
        self.error = QLabel(controller.last_error)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        controller.prompts_changed.connect(self.refresh)
        controller.saved_changed.connect(self.refresh)
        controller.error.connect(self.error.setText)
        self.refresh()

    def refresh(self):
        self.setVisible(self.controller.enabled)
        candidates = {entry.name.casefold(): entry for entry in self.controller.pending_candidates}
        for key in set(self._rows) - set(candidates):
            row, _remaining = self._rows.pop(key)
            _scrub_row(row)
            self._row_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for key, candidate in candidates.items():
            if key not in self._rows:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                name = ElidedLabel(candidate.name)
                name.setToolTip(candidate.name)
                line.addWidget(name, 1)
                remaining = QLabel()
                line.addWidget(remaining)
                add = QPushButton("+")
                add.setObjectName("Chip")
                add.setFixedWidth(36)
                add.setAccessibleName(f"Save {candidate.name} to revenge list")
                add.setToolTip("Save this name to your revenge list")
                add.clicked.connect(lambda _checked=False, name=candidate.name: self.controller.save_attacker(name, from_prompt=True))
                line.addWidget(add)
                self._row_layout.addWidget(row)
                self._rows[key] = (row, remaining)
            self._rows[key][1].setText(f"{max(0, math.ceil(candidate.expires_at - self.controller._clock()))} s")
        self.heading.setVisible(self.controller.available)
        self.rows_scroll.setVisible(bool(candidates))
        self.status.setText(_hint(self.controller) or ("Use + to remember an attacker. Prompts expire after 30 seconds; ignore named NPCs."
                                                    if candidates else "No recent PvP attackers. Valid-looking names may be NPCs; save only players you recognize."))
        self.error.setText(self.controller.last_error)


class _RevengeAddDialog(QDialog):
    """Focusable manual add with a reviewed, per-list clipboard batch."""

    def __init__(self, controller: RevengeController, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.controller = controller
        self.kind = kind
        self._clipboard_entries: tuple[CsvEntry, ...] = ()
        self._clipboard_text = ""
        self.setWindowTitle(window_title(f"revenge-add-{kind.lower()}"))
        self.setStyleSheet(theme.main_qss() + button_qss())
        self.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        heading = QLabel(f"Add {kind}(s)")
        heading.setProperty("class", "h2")
        layout.addWidget(heading)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText(f"{kind} name")
        self.name_input.setAccessibleName(f"{kind} name to add")
        layout.addWidget(self.name_input)
        self.from_clipboard = QPushButton("CSV from clipboard")
        self.from_clipboard.setToolTip("Import CSV from Clipboard")
        self.from_clipboard.clicked.connect(self._read_clipboard)
        layout.addWidget(self.from_clipboard)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName(f"{kind} clipboard import preview")
        self.preview.setMaximumHeight(200)
        self.preview.hide()
        layout.addWidget(self.preview)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        self.add_button = QPushButton("Add")
        self.add_button.setProperty("class", "primary")
        self.add_button.clicked.connect(self.accept)
        row.addWidget(self.add_button)
        layout.addLayout(row)
        self.name_input.textChanged.connect(self._sync_add)
        self.name_input.returnPressed.connect(self.accept)
        self._sync_add()

    def textValue(self) -> str:  # noqa: N802 - preserve the input dialog's small interface
        return self.name_input.text() or self._clipboard_text

    def setTextValue(self, text: str) -> None:  # noqa: N802
        self._clipboard_entries = ()
        self._clipboard_text = ""
        self.preview.clear()
        self.preview.hide()
        self.summary.clear()
        self.error.clear()
        self.name_input.setText(text)
        self._sync_add()

    def _sync_add(self) -> None:
        self.add_button.setEnabled(self.controller.available
                                   and (bool(self._clipboard_entries) or _valid_saved_name(self.name_input.text())))

    def _read_clipboard(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        self._clipboard_entries = ()
        self._clipboard_text = ""
        self.preview.clear()
        self.preview.hide()
        self.summary.clear()
        self.error.clear()
        try:
            try:
                entries = parse_csv(text, default_kind=self.kind)
            except RevengeCsvError:
                # Ordinary one-name-per-line clipboard text is also useful.
                # CSV-shaped text stays strict so a malformed batch cannot
                # quietly become a collection of literal CSV rows.
                first_line = text.lstrip("\ufeff").strip().split("\n", 1)[0].strip().casefold()
                if "," in text or '"' in text or first_line in {"type", "name"}:
                    raise
                entries = parse_names(text, kind=self.kind)
            selected = tuple(entry for entry in entries if entry.kind == self.kind)
            excluded = len(entries) - len(selected)
            if not selected:
                raise RevengeCsvError(f"No {self.kind.lower()} entries in the clipboard.")
            self._clipboard_entries = selected
            self._clipboard_text = text
            self.summary.setText(f"{len(selected)} {self.kind.lower()} entries ready"
                                 + (f" · {excluded} other entries ignored" if excluded else ""))
            self.preview.setPlainText("\n".join(entry.name[:400] + ("…" if len(entry.name) > 400 else "")
                                                for entry in selected[:100]))
            self.preview.show()
        except RevengeCsvError as exc:
            self.error.setText(str(exc))
        self._sync_add()

    def accept(self) -> None:
        entries = self._clipboard_entries
        if self.name_input.text().strip():
            entries += (CsvEntry(self.name_input.text().strip(), self.kind),)
        if not entries:
            return
        if self.controller.import_entries(entries, kind=self.kind):
            super().accept()
        else:
            self.error.setText(self.controller.last_error)


class RevengeList(QFrame):
    """A persistent name list suitable for the overlay's Revenge tab."""

    def __init__(self, controller: RevengeController, parent: QWidget | None = None, *, compact: bool = False,
                 allow_popout: bool = True, minimal: bool = False):
        super().__init__(parent)
        self.controller = controller
        self.compact = compact
        self.allow_popout = allow_popout
        self.minimal = minimal
        self._manual_dialog: _RevengeAddDialog | None = None
        self.setStyleSheet(button_qss())
        self._rows: dict[str | tuple[str, str], QWidget] = {}
        self._dates: dict[str | tuple[str, str], QLabel] = {}
        self._labels: dict[str | tuple[str, str], ElidedLabel] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(12)
        self.player_column = QWidget(self)
        self.guild_column = QWidget(self)
        player_layout = QVBoxLayout(self.player_column)
        guild_layout = QVBoxLayout(self.guild_column)
        for column, title in ((player_layout, "Players"), (guild_layout, "Guilds")):
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(6)
            heading = QLabel(title)
            heading.setProperty("class", "muted")
            heading.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            column.addWidget(heading)
        columns.addWidget(self.player_column, 1)
        columns.addWidget(self.guild_column, 1)
        layout.addLayout(columns, 1)
        self.status = QLabel(self.player_column)
        self.status.setWordWrap(True)
        self.guild_status = QLabel(self.guild_column)
        self.guild_status.setWordWrap(True)
        if minimal:
            self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.guild_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
            self.guild_status.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        player_layout.addWidget(self.status)
        guild_layout.addWidget(self.guild_status)
        manual = QHBoxLayout()
        manual.setSpacing(6)
        self.name_input = QLineEdit(self.player_column)
        self.name_input.setPlaceholderText("Name")
        self.name_input.setAccessibleName("Player name to add to revenge list")
        self.name_input.setMinimumWidth(140)
        if not minimal:
            player_layout.addWidget(self.name_input)
        self.add_button = QPushButton("Add Player(s)", self.player_column)
        self.add_button.setObjectName("Chip")
        self.add_button.setAccessibleName("Add player name to revenge list")
        self.add_button.clicked.connect(lambda _checked=False: self._open_manual())
        self.name_input.returnPressed.connect(self._add_manual)
        self.name_input.textChanged.connect(self._update_add_enabled)
        if not minimal:
            manual.addWidget(self.add_button)
        self.pop_out_button = QPushButton("Pop out", self)
        self.pop_out_button.setObjectName("Chip")
        self.pop_out_button.setAccessibleName("Pop out revenge list")
        self.pop_out_button.clicked.connect(self._pop_out)
        self.export_button = QPushButton("Export CSV", self.player_column)
        self.export_button.setObjectName("Chip")
        self.export_button.setAccessibleName("Copy revenge list as CSV")
        self.export_button.clicked.connect(lambda _checked=False: self._export_csv())
        self.guild_add_button = QPushButton("Add Guild(s)", self.guild_column)
        self.guild_add_button.setObjectName("Chip")
        self.guild_add_button.setAccessibleName("Add guild reminders")
        self.guild_add_button.clicked.connect(self._open_guild_manual)
        self.guild_export_button = QPushButton("Export CSV", self.guild_column)
        self.guild_export_button.setObjectName("Chip")
        self.guild_export_button.setAccessibleName("Copy guild reminders as CSV")
        self.guild_export_button.clicked.connect(self._export_guild_csv)
        if not minimal:
            manual.addWidget(self.export_button)
            player_layout.addLayout(manual)
            guild_manual = QHBoxLayout()
            guild_manual.setSpacing(6)
            guild_manual.addWidget(self.guild_add_button)
            guild_manual.addWidget(self.guild_export_button)
            guild_layout.addLayout(guild_manual)
            layout.addWidget(self.pop_out_button, 0, Qt.AlignmentFlag.AlignRight)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if not minimal:
            self.rows_scroll.setMaximumHeight(160 if compact else 200)
        rows = QWidget()
        self._row_layout = QVBoxLayout(rows)
        self._row_layout.setContentsMargins(0, 0, 0, 0)
        self._row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._row_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.rows_scroll.setWidget(rows)
        player_layout.addWidget(self.rows_scroll, 1)
        self.guild_rows_scroll = QScrollArea(self.guild_column)
        self.guild_rows_scroll.setWidgetResizable(True)
        self.guild_rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.guild_rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        if not minimal:
            self.guild_rows_scroll.setMaximumHeight(160 if compact else 200)
        guild_rows = QWidget()
        self._guild_row_layout = QVBoxLayout(guild_rows)
        self._guild_row_layout.setContentsMargins(0, 0, 0, 0)
        self._guild_row_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._guild_row_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        self.guild_rows_scroll.setWidget(guild_rows)
        guild_layout.addWidget(self.guild_rows_scroll, 1)
        self.error = QLabel(controller.last_error)
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        controller.saved_changed.connect(self.refresh)
        controller.error.connect(self._set_error)
        self.refresh()

    def _set_error(self, message: str) -> None:
        self.error.setText(message)
        if self.minimal:
            self.error.setVisible(bool(message))

    def _update_add_enabled(self):
        self.add_button.setEnabled(self.controller.available)
        self.guild_add_button.setEnabled(self.controller.available)

    def _add_manual(self):
        if self.controller.save_attacker(self.name_input.text().strip()):
            self.name_input.clear()

    def _pop_out(self):
        if self.allow_popout and self.controller.available:
            self.controller.pop_out_requested.emit()

    def _open_manual(self, _checked: bool = False, *, kind: str = "Player"):
        if not self.controller.available:
            return
        if self._manual_dialog is not None:
            self._manual_dialog.raise_()
            self._manual_dialog.activateWindow()
            return
        # The overlay itself refuses focus. An explicitly opened normal dialog
        # can accept keyboard input without changing the overlay's window flags.
        dialog = _RevengeAddDialog(self.controller, kind, self.window())
        if kind == "Player" and self.name_input.text():
            dialog.setTextValue(self.name_input.text())
        self._manual_dialog = dialog
        dialog.finished.connect(lambda result: self._finish_manual(dialog, result))
        dialog.open()
        dialog.activateWindow()

    def _open_guild_manual(self, _checked: bool = False):
        self._open_manual(kind="Guild")

    def _remove_saved(self, name: str, *, kind: str = "Player") -> bool:
        if not self.controller.available:
            return False
        if self.minimal:
            dialog = QMessageBox(self.window())
            dialog.setWindowTitle(window_title("revenge-remove"))
            dialog.setStyleSheet(theme.main_qss() + button_qss())
            dialog.setTextFormat(Qt.TextFormat.PlainText)
            dialog.setText(f"Remove {name} from {'guilds' if kind == 'Guild' else 'players'}?")
            dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Yes).setText("Remove")
            dialog.setDefaultButton(QMessageBox.StandardButton.Cancel)
            dialog.setEscapeButton(QMessageBox.StandardButton.Cancel)
            accepted = dialog.exec() == QMessageBox.StandardButton.Yes
            dialog.deleteLater()
            if not accepted:
                return False
        return self.controller.remove_saved(name, kind=kind)

    def _export_csv(self, _checked: bool = False, *, kind: str = "Player"):
        text = self.controller.export_csv(kind)
        clipboard = QApplication.clipboard()
        if text and clipboard is not None:
            clipboard.setText(text)
            status = self.guild_status if kind == "Guild" else self.status
            status.setText(f"{kind} CSV copied.")
            status.show()
            QTimer.singleShot(2500, self, self.refresh)

    def _export_guild_csv(self, _checked: bool = False):
        self._export_csv(kind="Guild")

    def _finish_manual(self, dialog: _RevengeAddDialog, result: int):
        if result == QDialog.DialogCode.Accepted:
            self.name_input.clear()
        dialog.setTextValue("")
        if self._manual_dialog is dialog:
            self._manual_dialog = None
        dialog.deleteLater()

    def refresh(self):
        self.setVisible(self.controller.enabled)
        player_entries = self.controller.entries_for("Player")
        guild_entries = self.controller.entries_for("Guild")
        entries = player_entries + guild_entries
        keyed_entries = {("Guild", entry.name) if getattr(entry, "kind", "Player") == "Guild" else entry.name: entry
                         for entry in entries}
        self.name_input.setVisible(self.controller.available and not self.compact)
        self.add_button.setVisible(self.controller.available)
        self.export_button.setVisible(self.controller.available)
        self.export_button.setEnabled(self.controller.available)
        self.guild_add_button.setVisible(self.controller.available)
        self.guild_export_button.setVisible(self.controller.available)
        self.guild_export_button.setEnabled(self.controller.available)
        self.pop_out_button.setVisible(self.controller.available and self.allow_popout)
        self.pop_out_button.setEnabled(self.controller.available and self.allow_popout)
        self.name_input.setEnabled(self.controller.available)
        if not self.controller.available:
            self.name_input.clear()
            if self._manual_dialog is not None:
                self._manual_dialog.setTextValue("")
                self._manual_dialog.reject()
        self._update_add_enabled()
        for key in set(self._rows) - set(keyed_entries):
            row = self._rows.pop(key)
            self._dates.pop(key, None)
            self._labels.pop(key, None)
            _scrub_row(row)
            (self._guild_row_layout if isinstance(key, tuple) else self._row_layout).removeWidget(row)
            row.hide()
            row.deleteLater()
        for key, entry in keyed_entries.items():
            name = entry.name
            kind = getattr(entry, "kind", "Player")
            if key not in self._rows:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                label = ElidedLabel()
                line.addWidget(label, 1)
                date = QLabel()
                date.setTextFormat(Qt.TextFormat.PlainText)
                line.addWidget(date)
                remove = QPushButton("×" if self.compact else "Remove")
                remove.setObjectName("Chip")
                remove.setAccessibleName(f"Remove {name} guild reminder" if kind == "Guild" else f"Remove {name} from revenge list")
                remove.setToolTip("Remove guild reminder" if kind == "Guild" else "Remove player")
                if self.compact:
                    remove.setFixedWidth(24)
                    remove.setStyleSheet("QPushButton { padding: 3px 4px; }")
                remove.clicked.connect(lambda _checked=False, n=name, k=kind: self._remove_saved(n, kind=k))
                line.addWidget(remove)
                self._rows[key], self._dates[key], self._labels[key] = row, date, label
            self._labels[key].setText(name)
            # Manual names may contain markup; keep their tooltip text literal.
            text, tooltip = _activity_caption(entry)
            self._labels[key].setToolTip("<span>" + escape(name)
                                        + ("<br>Guild reminder" if kind == "Guild" else "")
                                        + ("<br>" + escape(tooltip) if self.compact else "") + "</span>")
            self._dates[key].setText(text)
            self._dates[key].setToolTip(tooltip)
            self._dates[key].setVisible(not self.compact)
            # Reposition existing rows as activity changes, not only new rows.
            column_layout = self._guild_row_layout if kind == "Guild" else self._row_layout
            column_layout.removeWidget(self._rows[key])
            column_layout.addWidget(self._rows[key])
        self.status.setText("No players to show.")
        self.status.setVisible(not player_entries)
        self.guild_status.setText("No guilds to show.")
        self.guild_status.setVisible(not guild_entries)
        self.rows_scroll.setVisible(bool(player_entries))
        self.guild_rows_scroll.setVisible(bool(guild_entries))
        self._set_error(self.controller.last_error)


class _PopoutHeader(QFrame):
    def __init__(self, owner: "RevengePopoutWindow"):
        super().__init__(owner)
        self._owner = owner
        self._drag: QPoint | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)
        self.heading = QLabel("Revenge")
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setProperty("class", "title")
        layout.addWidget(self.heading, 1)
        layout.addWidget(QLabel("Players"))
        self.add_button = owner.list.add_button
        self.add_button.setText("+")
        self.add_button.setObjectName("RevengeAdd")
        self.add_button.setToolTip("Add Player(s)")
        self.add_button.setAccessibleName("Add players manually or from clipboard")
        self.add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_button.clicked.connect(owner._show_header)
        layout.addWidget(self.add_button)
        self.export_button = owner.list.export_button
        self.export_button.setObjectName("RevengeExport")
        self.export_button.setText("📋")
        self.export_button.setToolTip("Copy CSV to Clipboard")
        self.export_button.setAccessibleName("Copy players CSV to clipboard")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.export_button)
        layout.addWidget(QLabel("Guilds"))
        self.guild_add_button = owner.list.guild_add_button
        self.guild_add_button.setText("+")
        self.guild_add_button.setObjectName("RevengeAdd")
        self.guild_add_button.setToolTip("Add Guild(s)")
        self.guild_add_button.setAccessibleName("Add guilds manually or from clipboard")
        self.guild_add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.guild_add_button.clicked.connect(owner._show_header)
        layout.addWidget(self.guild_add_button)
        self.guild_export_button = owner.list.guild_export_button
        self.guild_export_button.setObjectName("RevengeExport")
        self.guild_export_button.setText("📋")
        self.guild_export_button.setToolTip("Copy CSV to Clipboard")
        self.guild_export_button.setAccessibleName("Copy guilds CSV to clipboard")
        self.guild_export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.guild_export_button)
        self.close_button = QPushButton("×")
        self.close_button.setObjectName("Chip")
        self.close_button.setAccessibleName("Close revenge list popout")
        self.close_button.setFixedWidth(32)
        self.close_button.clicked.connect(owner.hide)
        layout.addWidget(self.close_button)

    def update_cursor(self) -> None:
        if self._owner.locked:
            self._drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor if self._owner.locked else Qt.CursorShape.SizeAllCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._owner.locked and event.button() == Qt.MouseButton.LeftButton:
            self._owner._show_header()
            self._drag = event.globalPosition().toPoint() - self._owner.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self._owner.locked and self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._owner.move(event.globalPosition().toPoint() - self._drag)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        self._owner._header_hide.start()
        super().mouseReleaseEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor("#ffffff", 0.14), 1))
        painter.setBrush(theme.qcolor(theme.BG1, 0.97))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                theme.RADIUS_CHIP, theme.RADIUS_CHIP)


class RevengePopoutWindow(QWidget):
    """A passive list with the combat overlay's glass, text scale and move lock."""

    def __init__(self, controller: RevengeController, settings: Any = None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.controller = controller
        self._settings = settings
        self._state_ready = False
        self._restored = False
        self._closing = False
        self._wanted_visible = settings.value("revenge/visible", False, type=bool) if settings is not None else False
        self._locked = True
        self._opacity = 0.85
        self._font_scale = 1.0
        self.setObjectName("RevengePopout")
        self.setWindowTitle(window_title("revenge-popout"))
        self.setAccessibleName("Revenge List popout")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setAutoFillBackground(False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setPalette(theme._build_palette())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 8)
        layout.setSpacing(0)
        self.list = RevengeList(controller, self, compact=True, allow_popout=False, minimal=True)
        layout.addWidget(self.list, 1)
        # Floating controls reveal over the list without changing its viewport.
        self.header = _PopoutHeader(self)
        self._header_hide = QTimer(self)
        self._header_hide.setSingleShot(True)
        self._header_hide.setInterval(300)
        self._header_hide.timeout.connect(self._hide_header)
        self._grip = OverlaySizeGrip(self)
        self._grip.setAccessibleName("Resize revenge list popout")
        self._grip.setToolTip("Drag to resize")
        self.setMinimumSize(360, 200)
        self.resize(460, 380)
        cfg = controller._cfg
        locked = bool(getattr(cfg, "overlay_locked", True))
        opacity = float(getattr(cfg, "overlay_opacity", 0.85))
        font_scale = float(getattr(cfg, "overlay_font_scale", 1.0))
        if settings is not None:
            locked = settings.value("overlay/locked", locked, type=bool)
            opacity = settings.value("overlay/opacity", opacity, type=float)
            font_scale = settings.value("overlay/font_scale", font_scale, type=float)
        self.set_appearance(opacity, font_scale)
        self.set_locked(locked)
        self._watch_hover_targets()
        self.header.hide()
        if settings is not None:
            rect = settings.value("revenge/geometry")
            if isinstance(rect, QRect) and rect.isValid():
                self._restore_geometry(rect)
        controller.saved_changed.connect(self._sync_visibility)
        controller.prompts_changed.connect(self._sync_visibility)
        self._state_ready = True

    def _available_area(self, rect: QRect) -> QRect | None:
        screens = QApplication.screens()
        if not screens:
            return None
        screen = next((screen for screen in screens if screen.availableGeometry().contains(rect.center())),
                      QApplication.primaryScreen() or screens[0])
        return screen.availableGeometry()

    def _restore_geometry(self, rect: QRect) -> None:
        area = self._available_area(rect)
        self._fit_header(area)
        if area is None:
            self.setGeometry(rect)
            return
        width = min(area.width(), max(self.minimumWidth(), rect.width()))
        height = min(area.height(), max(self.minimumHeight(), rect.height()))
        x = min(max(rect.x(), area.left()), area.right() - width + 1)
        y = min(max(rect.y(), area.top()), area.bottom() - height + 1)
        self.setGeometry(x, y, width, height)

    def save_state(self) -> None:
        if self._settings is not None and self._state_ready and not self._closing:
            rect = self.geometry()
            if rect.isValid():
                self._settings.setValue("revenge/geometry", rect)
            self._settings.setValue("revenge/visible", self._wanted_visible)

    def restore_state(self) -> None:
        """Restore the user's pop-out choice after startup appearance is applied."""
        self._restored = True
        self._sync_visibility()

    def shutdown(self) -> None:
        """Freeze the open preference before teardown hides the window."""
        self.save_state()
        self._closing = True
        self.close()

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def opacity(self) -> float:
        return self._opacity

    @property
    def font_scale(self) -> float:
        return self._font_scale

    def set_locked(self, locked: bool) -> None:
        """Prevent moving and resizing while keeping list controls interactive."""
        self._locked = bool(locked)
        self.header.update_cursor()
        self._grip.setVisible(not self._locked)

    def set_appearance(self, opacity: float, font_scale: float) -> None:
        """Match the overlay's background alpha without fading its text."""
        self._opacity = min(1.0, max(0.2, float(opacity)))
        self._font_scale = min(2.0, max(0.6, float(font_scale)))
        px = max(9, round(theme.OVERLAY_FONT_PX * self._font_scale))
        self.setStyleSheet(theme.overlay_qss(self._opacity) + button_qss()
                          + f"QWidget {{ font-size: {px}px; }}")
        self._restore_geometry(self.geometry())
        self._position_chrome()
        self.update()

    def _fit_header(self, area: QRect | None) -> None:
        """Keep controls reachable when a smaller monitor cannot fit their scale."""
        normal_px = max(9, round(theme.OVERLAY_FONT_PX * self._font_scale))
        style = f"""
            QPushButton#RevengeAdd, QPushButton#RevengeExport {{ background: transparent; color: {theme.MUTED};
                border: none; border-radius: 6px; padding: 3px 8px; min-height: 20px; }}
            QPushButton#RevengeAdd:hover, QPushButton#RevengeExport:hover {{
                background: rgba(255,255,255,0.08); color: {theme.TEXT}; }}
        """
        for px in range(normal_px, 8, -1):
            self.header.setStyleSheet(style + (f"QWidget {{ font-size: {px}px; }}" if px < normal_px else ""))
            self.header.close_button.setFixedWidth(max(32, round(px * 2.5)))
            self.header.layout().invalidate()
            if area is None or self.header.sizeHint().width() + 10 <= area.width():
                break
        minimum = max(360, round(360 * self._font_scale), self.header.sizeHint().width() + 10)
        self.setMinimumWidth(min(minimum, area.width()) if area is not None else minimum)

    def _position_chrome(self) -> None:
        self.layout().activate()
        self.header.setGeometry(5, 5, self.width() - 10, self.header.sizeHint().height())
        self._grip.resize(16, 16)
        self._grip.move(self.width() - self._grip.width() - 2,
                        self.height() - self._grip.height() - 2)
        self._grip.raise_()

    def _watch_hover_targets(self) -> None:
        for widget in (self, *self.findChildren(QWidget)):
            if widget.window() is self and not widget.property("revengeHoverTracked"):
                widget.setProperty("revengeHoverTracked", True)
                widget.setMouseTracking(True)
                widget.installEventFilter(self)

    def _near_header(self, point: QPoint) -> bool:
        return self.rect().contains(point) and point.y() <= self.header.geometry().bottom()

    def _show_header(self) -> None:
        self._header_hide.stop()
        self.header.show()
        self.header.raise_()

    def _hide_header(self) -> None:
        if self.header._drag is not None or self.list._manual_dialog is not None:
            self._header_hide.start()
        elif self._near_header(self.mapFromGlobal(QCursor.pos())):
            return
        else:
            self.header.hide()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if isinstance(watched, QWidget) and watched.window() is self:
            kind = event.type()
            if kind in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
                position = event.globalPosition().toPoint() if hasattr(event, "globalPosition") else QCursor.pos()
                if self._near_header(self.mapFromGlobal(position)):
                    self._show_header()
                elif not self._header_hide.isActive():
                    self._header_hide.start()
            elif kind == QEvent.Type.Leave and not self._header_hide.isActive():
                self._header_hide.start()
        return super().eventFilter(watched, event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.qcolor("#ffffff", 0.14), 1))
        painter.setBrush(theme.qcolor(theme.BG0, self._opacity))
        painter.drawRoundedRect(rect, 12, 12)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, "_grip"):
            self._position_chrome()
        self.save_state()

    def moveEvent(self, event: QMoveEvent) -> None:  # noqa: N802
        super().moveEvent(event)
        self.save_state()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._closing:
            self._wanted_visible = False
            self.save_state()
        super().closeEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802
        self._header_hide.stop()
        self.header._drag = None
        self.header.hide()
        super().hideEvent(event)

    def _sync_visibility(self):
        self._watch_hover_targets()
        if not self.controller.enabled:
            super().hide()
        elif self._restored and self._wanted_visible and not self._closing:
            super().show()

    def hide(self):
        if self._state_ready and not self._closing:
            self._wanted_visible = False
            self.save_state()
        super().hide()

    def show(self):
        if self.controller.enabled:
            self._restored = True
            self._wanted_visible = True
            self.save_state()
            # No activation or raising: adding a name uses the explicit normal
            # input dialog, while the passive list leaves game focus untouched.
            super().show()


__all__ = ["RevengeController", "RevengePrompts", "RevengeList", "RevengePopoutWindow", "RevengeEntry", "AttackerCandidate", "plausible_attacker"]
