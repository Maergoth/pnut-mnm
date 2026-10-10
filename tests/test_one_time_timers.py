"""Independent one-time countdowns never become persistent chat triggers."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.triggers import TimerBoard, Trigger, TriggerStore


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class OneTimeTimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from mnmparse.app.triggers_runtime import TriggerRunner

        self.tmp = tempfile.TemporaryDirectory()
        self.store = TriggerStore(Path(self.tmp.name) / "triggers.json")
        self.store.triggers = [Trigger(name="Existing", pattern="existing", action="none")]
        self.store.save()
        self.saved_text = self.store.path.read_text(encoding="utf-8")
        self.runner = TriggerRunner(self.store)
        self.runner.audio.run = Mock()
        self.runner.audio.cancel_speech = Mock()
        self.changes = Mock()
        self.fired = Mock()
        self.runner.timers_changed.connect(self.changes)
        self.runner.fired.connect(self.fired)

    def tearDown(self) -> None:
        self.runner._clock.stop()
        self.runner.audio.stop()
        self.tmp.cleanup()

    def test_same_label_countdowns_are_independent_and_start_the_clock(self) -> None:
        first = self.runner.start_one_time_timer("a skeletal vicar respawn", 600, now=100)
        second = self.runner.start_one_time_timer("a skeletal vicar respawn", 600, now=110)
        third = self.runner.start_one_time_timer("a rat respawn", 30, now=115)

        self.assertEqual(self.runner.board.timers, [first, second, third])
        self.assertEqual(len({timer.id for timer in self.runner.board.timers}), 3)
        self.assertEqual(len({timer.trigger_id for timer in self.runner.board.timers}), 3)
        self.assertEqual((first.remaining(110), second.remaining(110)), (590, 600))
        self.assertEqual((first.color, first.warn_color, first.low_color), ("", "", ""))
        self.assertEqual(first.warn_s, 0)
        self.assertTrue(self.runner._clock.isActive())
        self.assertEqual(self.changes.call_count, 3)
        self.runner.audio.run.assert_not_called()
        self.fired.assert_not_called()

    def test_one_time_countdown_does_not_persist_or_match_chat(self) -> None:
        timer = self.runner.start_one_time_timer("a rat", 30, now=100)
        self.assertEqual(self.runner.observe("a rat", now=110), [])
        self.assertEqual(self.runner.board.timers, [timer])
        self.assertEqual(timer.start, 100)
        self.assertIsNone(self.store.find(timer.trigger_id))
        self.assertEqual([trigger.name for trigger in self.store.triggers], ["Existing"])
        self.runner.save()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), self.saved_text)

    def test_new_longest_countdown_survives_at_capacity_and_cancels_pruned_speech(self) -> None:
        previous = [self.runner.start_one_time_timer(f"Mob {number}", number + 10, now=100)
                    for number in range(TimerBoard.MAX_TIMERS)]
        longest = previous[-1]
        with patch("mnmparse.app.triggers_runtime.time.time", return_value=100) as clock:
            newest = self.runner.start_one_time_timer("New ten minute respawn", 600)
        self.assertEqual(len(self.runner.board.timers), TimerBoard.MAX_TIMERS)
        self.assertIn(newest, self.runner.board.timers)
        self.assertNotIn(longest, self.runner.board.timers)
        self.assertTrue(all(timer in self.runner.board.timers for timer in previous[:-1]))
        self.runner.audio.cancel_speech.assert_called_once_with(self.runner._timer_scope(longest))
        clock.assert_called_once_with()

    def test_capacity_prefers_removing_an_ended_countdown(self) -> None:
        previous = [self.runner.start_one_time_timer(f"Mob {number}", number + 10, now=100)
                    for number in range(TimerBoard.MAX_TIMERS)]
        ended, longest = previous[0], previous[-1]
        self.runner.board.tick(110)
        self.assertTrue(ended.ended)
        newest = self.runner.start_one_time_timer("New ten minute respawn", 600, now=110)
        self.assertEqual(len(self.runner.board.timers), TimerBoard.MAX_TIMERS)
        self.assertIn(newest, self.runner.board.timers)
        self.assertIn(longest, self.runner.board.timers)
        self.assertNotIn(ended, self.runner.board.timers)
        self.runner.audio.cancel_speech.assert_called_once_with(self.runner._timer_scope(ended))

    def test_invalid_inputs_leave_board_clock_and_store_untouched(self) -> None:
        invalid = [("", 30), ("  \t", 30), (None, 30), ("a rat", 0), ("a rat", -10),
                   ("a rat", float("nan")), ("a rat", float("inf")), ("a rat", float("-inf")),
                   ("a rat", None), ("a rat", "invalid")]
        for label, duration in invalid:
            with self.subTest(label=label, duration=duration), self.assertRaises(ValueError):
                self.runner.start_one_time_timer(label, duration, now=100)
        self.assertEqual(self.runner.board.timers, [])
        self.assertFalse(self.runner._clock.isActive())
        self.changes.assert_not_called()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), self.saved_text)

    def test_cancel_and_clear_remove_only_requested_countdowns(self) -> None:
        first = self.runner.start_one_time_timer("a rat", 30, now=100)
        second = self.runner.start_one_time_timer("a rat", 30, now=110)
        self.changes.reset_mock()
        self.runner.cancel_timer(first.id)
        self.assertEqual(self.runner.board.timers, [second])
        self.runner.audio.cancel_speech.assert_called_once_with(self.runner._timer_scope(first))
        self.assertEqual(self.changes.call_count, 1)
        self.runner.clear_timers()
        self.assertEqual(self.runner.board.timers, [])
        self.assertEqual(self.changes.call_count, 2)
        with patch("mnmparse.triggers.time.time", return_value=120):
            self.runner._tick()
        self.assertFalse(self.runner._clock.isActive())

    def test_expiry_is_silent_once_then_lingers_and_stops_clock(self) -> None:
        timer = self.runner.start_one_time_timer(" a rat respawn ", 10, now=100)
        self.assertEqual(timer.label, "a rat respawn")
        self.changes.reset_mock()
        with patch("mnmparse.triggers.time.time", return_value=110):
            self.runner._tick()
            self.runner._tick()
        self.assertTrue(timer.ended)
        self.assertEqual(self.runner.board.timers, [timer])
        self.assertEqual(self.changes.call_count, 1)
        self.runner.audio.run.assert_not_called()
        with patch("mnmparse.triggers.time.time", return_value=110 + TimerBoard.LINGER_S + 0.1):
            self.runner._tick()
        self.assertEqual(self.runner.board.timers, [])
        self.assertEqual(self.changes.call_count, 2)
        self.assertFalse(self.runner._clock.isActive())
        self.assertEqual(self.runner.observe("a rat respawn", now=200), [])
        self.assertEqual(self.runner.board.timers, [])

    def test_retained_expiry_stays_until_dismissed_and_stops_clock(self) -> None:
        timer = self.runner.start_one_time_timer("a rat respawn", 10, now=100, keep_until_dismissed=True)
        self.changes.reset_mock()
        with patch("mnmparse.triggers.time.time", return_value=110):
            self.runner._tick()
        self.assertTrue(timer.ended)
        self.assertTrue(timer.keep_until_dismissed)
        self.assertFalse(self.runner._clock.isActive())
        self.assertEqual(self.changes.call_count, 1)
        with patch("mnmparse.triggers.time.time", return_value=1000000):
            self.runner._tick()
        self.assertEqual(self.runner.board.timers, [timer])
        self.assertEqual(self.changes.call_count, 1)
        self.runner.audio.run.assert_not_called()
        self.runner.save()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), self.saved_text)
        self.runner.cancel_timer(timer.id)
        self.assertEqual(self.runner.board.timers, [])
        self.assertFalse(self.runner.restart_timer(timer.id, now=1000010))

    def test_retained_timer_restarts_same_countdown_through_multiple_expiries(self) -> None:
        timer = self.runner.start_one_time_timer("a rat respawn", 10, now=100, keep_until_dismissed=True)
        timer.color, timer.low_color = "#112233", "#aabbcc"
        original = (timer.id, timer.trigger_id, timer.label, timer.duration, timer.color, timer.low_color)
        for restarted_at in (120, 1000000):
            with patch("mnmparse.triggers.time.time", return_value=restarted_at):
                self.runner._tick()
            self.assertTrue(timer.ended)
            timer.warned = True
            self.changes.reset_mock()
            with patch("mnmparse.app.triggers_runtime.time.time", return_value=restarted_at) as clock:
                self.assertTrue(self.runner.restart_timer(timer.id))
            clock.assert_called_once_with()
            self.assertIs(self.runner.board.timers[0], timer)
            self.assertEqual((timer.id, timer.trigger_id, timer.label, timer.duration, timer.color, timer.low_color), original)
            self.assertEqual(timer.remaining(restarted_at), 10)
            self.assertFalse(timer.warned)
            self.assertFalse(timer.ended)
            self.assertTrue(timer.keep_until_dismissed)
            self.assertTrue(self.runner._clock.isActive())
            self.assertEqual(self.changes.call_count, 1)
        self.runner.audio.run.assert_not_called()
        self.fired.assert_not_called()
        self.runner.save()
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), self.saved_text)

    def test_restart_ignores_running_missing_and_ordinary_timers(self) -> None:
        running = self.runner.start_one_time_timer("a rat respawn", 30, now=100, keep_until_dismissed=True)
        ordinary = self.runner.start_one_time_timer("ordinary", 1, now=100)
        with patch("mnmparse.triggers.time.time", return_value=101):
            self.runner._tick()
        self.changes.reset_mock()
        self.runner.audio.cancel_speech.reset_mock()
        self.assertFalse(self.runner.restart_timer("missing", now=102))
        self.assertFalse(self.runner.restart_timer(running.id, now=102))
        self.assertFalse(self.runner.restart_timer(ordinary.id, now=102))
        self.assertEqual((running.start, ordinary.start), (100, 100))
        self.assertFalse(running.ended)
        self.assertTrue(ordinary.ended)
        self.changes.assert_not_called()
        self.runner.audio.cancel_speech.assert_not_called()

    def test_retained_rows_do_not_consume_capacity_or_get_evicted_by_other_timers(self) -> None:
        retained = [self.runner.start_one_time_timer(f"Respawn {number}", 1 if number == 0 else 1000,
                                                     now=100, keep_until_dismissed=True)
                    for number in range(TimerBoard.MAX_TIMERS + 2)]
        with patch("mnmparse.triggers.time.time", return_value=101):
            self.runner._tick()
        self.assertTrue(retained[0].ended)
        ordinary = [self.runner.start_one_time_timer(f"ordinary {number}", 200 + number, now=101)
                    for number in range(TimerBoard.MAX_TIMERS)]
        another = self.runner.start_one_time_timer("another respawn", 2000, now=101, keep_until_dismissed=True)
        retained.append(another)
        self.assertTrue(all(timer in self.runner.board.timers for timer in ordinary))

        automatic = Trigger(name="automatic", pattern="automatic", timer=True, timer_seconds=2, action="none")
        self.store.triggers.append(automatic)
        self.runner.observe("automatic", now=101)
        self.assertTrue(all(timer in self.runner.board.timers for timer in retained))
        self.assertEqual(sum(not timer.keep_until_dismissed for timer in self.runner.board.timers), TimerBoard.MAX_TIMERS)
        self.assertNotIn(ordinary[-1], self.runner.board.timers)
        manual = self.runner.start_one_time_timer("ordinary manual", 600, now=101)
        self.assertIn(manual, self.runner.board.timers)
        self.assertTrue(all(timer in self.runner.board.timers for timer in retained))
        self.assertEqual(sum(not timer.keep_until_dismissed for timer in self.runner.board.timers), TimerBoard.MAX_TIMERS)

    def test_runner_clock_keeps_ordinary_linger_but_stops_when_only_retained_expiry_remains(self) -> None:
        retained = self.runner.start_one_time_timer("respawn", 1, now=100, keep_until_dismissed=True)
        ordinary = self.runner.start_one_time_timer("ordinary", 1, now=100)
        with patch("mnmparse.triggers.time.time", return_value=101):
            self.runner._tick()
        self.assertTrue(retained.ended)
        self.assertTrue(ordinary.ended)
        self.assertTrue(self.runner._clock.isActive())
        with patch("mnmparse.triggers.time.time", return_value=101 + TimerBoard.LINGER_S + 0.1):
            self.runner._tick()
        self.assertEqual(self.runner.board.timers, [retained])
        self.assertFalse(self.runner._clock.isActive())
        fresh = self.runner.start_one_time_timer("fresh", 30, now=105)
        self.assertTrue(self.runner._clock.isActive())
        self.runner.cancel_timer(fresh.id)
        self.assertFalse(self.runner._clock.isActive())
        self.runner.clear_timers()
        self.assertEqual(self.runner.board.timers, [])


if __name__ == "__main__":
    unittest.main()
