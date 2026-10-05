"""Timer overlap semantics, cancelable speech, starter presets and custom colors."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.triggers import TimerBoard, Trigger, TriggerStore


class TimerSettingsTests(unittest.TestCase):
    def test_replace_invalidates_old_instance_and_retain_keeps_it(self):
        board = TimerBoard()
        trigger = Trigger(timer=True, timer_seconds=10, timer_warn_s=2)
        old = board.start(trigger, "First", 0)
        board.tick(8)
        new = board.start(trigger, "Replacement", 9)
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(board.tick(10), ([], []), "old expiry must not fire")
        self.assertEqual(board.timers, [new])
        trigger.timer_mode = "retain"
        self.assertIsNone(board.start(trigger, "Ignored", 12))
        self.assertEqual((new.label, new.start), ("Replacement", 9))
        # A delayed GUI tick must not make an expired timer count as running.
        newest = board.start(trigger, "After expiry", 20)
        self.assertEqual(board.timers, [newest])

    def test_legacy_modes_and_color_settings_round_trip(self):
        self.assertEqual(Trigger.from_dict({"timer_mode": "restart"}).timer_mode, "replace")
        self.assertEqual(Trigger.from_dict({"timer_mode": "ignore"}).timer_mode, "retain")
        original = Trigger(timer_color="#112233", timer_warn_color="#445566", timer_low_color="#abcdef", timer_low_s=3.5)
        self.assertEqual(Trigger.from_dict(original.to_dict()), original)
        repaired = Trigger.from_dict({"timer_color": "bad", "timer_warn_color": None, "timer_low_s": -5})
        self.assertEqual((repaired.timer_color, repaired.timer_warn_color, repaired.timer_low_s), ("", "", 0))

    def test_starter_presets_preserve_customizations_and_deletions(self):
        store = TriggerStore()
        customized = Trigger(id="a8089904bf", name="My gate alert", pattern="casting Gate", enabled=False, volume=42)
        unrelated = Trigger(name="Custom", pattern="custom")
        store.triggers = [customized, unrelated]
        self.assertTrue(store.install_presets())
        self.assertIs(store.triggers[0], customized)
        self.assertEqual((customized.enabled, customized.volume), (False, 42))
        self.assertEqual(len(store.triggers), 4)
        self.assertFalse(store.find("357af85f56").enabled, "keep original Healkick enabled state")
        store.triggers = [unrelated]  # deleting presets is deliberate
        again = TriggerStore()
        again.load_dict(store.to_dict())
        self.assertFalse(again.install_presets())
        self.assertEqual([t.id for t in again.triggers], [unrelated.id])

    def test_imported_presets_are_not_duplicated(self):
        store = TriggerStore()
        store.triggers = [Trigger(name="Invis break", pattern="customized pattern"),
                          Trigger(name="My heal warning", pattern="begins casting Heal")]
        store.install_presets()
        self.assertEqual(len(store.triggers), 3)


@unittest.skipUnless(sys.platform == "win32", "Qt is exercised on the Windows build")
class TimerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def runner(self, trigger):
        from mnmparse.app.triggers_runtime import TriggerRunner

        store = TriggerStore()
        store.triggers = [trigger]
        runner = TriggerRunner(store)
        self.addCleanup(lambda: runner._clock.stop())
        self.addCleanup(lambda: runner.audio.stop())
        return runner

    @staticmethod
    def fake_speech(audio):
        from PySide6.QtTextToSpeech import QTextToSpeech

        class Speech:
            def __init__(self):
                self.current_state = QTextToSpeech.State.Ready
                self.spoken = []
                self.stops = 0

            def state(self):
                return self.current_state

            def setVolume(self, _volume):
                pass

            def say(self, text):
                self.spoken.append(text)
                self.current_state = QTextToSpeech.State.Speaking
                audio._speech_state_changed(self.current_state)

            def finish(self):
                self.current_state = QTextToSpeech.State.Ready
                audio._speech_state_changed(self.current_state)

            def stop(self):
                self.stops += 1
                self.finish()

        audio._tts = fake = Speech()
        return fake

    def test_retain_suppresses_action_and_fired_signal(self):
        trigger = Trigger(pattern="rooted", action="speak", speech="Root", timer=True, timer_mode="retain")
        runner = self.runner(trigger)
        actions, events = [], []
        runner.audio.run = lambda action, **kw: actions.append((action, kw))
        runner.fired.connect(events.append)
        self.assertEqual(len(runner.observe("rooted", 0)), 1)
        original = runner.board.timers[0]
        self.assertEqual(runner.observe("rooted", 5), [])
        self.assertEqual((len(actions), len(events)), (1, 1))
        self.assertEqual(runner.board.timers, [original])

    def test_replace_removes_queued_warning_and_end_without_stopping_other_speech(self):
        trigger = Trigger(pattern="rooted", action="none", timer=True, timer_seconds=10,
                          timer_warn_action="speak", timer_end_action="speak")
        runner = self.runner(trigger)
        speech = self.fake_speech(runner.audio)
        runner.audio.speak("Unrelated announcement")
        runner.observe("rooted", 0)
        old = runner.board.timers[0]
        runner._alert(old, "warn")
        runner._alert(old, "end")
        self.assertEqual(len(runner.audio._speech_queue), 2)
        # The end may remain queued after the timer has disappeared from the panel.
        runner.board.tick(14)
        runner.observe("rooted", 15)
        self.assertEqual(runner.audio._speech_queue, [])
        self.assertEqual(speech.stops, 0)
        runner._alert(old, "end")
        speech.finish()
        runner.audio._pump_speech()
        self.assertEqual(speech.spoken, ["Unrelated announcement"])

    def test_replace_and_cancel_stop_the_obsolete_timer_speech(self):
        trigger = Trigger(pattern="rooted", action="speak", speech="Root", timer=True)
        runner = self.runner(trigger)
        speech = self.fake_speech(runner.audio)
        runner.observe("rooted", 0)
        runner.observe("rooted", 5)
        self.assertEqual(speech.stops, 1)
        self.assertEqual(speech.spoken, ["Root", "Root"])
        runner.cancel_timer(runner.board.timers[0].id)
        self.assertEqual(speech.stops, 2)
        self.assertEqual(runner.audio._speech_queue, [])

    def test_stack_keeps_independent_timer_speech(self):
        trigger = Trigger(pattern="rooted", action="speak", speech="Root", timer=True, timer_mode="stack")
        runner = self.runner(trigger)
        speech = self.fake_speech(runner.audio)
        runner.observe("rooted", 0)
        runner.observe("rooted", 5)
        self.assertEqual(len(runner.board.timers), 2)
        self.assertEqual(len(runner.audio._speech_queue), 1)
        runner.cancel_timer(runner.board.timers[1].id)
        self.assertEqual(runner.audio._speech_queue, [])
        self.assertEqual(speech.stops, 0)

    def test_custom_colors_apply_at_warning_and_low_thresholds(self):
        from mnmparse.app.timer_panel import timer_color
        from mnmparse.app.widgets import token

        trigger = Trigger(timer_seconds=30, timer_warn_s=12, timer_low_s=3,
                          timer_color="#112233", timer_warn_color="#445566", timer_low_color="#abcdef")
        timer = TimerBoard().start(trigger, "Test", 0)
        self.assertEqual(timer_color(timer, 0).name(), "#112233")
        self.assertEqual(timer_color(timer, 18).name(), "#445566")
        self.assertEqual(timer_color(timer, 27).name(), "#abcdef")
        timer.ended = True
        self.assertEqual(timer_color(timer, 30).name(), "#abcdef")
        defaults = TimerBoard().start(Trigger(timer_seconds=30), "Default", 0)
        self.assertEqual(timer_color(defaults, 0).name(), token("SUCCESS"))
        self.assertEqual(timer_color(defaults, 25).name(), token("DANGER"))

    def test_first_install_seeds_only_public_starters_and_preserves_corrupt_file(self):
        from mnmparse.app.triggers_runtime import default_store

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "triggers.json"
            with patch("mnmparse.app.triggers_runtime.project_path", return_value=path):
                store = default_store()
                self.assertEqual([t.name for t in store.triggers], ["Gatekick", "Healkick", "Invis Break"])
                self.assertTrue(path.is_file())
                self.assertEqual(default_store().to_dict(), store.to_dict())
                path.write_text("invalid json", encoding="utf-8")
                self.assertEqual(len(default_store().triggers), 3)
                self.assertEqual(path.read_text(encoding="utf-8"), "invalid json")


if __name__ == "__main__":
    unittest.main()
