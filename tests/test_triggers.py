"""Triggers: matching, storage, timers, the runner, the timer panel and the page."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse.swing import HandState
from mnmparse.triggers import TimerBoard, Trigger, TriggerStore, fill_placeholders, match_trigger


class MatchTests(unittest.TestCase):
    def test_contains_ignores_case_punctuation_and_ocr_typos(self) -> None:
        t = Trigger(pattern="begins casting Mesmerize")
        self.assertIsNotNone(match_trigger(t, "a skeletal vicar begins casting Mesmerize."))
        self.assertIsNotNone(match_trigger(t, "a skeletal vicar bezins castinz Mesmerize"))  # OCR
        self.assertIsNone(match_trigger(t, "a skeletal vicar begins casting Root."))
        t.fuzzy = False
        self.assertIsNone(match_trigger(t, "a skeletal vicar bezins castinz Mesmerize"))

    def test_short_words_must_match_exactly(self) -> None:
        t = Trigger(pattern="is mez")
        self.assertIsNone(match_trigger(t, "a rat is met."))

    def test_starts_exact_and_disabled(self) -> None:
        self.assertIsNotNone(match_trigger(Trigger(pattern="You are stunned", mode="starts"), "You are stunned!"))
        self.assertIsNone(match_trigger(Trigger(pattern="are stunned", mode="starts"), "You are stunned!"))
        self.assertIsNotNone(match_trigger(Trigger(pattern="You are stunned", mode="exact"), "You are stunned!"))
        self.assertIsNone(match_trigger(Trigger(pattern="You are", mode="exact"), "You are stunned!"))
        self.assertIsNone(match_trigger(Trigger(pattern="stunned", enabled=False), "You are stunned!"))

    def test_regex_groups_fill_the_speech(self) -> None:
        t = Trigger(pattern=r"(?P<mob>an? [a-z ]+) begins casting (?P<spell>\w+)", mode="regex", speech="{spell} on {mob}")
        m = match_trigger(t, "a skeletal vicar begins casting Root.")
        self.assertEqual(fill_placeholders(t.speech, m.values()), "Root on a skeletal vicar")
        self.assertIsNone(match_trigger(Trigger(pattern="(", mode="regex"), "anything"))
        self.assertTrue(Trigger(pattern="(", mode="regex").problems())


class StoreTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "triggers.json"
            store = TriggerStore(path)
            store.volume, store.voice = 55, "Microsoft Zira"
            store.triggers = [Trigger(name="Mez", pattern="is mesmerized", timer=True, timer_seconds=24)]
            store.save()
            again = TriggerStore(path)
            self.assertTrue(again.load())
            self.assertEqual((again.volume, again.voice), (55, "Microsoft Zira"))
            self.assertEqual(again.triggers[0].to_dict(), store.triggers[0].to_dict())

    def test_bad_values_are_repaired(self) -> None:
        t = Trigger.from_dict({"mode": "nonsense", "action": "explode", "volume": 900, "timer_seconds": 0, "junk": 1})
        self.assertEqual((t.mode, t.action, t.volume, t.timer_seconds), ("contains", "none", 100, 1.0))


class TimerBoardTests(unittest.TestCase):
    def test_restart_stack_ignore(self) -> None:
        board = TimerBoard()
        t = Trigger(name="Mez", timer=True, timer_seconds=30, timer_mode="restart")
        board.start(t, "Mez", now=0.0)
        board.start(t, "Mez", now=10.0)
        self.assertEqual(len(board.timers), 1)
        self.assertEqual(board.timers[0].remaining(10.0), 30.0)
        t.timer_mode = "stack"
        board.start(t, "Mez 2", now=11.0)
        self.assertEqual(len(board.timers), 2)
        t.timer_mode = "ignore"
        self.assertIsNone(board.start(t, "Mez 3", now=12.0))
        self.assertEqual(len(board.timers), 2)

    def test_warn_end_and_linger(self) -> None:
        board = TimerBoard()
        t = Trigger(name="Root", timer=True, timer_seconds=10, timer_warn_s=3)
        board.start(t, "Root", now=0.0)
        self.assertEqual(board.tick(5.0), ([], []))
        warned, ended = board.tick(7.5)
        self.assertEqual((len(warned), len(ended)), (1, 0))
        warned, ended = board.tick(10.2)
        self.assertEqual((len(warned), len(ended)), (0, 1))
        self.assertEqual(len(board.timers), 1, "an ended timer lingers")
        board.tick(10.0 + TimerBoard.LINGER_S + 0.5)
        self.assertEqual(board.timers, [])


class SwingBarTests(unittest.TestCase):
    def test_bar_waits_at_full_until_the_swing_is_read(self) -> None:
        h = HandState("crush", last=100.0, delay=3.0, swings=5)
        self.assertAlmostEqual(h.progress(101.5), 0.5)
        self.assertEqual(h.progress(104.5), 1.0, "no swing line yet: the bar holds at full")
        self.assertTrue(h.due(104.5))
        self.assertFalse(h.due(101.0))

    @staticmethod
    def _swing(ts: float):
        from types import SimpleNamespace

        return SimpleNamespace(kind="melee_hit", actor="You", skill="crush", ts=ts, weapon=None, is_pet=False)

    def test_read_swing_resets_from_the_time_already_gone(self) -> None:
        from mnmparse.swing import SwingTracker

        tr = SwingTracker()
        for i in range(6):
            tr.observe(self._swing(100.0 + 3.3 * i))
        (h,) = tr.hands(116.5 + 0.27)  # the line arrives a quarter second after the swing
        self.assertAlmostEqual(h.progress(116.5 + 0.27), 0.27 / h.delay, places=3)

    def test_delay_never_changes_while_the_bar_fills(self) -> None:
        from mnmparse.swing import SwingTracker

        tr = SwingTracker()
        for i in range(10):
            tr.observe(self._swing(100.0 + 3.3 * i))
        delays = {tr.hands(129.7 + dt)[0].delay for dt in (0.0, 1.0, 2.0, 3.0, 5.0)}
        self.assertEqual(len(delays), 1)

    def test_haste_switches_the_delay_within_two_swings(self) -> None:
        from mnmparse.swing import SwingTracker

        tr = SwingTracker()
        ts = 100.0
        for _ in range(12):
            tr.observe(self._swing(ts))
            ts += 3.33
        self.assertAlmostEqual(tr.hands(ts)[0].delay, 3.33, places=1)
        for _ in range(3):  # haste: 2.75 s swings
            ts += 2.75 - 3.33
            tr.observe(self._swing(ts))
            ts += 3.33
        self.assertAlmostEqual(tr.hands(ts - 3.33)[0].delay, 2.75, places=1)

    def test_a_single_delayed_swing_keeps_the_delay(self) -> None:
        from mnmparse.swing import SwingTracker

        tr = SwingTracker()
        times = [100.0 + 3.33 * i for i in range(12)]
        times.append(times[-1] + 4.6)  # a cast held this swing
        times.append(times[-1] + 3.33)
        for t in times:
            tr.observe(self._swing(t))
        self.assertAlmostEqual(tr.hands(times[-1])[0].delay, 3.33, places=1)


@unittest.skipUnless(sys.platform == "win32", "Qt is exercised on the Windows build")
class RuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def _runner(self, tmp: str, *triggers: Trigger):
        from mnmparse.app.triggers_runtime import TriggerRunner

        store = TriggerStore(Path(tmp) / "triggers.json")
        store.triggers = list(triggers)
        runner = TriggerRunner(store)
        runner.played = []
        runner.audio.run = lambda action, **kw: runner.played.append((action, kw))  # no real sound in tests
        return runner

    def test_observe_plays_speaks_and_starts_timers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mez = Trigger(name="Mez", pattern=r"(?P<mob>an? [a-z ]+) is mesmerized", mode="regex", action="speak",
                          speech="{mob} mezzed", timer=True, timer_seconds=24, timer_label="Mez {mob}")
            runner = self._runner(tmp, mez)
            fired = runner.observe("a skeletal knight is mesmerized.", now=1000.0)
            self.assertEqual(len(fired), 1)
            self.assertEqual(runner.played, [("speak", {"sound": "Chime", "file": "", "speech": "a skeletal knight mezzed", "volume": 80,
                                                     "scope": runner._timer_scope(runner.board.timers[0])})])
            self.assertEqual([t.label for t in runner.board.timers], ["Mez a skeletal knight"])
            self.assertEqual(runner.observe("a skeletal knight is rooted.", now=1001.0), [])

    def test_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp, Trigger(pattern="stunned", cooldown_s=5))
            self.assertEqual(len(runner.observe("You are stunned!", now=0.0)), 1)
            self.assertEqual(len(runner.observe("You are stunned!", now=2.0)), 0)
            self.assertEqual(len(runner.observe("You are stunned!", now=6.0)), 1)

    def test_fire_now_uses_damage_capture_for_label_and_speech(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Trigger(name="Smite II", enabled=False, mode="regex",
                              pattern=r"Your Righteous Smite II hits .+? for (?P<damage>\d+) points of Holy Damage",
                              timer=True, timer_label="Smite: {damage} damage", action="speak", speech="{damage} damage")
            runner = self._runner(tmp, trigger)
            matches = []
            runner.fired.connect(matches.append)
            try:
                runner.test(trigger, "Your Righteous Smite II hits a skeletal knight for 154 points of Holy Damage.")
                self.assertEqual(runner.board.timers[0].label, "Smite: 154 damage")
                self.assertEqual(runner.played[0][1]["speech"], "154 damage")
                self.assertEqual(matches[0].groups["damage"], "154")
                self.assertIs(matches[0].trigger, trigger)
                self.assertFalse(trigger.enabled, "preview must not enable a disabled trigger")
            finally:
                runner._clock.stop()

    def test_fire_now_without_matching_sample_still_fires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trigger = Trigger(name="Alert", pattern="rooted", action="none", enabled=False)
            runner = self._runner(tmp, trigger)
            matches = []
            runner.fired.connect(matches.append)
            runner.test(trigger)
            runner.test(trigger, "a different sample")
            self.assertEqual([m.line for m in matches], ["rooted", "a different sample"])

    def test_builtin_sounds_are_generated(self) -> None:
        from mnmparse.app.triggers_runtime import builtin_sound_path
        from mnmparse.triggers import BUILTIN_SOUNDS

        for name in BUILTIN_SOUNDS:
            path = builtin_sound_path(name)
            self.assertTrue(path.is_file() and path.stat().st_size > 1000, name)

    def test_timer_panel_docks_between_overlay_and_attack_bar(self) -> None:
        from PySide6.QtCore import QSettings, Qt

        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.app.timer_panel import format_remaining

        self.assertEqual((format_remaining(75.2), format_remaining(0), format_remaining(3725)), ("1:16", "0:00", "1:02:05"))
        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "o.ini"), QSettings.Format.IniFormat)
            overlay = OverlayWindow(settings, __import__("mnmparse.config", fromlist=["Config"]).Config())
            for w in (overlay, overlay.attack_bar, overlay.timer_panel):
                w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            runner = self._runner(tmp, Trigger(name="Root", pattern="rooted", action="none", timer=True, timer_seconds=20))
            overlay.set_trigger_runner(runner)
            try:
                overlay.setGeometry(100, 100, 400, 250)
                overlay.show()
                self.app.processEvents()
                panel, bar = overlay.timer_panel, overlay.attack_bar
                self.assertFalse(panel.isVisible(), "no timers: no panel")
                bar_y_alone = bar.y()
                runner.observe("a skeletal knight is rooted.")
                self.app.processEvents()
                self.assertTrue(panel.isVisible())
                self.assertGreater(panel.y(), overlay.frameGeometry().bottom())
                self.assertGreater(bar.y(), panel.frameGeometry().bottom(), "the attack bar sits under the timers")
                runner.clear_timers()
                self.app.processEvents()
                self.assertFalse(panel.isVisible(), "timer triggers do not leave a fading notification")
                self.assertEqual(bar.y(), bar_y_alone, "and moves back up when they are gone")
            finally:
                overlay.close()
                overlay.deleteLater()
                self.app.processEvents()

    def test_page_creates_and_edits_triggers(self) -> None:
        from PySide6.QtCore import QSettings

        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage
        from mnmparse.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            runner = self._runner(tmp)
            page = TriggersPage(_MissingEngine(Config()), Config(), QSettings(str(Path(tmp) / "p.ini"), QSettings.Format.IniFormat))
            page.set_runner(runner)
            try:
                page._on_new()
                self.assertEqual(len(runner.store.triggers), 1)
                page.pattern.setText("is mesmerized")
                page.timer.setChecked(True)
                page.seconds.setValue(24)
                page._set_timer_color("timer_color", "#123456")
                page._set_timer_color("timer_low_color", "#abcdef")
                page.low_s.setValue(4.5)
                page.timer_mode.setCurrentIndex(page.timer_mode.findData("retain"))
                page._on_edit()
                page.test_line.setText("a skeletal knight is mesmerized.")
                self.assertIn("Matches", page.test_result.text())
                page._save_now()
                saved = TriggerStore(Path(tmp) / "triggers.json")
                self.assertTrue(saved.load())
                self.assertEqual((saved.triggers[0].pattern, saved.triggers[0].timer, saved.triggers[0].timer_seconds),
                                 ("is mesmerized", True, 24.0))
                self.assertEqual((saved.triggers[0].timer_color, saved.triggers[0].timer_low_color,
                                  saved.triggers[0].timer_low_s, saved.triggers[0].timer_mode),
                                 ("#123456", "#abcdef", 4.5, "retain"))
            finally:
                page.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
