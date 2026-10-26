"""Eligibility follows the actual live capture source and fresh reader evidence."""
import dataclasses
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.capture import MssRegionSource, WgcWindowSource
from mnmparse.config import Config
from mnmparse.ocr import OcrLine
from mnmparse.parser import parse_line
from mnmparse.player_status import PlayerStatusReading, read_player_status
from mnmparse.protected_stats import ProtectedLedger
from mnmparse.tracker import Message


class CaptureEligibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(player_name="Owner", log_dir=self.tmp.name, crop=(0, 0, 8, 8))
        self.engine = Engine(self.cfg)
        self.ledger = ProtectedLedger(Path(self.tmp.name) / "leaderboard.bin")
        self.engine.set_protected_ledger(self.ledger)
        self.engine._source = object.__new__(WgcWindowSource)
        self.engine._state = "running"
        self.engine._window_found = True
        self.addCleanup(self.engine.deleteLater)

    def read_stats(self):
        reading = PlayerStatusReading(10, 100, (0, 0, 8, 8), (0,), mana_max=300)
        self.engine._observe_player_status(reading, self.engine._cfg, time.time(), (8, 8, 3))

    def test_only_real_window_source_and_fresh_maxima_enable_submit(self):
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.read_stats()
        record = self.engine.leaderboard_submission()
        self.assertEqual((record["max_health"], record["max_mana"], record["capture_backend"]), (100, 300, "window"))
        self.engine._source = object.__new__(MssRegionSource)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        with self.assertRaises(ValueError):
            self.engine.leaderboard_submission()
        self.engine._source = object.__new__(WgcWindowSource)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.read_stats()
        self.assertTrue(self.engine.leaderboard_status()["eligible"])

    def test_config_switch_pause_stop_and_expiry_invalidate(self):
        self.read_stats()
        self.engine.update_config(dataclasses.replace(self.cfg, capture_backend="mss"))
        self.assertIn("Window Capture", self.engine.leaderboard_status()["reason"])
        self.read_stats()
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.engine.update_config(self.cfg)
        self.read_stats()
        self.assertTrue(self.engine.leaderboard_status()["eligible"])
        self.engine.set_paused(True)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.engine.set_paused(False)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.read_stats()
        self.engine._stat_snapshot_valid_until = time.time() - 1
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.read_stats()
        self.engine._state = "stopped"
        self.assertFalse(self.engine.leaderboard_status()["eligible"])

    def test_busy_view_is_distinct_from_missing_data_and_actions_recheck(self):
        self.read_stats()
        expected_alias = self.ledger.status()["alias"]
        held, release = threading.Event(), threading.Event()

        def hold_reader_lock():
            with self.engine._lock:
                held.set()
                release.wait(5)

        reader = threading.Thread(target=hold_reader_lock)
        reader.start()
        try:
            self.assertTrue(held.wait(2))
            with patch.object(self.ledger, "status", wraps=self.ledger.status) as status:
                self.assertIsNone(self.engine.leaderboard_view())
                status.assert_not_called()
                for action in (self.engine.leaderboard_submission, self.engine.leaderboard_copy_text):
                    with self.assertRaisesRegex(ValueError, "Waiting for the screen reader"):
                        action()
        finally:
            release.set()
            reader.join(2)
        self.assertFalse(reader.is_alive())
        status, stats = self.engine.leaderboard_view()
        self.assertEqual(status["alias"], expected_alias)
        self.assertTrue(status["eligible"])
        self.assertEqual(stats["max_health"], 100)

    def test_view_eligibility_uses_the_same_single_freshness_check_as_values(self):
        self.read_stats()
        snapshot = self.engine.player_stat_snapshot()
        # A second freshness check could cross the reading's expiry boundary.
        with patch.object(self.engine, "player_stat_snapshot", side_effect=[snapshot, None]) as read:
            status, stats = self.engine.leaderboard_view()
        read.assert_called_once_with()
        self.assertIs(stats, snapshot)
        self.assertTrue(status["eligible"])
        self.assertTrue(status["copy_eligible"])
        stats["max_health"] = 999
        self.assertEqual(self.engine.player_stat_snapshot()["max_health"], 100)

    def test_complete_views_report_real_expiry_and_unavailable_history(self):
        self.read_stats()
        expected_alias = self.ledger.status()["alias"]
        self.engine._stat_snapshot_valid_until = time.time() - 1
        status, stats = self.engine.leaderboard_view()
        self.assertIsNone(stats)
        self.assertEqual(status["alias"], expected_alias)
        self.assertEqual(status["records"]["max_health"]["value"], 100)
        self.assertFalse(status["eligible"])
        self.assertFalse(status["copy_eligible"])
        self.ledger.error = "Test history unavailable"
        status, stats = self.engine.leaderboard_view()
        self.assertIsNone(stats)
        self.assertEqual(status["error"], "Test history unavailable")
        self.assertNotIn("alias", status)
        self.assertFalse(status["eligible"])
        self.engine._protected_ledger = None
        status, stats = self.engine.leaderboard_view()
        self.assertIsNone(stats)
        self.assertNotIn("alias", status)
        self.assertIn("unavailable", status["reason"])
        self.assertFalse(status["eligible"])

    def test_only_live_complete_exact_events_enter_new_counter_ledger(self):
        event = parse_line("You have slain a rat.", time.time(), "Owner")
        msg = Message(event.text, event.ts, 2)
        for changes in ({"backlog": True}, {"fragment": True}):
            self.engine._protect_live_event(dataclasses.replace(msg, **changes), event)
        self.engine._protect_live_event(msg, dataclasses.replace(event, estimated=True))
        self.engine._protect_live_event(msg, dataclasses.replace(event, estimated_ts=True))
        self.assertEqual(self.ledger.status()["personal_kills"], 0)
        self.engine._source = object.__new__(MssRegionSource)
        self.engine._protect_live_event(msg, event)
        self.assertEqual(self.ledger.status()["personal_kills"], 0)
        self.engine._source = object.__new__(WgcWindowSource)
        self.engine._protect_live_event(msg, event)
        self.assertEqual(self.ledger.status()["personal_kills"], 1)
        self.engine._protect_live_event(msg, event)
        self.assertEqual(self.ledger.status()["personal_kills"], 1)

    def test_named_self_coin_and_hits_count_with_current_viewer_ownership(self):
        for text in ("Owner pierces a rat for 70 points of Physical Damage.",
                     "Owner loots 7 copper coins from a rat's corpse.",
                     "Other pierces a rat for 999 points of Physical Damage."):
            event = parse_line(text, time.time(), "Owner")
            self.engine._protect_live_event(Message(text, event.ts, 2), event)
        status = self.ledger.status()
        self.assertEqual((status["coin_looted"], status["high_hit"]), (7, 70))

    def test_copy_submit_never_reconstructs_from_editable_session_totals(self):
        self.engine._session_stats = Mock()
        self.engine._session_stats.snapshot.return_value = dict(kills=99999, high_hit=99999)
        self.read_stats()
        self.assertEqual(self.engine.leaderboard_submission()["personal_kills"], 0)
        self.engine._session_stats.snapshot.assert_not_called()

    def test_desktop_copy_remains_local_while_submission_and_counters_are_ineligible(self):
        self.engine._source = object.__new__(MssRegionSource)
        self.engine._cfg = dataclasses.replace(self.cfg, capture_backend="mss")
        self.read_stats()
        status = self.engine.leaderboard_status()
        self.assertTrue(status["copy_eligible"])
        self.assertFalse(status["eligible"])
        copied = self.engine.leaderboard_copy_text()
        self.assertIn("Max Health: 100", copied)
        self.assertIn("Max Mana: 300", copied)
        self.assertIn("Kills: 0", copied)
        with self.assertRaises(ValueError):
            self.engine.leaderboard_submission()

    def test_unreadable_mana_does_not_borrow_endurance_or_weight(self):
        for text, expected in (("Mana 20/300", 300), ("Mana unreadable", None)):
            lines = [OcrLine(100, 10, 10, "Health 10/100"), OcrLine(100, 35, 10, text),
                     OcrLine(100, 60, 10, "Endurance 99/999"), OcrLine(100, 85, 10, "Weight 10/200")]
            reading = read_player_status(lines)
            self.assertIsNotNone(reading)
            self.assertEqual(reading.mana_max, expected)
