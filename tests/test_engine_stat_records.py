"""New stat fields follow the existing fresh Window Capture evidence boundary."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from types import SimpleNamespace
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
from mnmparse.leaderboard import READING_FIELDS
from mnmparse.ocr import OcrLine
from mnmparse.player_status import PlayerStatusReading
from mnmparse.protected_stats import ProtectedLedger
from mnmparse.tracker import Tracker


@unittest.skipUnless(os.name == "nt", "Windows protected live reader")
class EngineStatRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name="FictionalOwner", log_dir=self.temporary.name, crop=(0, 0, 8, 8),
                          player_status_separate=False)
        self.engine = Engine(self.cfg)
        self.ledger = ProtectedLedger(Path(self.temporary.name) / "fixture.bin")
        self.engine.set_protected_ledger(self.ledger)
        self.engine._source = object.__new__(WgcWindowSource)
        self.engine._state = "running"
        self.engine._window_found = True

    def tearDown(self):
        self.engine._source = None
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temporary.cleanup()

    def reading(self, number=1, **changes):
        base = dataclasses.asdict(PlayerStatusReading(10, 100, (0, 0, 8, 8), (), mana_max=300))
        return SimpleNamespace(**{**base, **dict.fromkeys(READING_FIELDS, number), **changes}, hp_percent=10.0)

    def observe(self, reading):
        self.engine._observe_player_status(reading, self.engine._cfg, time.time(), (8, 8, 3))

    def test_current_snapshot_submission_and_local_records_are_distinct(self):
        self.observe(self.reading(3, strength=100, resist_cold=100))
        self.observe(self.reading(2, strength=0, armor_class=-7, resist_cold=0))
        current = self.engine.player_stat_snapshot()
        self.assertEqual(current["strength"], 0)
        self.assertEqual(current["armor_class"], -7)
        self.assertEqual(current["stat_total"], 12)
        self.assertEqual(current["resist_total"], 14)
        submitted = self.engine.leaderboard_submission()
        self.assertEqual(submitted["schema_version"], 3)
        self.assertEqual((submitted["strength"], submitted["stat_total"], submitted["resist_total"]), (100, 118, 121))
        copied = self.engine.leaderboard_copy_text()
        self.assertIn("Strength: 0", copied)
        self.assertIn("Stat Total: 12", copied)
        self.assertIn("Resist Total: 14", copied)
        self.assertEqual(self.engine.leaderboard_submission(message="  SHIT! 🐉  ")["message"], "****! 🐉")
        records = self.engine.leaderboard_status()["records"]
        self.assertEqual(records["strength"]["value"], 100)
        self.assertEqual(records["stat_total"]["value"], 118)
        self.assertEqual(records["max_health"]["value"], 100)
        self.assertEqual(records["max_mana"]["value"], 300)
        self.assertNotIn("strength", json.dumps(self.engine._session_stats.player_status))

    def test_desktop_copy_uses_current_stats_without_creating_local_records(self):
        self.engine._source = object.__new__(MssRegionSource)
        self.engine.update_config(dataclasses.replace(self.cfg, capture_backend="mss"))
        self.observe(self.reading(0))
        self.assertEqual(self.engine.leaderboard_status()["records"], {})
        self.assertIn("Stat Total: 0", self.engine.leaderboard_copy_text())
        self.assertIn("Resist Total: 0", self.engine.leaderboard_copy_text())
        with self.assertRaises(ValueError):
            self.engine.leaderboard_submission()

    def test_inflight_ocr_from_before_pause_cannot_update_any_local_high(self):
        self.observe(self.reading(2))
        before = self.ledger.status()["records"]
        self.engine._ocr = Mock()
        self.engine._tracker = Tracker()

        def pause_during_ocr(_image):
            self.engine.set_paused(True)
            self.engine.set_paused(False)
            return []

        self.engine._ocr.read.side_effect = pause_during_ocr
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading(99999)):
            self.engine._process_frame(np.zeros((8, 8, 3), dtype=np.uint8), self.cfg)
        self.assertEqual(self.ledger.status()["records"], before)
        self.assertIsNone(self.engine.player_stat_snapshot())
        self.assertFalse(self.engine.leaderboard_status()["eligible"])

    def test_verified_shared_stat_rows_are_absent_from_plaintext_ocr_diagnostics(self):
        self.engine._ocr = Mock()
        self.engine._ocr.language = "fixture"
        self.engine._tracker = Tracker()
        self.engine._ocr.read.return_value = [OcrLine(1, 1, 1, "STR 1234567"),
                                              OcrLine(1, 2, 1, "Cold 7654321"),
                                              OcrLine(1, 5, 1, "You say, 'Fictional chat remains.'")]
        reading = self.reading(1, panel_line_indices=(0, 1), strength=1234567, resist_cold=7654321)
        with patch("mnmparse.player_status.read_player_status", return_value=reading):
            self.engine._process_frame(np.zeros((8, 8, 3), dtype=np.uint8), self.cfg)
        diagnosis = self.engine.ocr_diagnosis()
        frame = diagnosis["recent_ocr_frames"][-1]
        self.assertEqual(frame["row_count"], 1)
        self.assertFalse(frame["rows_truncated"])
        self.assertEqual(frame["rows"][0]["text"], "You say, 'Fictional chat remains.'")
        self.assertEqual(frame["rows"][0]["y"], 5)
        serialized = json.dumps(diagnosis)
        for private in ("1234567", "7654321", "strength", "resist_cold", "stat_total"):
            self.assertNotIn(private, serialized)


if __name__ == "__main__":
    unittest.main()
