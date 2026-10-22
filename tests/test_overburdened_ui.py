"""The weight warning shares passive edge rendering and the mini-stat options."""
from __future__ import annotations

from contextlib import ExitStack
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
        host._read_area_gears.shutdown()
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
        helper.stop.assert_not_called()
        self.assertFalse(host.overlay.isVisible())
        host.engine.state = "paused"
        host._sync_read_area_gears()
        helper.stop.assert_called()
        host.engine.read_area_snapshot = lambda: {"hwnd": 123}
        self.assertEqual(host._read_area_snapshot(), {"hwnd": 123})

    def test_real_reader_controls_hover_and_route_actions_with_combat_overlays_hidden(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from mnmparse.app.read_area_gears import ScreenMapping

        # Reproduce a saved hidden overlay group at launch, not just a later toggle.
        self.fixture.settings.setValue("main/overlay_visible", False)
        host = self.fixture.bootstrap(Config(reduced_motion=True))
        gears = host._read_area_gears
        for window in gears._windows.values():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        current = {"point": (500, 600), "foreground": True}
        host.engine.read_area_snapshot = lambda: {
            "hwnd": 123, "capture_backend": "wgc", "frame_dimensions": (1000, 800),
            "source_rect": (0, 0, 1000, 800), "chat_crop": (100, 400, 900, 750),
            "stats_crop": (50, 100, 300, 250),
        }
        host.engine.leaderboard_status = lambda: {
            "alias": "Sample Amber Finch", "eligible": True, "copy_eligible": True,
            "submitted_day": "", "error": "",
        }
        host.engine.leaderboard_copy_text = lambda: "Fictional local mini stats"
        record = {"synthetic": "submission routed only to the mocked client"}
        host.engine.leaderboard_submission = lambda: record

        def hover(point):
            current["point"] = point
            gears.refresh()
            self.app.processEvents()

        with ExitStack() as stack:
            stack.enter_context(patch("mnmparse.app.read_area_gears._cursor_position",
                                      side_effect=lambda: current["point"]))
            stack.enter_context(patch("mnmparse.app.read_area_gears._is_foreground",
                                      side_effect=lambda _hwnd: current["foreground"]))
            stack.enter_context(patch("mnmparse.app.read_area_gears._window_rectangles",
                                      return_value={"frame": (0, 0, 1000, 800)}))
            stack.enter_context(patch("mnmparse.app.read_area_gears._screen_mappings", return_value=(
                ScreenMapping((0, 0, 1920, 1080), (0, 0, 1920, 1080), 1.0),)))
            stack.enter_context(patch("mnmparse.app.read_area_gears._exclude_own_window", return_value=True))
            submit = stack.enter_context(patch.object(host._leaderboard_client, "submit", return_value=True))
            open_settings = stack.enter_context(patch.object(host, "_open_overlay_settings"))
            host.engine.state = "running"
            host._sync_read_area_gears()
            self.assertTrue(gears._geometry_timer.isActive())
            self.assertTrue(gears._hover_timer.isActive())
            self.assertFalse(host.overlay.isVisible())
            self.assertFalse(host.map_overlay.isVisible())
            self.assertFalse(host.revenge_popout.isVisible())
            chat = gears._windows["crop_picker"]
            self.assertTrue(chat.button.isVisible())
            hover((chat.x() + 10, chat.y() + 10))
            QTest.mouseClick(chat.button, Qt.MouseButton.LeftButton)
            open_settings.assert_called_once_with("crop_picker")

            hover((100, 150))
            for key in ("player_status_enabled", "copy_stats", "submit_stats"):
                self.assertTrue(gears._windows[key].button.isVisible(), key)
                self.assertTrue(gears._windows[key].button.isEnabled(), key)
            copy = gears._windows["copy_stats"]
            hover((copy.x() + 10, copy.y() + 10))
            QTest.mouseClick(copy.button, Qt.MouseButton.LeftButton)
            self.assertEqual(self.app.clipboard().text(), "Fictional local mini stats")
            submit.assert_not_called()
            upload = gears._windows["submit_stats"]
            hover((upload.x() + 10, upload.y() + 10))
            QTest.mouseClick(upload.button, Qt.MouseButton.LeftButton)
            submit.assert_called_once_with(record)

            # Existing safety gates still apply with the overlay group hidden.
            current["foreground"] = False
            hover((100, 150))
            self.assertFalse(any(window.isVisible() for window in gears._windows.values()))
            current["foreground"] = True
            host.overlay.set_click_through(True)
            hover((100, 150))
            self.assertFalse(any(window.isVisible() for window in gears._windows.values()))
            host.overlay.set_click_through(False)
            hover((100, 150))
            self.assertTrue(copy.button.isVisible())
            host.set_overlay_visible(False)
            self.assertTrue(gears._hover_timer.isActive())
            for flag in ("_demo_active", "_selftest", "_shut_down"):
                with self.subTest(flag=flag):
                    setattr(host, flag, True)
                    host._sync_read_area_gears()
                    self.assertFalse(gears._hover_timer.isActive())
                    self.assertFalse(any(window.isVisible() for window in gears._windows.values()))
                    setattr(host, flag, False)
                    host._sync_read_area_gears()
            host.engine.state = "paused"
            host._sync_read_area_gears()
            self.assertFalse(gears._geometry_timer.isActive())
            self.assertFalse(any(window.isVisible() for window in gears._windows.values()))


if __name__ == "__main__":
    unittest.main()
