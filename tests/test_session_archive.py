"""Durable session details, raw corrections and snapshot cache regressions."""
from __future__ import annotations

import dataclasses
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mnmparse.app import models
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.session import SessionStats, filter_session
from mnmparse.session_archive import DiskDetails, SessionArchive
from mnmparse.stats import Stats
from mnmparse.vocab import Vocabulary

PLAYER = "Pidef"


class SnapshotCacheTests(unittest.TestCase):
    def test_idle_encounter_retimes_without_reaggregating_events(self) -> None:
        stats = Stats(player_name=PLAYER)
        stats.add(parse_line("You crush a rat for 20 points of damage.", 100, PLAYER))
        enc = stats.current()
        with patch.object(models, "_accumulate", wraps=models._accumulate) as aggregate:
            first = models.build_snapshot(stats, enc, PLAYER, now=102)
            later = models.build_snapshot(stats, enc, PLAYER, now=104)
        self.assertEqual(aggregate.call_count, 1)
        self.assertEqual(first.revision, later.revision)
        self.assertEqual((first.raid_dps, later.raid_dps), (10, 5))
        self.assertEqual(first.active_duration, later.active_duration)
        self.assertIsNot(first.rows[0], later.rows[0])

    def test_marker_roster_and_vocabulary_invalidate_content(self) -> None:
        vocab = Vocabulary()
        stats = Stats(player_name=PLAYER, vocab=vocab)
        stats.add(parse_line("You crush a rat for 20 points of damage.", 100, PLAYER))
        stats.add(parse_line("Tovozen crushes a rat for 10 points of damage.", 101, PLAYER))
        enc = stats.current()
        first = models.build_snapshot(stats, enc, PLAYER)
        stats.roster.set_manual("Tovozen", True)
        grouped = models.build_snapshot(stats, enc, PLAYER)
        self.assertEqual(grouped.total_damage, 30)
        self.assertGreater(grouped.revision, first.revision)
        stats.add(Event(102, "marker", "(Block 3)", amount=3, outcome="block"))
        marked = models.build_snapshot(stats, enc, PLAYER)
        self.assertGreater(marked.revision, grouped.revision)
        self.assertEqual(next(r for r in marked.rows if r.name == "a rat").prevented, 3)
        vocab.observe("skill", "crush")
        renamed = models.build_snapshot(stats, enc, PLAYER)
        self.assertGreater(renamed.revision, marked.revision)

    def test_idle_session_cache_and_late_roster_correction(self) -> None:
        session = SessionStats(PLAYER, started=100)
        session.add(parse_line("Tovozen has slain a rat!", 101, PLAYER))
        with patch.object(session, "_classify_slain", wraps=session._classify_slain) as classify:
            first = session.snapshot(now=102)
            second = session.snapshot(now=103)
            self.assertEqual(classify.call_count, 1)
            session._local_roster.set_manual("Tovozen", True)
            corrected = session.snapshot(now=104)
        self.assertEqual(first.kills, 0)
        self.assertEqual(corrected.kills, 1)
        self.assertEqual(second.revision, first.revision)
        self.assertGreater(corrected.revision, first.revision)

    def test_limited_details_keep_full_self_totals(self) -> None:
        session = SessionStats(PLAYER, started=100)
        for index in range(10):
            session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101 + index, PLAYER))
            session.add(parse_line("You have slain a rat!", 101 + index, PLAYER))
            session.add(parse_line("You craft Cloth Scraps(3).", 101 + index, PLAYER))
        snap = session.snapshot(now=200, detail_limit=2)
        own = filter_session(snap, PLAYER)
        self.assertEqual(len(own.loot), 2)
        self.assertEqual((own.items, own.kills, own.crafts), (10, 10, 30))
        self.assertEqual(dict(own.crafts_by_item), {"Cloth Scraps": 30})
        self.assertNotIn("_personal_totals", dataclasses.asdict(snap))


class ArchiveTests(unittest.TestCase):
    def test_disk_details_and_wrapped_coin_split_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionArchive(Path(directory) / "sessions.sqlite3")
            session = SessionStats(PLAYER, started=100)
            store.attach_details(session, "first")
            session.add(parse_line("You loot 20 copper coins from a rat's corpse.", 101, PLAYER))
            session.add(parse_line("5 copper coins from a rat's corpse as your split.", 102, PLAYER))
            for index in range(300):
                session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 103 + index, PLAYER))
            store.save_session("first", session)
            restored = store.restore("first")
            self.assertIsInstance(restored._loot, DiskDetails)
            snap = restored.snapshot(now=500, detail_limit=20)
            self.assertEqual((snap.items, len(snap.loot), snap.coin_received), (300, 20, 5))
            full = restored.snapshot(now=500)
            self.assertEqual(len(full.loot), 300)
            self.assertEqual(filter_session(snap, PLAYER).items, 300)
            self.assertIn("your split 5c", restored._coins[0].entry.text)
            self.assertEqual([entry.ts for entry in restored._loot[-5::2]], [398, 400, 402])
            self.assertEqual([entry.ts for entry in restored._loot[-1:-6:-2]], [402, 400, 398])
            self.assertEqual(restored._loot[10:5], [])

    def test_event_and_counter_checkpoint_roll_back_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionArchive(Path(directory) / "sessions.sqlite3")
            session = SessionStats(PLAYER, started=100)
            store.attach_details(session, "first")
            store.save_session("first", session)
            with self.assertRaises(RuntimeError):
                with store.transaction():
                    session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101, PLAYER))
                    store.checkpoint("first", session)
                    raise RuntimeError("simulated interrupted transaction")
            restored = store.restore("first")
            self.assertEqual((restored.snapshot(now=200).items, len(restored._loot)), (0, 0))

    def test_raw_encounter_keeps_timestamp_quality_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionArchive(Path(directory) / "sessions.sqlite3")
            stats = Stats(player_name=PLAYER)
            stats.zone_changes.append((90, "Night Harbor"))
            ev = parse_line("You crush a rat for 20 points of damage.", 100, PLAYER)
            ev.estimated = ev.estimated_ts = True
            stats.add(ev)
            enc = stats.expire(200)
            enc.capture_quality = {"intervals": [{"kind": "chat_occluded", "start": 100, "end": 105}]}
            snap = models.build_snapshot(stats, enc, PLAYER)
            store.save_encounter("first", stats, enc, snap)
            rebuilt, raw = store.encounter(snap.key, session_id="first")
            result = models.build_snapshot(rebuilt, raw, PLAYER)
            self.assertTrue(raw.events[0].estimated_ts)
            self.assertEqual(result.capture_quality, snap.capture_quality)
            self.assertEqual(result.zone, "Night Harbor")

    def test_preview_uses_bounded_rows_and_complete_totals_without_raw_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionArchive(Path(directory) / "sessions.sqlite3")
            session = SessionStats(PLAYER, started=100)
            store.attach_details(session, "first")
            for index in range(250):
                session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101 + index, PLAYER))
                session.add(parse_line("You have slain a rat!", 101 + index, PLAYER))
            store.save_session("first", session, ended=500)
            with patch.object(DiskDetails, "__iter__", side_effect=AssertionError("raw history was scanned")):
                preview = store.preview("first", detail_limit=2)
            own = filter_session(preview, PLAYER)
            self.assertEqual((preview.items, preview.kills, len(preview.loot), len(preview.kill_entries)), (250, 250, 2, 2))
            self.assertEqual((own.items, own.kills), (250, 250))
            self.assertEqual(preview.elapsed, 400)
            self.assertEqual(store.preview("first", detail_limit=0).loot, [])
            self.assertEqual(store.metadata("first")["items"], 250)

    def test_active_recovery_is_not_limited_to_recent_sealed_imports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SessionArchive(Path(directory) / "sessions.sqlite3")
            store.save_session("active", SessionStats(PLAYER, started=100))
            for index in range(101):
                store.save_session(f"import-{index}", SessionStats(PLAYER, started=200), ended=300)
            self.assertNotIn("active", {item["id"] for item in store.sessions()})
            self.assertEqual(store.latest_active()["id"], "active")
            self.assertEqual(len(store.sessions(limit=10, offset=5)), 10)

    def test_startup_discards_only_incomplete_import_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.sqlite3"
            store = SessionArchive(path)
            complete = SessionStats(PLAYER, started=100)
            store.attach_details(complete, "complete")
            complete.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101, PLAYER))
            store.save_session("complete", complete, ended=200)
            pending = SessionStats(PLAYER, started=300)
            store.attach_details(pending, "pending")
            pending.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 301, PLAYER))
            stats = Stats(player_name=PLAYER)
            stats.add(parse_line("You crush a rat for 20 points of damage.", 301, PLAYER))
            enc = stats.expire(400)
            store.save_encounter("pending", stats, enc, models.build_snapshot(stats, enc, PLAYER))
            restarted = SessionArchive(path)
            with restarted._connect() as db:
                self.assertEqual(db.execute("SELECT DISTINCT session_id FROM details").fetchall(), [("complete",)])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM encounters").fetchone()[0], 0)
            self.assertEqual(restarted.restore("complete").snapshot().items, 1)

    def test_retention_removes_only_stale_unprotected_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sessions.sqlite3"
            store = SessionArchive(path)
            for key in ("stale", "active", "recent"):
                session = SessionStats(PLAYER, started=100)
                store.save_session(key, session)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("UPDATE sessions SET updated=? WHERE id IN ('stale','active')", (time.time() - 60 * 86400,))
            self.assertEqual(store.prune(30, protected="active"), 1)
            self.assertEqual({item["id"] for item in store.sessions()}, {"active", "recent"})


if __name__ == "__main__":
    unittest.main()
