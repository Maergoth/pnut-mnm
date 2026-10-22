"""Capture transitions require a new physical reader sample before publication."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.capture import MssRegionSource, WgcWindowSource
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.player_status import PlayerStatusReading
from mnmparse.protected_stats import ProtectedLedger
from mnmparse.stats import Stats
from mnmparse.tracker import Message, Tracker


@unittest.skipUnless(os.name == "nt", "Windows protected capture ledger")
class LeaderboardCaptureEdgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name="FictionalOwner", log_dir=self.tmp.name, crop=(0, 0, 8, 8),
                          player_status_separate=False)
        self.engine = Engine(self.cfg)
        self.ledger = ProtectedLedger(Path(self.tmp.name) / "fixture.bin")
        self.engine.set_protected_ledger(self.ledger)
        self.engine._source = object.__new__(WgcWindowSource)
        self.engine._state = "running"
        self.engine._window_found = True
        self.engine._ocr = Mock()
        self.engine._ocr.read.return_value = []
        self.engine._tracker = Tracker()

    def tearDown(self):
        self.engine._source = None  # Fixture sources have no native capture session.
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.tmp.cleanup()

    @staticmethod
    def reading(maximum=100):
        return PlayerStatusReading(10, maximum, (0, 0, 8, 8), (), mana_max=300)

    def observe(self, maximum=100):
        self.engine._observe_player_status(self.reading(maximum), self.engine._cfg, time.time(), (8, 8, 3))

    def frame(self, *, fresh=True, during_ocr=None):
        if during_ocr is not None:
            def read(_image):
                during_ocr()
                return []
            self.engine._ocr.read.side_effect = read
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading(999)):
            self.engine._process_frame(np.zeros((8, 8, 3), dtype=np.uint8), self.engine.config, fresh=fresh)

    def test_mss_to_wgc_preference_does_not_relabel_old_mss_source_or_add_counters(self):
        self.engine._source = object.__new__(MssRegionSource)
        self.engine.update_config(dataclasses.replace(self.cfg, capture_backend="mss"))
        self.observe()
        self.assertIn("Max Health: 100", self.engine.leaderboard_copy_text())
        self.engine.update_config(self.cfg)  # Preference changed; the old source is still alive.
        self.observe(200)
        event = parse_line("You have slain a fictional rat.", time.time(), self.cfg.player_name)
        self.engine._protect_live_event(Message(event.text, event.ts, 2), event)
        status = self.engine.leaderboard_status()
        self.assertTrue(status["copy_eligible"])
        self.assertFalse(status["eligible"])
        self.assertEqual(status["personal_kills"], 0)
        self.assertIn("Max Health: 200", self.engine.leaderboard_copy_text())
        with self.assertRaises(ValueError):
            self.engine.leaderboard_submission()
        self.engine._source = object.__new__(WgcWindowSource)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.observe(300)
        self.assertEqual(self.engine.leaderboard_submission()["max_health"], 300)

    def test_pause_and_window_loss_require_new_stats_before_submission(self):
        self.observe()
        self.engine.set_paused(True)
        self.engine.set_paused(False)
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.observe()
        self.engine._window_found = False
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.engine._window_found = True
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        self.observe()
        self.assertTrue(self.engine.leaderboard_status()["eligible"])

    def test_repeated_source_frame_does_not_extend_reader_freshness(self):
        self.observe()
        before = self.engine._stat_snapshot_valid_until
        self.frame(fresh=False)
        self.assertEqual(self.engine._stat_snapshot_valid_until, before)
        self.assertEqual(self.engine.leaderboard_submission()["max_health"], 100)

    def test_crop_changed_during_ocr_cannot_restore_invalidated_stats(self):
        self.observe()
        self.frame(during_ocr=lambda: self.engine.update_config(dataclasses.replace(self.cfg, crop=(0, 0, 7, 7))))
        self.assertIsNone(self.engine.player_stat_snapshot())
        self.assertFalse(self.engine.leaderboard_status()["eligible"])

    def test_pause_and_resume_during_ocr_cannot_publish_the_pre_pause_frame(self):
        self.observe()
        def pause_and_resume():
            self.engine.set_paused(True)
            self.engine.set_paused(False)
        self.frame(during_ocr=pause_and_resume)
        self.assertIsNone(self.engine.player_stat_snapshot())
        self.assertFalse(self.engine.leaderboard_status()["eligible"])

    def test_direct_old_config_sample_cannot_refill_the_protected_reader(self):
        old_cfg = self.engine.config
        self.observe()
        self.engine.update_config(dataclasses.replace(self.cfg, crop=(0, 0, 7, 7)))
        # Legacy telemetry callers may still supply independent config fixtures;
        # their values must not become a new leaderboard sample.
        self.engine._observe_player_status(self.reading(999), old_cfg, time.time(), (8, 8, 3))
        self.assertFalse(self.engine.leaderboard_status()["eligible"])
        with self.assertRaises(ValueError):
            self.engine.leaderboard_submission()

    def test_source_replaced_during_ocr_discards_the_previous_source_sample(self):
        self.observe()
        self.frame(during_ocr=lambda: setattr(self.engine, "_source", object.__new__(WgcWindowSource)))
        # Source replacement in production also invokes _pause_player_status;
        # identity alone must reject the callback if that ordering ever changes.
        self.assertEqual(self.engine.player_stat_snapshot()["max_health"], 100)

    def test_session_reset_during_ocr_requires_a_post_reset_sample(self):
        self.observe()
        with patch.object(self.engine, "_save_session_archive", return_value=True), \
                patch.object(self.engine, "_ensure_archive"):
            self.frame(during_ocr=self.engine.reset_session)
        self.assertIsNone(self.engine.player_stat_snapshot())
        self.assertFalse(self.engine.leaderboard_status()["eligible"])

    def test_pre_pause_frame_messages_do_not_enter_protected_counters(self):
        event = parse_line("You have slain a fictional rat.", time.time(), self.cfg.player_name)
        tracker = Mock()
        tracker.prev, tracker.scrolled_back, tracker._state = [], False, "live"
        tracker.update.return_value = [Message(event.text, event.ts, 2)]
        self.engine._tracker = tracker
        self.engine._stats = Stats(player_name=self.cfg.player_name)
        self.engine._writer = Mock()
        def pause_and_resume():
            self.engine.set_paused(True)
            self.engine.set_paused(False)
        self.frame(during_ocr=pause_and_resume)
        self.assertEqual(self.ledger.status()["personal_kills"], 0)
        self.engine._ocr.read.side_effect = None
        tracker.update.return_value = [Message(event.text, event.ts + 1, 2)]
        self.frame()
        self.assertEqual(self.ledger.status()["personal_kills"], 1)


if __name__ == "__main__":
    unittest.main()
