"""Live chat and mini-stat OCR progress independently without weaker evidence."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.app.stat_reader_worker import StatReaderWorker
from mnmparse.app.stat_reader_worker import StatReadResult
from mnmparse.config import Config
from mnmparse.ocr import OcrLine
from mnmparse.parser import parse_line
from mnmparse.player_status import PlayerStatusReading
from mnmparse.trigger_presets import RESIST_PATTERN
from mnmparse.triggers import Trigger, match_trigger


class _AdvancingSource:
    """Synthetic captures only: each copied region has new frame evidence."""

    full_frame_dimensions = (256, 80)
    is_running = True

    def __init__(self):
        self._lock = threading.Lock()
        self._count = 0
        self.latest_frame_time = time.monotonic()
        self.advance = True

    @property
    def frame_count(self):
        with self._lock:
            return self._count

    def latest_region(self, rect):
        with self._lock:
            self._count += 1
            self.latest_frame_time = time.monotonic()
        return np.zeros((rect[3] - rect[1], rect[2] - rect[0], 3), np.uint8)

    def latest_regions(self, rects):
        return tuple(self.latest_region(rect) for rect in rects)

    def region_snapshot(self, rect):
        with self._lock:
            if self.advance:
                self._count += 1
                self.latest_frame_time = time.monotonic()
            count, captured_at = self._count, self.latest_frame_time
        image = np.zeros((rect[3] - rect[1], rect[2] - rect[0], 3), np.uint8)
        return image, count, captured_at

    def stop(self):
        self.is_running = False


class AsyncStatsEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cfg = Config(player_name="FixtureOwner", log_dir=self.temporary.name,
                          crop=(0, 0, 128, 80), fps=20,
                          player_status_separate=True,
                          player_status_crop=(128, 0, 256, 80))
        self.engine = Engine(self.cfg)
        self.source = _AdvancingSource()
        self.workers = []

    @staticmethod
    def reading():
        return PlayerStatusReading(8, 100, (0, 0, 128, 80), (),
                                   weight_current=101, weight_max=100)

    @staticmethod
    def lines(texts):
        return [OcrLine(1, 5 + index * 16, 12, text)
                for index, text in enumerate(texts)]

    def start_pipeline(self, chat_read, stat_read):
        """Use the production engine and worker with synthetic OCR and captures."""
        chat_ocr = type("ChatOcr", (), {"read": staticmethod(chat_read),
                                       "close": lambda self: None})()
        stat_ocr = type("StatOcr", (), {"close": lambda self: None})()
        original_init = StatReaderWorker.__init__
        workers = self.workers

        def worker_init(worker, **options):
            options.update(factory=lambda _cfg: stat_ocr, read=stat_read)
            original_init(worker, **options)
            workers.append(worker)

        def open_source():
            with self.engine._lock:
                self.engine._source = self.source
                self.engine._window_found = True
            return self.source

        self.engine._open_source = open_source
        make_ocr = patch("mnmparse.ocr.make_engine", return_value=chat_ocr)
        worker_factory = patch.object(StatReaderWorker, "__init__", worker_init)
        make_ocr.start()
        worker_factory.start()
        self.addCleanup(make_ocr.stop)
        self.addCleanup(worker_factory.stop)
        self.engine.start()

    def stop_pipeline(self):
        self.engine.stop()
        self.assertTrue(self.engine.wait_stopped(3), "engine did not release its OCR workers")
        self.assertTrue(all(worker.wait_stopped(1) for worker in self.workers))

    def next_stat_result(self):
        """Create evidence through the production provider, without native capture."""
        self.engine._source = self.source
        self.engine._state = "running"
        self.engine._window_found = True
        self.engine._stat_sampling_active = True
        request = self.engine._next_stat_request()
        self.assertIsNotNone(request)
        return StatReadResult(request.captured_at, request.context,
                              tuple(request.crop.shape), self.reading(), .1)

    def test_inflight_stats_are_discarded_across_capture_and_reader_transitions(self):
        transitions = {
            "pause and resume": lambda: (self.engine.set_paused(True), self.engine.set_paused(False)),
            "reader round trip": lambda: (
                self.engine.update_config(dataclasses.replace(self.cfg, player_status_crop=(127, 0, 255, 80))),
                self.engine.update_config(self.cfg)),
            "source closed": lambda: self.engine._close_source(),
            "native capture ended": lambda: setattr(self.source, "is_running", False),
            "stop": lambda: self.engine.stop(),
            "session replaced": lambda: setattr(self.engine, "_session_stats", object()),
        }
        for name, transition in transitions.items():
            with self.subTest(transition=name):
                # Each transition gets a fresh engine and capture identity.
                self.engine = Engine(self.cfg)
                self.source = _AdvancingSource()
                result = self.next_stat_result()
                self.engine._observe_player_status = Mock()
                transition()
                self.engine._accept_stat_result(result)
                self.engine._observe_player_status.assert_not_called()

    def test_optional_stat_worker_start_failure_does_not_stop_chat_capture(self):
        self.engine._source = self.source
        self.engine._window_found = True
        self.engine._process_frame = Mock(side_effect=lambda *_args, **_kwargs: self.engine._stop_event.set())
        worker = Mock()
        worker.start.side_effect = RuntimeError("Cannot start optional reader")
        with patch("mnmparse.app.engine.StatReaderWorker", return_value=worker), \
                self.assertLogs("mnmparse.app.engine", level="WARNING"):
            self.engine._capture_loop(self.source)
        self.engine._process_frame.assert_called_once()
        worker.start.assert_called_once()

    def test_stat_acceptance_uses_original_capture_age_and_rejects_old_or_repeated_results(self):
        older = self.next_stat_result()
        newer_request = self.engine._next_stat_request()
        self.assertIsNotNone(newer_request)
        newer = StatReadResult(newer_request.captured_at, newer_request.context,
                               tuple(newer_request.crop.shape), self.reading(), .1)
        self.engine._observe_player_status = Mock()
        self.engine._accept_stat_result(newer)
        self.engine._observe_player_status.assert_called_once()
        self.assertEqual(self.engine._observe_player_status.call_args.args[2], newer.captured_at)
        self.engine._accept_stat_result(older)
        self.engine._accept_stat_result(newer)
        self.assertEqual(self.engine._observe_player_status.call_count, 1)

        expired_request = self.engine._next_stat_request()
        self.assertIsNotNone(expired_request)
        expired_context = dataclasses.replace(expired_request.context,
                                              captured_monotonic=time.monotonic() - 30)
        expired = StatReadResult(time.time() - 30, expired_context,
                                 tuple(expired_request.crop.shape), self.reading(), .1)
        self.engine._accept_stat_result(expired)
        self.assertEqual(self.engine._observe_player_status.call_count, 1)

    def test_repeated_source_frame_is_not_a_second_stat_confirmation(self):
        result = self.next_stat_result()
        self.engine._accept_stat_result(result)
        self.source.advance = False
        self.assertIsNone(self.engine._next_stat_request())
        self.assertIsNone(self.engine._next_stat_request())
        self.assertFalse(self.engine._low_health_active)
        self.assertFalse(self.engine.overburdened_active)

    def test_inflight_stat_result_uses_the_current_health_threshold(self):
        first = self.next_stat_result()
        alerts = []
        self.engine.low_health.connect(alerts.append)
        self.engine.update_config(dataclasses.replace(self.cfg, low_health_threshold=5))
        self.engine._accept_stat_result(first)
        self.assertIsNone(self.engine.player_stat_snapshot(), "pre-change capture survived a threshold change")
        self.engine._accept_stat_result(self.next_stat_result())
        self.engine._accept_stat_result(self.next_stat_result())
        self.assertEqual(alerts, [])
        self.assertFalse(self.engine._low_health_active)

        self.engine.update_config(self.cfg)
        self.engine._accept_stat_result(self.next_stat_result())
        self.assertEqual(alerts, [], "changing a threshold bypassed fresh two-read confirmation")
        self.engine._accept_stat_result(self.next_stat_result())
        self.assertEqual(alerts, [8.0])

    def test_health_policy_changes_reject_old_capture_and_require_two_new_readings(self):
        for change in ("threshold", "disable and reenable"):
            with self.subTest(change=change):
                self.engine = Engine(self.cfg)
                self.source = _AdvancingSource()
                held = self.next_stat_result()
                alerts = []
                self.engine.low_health.connect(alerts.append)
                if change == "threshold":
                    self.engine.update_config(dataclasses.replace(self.cfg, low_health_threshold=25))
                else:
                    self.engine.update_config(dataclasses.replace(self.cfg, low_health_enabled=False))
                    self.engine.update_config(self.cfg)
                self.engine._accept_stat_result(held)
                self.assertIsNone(self.engine.player_stat_snapshot())
                self.assertEqual(alerts, [])
                self.engine._accept_stat_result(self.next_stat_result())
                self.assertEqual(alerts, [], "an old capture counted toward new health confirmation")
                self.assertFalse(self.engine._low_health_active)
                self.engine._accept_stat_result(self.next_stat_result())
                self.assertEqual(alerts, [8.0])

    def test_closed_worker_remains_owned_until_its_thread_exits(self):
        # A native recognizer can outlive the engine's bounded shutdown wait.
        # Restarting chat must not create a second stat recognizer beside it.
        retained = Mock(closed=True, is_running=True)
        self.engine._stat_worker = retained
        replacement = Mock(closed=False)
        with patch("mnmparse.app.engine.StatReaderWorker", return_value=replacement) as factory:
            self.assertIsNone(self.engine._ensure_stat_worker())
            self.assertIsNone(self.engine._ensure_stat_worker())
            self.assertIs(self.engine._stat_worker, retained)
            factory.assert_not_called()
            retained.is_running = False
            self.assertIs(self.engine._ensure_stat_worker(), replacement)
            self.assertIs(self.engine._stat_worker, replacement)
            factory.assert_called_once()

    def test_late_stat_completion_after_stop_cannot_update_or_alert(self):
        stat_started, release_stat = threading.Event(), threading.Event()

        def stat_read(*_args):
            stat_started.set()
            release_stat.wait(3)
            return self.reading(), []

        try:
            self.start_pipeline(lambda _image: [], stat_read)
            self.assertTrue(stat_started.wait(1))
            self.engine._observe_player_status = Mock()
            self.engine.stop()
            self.assertTrue(self.engine.wait_stopped(1), "optional stat OCR held up chat shutdown")
            self.assertTrue(any(worker.is_running for worker in self.workers))
            release_stat.set()
            self.assertTrue(all(worker.wait_stopped(1) for worker in self.workers))
            self.engine._observe_player_status.assert_not_called()
            self.assertIsNone(self.engine.player_stat_snapshot())
            self.assertFalse(self.engine._low_health_active)
            self.assertFalse(self.engine.overburdened_active)
        finally:
            release_stat.set()
            self.stop_pipeline()

    def test_xp_level_up_and_stat_completion_order_preserve_the_same_gain(self):
        for chat_first in (True, False):
            with self.subTest(chat_first=chat_first):
                engine = Engine(self.cfg)
                def observe(now, percent):
                    reading = dataclasses.replace(self.reading(), xp_percent=percent,
                                                  xp_bar_rect=(0, 70, 128, 80),
                                                  xp_resolution_points=.1)
                    engine._observe_player_status(reading, self.cfg, now, (80, 128, 3))
                for now in range(25):
                    observe(now, 98)
                event = parse_line("You have leveled up!", 25, self.cfg.player_name)
                if chat_first:
                    engine._note_player_event(event, self.cfg)
                observe(25, 2)
                observe(26, 2)
                if not chat_first:
                    engine._note_player_event(event, self.cfg)
                for now in range(27, 32):
                    observe(now, 2)
                summary = engine.session_snapshot().player_status
                self.assertEqual(summary["xp_gain_points"], 4)
                self.assertEqual(summary["xp_level_ups"], 1)
                self.assertEqual(summary["xp_observed_seconds"], 30)

    def test_blocked_stat_ocr_does_not_delay_two_read_resist_or_duplicate_hits(self):
        stat_started, release_stat, resist_seen = (threading.Event() for _ in range(3))
        target = "a gorged ghoul resists your Fear the Dead!"
        hit = "You crush a rat for 17 points of damage."
        trigger = Trigger(name="Resist", mode="regex", pattern=RESIST_PATTERN)
        received = []
        reads = 0
        baseline = ["You begin casting Heal.", "You have gained an experience point."]

        def chat_read(_image):
            nonlocal reads
            self.assertTrue(stat_started.wait(1), "independent mini-stat OCR never started")
            reads += 1
            if reads == 4:
                self.assertFalse(resist_seen.is_set(), "one OCR observation fired the Resist trigger")
            return self.lines(baseline if reads <= 2 else baseline + [target, hit, hit])

        def stat_read(*_args):
            stat_started.set()
            release_stat.wait(3)
            return self.reading(), []

        def delivered(message, _event):
            received.append(message)
            if match_trigger(trigger, message.text):
                resist_seen.set()

        self.engine.message.connect(delivered, Qt.ConnectionType.DirectConnection)
        try:
            self.start_pipeline(chat_read, stat_read)
            self.assertTrue(resist_seen.wait(2), "chat alert waited for the blocked mini-stat reader")
            self.assertFalse(release_stat.is_set())
            # Let unchanged OCR frames pass. They must not replay a trigger or
            # collapse the two equal-damage rows into a single hit.
            deadline = time.monotonic() + 1
            while reads < 8 and time.monotonic() < deadline:
                time.sleep(.005)
            relevant = [message for message in received if message.text in (target, hit)]
            self.assertEqual([message.text for message in relevant], [target, hit, hit])
            self.assertTrue(all(message.frames_seen >= 2 for message in relevant))
            self.assertEqual(self.engine._tracker.min_frames, 2)
        finally:
            release_stat.set()
            self.stop_pipeline()

    def test_blocked_chat_ocr_does_not_delay_health_or_weight_confirmation(self):
        chat_started, release_chat = threading.Event(), threading.Event()
        health_seen, weight_seen = threading.Event(), threading.Event()
        stats_reads = 0

        def chat_read(_image):
            chat_started.set()
            release_chat.wait(3)
            return []

        def stat_read(*_args):
            nonlocal stats_reads
            stats_reads += 1
            return self.reading(), []

        self.engine.low_health.connect(lambda _percent: health_seen.set(),
                                       Qt.ConnectionType.DirectConnection)
        self.engine.overburdened_changed.connect(lambda active: weight_seen.set() if active else None,
                                                 Qt.ConnectionType.DirectConnection)
        try:
            self.start_pipeline(chat_read, stat_read)
            self.assertTrue(chat_started.wait(1))
            self.assertTrue(health_seen.wait(2), "health confirmation waited for blocked chat OCR")
            self.assertTrue(weight_seen.wait(1), "weight confirmation waited for blocked chat OCR")
            self.assertFalse(release_chat.is_set())
            self.assertGreaterEqual(stats_reads, 2)
        finally:
            release_chat.set()
            self.stop_pipeline()


if __name__ == "__main__":
    unittest.main()
