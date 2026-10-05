"""Non-timer triggers render temporary labels below the independent timer rows."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
    from PySide6.QtGui import QPainter
    from PySide6.QtWidgets import QApplication, QMenu, QWidget

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.triggers import Trigger, TriggerStore


if HAVE_QT:
    class _Owner(QWidget):
        locked = True
        click_through = False
        opacity = 0.85

        def __init__(self) -> None:
            super().__init__()
            self.resize(600, 100)
            self.panels_changed = Mock()

        def dock_anchor(self, panel: QWidget):
            return self.frameGeometry()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TriggerPopupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from mnmparse.app.timer_panel import TimerPanel
        from mnmparse.app.triggers_runtime import TriggerRunner

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.owner = _Owner()
        self.owner.show()
        self.panel = TimerPanel(self.owner, self.settings, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.runner = TriggerRunner(TriggerStore(Path(self.tmp.name) / "triggers.json"))
        self.runner.audio.run = Mock()
        self.panel.set_runner(self.runner)
        self.clock_patch = patch("mnmparse.app.timer_panel.time.monotonic", return_value=100.0)
        self.clock = self.clock_patch.start()

    def tearDown(self) -> None:
        self.panel.close()
        self.owner.close()
        self.runner._clock.stop()
        self.clock_patch.stop()
        self.app.processEvents()
        self.tmp.cleanup()

    def fire(self, name: str = "Righteous Smite", **kwargs) -> Trigger:
        trigger = Trigger(name=name, pattern=name, action="none", **kwargs)
        self.runner.store.triggers.append(trigger)
        self.runner.observe(name)
        self.runner._clock.stop()
        return trigger

    def painted_text(self) -> list[tuple[str, float, float]]:
        drawn = []

        class _Painter(QPainter):
            def drawText(self, rect, flags, text):  # noqa: N802
                drawn.append((text, rect.top(), self.opacity()))
                return super().drawText(rect, flags, text)

        with patch("mnmparse.app.timer_panel.QPainter", _Painter):
            self.panel.grab()
        return drawn

    def test_non_timer_match_shows_rendered_capture_label_without_countdown(self) -> None:
        trigger = Trigger(name="Righteous Smite", pattern=r"smite hits (?P<target>.+) for (?P<damage>\d+)",
                          mode="regex", timer=False, timer_label="{name}: {damage} on {target}", action="none")
        self.runner.store.triggers.append(trigger)
        self.runner.observe("smite hits a skeleton for 84")
        self.assertEqual(self.panel.timers(), [])
        self.assertTrue(self.panel.isVisible())
        self.assertTrue(self.panel._anim.isActive())
        self.assertEqual([text for text, _, _ in self.painted_text()], ["Righteous Smite: 84 on a skeleton"])
        self.assertEqual(self.panel._rows, [])
        self.assertTrue(self.panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))

    def test_notification_uses_name_when_label_is_empty(self) -> None:
        self.fire("Gatekick")
        self.assertEqual([text for text, _, _ in self.painted_text()], ["Gatekick"])

    def test_timer_match_replacement_and_manual_fire_only_show_countdown(self) -> None:
        trigger = self.fire("Root", timer=True, timer_seconds=30, timer_mode="replace")
        original = self.runner.board.timers[0]
        self.assertEqual(self.panel._popups, [])
        self.assertEqual([text for text, _, _ in self.painted_text()].count("Root"), 1)

        self.runner.observe("Root", now=original.start + 5)
        replacement = self.runner.board.timers[0]
        self.assertIsNot(replacement, original)
        self.assertEqual(replacement.remaining(original.start + 5), 30)
        self.assertEqual(self.panel._popups, [])
        self.assertEqual([text for text, _, _ in self.painted_text()].count("Root"), 1)

        trigger.enabled = False
        self.runner.test(trigger, "Root")
        self.runner._clock.stop()
        self.assertIsNot(self.runner.board.timers[0], replacement)
        self.assertEqual(len(self.runner.board.timers), 1)
        self.assertEqual(self.panel._popups, [])
        self.assertEqual([text for text, _, _ in self.painted_text()].count("Root"), 1)
        self.runner.clear_timers()
        self.assertFalse(self.panel.isVisible())

    def test_popup_fades_during_last_second_then_panel_expires(self) -> None:
        self.fire()
        for when, opacity in ((102.9, 1.0), (103.5, 0.5), (103.9, 0.1)):
            with self.subTest(when=when):
                self.clock.return_value = when
                self.panel._frame()
                self.assertAlmostEqual(self.painted_text()[0][2], opacity)
        self.clock.return_value = 104.0
        self.panel._frame()
        self.assertEqual(self.panel._popups, [])
        self.assertFalse(self.panel.isVisible())
        self.assertFalse(self.panel._anim.isActive())

    def test_popup_lifetime_is_independent_of_wall_clock(self) -> None:
        self.fire()
        with patch("mnmparse.app.timer_panel.time.time", return_value=10**12):
            self.panel._frame()
            self.assertTrue(self.panel.isVisible())
            self.assertEqual(self.painted_text()[0][2], 1.0)

    def test_newest_four_notices_remain_in_chronological_order(self) -> None:
        for index in range(6):
            self.clock.return_value = 100.0 + index / 10
            self.fire(f"Trigger {index}")
        self.assertEqual([text for text, _, _ in self.painted_text()],
                         [f"Trigger {index}" for index in range(2, 6)])

    def test_timer_fires_do_not_displace_or_extend_existing_popups_at_capacity(self) -> None:
        from mnmparse.app.timer_panel import MAX_POPUPS

        for index in range(MAX_POPUPS):
            self.fire(f"Notice {index}")
        popups = list(self.panel._popups)
        self.clock.return_value = 102.0
        for index in range(MAX_POPUPS + 1):
            self.fire(f"Timer {index}", timer=True, timer_seconds=120)
        self.assertEqual(self.panel._popups, popups)
        self.clock.return_value = 104.0
        self.panel._frame()
        self.assertEqual(self.panel._popups, [])
        self.assertEqual(len(self.panel.timers()), MAX_POPUPS + 1)
        self.assertTrue(self.panel.isVisible())

    def test_popups_are_below_all_eight_timer_rows_and_not_cancellable(self) -> None:
        from mnmparse.app.timer_panel import MAX_ROWS

        for index in range(MAX_ROWS + 1):
            self.fire(f"Timer {index}", timer=True, timer_seconds=120)
        self.fire("Proc", timer_label="84 damage")
        drawn = self.painted_text()
        self.assertEqual(len(self.panel._rows), MAX_ROWS)
        last_timer_bottom = self.panel._rows[-1][0].bottom()
        popup_texts = [(text, top) for text, top, _ in drawn if top > last_timer_bottom]
        self.assertEqual([text for text, _ in popup_texts], ["84 damage"])
        self.assertEqual(len(self.runner.board.timers), MAX_ROWS + 1)
        popup_point = QPoint(20, round(popup_texts[-1][1] + self.panel._row_h() / 2))
        self.assertIsNone(self.panel._timer_at(QPointF(popup_point)))
        menu = QMenu()
        self.panel.add_menu_entries(menu, SimpleNamespace(pos=lambda: popup_point))
        self.assertEqual([a.text() for a in menu.actions()], ["Cancel all timers", "Always show this panel"])
        old_height = self.panel.height()
        self.clock.return_value = 104.0
        self.panel._frame()
        self.assertLess(self.panel.height(), old_height)
        self.assertTrue(self.panel.isVisible())
        self.assertTrue(self.panel._anim.isActive())

    def test_cancel_timers_leaves_recent_notifications_visible(self) -> None:
        self.fire("Root", timer=True)
        self.fire("Righteous Smite")
        self.runner.clear_timers()
        self.assertEqual(self.panel.timers(), [])
        self.assertTrue(self.panel.isVisible())
        self.assertEqual([text for text, _, _ in self.painted_text()], ["Righteous Smite"])

    def test_hidden_overlay_never_replays_expired_popups(self) -> None:
        self.owner.hide()
        self.panel.sync()
        self.fire()
        self.assertFalse(self.panel.isVisible())
        self.assertFalse(self.panel._anim.isActive())
        self.clock.return_value = 105.0
        self.owner.show()
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel._popups, [])

    def test_reshown_panel_resumes_fade_and_timer_animation(self) -> None:
        self.fire("Root", timer=True)
        self.fire("Righteous Smite")
        self.owner.hide()
        self.panel.hide()
        self.assertFalse(self.panel._anim.isActive())
        self.clock.return_value = 103.5
        self.owner.show()
        self.panel.sync()
        self.assertTrue(self.panel._anim.isActive())
        self.assertAlmostEqual(self.painted_text()[-1][2], 0.5)
        self.owner.hide()
        self.panel.hide()
        self.clock.return_value = 105.0
        self.owner.show()
        self.panel.sync()
        self.assertEqual(self.panel._popups, [])
        self.assertTrue(self.panel._anim.isActive())

    def test_disabled_panel_does_not_show_and_prunes_before_reenabling(self) -> None:
        self.panel.enabled = False
        self.fire()
        self.assertFalse(self.panel.isVisible())
        self.clock.return_value = 105.0
        self.panel.enabled = True
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())

    def test_always_show_returns_to_empty_state_after_popup_expires(self) -> None:
        self.panel.set_always_show(True)
        self.fire()
        self.clock.return_value = 104.0
        self.panel._frame()
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel._anim.isActive())
        self.assertEqual([text for text, _, _ in self.painted_text()], ["Timers: none running"])

    def test_switching_runner_disconnects_old_signals_and_does_not_duplicate(self) -> None:
        from mnmparse.app.triggers_runtime import TriggerRunner

        self.fire("Before")
        other = TriggerRunner(TriggerStore(Path(self.tmp.name) / "other.json"))
        other.audio.run = Mock()
        self.panel.set_runner(other)
        self.panel.set_runner(other)
        self.assertEqual(self.panel._popups, [])
        self.fire("Disconnected")
        self.assertEqual(self.panel._popups, [])
        trigger = Trigger(name="New", pattern="New", action="none")
        other.store.triggers.append(trigger)
        other.observe("New")
        self.assertEqual([popup.label for popup in self.panel._popups], ["New"])
        self.panel.close()
        other.observe("New")
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel._popups, [])

    def test_cooldown_and_retain_ignored_matches_do_not_generate_popups(self) -> None:
        cooldown = Trigger(name="Cooldown", pattern="Cooldown", cooldown_s=10, action="none")
        retain = Trigger(name="Retain", pattern="Retain", timer=True, timer_mode="retain", action="none")
        self.runner.store.triggers = [cooldown, retain]
        for name in ("Cooldown", "Retain"):
            self.runner.observe(name, now=100)
            self.runner.observe(name, now=101)
        self.runner._clock.stop()
        self.assertEqual([popup.label for popup in self.panel._popups], ["Cooldown"])
        self.assertEqual(len(self.runner.board.timers), 1)
        self.assertEqual(self.runner.board.timers[0].start, 100)

    def test_font_scaling_and_popup_height_update_docking(self) -> None:
        self.fire()
        initial_height = self.panel.height()
        self.owner.panels_changed.reset_mock()
        self.panel.set_font_px(20)
        self.assertGreater(self.panel.height(), initial_height)
        self.owner.panels_changed.assert_called()
        self.assertEqual(self.panel.width(), self.owner.frameGeometry().width())


if __name__ == "__main__":
    unittest.main()
