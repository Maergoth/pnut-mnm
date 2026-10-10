"""Local session recovery and on-demand raw encounter history (JSON in SQLite).

The database belongs to PNUT's configured log folder. Raw names and events stay
local; callers must apply the configured privacy projection before displaying them.
Connections are short lived, writes are transactional, and large detail streams
stay on disk instead of growing in the capture worker's memory.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sqlite3
import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from collections.abc import Iterator, Sequence
from typing import Any
from itertools import islice

from .grammar import Event
from .party import PartyRoster
from .session import LootEntry, SessionEntry, SessionSnapshot, SessionStats, _Coin
from .stats import Encounter, Stats

DETAIL_FIELDS = {"_loot": LootEntry, "_rewards": LootEntry, "_coins": _Coin,
                 "_slain": tuple, "_craft_entries": tuple, "_mez_breaks": tuple}
COUNTER_FIELDS = {"_items_by_name", "_items_by_looter", "_coin_by_looter", "_cc_by_type", "_faction", "_names"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
                      default=lambda obj: sorted(obj) if isinstance(obj, set) else _encode(obj))


def _encode(value: Any) -> Any:
    return dataclasses.asdict(value) if dataclasses.is_dataclass(value) else value


def _decode(value: Any, kind: Any) -> Any:
    if kind is _Coin:
        return _Coin(**dict(value, entry=SessionEntry(**value["entry"])))
    return kind(**value) if dataclasses.is_dataclass(kind) else kind(value)


class DiskDetails(Sequence):
    """Appendable sequence with no retained full-history Python list."""
    def __init__(self, archive: SessionArchive, session_id: str, field: str, kind: Any) -> None:
        self.archive, self.session_id, self.field, self.kind = archive, session_id, field, kind

    def __len__(self) -> int:
        with self.archive._lock, self.archive._connect() as db:
            return db.execute("SELECT COUNT(*) FROM details WHERE session_id=? AND field=?",
                              (self.session_id, self.field)).fetchone()[0]

    def append(self, value: Any) -> None:
        with self.archive._lock, self.archive._connect() as db:
            db.execute("INSERT INTO details(session_id,field,position,payload) SELECT ?,?,COALESCE(MAX(position)+1,0),? "
                       "FROM details WHERE session_id=? AND field=?",
                       (self.session_id, self.field, _json(_encode(value)), self.session_id, self.field))

    def extend(self, values: Any, *, cancelled: Any = None) -> None:
        iterator = iter(values)
        while batch := list(islice(iterator, 128)):
            if cancelled is not None and cancelled():
                raise InterruptedError("import archive cancelled")
            with self.archive._lock, self.archive._connect() as db:
                position = db.execute("SELECT COALESCE(MAX(position)+1,0) FROM details WHERE session_id=? AND field=?",
                                      (self.session_id, self.field)).fetchone()[0]
                db.executemany("INSERT INTO details VALUES(?,?,?,?)",
                               ((self.session_id, self.field, position + offset, _json(_encode(value)))
                                for offset, value in enumerate(batch)))

    def __iter__(self) -> Iterator[Any]:
        # A dedicated read connection lets callers iterate without holding a global lock.
        with self.archive._connect() as db:
            rows = db.execute("SELECT payload FROM details WHERE session_id=? AND field=? ORDER BY position",
                              (self.session_id, self.field))
            for row in rows:
                yield _decode(json.loads(row[0]), self.kind)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            positions = range(*index.indices(len(self)))
            if not positions:
                return []
            lower, upper = min(positions[0], positions[-1]), max(positions[0], positions[-1])
            order = "DESC" if positions.step < 0 else "ASC"
            with self.archive._lock, self.archive._connect() as db:
                rows = db.execute("SELECT payload FROM details WHERE session_id=? AND field=? "
                                  f"AND position BETWEEN ? AND ? AND (position-?) % ?=0 ORDER BY position {order}",
                                  (self.session_id, self.field, lower, upper, positions.start, abs(positions.step))).fetchall()
            return [_decode(json.loads(row[0]), self.kind) for row in rows]
        if index < 0:
            index += len(self)
        if index < 0:
            raise IndexError(index)
        with self.archive._lock, self.archive._connect() as db:
            row = db.execute("SELECT payload FROM details WHERE session_id=? AND field=? AND position=?",
                             (self.session_id, self.field, index)).fetchone()
        if row is None:
            raise IndexError(index)
        return _decode(json.loads(row[0]), self.kind)

    def __setitem__(self, index: int, value: Any) -> None:
        if index < 0:
            index += len(self)
        with self.archive._lock, self.archive._connect() as db:
            result = db.execute("UPDATE details SET payload=? WHERE session_id=? AND field=? AND position=?",
                                (_json(_encode(value)), self.session_id, self.field, index))
            if result.rowcount != 1:
                raise IndexError(index)


class SessionArchive:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._local = threading.local()
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, started REAL NOT NULL, updated REAL NOT NULL,
                    ended REAL, player TEXT NOT NULL, state TEXT NOT NULL, snapshot TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS encounters(
                    session_id TEXT NOT NULL, key TEXT NOT NULL, started REAL NOT NULL,
                    raw TEXT NOT NULL, context TEXT NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY(session_id,key));
                CREATE TABLE IF NOT EXISTS details(
                    session_id TEXT NOT NULL, field TEXT NOT NULL, position INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(session_id,field,position));
                CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated DESC);
                CREATE INDEX IF NOT EXISTS sessions_active_updated ON sessions(ended,updated DESC);
                CREATE INDEX IF NOT EXISTS encounters_started ON encounters(session_id,started);
            """)
            if "preview" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN preview TEXT")
            db.execute("PRAGMA user_version=2")
            # This archive is opened once at application startup, before import
            # workers exist. A staged import becomes visible only when its final
            # session row commits; missing parents therefore identify interrupted
            # imports, whose raw/detail rows otherwise escape retention pruning.
            for table in ("encounters", "details"):
                db.execute(f"DELETE FROM {table} WHERE session_id NOT IN (SELECT id FROM sessions)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._local, "connection", None)
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=5.0)
        self._local.connection = connection
        try:
            with connection:
                yield connection
        finally:
            self._local.connection = None
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group a session event's detail appends and recovery checkpoint atomically."""
        with self._lock, self._connect():
            yield

    def attach_details(self, stats: SessionStats, session_id: str, *, restore: bool = False, cancelled: Any = None) -> None:
        for field, kind in DETAIL_FIELDS.items():
            original = getattr(stats, field)
            disk = DiskDetails(self, session_id, field, kind)
            if not restore:
                disk.extend(original, cancelled=cancelled)
            setattr(stats, field, disk)

    def save_session(self, session_id: str, stats: SessionStats, *, ended: float | None = None) -> None:
        state = self._state(stats)
        snap = stats.snapshot(now=ended, detail_limit=200)
        preview = dataclasses.asdict(snap)
        preview["_personal_totals"] = getattr(snap, "_personal_totals", {})
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO sessions(id,started,updated,ended,player,state,snapshot,preview) VALUES(?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET updated=excluded.updated,ended=excluded.ended,player=excluded.player,"
                       "state=excluded.state,snapshot=excluded.snapshot,preview=excluded.preview",
                       (session_id, stats.started, time.time(), ended, stats.player_name, _json(state),
                        _json({"zones": snap.zones, "encounters": snap.encounters,
                               "items": snap.items, "capture_quality": snap.capture_quality}), _json(preview)))

    def checkpoint(self, session_id: str, stats: SessionStats) -> None:
        """Persist counters cheaply; full aggregate display content updates separately."""
        state = _json(self._state(stats))
        with self._lock, self._connect() as db:
            changed = db.execute("UPDATE sessions SET state=?,updated=? WHERE id=?",
                                 (state, time.time(), session_id))
            if not changed.rowcount:
                self.save_session(session_id, stats)

    @staticmethod
    def _state(stats: SessionStats) -> dict[str, Any]:
        fields = {name: value for name, value in vars(stats).items()
                  if name not in DETAIL_FIELDS and name not in {"roster", "vocab", "_local_roster", "_snapshot_cache"}}
        fields["_recent"] = [dataclasses.asdict(entry) for entry in stats._recent]
        fields["_local_roster"] = stats._local_roster.to_dict()
        fields["roster"] = stats.roster.to_dict() if stats.roster is not None else None
        # Standalone archives also support in-memory SessionStats supplied by tests/imports.
        for field in DETAIL_FIELDS:
            if not isinstance(getattr(stats, field), DiskDetails):
                fields[field] = [_encode(value) for value in getattr(stats, field)]
        return fields

    def restore(self, session_id: str, *, vocab: Any = None) -> SessionStats:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT state FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        state = json.loads(row[0])
        stats = SessionStats(state["player_name"], started=state["started"], vocab=vocab)
        for name, value in state.items():
            if name in {"roster", "_local_roster"}:
                if value is not None:
                    roster = PartyRoster(stats.player_name)
                    roster.merge_dict(value, max_age_s=float("inf"))
                    setattr(stats, name, roster)
            elif name == "_recent":
                stats._recent = deque((SessionEntry(**entry) for entry in value), maxlen=200)
            elif name in COUNTER_FIELDS:
                setattr(stats, name, Counter(value))
            elif name == "_item_looters":
                stats._item_looters = {key: Counter(table) for key, table in value.items()}
            elif name in DETAIL_FIELDS:
                setattr(stats, name, [_decode(item, DETAIL_FIELDS[name]) for item in value])
            elif name in vars(stats):
                setattr(stats, name, value)
        for field, kind in DETAIL_FIELDS.items():
            if field not in state:
                setattr(stats, field, DiskDetails(self, session_id, field, kind))
        return stats

    @staticmethod
    def _metadata(row: Any) -> dict[str, Any]:
        session_id, started, updated, ended, player, payload = row
        snap = json.loads(payload)
        return dict(id=session_id, started=started, updated=updated, ended=ended, player_name=player,
                    zones=snap["zones"], encounters=snap["encounters"], items=snap["items"],
                    capture_quality=snap.get("capture_quality", {}))

    def metadata(self, session_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT id,started,updated,ended,player,snapshot FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        return self._metadata(row)

    def latest_active(self) -> dict[str, Any] | None:
        """Find recovery state independently of recent sealed imports."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT id,started,updated,ended,player,snapshot FROM sessions "
                             "WHERE ended IS NULL ORDER BY updated DESC LIMIT 1").fetchone()
        return self._metadata(row) if row is not None else None

    def preview(self, session_id: str, *, detail_limit: int = 200) -> SessionSnapshot | None:
        """Read the persisted bounded display view without scanning raw detail history."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT preview FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError(session_id)
        if row[0] is None:  # archives written before bounded previews were added
            return None
        data = json.loads(row[0])
        personal = data.pop("_personal_totals", {})
        for field in ("loot", "rewards"):
            data[field] = [LootEntry(**item) for item in data[field]]
        data["recent"] = [SessionEntry(**item) for item in data["recent"]]
        for field in ("items_by_name", "items_by_looter", "mez_breaks", "kill_entries", "craft_entries",
                      "coin_by_looter", "crafts_by_crafter", "crafts_by_item", "kills_by_killer", "kills_by_target",
                      "deaths_by_player", "cc_by_type", "skill_ups", "faction", "outsider_deaths", "outsider_crafts"):
            data[field] = [tuple(item) for item in data[field]]
        data["item_looters"] = {item: [tuple(pair) for pair in looters] for item, looters in data["item_looters"].items()}
        size = max(0, int(detail_limit))
        for field in ("loot", "rewards", "recent", "mez_breaks", "kill_entries", "craft_entries"):
            data[field] = data[field][-size:] if size else []
        snap = SessionSnapshot(**data)
        snap._personal_totals = personal
        return snap

    def sessions(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id,started,updated,ended,player,snapshot FROM sessions ORDER BY updated DESC LIMIT ? OFFSET ?",
                              (max(1, int(limit)), max(0, int(offset)))).fetchall()
        return [self._metadata(row) for row in rows]

    def save_encounter(self, session_id: str, stats: Stats, enc: Encounter, snap: Any) -> None:
        context = dict(player_name=stats.player_name, encounter_timeout_s=stats.encounter_timeout_s,
                       zone_changes=stats.zone_changes, others_misses_seen=stats.others_misses_seen,
                       roster=stats.roster.to_dict())
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO encounters VALUES(?,?,?,?,?,?) ON CONFLICT(session_id,key) DO UPDATE SET "
                       "raw=excluded.raw,context=excluded.context,snapshot=excluded.snapshot",
                       (session_id, snap.key, enc.start, _json(dataclasses.asdict(enc)), _json(context),
                        _json(dataclasses.asdict(snap))))

    def encounter_keys(self, session_id: str, *, limit: int | None = None, offset: int = 0, newest: bool = False) -> list[str]:
        with self._lock, self._connect() as db:
            sql = "SELECT key FROM encounters WHERE session_id=? ORDER BY started " + ("DESC" if newest else "ASC")
            args: tuple = (session_id,)
            if limit is not None:
                sql += " LIMIT ? OFFSET ?"
                args += (max(0, limit), max(0, offset))
            return [row[0] for row in db.execute(sql, args)]

    def contains_encounter(self, session_id: str, key: str) -> bool:
        with self._lock, self._connect() as db:
            return db.execute("SELECT 1 FROM encounters WHERE session_id=? AND key=?", (session_id, key)).fetchone() is not None

    def discard(self, session_id: str) -> None:
        """Discard only the caller's uncommitted import identifier."""
        with self._lock, self._connect() as db:
            for table in ("sessions", "encounters", "details"):
                db.execute(f"DELETE FROM {table} WHERE {'id' if table == 'sessions' else 'session_id'}=?", (session_id,))

    def encounter(self, key: str, *, session_id: str | None = None, vocab: Any = None) -> tuple[Stats, Encounter]:
        with self._lock, self._connect() as db:
            sql = "SELECT raw,context FROM encounters WHERE key=?"
            args: tuple = (key,)
            if session_id is not None:
                sql += " AND session_id=?"
                args += (session_id,)
            row = db.execute(sql + " ORDER BY started DESC LIMIT 1", args).fetchone()
        if row is None:
            raise KeyError(key)
        raw, context = (json.loads(payload) for payload in row)
        raw["events"] = [Event(**event) for event in raw["events"]]
        raw["target_names"], raw["killed_names"] = set(raw["target_names"]), set(raw["killed_names"])
        enc = Encounter(**raw)
        stats = Stats(context["encounter_timeout_s"], player_name=context["player_name"], vocab=vocab)
        stats.zone_changes = [tuple(change) for change in context["zone_changes"]]
        stats.others_misses_seen = context["others_misses_seen"]
        stats.roster.merge_dict(context["roster"], max_age_s=float("inf"))
        stats.history = [enc]
        return stats, enc

    def prune(self, retention_days: int, *, protected: str) -> int:
        cutoff = time.time() - max(1, retention_days) * 86400
        with self._lock, self._connect() as db:
            stale = [row[0] for row in db.execute("SELECT id FROM sessions WHERE updated<? AND id<>?", (cutoff, protected))]
            for session_id in stale:
                for table in ("sessions", "encounters", "details"):
                    db.execute(f"DELETE FROM {table} WHERE {'id' if table == 'sessions' else 'session_id'}=?", (session_id,))
        return len(stale)
