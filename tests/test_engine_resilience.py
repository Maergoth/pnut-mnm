"""Worker ownership, recovery, quality annotations and bounded history."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import dataclasses
import gc
import json
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QObject, QSettings
from mnmparse.app.engine import Engine
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.session import SessionStats, filter_session
from mnmparse.session_archive import DiskDetails
from mnmparse.app.models import build_snapshot
from mnmparse.capture import WgcWindowSource
from mnmparse.tracker import Message
from mnmparse.vocab import Vocabulary
from mnmparse.privacy import project_session

PLAYER = "Pidef"


class EngineResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_engine(self, directory: str, *, limit: int = 2) -> Engine:
        return Engine(Config(player_name=PLAYER, log_dir=directory, history_recent_fights=limit))

    def fight(self, engine: Engine, start: float, actor: str = "You"):
        if engine._stats is None:
            engine._install_stats(engine.config)
        stats = engine._stats
        stats.add(parse_line(f"{actor} crushes a rat for 10 points of damage.", start, PLAYER))
        stats.add(parse_line(f"{actor} crushes a rat for 10 points of damage.", start + 3, PLAYER))
        enc = stats.expire(start + 50)
        self.assertIsNotNone(enc)
        return engine._emit_closed(enc)

    def test_slow_cleanup_retains_worker_and_rejects_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            started, closing, release = threading.Event(), threading.Event(), threading.Event()
            engine._prepare_run = Mock()
            engine._open_source = lambda: (started.set(), engine._stop_event.wait(2), None)[-1]
            def finish():
                closing.set()
                release.wait(3)
                engine._set_state("stopped")
            engine._finish = finish
            with patch("mnmparse.ocr.make_engine", return_value=object()):
                engine.start()
                self.assertTrue(started.wait(1))
                worker = engine._thread
                begin = time.perf_counter()
                engine.stop()
                self.assertLess(time.perf_counter() - begin, .1)
                self.assertTrue(closing.wait(1))
                self.assertEqual(engine.state, "stopping")
                self.assertFalse(engine.wait_stopped(.01))
                engine.start()
                self.assertIs(engine._thread, worker)
                self.assertTrue(engine.is_running)
                release.set()
                self.assertTrue(engine.wait_stopped(1))
                self.assertIsNone(engine._thread)

    def test_recent_history_bound_and_older_raw_correction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            older = self.fight(engine, 100, "Tovozen")
            self.assertFalse(older.ours)
            for index in range(5):
                self.fight(engine, 200 + index * 100)
            self.assertEqual((len(engine.history()), len(engine._stats.history)), (2, 2))
            self.assertEqual(engine.session_snapshot().encounters, 5)
            corrected = engine.correct_archived_encounter(older.key, name="Tovozen", in_group=True)
            self.assertTrue(corrected.ours)
            self.assertEqual(engine.session_snapshot().encounters, 6)
            restored = self.make_engine(directory)
            self.assertEqual(restored.session_snapshot().encounters, 6)
            self.assertEqual(len(restored.history()), 2)
            self.assertTrue(restored.archived_encounters(engine._archive_id, full=True)[0].ours)

    def test_reset_archives_and_recovery_keeps_distinct_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            self.fight(engine, 100)
            old_id = engine._archive_id
            self.assertTrue(engine.reset_session())
            self.assertNotEqual(engine._archive_id, old_id)
            self.assertEqual(engine.session_snapshot().encounters, 0)
            self.assertEqual(engine.archived_session(old_id).encounters, 1)
            restarted = self.make_engine(directory)
            self.assertEqual(restarted._archive_id, engine._archive_id)
            self.assertEqual(restarted.session_snapshot().encounters, 0)
            self.assertTrue(restarted.restore_session(old_id))
            self.assertEqual(restarted.session_snapshot().encounters, 1)

    def test_archive_failure_never_prunes_editable_raw_fights_or_resets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            store = Mock()
            store.save_encounter.side_effect = OSError("disk full")
            store.save_session.side_effect = OSError("disk full")
            engine._archive = store
            with self.assertLogs("mnmparse.app.engine", "ERROR"):
                for index in range(5):
                    self.fight(engine, 100 + index * 100)
                self.assertFalse(engine.reset_session())
            self.assertEqual((len(engine.history()), len(engine._stats.history)), (5, 5))
            self.assertEqual(engine.session_snapshot().encounters, 5)

    def test_quality_intervals_survive_close_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            engine._install_stats(engine.config)
            engine._quality_tick("chat_occluded", True, 100)
            engine._quality_tick("chat_occluded", False, 105)
            snap = self.fight(engine, 100)
            self.assertEqual(snap.capture_quality["intervals"][0]["kind"], "chat_occluded")
            restarted = self.make_engine(directory)
            quality = restarted.archived_encounters(engine._archive_id)[0].capture_quality
            self.assertEqual(quality, snap.capture_quality)
            self.assertEqual(restarted.session_snapshot().capture_quality["interruption_count"], 1)

    def test_timing_samples_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            for value in range(1000):
                engine.note_ui_delivery(value)
            status = engine._status_payload()
            self.assertEqual(status["pipeline_ms"]["ui_delivery"]["samples"], 256)
            self.assertGreater(status["pipeline_ms"]["ui_delivery"]["p95"], 900)

    def import_result(self):
        stats = Stats(player_name=PLAYER)
        stats.add(parse_line("You crush a rat for 10 points of damage.", 100, PLAYER))
        enc = stats.expire(150)
        session = SessionStats(PLAYER, started=100)
        for index in range(300):
            session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101 + index, PLAYER))
        session.note_encounter(enc.end - enc.start)
        return SimpleNamespace(session_stats=session, stats=stats,
                               encounters=[build_snapshot(stats, enc, PLAYER)], ended=500)

    def test_import_archive_bounds_display_but_keeps_full_export_and_corrections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            result = self.import_result()
            session_id = engine.archive_import(result)
            self.assertIsInstance(session_id, str)
            self.assertIsInstance(result.session_stats._loot, list)
            with patch.object(DiskDetails, "__iter__", side_effect=AssertionError("raw history scanned on selection")):
                preview = engine.archived_session(session_id)
            self.assertEqual((preview.items, len(preview.loot)), (300, 200))
            self.assertEqual(filter_session(preview, PLAYER).items, 300)
            self.assertEqual(len(engine.archived_session(session_id, full=True).loot), 300)
            raw_stats, enc = engine.archived_encounter(result.encounters[0].key, session_id=session_id)
            self.assertEqual(enc.events[0].amount, 10)
            self.assertEqual(engine.session_snapshot().items, 0)

    def test_cancelled_import_discards_partial_rows_and_preserves_original(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            result = self.import_result()
            calls = 0
            def cancelled():
                nonlocal calls
                calls += 1
                return calls >= 2
            with self.assertLogs("mnmparse.app.engine", "ERROR"), self.assertRaises(InterruptedError):
                engine.archive_import(result, cancelled=cancelled)
            self.assertIsInstance(result.session_stats._loot, list)
            self.assertEqual(len(result.session_stats._loot), 300)
            with engine._archive._connect() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM details").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM encounters").fetchone()[0], 0)
            self.assertEqual(len(engine.archived_sessions()), 1)

    def test_failed_detail_checkpoint_falls_back_once_without_losing_or_duplicating_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            engine._install_stats(engine.config)
            engine._writer = Mock()
            engine._ensure_archive()
            archive = engine._archive
            with patch.object(archive, "checkpoint", side_effect=OSError("disk full")), self.assertLogs("mnmparse.app.engine", "ERROR"):
                engine._handle_part(Message("You loot [Bone Chips] from a rat's corpse.", 101, 1), engine.config)
            self.assertIsNone(engine._archive)
            self.assertIsInstance(engine._session_stats._loot, list)
            self.assertEqual((engine.session_snapshot().items, len(engine._session_stats._loot)), (1, 1))
            self.assertEqual(archive.restore(engine._archive_id).snapshot().items, 0)

    def test_preference_reload_replaces_vocab_and_writes_party_before_first_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("mnmparse.app.engine.VOCAB", Vocabulary()) as vocab:
            engine = self.make_engine(directory)
            for _ in range(3):
                vocab.observe("player", "Tovozen")
            self.assertTrue(engine.flush_preferences())
            expected = vocab.to_dict()
            self.assertTrue((Path(directory) / "party.json").is_file())
            vocab.observe("player", "Otherpeer")
            self.assertTrue(engine.reload_preferences())
            self.assertEqual(vocab.to_dict(), expected)
            self.assertTrue(engine.reload_preferences())
            self.assertEqual(vocab.to_dict(), expected)

    def test_ongoing_capture_gap_recovers_only_observed_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            engine._quality_tick("chat_occluded", True, 100)
            with patch("mnmparse.app.engine.time.time", return_value=105):
                self.assertTrue(engine._save_session_archive(force=True))
            restarted = self.make_engine(directory)
            quality = restarted.session_snapshot().capture_quality
            self.assertEqual(quality["interruption_seconds"], 5)
            self.assertEqual(quality["intervals"][0]["end"], 105)
            self.assertNotIn("ongoing_intervals", quality)

    def test_native_cleanup_stays_owned_until_thread_exits(self) -> None:
        source = WgcWindowSource("unused")
        worker = Mock()
        source._thread = worker
        worker.is_alive.return_value = True
        with self.assertLogs("mnmparse.capture", "WARNING"):
            source.stop()
        self.assertIs(source._thread, worker)
        self.assertFalse(source.wait_stopped(0))
        worker.is_alive.return_value = False
        self.assertTrue(source.wait_stopped(0))
        self.assertIsNone(source._thread)

    def test_source_dimensions_notice_uses_full_bounds_once_per_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            cfg = engine.config
            cfg.capture_profiles = {"Default": {"calibration": {"width": 1920, "height": 1080}}}
            source = SimpleNamespace(full_frame_dimensions=(2560, 1440))
            notices = []
            engine.notice.connect(notices.append)
            engine._check_source_dimensions(source, cfg)
            engine._check_source_dimensions(source, cfg)
            self.assertEqual(len(notices), 1)
            self.assertEqual(engine._status_payload()["source_dimensions"], {"width": 2560, "height": 1440})

    def test_default_session_view_is_bounded_but_exact_self_totals_survive_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            session = engine._session_stats
            for index in range(250):
                session.add(parse_line("You loot [Bone Chips] from a rat's corpse.", 101 + index, PLAYER))
                session.add(parse_line("Tovozen loots [Spider Silk] from a rat's corpse.", 101 + index, PLAYER))
                session.add(parse_line("You have slain a rat!", 101 + index, PLAYER))
                session.add(parse_line("You craft Cloth Scraps(3).", 101 + index, PLAYER))
            bounded = engine.session_snapshot()
            safe = project_session(bounded, engine.config)
            full = engine.session_snapshot(full=True)
            self.assertEqual((len(bounded.loot), len(bounded.kill_entries), len(bounded.craft_entries)), (200, 200, 200))
            self.assertEqual((safe.items, safe.kills, safe.crafts), (250, 250, 750))
            self.assertEqual((len(full.loot), len(full.kill_entries), len(full.craft_entries)), (500, 250, 250))

    def test_archive_correction_same_timestamp_does_not_modify_other_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            active = self.fight(engine, 100)
            imported = Stats(player_name=PLAYER)
            imported.add(parse_line("Tovozen crushes a rat for 99 points of damage.", 100, PLAYER))
            imported.add(parse_line("Tovozen crushes a rat for 99 points of damage.", 103, PLAYER))
            enc = imported.expire(150)
            result = SimpleNamespace(stats=imported, encounters=[build_snapshot(imported, enc, PLAYER)],
                                     session_stats=SessionStats(PLAYER, started=100), ended=150)
            archive_id = engine.archive_import(result)
            updated = []
            engine.encounter_updated.connect(updated.append)
            corrected = engine.correct_archived_encounter(active.key, session_id=archive_id,
                                                          name="Tovozen", in_group=True)
            self.assertTrue(corrected.ours)
            self.assertEqual(engine.history()[0], active)
            self.assertEqual(updated, [])
            self.assertEqual(engine.session_snapshot().encounters, 1)

    def test_active_archived_correction_emits_updated_stopped_session_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            outside = self.fight(engine, 100, "Tovozen")
            sessions = []
            engine.session.connect(sessions.append)
            engine.correct_archived_encounter(outside.key, name="Tovozen", in_group=True)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].encounters, 1)
            self.assertGreater(sessions[0].emitted_at, 0)

    def test_worker_message_delivery_runs_gui_receivers_on_gui_thread(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            received = []
            class Receiver(QObject):
                def observe(self, _message, _event):
                    received.append(("bound", threading.get_ident()))
            receiver = Receiver()
            engine.message.connect(receiver.observe)
            engine.message.connect(lambda _message, _event: received.append(("lambda", threading.get_ident())))
            gui_thread = threading.get_ident()
            worker = threading.Thread(target=lambda: engine.message.emit(object(), object()))
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            deadline = time.monotonic() + 1
            while len(received) < 2 and time.monotonic() < deadline:
                self.app.processEvents()
            self.assertEqual(received, [("bound", gui_thread), ("lambda", gui_thread)])

    def test_archived_import_ui_releases_raw_parsers_and_projects_preview(self) -> None:
        from mnmparse.app.pages import LivePage, SessionPage
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            cfg = engine.config
            settings = QSettings(str(Path(directory) / "ui.ini"), QSettings.Format.IniFormat)
            live = LivePage(engine, cfg, settings)
            session_page = SessionPage(engine, cfg, settings)
            live.imported.connect(session_page.add_imported_result)
            result = self.import_result()
            result.session_stats.add(parse_line("Tovozen loots [Spider Silk] from a rat's corpse.", 450, PLAYER))
            result.name = "Tovozen source"
            result.session = result.session_stats.snapshot()
            result.archive_id = engine.archive_import(result)
            raw_stats, raw_session = weakref.ref(result.stats), weakref.ref(result.session_stats)
            archive_id = result.archive_id
            live._install_import_results([result], [])
            del result
            gc.collect()
            self.assertIsNone(raw_stats())
            self.assertIsNone(raw_session())
            self.assertFalse(live._import_sources)
            self.assertEqual(set(live._archive_sources.values()), {archive_id})
            shown = session_page.shown()
            self.assertEqual(shown.items, 300)
            self.assertLessEqual(len(shown.loot), 200)
            self.assertNotIn("Tovozen", json.dumps(dataclasses.asdict(shown)))
            self.assertNotIn("Tovozen", session_page._source.currentText())
            live.close()
            session_page.close()

    def test_current_snapshot_reads_only_open_fight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = self.make_engine(directory)
            self.assertIsNone(engine.current_snapshot())
            stats = engine._install_stats(engine.config)
            stats.add(parse_line("You crush a rat for 10 points of damage.", 100, PLAYER))
            snap = engine.current_snapshot()
            self.assertEqual(snap.total_damage, 10)
            self.assertFalse(snap.closed)
            self.assertEqual(snap.key, "100.000")
            stats.expire(150)
            self.assertIsNone(engine.current_snapshot())


if __name__ == "__main__":
    unittest.main()
