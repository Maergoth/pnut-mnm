"""Overlay settings destinations highlight briefly and share one weapon-delay preference."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QGraphicsOpacityEffect, QInputDialog, QPushButton, QWidget

from mnmparse.app.attack_bar import MAX_WEAPON_DELAY
from mnmparse.app.pages import SettingsPage
from mnmparse.app.profile_tools import apply_settings, settings_payload
from mnmparse.app.settings_highlight import HIGHLIGHT_MS, SettingsHighlight
from mnmparse.config import Config
from mnmparse.parser import parse_line
from tests import test_map_lock_wiring as wiring


class SettingsHighlightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.target = QWidget()
        self.target.resize(240, 100)
        self.target.setStyleSheet("background: #202020; border-radius: 7px;")
        self.button = QPushButton("Existing control", self.target)
        self.button.setGeometry(20, 20, 160, 40)
        self.target.show()
        self.app.processEvents()

    def tearDown(self):
        self.target.close()
        self.target.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()

    def highlight(self, *, static=False):
        with patch("mnmparse.app.settings_highlight.reduced_motion", return_value=static):
            return SettingsHighlight(self.target, Config())

    def test_outline_is_transparent_to_input_and_preserves_layout_style_and_effect(self):
        effect = QGraphicsOpacityEffect(self.target)
        self.target.setGraphicsEffect(effect)
        original = (self.target.geometry(), self.button.geometry(), self.target.styleSheet())
        highlight = self.highlight()
        self.assertTrue(highlight.isVisible())
        self.assertTrue(highlight.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents))
        self.assertEqual(highlight.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertIs(self.target.graphicsEffect(), effect)
        self.assertEqual((self.target.geometry(), self.button.geometry(), self.target.styleSheet()), original)
        self.assertEqual(highlight.geometry(), self.target.rect())
        self.target.resize(280, 120)
        self.assertEqual(highlight.geometry(), self.target.rect())
        highlight._elapsed = SimpleNamespace(elapsed=lambda: HIGHLIGHT_MS)
        highlight._tick()
        self.assertFalse(highlight.isVisible())
        self.assertFalse(highlight._timer.isActive())
        self.assertIs(self.target.graphicsEffect(), effect)

    def test_reduced_motion_has_one_static_brief_outline(self):
        highlight = self.highlight(static=True)
        self.assertTrue(highlight._static)
        self.assertEqual(highlight._timer.interval(), HIGHLIGHT_MS)
        highlight._elapsed = SimpleNamespace(elapsed=lambda: 10)
        first = highlight.grab().toImage()
        highlight._elapsed = SimpleNamespace(elapsed=lambda: 1000)
        self.assertEqual(highlight.grab().toImage(), first)
        highlight._elapsed = SimpleNamespace(elapsed=lambda: HIGHLIGHT_MS)
        highlight._timer.timeout.emit()
        self.assertFalse(highlight.isVisible())
        self.assertFalse(highlight._timer.isActive())

    def test_config_and_windows_reduced_motion_preferences_use_existing_helper(self):
        self.assertTrue(SettingsHighlight(self.target, Config(reduced_motion=True))._static)
        with patch("mnmparse.app.morality.sys.platform", "win32"), \
                patch("winreg.OpenKey") as key, patch("winreg.QueryValueEx", return_value=("0", 1)):
            self.assertTrue(SettingsHighlight(self.target, Config())._static)
            key.assert_called_once()

    def test_hiding_or_destroying_destination_stops_pending_animation(self):
        highlight = self.highlight()
        self.target.hide()
        self.assertFalse(highlight.isVisible())
        self.assertFalse(highlight._timer.isActive())
        self.target.show()
        second = self.highlight()
        destroyed = []
        second.destroyed.connect(lambda: destroyed.append(True))
        self.target.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertEqual(destroyed, [True])
        self.target = QWidget()  # Keep teardown independent of the deleted destination.


class SettingsShortcutIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wiring.MapLockWiringTests.setUpClass()
        cls.app = QApplication.instance()

    def setUp(self):
        self.fixture = wiring.MapLockWiringTests()
        self.fixture.setUp()
        self.settings = self.fixture.settings

    def tearDown(self):
        self.fixture.tearDown()

    def bootstrap(self):
        return self.fixture.bootstrap(Config(player_name="SampleHero", reduced_motion=True,
                                             revenge_enabled=True,
                                             log_dir=str(Path(self.fixture.temp.name) / "logs")))

    def highlights(self, host):
        return [widget for widget in host.window.findChildren(SettingsHighlight) if widget.isVisible()]

    def test_gears_restore_main_window_reveal_real_destination_and_highlight_once(self):
        host = self.bootstrap()
        options = host.window.page("settings")
        host.revenge.save_attacker("FictionalRival")
        host.show_revenge_popout()
        for panel, target_name in ((host.overlay, "overlay_enabled"),
                                   (host.overlay.attack_bar, "weapon_delay"),
                                   (host.overlay.npc_panel, "npc_enabled"),
                                   (host.map_overlay, "map_download_button"),
                                   (host.revenge_popout, "revenge_enabled")):
            with self.subTest(target=target_name):
                host.window.hide()
                panel.show()
                gear = panel._settings_gear
                with patch("mnmparse.app.overlay_settings.QCursor.pos",
                           return_value=panel.mapToGlobal(QPoint(10, 10))):
                    gear.sync()
                    gear.click()
                self.app.processEvents()
                self.assertTrue(host.window.isVisible())
                self.assertIs(host.window._stack.currentWidget(), options)
                highlighted, = self.highlights(host)
                control = getattr(options, target_name)
                self.assertTrue(highlighted.parentWidget() is control or highlighted.parentWidget().isAncestorOf(control))
                point = control.mapTo(options._scroll.viewport(), control.rect().center())
                self.assertTrue(options._scroll.viewport().rect().contains(point))
                self.assertTrue(highlighted._static)
                self.assertEqual(len(self.highlights(host)), 1)

    def test_timer_gear_highlights_timer_list_without_opening_an_editor_or_dialog(self):
        host = self.bootstrap()
        host.window.hide()
        host.overlay.timer_panel.set_always_show(True)
        gear = host.overlay.timer_panel._settings_gear
        with patch("mnmparse.app.overlay_settings.QCursor.pos",
                   return_value=host.overlay.timer_panel.mapToGlobal(QPoint(10, 10))):
            gear.sync()
            gear.click()
        self.app.processEvents()
        page = host.window.page("triggers")
        self.assertIs(host.window._stack.currentWidget(), page)
        highlight, = self.highlights(host)
        self.assertIs(highlight.parentWidget(), page.timer_help.parentWidget())
        self.assertIsNone(page._current)
        host.window.show_page("live")
        self.assertFalse(highlight._timer.isActive())

    def test_leaving_options_before_deferred_layout_does_not_highlight_hidden_destination(self):
        host = self.bootstrap()
        host.window.show()
        host.window._open_settings_option("weapon_delay")
        host.window.show_page("live")
        self.app.processEvents()
        self.assertEqual(self.highlights(host), [])

    def test_weapon_delay_is_next_to_auto_attack_uses_stat_units_and_applies_immediately(self):
        self.settings.setValue("attack_bar/weapon_delay", 36)
        host = self.bootstrap()
        options, bar = host.window.page("settings"), host.overlay.attack_bar
        self.assertIs(options.weapon_delay.parentWidget(), options.attack_bar.parentWidget())
        self.assertEqual((options.weapon_delay.minimum(), options.weapon_delay.maximum()), (0, MAX_WEAPON_DELAY))
        self.assertEqual(options.weapon_delay.specialValueText(), "Automatic")
        self.assertIn("36 = 3.6 seconds", options.weapon_delay.accessibleDescription())
        self.assertEqual((options.weapon_delay.value(), bar.weapon_delay, bar.tracker.weapon_delay), (36, 36, 3.6))
        cfg = options.form_config()
        options.weapon_delay.setValue(42)
        self.assertEqual((bar.weapon_delay, bar.tracker.weapon_delay), (42, 4.2))
        self.assertEqual(self.settings.value("attack_bar/weapon_delay", type=int), 42)
        self.assertEqual(options.form_config(), cfg)
        self.settings.sync()
        reopened = SettingsPage(host.engine, host.cfg, QSettings(self.settings.fileName(), QSettings.Format.IniFormat))
        try:
            self.assertEqual(reopened.weapon_delay.value(), 42)
        finally:
            reopened.deleteLater()

    def test_right_click_editor_and_options_sync_both_directions_without_resetting_swings(self):
        host = self.bootstrap()
        options, bar = host.window.page("settings"), host.overlay.attack_bar
        for timestamp in (100, 103, 106, 109):
            bar.tracker.observe(parse_line("You crush a rat for 10 points of damage.", timestamp, "SampleHero"))
        learned = bar.tracker.hands(109)[0]
        previous = (learned.delay, learned.last, learned.swings)
        options.weapon_delay.setValue(36)

        def accept(dialog):
            self.assertEqual(dialog.intValue(), 36)
            dialog.setIntValue(50)
            return QDialog.DialogCode.Accepted

        with patch.object(QInputDialog, "exec", accept):
            bar._edit_weapon_delay()
        self.assertEqual((options.weapon_delay.value(), bar.weapon_delay), (50, 50))
        options.weapon_delay.setValue(0)
        self.assertIsNone(bar.tracker.weapon_delay)
        with patch.object(QInputDialog, "exec", return_value=QDialog.DialogCode.Rejected):
            bar._edit_weapon_delay()
        self.assertEqual((options.weapon_delay.value(), bar.weapon_delay), (0, 0))
        hand = bar.tracker.hands(109)[0]
        self.assertEqual((hand.delay, hand.last, hand.swings), previous)

    def test_typing_weapon_delay_commits_once_without_seeding_partial_digits(self):
        host = self.bootstrap()
        options, bar = host.window.page("settings"), host.overlay.attack_bar
        host._open_overlay_settings("weapon_delay")
        self.app.processEvents()
        bar.tracker.observe(parse_line("You crush a rat for 10 points of damage.", 100, "SampleHero"))
        self.assertIsNone(bar.tracker.hands(100)[0].delay)
        changes = []
        bar.weapon_delay_changed.connect(changes.append)
        editor = options.weapon_delay.lineEdit()
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "36")
        self.assertEqual(changes, [])
        self.assertEqual(options.weapon_delay.value(), 0)
        self.assertFalse(self.settings.contains("attack_bar/weapon_delay"))
        self.assertIsNone(bar.tracker.hands(100)[0].delay)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        self.assertEqual(changes, [36])
        self.assertEqual(self.settings.value("attack_bar/weapon_delay", type=int), 36)
        self.assertEqual(bar.tracker.hands(100)[0].delay, 3.6)

    def test_restored_backup_preference_updates_both_controls_without_config_migration(self):
        host = self.bootstrap()
        options, bar = host.window.page("settings"), host.overlay.attack_bar
        for timestamp in (100, 103, 106, 109):
            bar.tracker.observe(parse_line("You crush a rat for 10 points of damage.", timestamp, "SampleHero"))
        hand = bar.tracker.hands(109)[0]
        learned = (hand.delay, hand.last, hand.swings)
        options.weapon_delay.setValue(36)
        payload = settings_payload(self.settings)
        self.assertEqual(payload["values"]["attack_bar/weapon_delay"], 36)
        options.weapon_delay.setValue(50)
        apply_settings(self.settings, payload)
        host._on_preferences_restored(host.cfg)
        self.assertEqual((options.weapon_delay.value(), bar.weapon_delay, bar.tracker.weapon_delay), (36, 36, 3.6))
        hand = bar.tracker.hands(109)[0]
        self.assertEqual((hand.delay, hand.last, hand.swings), learned)
        self.assertNotIn("weapon_delay", {field.name for field in dataclasses.fields(Config)})


if __name__ == "__main__":
    unittest.main()
