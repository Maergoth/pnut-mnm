"""Confirmed screen-reader XP losses reach durable session totals and rates."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.player_status import PlayerStatusReading
from mnmparse.stats import Stats
from mnmparse.tracker import Message, Tracker


class EngineXPLossTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.now = 100.0
        clock = patch("mnmparse.app.engine.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.cfg = Config(player_name="Owner", log_dir=str(self.folder), crop=(0, 0, 8, 8),
                          fps=4, player_status_enabled=True, player_status_xp=True,
                          player_status_separate=False, low_health_enabled=False)
        self.pixels = np.zeros((8, 8, 3), np.uint8)

    def engine(self, cfg=None):
        engine = Engine(cfg or self.cfg)
        engine._tracker = Tracker()
        engine._ocr = Mock(read=Mock(return_value=[]))
        engine._stats = Stats(player_name="Owner")
        engine._writer = Mock()  # Confirmed chat uses production parsing, without writing raw logs.
        return engine

    def frame(self, engine, now, percent, *, fresh=True):
        self.now = now
        reading = PlayerStatusReading(100, 100, (0, 0, 8, 8), (), percent, (0, 6, 8, 8), .1)
        # Stub recognition only. The real Engine frame observer, freshness gate,
        # two-read telemetry and SessionStats sanitizer remain in this path.
        with patch("mnmparse.player_status.read_player_status", return_value=reading):
            engine._process_frame(self.pixels, engine.config, cropped=True, fresh=fresh)

    def sample(self, engine, start, end, percent):
        for tick in range(round(start * 4), round(end * 4) + 1):
            self.frame(engine, tick / 4, percent)

    def death(self, engine):
        self.sample(engine, 100, 120.75, 80)
        self.frame(engine, 121, 60)
        self.assertEqual(engine.session_snapshot().player_status["xp_percent"], 80)
        self.frame(engine, 121.25, 60, fresh=False)
        self.assertEqual(engine.session_snapshot().player_status["xp_percent"], 80)
        self.frame(engine, 121.5, 60)
        self.assertEqual(engine.session_snapshot().player_status["xp_percent"], 60)
        self.assertEqual(engine.session_snapshot().player_status["xp_gain_points"], 0)
        self.sample(engine, 121.75, 130.25, 60)

    def test_fresh_two_read_loss_propagates_to_signed_session_and_hourly_rate(self):
        engine = self.engine()
        self.death(engine)
        summary = engine.session_snapshot().player_status
        self.assertEqual(summary["xp_gain_points"], -20)
        self.assertEqual(summary["xp_rate_gain_points"], -20)
        self.assertEqual(summary["xp_points_per_hour"], -2400)
        self.assertEqual(summary["xp_observed_seconds"], 30)
        self.assertEqual(summary["xp_level_ups"], 0)

    def test_only_own_level_up_chat_turns_a_bar_wrap_into_positive_progress(self):
        for actor, expected_gain, expected_levels in (("You", 4, 1), ("Friend", 0, 0)):
            with self.subTest(actor=actor):
                self.now = 100
                engine = self.engine(dataclasses.replace(self.cfg, log_dir=str(self.folder / actor)))
                self.sample(engine, 100, 124.75, 98)
                text = f"{actor} have leveled up! They are now level 7!" if actor == "You" else (
                    "Friend has leveled up! They are now level 7!")
                event = parse_line(text, 125, "Owner")
                self.assertEqual(event.kind, "level_up")
                self.assertEqual(event.actor, "Owner" if actor == "You" else "Friend")
                self.now = 125
                engine._handle_message(Message(text, 125, 2), engine.config)
                self.sample(engine, 125, 140.25, 2)
                summary = engine.session_snapshot().player_status
                self.assertEqual(summary["xp_gain_points"], expected_gain)
                self.assertEqual(summary["xp_level_ups"], expected_levels)
                if actor == "You":
                    self.assertEqual(summary["xp_points_per_hour"], 480)
                else:
                    self.assertIsNone(summary["xp_points_per_hour"])

    def test_pause_and_archive_restart_preserve_loss_without_bridging_unknown_xp(self):
        for restart in (False, True):
            with self.subTest(restart=restart):
                self.now = 100
                cfg = dataclasses.replace(self.cfg, log_dir=str(self.folder / str(restart)))
                engine = self.engine(cfg)
                self.death(engine)
                engine.set_paused(True)
                self.assertEqual(engine.session_snapshot().player_status["xp_gain_points"], -20)
                self.assertIsNone(engine.session_snapshot().player_status["xp_points_per_hour"])
                if restart:
                    self.assertTrue(engine._save_session_archive(force=True))
                    engine = self.engine(cfg)  # Production startup restores the active archive.
                    self.assertEqual(engine.session_snapshot().player_status["xp_gain_points"], -20)
                else:
                    engine.set_paused(False)
                # The unseen jump from60% to90% is a fresh baseline, not a rez
                # or other measured gain. Only the new stable interval counts.
                self.sample(engine, 200, 230.25, 90)
                summary = engine.session_snapshot().player_status
                self.assertEqual(summary["xp_gain_points"], -20)
                self.assertEqual(summary["xp_observed_seconds"], 60)
                self.assertEqual(summary["xp_rate_gain_points"], 0)
                self.assertEqual(summary["xp_points_per_hour"], 0)


if __name__ == "__main__":
    unittest.main()
