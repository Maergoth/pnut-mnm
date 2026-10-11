"""Incoming PvP candidates appear in the floating timers without new lifetimes."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.revenge import RevengeController
from mnmparse.app.timer_panel import MAX_ROWS, TimerPanel
from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.triggers import TriggerStore


class _Owner(QWidget):
    locked = True
    click_through = False
    opacity = 0.85

    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.resize(600, 100)
        self.panels_changed = Mock()

    def dock_anchor(self, _panel):
        return self.frameGeometry()


class RevengeTimerPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.cfg = Config(player_name="Hero", revenge_enabled=True)
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.controller = RevengeController(self.cfg, Path(self.tmp.name) / "revenge.json", clock=lambda: self.now)
        self.owner = _Owner()
        self.owner.show()
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowDoesNotAcceptFocus
        self.panel = TimerPanel(self.owner, self.settings, flags)
        self.panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.panel.set_revenge_controller(self.controller)
        self.runner = TriggerRunner(TriggerStore(Path(self.tmp.name) / "triggers.json"))
        self.runner.audio = Mock()
        self.extra_windows = []

    def tearDown(self):
        self.panel.close()
        self.owner.close()
        for window in self.extra_windows:
            window.close()
            window.deleteLater()
        self.runner._clock.stop()
        for name in ("_timer", "_save_timer", "_activity_refresh", "_display_timer"):
            getattr(self.controller, name).stop()
        self.panel.deleteLater()
        self.owner.deleteLater()
        self.runner.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def attack(self, name="Caster"):
        text = f"{name}'s Lesser Iceblast hits YOU for 177 points of Cold Damage!"
        event = parse_line(text, self.now, "Hero")
        self.controller.on_message(SimpleNamespace(backlog=False, text=text), event)
        return event

    def painted_text(self):
        drawn = []

        class Painter(QPainter):
            def drawText(self, rect, flags, text):
                drawn.append((text, rect.top(), self.opacity()))
                return super().drawText(rect, flags, text)

        with patch("mnmparse.app.timer_panel.QPainter", Painter):
            self.panel.grab()
        return drawn

    def test_incoming_cast_shows_plus_and_save_without_any_running_timer(self):
        self.attack()
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel.timers(), [])
        self.assertEqual([text for text, _, _ in self.painted_text()], ["30s", "Revenge: Caster"])
        button = self.panel._revenge_buttons["caster"]
        self.assertEqual(button.text(), "+")
        self.assertTrue(button.isVisible())
        self.assertGreater(button.x(), self.panel.width() / 2)
        self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        button.click()
        self.assertEqual(self.controller.saved_names, ("Caster",))
        self.assertEqual(self.panel._revenge_buttons, {})
        self.assertFalse(self.panel.isVisible())
        self.assertTrue((Path(self.tmp.name) / "revenge.json").exists())

    def test_original_thirty_second_lifetime_fades_then_expires_without_reprompt(self):
        self.attack()
        self.now = 129.5
        self.panel._frame()
        self.assertAlmostEqual(self.panel._revenge_buttons["caster"].graphicsEffect().opacity(), 0.5)
        for _, _, opacity in self.painted_text():
            self.assertAlmostEqual(opacity, 0.5)
        self.now = 130.0
        self.panel._frame()
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel._revenge_buttons, {})
        self.panel._save_revenge_attacker("Caster")
        self.assertEqual(self.controller.saved_names, ())
        self.attack()
        self.assertFalse(self.panel.isVisible())
        self.controller.new_session()
        self.now += 1.0
        self.attack()
        self.assertTrue(self.panel.isVisible())

    def test_hiding_or_rewiring_does_not_extend_candidate_lifetime(self):
        self.attack()
        self.owner.hide()
        self.panel.sync()
        self.now = 120.0
        self.panel.set_revenge_controller(self.controller)
        self.owner.show()
        self.panel.sync()
        self.assertIn("10s", [text for text, _, _ in self.painted_text()])
        self.owner.hide()
        self.panel.sync()
        self.now = 131.0
        self.owner.show()
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.assertEqual(self.panel._revenge_buttons, {})

    def test_disabled_feature_clears_prompt_and_leaves_timer_visible(self):
        self.panel.set_runner(self.runner)
        self.runner.start_one_time_timer("Rebuff", 300, now=self.now)
        self.attack()
        self.controller.set_config(replace(self.cfg, revenge_enabled=False))
        self.assertEqual(self.panel._revenge_buttons, {})
        self.assertTrue(self.panel.isVisible())
        self.assertEqual([timer.label for timer in self.panel.timers()], ["Rebuff"])
        self.now = 101.0
        self.attack("Newcaster")
        self.assertEqual(self.panel._revenge_buttons, {})
        self.controller.set_config(self.cfg)
        self.now = 102.0
        self.attack("Newcaster")
        self.assertIn("newcaster", self.panel._revenge_buttons)

    def test_backlog_and_npc_names_do_not_create_overlay_candidates(self):
        text = "Caster's Lesser Iceblast hits YOU for 177 points of Cold Damage!"
        self.controller.on_message(SimpleNamespace(backlog=True, text=text), parse_line(text, self.now, "Hero"))
        self.attack("a skeleton")
        self.assertEqual(self.panel._revenge_buttons, {})
        self.assertFalse(self.panel.isVisible())

    def test_new_candidate_is_visible_above_full_timer_list_and_rows_scroll(self):
        self.panel.set_runner(self.runner)
        for index in range(MAX_ROWS + 1):
            self.runner.start_one_time_timer(f"Rebuff {index}", 300, now=self.now)
        self.panel._scroll.setValue(1)
        self.attack()
        self.assertEqual(self.panel._scroll.value(), 0)
        self.assertEqual(self.panel._scroll.maximum(), 2)
        self.assertTrue(self.panel._revenge_buttons["caster"].isVisible())
        self.assertEqual(len(self.panel._visible_timers()), MAX_ROWS - 1)
        self.painted_text()
        self.assertTrue(all(rect.top() > self.panel._row_rect(0).bottom() for rect, _ in self.panel._rows))
        self.panel._scroll.setValue(2)
        self.assertEqual(self.panel._revenge_buttons, {})
        self.assertEqual(len(self.panel._visible_timers()), MAX_ROWS)
        self.attack("Anothercaster")
        self.assertEqual(self.panel._scroll.value(), 0)
        self.assertEqual(set(self.panel._revenge_buttons), {"caster", "anothercaster"})

    def test_replacing_controller_disconnects_old_candidates(self):
        self.attack()
        replacement = RevengeController(self.cfg, Path(self.tmp.name) / "other.json", clock=lambda: self.now)
        self.addCleanup(replacement.deleteLater)
        self.addCleanup(replacement._display_timer.stop)
        self.addCleanup(replacement._timer.stop)
        self.panel.set_revenge_controller(replacement)
        self.attack("Anothercaster")
        self.assertEqual(self.panel._revenge_buttons, {})
        replacement.observe("Bandit hits YOU for 5 points of damage.")
        self.assertEqual(set(self.panel._revenge_buttons), {"bandit"})

    def test_ninth_attacker_is_visible_first_and_older_candidates_remain_scrollable(self):
        names = [f"Caster{letter}" for letter in "ABCDEFGHI"]
        for name in names:
            self.attack(name)
            self.now += 0.1
        self.assertEqual(self.panel._scroll.value(), 0)
        self.assertEqual(self.panel._visible_revenge_candidates()[0].name, names[-1])
        self.assertIn(names[-1].casefold(), self.panel._revenge_buttons)
        self.assertNotIn(names[0].casefold(), self.panel._revenge_buttons)
        newest = self.panel._revenge_buttons[names[-1].casefold()]
        self.assertTrue(self.panel._row_rect(0).contains(newest.geometry().center()))
        self.panel._scroll.setValue(1)
        self.assertIn(names[0].casefold(), self.panel._revenge_buttons)
        oldest = self.panel._visible_revenge_candidates()[-1]
        self.assertEqual((oldest.appeared_at, oldest.expires_at), (100.0, 130.0))

    def test_existing_overlay_hook_connects_floating_timers_in_carebear_mode(self):
        overlay = OverlayWindow(self.settings, self.cfg)
        self.extra_windows.append(overlay)
        overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        overlay.timer_panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        overlay.set_revenge_controller(self.controller)
        overlay.show()
        self.attack()
        self.assertTrue(overlay.timer_panel.isVisible())
        self.assertEqual(set(overlay.timer_panel._revenge_buttons), {"caster"})
        self.assertTrue(self.cfg.casual_mode)


if __name__ == "__main__":
    unittest.main()
