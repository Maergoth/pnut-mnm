"""Only unintended native main-window hiding should be repaired."""
from __future__ import annotations

import ctypes
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app.main import _ensure_native_visible


class WindowVisibilityTests(unittest.TestCase):
    def window(self, **overrides):
        values = dict(isVisible=True, isMinimized=False, isMaximized=False,
                      windowHandle=object(), winId=(1 << 40) + 123)
        values.update(overrides)
        window = Mock()
        for method, value in values.items():
            getattr(window, method).return_value = value
        return window

    def test_repairs_native_hidden_window_without_losing_maximization(self):
        for maximized, command in ((False, 8), (True, 3)):
            with self.subTest(maximized=maximized):
                window = self.window(isMaximized=maximized)
                native = Mock()
                native.IsWindowVisible.return_value = False
                with patch("mnmparse.app.main.QGuiApplication.platformName", return_value="windows"), \
                     patch.object(ctypes, "WinDLL", return_value=native, create=True):
                    _ensure_native_visible(window)
                native.IsWindowVisible.assert_called_once_with(window.winId())
                native.ShowWindow.assert_called_once_with(window.winId(), command)
                # HWND is pointer-sized, including on 64-bit Windows.
                self.assertEqual(ctypes.sizeof(native.ShowWindow.argtypes[0]), ctypes.sizeof(ctypes.c_void_p))

    def test_visible_window_is_not_activated_or_reshown(self):
        native = Mock()
        native.IsWindowVisible.return_value = True
        with patch("mnmparse.app.main.QGuiApplication.platformName", return_value="windows"), \
             patch.object(ctypes, "WinDLL", return_value=native, create=True):
            _ensure_native_visible(self.window())
        native.ShowWindow.assert_not_called()

    def test_intentional_hide_minimize_and_missing_surface_are_respected(self):
        for state in ({"isVisible": False}, {"isMinimized": True}, {"windowHandle": None}):
            with self.subTest(state=state), \
                 patch("mnmparse.app.main.QGuiApplication.platformName", return_value="windows"), \
                 patch.object(ctypes, "WinDLL", create=True) as load:
                _ensure_native_visible(self.window(**state))
                load.assert_not_called()

    def test_other_platforms_do_not_call_windows_apis(self):
        for platform in ("offscreen", "xcb", "cocoa"):
            with self.subTest(platform=platform), \
                 patch("mnmparse.app.main.QGuiApplication.platformName", return_value=platform), \
                 patch.object(ctypes, "WinDLL", create=True) as load:
                _ensure_native_visible(self.window())
                load.assert_not_called()

    def test_native_failure_does_not_break_window_show(self):
        with patch("mnmparse.app.main.QGuiApplication.platformName", return_value="windows"), \
             patch.object(ctypes, "WinDLL", side_effect=OSError("unavailable"), create=True):
            _ensure_native_visible(self.window())


if __name__ == "__main__":
    unittest.main()
