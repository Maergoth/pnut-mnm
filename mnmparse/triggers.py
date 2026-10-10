"""Triggers: match a chat line, then play a sound, speak, and/or start a timer.

A :class:`Trigger` has a text pattern and a match mode:

* ``contains`` - the pattern appears somewhere in the line (the default);
* ``starts``   - the line starts with the pattern;
* ``exact``    - the whole line is the pattern;
* ``regex``    - a Python regular expression (named groups can be spoken: ``{who}``).

Except for regex, matching ignores case, punctuation and spacing, and with ``fuzzy`` on
(the default) every word of the pattern may be one or two letters off, because the chat
is read by OCR ("Dogabetarozem", "begins castinz").

The action is ``none``, ``sound`` (a built-in cue), ``file`` (a .wav/.mp3/.ogg) or
``speak`` (text to speech; ``{line}``, ``{match}`` and regex groups are filled in).  A
trigger can also start a timer (``timer_seconds``) shown on the overlay's timer panel;
the timer can warn shortly before it ends and alert when it does.

Triggers are stored in ``triggers.json`` next to ``config.json``.  This module has no Qt:
the app plays the sounds and draws the timers (``mnmparse.app.triggers_runtime``).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import regex

from .vocab import edit_distance, fold, word_limit

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS", "BUILTIN_SOUNDS", "MODES", "TIMER_MODES", "ActiveTimer", "Match", "TimerBoard", "Trigger",
    "TriggerStore", "fill_placeholders", "match_trigger",
]

MODES: tuple[str, ...] = ("contains", "starts", "exact", "regex")
ACTIONS: tuple[str, ...] = ("none", "sound", "file", "speak")
TIMER_MODES: tuple[str, ...] = ("replace", "retain", "stack")
_LEGACY_TIMER_MODES = {"restart": "replace", "ignore": "retain"}
#: Built-in cues (generated as small .wav files the first time they are needed).
BUILTIN_SOUNDS: tuple[str, ...] = ("Chime", "Dink", "Alert", "Bell", "Beep", "Low tone", "Tick", "Rising", "Falling", "Siren")
STORE_VERSION = 1
# Each search has an enforceable engine deadline. The store also limits an entire
# line so many individually expensive expressions cannot monopolize the GUI.
REGEX_TIMEOUT_S = 0.010
LINE_MATCH_BUDGET_S = 0.050
MAX_PATTERN_CHARS = 2048
MAX_LINE_CHARS = 8192
_quarantined_patterns: dict[str, str] = {}
_store_replace_lock = threading.Lock()

_WORD_RX = re.compile(r"[0-9a-z]+")


# --------------------------------------------------------------------------- model
@dataclass
class Trigger:
    """One trigger (see module docstring)."""

    name: str = "New trigger"
    pattern: str = ""
    mode: str = "contains"
    fuzzy: bool = True
    enabled: bool = True
    action: str = "sound"
    sound: str = "Chime"
    file: str = ""
    speech: str = "{match}"
    volume: int = 80  #: 0..100, multiplied by the master volume
    cooldown_s: float = 0.0  #: ignore repeats within this many seconds (0: every match)
    timer: bool = False
    timer_seconds: float = 30.0
    timer_label: str = ""  #: popup/timer label ("" = the trigger's name); placeholders allowed
    timer_mode: str = "replace"  #: replace / retain this trigger's running timer, or stack
    timer_color: str = ""  #: #rrggbb, or empty to use the theme's green
    timer_warn_color: str = ""  #: empty: theme amber
    timer_low_color: str = ""  #: empty: theme red (also used when the timer ends)
    timer_low_s: float = 5.0
    timer_warn_s: float = 0.0  #: warn this many seconds before the end (0: no warning)
    timer_warn_action: str = "sound"
    timer_warn_sound: str = "Tick"
    timer_warn_speech: str = "{label} soon"
    timer_end_action: str = "sound"
    timer_end_sound: str = "Bell"
    timer_end_speech: str = "{label}"
    category: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Trigger":
        known = {f.name for f in dataclasses.fields(cls)}
        trig = cls(**{k: v for k, v in data.items() if k in known})
        if trig.mode not in MODES:
            trig.mode = "contains"
        for name in ("action", "timer_warn_action", "timer_end_action"):
            if getattr(trig, name) not in ACTIONS:
                setattr(trig, name, "none")
        trig.timer_mode = _LEGACY_TIMER_MODES.get(trig.timer_mode, trig.timer_mode)
        if trig.timer_mode not in TIMER_MODES:
            trig.timer_mode = "replace"
        for name in ("timer_color", "timer_warn_color", "timer_low_color"):
            value = getattr(trig, name)
            if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                setattr(trig, name, "")
        trig.timer_low_s = max(0.0, float(trig.timer_low_s))
        trig.volume = max(0, min(100, int(trig.volume)))
        trig.timer_seconds = max(1.0, float(trig.timer_seconds))
        return trig

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def problems(self) -> list[str]:
        out: list[str] = []
        if not self.pattern.strip():
            out.append("The pattern is empty.")
        if len(self.pattern) > MAX_PATTERN_CHARS:
            out.append(f"The pattern is too long (maximum {MAX_PATTERN_CHARS} characters).")
        if self.mode == "regex":
            problem = regex_problem(self.pattern)
            if problem:
                out.append(problem)
        if self.action == "file" and not self.file:
            out.append("Choose a sound file.")
        return out


@dataclass
class Match:
    """A successful match: the matched text and any named regex groups."""

    trigger: Trigger
    line: str
    text: str
    groups: dict[str, str] = field(default_factory=dict)
    privacy_mode: str = ""  #: presentation-only matches have already withheld source captures

    def values(self, **extra: str) -> dict[str, str]:
        vals = {"line": self.line, "match": self.text, "name": self.trigger.name}
        vals.update({k: v for k, v in self.groups.items() if v is not None})
        vals.update(extra)
        return vals


def fill_placeholders(template: str, values: dict[str, str]) -> str:
    """``"{who} is down"`` with ``values``; unknown placeholders stay as written."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template or "")


# --------------------------------------------------------------------------- matching
@lru_cache(maxsize=256)
def _compiled(pattern: str) -> Any | None:
    if len(pattern) > MAX_PATTERN_CHARS:
        return None
    try:
        # VERSION0 keeps compatibility with the Python re expressions accepted
        # by previous releases, while adding the timeout-capable engine.
        return regex.compile(pattern, regex.IGNORECASE | regex.VERSION0)
    except (regex.error, RecursionError, OverflowError):
        return None


def regex_problem(pattern: str) -> str:
    """A safe, source-free explanation suitable for live/editor/import feedback."""
    if len(pattern) > MAX_PATTERN_CHARS:
        return f"The pattern is too long (maximum {MAX_PATTERN_CHARS} characters)."
    if pattern in _quarantined_patterns:
        return _quarantined_patterns[pattern]
    if _compiled(pattern) is None:
        return "The regular expression is invalid."
    return ""


def _words(text: str) -> list[tuple[str, int, int]]:
    """Folded words of ``text`` with their spans in the folded string."""
    folded = fold(text)
    return [(m.group(0), m.start(), m.end()) for m in _WORD_RX.finditer(folded)]


def _word_ok(pat: str, word: str, fuzzy: bool) -> bool:
    if pat == word:
        return True
    if not fuzzy:
        return False
    limit = word_limit(min(len(pat), len(word)))
    return limit > 0 and edit_distance(pat, word, limit) <= limit


def match_trigger(trigger: Trigger, line: str, *, deadline: float | None = None) -> Match | None:
    """``Match`` when ``line`` satisfies ``trigger`` (disabled triggers never match)."""
    if not trigger.enabled or not trigger.pattern.strip() or not line:
        return None
    if len(trigger.pattern) > MAX_PATTERN_CHARS or len(line) > MAX_LINE_CHARS:
        return None
    deadline = time.monotonic() + REGEX_TIMEOUT_S if deadline is None else deadline
    if time.monotonic() >= deadline:
        return None
    if trigger.mode == "regex":
        if trigger.pattern in _quarantined_patterns:
            return None
        rx = _compiled(trigger.pattern)
        if rx is None:
            return None
        try:
            m = rx.search(line, timeout=max(0.0001, min(REGEX_TIMEOUT_S, deadline - time.monotonic())))
        except TimeoutError:
            # Repeating an expensive expression on every OCR line would consume
            # the deadline repeatedly. Quarantine until its pattern is changed.
            if len(_quarantined_patterns) >= 256:
                _quarantined_patterns.pop(next(iter(_quarantined_patterns)))
            _quarantined_patterns[trigger.pattern] = "Regular expression exceeded its time limit; change the pattern before enabling it again."
            return None
        if m is None:
            return None
        groups = {k: v for k, v in m.groupdict().items() if v is not None}
        groups.update({str(i): g for i, g in enumerate(m.groups(), 1) if g is not None})
        return Match(trigger, line, m.group(0), groups)
    pat = [w for w, _s, _e in _words(trigger.pattern)]
    if not pat:
        return None
    words = _words(line)
    n, k = len(words), len(pat)
    starts: range | list[int]
    if trigger.mode == "starts":
        starts = [0]
    elif trigger.mode == "exact":
        if n != k:
            return None
        starts = [0]
    else:
        starts = range(0, n - k + 1)
    for i in starts:
        if time.monotonic() >= deadline:
            return None
        if i + k > n:
            break
        if all(_word_ok(p, words[i + j][0], trigger.fuzzy) for j, p in enumerate(pat)):
            folded = fold(line)
            return Match(trigger, line, folded[words[i][1]:words[i + k - 1][2]])
    return None


# --------------------------------------------------------------------------- storage
class TriggerStore:
    """The trigger list and the audio settings, saved as JSON."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else None
        self.triggers: list[Trigger] = []
        self.volume = 80  #: master volume 0..100
        self.voice = ""  #: text-to-speech voice name ("" = the system default)
        self.rate = 0.0  #: speech rate -1..1
        self.output_device = ""  #: audio output device description ("" = the default)
        self.installed_presets: set[str] = set()
        self.match_warnings: dict[str, str] = {}

    def load(self) -> bool:
        if self.path is None or not self.path.is_file():
            return False
        try:
            if self.path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Trigger settings exceed the 8 MB limit")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.load_dict(data)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
            log.warning("could not read %s: %s", self.path, exc)
            return False
        return True

    def load_dict(self, data: dict[str, Any]) -> None:
        """Validate a local store completely before changing in-memory settings.

        Unlike a share, a local row may be an unfinished edit with an empty pattern.
        Invalid versions/types/nonfinite values are rejected rather than dropped.
        """
        if not isinstance(data, dict):
            raise ValueError("Trigger settings must be an object")
        if "version" in data and (type(data["version"]) is not int or data["version"] != STORE_VERSION):
            raise ValueError("Unsupported trigger settings version")
        settings = data.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("Trigger audio settings must be an object")
        for key in ("volume", "rate"):
            value = settings.get(key, getattr(self, key))
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"Trigger {key} must be a finite number")
        for key in ("voice", "output_device"):
            if not isinstance(settings.get(key, getattr(self, key)), str):
                raise ValueError(f"Trigger {key} must be text")
        presets = data.get("installed_presets", [])
        if not isinstance(presets, list) or any(not isinstance(key, str) for key in presets):
            raise ValueError("Installed trigger presets must be a list of text IDs")
        rows = data.get("triggers", [])
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("Trigger settings must contain at most 5000 trigger objects")
        boolean = {"enabled", "fuzzy", "timer"}
        numbers = {"volume", "cooldown_s", "timer_seconds", "timer_warn_s", "timer_low_s"}
        known = {f.name for f in dataclasses.fields(Trigger)}
        validated: list[Trigger] = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - known:
                raise ValueError("Trigger settings contain an invalid or unsupported field")
            for key, value in row.items():
                if key in boolean:
                    if type(value) is not bool:
                        raise ValueError(f"Trigger {key} must be true or false")
                elif key in numbers:
                    if type(value) not in (int, float) or not math.isfinite(value):
                        raise ValueError(f"Trigger {key} must be a finite number")
                elif not isinstance(value, str):
                    raise ValueError(f"Trigger {key} must be text")
            validated.append(Trigger.from_dict(row))
        self.volume = max(0, min(100, int(settings.get("volume", self.volume))))
        self.voice = settings.get("voice", self.voice)
        self.rate = max(-1.0, min(1.0, float(settings.get("rate", self.rate))))
        self.output_device = settings.get("output_device", self.output_device)
        self.installed_presets = set(presets)
        self.triggers = validated

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "settings": {"volume": self.volume, "voice": self.voice, "rate": self.rate,
                         "output_device": self.output_device},
            "triggers": [t.to_dict() for t in self.triggers],
            "installed_presets": sorted(self.installed_presets),
        }

    def install_presets(self) -> bool:
        """Add bundled starters once, preserving customizations and deliberate deletions."""
        from .trigger_presets import (
            INVIS_BREAK_ID, INVIS_BREAK_PATTERN, INVIS_BREAK_PATTERN_REVISION,
            INVIS_BREAK_ALERT_REVISION, PRESETS,
        )

        changed = False
        for data in PRESETS:
            key = str(data["id"])
            if key in self.installed_presets:
                continue
            # Recognize the author's original IDs and renamed/imported copies too.
            exists = any(t.id == key or fold(t.name).replace(" ", "") == fold(str(data["name"])).replace(" ", "")
                         or (t.mode == data.get("mode", "contains") and fold(t.pattern) == fold(str(data["pattern"])))
                         for t in self.triggers)
            if not exists:
                self.triggers.append(Trigger.from_dict(data))
            self.installed_presets.add(key)
            changed = True
        if INVIS_BREAK_PATTERN_REVISION not in self.installed_presets:
            # Correct only the original preset's unchanged match. Imported same-name
            # triggers, custom patterns/modes and every other user setting stay intact.
            trigger = self.find(INVIS_BREAK_ID)
            if trigger is not None and trigger.pattern == "to appear" and trigger.mode == "contains":
                trigger.pattern = INVIS_BREAK_PATTERN
            # Record skipped/deleted presets too: a later deliberate edit must not be
            # silently undone on every startup (or a future app update).
            self.installed_presets.add(INVIS_BREAK_PATTERN_REVISION)
            changed = True
        if INVIS_BREAK_ALERT_REVISION not in self.installed_presets:
            trigger = self.find(INVIS_BREAK_ID)
            # Retire the starter's old 30-second countdown, including copies
            # whose owner already silenced its warning/end. Preserve customized
            # timers and the user's chosen immediate audio, label and match mode.
            if (trigger is not None and trigger.pattern == INVIS_BREAK_PATTERN
                    and trigger.mode in ("contains", "starts")
                    and trigger.timer and trigger.timer_seconds == 30
                    and trigger.timer_warn_s in (0, 1)
                    and (trigger.timer_warn_action == "none"
                         or (trigger.timer_warn_action == "speak"
                             and trigger.timer_warn_speech == "{label} soon"))
                    and (trigger.timer_end_action == "none"
                         or (trigger.timer_end_action == "sound" and trigger.timer_end_sound == "Bell"))):
                trigger.timer = False
                trigger.timer_warn_s = 0.0
                trigger.timer_warn_action = "none"
                trigger.timer_end_action = "none"
            self.installed_presets.add(INVIS_BREAK_ALERT_REVISION)
            changed = True
        return changed

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=f".{self.path.name}.", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.to_dict(), stream, indent=2, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            # Windows replacement handles can briefly deny sharing when two
            # writers target the same file. Serialize the final rename in-process.
            with _store_replace_lock:
                os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def find(self, trigger_id: str) -> Trigger | None:
        return next((t for t in self.triggers if t.id == trigger_id), None)

    def matches(self, line: str) -> list[Match]:
        out = []
        self.match_warnings = {}
        if len(line) > MAX_LINE_CHARS:
            self.match_warnings[""] = f"Chat line exceeds the {MAX_LINE_CHARS}-character matching limit."
            return out
        deadline = time.monotonic() + LINE_MATCH_BUDGET_S
        for trig in self.triggers:
            if time.monotonic() >= deadline:
                self.match_warnings[""] = "Trigger matching reached the time limit for this line; some triggers were skipped."
                break
            m = match_trigger(trig, line, deadline=deadline)
            if trig.enabled and trig.mode == "regex" and trig.pattern in _quarantined_patterns:
                self.match_warnings[trig.id] = regex_problem(trig.pattern)
                trig.enabled = False
            if m is not None:
                out.append(m)
        return out


# --------------------------------------------------------------------------- timers
@dataclass
class ActiveTimer:
    """A running countdown on the timer panel."""

    id: str
    trigger_id: str
    label: str
    start: float
    duration: float
    warn_s: float = 0.0
    warned: bool = False
    ended: bool = False
    color: str = ""
    warn_color: str = ""
    low_color: str = ""
    low_s: float = 5.0
    keep_until_dismissed: bool = False
    private_label: str = ""  #: approved static personal title for protected presentation
    private_player: str = ""  #: character identity required by that approval; empty for manual titles

    def remaining(self, now: float) -> float:
        return max(0.0, self.start + self.duration - now)

    def fraction(self, now: float) -> float:
        """Share of the time still left (1 -> 0)."""
        return self.remaining(now) / self.duration if self.duration > 0 else 0.0


class TimerBoard:
    """Countdowns with warnings and expiry; retained timers wait for dismissal."""

    LINGER_S = 3.0  #: an ended timer stays on screen this long (flashing 0:00)
    MAX_TIMERS = 24  #: ordinary timer limit; retained countdowns do not consume this capacity

    def __init__(self) -> None:
        self.timers: list[ActiveTimer] = []

    def start(self, trigger: Trigger, label: str, now: float | None = None, *,
              keep_until_dismissed: bool = False) -> ActiveTimer | None:
        """Start a fresh timer; ``None`` means retain an unexpired timer and ignore the match."""
        now = time.time() if now is None else now
        running = [t for t in self.timers if t.trigger_id == trigger.id and not t.ended and t.remaining(now) > 0]
        mode = _LEGACY_TIMER_MODES.get(trigger.timer_mode, trigger.timer_mode)
        if running and mode == "retain":
            return None
        if mode != "stack":
            self.timers = [t for t in self.timers if t.trigger_id != trigger.id]
        timer = ActiveTimer(uuid.uuid4().hex[:8], trigger.id, label, now, float(trigger.timer_seconds),
                            float(trigger.timer_warn_s), color=trigger.timer_color,
                            warn_color=trigger.timer_warn_color, low_color=trigger.timer_low_color,
                            low_s=trigger.timer_low_s, keep_until_dismissed=keep_until_dismissed)
        self.timers.append(timer)
        ordinary = [t for t in self.timers if not t.keep_until_dismissed]
        if len(ordinary) > self.MAX_TIMERS:
            kept = {t.id for t in sorted(ordinary, key=lambda t: t.remaining(now))[:self.MAX_TIMERS]}
            self.timers = [t for t in self.timers if t.keep_until_dismissed or t.id in kept]
        return timer

    def cancel(self, timer_id: str) -> None:
        self.timers = [t for t in self.timers if t.id != timer_id]

    def clear(self) -> None:
        self.timers.clear()

    def tick(self, now: float | None = None) -> tuple[list[ActiveTimer], list[ActiveTimer]]:
        """``(warned now, ended now)``; ordinary ended timers disappear after ``LINGER_S``."""
        now = time.time() if now is None else now
        warned, ended = [], []
        for t in self.timers:
            left = t.start + t.duration - now
            if not t.warned and t.warn_s > 0 and 0 < left <= t.warn_s:
                t.warned = True
                warned.append(t)
            if not t.ended and left <= 0:
                t.ended = True
                ended.append(t)
        self.timers = [t for t in self.timers if t.keep_until_dismissed
                       or not (t.ended and now - (t.start + t.duration) > self.LINGER_S)]
        return warned, ended

    def ordered(self, now: float | None = None) -> list[ActiveTimer]:
        """Running timers soonest first, then the ones that just ended."""
        now = time.time() if now is None else now
        return sorted(self.timers, key=lambda t: (t.ended, t.remaining(now), t.label))
