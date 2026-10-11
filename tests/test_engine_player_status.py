"""Own stat readings stay separate from chat, and HP alerts remain passive."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.app.low_health import LowHealthFlash
from mnmparse.config import Config
from mnmparse.ocr import OcrLine
from mnmparse.player_status import PlayerStatusReading
from mnmparse.tracker import Tracker


class EnginePlayerStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(player_name="Owner", log_dir=self.tmp.name, crop=(0, 0, 8, 8),
                          player_status_separate=False)
        self.engine = Engine(self.cfg)
        self.engine._tracker = Tracker()

    @staticmethod
    def reading(hp=8):
        return PlayerStatusReading(hp, 100, (0, 0, 8, 8), (0,), 20.0, (0, 6, 8, 8), .1)

    def test_second_region_uses_original_pixels_and_never_enters_chat(self):
        cfg = dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=(20, 0, 28, 8), ocr_scale=2)
        chat = np.full((8, 8, 3), (8, 30, 60), np.uint8)
        stats = np.full((8, 8, 3), (2, 100, 200), np.uint8)
        chat_lines = [OcrLine(0, 0, 1, "You begin casting Heal.")]
        stat_lines = [OcrLine(0, 0, 1, "Health 8 / 100")]
        self.engine._ocr = Mock(read=Mock(side_effect=[chat_lines, stat_lines]))
        tracker_update = Mock(return_value=[])
        self.engine._tracker.update = tracker_update
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()) as reader:
            self.engine._process_frame(chat, cfg, cropped=True, status_frame=stats)
        self.assertEqual(tracker_update.call_args.args[0], chat_lines)
        self.assertIs(reader.call_args.args[1], stats)
        self.assertEqual(tuple(stats[0, 0]), (2, 100, 200))
        self.assertEqual(self.engine._ocr.read.call_count, 2)
        processed = self.engine._ocr.read.call_args.args[0]
        self.assertEqual(processed.shape, (12, 12, 3))  # Mini stats enlarge the active 1x OCR input to 1.5x.
        self.assertEqual(tuple(processed[0, 0]), (200, 200, 200))

    def test_stats_calibration_uses_recovery_without_changing_chat_ocr(self):
        frame = np.full((20, 30, 3), (2, 100, 200), np.uint8)
        cfg = dataclasses.replace(self.cfg, crop=(10, 5, 25, 15))
        self.engine._test_ocr = Mock(read=Mock(return_value=[OcrLine(0, 0, 1, "chat")]))
        self.engine._test_ocr_key = (cfg.ocr_engine, cfg.ocr_scale)
        recovered = [OcrLine(0, 0, 1, "Health 8 / 100")]
        with patch("mnmparse.player_status.read_status_crop", return_value=(self.reading(), recovered)) as reader:
            self.assertEqual(self.engine.test_stats_ocr(frame, cfg.crop, cfg), recovered)
            self.assertEqual(self.engine.test_ocr(frame, cfg.crop, cfg)[0].text, "chat")
        reader.assert_called_once()
        self.assertIs(reader.call_args.args[0], self.engine._test_ocr)
        np.testing.assert_array_equal(reader.call_args.args[1], frame[5:15, 10:25])
        self.assertEqual(reader.call_args.args[2:], (cfg.preprocess, cfg.ocr_scale))

    def test_shared_crop_excludes_only_verified_stat_rows(self):
        chat = OcrLine(0, 3, 1, "You begin casting Heal.")
        stats = OcrLine(4, 0, 1, "Health 8 / 100")
        lines = [stats, chat]
        self.engine._ocr = Mock(read=Mock(return_value=lines))
        self.engine._tracker.update = Mock(return_value=[])
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()):
            self.engine._process_frame(np.zeros((8, 8, 3), np.uint8), self.cfg, cropped=True)
        self.assertEqual(self.engine._tracker.update.call_args.args[0], [chat])
        self.assertEqual(self.engine.ocr_diagnosis()["recent_ocr_frames"][-1]["rows"][0]["text"], stats.text)

    def test_low_hp_requires_fresh_confirmation_and_alerts_once(self):
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        signals = []
        self.engine.low_health.connect(signals.append)
        frame = np.zeros((8, 8, 3), np.uint8)
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()), patch(
                "mnmparse.app.engine.time.time", side_effect=[100, 100.25, 100.5, 100.75]):
            self.engine._process_frame(frame, self.cfg, cropped=True)
            self.engine._process_frame(frame, self.cfg, cropped=True, fresh=False)
            self.assertEqual(signals, [])
            self.engine._process_frame(frame, self.cfg, cropped=True)
            self.engine._process_frame(frame, self.cfg, cropped=True)
        self.assertEqual(signals, [8.0])
        self.assertFalse(any(key.startswith("hp_") for key in self.engine.session_snapshot().player_status))
        self.engine.set_paused(True)
        self.assertFalse(self.engine._telemetry().snapshot()["hp_low"])

    def test_invalid_stats_crop_does_not_stop_chat_capture(self):
        self.engine.update_config(dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=(20, 0, 28, 8)))
        source = SimpleNamespace(full_frame_dimensions=(8, 8), frame_count=1,
                                 latest_region=Mock(return_value=np.zeros((8, 8, 3), np.uint8)),
                                 latest_regions=Mock())
        notices = []
        self.engine.notice.connect(notices.append)
        self.engine._process_frame = Mock(side_effect=lambda *a, **k: self.engine._stop_event.set())
        self.engine._capture_loop(source)
        source.latest_region.assert_called_once_with((0, 0, 8, 8))
        source.latest_regions.assert_not_called()
        self.assertEqual(len(notices), 1)
        self.assertIsNone(self.engine._process_frame.call_args.kwargs["status_frame"])

    def test_native_multiple_regions_and_compatibility_fallback_use_one_frame(self):
        cfg = dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=(20, 0, 28, 8))
        self.engine.update_config(cfg)
        full = np.full((8, 32, 3), 23, np.uint8)
        original_chat = np.full((8, 8, 3), 9, np.uint8)
        stats = full[:, 20:28].copy()
        for multiple in (True, False):
            with self.subTest(multiple=multiple):
                self.engine._stop_event.clear()
                source = SimpleNamespace(full_frame_dimensions=(32, 8), frame_count=1,
                                         latest_region=Mock(return_value=original_chat),
                                         latest=Mock(return_value=full))
                if multiple:
                    source.latest_regions = Mock(return_value=(original_chat, stats))
                self.engine._process_frame = Mock(side_effect=lambda *a, **k: self.engine._stop_event.set())
                self.engine._capture_loop(source)
                args, kwargs = self.engine._process_frame.call_args
                self.assertTrue(kwargs["cropped"])
                self.assertTrue(kwargs["fresh"])
                if multiple:
                    source.latest_regions.assert_called_once_with((self.cfg.crop, cfg.player_status_crop))
                    source.latest_region.assert_not_called()
                    source.latest.assert_not_called()
                    self.assertIs(args[0], original_chat)
                    self.assertIs(kwargs["status_frame"], stats)
                else:
                    source.latest.assert_called_once_with()
                    self.assertEqual(int(args[0][0, 0, 0]), 23)
                    self.assertEqual(int(kwargs["status_frame"][0, 0, 0]), 23)

    def test_optional_reader_failure_keeps_chat_and_warns_once(self):
        cfg = dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=(20, 0, 28, 8))
        chat = [OcrLine(0, 0, 1, "You begin casting Heal.")]
        frame = np.zeros((8, 8, 3), np.uint8)
        self.engine._ocr = Mock(read=Mock(side_effect=[chat, RuntimeError("stats OCR unavailable"),
                                                     chat, RuntimeError("stats OCR unavailable")]))
        self.engine._tracker.update = Mock(return_value=[])
        notices = []
        self.engine.notice.connect(notices.append)
        with self.assertLogs("mnmparse.app.engine", level="WARNING"):
            self.engine._process_frame(frame, cfg, cropped=True, status_frame=frame)
            self.engine._process_frame(frame, cfg, cropped=True, status_frame=frame)
        self.assertEqual(self.engine._tracker.update.call_count, 2)
        self.assertTrue(all(call.args[0] == chat for call in self.engine._tracker.update.call_args_list))
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.engine.session_snapshot().player_status, {})

    def test_small_ocr_position_jitter_does_not_reset_low_hp_confirmation(self):
        first = dataclasses.replace(self.reading(), panel_rect=(10, 3, 319, 248))
        shifted = dataclasses.replace(first, panel_rect=(13, 5, 322, 250))
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        signals = []
        self.engine.low_health.connect(signals.append)
        with patch("mnmparse.player_status.read_player_status", side_effect=[first, shifted]), patch(
                "mnmparse.app.engine.time.time", side_effect=[100, 100.25]):
            for _ in range(2):
                self.engine._process_frame(np.zeros((8, 8, 3), np.uint8), self.cfg, cropped=True)
        self.assertEqual(signals, [8.0])

    def test_one_pixel_label_height_change_preserves_actual_panel_confirmation(self):
        signals = []
        self.engine.low_health.connect(signals.append)
        first = PlayerStatusReading(73, 767, (10, 3, 319, 248), (), 7.0, (37, 212, 276, 231), .08368)
        second = dataclasses.replace(first, panel_rect=(17, 7, 307, 248))
        for now, reading in ((100, first), (100.25, second), (100.5, first)):
            self.engine._observe_player_status(reading, self.cfg, now, (248, 324, 3))
        self.assertEqual(signals, [round(100 * 73 / 767, 6)])
        self.assertIsNotNone(self.engine._telemetry()._xp_baseline)
        # A brief OCR miss resets HP confirmation while preserving the XP baseline.
        self.engine._observe_player_status(None, self.cfg, 100.75, (248, 324, 3))
        self.engine._observe_player_status(first, self.cfg, 101, (248, 324, 3))
        self.assertFalse(self.engine._telemetry().snapshot()["hp_low"])
        self.assertIsNotNone(self.engine._telemetry()._xp_baseline)
        # A different, distant rectangle needs a new baseline.
        distant = dataclasses.replace(first, panel_rect=(100, 3, 324, 248))
        self.engine._observe_player_status(distant, self.cfg, 101.25, (248, 324, 3))
        self.assertFalse(self.engine._telemetry().snapshot()["hp_low"])
        self.assertIsNone(self.engine._telemetry()._xp_baseline)

    def test_short_panel_or_bar_ocr_misses_do_not_restart_five_minute_xp_comparison(self):
        for missing in (None, dataclasses.replace(self.reading(), xp_percent=None, xp_bar_rect=None)):
            with self.subTest(missing=missing):
                engine = Engine(self.cfg)
                for now in range(100, 402):
                    value = missing if now in (150, 200, 250, 300, 350) else dataclasses.replace(
                        self.reading(), xp_percent=22.0 if now >= 390 else 20.0)
                    engine._observe_player_status(value, self.cfg, now, (8, 8, 3))
                    if now in (151, 201, 251, 301, 351) and missing is None:
                        self.assertFalse(engine._telemetry().snapshot()["hp_low"])
                snap = engine._telemetry().snapshot()
                self.assertEqual(snap["xp_observed_seconds"], 300)
                self.assertAlmostEqual(snap["xp_points_per_hour"], 24)
                engine._observe_player_status(None, self.cfg, 402, (8, 8, 3))
                engine._observe_player_status(None, self.cfg, 404, (8, 8, 3))
                self.assertIsNone(engine._telemetry()._xp_baseline)
                self.assertIsNone(engine._telemetry().snapshot()["xp_points_per_hour"])
                self.assertEqual(engine._telemetry().snapshot()["xp_gain_points"], 2)

    def test_unreadable_health_does_not_stop_xp_or_invent_low_health_alerts(self):
        signals = []
        self.engine.low_health.connect(signals.append)
        for now in range(100, 402):
            value = dataclasses.replace(self.reading(), hp_current=None, hp_max=None,
                                        xp_percent=22.0 if now >= 390 else 20.0)
            self.engine._observe_player_status(value, self.cfg, now, (8, 8, 3))
        snap = self.engine._telemetry().snapshot()
        self.assertEqual(snap["xp_observed_seconds"], 300)
        self.assertAlmostEqual(snap["xp_points_per_hour"], 24)
        self.assertEqual(signals, [])
        self.assertIsNone(snap["hp_percent"])
        self.assertFalse(snap["hp_low"])
        self.engine._observe_player_status(self.reading(), self.cfg, 402, (8, 8, 3))
        self.assertEqual(signals, [])
        self.engine._observe_player_status(self.reading(), self.cfg, 403, (8, 8, 3))
        self.assertEqual(signals, [8.0])

    def test_slow_cadence_ocr_miss_tolerates_small_scheduling_jitter(self):
        for fps, ocr_ms in ((1 / 3, 0), (4, 3000)):
            with self.subTest(fps=fps, ocr_ms=ocr_ms):
                cfg = dataclasses.replace(self.cfg, fps=fps)
                engine = Engine(cfg)
                for now, value in ((100, self.reading()), (103.02, self.reading()),
                                   (106.04, None), (109.06, self.reading())):
                    engine._observe_player_status(value, cfg, now, (8, 8, 3), ocr_ms=ocr_ms)
                self.assertEqual(engine._telemetry()._xp_baseline, (103.02, .2))
                self.assertFalse(engine._telemetry().snapshot()["hp_low"])
                engine._observe_player_status(self.reading(), cfg, 112.08, (8, 8, 3), ocr_ms=ocr_ms)
                self.assertTrue(engine._telemetry().snapshot()["hp_low"])

    def test_slow_ocr_and_poll_cadences_confirm_health_and_complete_xp_window(self):
        for fps, ocr_ms in ((4, 1500), (1 / 3, 0)):
            with self.subTest(fps=fps, ocr_ms=ocr_ms):
                cfg = dataclasses.replace(self.cfg, fps=fps)
                engine = Engine(cfg)
                signals = []
                engine.low_health.connect(signals.append)
                for now in range(100, 404, 3):
                    reading = dataclasses.replace(self.reading(), xp_percent=22.0 if now >= 400 else 20.0)
                    engine._observe_player_status(reading, cfg, now, (8, 8, 3), ocr_ms=ocr_ms)
                    if now == 100:
                        self.assertEqual(signals, [])
                self.assertEqual(signals, [8.0])
                snap = engine._telemetry().snapshot()
                self.assertEqual(snap["xp_observed_seconds"], 300)
                self.assertAlmostEqual(snap["xp_points_per_hour"], 24)
                # A very slow pass cannot grant an unbounded continuity budget.
                engine._observe_player_status(reading, cfg, 414, (8, 8, 3), ocr_ms=60_000)
                self.assertFalse(engine._telemetry().snapshot()["hp_low"])
                self.assertIsNone(engine._telemetry()._xp_baseline)

    def test_pending_separate_region_does_not_read_stats_or_hide_chat(self):
        cfg = dataclasses.replace(self.cfg, player_status_separate=True, player_status_crop=None)
        chat = [OcrLine(0, 0, 1, "You begin casting Heal.")]
        self.engine._ocr = Mock(read=Mock(return_value=chat))
        self.engine._tracker.update = Mock(return_value=[])
        frame = np.zeros((8, 8, 3), np.uint8)
        with patch("mnmparse.player_status.read_player_status") as reader:
            self.engine._process_frame(frame, cfg, cropped=True, status_frame=frame)
        reader.assert_not_called()
        self.assertEqual(self.engine._ocr.read.call_count, 1)
        self.assertEqual(self.engine._tracker.update.call_args.args[0], chat)
        self.assertEqual(self.engine.session_snapshot().player_status, {})
        self.assertIsNone(self.engine._status_crop_for_source(None, cfg))

    def test_explicit_shared_mode_ignores_retained_separate_rectangle(self):
        cfg = dataclasses.replace(self.cfg, player_status_crop=(20, 0, 28, 8))
        self.assertIsNone(self.engine._status_crop_for_source(None, cfg))
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        frame = np.zeros((8, 8, 3), np.uint8)
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()) as reader:
            self.engine._process_frame(frame, cfg, cropped=True)
        self.assertIs(reader.call_args.args[1], frame)
        self.assertEqual(self.engine._ocr.read.call_count, 1)

    def test_threshold_change_and_reenable_require_fresh_confirmation_preserve_xp(self):
        cfg = dataclasses.replace(self.cfg, low_health_threshold=25)
        self.engine.update_config(cfg)
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        frame = np.zeros((8, 8, 3), np.uint8)
        signals = []
        self.engine.low_health.connect(signals.append)
        now = [100.0]
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading(hp=20)), patch(
                "mnmparse.app.engine.time.time", side_effect=lambda: now[0]):
            self.engine._process_frame(frame, cfg, cropped=True)
            now[0] += .25
            self.engine._process_frame(frame, cfg, cropped=True)
            self.assertEqual(signals, [20.0])
            baseline = self.engine._telemetry()._xp_baseline
            cfg = dataclasses.replace(cfg, low_health_threshold=30)
            self.engine.update_config(cfg)
            self.assertEqual(self.engine._telemetry()._xp_baseline, baseline)
            now[0] += .25
            self.engine._process_frame(frame, cfg, cropped=True)
            self.assertEqual(signals, [20.0])
            now[0] += .25
            self.engine._process_frame(frame, cfg, cropped=True)
            self.assertEqual(signals, [20.0, 20.0])
            self.engine.update_config(dataclasses.replace(cfg, low_health_enabled=False))
            now[0] += .25
            self.engine._process_frame(frame, self.engine._cfg, cropped=True)
            self.engine.update_config(cfg)
            now[0] += .25
            self.engine._process_frame(frame, cfg, cropped=True)
            self.assertEqual(signals, [20.0, 20.0])
            now[0] += .25
            self.engine._process_frame(frame, cfg, cropped=True)
            self.assertEqual(signals, [20.0, 20.0, 20.0])
            self.assertEqual(self.engine._telemetry()._xp_baseline, baseline)

    def test_session_owner_change_restarts_live_baseline_and_hp_confirmation(self):
        from mnmparse.session import SessionStats

        frame = np.zeros((8, 8, 3), np.uint8)
        self.engine._ocr = Mock(read=Mock(return_value=[]))
        signals = []
        self.engine.low_health.connect(signals.append)
        now = [100.0]
        with patch("mnmparse.player_status.read_player_status", return_value=self.reading()), patch(
                "mnmparse.app.engine.time.time", side_effect=lambda: now[0]):
            self.engine._process_frame(frame, self.cfg, cropped=True)
            now[0] = 100.25
            self.engine._process_frame(frame, self.cfg, cropped=True)
            previous = self.engine._telemetry()
            saved = self.engine.session_snapshot().player_status
            self.engine._session_stats = SessionStats("Owner")
            self.engine._session_stats.note_player_status(saved)
            now[0] = 111
            self.engine._process_frame(frame, self.cfg, cropped=True)
            self.assertIsNot(self.engine._telemetry(), previous)
            self.assertEqual(signals, [8.0])
            self.assertFalse(self.engine._telemetry().snapshot()["hp_low"])
            now[0] = 111.25
            self.engine._process_frame(frame, self.cfg, cropped=True)
        self.assertEqual(signals, [8.0, 8.0])
        self.assertFalse(any(key.startswith("hp_") for key in self.engine.session_snapshot().player_status))

    def test_flash_has_transparent_center_ignores_input_and_finishes(self):
        flash = LowHealthFlash()
        self.addCleanup(flash.close)
        flash.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        with patch("mnmparse.app.low_health.time.monotonic", return_value=100):
            flash.flash(QRect(0, 0, 400, 250), reduced_motion=True)
            image = flash.grab().toImage()
        self.assertEqual(image.pixelColor(200, 125).alpha(), 0)
        self.assertGreater(image.pixelColor(1, 125).alpha(), 0)
        self.assertTrue(flash.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(flash.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertEqual(flash.focusPolicy(), Qt.FocusPolicy.NoFocus)
        with patch("mnmparse.app.low_health.time.monotonic", return_value=102):
            flash._tick()
        self.assertTrue(flash.isHidden())
        self.assertFalse(flash._timer.isActive())
