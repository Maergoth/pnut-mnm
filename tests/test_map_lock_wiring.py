"""The map shares the overlay lock and appearance through every application control."""
from __future__ import annotations

from dataclasses import replace
import inspect
import os
from pathlib import Path
import tempfile
from types import MethodType
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.main import App, _MissingEngine
from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.tray import TrayIcon
from mnmparse.config import Config


class _MapWindow(QWidget):
    visibility_changed = Signal(bool)

    def __init__(self, settings):
        super().__init__()
        self.repo = object()
        self.locked = None
        self.appearance = None

    def set_locked(self, locked):
        self.locked = locked

    def set_appearance(self, opacity, font_scale):
        self.appearance = (opacity, font_scale)

    def on_message(self, *_args):
        pass


class _DownloadController(QObject):
    started = Signal()
    progress = Signal(str)
    finished = Signal(str)
    ready = Signal(str)

    def start(self):
        raise AssertionError("No network work should run while testing the shared lock")


class _AppHarness:
    """Run real bootstrap/wiring without trying to create a second QApplication."""

    def __init__(self, settings, *, combat_available=True):
        self.settings = settings
        self.icon = QIcon()
        self._selftest = False
        self._shut_down = False
        self._make_engine = lambda cfg: _MissingEngine(cfg)
        self._make_overlay = lambda cfg: OverlayWindow(settings, cfg) if combat_available else None
        self._make_trigger_runner = lambda: None
        self.request_quit = Mock()

    def __getattr__(self, name):
        method = getattr(App, name)
        if isinstance(inspect.getattr_static(App, name), staticmethod):
            return method
        return MethodType(method, self)


class MapLockWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.host = None

    def tearDown(self):
        if self.host is not None:
            self.host._shut_down = True
            self.host.window.prepare_quit()
            for widget in (self.host.window, self.host.overlay, self.host.map_overlay):
                if widget is not None:
                    widget.close()
                    widget.deleteLater()
            self.host.tray.hide()
            self.host.tray.deleteLater()
        self.qt_app.processEvents()
        self.temp.cleanup()

    def bootstrap(self, cfg=None, *, combat_available=True):
        self.host = _AppHarness(self.settings, combat_available=combat_available)
        with patch("mnmparse.app.main.app_icon.ensure_icon_file"), \
             patch("mnmparse.app.main.QSystemTrayIcon.isSystemTrayAvailable", return_value=False), \
             patch("mnmparse.app.main.TrayIcon", side_effect=lambda icon, _parent: TrayIcon(icon)), \
             patch("mnmparse.app.map_overlay.MapOverlay", _MapWindow), \
             patch("mnmparse.app.map_downloads.MapDownloadController", side_effect=lambda *_: _DownloadController()), \
             patch("mnmparse.app.app_updates.AppUpdateController", side_effect=lambda *_: _DownloadController()):
            self.host.bootstrap(replace(cfg or Config(), start_capture_on_launch=False))
        return self.host

    def assert_locked(self, locked):
        host = self.host
        self.assertEqual(host.map_overlay.locked, locked)
        if host.overlay is not None:
            self.assertEqual(host.overlay.locked, locked)
        self.assertEqual(host.window._lock_switch.isChecked(), locked)
        self.assertEqual(host.window.page("settings").overlay_locked.isChecked(), locked)
        self.assertEqual(host.tray._overlay_locked, locked)
        self.assertEqual(self.settings.value("overlay/locked", type=bool), locked)

    def test_bootstrap_uses_persisted_effective_lock_and_appearance(self):
        self.settings.setValue("overlay/locked", True)
        self.settings.setValue("overlay/opacity", 0.54)
        self.settings.setValue("overlay/font_scale", 1.3)
        host = self.bootstrap(Config(overlay_locked=False, overlay_opacity=0.8))
        self.assert_locked(True)
        self.assertEqual(host.map_overlay.appearance, (0.54, 1.3))

    def test_main_tray_settings_and_combat_header_share_the_lock(self):
        host = self.bootstrap()
        host.window._lock_switch.setChecked(False)
        self.assert_locked(False)
        host.tray._lock_action.trigger()
        self.assert_locked(True)
        host.window.page("settings").overlay_locked.setChecked(False)
        self.assert_locked(False)
        host.overlay.set_locked(True)
        self.assert_locked(True)
        host.on_config_changed(replace(host.cfg, overlay_locked=False))
        self.assert_locked(False)

    def test_settings_and_combat_appearance_changes_reach_the_map(self):
        host = self.bootstrap()
        page = host.window.page("settings")
        page.overlay_opacity.set_value(47, emit=True)
        page.overlay_font_scale.set_value(125, emit=True)
        self.assertEqual(host.map_overlay.appearance, (0.47, 1.25))
        host.overlay.set_opacity(0.64)
        host.overlay.set_font_scale(1.4)
        self.assertEqual(host.map_overlay.appearance, (0.64, 1.4))
        host.on_config_changed(replace(host.cfg, overlay_opacity=0.72, overlay_font_scale=1.15))
        self.assertEqual(host.map_overlay.appearance, (0.72, 1.15))

    def test_map_controls_work_when_combat_overlay_is_unavailable(self):
        self.settings.setValue("overlay/locked", False)
        self.settings.setValue("overlay/opacity", 0.6)
        host = self.bootstrap(combat_available=False)
        self.assert_locked(False)
        self.assertEqual(host.map_overlay.appearance, (0.6, 1.0))
        self.assertTrue(host.window._lock_switch.isEnabled())
        self.assertTrue(host.tray._lock_action.isEnabled())
        self.assertFalse(host.window._overlay_switch.isEnabled())
        self.assertFalse(host.tray._overlay_action.isEnabled())
        host.window._lock_switch.setChecked(True)
        self.assert_locked(True)
        host.tray._lock_action.trigger()
        self.assert_locked(False)
        page = host.window.page("settings")
        page.overlay_locked.setChecked(True)
        self.assert_locked(True)
        page.overlay_opacity.set_value(35, emit=True)
        page.overlay_font_scale.set_value(150, emit=True)
        self.assertEqual(host.map_overlay.appearance, (0.35, 1.5))
        host.on_config_changed(replace(host.cfg, overlay_locked=False, overlay_opacity=0.72, overlay_font_scale=1.1))
        self.assert_locked(False)
        self.assertEqual(host.map_overlay.appearance, (0.72, 1.1))


if __name__ == "__main__":
    unittest.main()
