"""Live trigger matching leaves the GUI responsive and preserves event timing."""

from __future__ import annotations

from contextlib import contextmanager
import itertools
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.triggers import Trigger, TriggerStore


class AsyncTriggerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.runners = []

    def tearDown(self):
        for runner in self.runners:
            runner.close()
            runner.deleteLater()
        self.app.processEvents()

    def wait_for(self, predicate, message="Asynchronous matching did not finish"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(5)
        self.fail(message)

    def runner(self, *triggers):
        store = TriggerStore()
        self.sentinel = Trigger(name="Processing complete", pattern="processing complete", fuzzy=False)
        store.triggers = [*triggers, self.sentinel]
        runner = TriggerRunner(store)
        runner.audio.run = Mock()
        runner.events = []
        runner.fired.connect(runner.events.append)
        self.runners.append(runner)
        return runner

    def flush(self, runner):
        """A FIFO marker proves previous jobs and their queued GUI results arrived."""
        marker_id = self.sentinel.id
        before = sum(match.trigger.id == marker_id for match in runner.events)
        runner.enqueue("processing complete")
        self.wait_for(lambda: sum(match.trigger.id == marker_id for match in runner.events) > before)

    @contextmanager
    def blocked_matching(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        original = TriggerStore.matches

        def blocked(store, line, *args, **kwargs):
            started.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("The test did not release the matching worker")
                return original(store, line, *args, **kwargs)
            finally:
                finished.set()

        with patch("mnmparse.app.triggers_runtime.TriggerStore.matches", blocked):
            try:
                yield started, release, finished
            finally:
                release.set()
                if started.is_set():
                    self.wait_for(finished.is_set, "The released matching worker did not finish")
                self.app.processEvents()

    @contextmanager
    def expensive_regex(self, trigger):
        """Inject a native timeout with measured CPU, preserving all worker delivery code."""
        from mnmparse import triggers

        original = triggers._compiled
        clock = itertools.count()
        search = Mock(side_effect=TimeoutError)

        def compiled(pattern):
            return SimpleNamespace(search=search) if pattern == trigger.pattern else original(pattern)

        with patch("mnmparse.triggers._compiled", side_effect=compiled), \
                patch("mnmparse.triggers.time.thread_time", side_effect=lambda: next(clock) * .04):
            try:
                yield search
            finally:
                with triggers._quarantine_lock:
                    triggers._quarantined_patterns.pop(trigger.pattern, None)

    def test_enqueue_and_gui_heartbeat_do_not_wait_for_matching(self):
        trigger = Trigger(pattern=r"(?P<target>.+?) is rooted", mode="regex", action="none")
        runner = self.runner(trigger)
        heartbeat = []
        with self.blocked_matching() as (started, release, _finished):
            before = time.monotonic()
            runner.enqueue("a goblin is rooted")
            self.assertLess(time.monotonic() - before, .5)
            self.wait_for(started.is_set)
            QTimer.singleShot(0, lambda: heartbeat.append(True))
            self.wait_for(lambda: bool(heartbeat), "Matching blocked the GUI event loop")
            self.assertFalse(release.is_set())
            self.assertEqual(runner.events, [])
            release.set()
            self.flush(runner)
        self.assertEqual(runner.events[0].groups["target"], "a goblin")

    def test_delayed_timer_uses_original_observation_time(self):
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=10, action="none")
        runner = self.runner(trigger)
        observed_at = time.time() - 2
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted", now=observed_at)
            self.wait_for(started.is_set)
            self.assertEqual(runner.board.timers, [])
            release.set()
            self.flush(runner)
        self.assertEqual(len(runner.board.timers), 1)
        timer = runner.board.timers[0]
        self.assertEqual(timer.start, observed_at)
        self.assertGreater(timer.remaining(time.time()), 6)
        self.assertLessEqual(timer.remaining(time.time()), 8)

    def test_real_app_delivery_preserves_capture_time_before_gui_and_matching_delay(self):
        from mnmparse.app.main import App
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=10, action="none")
        runner = self.runner(trigger)
        observed_at = time.time() - 2
        host = SimpleNamespace(triggers=runner, window=None)
        message = SimpleNamespace(text="rooted", first_seen=observed_at)
        App._on_message_for_triggers(host, message, None)
        self.flush(runner)
        self.assertEqual(len(runner.board.timers), 1)
        self.assertEqual(runner.board.timers[0].start, observed_at)
        self.assertLessEqual(runner.board.timers[0].remaining(time.time()), 8)

    def test_expired_delayed_timer_does_not_start_a_fresh_countdown(self):
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=10, action="speak", speech="Root")
        runner = self.runner(trigger)
        observed_at = time.time() - 20
        runner.enqueue("rooted", now=observed_at)
        self.flush(runner)
        self.assertEqual(runner.board.timers, [])
        self.assertFalse(any(match.trigger.id == trigger.id for match in runner.events))
        self.assertFalse(any(call.args[0] == "speak" for call in runner.audio.run.call_args_list))

    def test_expired_retained_timer_does_not_extend_on_queued_overlap(self):
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=10,
                          timer_mode="retain", action="speak", speech="Root")
        runner = self.runner(trigger)
        observed_at = time.time() - 12
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted", now=observed_at)
            self.wait_for(started.is_set)
            # This line arrived inside the original timer's retain window.
            runner.enqueue("rooted", now=observed_at + 7)
            release.set()
            self.flush(runner)
        self.assertEqual(runner.board.timers, [])
        self.assertFalse(any(match.trigger.id == trigger.id for match in runner.events))
        self.assertFalse(any(call.args[0] == "speak" for call in runner.audio.run.call_args_list))

    def test_changed_deleted_or_disabled_trigger_cannot_fire_an_old_result(self):
        for change in ("edit", "delete", "disable"):
            with self.subTest(change=change):
                trigger = Trigger(pattern="rooted", timer=True, action="speak", speech="Root")
                runner = self.runner(trigger)
                with self.blocked_matching() as (started, release, _finished):
                    runner.enqueue("rooted")
                    self.wait_for(started.is_set)
                    if change == "edit":
                        # The editor changes the object before its debounced save.
                        trigger.speech = "A different action"
                    elif change == "delete":
                        runner.store.triggers.remove(trigger)
                    else:
                        trigger.enabled = False
                    release.set()
                    self.flush(runner)
                self.assertEqual(runner.board.timers, [])
                self.assertFalse(any(match.trigger.id == trigger.id for match in runner.events))
                self.assertFalse(any(call.args[0] == "speak" for call in runner.audio.run.call_args_list))

    def test_clear_timers_discards_pending_matches(self):
        trigger = Trigger(pattern="rooted", timer=True, action="speak", speech="Root")
        runner = self.runner(trigger)
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted")
            self.wait_for(started.is_set)
            runner.clear_timers()
            release.set()
            self.flush(runner)
        self.assertEqual(runner.board.timers, [])
        self.assertFalse(any(match.trigger.id == trigger.id for match in runner.events))
        self.assertFalse(any(call.args[0] == "speak" for call in runner.audio.run.call_args_list))

    def test_close_is_prompt_and_ignores_late_results(self):
        trigger = Trigger(pattern="rooted", timer=True, action="speak", speech="Root")
        runner = self.runner(trigger)
        with self.blocked_matching() as (started, release, finished):
            runner.enqueue("rooted")
            self.wait_for(started.is_set)
            before = time.monotonic()
            runner.close()
            self.assertLess(time.monotonic() - before, .5)
            release.set()
            self.wait_for(finished.is_set)
            QTest.qWait(50)
        self.assertEqual(runner.events, [])
        self.assertEqual(runner.board.timers, [])
        runner.audio.run.assert_not_called()

    def test_fifo_matching_preserves_cooldown_and_independent_stack_timers(self):
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=60,
                          timer_mode="stack", cooldown_s=5, action="none")
        runner = self.runner(trigger)
        observed_at = time.time() - 10
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted", now=observed_at)
            self.wait_for(started.is_set)
            runner.enqueue("rooted", now=observed_at + 2)
            runner.enqueue("rooted", now=observed_at + 6)
            release.set()
            self.flush(runner)
        self.assertEqual([timer.start for timer in runner.board.timers], [observed_at, observed_at + 6])
        self.assertEqual(sum(match.trigger.id == trigger.id for match in runner.events), 2)

    def test_fifo_retain_ignores_overlapping_match_and_audio(self):
        trigger = Trigger(pattern="rooted", timer=True, timer_seconds=60,
                          timer_mode="retain", action="speak", speech="Root")
        runner = self.runner(trigger)
        observed_at = time.time() - 10
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted", now=observed_at)
            self.wait_for(started.is_set)
            runner.enqueue("rooted", now=observed_at + 5)
            release.set()
            self.flush(runner)
        self.assertEqual([timer.start for timer in runner.board.timers], [observed_at])
        self.assertEqual(sum(match.trigger.id == trigger.id for match in runner.events), 1)
        self.assertEqual(sum(call.args[0] == "speak" for call in runner.audio.run.call_args_list), 1)

    def test_expensive_regex_does_not_starve_a_later_simple_capture(self):
        slow = Trigger(mode="regex", pattern=r"(?:(?:a+)+)+$(?#async-later-trigger)")
        smite = Trigger(name="Smite", mode="regex",
                        pattern=r"Your Righteous Smite II hits .+? for (?P<damage>\d+) points of Holy Damage",
                        timer=True, timer_seconds=20, timer_label="Smite: {damage}", action="none")
        runner = self.runner(slow, smite)
        with self.expensive_regex(slow) as search:
            runner.enqueue("Your Righteous Smite II hits a skeleton for 154 points of Holy Damage")
            self.flush(runner)
            search.assert_called_once()
        self.assertFalse(slow.enabled)
        self.assertTrue(smite.enabled)
        matches = [match for match in runner.events if match.trigger.id == smite.id]
        self.assertEqual([match.groups["damage"] for match in matches], ["154"])
        self.assertIs(matches[0].trigger, smite)
        self.assertEqual([timer.label for timer in runner.board.timers], ["Smite: 154"])

    def test_worker_disable_and_warning_survive_snapshot_validation_and_are_saved(self):
        slow = Trigger(mode="regex", pattern=r"(?:(?:a+)+)+$(?#async-save-warning)")
        runner = self.runner(slow)
        warnings, changes = [], []
        runner.matching_warning.connect(warnings.append)
        runner.store_changed.connect(lambda: changes.append(True))
        with tempfile.TemporaryDirectory() as folder:
            runner.store.path = Path(folder) / "triggers.json"
            with self.expensive_regex(slow):
                runner.enqueue("a" * 100 + "!")
                self.flush(runner)
            self.assertFalse(slow.enabled)
            self.assertEqual(len(changes), 1)
            self.assertTrue(any("Complex regular expressions can cause slowdowns" in warning for warning in warnings))
            saved = json.loads(runner.store.path.read_text(encoding="utf-8"))
            row = next(row for row in saved["triggers"] if row["id"] == slow.id)
            self.assertFalse(row["enabled"])

    def test_paused_gui_applies_backpressure_before_matching_another_batch(self):
        trigger = Trigger(pattern="rooted", fuzzy=False, action="none")
        runner = self.runner(trigger)
        original = TriggerStore.matches
        first_processed, second_processed = threading.Event(), threading.Event()
        processed = []

        def matched(store, line, *args, **kwargs):
            result = original(store, line, *args, **kwargs)
            processed.append(line)
            if len(processed) == 1:
                first_processed.set()
            else:
                second_processed.set()
            return result

        with patch("mnmparse.app.triggers_runtime.TriggerStore.matches", matched):
            runner.enqueue("rooted first")
            # Deliberately leave Qt events undelivered, like a busy GUI frame.
            self.assertTrue(first_processed.wait(2))
            runner.enqueue("rooted second")
            runner.enqueue("rooted third")
            self.assertFalse(second_processed.wait(.15), "The worker accumulated another undelivered result")
            self.assertEqual(processed, ["rooted first"])
            self.assertEqual(runner.events, [])
            self.flush(runner)
        self.assertEqual(processed, ["rooted first", "rooted second", "rooted third", "processing complete"])
        self.assertEqual([match.line for match in runner.events], processed)

    def test_full_pending_queue_warns_without_blocking_and_preserves_accepted_jobs(self):
        trigger = Trigger(pattern=r"rooted (?P<target>first|second|third)", mode="regex",
                          timer=True, timer_mode="stack", timer_seconds=60,
                          timer_label="Root {target}", action="none")
        with patch("mnmparse.app.trigger_matching.MAX_PENDING_LINES", 1):
            runner = self.runner(trigger)
        warnings = []
        runner.matching_warning.connect(warnings.append)
        with self.blocked_matching() as (started, release, _finished):
            runner.enqueue("rooted first")
            self.wait_for(started.is_set)
            runner.enqueue("rooted second")
            before = time.monotonic()
            runner.enqueue("rooted third")
            self.assertLess(time.monotonic() - before, .5)
            self.assertEqual(len(warnings), 1)
            self.assertIn("a chat line was skipped", warnings[0])
            release.set()
            self.wait_for(lambda: len(runner.events) == 2)
        self.assertEqual([match.line for match in runner.events], ["rooted first", "rooted second"])
        self.assertEqual([timer.label for timer in runner.board.timers], ["Root first", "Root second"])


if __name__ == "__main__":
    unittest.main()
