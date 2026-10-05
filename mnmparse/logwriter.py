"""Log file writers: an EverQuest-style raw combat log and an events JSONL file.

Files are opened lazily on first write, appended to and flushed after every write (so a
crash loses nothing).  The two files of a *segment* are always paired: the raw log
``logs/combat_2026-10-02_015701.log`` and the events file
``logs/events_2026-10-02_015701.jsonl`` carry the date and time of the first message they
hold, and a new segment starts

* when more than ``break_s`` seconds (default one hour) passed since the previous message,
* or when the raw log reaches ``max_bytes`` (default 5 MB),
* or whenever a new :class:`LogWriter` is created (each capture start).

Each new file starts with a header line naming its format (:data:`LOG_FORMAT`):
``# mnmparse combat log, format 2`` in the raw log, ``{"mnmparse": "events", "format": 2}``
in the events file.  Readers skip it (:func:`is_header`) and :func:`file_format` reads it.

Older files named ``combat_YYYY-MM-DD.log`` (one per day, from earlier versions) are still
readable by :mod:`mnmparse.importer`.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from .config import project_path

if TYPE_CHECKING:  # pragma: no cover - typing only; these modules live elsewhere
    from .parser import Event
    from .tracker import Message

log = logging.getLogger(__name__)

#: EverQuest-style timestamp, e.g. ``Wed Oct 01 17:11:03 2026``.
EQ_TIMESTAMP_FORMAT = "%a %b %d %H:%M:%S %Y"
#: Segment name stamp, e.g. ``2026-10-02_015701``.
SEGMENT_FORMAT = "%Y-%m-%d_%H%M%S"
DEFAULT_MAX_BYTES = 5_000_000
DEFAULT_BREAK_S = 3600.0

#: Format of the files written here, named in their header line.  Files without a header are
#: format 1.  Format 2: the tracker that wrote them recognises a re-shown screen itself, so a
#: reader need not look for replayed blocks (mnmparse.replay).
LOG_FORMAT = 2
#: Start of the raw log's header line, ``# mnmparse combat log, format 2``.
RAW_HEADER_PREFIX = "# mnmparse"
#: Key of the events file's header object, ``{"mnmparse": "events", "format": 2}`` (it has no
#: "text", so every events reader passes over it).
EVENTS_HEADER_KEY = "mnmparse"
_FORMAT_RX = re.compile(r"\bformat\s+(\d+)")


def eq_timestamp(ts: float) -> str:
    """Format an epoch time as the bracketed EverQuest log timestamp."""
    return "[" + time.strftime(EQ_TIMESTAMP_FORMAT, time.localtime(ts)) + "]"


def raw_header(fmt: int = LOG_FORMAT) -> str:
    """The raw log's header line (without the line break)."""
    return f"{RAW_HEADER_PREFIX} combat log, format {int(fmt)}"


def events_header(fmt: int = LOG_FORMAT) -> str:
    """The events file's header line (without the line break)."""
    return json.dumps({EVENTS_HEADER_KEY: "events", "format": int(fmt)})


def is_header(line: str) -> bool:
    """True for the header line of a raw log or an events file: not a message, skip it."""
    text = line.lstrip("﻿").strip()
    return text.startswith(RAW_HEADER_PREFIX) or text.startswith('{"' + EVENTS_HEADER_KEY + '"')


def file_format(path: str | Path) -> int:
    """The format a raw log or events file was written in: from its header, 1 without one."""
    try:
        with Path(path).open(encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith(RAW_HEADER_PREFIX):
                    m = _FORMAT_RX.search(line)
                    return int(m.group(1)) if m else 1
                if is_header(line):
                    try:
                        return int(json.loads(line).get("format") or 1)
                    except (ValueError, TypeError, AttributeError):
                        return 1
                return 1
    except OSError:
        log.debug("cannot read the format of %s", path, exc_info=True)
    return 1


class LogWriter:
    """Writes raw messages and parsed events to paired, time-stamped segment files.

    Args:
        log_dir: Directory for the files; a relative path is resolved against the project
            root (see :func:`mnmparse.config.project_path`).
        max_bytes: Start a new segment once the raw log is at least this big.
        break_s: Start a new segment when the gap between two messages exceeds this.
    """

    def __init__(self, log_dir: str, *, max_bytes: int = DEFAULT_MAX_BYTES, break_s: float = DEFAULT_BREAK_S) -> None:
        self.log_dir: Path = project_path(log_dir)
        self.max_bytes = max(1, int(max_bytes))
        self.break_s = max(0.0, float(break_s))
        self._raw: IO[str] | None = None
        self._events: IO[str] | None = None
        self.raw_path: Path | None = None
        self.events_path: Path | None = None
        self.segments: list[tuple[Path, Path]] = []  #: every (raw, events) pair opened so far
        self._raw_bytes = 0
        self._last_ts: float | None = None

    # ------------------------------------------------------------------
    def write_raw(self, msg: Message) -> None:
        """Append ``msg`` as ``[Wed Oct 01 17:11:03 2026] <text>`` to the current raw log.

        The timestamp is ``msg.first_seen`` (an epoch time from ``time.time()``); a falsy
        value falls back to now.  Line breaks inside the text are collapsed so each message
        stays on one line.  Segment rotation (idle gap, size) is decided here so that a
        message's raw line and its event always land in the same segment.
        """
        ts = float(getattr(msg, "first_seen", 0.0) or 0.0) or time.time()
        text = " ".join(str(msg.text).split())
        line = f"{eq_timestamp(ts)} {text}\n"
        self._maybe_rotate(ts)
        self._ensure_open(ts)
        assert self._raw is not None
        self._raw.write(line)
        self._raw.flush()
        self._raw_bytes += len(line.encode("utf-8"))
        self._last_ts = ts if self._last_ts is None else max(self._last_ts, ts)

    def write_event(self, ev: Event) -> None:
        """Append ``ev`` as one JSON object per line to the current events file."""
        ts = float(getattr(ev, "ts", 0.0) or 0.0) or time.time()
        data: dict[str, Any]
        if dataclasses.is_dataclass(ev) and not isinstance(ev, type):
            data = dataclasses.asdict(ev)
        else:
            data = dict(vars(ev))
        self._ensure_open(ts)
        assert self._events is not None
        self._events.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
        self._events.flush()

    def close(self) -> None:
        """Close any open files (safe to call more than once)."""
        for fh in (self._raw, self._events):
            if fh is not None:
                try:
                    fh.close()
                except OSError:  # pragma: no cover
                    log.debug("closing a log file failed", exc_info=True)
        self._raw = self._events = None

    def __enter__(self) -> LogWriter:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    def _maybe_rotate(self, ts: float) -> None:
        if self._raw is None:
            return
        if self._last_ts is not None and self.break_s > 0 and ts - self._last_ts > self.break_s:
            log.info("log segment ends: %.0f s without messages", ts - self._last_ts)
            self.close()
        elif self._raw_bytes >= self.max_bytes:
            log.info("log segment ends: %d bytes", self._raw_bytes)
            self.close()

    def _ensure_open(self, ts: float) -> None:
        if self._raw is not None and self._events is not None:
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime(SEGMENT_FORMAT, time.localtime(ts))
        self.raw_path = self.log_dir / f"combat_{stamp}.log"
        self.events_path = self.log_dir / f"events_{stamp}.jsonl"
        self._raw = self.raw_path.open("a", encoding="utf-8", newline="\n")
        self._events = self.events_path.open("a", encoding="utf-8", newline="\n")
        self._raw_bytes = self.raw_path.stat().st_size if self.raw_path.exists() else 0
        if self._raw_bytes == 0:  # a new file (not one appended to): name its format first
            header = raw_header() + "\n"
            self._raw.write(header)
            self._raw.flush()
            self._raw_bytes = len(header.encode("utf-8"))
        if self.events_path.stat().st_size == 0:
            self._events.write(events_header() + "\n")
            self._events.flush()
        self._last_ts = None
        self.segments.append((self.raw_path, self.events_path))
        log.info("Writing %s and %s", self.raw_path.name, self.events_path.name)
