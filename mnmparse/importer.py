"""Import previously written log files back into encounters and a session.

Two formats are understood, both produced by :class:`mnmparse.logwriter.LogWriter`:

* the raw EverQuest-style log, ``[Fri Oct 02 01:57:34 2026] You crush a crocodile ...``
  (also the older one-per-day ``combat_YYYY-MM-DD.log`` files, and logs edited by hand);
* the events file, one JSON object per line as written by ``write_event``.

The raw log is re-parsed with the current grammar, so improvements to the parser apply to
old sessions; the events file replays exactly what was parsed at the time.

Two kinds of re-read line are left out (see :mod:`mnmparse.replay`): blocks the tracker read
twice, in logs written before it learned to recognise a re-shown screen (no format header and
started before :data:`REPLAY_AWARE_SINCE`), and, when :func:`import_files` imports a run of
files, the lines a file opens with that the previous file already logged (each app start reads
the chat window as it is).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .app.models import EncounterSnapshot, build_snapshot
from .grammar import Event
from .logwriter import file_format, is_header
from .parser import NameCompleter, parse_garbled_amount, parse_line, split_fused
from .party import RELOAD_MAX_AGE_S, PartyRoster
from .session import SessionSnapshot, SessionStats
from .replay import RESTART_GAP_S, TAIL_LINES, find_backlog, find_replays
from .stats import Stats
from .vocab import Vocabulary

__all__ = [
    "ImportResult",
    "import_file",
    "import_files",
    "iter_log_events",
    "iter_jsonl_events",
    "LOG_LINE_RE",
    "REPLAY_AWARE_SINCE",
]

log = logging.getLogger(__name__)

LOG_STAMP_FMT = "%a %b %d %H:%M:%S %Y"
LOG_LINE_RE = re.compile(r"^\[(?P<stamp>[A-Za-z]{3} [A-Za-z]{3} \d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]\s?(?P<text>.*)$")
_EVENT_FIELDS = {f.name for f in dataclasses.fields(Event)}
#: Since this moment (local time) the tracker suppresses a re-shown screen itself, so logs
#: started then or later hold no replayed blocks even without a format header.
REPLAY_AWARE_SINCE = time.mktime((2026, 10, 2, 16, 44, 0, 0, 0, -1))


@dataclass
class ImportResult:
    """Everything one imported file produced."""

    path: Path
    kind: str  #: "log" or "events"
    messages: int
    counts: dict[str, int]
    started: float | None
    ended: float | None
    encounters: list[EncounterSnapshot]
    session: SessionSnapshot
    stats: Stats
    events: list[Event] = field(default_factory=list, repr=False)
    replays_dropped: int = 0  #: lines removed as re-read copies of earlier lines (old logs)
    #: opening lines removed as repeats of the previous file's last lines (import_files only)
    backlog_dropped: int = 0

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def unknown(self) -> int:
        return int(self.counts.get("unknown", 0))


def _iter_log_lines(path: Path) -> Iterator[tuple[float, str]]:
    """``(ts, text)`` of a raw combat log.  Lines without a stamp inherit the previous time."""
    last_ts = 0.0
    # utf-8-sig drops a BOM that Notepad/PowerShell may have added.
    with Path(path).open(encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\r\n")
            if not raw.strip() or is_header(raw):
                continue
            m = LOG_LINE_RE.match(raw)
            text = raw
            if m is not None:
                text = m.group("text")
                try:
                    last_ts = time.mktime(time.strptime(m.group("stamp"), LOG_STAMP_FMT))
                except (ValueError, OverflowError):
                    pass
            yield last_ts, text


def _iter_jsonl_lines(path: Path) -> Iterator[tuple[float, str]]:
    """``(ts, text)`` of an events file (the stored classification is not used)."""
    with Path(path).open(encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("skipping unreadable events line: %r", raw[:80])
                continue
            if not isinstance(data, dict) or not isinstance(data.get("text"), str):
                continue
            try:
                ts = float(data.get("ts") or 0.0)
            except (TypeError, ValueError):
                continue
            yield ts, data["text"]


def _parse_lines(lines: Iterable[tuple[float, str]], player_name: str) -> Iterator[Event]:
    """Parse ``(ts, text)`` lines with today's grammar.

    Lines the grammar cannot classify get one repair attempt through
    :class:`~mnmparse.parser.NameCompleter` (names learned from the lines before them).
    """
    names = NameCompleter()
    for ts, line in lines:
        for text in split_fused(line, player_name):  # two messages the OCR ran together
            ev = parse_line(text, ts, player_name)
            if ev.kind == "unknown":
                fixed = names.complete(text)
                if fixed is not None:
                    repaired = parse_line(fixed, ts, player_name)
                    if repaired.kind != "unknown":
                        ev = repaired
            names.observe(ev)
            yield ev


def iter_log_events(path: Path, player_name: str = "") -> Iterator[Event]:
    """Parse a raw combat log line by line (see :func:`_parse_lines`)."""
    return _parse_lines(_iter_log_lines(path), player_name)


def iter_jsonl_events(path: Path) -> Iterator[Event]:
    """Replay an events file written by :class:`~mnmparse.logwriter.LogWriter`."""
    with Path(path).open(encoding="utf-8-sig", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                log.debug("skipping unreadable events line: %r", raw[:80])
                continue
            if not isinstance(data, dict) or "text" not in data:
                continue
            kwargs = {k: v for k, v in data.items() if k in _EVENT_FIELDS}
            kwargs.setdefault("ts", 0.0)
            kwargs.setdefault("kind", "unknown")
            try:
                kwargs["ts"] = float(kwargs["ts"] or 0.0)
                ev = Event(**kwargs)
            except (TypeError, ValueError):
                log.debug("skipping malformed event: %r", raw[:80])
                continue
            if ev.kind == "coin":
                _recount_coin(ev)
            yield ev


def _recount_coin(ev: Event) -> None:
    """Recompute a coin event's copper from its text.

    Events files written before 2026-10-02 stored copper with the wrong ratios (10:1);
    the text is the truth, so the values are rebuilt with today's 100:1 ratios.
    """
    from mnmparse.parser import parse_line

    fresh = parse_line(ev.text, ev.ts, "")
    if fresh.kind == "coin":
        ev.copper = fresh.copper
        ev.split_copper = fresh.split_copper


def import_file(
    path: str | Path,
    *,
    player_name: str = "",
    encounter_timeout_s: float = 12.0,
    include_personal: bool = False,
    keep_events: bool = False,
    vocab: Vocabulary | None = None,
    drop_replays: bool = True,
    dummy_fix: bool = False,
    pet_owners: dict[str, str] | None = None,
) -> ImportResult:
    """Rebuild encounters and a session from a raw log or an events file.

    ``vocab`` collects the file's spellings and merges OCR variants (a fresh one when
    ``None``; the app passes its shared one so imports and live capture learn from each
    other).  ``drop_replays`` removes blocks that re-read earlier lines (see
    :mod:`mnmparse.replay`) from logs written before the tracker recognised them itself
    (no format header, started before :data:`REPLAY_AWARE_SINCE`); newer logs are taken
    as they are.  To import consecutive files of one session, use :func:`import_files`.

    Raises ``FileNotFoundError`` when ``path`` does not exist; any other trouble in a
    single line is skipped and logged.
    """
    return _import(
        _read(path, player_name),
        player_name=player_name,
        encounter_timeout_s=encounter_timeout_s,
        include_personal=include_personal,
        keep_events=keep_events,
        vocab=vocab if vocab is not None else Vocabulary(),
        drop_replays=drop_replays,
        dummy_fix=dummy_fix,
        pet_owners=pet_owners,
    )


def import_files(
    paths: Sequence[str | Path],
    *,
    player_name: str = "",
    encounter_timeout_s: float = 12.0,
    include_personal: bool = False,
    keep_events: bool = False,
    vocab: Vocabulary | None = None,
    drop_replays: bool = True,
    dummy_fix: bool = False,
    pet_owners: dict[str, str] | None = None,
) -> list[ImportResult]:
    """Import several files as one stretch of play; results in time order (by first stamp).

    The files share ``vocab`` (a fresh one when ``None``) and the party roster, which goes
    on from one file to the next as it would have live (after a break longer than
    :data:`~mnmparse.party.RELOAD_MAX_AGE_S` it starts over, as the app's saved party
    would).  A file that starts at most :data:`~mnmparse.replay.RESTART_GAP_S` after the
    previous one ended is a restart of the app: its opening lines that repeat the previous
    file's last ones were logged twice, and are left out
    (:func:`~mnmparse.replay.find_backlog`, counted in ``backlog_dropped``).  The other
    arguments are those of :func:`import_file`.

    Raises ``FileNotFoundError`` (before importing anything) when a path does not exist.
    """
    for path in paths:
        if not Path(path).is_file():
            raise FileNotFoundError(path)
    vocab = vocab if vocab is not None else Vocabulary()
    parsed = [_read(path, player_name) for path in paths]
    order = sorted(range(len(parsed)), key=lambda i: (parsed[i].first is None, parsed[i].first or 0.0, i))
    results: list[ImportResult] = []
    roster: PartyRoster | None = None
    done: list[_Parsed] = []
    for i in order:
        current = parsed[i]
        tail: list[str] = []
        start = current.first
        # The file that ended last before this one started (a .log and its .jsonl overlap
        # each other entirely: neither is the other's predecessor).
        ends = [(p.last, p) for p in done if start is not None and p.last is not None and p.last <= start]
        if ends and start is not None:
            end, previous = max(ends, key=lambda e: e[0])
            if start - end <= RESTART_GAP_S:
                tail = [ev.text for ev in previous.events[-TAIL_LINES:]]
            elif start - end > RELOAD_MAX_AGE_S:
                roster = None  # another session: the app would not have kept that party either
        result = _import(
            current,
            player_name=player_name,
            encounter_timeout_s=encounter_timeout_s,
            include_personal=include_personal,
            keep_events=keep_events,
            vocab=vocab,
            drop_replays=drop_replays,
            dummy_fix=dummy_fix,
            pet_owners=pet_owners,
            roster=roster,
            tail=tail,
        )
        results.append(result)
        roster = result.stats.roster
        done.append(current)
    return results


@dataclass
class _Parsed:
    """One file, parsed line by line (nothing dropped yet)."""

    path: Path
    kind: str
    events: list[Event]
    first: float | None  #: the first stamp (None for a file without one)
    last: float | None  #: the latest stamp
    replay_aware: bool  #: written by a tracker that suppresses re-shown screens itself


def _read(path: str | Path, player_name: str) -> _Parsed:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    kind = "events" if path.suffix.lower() == ".jsonl" else "log"
    # Both formats are re-parsed from their text, so files written by older versions get
    # today's grammar (an events file stores the classification of the day it was written).
    lines = _iter_jsonl_lines(path) if kind == "events" else _iter_log_lines(path)
    events = list(_parse_lines(lines, player_name))
    stamps = [float(ev.ts) for ev in events if ev.ts]
    first = stamps[0] if stamps else None
    aware = file_format(path) >= 2 or (first is not None and first >= REPLAY_AWARE_SINCE)
    return _Parsed(path, kind, events, first, max(stamps) if stamps else None, aware)


def _import(
    parsed: _Parsed,
    *,
    player_name: str,
    encounter_timeout_s: float,
    include_personal: bool,
    keep_events: bool,
    vocab: Vocabulary,
    drop_replays: bool,
    dummy_fix: bool,
    roster: PartyRoster | None = None,
    pet_owners: dict[str, str] | None = None,
    tail: Sequence[str] = (),
) -> ImportResult:
    """Build an :class:`ImportResult` from ``parsed`` (``roster``: carry on with this party;
    ``tail``: the previous file's last lines, for the restart backlog)."""
    path, all_events = parsed.path, parsed.events
    backlog = find_backlog(tail, [(float(ev.ts), ev.text) for ev in all_events]) if tail else set()
    if backlog:
        log.info("%s: %d opening lines repeat the previous file", path.name, len(backlog))
    replays: set[int] = set()
    if drop_replays and not parsed.replay_aware:
        rest = [i for i in range(len(all_events)) if i not in backlog]
        found = find_replays([(float(all_events[i].ts), all_events[i].text) for i in rest])
        replays = {rest[j] for j in found}
        if replays:
            log.info("%s: %d replayed lines dropped", path.name, len(replays))

    stats = Stats(encounter_timeout_s, vocab=vocab, player_name=player_name)
    if roster is not None:
        stats.roster = roster
    for pet, owner in (pet_owners or {}).items():
        stats.roster.set_pet_owner(pet, owner)
    session: SessionStats | None = None
    counts: Counter[str] = Counter()
    events: list[Event] = []
    first: float | None = None
    last: float | None = None
    n = 0
    for i, ev in enumerate(all_events):
        if i in backlog:
            continue  # the roster saw these in the previous file
        if i in replays:
            # A party line among them still counts (a real one may be taken for a copy).
            stats.roster.observe(ev)
            continue
        if dummy_fix and ev.kind in ("unknown", "ability_partial"):
            guess = parse_garbled_amount(ev.text, ev.ts, player_name)
            if guess is not None and stats.estimate_amount(guess):
                ev = guess
        n += 1
        counts[ev.kind] += 1
        if first is None:
            first = float(ev.ts)
            session = SessionStats(
                player_name, include_personal=include_personal, started=first, vocab=vocab, roster=stats.roster
            )
        last = float(ev.ts) if last is None else max(last, float(ev.ts))
        stats.add(ev)
        assert session is not None
        session.add(ev)
        if keep_events:
            events.append(ev)
    if session is None:
        session = SessionStats(
            player_name, include_personal=include_personal, started=0.0, vocab=vocab, roster=stats.roster
        )
    if last is not None:
        stats.expire(last + stats.encounter_timeout_s + 1.0)  # close the trailing encounter
    encounters = [build_snapshot(stats, enc, player_name) for enc in stats.history]
    for snap in encounters:
        if snap.ours:  # other groups' fights nearby are not the session's time in combat
            session.note_encounter(snap.duration)
    result = ImportResult(
        path=path,
        kind=parsed.kind,
        messages=n,
        counts=dict(counts),
        started=first,
        ended=last,
        encounters=encounters,
        session=session.snapshot(now=last if last is not None else 0.0),
        stats=stats,
        events=events,
        replays_dropped=len(replays),
        backlog_dropped=len(backlog),
    )
    log.info("imported %s: %d lines, %d encounters", path.name, n, len(encounters))
    return result
