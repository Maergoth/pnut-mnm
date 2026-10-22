"""Read-area shortcuts preserve physical geometry and intercept only their gear."""
from __future__ import annotations

import os
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from mnmparse.app import read_area_gears as reader_gears
from mnmparse.app.read_area_gears import ReadAreaGears, ScreenMapping, resolve_read_areas, screen_at_physical


def snapshot(**changes):
    return {"hwnd": 123, "capture_backend": "wgc", "frame_dimensions": (1000, 800),
            "source_rect": (0, 0, 1000, 800), "chat_crop": (100, 400, 900, 750),
            "stats_crop": (50, 100, 300, 250), **changes}


def fake_screen(name, rect, scale=1.0, handle=None):
    return SimpleNamespace(name=lambda: name, geometry=lambda: QRect(*rect), devicePixelRatio=lambda: scale,
                           nativeInterface=lambda: SimpleNamespace(handle=lambda: handle))


class ReadAreaGeometryTests(unittest.TestCase):
    def test_exact_composed_frame_and_client_origins_do_not_scale_outer_borders(self):
        bounds = {"outer": (-1510, -30, -490, 810), "frame": (-1508, -28, -492, 808),
                  "client": (-1500, 0, -500, 800)}
        areas = resolve_read_areas(snapshot(), window_rectangles=bounds)
        self.assertEqual(areas, {"crop_picker": (-1400, 400, -600, 750),
                                 "player_status_enabled": (-1450, 100, -1200, 250)})
        areas = resolve_read_areas(snapshot(frame_dimensions=(1016, 836)), window_rectangles=bounds)
        self.assertEqual(areas["crop_picker"], (-1408, 372, -608, 722))

    def test_mss_uses_only_exact_outer_window_pixels(self):
        bounds = {"outer": (100, 200, 1100, 1000), "client": (110, 230, 1110, 1030)}
        areas = resolve_read_areas(snapshot(capture_backend="mss"), window_rectangles=bounds)
        self.assertEqual(areas["crop_picker"], (200, 600, 1000, 950))
        self.assertEqual(resolve_read_areas(snapshot(capture_backend="mss", frame_dimensions=(980, 760)),
                                           window_rectangles=bounds), {})

    def test_no_matching_frame_or_current_window_never_reuses_stale_source_rect(self):
        self.assertEqual(resolve_read_areas(snapshot(), window_rectangles={}), {})
        self.assertEqual(resolve_read_areas(snapshot(), window_rectangles={"outer": (0, 0, 2000, 1600)}), {})
        self.assertEqual(resolve_read_areas(snapshot(capture_backend="unknown"),
                                           window_rectangles={"outer": (0, 0, 1000, 800)}), {})

    def test_crops_are_validated_independently_without_guessed_stat_region(self):
        bounds = {"frame": (0, 0, 1000, 800)}
        self.assertEqual(resolve_read_areas(snapshot(stats_crop=None), window_rectangles=bounds),
                         {"crop_picker": (100, 400, 900, 750)})
        self.assertEqual(resolve_read_areas(snapshot(chat_crop=(-1, 0, 100, 100)), window_rectangles=bounds),
                         {"player_status_enabled": (50, 100, 300, 250)})
        for values in ({"hwnd": True}, {"hwnd": 0}, {"frame_dimensions": (1000, 0)},
                       {"frame_dimensions": (True, 800)}, {"chat_crop": (0, 0, 1100, 800), "stats_crop": None}):
            with self.subTest(values=values):
                self.assertEqual(resolve_read_areas(snapshot(**values), window_rectangles=bounds), {})

    def test_physical_points_use_monitor_origin_and_its_own_dpi(self):
        left = ScreenMapping((-2560, -300, 0, 1140), (-2560, -200, -853, 760), 1.5)
        right = ScreenMapping((0, 0, 3840, 2160), (0, 0, 1920, 1080), 2.0)
        self.assertEqual(left.qt_point((-2410, -150)), QPoint(-2460, -100))
        self.assertEqual(right.qt_point((300, 200)), QPoint(150, 100))

    def test_warning_screen_lookup_matches_native_bounds_on_mixed_dpi_monitors(self):
        primary = fake_screen("AORUS FO32U2P", (0, 0, 2560, 1440), 1.5)
        secondary = fake_screen("ASUS VS228", (3840, 799, 1080, 1920))
        with patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                patch("mnmparse.app.read_area_gears._native_monitors", return_value={
                    "display1": (0, 0, 3840, 2160), "display2": (3840, 799, 4920, 2719)}), \
                patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[primary, secondary]):
            self.assertEqual(reader_gears._screen_mappings(), (
                ScreenMapping((0, 0, 3840, 2160), (0, 0, 2560, 1440), 1.5),
                ScreenMapping((3840, 799, 4920, 2719), (3840, 799, 4920, 2719), 1.0)))
            self.assertIs(screen_at_physical((4380, 1759)), secondary)
            self.assertIs(screen_at_physical((100, 100)), primary)
            self.assertIsNone(screen_at_physical((-3000, 0)))

    def test_native_monitor_handle_is_preferred_and_keeps_all_64_bits(self):
        import ctypes.wintypes as wt

        handle = (1 << 40) + 65537
        screen = fake_screen("Friendly monitor", (0, 0, 2560, 1440), 1.5, handle)

        def fill_monitor(actual_handle, pointer):
            self.assertEqual(actual_handle, handle)
            info = pointer._obj
            self.assertEqual(info.size, ctypes.sizeof(info))
            info.device = "DISPLAY1"
            info.monitor.left, info.monitor.top = 0, 0
            info.monitor.right, info.monitor.bottom = 3840, 2160
            return True

        get = Mock(side_effect=fill_monitor)
        with patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                patch("mnmparse.app.read_area_gears.ctypes.windll", create=True,
                      new=SimpleNamespace(user32=SimpleNamespace(GetMonitorInfoW=get))), \
                patch("mnmparse.app.read_area_gears._native_monitors", return_value={}), \
                patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[screen]):
            self.assertEqual(reader_gears._screen_mappings(), (
                ScreenMapping((0, 0, 3840, 2160), (0, 0, 2560, 1440), 1.5),))
            self.assertIs(screen_at_physical((3000, 2000)), screen)
        self.assertIs(get.argtypes[0], wt.HANDLE)
        self.assertIs(get.restype, wt.BOOL)

    def test_duplicate_friendly_names_negative_origins_and_fractional_rounding_match_geometry(self):
        left = fake_screen("Generic monitor", (-2560, -300, 1707, 960), 1.5)
        primary = fake_screen("Generic monitor", (0, 0, 1920, 1080), 2.0)
        with patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                patch("mnmparse.app.read_area_gears._native_monitors", return_value={
                    "display1": (-2560, -300, 0, 1140), "display2": (0, 0, 3840, 2160)}), \
                patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[left, primary]):
            mappings = reader_gears._screen_mappings()
            self.assertEqual(len(mappings), 2)
            self.assertEqual(mappings[0].qt_point((-2410, -150)), QPoint(-2460, -200))
            self.assertEqual(mappings[1].qt_point((300, 200)), QPoint(150, 100))
            self.assertIs(screen_at_physical((-2410, -150)), left)
            self.assertIs(screen_at_physical((3000, 2000)), primary)

    def test_ambiguous_fallback_or_duplicate_screen_assignment_fails_closed(self):
        screen = fake_screen("DISPLAY1", (0, 0, 1000, 800))
        with patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                patch("mnmparse.app.read_area_gears._native_monitors", return_value={
                    "display1": (0, 0, 1000, 800), "display2": (0, 0, 1001, 800)}), \
                patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[screen]):
            self.assertEqual(reader_gears._screen_mappings(), ())
            self.assertIsNone(screen_at_physical((100, 100)))
        with patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                patch("mnmparse.app.read_area_gears._native_monitors", return_value={
                    "display1": (0, 0, 1000, 800)}), \
                patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[screen, screen]):
            self.assertEqual(reader_gears._screen_mappings(), ())
            self.assertIsNone(screen_at_physical((100, 100)))

    def test_matching_name_cannot_override_wrong_geometry_or_invalid_dpi(self):
        for scale in (1.0, 0.0, -1.0, float("nan"), float("inf")):
            with self.subTest(scale=scale), patch("mnmparse.app.read_area_gears.sys.platform", "win32"), \
                    patch("mnmparse.app.read_area_gears._native_monitors", return_value={
                        "display1": (0, 0, 1000, 800)}), \
                    patch("mnmparse.app.read_area_gears.QGuiApplication.screens", return_value=[
                        fake_screen("DISPLAY1", (100, 0, 1000, 800), scale)]):
                self.assertEqual(reader_gears._screen_mappings(), ())


class ReadAreaGearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cursor = (1200, 900)
        self.foreground = self.enabled = True
        self.current = snapshot()
        self.bounds = {"outer": (0, 0, 1000, 800), "frame": (0, 0, 1000, 800)}
        self.screens = (ScreenMapping((0, 0, 1920, 1080), (0, 0, 1920, 1080), 1.0),)
        self.patches = [
            patch("mnmparse.app.read_area_gears._cursor_position", side_effect=lambda: self.cursor),
            patch("mnmparse.app.read_area_gears._is_foreground", side_effect=lambda _hwnd: self.foreground),
            patch("mnmparse.app.read_area_gears._window_rectangles", side_effect=lambda _hwnd: self.bounds),
            patch("mnmparse.app.read_area_gears._screen_mappings", side_effect=lambda: self.screens),
            patch("mnmparse.app.read_area_gears._exclude_own_window", return_value=True),
        ]
        self.mocks = [item.start() for item in self.patches]
        self.provider = Mock(side_effect=lambda: self.current)
        self.parent = QWidget()
        self.gears = ReadAreaGears(self.provider, input_enabled=lambda: self.enabled, parent=self.parent)
        for window in self.gears._windows.values():
            window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.opened = []
        self.gears.activated.connect(self.opened.append)

    def tearDown(self):
        if isValid(self.gears):
            self.gears.shutdown()
        if isValid(self.parent):
            self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        for item in reversed(self.patches):
            item.stop()

    def hover(self, point):
        self.cursor = point
        self.gears._poll_hover()
        self.app.processEvents()

    def test_hover_reveals_only_tiny_masked_nonactivating_gear_and_routes_click(self):
        self.gears.start()
        self.assertFalse(any(window.isVisible() for window in self.gears._windows.values()))
        self.hover((500, 600))
        chat = self.gears._windows["crop_picker"]
        self.assertTrue(chat.isVisible())
        self.assertTrue(chat.button.isVisible())
        self.assertEqual(chat.pos(), QPoint(100, 400))
        self.assertEqual(chat.size().width(), 30)
        self.assertEqual(chat.mask().boundingRect(), QRect(4, 4, 22, 22))
        self.assertEqual(chat.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(chat.button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertTrue(chat.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(chat.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertNotEqual(chat.windowTitle(), self.gears._windows["player_status_enabled"].windowTitle())
        own_handles = {int(window.winId()) for window in self.gears._windows.values()}
        self.assertTrue({call.args[0] for call in self.mocks[-1].call_args_list} <= own_handles)
        self.hover((110, 410))
        QTest.mouseClick(chat.button, Qt.MouseButton.RightButton)
        self.assertEqual(self.opened, [])
        QTest.mouseClick(chat.button, Qt.MouseButton.LeftButton)
        self.assertEqual(self.opened, ["crop_picker"])
        self.hover((1200, 900))
        self.assertFalse(chat.isVisible())

    def test_stats_routes_to_mini_stat_settings_and_does_not_intercept_chat_area(self):
        self.gears.start()
        self.hover((100, 150))
        stats = self.gears._windows["player_status_enabled"]
        self.assertTrue(stats.isVisible())
        self.assertFalse(self.gears._windows["crop_picker"].isVisible())
        self.hover((60, 110))
        stats.button.click()
        self.assertEqual(self.opened, ["player_status_enabled"])

    def test_copy_and_submit_are_separate_adjacent_shortcuts_with_disabled_state(self):
        self.gears.start()
        self.hover((100, 150))
        copy = self.gears._windows["copy_stats"]
        submit = self.gears._windows["submit_stats"]
        self.assertEqual(copy.button.accessibleName(), "Copy current stats")
        self.assertEqual(submit.button.accessibleName(), "Submit personal bests")
        self.assertTrue(copy.button.isVisible())
        self.assertTrue(submit.button.isVisible())
        self.assertEqual(submit.x() - copy.x(), 30)
        self.assertEqual(submit.y(), copy.y())
        self.hover((copy.x() + 10, copy.y() + 10))
        copy.button.click()
        self.assertEqual(self.opened, ["copy_stats"])
        self.hover((submit.x() + 10, submit.y() + 10))
        submit.button.click()
        self.assertEqual(self.opened, ["copy_stats", "submit_stats"])
        self.gears._action_enabled = lambda key: key != "submit_stats"
        self.gears.refresh()
        self.assertTrue(submit.button.isVisible())
        self.assertFalse(submit.button.isEnabled())
        submit.button.click()
        self.assertEqual(self.opened, ["copy_stats", "submit_stats"])

    def test_right_edge_stats_keep_both_shortcuts_on_screen_in_order(self):
        self.current = snapshot(stats_crop=(800, 100, 1000, 250))
        self.screens = (ScreenMapping((0, 0, 1000, 800), (0, 0, 1000, 800), 1.0),)
        self.gears.start()
        self.hover((900, 150))
        copy = self.gears._windows["copy_stats"]
        submit = self.gears._windows["submit_stats"]
        self.assertTrue(copy.isVisible() and submit.isVisible())
        self.assertLess(copy.x(), submit.x())
        self.assertLessEqual(submit.geometry().right(), 999)

    def test_changed_input_or_foreground_hides_and_rejects_queued_click(self):
        self.gears.start()
        self.hover((110, 410))
        chat = self.gears._windows["crop_picker"]
        self.enabled = False
        chat.button.click()
        self.assertFalse(chat.button.isVisible())
        self.assertEqual(self.opened, [])
        self.gears.refresh()
        self.assertFalse(chat.isVisible())
        self.enabled = True
        self.gears.refresh()
        self.assertTrue(chat.isVisible())
        self.foreground = False
        self.gears._poll_hover()
        chat.button.click()
        self.assertFalse(chat.isVisible())
        self.assertEqual(self.opened, [])

    def test_click_rechecks_capture_snapshot_and_does_not_open_stale_crop(self):
        self.gears.start()
        self.hover((110, 410))
        chat = self.gears._windows["crop_picker"]
        self.current = {}
        chat.button.click()
        self.assertEqual(self.opened, [])
        self.assertFalse(chat.isVisible())

    def test_current_window_movement_and_source_loss_replace_old_geometries(self):
        self.gears.start()
        self.hover((110, 410))
        self.bounds = {"frame": (300, 100, 1300, 900)}
        self.gears.refresh()
        self.assertFalse(self.gears._windows["crop_picker"].isVisible())
        self.hover((410, 510))
        self.assertEqual(self.gears._windows["crop_picker"].pos(), QPoint(400, 500))
        self.bounds = {}
        self.gears.refresh()
        self.assertFalse(any(window.isVisible() for window in self.gears._windows.values()))

    def test_mss_prefers_outside_crop_and_slow_gap_crossing_keeps_button_reachable(self):
        self.current = snapshot(capture_backend="mss")
        self.mocks[-1].return_value = False
        self.gears.start()
        self.hover((110, 410))
        chat = self.gears._windows["crop_picker"]
        self.assertTrue(chat.isVisible())
        self.assertEqual(chat.pos(), QPoint(70, 400))
        self.assertEqual(self.gears._button_rects["crop_picker"], (74, 404, 96, 426))
        self.hover((98, 410))  # The four-pixel gap between crop and button.
        self.assertTrue(chat.isVisible())
        self.hover((80, 410))
        self.assertTrue(chat.button.isVisible())
        chat.button.click()
        self.assertEqual(self.opened, ["crop_picker"])

    def test_mss_inside_fallback_requires_confirmed_own_window_capture_exclusion(self):
        self.current = snapshot(capture_backend="mss", chat_crop=(0, 0, 900, 750), stats_crop=None)
        self.mocks[-1].return_value = False
        self.cursor = (100, 100)
        self.gears.start()
        self.assertFalse(self.gears._windows["crop_picker"].isVisible())
        self.mocks[-1].return_value = True
        self.gears._windows["crop_picker"].excluded = None
        self.gears.refresh()
        self.assertTrue(self.gears._windows["crop_picker"].isVisible())

    def test_coincident_read_corners_prefer_more_specific_mini_panel(self):
        self.current = snapshot(stats_crop=(100, 400, 200, 500))
        self.cursor = (150, 450)
        self.gears.start()
        self.assertTrue(self.gears._windows["player_status_enabled"].isVisible())
        self.assertFalse(self.gears._windows["crop_picker"].isVisible())
        self.hover((500, 600))
        self.assertTrue(self.gears._windows["crop_picker"].isVisible())

    def test_negative_monitor_mapping_places_window_without_global_dpi_division(self):
        self.screens = (ScreenMapping((-2560, -300, 0, 1140), (-2560, -200, -853, 760), 1.5),)
        self.bounds = {"frame": (-2500, 0, -1500, 800)}
        self.cursor = (-2350, 450)
        self.gears.start()
        chat = self.gears._windows["crop_picker"]
        self.assertTrue(chat.isVisible())
        self.assertEqual(chat.pos(), self.screens[0].qt_point((-2400, 400)))
        self.assertEqual(self.gears._button_rects["crop_picker"], (-2394, 406, -2361, 439))

    def test_stop_and_parent_teardown_hide_windows_and_stop_both_timers(self):
        self.cursor = (110, 410)
        self.gears.start()
        self.assertTrue(self.gears._hover_timer.isActive())
        self.assertTrue(self.gears._geometry_timer.isActive())
        self.gears.stop()
        self.assertFalse(self.gears._hover_timer.isActive())
        self.assertFalse(self.gears._geometry_timer.isActive())
        self.assertFalse(any(window.isVisible() for window in self.gears._windows.values()))
        self.gears.start()
        windows = list(self.gears._windows.values())
        self.parent.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertTrue(all(not isValid(window) for window in windows))


if __name__ == "__main__":
    unittest.main()
