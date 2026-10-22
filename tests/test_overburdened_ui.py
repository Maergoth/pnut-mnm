"""The weight warning shares passive edge rendering and the mini-stat options."""
from __future__ import annotations

from dataclasses import replace
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app.low_health import LOW_HEALTH_COLOR, OVERBURDENED_COLOR
from mnmparse.config import Config, load_config
from tests import test_map_lock_wiring as wiring


class OverburdenedUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wiring.MapLockWiringTests.setUpClass()
        cls.app = wiring.MapLockWiringTests.qt_app

    def setUp(self):
        self.fixture = wiring.MapLockWiringTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_gray_warning_stays_until_recovery_and_uses_no_sound(self):
        with self.fixture.health_warning(reduced_motion=True) as (host, flash, factory, triggers, geometry):
            host.engine.overburdened_changed.emit(True)
            factory.assert_called_once_with()
            flash.set_warning_color.assert_called_with(OVERBURDENED_COLOR)
            flash.flash.assert_called_with(geometry, reduced_motion=True)
            host.engine.overburdened_changed.emit(True)
            self.assertEqual(factory.call_count, 1)
            host.engine.overburdened_changed.emit(False)
            flash.stop.assert_called()
            triggers.fire.assert_not_called()

    def test_low_health_takes_priority_and_recovers_to_gray_while_weight_remains_high(self):
        with self.fixture.health_warning() as (host, flash, factory, _triggers, _geometry):
            host.engine.overburdened_changed.emit(True)
            host.engine.low_health_changed.emit(True)
            flash.set_warning_color.assert_called_with(LOW_HEALTH_COLOR)
            host.engine.overburdened_changed.emit(False)
            flash.set_warning_color.assert_called_with(LOW_HEALTH_COLOR)
            host.engine.overburdened_changed.emit(True)
            host.engine.low_health_changed.emit(False)
            flash.set_warning_color.assert_called_with(OVERBURDENED_COLOR)
            self.assertEqual(factory.call_count, 1)

    def test_gray_toggle_is_independent_of_health_and_restores_an_existing_weight_episode(self):
        with self.fixture.health_warning(low_health_enabled=False) as (host, flash, factory, _triggers, geometry):
            host.engine.overburdened_changed.emit(True)
            factory.assert_called_once_with()
            host.on_config_changed(replace(host.cfg, overburdened_flash=False))
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host._sync_low_health_flash()
            flash.flash.assert_not_called()
            host.on_config_changed(replace(host.cfg, overburdened_flash=True))
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            flash.set_warning_color.assert_called_with(OVERBURDENED_COLOR)

    def test_weight_warning_uses_native_monitor_lookup_before_qt_logical_coordinates(self):
        from PySide6.QtCore import QRect

        secondary = SimpleNamespace(geometry=lambda: QRect(1920, 0, 1280, 720))
        with self.fixture.health_warning() as (host, flash, _factory, _triggers, _geometry), \
                patch("mnmparse.capture.find_game_window_info", return_value=SimpleNamespace(rect=(3000, 0, 3600, 1080))), \
                patch("mnmparse.app.read_area_gears.screen_at_physical", return_value=secondary) as lookup:
            host.engine.overburdened_changed.emit(True)
            lookup.assert_called_with((3300, 540))
            flash.flash.assert_called_with(secondary.geometry(), reduced_motion=False)

    def test_shared_hide_and_capture_pause_stop_weight_warning_until_new_confirmation(self):
        with self.fixture.health_warning() as (host, flash, _factory, _triggers, geometry):
            host.engine.overburdened_changed.emit(True)
            host.set_overlay_visible(False)
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host.set_overlay_visible(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            host.engine.state = "paused"
            host.engine.state_changed.emit("paused")
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host.engine.state = "running"
            host.engine.state_changed.emit("running")
            flash.flash.assert_not_called()

    def test_weight_setting_can_be_saved_and_reverted_under_mini_stats(self):
        host = self.fixture.bootstrap(Config())
        options = host.window.page("settings")
        self.assertTrue(options.overburdened_flash.isChecked())
        self.assertIs(options.overburdened_flash.parentWidget(), options.player_status_enabled.parentWidget())
        options.overburdened_flash.setChecked(False)
        self.assertFalse(options.form_config().overburdened_flash)
        options.revert()
        self.assertTrue(options.overburdened_flash.isChecked())
        options.low_health_enabled.setChecked(False)
        self.assertTrue(options.overburdened_flash.isEnabled())
        options.overburdened_flash.setChecked(False)
        options.save()
        self.assertFalse(load_config(str(host.config_path)).overburdened_flash)
        options.player_status_enabled.setChecked(False)
        self.assertFalse(options.overburdened_flash.isEnabled())

    def test_read_area_shortcuts_use_the_settings_highlight_and_share_lifecycle(self):
        from mnmparse.app.settings_highlight import SettingsHighlight

        host = self.fixture.bootstrap(Config(reduced_motion=True))
        helper = Mock()
        host._read_area_gears = helper
        host.engine.state = "running"
        host._sync_read_area_gears()
        helper.start.assert_called_once()
        helper.refresh.assert_called_once()
        for target in ("player_status_enabled", "crop_picker"):
            host._open_overlay_settings(target)
            self.app.processEvents()
            visible = [outline for outline in host.window.findChildren(SettingsHighlight) if outline.isVisible()]
            self.assertEqual(len(visible), 1)
            control = getattr(host.window.page("settings"), target)
            self.assertTrue(visible[0].parentWidget() is control or visible[0].parentWidget().isAncestorOf(control))
        host.set_overlay_visible(False)
        helper.stop.assert_called()
        host.engine.read_area_snapshot = lambda: {"hwnd": 123}
        self.assertEqual(host._read_area_snapshot(), {"hwnd": 123})


if __name__ == "__main__":
    unittest.main()
