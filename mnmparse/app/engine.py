"""The capture/OCR/parse pipeline as a background worker with Qt signals (APP_SPEC section 6).

:class:`Engine` is a :class:`QObject` that must be created in the GUI thread.
:meth:`Engine.start` spawns a daemon :class:`threading.Thread` that re-implements
the per-frame sequence of :class:`mnmparse.cli._RunSession`::

    source.latest() -> capture.crop_frame -> ocr.preprocess -> engine.read
    -> tracker.update -> (write_raw, parse_line, write_event, stats.add) per message

and reports through signals only; Qt queues them to the receivers in the GUI
thread (the default ``AutoConnection``), so the worker never touches widgets.

A restart must not log the chat lines still on screen a second time: the tracker goes on
over Stop/Start, its remembered rows are saved next to the logs (``tracker_state.json``) and
taken back by the next app if recent, lines a tracker without history found on screen
(``Message.backlog``) are neither logged nor counted, and as a backstop the first frame's
lines that repeat the end of the newest log are left out.  The party roster and the last zone
carry over too.

Safety posture (APP_SPEC section 1): this module only drives the read-only
capture and OCR modules and writes files under the project folder.  The game
window is located with :func:`mnmparse.capture.find_game_window` (read-only
Win32 lookup) and never activated, messaged or sent input.
"""

from __future__ import annotations

import dataclasses
import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Signal

from mnmparse.app.models import EncounterSnapshot, build_snapshot
from mnmparse.config import Config, project_path
from mnmparse.replay import BACKLOG_S
from mnmparse.session import SessionSnapshot, SessionStats
from mnmparse.vocab import GLOBAL as VOCAB

#: Chat lines never written to the log files: the viewer's own button-mashing noise
#: ("This ability is not available right now." was 4,600 of 44,000 lines on 2026-10-02).
NOT_LOGGED_RX = re.compile(r"^\W*this\s+abilit\w*\s+is\s+not\b", re.IGNORECASE)

#: The learned spellings (see :mod:`mnmparse.vocab`) live next to the logs.
VOCAB_FILE = "vocabulary.json"
VOCAB_SAVE_S = 600.0  #: save the learned spellings this often while capturing
ROSTER_SAVE_S = 5.0  #: save the party roster (party.json) at most this often after it changed
#: The warning banner: frames skipped because a panel covered the chat within this long...
OCCLUDED_RECENT_S = 10.0
#: ...or at least this share of the last messages unreadable (with at least GARBLE_MIN_MESSAGES).
GARBLE_SHARE = 0.35
GARBLE_WINDOW = 40
GARBLE_MIN_MESSAGES = 20
#: Unrecognized messages, incomplete abilities, and Dummy Fix guesses count as unreadable.
#: A tracker fragment can still parse completely (e.g. a hit missing its final period),
#: so the fragment flag alone does not mean combat information was lost.
UNREADABLE_KINDS = frozenset({"unknown", "ability_partial"})
DIAG_MESSAGE_LIMIT = 100
DIAG_FRAME_LIMIT = 3
DIAG_ROW_LIMIT = 100
DIAG_TEXT_LIMIT = 2000

#: What the tracker remembered when capture stopped (seq and text of the emitted rows), so a
#: restart recognises the chat lines still on screen instead of logging them again.
TRACKER_STATE_FILE = "tracker_state.json"
#: The last zone seen (written with the tracker state), carried into the next run.
SESSION_STATE_FILE = "session_state.json"
#: Both are taken back on start only when saved this recently (the tracker refuses older state).
STATE_MAX_AGE_S = 600.0
STATE_SAVE_MESSAGES = 50  #: also save them after this many messages (a killed app never stops)
#: Without a usable tracker state, the first frame's lines that repeat the newest log's last
#: lines in order are not logged again (replay.find_backlog), when that log ended this recently.
RESTART_TAIL_MAX_AGE_S = 600.0
RESTART_TAIL_BYTES = 64_000  #: how much of the end of that log is read
#: The lines first seen this soon after the first frame are its lines (replay.BACKLOG_S); they
#: wait for that check until a later line arrives (the chat scrolled), RESTART_HOLD_S at most.
RESTART_BLOCK_S = BACKLOG_S
RESTART_HOLD_S = 3.0
#: The chat window out of step with its newest line (scrolled up, covered, jumped) for this
#: long shows as "Chat scrolled up"; meanwhile the open fight does not time out (its lines are
#: still coming), for at most SCROLLED_BACK_HOLD_MAX_S.
SCROLLED_BACK_SHOW_S = 1.0
SCROLLED_BACK_HOLD_MAX_S = 120.0
#: A crop shorter than this many chat rows is warned about once: one group-heal block between
#: two frames can scroll a short window past unseen.
MIN_CROP_ROWS = 12
CROP_ROWS_FRAMES = 10  #: frames with at least 4 rows measured before the crop is judged
#: When the party roster learns a member, the closed fights of the current zone visit that
#: ended within this long are counted again (at most REBUILD_MAX_FIGHTS of them).
REBUILD_WINDOW_S = 900.0
REBUILD_MAX_FIGHTS = 30
_vocab_loaded_from: set[str] = set()


def _load_vocab_once(cfg: Config) -> None:
    """Load the saved spellings for ``cfg.log_dir`` once per process."""
    try:
        path = project_path(cfg.log_dir) / VOCAB_FILE
    except Exception:  # noqa: BLE001 - a bad log_dir must not stop the engine
        return
    key = str(path)
    if key in _vocab_loaded_from:
        return
    _vocab_loaded_from.add(key)
    if VOCAB.load(path):
        log.info("learned spellings loaded from %s", path)
        return
    # First run with this log folder: learn the spellings from the logs already recorded,
    # in the background so capture does not wait.
    threading.Thread(target=_seed_vocab, args=(path,), name="mnmparse-vocab-seed", daemon=True).start()


#: Lines read at most when seeding the spellings from old logs.
SEED_MAX_LINES = 250_000


def _seed_vocab(path: Path) -> None:
    """Feed every recorded ``combat_*.log`` line in ``path.parent`` to :data:`VOCAB`, then save."""
    from mnmparse.importer import LOG_LINE_RE
    from mnmparse.logwriter import is_header
    from mnmparse.parser import parse_line
    from mnmparse.vocab import observe_event

    started = time.monotonic()
    n = 0
    try:
        for log_file in sorted(path.parent.glob("combat_*.log")):
            with log_file.open(encoding="utf-8-sig", errors="replace") as fh:
                for raw in fh:
                    if is_header(raw):
                        continue
                    m = LOG_LINE_RE.match(raw.rstrip("\r\n"))
                    text = m.group("text") if m else raw.strip()
                    if text:
                        observe_event(VOCAB, parse_line(text, 0.0, ""))
                        n += 1
                    if n >= SEED_MAX_LINES:
                        break
            if n >= SEED_MAX_LINES:
                break
        if n:
            VOCAB.save(path)
            log.info("learned spellings from %d recorded lines in %.1f s", n, time.monotonic() - started)
    except Exception:  # noqa: BLE001 - seeding is a nicety; never disturb the app
        log.exception("seeding the learned spellings failed")


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` through a temporary file, so a crash never leaves half a file."""
    from mnmparse.storage import atomic_json
    atomic_json(path, data)


def _read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in ``path``, or ``None`` (missing, unreadable, not an object)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _capture_key(cfg: Config) -> list[Any]:
    """What the tracker's measured geometry (row pitch, text margin) depends on."""
    return [[int(v) for v in cfg.crop], float(cfg.ocr_scale), str(cfg.ocr_engine)]


def _previous_log_tail(log_dir: Path, now: float) -> list[str] | None:
    """The texts of the last lines of the newest ``combat_*.log`` in ``log_dir``, oldest first;
    ``None`` when there is none or its last line is older than RESTART_TAIL_MAX_AGE_S at ``now``."""
    from mnmparse.importer import LOG_LINE_RE, LOG_STAMP_FMT
    from mnmparse.logwriter import is_header
    from mnmparse.replay import TAIL_LINES

    try:
        logs = [(p.stat().st_mtime, p) for p in log_dir.glob("combat_*.log")]
        if not logs:
            return None
        newest = max(logs)[1]
        with newest.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - RESTART_TAIL_BYTES))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    lines = data.splitlines()
    if size > RESTART_TAIL_BYTES:
        lines = lines[1:]  # cut in the middle
    texts: list[str] = []
    last_ts: float | None = None
    for raw in lines:
        if is_header(raw):
            continue
        m = LOG_LINE_RE.match(raw.lstrip("﻿").rstrip())
        if m is None:
            continue
        try:
            last_ts = time.mktime(time.strptime(m.group("stamp"), LOG_STAMP_FMT))
        except (ValueError, OverflowError):
            continue
        texts.append(m.group("text"))
    if last_ts is None or now - last_ts > RESTART_TAIL_MAX_AGE_S:
        return None
    return texts[-TAIL_LINES:]


@dataclasses.dataclass
class _RestartTail:
    """The newest log's last lines, and the first frame's lines held back to compare with them."""

    tail: list[str]
    first_frame: float | None = None  #: ``now`` of the run's first frame
    held: list[Any] = dataclasses.field(default_factory=list)  #: Messages of the opening block


SESSION_MIN_INTERVAL_S = 0.5  #: session snapshots: at most this often while new entries arrive
SESSION_TICK_S = 5.0  #: ... and at least this often (elapsed time / rates keep moving)

if TYPE_CHECKING:
    import numpy as np

    from mnmparse.capture import FrameSource
    from mnmparse.grammar import Event
    from mnmparse.logwriter import LogWriter
    from mnmparse.ocr import OcrEngine, OcrLine
    from mnmparse.stats import Encounter, Stats
    from mnmparse.tracker import Message, Tracker

__all__ = ["Engine", "STATES"]

log = logging.getLogger(__name__)

STATES: tuple[str, ...] = ("stopped", "starting", "no_window", "running", "paused", "stopping")
"""Values carried by :attr:`Engine.state_changed`."""

SNAPSHOT_MIN_INTERVAL_S = 0.25
"""Snapshots are emitted at most this often (4 Hz) when messages arrive: plenty for a meter, and
each one costs ~13 ms of Python in the engine thread plus the redraws in the GUI thread."""
SNAPSHOT_TICK_S = 1.0
"""While an encounter is open a snapshot is emitted at least this often (duration ticks)."""
STATUS_INTERVAL_S = 1.0
"""Interval of the :attr:`Engine.status` dict."""
EXPIRE_INTERVAL_S = 1.0
"""How often ``stats.expire(now)`` runs (encounter timeout check)."""
WINDOW_RETRY_S = 2.0
"""Interval of the read-only window lookup while in the ``no_window`` state."""
FRAME_LOSS_S = 5.0
"""No frame for this long while running triggers a window lookup (lost window?)."""
FIRST_FRAME_TIMEOUT_S = 5.0
"""How long :meth:`Engine.grab_frame` waits for a frame from a temporary source."""
STOP_JOIN_TIMEOUT_S = 6.0
"""Default timeout for explicit non-GUI :meth:`Engine.wait_stopped` callers.

Covers one loop iteration (``1/fps`` plus an OCR pass), the tracker flush and
writer close, and ``WgcWindowSource.stop`` which itself joins the native
capture thread for up to 2 s.
"""


class Engine(QObject):
    """Pipeline worker thread plus the signals the UI subscribes to.

    Signals:
        message(Message, Event): every logged line (raw message and parsed event).
        snapshot(EncounterSnapshot): the open (or just-closed) encounter, at most
            every 100 ms and at least once per second while a fight is open.
        encounter_closed(EncounterSnapshot): an encounter closed (kill, timeout or reset).
        encounter_updated(EncounterSnapshot): a closed encounter counted again (same
            ``key``): the party roster learned a member who fought in it.  Not a new fight:
            nothing is copied and no sound plays.
        status(dict): every ~1 s: ``state fps ocr_ms frames messages occluded
            window_found lines scrolled_back``.
        state_changed(str): one of :data:`STATES`.
        error(str): a human-readable failure (the worker also logs it).
        notice(str): advice for the status bar (a crop too short, say); not a failure.

    The object must live in the GUI thread; only the worker thread emits.
    """

    message = Signal(object, object)
    snapshot = Signal(object)
    encounter_closed = Signal(object)
    encounter_updated = Signal(object)
    session = Signal(object)  #: SessionSnapshot (loot, coin, kills, deaths, CC) for the whole run
    status = Signal(dict)
    state_changed = Signal(str)
    error = Signal(str)
    notice = Signal(str)
    stopped = Signal()  #: cleanup completed; safe to start another worker or finish quitting

    def __init__(self, cfg: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg: Config = dataclasses.replace(cfg)
        self._lock = threading.RLock()  # guards the pipeline objects, counters and history
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: str = "stopped"
        self._paused = False
        self._window_found = False

        self._source: FrameSource | None = None
        self._ocr: OcrEngine | None = None
        self._active_ocr_settings = (cfg.ocr_engine, float(cfg.ocr_scale))
        self._tracker: Tracker | None = None
        self._stats: Stats | None = None
        self._stopped_stats: Stats | None = None  #: last capture's source events for ownership corrections
        self._writer: LogWriter | None = None
        self._history: list[EncounterSnapshot] = []
        self._history_sessions: dict[str, str] = {}
        self._session_encounter_keys: set[str] = set()  #: closes counted since the last session reset
        #: Carried over Stop/Start (``_finish`` drops ``_stats``): the party roster, the last
        #: zone, and the tracker with its key (_capture_key) and the time capture stopped.
        self._roster: Any | None = None
        self._last_zone: str = ""
        self._kept_tracker: tuple[list[Any], Tracker, float] | None = None
        self._restart: _RestartTail | None = None  #: the restart check of the first frame (see _filter_restart)
        self._backlog_dropped = 0  #: lines already on screen at start, not logged again (this run)
        self._since_state_save = 0  #: messages since the tracker state was saved
        self._scrolled_since: float | None = None  #: when the chat window got out of step (wall clock)
        self._scrolled_shown = False
        self._row_frames = 0  #: frames with 4+ rows seen (the crop is judged after CROP_ROWS_FRAMES)
        self._rows_warned = False
        self._rebuilt_version = -1  #: roster version the recent fights were last counted with
        self._rebuilt_members: set[str] = set()
        self._rebuild_all = False  #: the user changed the group by hand: re-count whatever changed
        _load_vocab_once(cfg)
        self._session_stats = SessionStats(
            cfg.player_name, include_personal=bool(getattr(cfg, "include_personal", False)), vocab=VOCAB
        )
        self._session_dirty = False
        self._recent_kinds: deque[str] = deque(maxlen=GARBLE_WINDOW)
        self._diagnostic_messages: deque[dict[str, Any]] = deque(maxlen=DIAG_MESSAGE_LIMIT)
        self._diagnostic_frames: deque[dict[str, Any]] = deque(maxlen=DIAG_FRAME_LIMIT)
        self._diagnostic_run_started: float | None = None
        self._occluded_seen = 0
        self._occluded_at = 0.0  #: monotonic time occlusion was last seen
        self._last_vocab_save = time.monotonic()
        self._last_session_at = 0.0
        from mnmparse.parser import NameCompleter

        self._names = NameCompleter()

        self._frames = 0
        self._messages = 0
        self._last_ocr_ms = 0.0
        self._last_lines = 0
        self._frame_times: deque[float] = deque(maxlen=64)
        self._snapshot_dirty = False
        self._last_snapshot_at = 0.0
        self._crop_warned = False

        self._archive: Any = None
        self._archive_attached = False
        self._archive_id = uuid.uuid4().hex
        self._archive_error: str = ""
        self._archive_saved_version: Any = None
        self._quality_active: dict[str, float] = {}
        self._timings: dict[str, deque[float]] = {}
        self._last_frame_at = 0.0
        self._frame_gap_ms = 0.0
        self._source_frame_age_ms: float | None = None
        self._source_frames_skipped = 0
        self._source_frames_repeated = 0
        self._source_frame_count: int | None = None
        self._source_dimensions: tuple[int, int] | None = None
        self._calibration_checked: Any = None
        # Creating an empty engine does not create files. Existing active sessions
        # recover automatically; explicit archive browsing is still available.
        if (project_path(cfg.log_dir) / "sessions.sqlite3").is_file():
            self._ensure_archive(recovering=True)
            if self._archive is not None:
                try:
                    active = self._archive.latest_active()
                    if active is not None:
                        self._restore_archive(active["id"])
                        self._last_zone = self._load_session_state(cfg)
                except Exception:
                    log.exception("recent session recovery failed")

        self._test_ocr: OcrEngine | None = None
        self._test_ocr_key: tuple[str, float] | None = None

    # ------------------------------------------------------------------
    # Public API (GUI thread)
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        """The current state string (see :data:`STATES`)."""
        return self._state

    @property
    def is_running(self) -> bool:
        """True while the worker thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def config(self) -> Config:
        """The configuration the engine currently holds (a copy)."""
        with self._lock:
            return dataclasses.replace(self._cfg)

    def start(self) -> None:
        """Spawn the worker thread; returns immediately (never blocks the GUI)."""
        if self._thread is not None:
            log.debug("Engine.start: already running")
            return
        self._stop_event.clear()
        with self._lock:
            self._frames = 0
            self._messages = 0
            self._last_ocr_ms = 0.0
            self._last_lines = 0
            self._frame_times.clear()
            self._snapshot_dirty = False
            self._last_snapshot_at = 0.0
            self._crop_warned = False
            self._backlog_dropped = 0
            self._since_state_save = 0
            self._scrolled_since = None
            self._scrolled_shown = False
            self._row_frames = 0
            self._rows_warned = False
            self._timings.clear()
            self._source_frame_count = None
            self._source_frames_skipped = self._source_frames_repeated = 0
            self._source_frame_age_ms = None
            self._source_dimensions = None
            self._calibration_checked = None
            self._last_frame_at = 0.0
        self._thread = threading.Thread(target=self._run, name="mnmparse-engine", daemon=True)
        self._set_state("starting")
        self._thread.start()
        log.info("Engine worker started")

    def stop(self) -> None:
        """Request cleanup without blocking the GUI or relinquishing a live worker."""
        thread = self._thread
        if thread is None:
            self._set_state("stopped")
            return
        if self._stop_event.is_set():
            return
        log.info("Engine stop requested")
        self._stop_event.set()
        self._set_state("stopping")

    def wait_stopped(self, timeout: float = STOP_JOIN_TIMEOUT_S) -> bool:
        """Wait from a non-GUI caller; timed-out workers remain owned and cannot restart."""
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        if thread.ident is None:
            return False
        thread.join(timeout=max(0.0, timeout))
        return not thread.is_alive()

    def set_paused(self, paused: bool) -> None:
        """Pause (skip frames, keep the encounter timeout ticking) or resume."""
        self._paused = bool(paused)
        with self._lock:
            self._quality_tick("capture_paused", self._paused, time.time())
        if self._state in ("running", "paused"):
            self._set_state("paused" if self._paused else "running")

    def reset_encounter(self) -> None:
        """Close the open encounter now; emits :attr:`encounter_closed` if there was one."""
        with self._lock:
            self._close_open_encounter("reset by the user")

    def _close_open_encounter(self, reason: str) -> EncounterSnapshot | None:
        """Close the open encounter now (lock held); returns its snapshot or ``None``.

        Uses the public ``Stats.expire`` with a time far enough in the future, so
        the encounter closes exactly as a timeout would, and announces it through
        :meth:`_emit_closed` (``encounter_closed`` plus a closed ``snapshot``).
        """
        stats = self._stats
        if stats is None:
            return None
        enc = stats.current()
        if enc is None:
            return None
        closed = stats.expire(enc.last_activity + stats.encounter_timeout_s + 1.0)
        if closed is None:
            return None
        log.info("encounter closed: %s", reason)
        return self._emit_closed(closed)

    def update_config(self, cfg: Config) -> None:
        """Replace the configuration.

        ``crop``, ``fps``, ``preprocess``, ``player_name`` and
        ``encounter_timeout_s`` apply live; the capture backend, window title,
        OCR engine/scale and log directory take effect on the next :meth:`start`.
        """
        with self._lock:
            self._cfg = dataclasses.replace(cfg)
            self._crop_warned = False
            self._row_frames = 0
            self._rows_warned = False
            if self._stats is not None:
                self._stats.encounter_timeout_s = float(cfg.encounter_timeout_s)
                self._stats.player_name = cfg.player_name
                self._stats.roster.player_name = cfg.player_name
            elif self._roster is not None:
                self._roster.player_name = cfg.player_name
            self._session_stats.player_name = cfg.player_name
            self._session_stats.include_personal = bool(getattr(cfg, "include_personal", False))
            if self._roster is not None:
                self._session_stats.set_roster(self._roster)
        log.info("Engine config updated")

    def history(self) -> list[EncounterSnapshot]:
        """Closed encounters so far, oldest first (newest last)."""
        with self._lock:
            return list(self._history)

    def current_snapshot(self) -> EncounterSnapshot | None:
        """Read the open fight when switching from archived history back to live."""
        with self._lock:
            stats = self._stats
            enc = stats.current() if stats is not None else None
            return build_snapshot(stats, enc, self._cfg.player_name, now=time.time()) if enc is not None else None

    def session_snapshot(self, *, full: bool = False) -> SessionSnapshot:
        """The current session (loot / coin / kills / deaths / CC) snapshot."""
        with self._lock:
            return self._timed_session_snapshot(detail_limit=None if full else 200)

    def ocr_diagnosis(self) -> dict[str, Any]:
        """Copy recent capture evidence without grabbing a frame or emitting signals.

        Safe to call from an export worker while live capture continues. Rows and messages
        are bounded samples, not a record of lines that were never captured.
        """
        with self._lock:
            saved = dataclasses.asdict(self._cfg)
            saved["crop"] = list(self._cfg.crop)
            active_cfg = self._capture_config(self._cfg) if self._ocr is not None else self._cfg
            effective = dataclasses.asdict(active_cfg)
            effective["crop"] = list(active_cfg.crop)
            active_engine, active_scale = self._active_ocr_settings
            tracker = self._tracker
            messages = list(self._diagnostic_messages)
            result = {
                "saved_settings": saved,
                "effective_settings": effective,
                "active_ocr": {
                    "available": self._ocr is not None,
                    "engine": active_engine if self._ocr is not None else None,
                    "scale": active_scale if self._ocr is not None else None,
                    "recognizer_language": getattr(self._ocr, "language", None),
                    "implementation": type(self._ocr).__name__ if self._ocr is not None else None,
                },
                "pending_ocr_settings": {
                    "engine": self._cfg.ocr_engine,
                    "scale": float(self._cfg.ocr_scale),
                    "requires_capture_restart": self._ocr is not None and (
                        self._cfg.ocr_engine != active_engine or float(self._cfg.ocr_scale) != active_scale
                    ),
                },
                "runtime": dict(self._status_payload(), paused=self._paused,
                                capture_thread_running=self.is_running,
                                run_started_at=self._diagnostic_run_started),
                "tracker": {
                    "available": tracker is not None,
                    "row_pitch_pixels": tracker.pitch if tracker is not None else None,
                    "line_height_pixels": tracker.line_height if tracker is not None else None,
                    "text_margin_pixels": tracker.margin if tracker is not None else None,
                    "typical_complete_line_characters": tracker.complete_len if tracker is not None else None,
                    "pending_rows": len(tracker.pending) if tracker is not None else 0,
                    "waiting_wrapped_line": tracker.held.text[:DIAG_TEXT_LIMIT]
                    if tracker is not None and tracker.held is not None else None,
                },
                "recent_messages": messages,
                "missed_messages": [message for message in messages if message["unreadable"]],
                "recent_ocr_frames": list(self._diagnostic_frames),
                "sample_limits": {
                    "messages": DIAG_MESSAGE_LIMIT, "ocr_frames": DIAG_FRAME_LIMIT,
                    "rows_per_frame": DIAG_ROW_LIMIT, "characters_per_text": DIAG_TEXT_LIMIT,
                },
                "limitations": [
                    "Missed messages are recent captured messages that were unrecognized, incomplete, or estimated.",
                    "Lines that were never captured, scrolled away between frames, or arrived while chat was covered cannot be recovered.",
                ],
            }
            return copy.deepcopy(result)

    def reset_session(self) -> bool:
        """Start the session counters over (keeps the encounter history)."""
        with self._lock:
            self._close_open_encounter("session reset")
            if not self._save_session_archive(ended=time.time(), force=True):
                self.notice.emit("Session reset could not save the old session. Check the log folder and try again.")
                return False
            self._archive_id = uuid.uuid4().hex
            self._archive_saved_version = None
            self._archive_attached = False
            cfg = self._cfg
            self._session_encounter_keys.clear()
            self._session_stats = SessionStats(
                cfg.player_name, include_personal=bool(getattr(cfg, "include_personal", False)), vocab=VOCAB,
                roster=self._roster,
            )
            self._quality_active.clear()
            self._ensure_archive()
            self._save_session_archive(force=True)
            self._refresh_ongoing_quality(time.time())
            snap = self._timed_session_snapshot(detail_limit=200)
        snap.emitted_at = time.monotonic()
        self.session.emit(snap)
        log.info("session counters reset")
        return True

    def _maybe_emit_session(self, now_mono: float) -> None:
        """Emit the session snapshot: <= 2 Hz when new entries arrived, and every 5 s regardless."""
        with self._lock:
            elapsed = now_mono - self._last_session_at
            if not ((self._session_dirty and elapsed >= SESSION_MIN_INTERVAL_S) or elapsed >= SESSION_TICK_S):
                return
            self._refresh_ongoing_quality(time.time())
            snap = self._timed_session_snapshot(detail_limit=200)
            self._session_dirty = False
            self._last_session_at = now_mono
            self._save_session_archive()
        snap.emitted_at = time.monotonic()
        self.session.emit(snap)
        with self._lock:
            self._maybe_save_roster(now_mono)
        if now_mono - self._last_vocab_save >= VOCAB_SAVE_S:
            self._last_vocab_save = now_mono
            self._save_vocab()

    def _ensure_archive(self, *, recovering: bool = False) -> None:
        if self._archive_error:
            return
        from mnmparse.session_archive import SessionArchive
        original_details = None
        try:
            if self._archive is None:
                self._archive = SessionArchive(project_path(self._cfg.log_dir) / "sessions.sqlite3")
            if not self._archive_attached and not recovering:
                from mnmparse.session_archive import DETAIL_FIELDS
                original_details = {field: getattr(self._session_stats, field) for field in DETAIL_FIELDS}
                with self._archive.transaction():
                    self._archive.attach_details(self._session_stats, self._archive_id)
                    self._archive.save_session(self._archive_id, self._session_stats)
                self._archive_attached = True
        except Exception as exc:
            if original_details is not None:
                for field, values in original_details.items():
                    setattr(self._session_stats, field, values)
            self._archive = None
            self._archive_error = str(exc)
            log.exception("session archive unavailable; keeping history in memory")
            self.notice.emit("Session recovery is unavailable; history will stay in memory. Check the log folder.")

    def _save_session_archive(self, *, ended: float | None = None, force: bool = False) -> bool:
        self._refresh_ongoing_quality(ended if ended is not None else time.time())
        self._ensure_archive()
        if self._archive is None:
            return False
        roster = self._session_stats.roster
        dependency = (self._session_stats.version, getattr(roster, "version", 0), ended)
        if not force and self._archive_saved_version == dependency:
            return True
        try:
            self._archive.save_session(self._archive_id, self._session_stats, ended=ended)
            self._archive_saved_version = dependency
            return True
        except Exception as exc:
            self._archive_error = str(exc)
            log.exception("session recovery checkpoint failed")
            return False

    def archived_sessions(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Raw local metadata; Casual UI must use date, own character and zone labels."""
        with self._lock:
            self._ensure_archive()
            return self._archive.sessions(limit=limit, offset=offset) if self._archive is not None else []

    def archive_import(self, result: Any, *, cancelled: Any = None) -> str:
        """Archive a parsed import in its background worker, preserving correction evidence."""
        session = getattr(result, "session_stats", None)
        if session is None:
            raise ValueError("import does not include raw session state")
        with self._lock:
            self._ensure_archive()
            archive = self._archive
        if archive is None:
            raise OSError("local session archive is unavailable")
        session_id = uuid.uuid4().hex
        stored = copy.copy(session)  # original raw import remains intact if archiving fails
        try:
            archive.attach_details(stored, session_id, cancelled=cancelled)
            snapshots = {snap.key: snap for snap in result.encounters}
            for enc in result.stats.history:
                if cancelled is not None and cancelled():
                    raise InterruptedError("import archive cancelled")
                key = f"{enc.start:.3f}"
                if key in snapshots:
                    archive.save_encounter(session_id, result.stats, enc, snapshots[key])
            ended = result.ended if result.ended is not None else session.started
            # The browser sees the import only after all raw evidence was saved.
            if cancelled is not None and cancelled():
                raise InterruptedError("import archive cancelled")
            archive.save_session(session_id, stored, ended=ended)
        except Exception:
            archive.discard(session_id)
            log.exception("import archive incomplete; keeping original import data")
            raise
        return session_id

    def flush_preferences(self) -> bool:
        """Flush capture preferences and the recovery checkpoint before a local backup."""
        with self._lock:
            if self.is_running:
                return False
            try:
                if self._roster is None:
                    from mnmparse.party import PartyRoster
                    self._roster = PartyRoster(self._cfg.player_name)
                    self._roster.load(self._party_path(self._cfg))
                    self._session_stats.set_roster(self._roster)
                if not self._roster.save(self._party_path(self._cfg)):
                    return False
                VOCAB.save(project_path(self._cfg.log_dir) / VOCAB_FILE)
                return self._save_session_archive(force=True)
            except OSError:
                log.exception("flushing preferences failed")
                return False

    def reload_preferences(self) -> bool:
        """Replace restored preferences while stopped; retained old counts are not merged."""
        from mnmparse.party import PartyRoster
        with self._lock:
            if self.is_running:
                return False
            data = _read_json(project_path(self._cfg.log_dir) / VOCAB_FILE)
            roster = PartyRoster(self._cfg.player_name)
            if data is None or not roster.load(self._party_path(self._cfg)):
                return False
            VOCAB.replace_dict(data)
            self._roster = roster
            if self._stopped_stats is not None:
                self._stopped_stats.roster = roster
            self._session_stats.set_roster(roster)
            self._session_dirty = True
            return True

    def archived_session(self, session_id: str, *, detail_limit: int | None = 200, full: bool = False) -> SessionSnapshot:
        with self._lock:
            self._ensure_archive()
            if self._archive is None:
                raise KeyError(session_id)
            if not full and detail_limit is not None and detail_limit <= 200:
                preview = self._archive.preview(session_id, detail_limit=detail_limit)
                if preview is not None:
                    return preview
            stats = self._archive.restore(session_id, vocab=VOCAB)
            meta = self._archive.metadata(session_id)
            ended = meta["ended"] if meta["ended"] is not None else meta["updated"]
            return stats.snapshot(now=ended, detail_limit=None if full else detail_limit)

    def archived_encounter(self, key: str, *, session_id: str | None = None) -> tuple[Stats, Encounter]:
        with self._lock:
            self._ensure_archive()
            if self._archive is None:
                raise KeyError(key)
            return self._archive.encounter(key, session_id=session_id, vocab=VOCAB)

    def archived_encounters(self, session_id: str, *, limit: int | None = None, offset: int = 0,
                            full: bool = False) -> list[EncounterSnapshot]:
        with self._lock:
            self._ensure_archive()
            if self._archive is None:
                return []
            count = None if full else max(1, int(limit if limit is not None else getattr(self._cfg, "history_recent_fights", 100)))
            keys = self._archive.encounter_keys(session_id, limit=count, offset=offset, newest=not full)
            if not full:
                keys.reverse()
            return [build_snapshot(stats, enc, stats.player_name)
                    for stats, enc in (self._archive.encounter(key, session_id=session_id, vocab=VOCAB)
                                       for key in keys)]

    def _restore_archive(self, session_id: str) -> None:
        restored = self._archive.restore(session_id, vocab=VOCAB)
        quality = restored.capture_quality
        observed = quality.pop("ongoing_intervals", [])
        if observed:
            quality["intervals"] = (quality.get("intervals", []) + observed)[-200:]
            quality["interruption_count"] = quality.get("interruption_count", 0) + len(observed)
            quality["interruption_seconds"] = quality.get("interruption_seconds", 0.0) + sum(
                max(0.0, item["end"] - item["start"]) for item in observed)
            restored._version += 1
        self._archive_id = session_id
        self._session_stats = restored
        self._archive_attached = True
        self._roster = restored.roster
        keys = self._archive.encounter_keys(session_id, limit=max(1, int(getattr(self._cfg, "history_recent_fights", 100))), newest=True)
        keys.reverse()
        self._session_encounter_keys = set(keys)
        self._history = []
        self._history_sessions = {}
        raw = None
        for key in keys[-max(1, int(getattr(self._cfg, "history_recent_fights", 100))) :]:
            stats, enc = self._archive.encounter(key, session_id=session_id, vocab=VOCAB)
            self._history.append(build_snapshot(stats, enc, stats.player_name))
            self._history_sessions[key] = session_id
            if raw is None:
                raw = stats
                raw.history = []
            raw.history.append(enc)
        self._stopped_stats = raw
        if raw is not None:
            raw._restored_archive = True
        if restored._zones:
            self._last_zone = restored._zones[-1]
        self._archive_saved_version = None
        self._session_dirty = True

    def restore_session(self, session_id: str) -> bool:
        """Recover a local session while capture is stopped; a live worker is never replaced."""
        with self._lock:
            if self.is_running:
                return False
            self._ensure_archive()
            if self._archive is None:
                return False
            try:
                if not self._save_session_archive(ended=time.time(), force=True):
                    return False
                self._restore_archive(session_id)
                self._save_session_archive(force=True)
            except (KeyError, ValueError, OSError):
                log.exception("session restore failed")
                return False
            snap = self._session_stats.snapshot(detail_limit=200)
            snap.emitted_at = time.monotonic()
        self.session.emit(snap)
        return True

    def correct_archived_encounter(self, key: str, *, session_id: str | None = None,
                                   name: str | None = None, in_group: bool | None = None,
                                   pet: str | None = None, owner: str | None = None) -> EncounterSnapshot:
        """Rebuild an older raw fight on demand, without loading the entire history."""
        notify_history = False
        session_snap = None
        with self._lock:
            session_id = session_id or self._archive_id
            stats, enc = self.archived_encounter(key, session_id=session_id)
            old = build_snapshot(stats, enc, stats.player_name)
            if name is not None:
                stats.roster.set_manual(name, in_group)
            if pet is not None:
                stats.roster.set_pet_owner(pet, owner)
            new = build_snapshot(stats, enc, stats.player_name)
            self._archive.save_encounter(session_id, stats, enc, new)
            if session_id == self._archive_id:
                self._revise_session_encounter(old, new)
                self._save_session_archive(force=True)
                session_snap = self._timed_session_snapshot(detail_limit=200)
            else:
                session = self._archive.restore(session_id, vocab=VOCAB)
                session.revise_encounter(old.duration if old.ours else None, new.duration if new.ours else None)
                meta = self._archive.metadata(session_id)
                self._archive.save_session(session_id, session, ended=meta["ended"])
            for index, snap in enumerate(self._history):
                if snap.key == key and self._history_sessions.get(key) == session_id:
                    self._history[index] = new
                    notify_history = True
        new.emitted_at = time.monotonic()
        if notify_history or session_id == self._archive_id:
            self.encounter_updated.emit(new)
        if session_snap is not None:
            session_snap.emitted_at = time.monotonic()
            self.session.emit(session_snap)
        return new

    def _record_timing(self, stage: str, started: float) -> None:
        self._timings.setdefault(stage, deque(maxlen=256)).append((time.perf_counter() - started) * 1000.0)

    def note_ui_delivery(self, milliseconds: float) -> None:
        """GUI integration may report signal-to-render latency without holding raw frames."""
        with self._lock:
            self._timings.setdefault("ui_delivery", deque(maxlen=256)).append(max(0.0, float(milliseconds)))

    def _timed_session_snapshot(self, *, detail_limit: int | None = None) -> SessionSnapshot:
        started = time.perf_counter()
        snap = self._session_stats.snapshot(detail_limit=detail_limit)
        self._record_timing("session_snapshot", started)
        return snap

    def _quality_tick(self, kind: str, active: bool, now: float) -> None:
        if active:
            self._quality_active.setdefault(kind, now)
            return
        start = self._quality_active.pop(kind, None)
        if start is None:
            return
        quality = self._session_stats.capture_quality
        ongoing = [item for item in quality.get("ongoing_intervals", []) if item["kind"] != kind]
        if ongoing:
            quality["ongoing_intervals"] = ongoing
        else:
            quality.pop("ongoing_intervals", None)
        quality.setdefault("intervals", []).append(dict(kind=kind, start=start, end=max(start, now)))
        quality["intervals"] = quality["intervals"][-200:]
        quality["interruption_count"] = quality.get("interruption_count", 0) + 1
        quality["interruption_seconds"] = quality.get("interruption_seconds", 0.0) + max(0.0, now - start)
        self._session_stats._version += 1
        self._session_dirty = True

    def _refresh_ongoing_quality(self, now: float) -> None:
        """Persist only the period actually observed, including an interrupted shutdown."""
        quality = self._session_stats.capture_quality
        intervals = [dict(kind=kind, start=start, end=max(start, float(int(now))))
                     for kind, start in self._quality_active.items()]
        if intervals != quality.get("ongoing_intervals", []):
            if intervals:
                quality["ongoing_intervals"] = intervals
            else:
                quality.pop("ongoing_intervals", None)
            self._session_stats._version += 1
            self._session_dirty = True

    def _quality_for(self, start: float, end: float) -> list[dict[str, Any]]:
        intervals = list(self._session_stats.capture_quality.get("intervals", []))
        intervals.extend(dict(kind=kind, start=since, end=end) for kind, since in self._quality_active.items())
        return [dict(kind=item["kind"], start=max(start, item["start"]), end=min(end, item["end"]))
                for item in intervals if item["end"] >= start and item["start"] <= end]

    @staticmethod
    def _party_path(cfg: Config) -> Path:
        from mnmparse.party import PARTY_FILE

        return project_path(cfg.log_dir) / PARTY_FILE

    def _maybe_save_roster(self, now_mono: float, *, force: bool = False) -> None:
        """Save the party roster when it changed (at most every ROSTER_SAVE_S)."""
        stats = self._stats
        if stats is None:
            return
        roster = stats.roster
        if roster.version == getattr(self, "_roster_saved", -1):
            return
        if not force and now_mono - getattr(self, "_roster_saved_at", 0.0) < ROSTER_SAVE_S:
            return
        self._roster_saved_at = now_mono
        if roster.save(self._party_path(self.config)):
            self._roster_saved = roster.version

    # -- restart: tracker state and last zone ------------------------------------------------
    def _start_tracker(self, cfg: Config) -> tuple[Tracker, bool]:
        """The tracker for a new run, and whether it is the last run's (kept in this process).

        The last run's tracker goes on when capture stopped less than STATE_MAX_AGE_S ago and
        the crop and OCR settings are the same: it recognises the lines still on screen.
        Otherwise a new tracker takes the remembered rows (that tracker's, or the saved
        tracker_state.json) through :meth:`Tracker.import_state`, which refuses stale state;
        the measured geometry only when the crop and OCR settings are the same.  Without any
        state the first frame's lines are flagged as backlog and not logged.
        """
        from mnmparse.tracker import Tracker

        key = _capture_key(cfg)
        now = time.time()
        kept = self._kept_tracker
        self._kept_tracker = None
        state: dict[str, Any] | None = None
        if kept is not None:
            kept_key, tracker, stopped = kept
            if kept_key == key and 0.0 <= now - stopped <= STATE_MAX_AGE_S:
                log.info("tracker of the last run goes on (%d remembered rows)", len(tracker.history))
                return tracker, True
            state = dict(tracker.export_state(stopped), capture=kept_key)
        if state is None:
            state = _read_json(project_path(cfg.log_dir) / TRACKER_STATE_FILE)
        tracker = Tracker()
        if state is not None:
            if state.get("capture") != key:
                state = dict(state, geometry={})  # measured with another crop or OCR scale
            tracker.import_state(state, now=now, max_age_s=STATE_MAX_AGE_S)
        return tracker, False

    def _install_stats(self, cfg: Config, saved_zone: str = "") -> Stats:
        """A new Stats for a run, with the zone and the party roster carried over (lock held).

        Stop/Start keeps the last known zone (a restart soon after: the saved one) until the
        next zone line, and the party roster (a restart: party.json, each member if recent).
        The session takes its party from the same roster."""
        from mnmparse.stats import Stats

        stats = Stats(cfg.encounter_timeout_s, vocab=VOCAB, player_name=cfg.player_name)
        zone = self._last_zone or saved_zone
        if zone:
            stats.zone_changes.append((0.0, zone))
        if self._roster is not None:
            stats.roster = self._roster
            stats.roster.player_name = cfg.player_name
        else:
            stats.roster.load(self._party_path(cfg))
        self._stats = stats
        self._roster = stats.roster
        self._session_stats.set_roster(stats.roster)
        self._roster_saved = stats.roster.version
        self._rebuilt_version = stats.roster.version
        self._rebuilt_members = stats.roster.members()
        self._rebuild_all = False
        return stats

    def _load_session_state(self, cfg: Config) -> str:
        """The zone saved by the last run, when it was saved within STATE_MAX_AGE_S (else ``""``)."""
        data = _read_json(project_path(cfg.log_dir) / SESSION_STATE_FILE)
        if data is None:
            return ""
        try:
            age = time.time() - float(data.get("saved", 0.0))
        except (TypeError, ValueError):
            return ""
        zone = data.get("zone")
        if not isinstance(zone, str) or not zone or not -5.0 <= age <= STATE_MAX_AGE_S:
            return ""
        log.info("last zone %r taken over from the last run (saved %.0f s ago)", zone, age)
        return zone

    def _current_zone(self) -> str:
        """The zone in effect (the last zone line, or the one carried over); lock held."""
        stats = self._stats
        if stats is not None and stats.zone_changes:
            return str(stats.zone_changes[-1][1])
        return self._last_zone

    def _save_state(self, cfg: Config, tracker: Tracker, zone: str) -> None:
        """Save the tracker state and the last zone next to the logs (worker thread)."""
        cfg = self._capture_config(cfg)
        now = time.time()
        folder = project_path(cfg.log_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(folder / TRACKER_STATE_FILE, dict(tracker.export_state(now), capture=_capture_key(cfg)))
            _write_json_atomic(folder / SESSION_STATE_FILE, {"version": 1, "saved": now, "zone": zone})
        except OSError as exc:
            log.warning("could not save the tracker state: %s", exc)
        self._since_state_save = 0

    def _capture_config(self, cfg: Config) -> Config:
        """Use the OCR settings selected at start until the running engine is replaced."""
        engine, scale = self._active_ocr_settings
        return dataclasses.replace(cfg, ocr_engine=engine, ocr_scale=scale)

    def set_group_override(self, name: str, in_group: bool | None) -> None:
        """Count ``name`` in or out of the group by hand (``None``: follow the chat again).

        Applies to the live fight at once (its next snapshot), to the recent closed fights of
        the zone visit (counted again, see :meth:`_maybe_rebuild_recent`) and to later fights,
        and is remembered across runs (party.json next to the logs)."""
        from mnmparse.party import PartyRoster

        with self._lock:
            stats = self._stats
            if stats is not None:
                stats.roster.set_manual(name, in_group)
                self._maybe_save_roster(time.monotonic(), force=True)
                self._snapshot_dirty = True
                self._rebuild_all = True
                return
            cfg = self._cfg
            roster = self._roster
            if roster is not None:
                # Not capturing, but the roster of the last run is kept for the next one.
                roster.set_manual(name, in_group)
                roster.save(self._party_path(cfg))
                return
        roster = PartyRoster(cfg.player_name)  # not capturing yet: change the saved roster directly
        path = self._party_path(cfg)
        roster.load(path)
        roster.set_manual(name, in_group)
        roster.save(path)

    def _save_vocab(self) -> None:
        try:
            VOCAB.save(project_path(self.config.log_dir) / VOCAB_FILE)
        except OSError as exc:
            log.warning("could not save the learned spellings: %s", exc)

    def set_pet_owner(self, pet: str, owner: str | None) -> None:
        """Persist ownership and immediately rebuild captured fights that contain this pet."""
        from mnmparse.party import PartyRoster

        with self._lock:
            roster = self._stats.roster if self._stats is not None else self._roster
            if roster is None:
                roster = PartyRoster(self._cfg.player_name)
                roster.load(self._party_path(self._cfg))
                self._roster = roster
            roster.set_pet_owner(pet, owner)
            path = self._party_path(self._cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            roster.save(path)
            self._snapshot_dirty = True
            self._rebuild_all = True
            updated: list[EncounterSnapshot] = []
            for stats in (self._stopped_stats, self._stats):
                if stats is None:
                    continue
                stats.roster.set_pet_owner(pet, owner)
                positions = {snap.key: index for index, snap in enumerate(self._history)}
                for enc in stats.history:
                    index = positions.get(f"{enc.start:.3f}")
                    if index is None or pet not in stats.canonical_map(enc).values():
                        continue
                    old = self._history[index]
                    context = stats
                    source = enc
                    if getattr(stats, "_restored_archive", False) and self._archive is not None:
                        context, source = self._archive.encounter(old.key, session_id=self._history_sessions.get(old.key), vocab=VOCAB)
                        context.roster.set_pet_owner(pet, owner)
                    snap = build_snapshot(context, source, self._cfg.player_name)
                    if dataclasses.replace(snap, emitted_at=0.0) != dataclasses.replace(old, emitted_at=0.0):
                        self._history[index] = snap
                        self._revise_session_encounter(old, snap)
                        if self._archive is not None:
                            self._archive.save_encounter(self._history_sessions.get(snap.key, self._archive_id), context, source, snap)
                        updated.append(snap)
            current = self._stats.current() if self._stats is not None else None
            live = build_snapshot(self._stats, current, self._cfg.player_name, now=time.time()) if current is not None else None
        for snap in updated:
            snap.emitted_at = time.monotonic()
            self.encounter_updated.emit(snap)
        if live is not None:
            live.emitted_at = time.monotonic()
            self.snapshot.emit(live)

    def pet_owners(self) -> dict[str, str]:
        """Saved ownership choices for imported logs as well as live capture."""
        from mnmparse.party import PartyRoster

        with self._lock:
            roster = self._stats.roster if self._stats is not None else self._roster
            if roster is None:
                roster = PartyRoster(self._cfg.player_name)
                roster.load(self._party_path(self._cfg))
            return roster.pet_owners()

    def grab_frame(self) -> np.ndarray | None:
        """Return one BGR frame of the game window, or ``None``.

        Uses the live source while running; otherwise starts a temporary source,
        waits for the first frame and stops it again.  Failures are logged and
        reported through :attr:`error`; nothing is raised.
        """
        from mnmparse.capture import CaptureError, make_source

        with self._lock:
            source = self._source if self.is_running else None
        if source is not None:
            frame = source.latest()
            if frame is None:
                frame = self._wait_for_frame(source, 2.0)
            return frame

        cfg = self.config
        try:
            temp = make_source(cfg)
            temp.start()
        except CaptureError as exc:
            log.warning("grab_frame: %s", exc)
            self.error.emit(str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 - surface anything from native code
            log.exception("grab_frame: could not start a temporary source")
            self.error.emit(f"Could not start capture: {exc}")
            return None
        try:
            frame = self._wait_for_frame(temp, FIRST_FRAME_TIMEOUT_S)
        finally:
            temp.stop()
        if frame is None:
            self.error.emit(
                f"The game window was found but no frame arrived within {FIRST_FRAME_TIMEOUT_S:.0f} s."
            )
        return frame

    def test_ocr(self, frame: np.ndarray, crop: tuple[int, int, int, int], cfg: Config) -> list[OcrLine]:
        """Crop, preprocess and OCR ``frame`` once with the settings in ``cfg``.

        A private OCR engine (cached by engine name and scale) is used so the
        GUI never competes with the worker's engine.  Returns an empty list on
        failure (logged and reported through :attr:`error`).
        """
        from mnmparse.capture import crop_frame
        from mnmparse.ocr import make_engine, preprocess

        try:
            key = (cfg.ocr_engine, float(cfg.ocr_scale))
            if self._test_ocr is None or self._test_ocr_key != key:
                self._test_ocr = make_engine(cfg)
                self._test_ocr_key = key
            img = preprocess(crop_frame(frame, tuple(crop)), cfg.preprocess, cfg.ocr_scale)
            return self._test_ocr.read(img)
        except Exception as exc:  # noqa: BLE001
            log.exception("test_ocr failed")
            self.error.emit(f"OCR test failed: {exc}")
            return []

    # ------------------------------------------------------------------
    # Worker thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Thread body: build the pipeline, then capture until stopped."""
        from mnmparse.capture import CaptureError
        from mnmparse.ocr import make_engine

        try:
            self._set_state("starting")
            cfg = self.config
            try:
                ocr = make_engine(cfg)
            except Exception as exc:  # noqa: BLE001 - "engine unavailable"
                log.exception("OCR engine %r unavailable", cfg.ocr_engine)
                self.error.emit(f"OCR engine '{cfg.ocr_engine}' unavailable: {exc}")
                return
            self._prepare_run(cfg, ocr)
            while not self._stop_event.is_set():
                source = self._open_source()
                if source is None:
                    break
                self._capture_loop(source)
                if not self._stop_event.is_set():
                    # The game window went away: release the source and look for it again.
                    # On a stop request the source is closed by _finish() AFTER the tracker
                    # flush and writer close, so the held lines reach the log files first
                    # (WgcWindowSource.stop may take up to 2 s).
                    self._close_source()
        except CaptureError as exc:
            log.error("Capture failed: %s", exc)
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - never let an exception escape the thread
            log.exception("Engine worker crashed")
            self.error.emit(f"Capture stopped: {exc}")
        finally:
            try:
                self._finish()
            finally:
                # Nothing after this handoff touches pipeline state. Keep ownership
                # throughout cleanup, including a recognizer/source that stops slowly.
                with self._lock:
                    if self._thread is threading.current_thread():
                        self._thread = None
                self.stopped.emit()

    def _prepare_run(self, cfg: Config, ocr: OcrEngine) -> None:
        """Build the pipeline of a run: tracker, restart check, Stats, log writer."""
        from mnmparse.logwriter import LogWriter

        tracker, kept = self._start_tracker(cfg)
        # Unless the tracker of the last run in this process goes on (its history is
        # complete), the first frame's lines are also checked against the end of the newest
        # log: a saved state can be up to STATE_SAVE_MESSAGES lines behind after an unclean exit.
        tail = None if kept else _previous_log_tail(project_path(cfg.log_dir), time.time())
        saved_zone = self._load_session_state(cfg)
        with self._lock:
            self._ocr = ocr
            self._active_ocr_settings = (cfg.ocr_engine, float(cfg.ocr_scale))
            self._tracker = tracker
            self._recent_kinds.clear()
            self._diagnostic_messages.clear()
            self._diagnostic_frames.clear()
            self._diagnostic_run_started = time.time()
            self._restart = _RestartTail(tail) if tail else None
            self._install_stats(cfg, saved_zone)
            self._ensure_archive()
            if self._archive is not None:
                self._archive.prune(int(getattr(cfg, "history_retention_days", 30)), protected=self._archive_id)
            self._writer = LogWriter(
                cfg.log_dir,
                max_bytes=int(float(getattr(cfg, "log_max_mb", 5.0)) * 1_000_000),
                break_s=float(getattr(cfg, "log_break_minutes", 60.0)) * 60.0,
            )

    def _open_source(self) -> FrameSource | None:
        """Start a frame source; wait in ``no_window`` until the game appears or we stop."""
        from mnmparse.capture import WindowNotFoundError, find_game_window, make_source

        while not self._stop_event.is_set():
            cfg = self.config
            try:
                source = make_source(cfg)
                source.start()
            except WindowNotFoundError as exc:
                log.info("%s; retrying every %.0f s", exc, WINDOW_RETRY_S)
            else:
                with self._lock:
                    self._source = source
                    self._window_found = True
                return source
            self._window_found = False
            self._set_state("no_window")
            # Poll the (read-only) window lookup every WINDOW_RETRY_S, keeping the
            # status signal ticking once per second meanwhile.
            next_lookup = time.monotonic() + WINDOW_RETRY_S
            while not self._stop_event.wait(STATUS_INTERVAL_S):
                self._emit_status()
                if time.monotonic() >= next_lookup:
                    if find_game_window(cfg.window_title) is not None:
                        break
                    next_lookup = time.monotonic() + WINDOW_RETRY_S
        return None

    def _close_source(self) -> None:
        with self._lock:
            source, self._source = self._source, None
        if source is not None:
            try:
                source.stop()
                wait = getattr(source, "wait_stopped", None)
                if callable(wait):
                    while not wait(.5):
                        # Cleanup happens only in the capture worker. Keep ownership
                        # so another source cannot overlap a slow native shutdown.
                        pass
            except Exception:  # noqa: BLE001
                log.debug("source.stop() raised", exc_info=True)

    def _capture_loop(self, source: FrameSource) -> None:
        """Frame loop; returns when stopped or when the game window disappears."""
        self._set_state("paused" if self._paused else "running")
        self._source_frame_count = None
        now = time.monotonic()
        last_frame_at = now
        last_lookup = now
        next_expire = now + EXPIRE_INTERVAL_S
        next_status = now + STATUS_INTERVAL_S
        self._emit_status()

        while not self._stop_event.is_set():
            t0 = time.monotonic()
            cfg = self.config
            if not self._paused:
                # A source that can cut the crop out of each frame copies a few MB, not the
                # whole 4K window (capture.WgcWindowSource.latest_region).
                region = getattr(source, "latest_region", None)
                try:
                    frame = region(tuple(cfg.crop)) if callable(region) else source.latest()
                except ValueError as exc:  # the crop does not fit the frame
                    frame = None
                    last_frame_at = t0
                    self._warn_crop(cfg, exc)
                with self._lock:
                    self._check_source_dimensions(source, cfg, frame if not callable(region) else None)
                if frame is not None:
                    last_frame_at = t0
                    with self._lock:
                        stamp = getattr(source, "latest_frame_time", None)
                        self._source_frame_age_ms = max(0.0, (time.monotonic() - stamp) * 1000.0) if stamp else None
                        count = getattr(source, "frame_count", None)
                        if isinstance(count, int):
                            if self._source_frame_count is not None:
                                self._source_frames_skipped += max(0, count - self._source_frame_count - 1)
                                self._source_frames_repeated += int(count == self._source_frame_count)
                            self._source_frame_count = count
                        self._quality_tick("capture_unavailable", False, time.time())
                    self._process_frame(frame, cfg, cropped=callable(region))
                elif self._window_lost(source, t0 - last_frame_at, t0 - last_lookup, cfg):
                    return
                elif t0 - last_frame_at > FRAME_LOSS_S:
                    last_lookup = t0
                    with self._lock:
                        self._quality_tick("capture_unavailable", True, time.time() - (t0 - last_frame_at))

            now = time.monotonic()
            if now >= next_expire:
                next_expire = now + EXPIRE_INTERVAL_S
                self._expire(time.time())
            self._maybe_emit_snapshot(now)
            self._maybe_rebuild_recent()
            self._maybe_emit_session(now)
            if now >= next_status:
                next_status = now + STATUS_INTERVAL_S
                self._emit_status()

            delay = (1.0 / max(cfg.fps, 0.1)) - (time.monotonic() - t0)
            if delay > 0:
                self._stop_event.wait(delay)

    def _window_lost(self, source: FrameSource, since_frame: float, since_lookup: float, cfg: Config) -> bool:
        """True when the source died or no frame arrived for a while and the window is gone."""
        from mnmparse.capture import find_game_window

        if getattr(source, "is_running", True) is False:
            log.warning("Capture session ended (game window closed or replaced); looking for the window")
            self._window_found = False
            return True
        if since_frame > FRAME_LOSS_S and since_lookup >= WINDOW_RETRY_S:
            if find_game_window(cfg.window_title) is None:
                log.warning("No frame for %.0f s and the game window is gone; waiting for it", since_frame)
                self._window_found = False
                return True
        return False

    def _check_source_dimensions(self, source: FrameSource, cfg: Config, frame: Any = None) -> None:
        dimensions = getattr(source, "full_frame_dimensions", None)
        if dimensions is None and frame is not None:
            dimensions = (int(frame.shape[1]), int(frame.shape[0]))
        if not dimensions:
            return
        self._source_dimensions = tuple(dimensions)
        calibration = cfg.capture_profiles.get(cfg.active_profile, {}).get("calibration")
        checked = (cfg.active_profile, self._source_dimensions, tuple(sorted(calibration.items())) if calibration else None)
        if checked == self._calibration_checked:
            return
        self._calibration_checked = checked
        if calibration and self._source_dimensions != (calibration["width"], calibration["height"]):
            self.notice.emit("Game dimensions changed; check the crop and capture profile.")

    def _warn_crop(self, cfg: Config, exc: Exception) -> None:
        if not self._crop_warned:
            self._crop_warned = True
            log.warning("Bad crop %s: %s", tuple(cfg.crop), exc)
            self.error.emit(f"Crop {tuple(cfg.crop)} does not fit the frame: {exc}")

    def _process_frame(self, frame: np.ndarray, cfg: Config, *, cropped: bool = False) -> None:
        """One frame: crop -> preprocess -> OCR -> tracker -> messages (cli._RunSession).

        ``cropped``: ``frame`` already is the crop (``latest_region``)."""
        from mnmparse.capture import crop_frame
        from mnmparse.ocr import preprocess

        ocr = self._ocr
        tracker = self._tracker
        if ocr is None or tracker is None:
            return
        cfg = self._capture_config(cfg)
        preprocess_started = time.perf_counter()
        try:
            img = preprocess(frame if cropped else crop_frame(frame, tuple(cfg.crop)), cfg.preprocess, cfg.ocr_scale)
        except ValueError as exc:
            self._warn_crop(cfg, exc)
            return
        t1 = time.perf_counter()
        with self._lock:
            self._record_timing("preprocess", preprocess_started)
        lines = ocr.read(img)
        ocr_ms = (time.perf_counter() - t1) * 1000.0
        now = time.time()
        with self._lock:
            self._timings.setdefault("ocr", deque(maxlen=256)).append(ocr_ms)
            tracking_started = time.perf_counter()
            new_messages = tracker.update(lines, now)
            self._record_timing("tracking", tracking_started)
            if self._last_frame_at:
                self._frame_gap_ms = max(0.0, (time.monotonic() - self._last_frame_at) * 1000.0)
            self._last_frame_at = time.monotonic()
            self._diagnostic_frames.append({
                "read_at": now,
                "crop": list(cfg.crop),
                "input_dimensions": {"width": int(frame.shape[1]), "height": int(frame.shape[0]),
                                     "already_cropped": bool(cropped)},
                "ocr_dimensions": {"width": int(img.shape[1]), "height": int(img.shape[0])},
                "ocr_engine": cfg.ocr_engine,
                "ocr_scale": float(cfg.ocr_scale),
                "preprocess": cfg.preprocess,
                "ocr_ms": round(ocr_ms, 1),
                "row_count": len(lines),
                "rows_truncated": len(lines) > DIAG_ROW_LIMIT,
                "coordinate_space": "unscaled_combat_crop_pixels",
                "rows": [{"x": int(line.x), "y": int(line.y), "h": int(line.h),
                          "text": line.text[:DIAG_TEXT_LIMIT],
                          "text_truncated": len(line.text) > DIAG_TEXT_LIMIT}
                         for line in lines[:DIAG_ROW_LIMIT]],
            })
            self._frames += 1
            self._frame_times.append(time.monotonic())
            self._last_ocr_ms = ocr_ms
            self._last_lines = len(lines)
            log.debug(
                "frame %d: ocr %.1f ms (%d lines), %d new message(s)",
                self._frames, ocr_ms, len(lines), len(new_messages),
            )
            if self._restart is not None:
                new_messages = self._filter_restart(new_messages, now)
            backlog = sum(1 for msg in new_messages if getattr(msg, "backlog", False))
            for msg in new_messages:
                self._handle_message(msg, cfg)
            if backlog:
                log.info("%d lines already on screen at start were not logged again (backlog)", backlog)
            self._note_window_state(tracker, now)
            self._check_crop_rows(cfg, tracker)
            self._since_state_save += len(new_messages)
            save = self._since_state_save >= STATE_SAVE_MESSAGES
            zone = self._current_zone()
        if save:
            self._save_state(cfg, tracker, zone)

    def _filter_restart(self, messages: list[Message], now: float) -> list[Message]:
        """The restart check (lock held): hold the first frame's lines until the chat scrolls
        (or RESTART_HOLD_S), then drop those that repeat the end of the newest log in order.

        Lines flagged as backlog pass (they are not logged anyway), and so does every line
        once the check is done.
        """
        restart = self._restart
        assert restart is not None
        if restart.first_frame is None:
            restart.first_frame = now
        out: list[Message] = []
        for msg in messages:
            if getattr(msg, "backlog", False):
                out.append(msg)
            elif self._restart is not None and msg.first_seen - restart.first_frame <= RESTART_BLOCK_S:
                restart.held.append(msg)
            else:
                out.extend(self._resolve_restart())  # the chat scrolled: the opening block is complete
                out.append(msg)
        if self._restart is not None and now - restart.first_frame >= RESTART_HOLD_S:
            out[:0] = self._resolve_restart()
        return out

    def _resolve_restart(self) -> list[Message]:
        """End the restart check: the held lines that the newest log did not end with (lock held)."""
        from mnmparse.replay import find_backlog

        restart, self._restart = self._restart, None
        if restart is None or not restart.held:
            return []
        held = restart.held
        # Lines never written to the logs cannot be in its tail: they take no part in lining up.
        candidates = [i for i, msg in enumerate(held) if not NOT_LOGGED_RX.match(msg.text)]
        found = find_backlog(restart.tail, [(held[i].first_seen, held[i].text) for i in candidates])
        drop = {candidates[j] for j in found}
        if drop:
            self._backlog_dropped += len(drop)
            log.info(
                "%d of the first frame's %d lines repeat the end of the last log; not logged again",
                len(drop), len(held),
            )
        return [msg for i, msg in enumerate(held) if i not in drop]

    def _note_window_state(self, tracker: Tracker, now: float) -> None:
        """Follow the tracker's scrolled-back state; log when it shows or clears (lock held)."""
        self._quality_tick("chat_occluded", getattr(tracker, "_state", "") == "occluded", now)
        self._quality_tick("chat_scrolled_back", bool(getattr(tracker, "scrolled_back", False)), now)
        if getattr(tracker, "scrolled_back", False):
            if self._scrolled_since is None:
                self._scrolled_since = now
        else:
            self._scrolled_since = None
        shown = self._scrolled_since is not None and now - self._scrolled_since >= SCROLLED_BACK_SHOW_S
        if shown != self._scrolled_shown:
            self._scrolled_shown = shown
            if shown:
                log.info("chat scrolled up (or out of step with its newest line): the open fight does not time out")
            else:
                log.info("chat shows its newest line again")

    def _timeout_held(self, now: float) -> bool:
        """True while the open fight must not time out: the chat window is out of step with
        its newest line, so the fight's next lines may still be coming (lock held)."""
        since = self._scrolled_since
        tracker = self._tracker
        return (
            since is not None
            and tracker is not None
            and getattr(tracker, "scrolled_back", False)
            and now - since <= SCROLLED_BACK_HOLD_MAX_S
        )

    def _check_crop_rows(self, cfg: Config, tracker: Tracker) -> None:
        """Warn once when the crop holds fewer than MIN_CROP_ROWS chat rows (lock held)."""
        if self._rows_warned or len(tracker.prev) < 4:
            return
        self._row_frames += 1
        if self._row_frames < CROP_ROWS_FRAMES:
            return
        self._rows_warned = True  # judged once per start (and per config change)
        _left, top, _right, bottom = (int(v) for v in cfg.crop)
        rows = (bottom - top) / max(1.0, float(tracker.pitch))
        if rows >= MIN_CROP_ROWS:
            return
        text = (
            f"The crop shows only about {int(rows)} chat lines; make it (and the Combat chat window) "
            f"at least {MIN_CROP_ROWS} lines tall, or a burst of lines can scroll past between two frames."
        )
        log.warning("%s (crop %s)", text, tuple(cfg.crop))
        self.notice.emit(text)

    def _handle_message(self, msg: Message, cfg: Config) -> None:
        """Log, parse, aggregate and announce one completed message (lock held).

        A backlog line (on screen when a tracker with no history saw its first frame) is
        neither logged nor counted: its time is unknown, and an earlier run may have logged it.
        """
        from mnmparse.parser import parse_line

        writer, stats = self._writer, self._stats
        if writer is None or stats is None:
            return
        if getattr(msg, "backlog", False):
            self._backlog_dropped += 1
            return
        if not NOT_LOGGED_RX.match(msg.text):
            writer.write_raw(msg)
        from mnmparse.parser import split_fused

        parts = split_fused(msg.text, cfg.player_name)
        if len(parts) > 1:
            # Two messages the OCR ran together: handle each on its own.
            for part in parts:
                self._handle_part(dataclasses.replace(msg, text=part), cfg)
            return
        self._handle_part(msg, cfg)

    def _handle_part(self, msg: Message, cfg: Config) -> None:
        """Parse, aggregate and announce one message (lock held)."""
        from mnmparse.parser import parse_garbled_amount, parse_line

        writer, stats = self._writer, self._stats
        if writer is None or stats is None:
            return
        parsing_started = time.perf_counter()
        ev: Event = parse_line(msg.text, msg.first_seen, cfg.player_name)
        if ev.kind == "unknown":
            # A clipped first glyph ("bepulifif pierces ...") is repaired from names seen so far.
            fixed = self._names.complete(msg.text)
            if fixed is not None:
                repaired = parse_line(fixed, msg.first_seen, cfg.player_name)
                if repaired.kind != "unknown":
                    ev = repaired
        if ev.kind in ("unknown", "ability_partial") and bool(getattr(cfg, "dummy_fix", False)):
            guess = parse_garbled_amount(msg.text, msg.first_seen, cfg.player_name)
            if guess is not None and stats.estimate_amount(guess):
                ev = guess
        self._names.observe(ev)
        ev.estimated_ts = bool(getattr(msg, "estimated_ts", False))
        self._record_timing("parsing", parsing_started)
        unreadable = ev.kind in UNREADABLE_KINDS or ev.estimated
        self._recent_kinds.append("unreadable" if unreadable else ev.kind)
        reason = ("estimated_amount" if ev.estimated else
                  "unrecognized_message" if ev.kind == "unknown" else
                  "incomplete_ability" if ev.kind == "ability_partial" else None)
        self._diagnostic_messages.append({
            "first_seen": msg.first_seen,
            "frames_seen": msg.frames_seen,
            "raw_text": msg.text[:DIAG_TEXT_LIMIT],
            "raw_text_truncated": len(msg.text) > DIAG_TEXT_LIMIT,
            "parsed_text": ev.text[:DIAG_TEXT_LIMIT],
            "kind": ev.kind, "actor": ev.actor, "target": ev.target, "amount": ev.amount,
            "skill": ev.skill, "damage_type": ev.dtype,
            "fragment": bool(msg.fragment), "estimated": bool(ev.estimated),
            "estimated_timestamp": bool(getattr(msg, "estimated_ts", False)),
            "unreadable": bool(unreadable), "unreadable_reason": reason,
        })
        if not NOT_LOGGED_RX.match(msg.text):
            writing_started = time.perf_counter()
            writer.write_event(ev)
            self._record_timing("writing", writing_started)
        before = len(stats.history)
        stats.add(ev)
        try:
            if self._archive is not None:
                with self._archive.transaction():
                    previous_version = self._session_stats.version
                    self._session_stats.add(ev)
                    if self._session_stats.version != previous_version:
                        self._archive.checkpoint(self._archive_id, self._session_stats)
            else:
                self._session_stats.add(ev)
            self._session_dirty = True
        except Exception:  # noqa: BLE001 - session bookkeeping must never stop the pipeline
            log.exception("session stats failed on %r", ev.text)
            archive, self._archive = self._archive, None
            self._archive_error = "session detail write failed"
            if archive is not None:
                try:
                    from mnmparse.session_archive import DETAIL_FIELDS
                    # The event transaction rolled back. Recover its last complete
                    # checkpoint, retain all durable detail in memory, then retry
                    # the event once without relying on the failed disk writer.
                    recovered = archive.restore(self._archive_id, vocab=VOCAB)
                    for field in DETAIL_FIELDS:
                        setattr(recovered, field, list(getattr(recovered, field)))
                    recovered.add(ev)
                    self._session_stats = recovered
                    self._session_dirty = True
                    self.notice.emit("Session recovery storage failed; current data is retained in memory. Check the log folder.")
                except Exception:
                    log.exception("session recovery fallback failed")
        self._messages += 1
        self.message.emit(msg, ev)
        for closed in stats.history[before:]:
            self._emit_closed(closed)
        if stats.current() is not None:
            self._snapshot_dirty = True

    def _expire(self, now: float) -> None:
        """Close a timed-out encounter (``stats.expire``) and announce it.

        Not while the chat window is out of step with its newest line (scrolled up, covered):
        the fight's lines that arrive meanwhile show up later, and a line that comes too late
        still closes the fight itself (``Stats.add``)."""
        with self._lock:
            stats = self._stats
            if stats is None or self._timeout_held(now):
                return
            closed = stats.expire(now)
            if closed is not None:
                self._emit_closed(closed)

    def _emit_closed(self, enc: Encounter) -> EncounterSnapshot | None:
        """Snapshot a just-closed encounter, remember it and emit both signals (lock held).

        Only the group's own fights count in the session's encounters and time in combat."""
        stats = self._stats
        if stats is None:
            return None
        enc.capture_quality["intervals"] = self._quality_for(enc.start, max(enc.end, time.time()))
        enc.revision += 1
        snap = build_snapshot(stats, enc, self._cfg.player_name)
        self._history.append(snap)
        self._history_sessions[snap.key] = self._archive_id
        self._session_encounter_keys.add(snap.key)
        if snap.ours:
            self._session_stats.note_encounter(snap.duration)
            self._session_dirty = True
        self._ensure_archive()
        if self._archive is not None:
            try:
                self._archive.save_encounter(self._archive_id, stats, enc, snap)
                limit = max(1, int(getattr(self._cfg, "history_recent_fights", 100)))
                del self._history[:-limit]
                del stats.history[:-limit]
                self._history_sessions = {item.key: self._history_sessions[item.key] for item in self._history}
                self._session_encounter_keys.intersection_update(item.key for item in self._history)
                self._save_session_archive(force=True)
            except Exception as exc:
                self._archive_error = str(exc)
                log.exception("raw encounter archive failed; retaining editable history in memory")
        snap.emitted_at = time.monotonic()
        self.encounter_closed.emit(snap)
        self.snapshot.emit(snap)
        self._last_snapshot_at = time.monotonic()
        self._snapshot_dirty = False
        return snap

    def _revise_session_encounter(self, old: EncounterSnapshot, new: EncounterSnapshot) -> None:
        if self._history_sessions.get(old.key, self._archive_id) != self._archive_id:
            return
        if old.key not in self._session_encounter_keys:
            if self._archive is None or not self._archive.contains_encounter(self._archive_id, old.key):
                return  # retained history from before Reset Session cannot change new counters
        previous = old.duration if old.ours else None
        replacement = new.duration if new.ours else None
        if previous != replacement:
            self._session_stats.revise_encounter(previous, replacement)
            self._session_dirty = True

    def _maybe_rebuild_recent(self, now: float | None = None) -> list[EncounterSnapshot]:
        """Count the recent closed fights again when the party roster learned someone.

        A member may be recognised only after explicit party evidence arrives;
        the fights already shown had them as an
        outsider.  The closed fights of the current zone visit that ended within
        REBUILD_WINDOW_S of ``now`` are rebuilt, and a fight's snapshot is replaced only when
        its group grew (somebody moved from outsider to group, nobody left it), so a member
        who leaves later stays in the fights they were in.  After a change by hand
        (:meth:`set_group_override`) whatever changed is taken.  The new snapshots go out
        through :attr:`encounter_updated` (no auto-copy, no sound); returns them.
        """
        now = time.time() if now is None else now
        with self._lock:
            stats = self._stats
            if stats is None:
                return []
            roster = stats.roster
            by_hand = self._rebuild_all
            if roster.version == self._rebuilt_version and not by_hand:
                return []
            members = roster.members()
            added = members - self._rebuilt_members
            self._rebuilt_version, self._rebuilt_members, self._rebuild_all = roster.version, members, False
            if not by_hand and (not added or not roster.known()):
                return []
            updated = self._rebuild_recent(stats, now, only_growth=not by_hand)
            for snap in updated:
                snap.emitted_at = time.monotonic()
                self.encounter_updated.emit(snap)
        if updated:
            log.info("party changed: %d recent fight(s) counted again", len(updated))
        return updated

    def _rebuild_recent(self, stats: Stats, now: float, *, only_growth: bool) -> list[EncounterSnapshot]:
        """The recent fights whose snapshot changed, oldest first (lock held; see above)."""
        _zone, entered = stats.zone_visit_at(now)
        positions = {snap.key: i for i, snap in enumerate(self._history[-4 * REBUILD_MAX_FIGHTS:],
                                                          start=max(0, len(self._history) - 4 * REBUILD_MAX_FIGHTS))}
        updated: list[EncounterSnapshot] = []
        rebuilt = 0
        for enc in reversed(stats.history):
            if now - enc.end > REBUILD_WINDOW_S or enc.start < entered or rebuilt >= REBUILD_MAX_FIGHTS:
                break
            index = positions.get(f"{enc.start:.3f}")
            if index is None:
                continue
            old = self._history[index]
            if only_growth and not any(not (r.in_group or r.is_npc or r.is_enemy) for r in old.rows):
                continue  # nobody outside the group: nothing can move in
            rebuilt += 1
            new = build_snapshot(stats, enc, self._cfg.player_name)
            old_group = {r.name for r in old.rows if r.in_group}
            new_group = {r.name for r in new.rows if r.in_group}
            changed = new_group > old_group if only_growth else (
                dataclasses.replace(new, emitted_at=0.0) != dataclasses.replace(old, emitted_at=0.0))
            if not changed:
                continue
            self._history[index] = new
            self._revise_session_encounter(old, new)
            if self._archive is not None:
                self._archive.save_encounter(self._history_sessions.get(new.key, self._archive_id), stats, enc, new)
            updated.append(new)
        updated.reverse()
        return updated

    def _maybe_emit_snapshot(self, now_mono: float) -> None:
        """Emit the open encounter: <= 10 Hz when dirty, and at least 1 Hz while open."""
        with self._lock:
            stats = self._stats
            if stats is None:
                return
            enc = stats.current()
            if enc is None or not enc.has_damage:
                # Nothing to show until something is damaged (a lone miss may still be dropped).
                self._snapshot_dirty = False
                return
            elapsed = now_mono - self._last_snapshot_at
            if (self._snapshot_dirty and elapsed >= SNAPSHOT_MIN_INTERVAL_S) or elapsed >= SNAPSHOT_TICK_S:
                started = time.perf_counter()
                now = time.time()
                snap = build_snapshot(stats, enc, self._cfg.player_name, now=now)
                snap = dataclasses.replace(snap, capture_quality=dict(snap.capture_quality,
                                           intervals=self._quality_for(enc.start, now)))
                self._record_timing("encounter_snapshot", started)
                self._last_snapshot_at = now_mono
                self._snapshot_dirty = False
                snap.emitted_at = time.monotonic()
                self.snapshot.emit(snap)

    def _status_payload(self) -> dict[str, Any]:
        """Snapshot counters shared by the status UI and local diagnosis exports."""
        with self._lock:
            now = time.monotonic()
            recent = [t for t in self._frame_times if now - t <= 2.0]
            fps = len(recent) / 2.0 if recent else 0.0
            tracker = self._tracker
            occluded = tracker.occluded_frames if tracker is not None else 0
            if occluded > self._occluded_seen:
                self._occluded_at = now
            self._occluded_seen = occluded
            kinds = list(self._recent_kinds)
            unreadable = kinds.count("unreadable")
            garbled = len(kinds) >= GARBLE_MIN_MESSAGES and unreadable >= GARBLE_SHARE * len(kinds)
            payload: dict[str, Any] = {
                "state": self._state,
                "emitted_at": now,
                "fps": round(fps, 1),
                "ocr_ms": round(self._last_ocr_ms, 1),
                "frames": self._frames,
                "messages": self._messages,
                "occluded": occluded,
                "occluded_recent": bool(self._occluded_at) and now - self._occluded_at <= OCCLUDED_RECENT_S,
                "unreadable_pct": round(100.0 * unreadable / len(kinds), 1) if kinds else 0.0,
                "garbled": garbled,
                "replays": tracker.replays_suppressed if tracker is not None else 0,
                "window_found": self._window_found,
                "lines": self._last_lines,
                "scrolled_back": self._scrolled_shown and self._state in ("running", "paused"),
                "backlog": self._backlog_dropped,
                "pipeline_ms": {stage: {"samples": len(values), "p50": round(sorted(values)[len(values) // 2], 2),
                                          "p95": round(sorted(values)[min(len(values) - 1, int(len(values) * .95))], 2)}
                                for stage, values in self._timings.items() if values},
                "frame_gap_ms": round(self._frame_gap_ms, 1),
                "source_frame_age_ms": round(self._source_frame_age_ms, 1) if self._source_frame_age_ms is not None else None,
                "source_frames_skipped": self._source_frames_skipped,
                "source_frames_repeated": self._source_frames_repeated,
                "source_dimensions": dict(width=self._source_dimensions[0], height=self._source_dimensions[1])
                                     if self._source_dimensions else None,
                "history_fights": len(self._history),
                "raw_history_fights": len(self._stats.history) if self._stats is not None else
                                      len(self._stopped_stats.history) if self._stopped_stats is not None else 0,
                "archive_error": bool(self._archive_error),
                "timing_sample_limit": 256,
            }
            return payload

    def _emit_status(self) -> None:
        self.status.emit(self._status_payload())

    def _finish(self) -> None:
        """Flush the tracker, close the open encounter and the writer, stop the source.

        Order matters: the tracker flush and the writer close come first so every
        held line reaches the log files even when the source takes a while to
        stop; the open encounter (if any) is closed and announced through
        :attr:`encounter_closed` so it lands in :meth:`history` and the views show
        it as ended instead of a frozen "live" fight.  The tracker (with its remembered
        rows), the party roster and the last zone are kept for the next start, and the
        tracker state and the zone are saved for a restart of the app.  Ends with state
        ``stopped``.
        """
        cfg = self._capture_config(self.config)
        try:
            with self._lock:
                tracker, writer = self._tracker, self._writer
                if tracker is not None and writer is not None:
                    try:
                        now = time.time()
                        messages = tracker.flush(now)
                        if self._restart is not None:
                            messages = self._filter_restart(messages, now)
                            messages[:0] = self._resolve_restart()
                        for msg in messages:
                            self._handle_message(msg, cfg)
                    except Exception:  # noqa: BLE001
                        log.exception("tracker flush failed")
                self._restart = None
                try:
                    self._close_open_encounter("capture stopped")
                    self._maybe_rebuild_recent()
                except Exception:  # noqa: BLE001
                    log.exception("closing the open encounter failed")
                self._maybe_save_roster(time.monotonic(), force=True)
                if self._backlog_dropped:
                    log.info("%d lines on screen at start were not logged again (backlog)", self._backlog_dropped)
                stats = self._stats
                if stats is not None:
                    self._roster = stats.roster
                    self._stopped_stats = stats
                    self._last_zone = self._current_zone()
                for kind in tuple(self._quality_active):
                    self._quality_tick(kind, False, time.time())
                self._save_session_archive(force=True)
                if tracker is not None:
                    try:
                        self._save_state(cfg, tracker, self._last_zone)
                    except Exception:  # noqa: BLE001 - the state is a nicety
                        log.exception("saving the tracker state failed")
                    self._kept_tracker = (_capture_key(cfg), tracker, time.time())
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:  # noqa: BLE001
                        log.exception("log writer close failed")
                ocr = self._ocr
                if ocr is not None:
                    close = getattr(ocr, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:  # noqa: BLE001
                            log.debug("ocr close raised", exc_info=True)
                self._tracker = None
                self._writer = None
                self._ocr = None
                self._stats = None
        finally:
            self._close_source()
            self._window_found = False
            self._save_vocab()
            log.info("Engine worker finished: %d frames, %d messages", self._frames, self._messages)
            self._set_state("stopped")
            self._emit_status()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown engine state {state!r}")
        if self._stop_event.is_set() and state not in {"stopping", "stopped"}:
            return
        if state == self._state:
            return
        self._state = state
        log.info("Engine state -> %s", state)
        self.state_changed.emit(state)

    @staticmethod
    def _wait_for_frame(source: FrameSource, timeout_s: float) -> np.ndarray | None:
        """``source.wait_for_frame`` when available, otherwise poll ``latest()``."""
        waiter = getattr(source, "wait_for_frame", None)
        if callable(waiter):
            try:
                return waiter(timeout_s)
            except Exception as exc:  # noqa: BLE001 - CaptureError from the capture thread
                log.warning("wait_for_frame failed: %s", exc)
                return None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            frame = source.latest()
            if frame is not None:
                return frame
            time.sleep(0.05)
        return None
