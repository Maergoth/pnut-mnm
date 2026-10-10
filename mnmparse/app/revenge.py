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

from PySide6.QtCore import QObject, QPoint, QTimer, Signal, Slot, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from mnmparse.app import theme
from mnmparse.app.crop_picker import button_qss
from mnmparse.app.widgets import ElidedLabel
from mnmparse.app.window_identity import window_title
from mnmparse.config import project_path
from mnmparse.grammar import is_you
from mnmparse.privacy import casual_enabled
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
    The feature requires an explicit PvP opt-in and confirmed full presentation.
    Unknown identity fails closed. Source text and damage values are never kept.
    """

    prompts_changed = Signal()
    saved_changed = Signal()
    error = Signal(str)
    pop_out_requested = Signal()

    def __init__(self, cfg: Any, path: str | Path | None = None, parent: QObject | None = None,
                 *, clock: Callable[[], float] = time.time):
        super().__init__(parent)
        self._cfg = cfg
        self.path = Path(path) if path is not None else project_path("revenge.json")
        self._clock = clock
        self._seen: set[str] = set()
        self._pending: dict[str, AttackerCandidate] = {}
        self._saved: dict[str, RevengeEntry] = {}
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
        return self.enabled and not casual_enabled(self._cfg)

    @property
    def pending_candidates(self) -> tuple[AttackerCandidate, ...]:
        return tuple(self._pending.values()) if self.available else ()

    @property
    def saved_names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.saved_entries)

    @property
    def saved_entries(self) -> tuple[RevengeEntry, ...]:
        if not self.available:
            return ()
        days = getattr(self._cfg, "revenge_days", 30)
        count = getattr(self._cfg, "revenge_entries", 100)
        days = min(3650, max(0, days)) if type(days) is int else 30
        count = min(MAX_SAVED_NAMES, max(0, count)) if type(count) is int else 100
        cutoff = self._clock() - days * 86400
        entries = [entry for entry in self._saved.values()
                   if not days or _activity_time(entry) is None or _activity_time(entry) >= cutoff]
        entries.sort(key=lambda entry: (_activity_time(entry) is None,
                                       -(_activity_time(entry) or 0), entry.name.casefold()))
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
            # hidden attacks must not appear when the opt-in or mode changes.
            self._delivery_cutoff = self._clock()
        self._presentation_available = self.available
        if self.available and not self._display_timer.isActive():
            self._display_timer.start()
        elif not self.available:
            self._display_timer.stop()
        if not self.available:
            # A hidden queued prompt must not appear after a later mode change.
            self._pending.clear()
            self._timer.stop()
            self._activity_refresh.stop()
        self.prompts_changed.emit()
        self.saved_changed.emit()

    @Slot(object, object)
    def on_message(self, _message: Any, event: Any) -> None:
        """QObject receiver for worker emissions; AutoConnection runs it on the GUI thread."""
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
            entry = self._saved[key]
            if _valid_time(now) and now is not None and (entry.last_activity is None or now > entry.last_activity):
                self._saved[key] = RevengeEntry(entry.name, entry.added_at, float(now))
                self._dirty = True
                # Coalesce a sustained fight into bounded two-second checkpoints,
                # and retain the newest value for shutdown/backup flushes.
                if not self._save_timer.isActive():
                    self._save_timer.start()
                if not self._activity_refresh.isActive():
                    self._activity_refresh.start()
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

    def save_attacker(self, name: str, *, from_prompt: bool = False) -> bool:
        if not self.available:
            return False
        if not _valid_saved_name(name):
            self._report_error("Enter a name to add to the revenge list.")
            return False
        name = name.strip()
        key = name.casefold()
        candidate = self._pending.get(key)
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
                             max(previous.last_activity or 0, float(now)) if previous else float(now))
        updated = {**self._saved, key: entry}
        if not self._persist(updated):
            return False
        self._saved = updated
        self._set_clean()
        self._seen.add(key)
        self._pending.pop(key, None)
        self.expire()
        self.saved_changed.emit()
        self.prompts_changed.emit()
        return True

    def remove_saved(self, name: str) -> bool:
        if not self.available or name.casefold() not in self._saved:
            return False
        updated = {key: value for key, value in self._saved.items() if key != name.casefold()}
        if not self._persist(updated):
            return False
        self._saved = updated
        self._set_clean()
        self.saved_changed.emit()
        return True

    def new_session(self) -> None:
        self._seen.clear()
        self._pending.clear()
        if self.available:
            self._delivery_cutoff = self._clock()
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
                    if (not isinstance(row, dict) or set(row) != {"name", "added_at", "last_activity"}
                            or not _valid_saved_name(row.get("name"))
                            or not _valid_time(row["added_at"]) or not _valid_time(row["last_activity"])
                            or row["name"].casefold() in saved):
                        raise ValueError("Invalid revenge entry")
                    saved[row["name"].casefold()] = RevengeEntry(row["name"], row["added_at"], row["last_activity"])
            else:
                raise ValueError("Unsupported revenge schema")
            self._saved = saved
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

    def _persist(self, saved: dict[str, RevengeEntry]) -> bool:
        try:
            payload = {"schema": 2, "entries": [
                {"name": entry.name, "added_at": entry.added_at, "last_activity": entry.last_activity}
                for entry in sorted(saved.values(), key=lambda entry: entry.name.casefold())]}
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
    if not controller.available:
        return "Carebear Mode hides revenge names and attacker prompts. Change Morality Adjustment in Settings to use this PvP feature."
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


class RevengeList(QFrame):
    """A persistent name list suitable for the overlay's Revenge tab."""

    def __init__(self, controller: RevengeController, parent: QWidget | None = None, *, compact: bool = False,
                 allow_popout: bool = True):
        super().__init__(parent)
        self.controller = controller
        self.compact = compact
        self.allow_popout = allow_popout
        self._manual_dialog: QInputDialog | None = None
        self.setStyleSheet(button_qss())
        self._rows: dict[str, QWidget] = {}
        self._dates: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        manual = QHBoxLayout()
        manual.setSpacing(6)
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Name")
        self.name_input.setAccessibleName("Player name to add to revenge list")
        self.name_input.setMinimumWidth(140)
        manual.addWidget(self.name_input, 1)
        self.add_button = QPushButton("Add player…" if compact else "Add")
        self.add_button.setObjectName("Chip")
        self.add_button.setAccessibleName("Add player name to revenge list")
        self.add_button.clicked.connect(self._open_manual if compact else self._add_manual)
        self.name_input.returnPressed.connect(self._add_manual)
        self.name_input.textChanged.connect(self._update_add_enabled)
        manual.addWidget(self.add_button)
        self.pop_out_button = QPushButton("Pop out")
        self.pop_out_button.setObjectName("Chip")
        self.pop_out_button.setAccessibleName("Pop out revenge list")
        self.pop_out_button.clicked.connect(self._pop_out)
        manual.addWidget(self.pop_out_button)
        layout.addLayout(manual)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.rows_scroll.setMaximumHeight(160 if compact else 200)
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
        controller.saved_changed.connect(self.refresh)
        controller.error.connect(self.error.setText)
        self.refresh()

    def _update_add_enabled(self):
        self.add_button.setEnabled(self.controller.available and (self.compact or _valid_saved_name(self.name_input.text())))

    def _add_manual(self):
        if self.controller.save_attacker(self.name_input.text().strip()):
            self.name_input.clear()

    def _pop_out(self):
        if self.allow_popout and self.controller.available:
            self.controller.pop_out_requested.emit()

    def _open_manual(self):
        if not self.controller.available:
            return
        if self._manual_dialog is not None:
            self._manual_dialog.raise_()
            self._manual_dialog.activateWindow()
            return
        # The overlay itself refuses focus. An explicitly opened normal dialog
        # can accept keyboard input without changing the overlay's window flags.
        dialog = QInputDialog(self.window())
        dialog.setWindowTitle(window_title("revenge-add"))
        dialog.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, False)
        dialog.setInputMode(QInputDialog.InputMode.TextInput)
        dialog.setLabelText("")
        dialog.setOkButtonText("Add player")
        dialog.setCancelButtonText("Cancel")
        self._manual_dialog = dialog
        # QInputDialog emits textValueSelected after finished. Reading and
        # clearing its value in one finished handler avoids either retaining a
        # hidden identity or clearing the accepted value before it is saved.
        dialog.finished.connect(lambda result: self._finish_manual(dialog, result))
        dialog.open()
        dialog.activateWindow()

    def _finish_manual(self, dialog: QInputDialog, result: int):
        if result == QDialog.DialogCode.Accepted:
            self.controller.save_attacker(dialog.textValue().strip())
        dialog.setTextValue("")
        if self._manual_dialog is dialog:
            self._manual_dialog = None
        dialog.deleteLater()

    def refresh(self):
        self.setVisible(self.controller.enabled)
        entries = self.controller.saved_entries
        names = tuple(entry.name for entry in entries)
        self.name_input.setVisible(self.controller.available and not self.compact)
        self.add_button.setVisible(self.controller.available)
        self.pop_out_button.setVisible(self.controller.available and self.allow_popout)
        self.pop_out_button.setEnabled(self.controller.available and self.allow_popout)
        self.name_input.setEnabled(self.controller.available)
        if not self.controller.available:
            self.name_input.clear()
            if self._manual_dialog is not None:
                self._manual_dialog.setTextValue("")
                self._manual_dialog.reject()
        self._update_add_enabled()
        for key in set(self._rows) - set(names):
            row = self._rows.pop(key)
            self._dates.pop(key, None)
            _scrub_row(row)
            self._row_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        for entry in entries:
            name = entry.name
            if name not in self._rows:
                row = QWidget()
                line = QHBoxLayout(row)
                line.setContentsMargins(0, 0, 0, 0)
                label = ElidedLabel(name)
                # Qt auto-detects rich tooltip text, although the label itself
                # is PlainText. Escape manual markup inside a trusted wrapper.
                label.setToolTip("<span>" + escape(name) + "</span>")
                line.addWidget(label, 1)
                date = QLabel()
                date.setTextFormat(Qt.TextFormat.PlainText)
                line.addWidget(date)
                remove = QPushButton("Remove")
                remove.setObjectName("Chip")
                remove.setAccessibleName(f"Remove {name} from revenge list")
                remove.clicked.connect(lambda _checked=False, n=name: self.controller.remove_saved(n))
                line.addWidget(remove)
                self._rows[name], self._dates[name] = row, date
            text, tooltip = _activity_caption(entry)
            self._dates[name].setText(text)
            self._dates[name].setToolTip(tooltip)
            # Reposition existing rows as activity changes, not only new rows.
            self._row_layout.removeWidget(self._rows[name])
            self._row_layout.addWidget(self._rows[name])
        message = (f"Revenge list · {len(names)} shown of {self.controller.saved_total} · newest activity first" if names else
                   "No entries match these limits. Saved names remain in your list; adjust Revenge List options."
                   if self.controller.saved_total else "Revenge list is empty. Add a player or use + on a recent PvP attacker.")
        self.status.setText(_hint(self.controller) or message)
        self.rows_scroll.setVisible(bool(names))
        self.error.setText(self.controller.last_error)


class _PopoutHeader(QFrame):
    def __init__(self, owner: "RevengePopoutWindow"):
        super().__init__(owner)
        self._owner = owner
        self._drag: QPoint | None = None
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 8, 4)
        heading = QLabel("Revenge List · PvP")
        heading.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(heading, 1)
        close = QPushButton("×")
        close.setObjectName("Chip")
        close.setAccessibleName("Close revenge list popout")
        close.setFixedWidth(32)
        close.clicked.connect(owner.hide)
        layout.addWidget(close)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag = event.globalPosition().toPoint() - self._owner.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self._owner.move(event.globalPosition().toPoint() - self._drag)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)


class RevengePopoutWindow(QWidget):
    """One root-owned passive window, synced to the same protected controller."""

    def __init__(self, controller: RevengeController, settings: Any = None, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.controller = controller
        self.setObjectName("RevengePopout")
        self.setWindowTitle(window_title("revenge-popout"))
        self.setAccessibleName("Revenge List popout")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setPalette(theme._build_palette())
        self.setStyleSheet(f"QWidget#RevengePopout {{ background: {theme.BG0}; border: 1px solid {theme.LINE}; }}"
                          f"QLabel {{ color: {theme.TEXT}; background: transparent; }}" + button_qss())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)
        self.header = _PopoutHeader(self)
        layout.addWidget(self.header)
        self.list = RevengeList(controller, compact=True, allow_popout=False)
        self.list.rows_scroll.setMaximumHeight(320)
        layout.addWidget(self.list, 1)
        self.setMinimumSize(360, 200)
        self.resize(460, 380)
        controller.saved_changed.connect(self._sync_visibility)
        controller.prompts_changed.connect(self._sync_visibility)
        self.hide()

    def _sync_visibility(self):
        if not self.controller.enabled:
            self.hide()

    def show(self):
        if self.controller.enabled:
            # No activation or raising: adding a name uses the explicit normal
            # input dialog, while the passive list leaves game focus untouched.
            super().show()


__all__ = ["RevengeController", "RevengePrompts", "RevengeList", "RevengePopoutWindow", "RevengeEntry", "AttackerCandidate", "plausible_attacker"]
