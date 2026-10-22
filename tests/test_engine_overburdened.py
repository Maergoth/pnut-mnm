"""Fresh own-weight confirmation, persistent episodes and monitoring resets."""
import dataclasses
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.config import Config
from mnmparse.player_status import PlayerStatusReading
from mnmparse.session import SessionStats
from mnmparse.tracker import Tracker


class EngineOverburdenedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = Config(log_dir=self.temp.name, crop=(0, 0, 8, 8), player_status_separate=False)
        self.engine = Engine(self.cfg)
        self.states = []
        self.engine.overburdened_changed.connect(self.states.append)

    @staticmethod
    def reading(current=150, maximum=122):
        return PlayerStatusReading(None, None, (0, 0, 8, 8), (), weight_current=current, weight_max=maximum)

    def observe(self, reading=None, now=100):
        self.engine._observe_player_status(reading, self.engine.config, now, (8, 8, 3))

    def confirm(self, start=100):
        self.observe(self.reading(), start)
        self.observe(self.reading(151), start + .25)
        self.assertTrue(self.engine.overburdened_active)

    def test_two_fresh_consistent_readings_confirm_without_audio_or_saved_live_state(self):
        alerts = []
        self.engine.low_health.connect(alerts.append)
        self.observe(self.reading(), 100)
        self.observe(self.reading(), 100)
        self.assertFalse(self.engine.overburdened_active)
        self.observe(self.reading(151), 100.25)
        self.observe(self.reading(152), 100.5)
        self.assertEqual(self.states, [True])
        self.assertFalse(alerts)
        self.assertFalse(any(key.startswith("weight") or key == "overburdened_active"
                             for key in self.engine.session_snapshot().player_status))

    def test_misses_and_stale_samples_cannot_clear_or_activate_an_episode(self):
        self.observe(self.reading(), 100)
        self.observe(None, 100.25)
        self.observe(self.reading(), 100.1)  # Old work cannot count after a newer miss.
        self.observe(self.reading(), 100.5)
        self.assertFalse(self.engine.overburdened_active)
        self.observe(self.reading(), 100.75)
        self.assertTrue(self.engine.overburdened_active)
        for now, reading in ((101, None), (105, self.reading(None)), (110, self.reading(1, 0)),
                             (111, self.reading(True)), (112, self.reading(1_000_000_001))):
            self.observe(reading, now)
        self.observe(self.reading(0), 111.5)
        self.assertEqual(self.states, [True])
        self.observe(self.reading(122), 112.25)
        self.assertEqual(self.states, [True, False])

    def test_confirmation_resets_after_capacity_change_gap_or_miss(self):
        for first, second, start in ((self.reading(), self.reading(160, 130), 100),
                                      (self.reading(), self.reading(), 110)):
            self.engine._pause_player_status()
            self.observe(first, start)
            second_time = start + (3 if start == 110 else .25)
            self.observe(second, second_time)
            self.assertFalse(self.engine.overburdened_active)
            self.observe(second, second_time + .25)
            self.assertTrue(self.engine.overburdened_active)

    def test_monitor_resets_and_session_replacement_require_new_confirmation(self):
        self.confirm()
        self.engine.set_paused(True)
        self.assertFalse(self.engine.overburdened_active)
        self.engine.set_paused(False)
        self.confirm(101)
        self.engine._close_source()
        self.assertFalse(self.engine.overburdened_active)
        self.confirm(102)
        self.engine._session_stats = SessionStats("")
        self.engine._telemetry()
        self.assertFalse(self.engine.overburdened_active)
        self.observe(self.reading(), 103)
        self.assertFalse(self.engine.overburdened_active)
        self.confirm(104)
        self.engine.stop()
        self.assertFalse(self.engine.overburdened_active)

    def test_display_flag_and_health_preferences_do_not_disable_weight_tracking(self):
        self.engine.update_config(dataclasses.replace(self.cfg, overburdened_flash=False, low_health_enabled=False))
        self.confirm()
        self.engine.update_config(dataclasses.replace(self.engine.config, overburdened_flash=True, low_health_threshold=20))
        self.assertTrue(self.engine.overburdened_active)
        self.engine.update_config(dataclasses.replace(self.engine.config, player_status_enabled=False))
        self.assertFalse(self.engine.overburdened_active)
        self.observe(self.reading(), 101)
        self.observe(self.reading(), 101.25)
        self.assertFalse(self.engine.overburdened_active)

    def test_crop_and_actual_panel_changes_clear_active_state(self):
        self.confirm()
        self.engine.update_config(dataclasses.replace(self.cfg, player_status_crop=(1, 1, 9, 9)))
        self.assertFalse(self.engine.overburdened_active)
        self.confirm(101)
        distant = dataclasses.replace(self.reading(), panel_rect=(40, 0, 48, 8))
        self.observe(distant, 102)
        self.assertFalse(self.engine.overburdened_active)
        self.observe(distant, 102.25)
        self.assertTrue(self.engine.overburdened_active)

    def test_backend_or_window_identity_changes_require_new_confirmation(self):
        for changes in ({"capture_backend": "mss"}, {"window_title": "Other Game Window"}):
            with self.subTest(changes=changes):
                self.confirm()
                self.engine.update_config(dataclasses.replace(self.engine.config, **changes))
                self.assertFalse(self.engine.overburdened_active)
                self.observe(self.reading(), 101)
                self.assertFalse(self.engine.overburdened_active)
                self.observe(self.reading(), 101.25)
                self.assertTrue(self.engine.overburdened_active)
                self.engine._pause_player_status()

    def test_repeated_capture_frame_is_not_confirmation_and_diagnostics_include_weight(self):
        self.engine._tracker = Tracker()
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        frame = np.zeros((8, 8, 3), np.uint8)
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()), patch(
                "mnmparse.app.engine.time.time", side_effect=[100, 100.25, 100.5]):
            self.engine._process_frame(frame, self.cfg, cropped=True)
            self.engine._process_frame(frame, self.cfg, cropped=True, fresh=False)
            self.assertFalse(self.engine.overburdened_active)
            self.engine._process_frame(frame, self.cfg, cropped=True)
        self.assertEqual(self.states, [True])
        status = self.engine.ocr_diagnosis()["recent_ocr_frames"][-1]["player_status"]
        self.assertEqual((status["weight_current"], status["weight_max"]), (150, 122))
        self.assertTrue(status["overburdened_active"])

    def test_read_area_snapshot_uses_only_active_geometry_and_never_copies_frames(self):
        cfg = dataclasses.replace(self.cfg, crop=(10, 20, 18, 28))
        self.engine.update_config(cfg)
        self.engine._state = "running"
        source = self.engine._source = SimpleNamespace(
            hwnd=123, full_frame_dimensions=(100, 80), window=SimpleNamespace(rect=(-100, 20, 0, 100)),
            latest=Mock(), latest_region=Mock())
        self.engine._observe_player_status(self.reading(), cfg, 100, (8, 8, 3))
        with patch("mnmparse.app.engine.time.time", return_value=100.5):
            snapshot = self.engine.read_area_snapshot()
        self.assertEqual(snapshot, {"hwnd": 123, "frame_dimensions": (100, 80), "capture_backend": "wgc",
                                    "chat_crop": (10, 20, 18, 28), "stats_crop": (10, 20, 18, 28),
                                    "source_rect": (-100, 20, 0, 100)})
        source.latest.assert_not_called()
        source.latest_region.assert_not_called()
        self.engine._observe_player_status(None, cfg, 100.75, (8, 8, 3))
        with patch("mnmparse.app.engine.time.time", return_value=103):
            self.assertIsNone(self.engine.read_area_snapshot()["stats_crop"])
        self.engine.set_paused(True)
        self.assertEqual(self.engine.read_area_snapshot(), {})

    def test_separate_read_area_snapshot_validates_region_and_has_no_stale_inline_fallback(self):
        self.engine._state = "running"
        self.engine._source = SimpleNamespace(hwnd=123, full_frame_dimensions=(100, 80), _rect=(1, 2, 101, 82))
        cfg = dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=(20, 30, 40, 60))
        self.engine.update_config(cfg)
        self.assertEqual(self.engine.read_area_snapshot()["stats_crop"], (20, 30, 40, 60))
        self.engine.update_config(dataclasses.replace(cfg, player_status_crop=(20, 30, 400, 60)))
        self.assertIsNone(self.engine.read_area_snapshot()["stats_crop"])
        self.engine.update_config(dataclasses.replace(cfg, player_status_enabled=False))
        self.assertIsNone(self.engine.read_area_snapshot()["stats_crop"])
        self.engine._source = None
        self.assertEqual(self.engine.read_area_snapshot(), {})

    def test_read_area_snapshot_rejects_unknown_window_invalid_dimensions_and_inactive_capture(self):
        self.assertEqual(self.engine.read_area_snapshot(), {})
        self.engine._state = "running"
        self.engine._source = SimpleNamespace(hwnd=None, full_frame_dimensions=(100, 80))
        self.assertEqual(self.engine.read_area_snapshot(), {})
        self.engine._source.hwnd = 123
        self.engine._source.full_frame_dimensions = (0, 80)
        self.assertEqual(self.engine.read_area_snapshot(), {})
        self.engine._source.full_frame_dimensions = None
        self.engine._source_dimensions = (100, 80)
        self.assertEqual(self.engine.read_area_snapshot()["frame_dimensions"], (100, 80))
        self.engine._stop_event.set()
        self.assertEqual(self.engine.read_area_snapshot(), {})

    def test_read_area_snapshot_never_waits_for_a_lock_held_by_another_thread(self):
        held, release, returned = threading.Event(), threading.Event(), threading.Event()
        snapshot = []

        def holder():
            with self.engine._lock:
                held.set()
                release.wait(3)

        def poller():
            snapshot.append(self.engine.read_area_snapshot())
            returned.set()

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(held.wait(1))
        poller_thread = threading.Thread(target=poller)
        poller_thread.start()
        try:
            self.assertTrue(returned.wait(.5), "GUI polling must return while the engine lock is still held")
            self.assertEqual(snapshot, [{}])
        finally:
            release.set()
            holder_thread.join(2)
            poller_thread.join(2)
