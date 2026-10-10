"""Current chat navigation evidence replaces stale maps without replaying combat."""
from __future__ import annotations

import os
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.app.models import build_snapshot
from mnmparse.config import Config
from mnmparse.maps import zone_from_who, zone_title
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.session import SessionStats
from mnmparse.tracker import Message
from mnmparse.tracker import Tracker


class ZoneEvidenceTests(unittest.TestCase):
    def test_compact_zone_name_is_canonicalized_without_fuzzy_guessing(self):
        self.assertEqual(zone_title("FallenPass"), "Fallen Pass")
        self.assertEqual(zone_title("fallen_pass."), "Fallen Pass")
        self.assertEqual(zone_title("Fallen Watch"), "Fallen Watch")
        self.assertEqual(zone_title("FallenPath"), "FallenPath")

    def test_local_who_summary_reads_only_a_complete_known_zone(self):
        for text in ("There are 43 players in Fallen Pass who match your search.",
                     "There is 1 player in FallenPass who match your search.",
                     "There are 0 players in Night Harbor (West) who match your search."):
            with self.subTest(text=text):
                self.assertIn(zone_from_who(text), ("Fallen Pass", "Night Harbor"))
        for text in ("There are 43 players in Monsters & Memories who match your search.",
                     "There are 43 players in Unknown Place who match your search.",
                     "Tamsin says There are 43 players in Fallen Pass who match your search.",
                     "There are 43 players in Fallen Pass who match your", "Players in Monsters & Memories"):
            with self.subTest(text=text):
                self.assertEqual(zone_from_who(text), "")

    def test_who_evidence_does_not_reclassify_personal_event_or_close_fight(self):
        stats = Stats(player_name="Maergoth")
        stats.add(parse_line("You crush a dusty skeleton for 20 points of damage.", 100, "Maergoth"))
        current = stats.current()
        event = parse_line("There are 43 players in Fallen Pass who match your search.", 101, "Maergoth")
        self.assertEqual(event.kind, "personal")
        stats.add(event)
        self.assertIs(stats.current(), current)
        self.assertFalse(current.closed)
        self.assertEqual(stats.zone_changes, [])


class MapNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "map.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("map/zone", "Tomb of the Last Wyrmsbane")
        self.repo = Mock()
        self.map = MapOverlay(self.settings, self.repo)

    def tearDown(self):
        self.map.shutdown()
        self.map.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def deliver(self, text: str):
        message = Message(text, 100, 2)
        self.map.on_message(message, parse_line(text, message.first_seen, "Maergoth"))

    def test_fresh_local_who_replaces_stale_wyrmsbane_with_fallen_pass(self):
        self.assertEqual(self.map.current_zone, "Tomb of the Last Wyrmsbane")
        self.deliver("There are 43 players in Fallen Pass who match your search.")
        self.assertEqual(self.map.current_zone, "Fallen Pass")
        self.assertEqual(self.settings.value("map/zone"), "Fallen Pass")
        self.assertFalse(self.map.isVisible())
        self.repo.maps.assert_not_called()
        self.repo.image.assert_not_called()

    def test_compact_entry_updates_map_in_default_carebear_mode(self):
        self.assertTrue(Config().casual_mode)
        self.deliver("You have entered FallenPass.")
        self.assertEqual(self.map.current_zone, "Fallen Pass")

    def test_manual_selection_survives_unrelated_or_quoted_chat(self):
        self.deliver("You crush a rat for 20 points of damage.")
        self.deliver("Tamsin says There are 43 players in Fallen Pass who match your search.")
        self.deliver("There are 43 players in Monsters & Memories who match your search.")
        self.assertEqual(self.map.current_zone, "Tomb of the Last Wyrmsbane")


class EngineNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mnmparse.app.engine import Engine
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name="Maergoth", log_dir=self.temp.name, start_capture_on_launch=False,
                          setup_complete=True)
        with patch("mnmparse.app.engine._load_vocab_once", lambda cfg: None):
            self.engine = Engine(self.cfg)
        self.engine._stats = Stats(player_name=self.cfg.player_name)
        self.engine._session_stats = SessionStats(self.cfg.player_name)
        self.engine._writer = Mock()
        self.seen = []
        self.zones = []
        self.engine.message.connect(lambda message, event: self.seen.append((message, event)))
        self.engine.zone_changed.connect(self.zones.append)

    def tearDown(self):
        self.engine._writer = None
        self.engine.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def deliver(self, text, ts=100, *, backlog=False):
        self.engine._handle_message(Message(text, ts, 2, backlog=backlog), self.cfg)

    def test_who_keeps_raw_input_and_personal_event_while_correcting_current_fight_zone(self):
        self.engine._stats.zone_changes.append((0.0, "Tomb of the Last Wyrmsbane"))
        self.deliver("You crush a dusty skeleton for 20 points of damage.", 100)
        fight = self.engine._stats.current()
        original = build_snapshot(self.engine._stats, fight, self.cfg.player_name)
        self.assertEqual(original.zone, "Tomb of the Last Wyrmsbane")
        text = "There are 43 players in Fallen Pass who match your search."
        self.deliver(text, 101)
        self.assertEqual(self.seen[-1][0].text, text)
        self.assertEqual(self.seen[-1][1].kind, "personal")
        self.assertIs(self.engine._stats.current(), fight)
        self.assertFalse(fight.closed)
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.assertEqual(self.zones, ["Fallen Pass"])
        self.assertEqual(build_snapshot(self.engine._stats, fight, self.cfg.player_name).zone, "Fallen Pass")
        self.assertEqual(self.engine._session_stats.snapshot(101).zones, ["Fallen Pass"])

    def test_backlog_hydrates_zone_without_emitting_chat_matching_triggers_or_counting_combat(self):
        from mnmparse.app.main import App
        from mnmparse.app.triggers_runtime import TriggerRunner
        from mnmparse.triggers import TriggerStore, Trigger
        store = TriggerStore(Path(self.temp.name) / "triggers.json")
        store.triggers = [Trigger(name="Old entry", pattern="entered", action="none", timer=True)]
        runner = TriggerRunner(store)
        runner.audio.run = Mock()
        host = SimpleNamespace(triggers=runner, window=None)
        self.engine.message.connect(lambda msg, event: App._on_message_for_triggers(host, msg, event))
        try:
            self.deliver("You have entered Fallen Pass.", backlog=True)
            self.deliver("You crush a dusty skeleton for 20 points of damage.", backlog=True)
            self.assertEqual(self.engine.current_zone, "Fallen Pass")
            self.assertEqual(self.zones, ["Fallen Pass"])
            self.assertEqual(self.seen, [])
            self.assertIsNone(self.engine._stats.current())
            self.assertEqual(self.engine._messages, 0)
            self.assertEqual(self.engine._session_stats.snapshot(100).recent, [])
            self.assertEqual(self.engine._session_stats.snapshot(100).zones, ["Fallen Pass"])
            self.engine._writer.write_raw.assert_not_called()
            self.engine._writer.write_event.assert_not_called()
            self.assertEqual(runner.board.timers, [])
            runner.audio.run.assert_not_called()
        finally:
            runner._clock.stop()
            runner.deleteLater()

    def test_only_latest_confirmed_startup_evidence_is_kept(self):
        self.deliver("You have entered Night Harbor (East).", backlog=True)
        self.deliver("Entering Tomb of the Last Wyrmsbane.", backlog=True)
        self.assertEqual(self.engine.current_zone, "Night Harbor (East)")
        self.deliver("You have entered FallenPass.", backlog=True)
        self.assertEqual(zone_title(self.engine.current_zone), "Fallen Pass")

    def test_unknown_global_or_scrolled_back_evidence_does_not_override_navigation(self):
        self.engine._last_zone = "Fallen Pass"
        self.deliver("There are 43 players in Monsters & Memories who match your search.")
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.engine._messages = 0
        self.engine._tracker = SimpleNamespace(scrolled_back=True)
        self.deliver("You have entered Night Harbor.", backlog=True)
        self.assertEqual(self.engine.current_zone, "Fallen Pass")

    def test_recent_saved_zone_is_available_without_archive_and_expires_normally(self):
        from mnmparse.app.engine import Engine, SESSION_STATE_FILE, STATE_MAX_AGE_S
        path = Path(self.temp.name) / SESSION_STATE_FILE
        path.write_text(json.dumps({"version": 1, "zone": "Fallen Pass", "saved": time.time()}), encoding="utf-8")
        with patch("mnmparse.app.engine._load_vocab_once", lambda cfg: None):
            restored = Engine(self.cfg)
            self.assertEqual(restored.current_zone, "Fallen Pass")
            restored._install_stats(self.cfg)
            self.assertEqual(restored._session_stats.snapshot().zones, ["Fallen Pass"])
            path.write_text(json.dumps({"version": 1, "zone": "Fallen Pass",
                                        "saved": time.time() - STATE_MAX_AGE_S - 1}), encoding="utf-8")
            expired = Engine(self.cfg)
            self.assertEqual(expired.current_zone, "")
            restored.deleteLater()
            expired.deleteLater()

    def test_initial_ocr_navigation_survives_tracker_deduplication_without_replaying_rows(self):
        import numpy as np
        from tests.test_audit2_engine import rows
        texts = [f"You crush a dusty skeleton for {index} points of damage." for index in range(1, 9)]
        texts.append("There are 43 players in Fallen Pass who match your search.")
        reading = rows(texts)
        before = Tracker()
        now = time.time()
        for index in range(4):
            before.update(reading, now + index * 0.25)
        restored = Tracker()
        self.assertTrue(restored.import_state(before.export_state(now), now=now))
        self.engine._tracker = restored
        self.engine._ocr = SimpleNamespace(read=lambda image: reading)
        self.engine._stats.zone_changes = [(0.0, "Tomb of the Last Wyrmsbane")]
        frame = np.zeros((8, 8, 3), np.uint8)
        self.engine._process_frame(frame, self.cfg, cropped=True)
        self.assertEqual(self.engine.current_zone, "Tomb of the Last Wyrmsbane")
        self.engine._process_frame(frame, self.cfg, cropped=True)
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.assertEqual(self.zones, ["Fallen Pass"])
        self.assertEqual(self.seen, [])
        self.assertEqual(self.engine._messages, 0)
        self.assertIsNone(self.engine._stats.current())
        self.assertEqual(self.engine._session_stats.snapshot().zones, ["Fallen Pass"])

    def test_zone_evidence_does_not_relabel_already_closed_encounters(self):
        self.engine._stats.zone_changes = [(0.0, "Tomb of the Last Wyrmsbane")]
        self.deliver("You crush a rat for 20 points of damage.", 100)
        old = self.engine._stats.current()
        self.engine._stats.expire(200)
        self.deliver("You crush a dusty skeleton for 20 points of damage.", 200)
        self.deliver("There are 43 players in Fallen Pass who match your search.", 201)
        self.assertEqual(build_snapshot(self.engine._stats, old, self.cfg.player_name).zone,
                         "Tomb of the Last Wyrmsbane")
        self.assertEqual(build_snapshot(self.engine._stats, self.engine._stats.current(), self.cfg.player_name).zone,
                         "Fallen Pass")

    def test_real_app_wiring_syncs_known_zone_and_follows_captured_entries_while_map_stays_closed(self):
        from mnmparse.app.tray import TrayIcon
        from tests.test_map_lock_wiring import _AppHarness, _DownloadController
        settings = QSettings(str(Path(self.temp.name) / "app.ini"), QSettings.Format.IniFormat)
        settings.setValue("map/zone", "Tomb of the Last Wyrmsbane")
        settings.setValue("map/visible", False)
        self.engine._last_zone = "Fallen Pass"
        host = _AppHarness(settings, combat_available=False)
        host._make_engine = lambda cfg: self.engine
        with patch("mnmparse.app.main.app_icon.ensure_icon_file"), \
             patch("mnmparse.app.main.QSystemTrayIcon.isSystemTrayAvailable", return_value=False), \
             patch("mnmparse.app.main.TrayIcon", side_effect=lambda icon, parent: TrayIcon(icon)), \
             patch("mnmparse.app.map_downloads.MapDownloadController", side_effect=lambda *args: _DownloadController()), \
             patch("mnmparse.app.app_updates.AppUpdateController", side_effect=lambda *args: _DownloadController()), \
             patch("mnmparse.app.map_overlay._Load.start", side_effect=AssertionError("No map loading while closed")):
            try:
                host.bootstrap(self.cfg)
                self.assertEqual(host.map_overlay.current_zone, "Fallen Pass")
                self.assertFalse(host.map_overlay.isVisible())
                self.deliver("You have entered Night Harbor (East).")
                self.assertEqual(host.map_overlay.current_zone, "Night Harbor")
                self.assertFalse(host.map_overlay.isVisible())
                self.assertEqual(self.engine.current_zone, "Night Harbor (East)")
            finally:
                host._shut_down = True
                host.window.prepare_quit()
                for widget in (host.window, host.overlay, host.map_overlay):
                    if widget is not None:
                        widget.close()
                        widget.deleteLater()
                host.tray.hide()
                host.tray.deleteLater()
                self.app.processEvents()

    def test_resuming_historical_session_does_not_replace_current_navigation_zone(self):
        from mnmparse.session_archive import SessionArchive
        archive = SessionArchive(Path(self.temp.name) / "sessions.sqlite3")
        old = SessionStats(self.cfg.player_name, started=100)
        old.add(parse_line("You have entered Tomb of the Last Wyrmsbane.", 100, self.cfg.player_name))
        archive.save_session("historical", old, ended=101)
        self.engine._archive = archive
        self.engine._stats = None
        self.engine._last_zone = "Fallen Pass"
        self.assertTrue(self.engine.restore_session("historical"))
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.assertEqual(self.engine.session_snapshot(full=True).zones, ["Tomb of the Last Wyrmsbane"])
        self.engine._install_stats(self.cfg)
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.assertEqual(self.engine.session_snapshot(full=True).zones[-1], "Fallen Pass")

    def test_delayed_fight_zone_correction_keeps_visits_ordered_and_survives_archive_round_trip(self):
        from mnmparse.session_archive import SessionArchive
        self.engine._stats.zone_changes = [(0.0, "Tomb of the Last Wyrmsbane"), (150.0, "Night Harbor")]
        self.deliver("You crush a rat for 20 points of damage.", 120)
        closed = self.engine._stats.current()
        self.engine._stats.expire(200)
        self.deliver("You crush a dusty skeleton for 20 points of damage.", 100)
        fight = self.engine._stats.current()
        self.deliver("There are 43 players in Fallen Pass who match your search.", 101)
        visits = self.engine._stats.zone_changes
        self.assertEqual([stamp for stamp, zone in visits], sorted(stamp for stamp, zone in visits))
        self.assertEqual(build_snapshot(self.engine._stats, closed, self.cfg.player_name).zone,
                         "Tomb of the Last Wyrmsbane")
        shown = build_snapshot(self.engine._stats, fight, self.cfg.player_name)
        self.assertEqual(shown.zone, "Fallen Pass")
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        archive = SessionArchive(Path(self.temp.name) / "delayed.sqlite3")
        archive.save_session("delayed", self.engine._session_stats)
        archive.save_encounter("delayed", self.engine._stats, fight, shown)
        restored_stats, restored_fight = archive.encounter(shown.key, session_id="delayed")
        self.assertEqual(build_snapshot(restored_stats, restored_fight, self.cfg.player_name).zone, "Fallen Pass")

    def test_same_current_who_zone_repairs_historical_session_metadata(self):
        self.engine._stats.zone_changes = [(0.0, "Fallen Pass")]
        self.engine._session_stats.note_zone("Tomb of the Last Wyrmsbane")
        self.deliver("There are 43 players in Fallen Pass who match your search.")
        self.assertEqual(self.engine.current_zone, "Fallen Pass")
        self.assertEqual(self.engine._session_stats.snapshot().zones[-1], "Fallen Pass")


if __name__ == "__main__":
    unittest.main()
