"""Map and Revenge popout share overlay settings through application controls."""
from __future__ import annotations

from dataclasses import replace
import inspect
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QRect, QSettings, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.main import App, _MissingEngine
from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.tray import TrayIcon
from mnmparse.config import Config


class _MapWindow(QWidget):
    visibility_changed = Signal(bool)
    npc_selected = Signal(str)
    npc_zone_requested = Signal(str)

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

    def set_notable_npcs(self, *_args):
        pass

    def showEvent(self, event):
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def shutdown(self):
        self.close()


class _DownloadController(QObject):
    started = Signal()
    progress = Signal(str)
    finished = Signal(str)
    ready = Signal(str)

    def start(self):
        raise AssertionError("No network work should run while testing the shared lock")

    def shutdown(self):
        pass


class _AppHarness:
    """Run real bootstrap/wiring without trying to create a second QApplication."""

    def __init__(self, settings, *, combat_available=True, trigger_runner=None):
        self.settings = settings
        self.icon = QIcon()
        self._selftest = False
        self._shut_down = False
        self._make_engine = lambda cfg: _MissingEngine(cfg)
        self._make_overlay = lambda cfg: OverlayWindow(settings, cfg) if combat_available else None
        self._make_trigger_runner = lambda: trigger_runner
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
        self.settings.setValue("main/overlay_visible", True)
        self.host = None

    def tearDown(self):
        if self.host is not None:
            self.host._shut_down = True
            self.host.window.prepare_quit()
            for widget in (self.host.window, self.host.overlay, self.host.map_overlay, self.host.revenge_popout):
                if widget is not None:
                    widget.close()
                    widget.deleteLater()
            self.host.tray.hide()
            self.host.tray.deleteLater()
            for name in ("_timer", "_save_timer", "_activity_refresh", "_display_timer"):
                getattr(self.host.revenge, name).stop()
            self.host.revenge.deleteLater()
            if self.host.triggers is not None:
                self.host.triggers._clock.stop()
                self.host.triggers.audio.stop()
                self.host.triggers.deleteLater()
        self.qt_app.processEvents()
        self.temp.cleanup()

    def bootstrap(self, cfg=None, *, combat_available=True, trigger_runner=None, map_factory=_MapWindow):
        self.host = _AppHarness(self.settings, combat_available=combat_available, trigger_runner=trigger_runner)
        with patch("mnmparse.app.main.app_icon.ensure_icon_file"), \
             patch("mnmparse.app.main.QSystemTrayIcon.isSystemTrayAvailable", return_value=False), \
             patch("mnmparse.app.main.TrayIcon", side_effect=lambda icon, _parent: TrayIcon(icon)), \
             patch("mnmparse.app.map_overlay.MapOverlay", map_factory), \
             patch("mnmparse.app.map_downloads.MapDownloadController", side_effect=lambda *_: _DownloadController()), \
             patch("mnmparse.app.app_updates.AppUpdateController", side_effect=lambda *_: _DownloadController()):
            self.host.bootstrap(replace(cfg or Config(), start_capture_on_launch=False, setup_complete=True),
                                config_path=Path(self.temp.name) / "config.json")
        return self.host

    def assert_locked(self, locked):
        host = self.host
        self.assertEqual(host.map_overlay.locked, locked)
        self.assertEqual(host.revenge_popout.locked, locked)
        if host.overlay is not None:
            self.assertEqual(host.overlay.locked, locked)
        self.assertEqual(host.window._lock_switch.isChecked(), locked)
        self.assertEqual(host.window.page("settings").overlay_locked.isChecked(), locked)
        self.assertEqual(host.tray._overlay_locked, locked)
        self.assertEqual(self.settings.value("overlay/locked", type=bool), locked)

    def assert_appearance(self, opacity, font_scale):
        self.assertEqual(self.host.map_overlay.appearance, (opacity, font_scale))
        self.assertEqual((self.host.revenge_popout.opacity, self.host.revenge_popout.font_scale),
                         (opacity, font_scale))

    def test_fresh_install_opens_map_and_quit_keeps_open_preference(self):
        host = self.bootstrap()
        self.assert_locked(False)
        self.assertTrue(host.map_overlay.isVisible())
        self.assertTrue(host.window._map_button.isChecked())
        host.shutdown()
        self.assertFalse(host.map_overlay.isVisible())
        reopened = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        self.assertTrue(reopened.value("map/visible", False, type=bool))

    def test_saved_closed_map_stays_closed_and_can_be_reopened(self):
        self.settings.setValue("map/visible", False)
        host = self.bootstrap()
        self.assertFalse(host.map_overlay.isVisible())
        self.assertFalse(host.window._map_button.isChecked())
        host.window._map_button.click()
        self.assertTrue(host.map_overlay.isVisible())
        self.assertTrue(self.settings.value("map/visible", False, type=bool))

    def test_closing_map_saves_closed_preference_through_app_quit(self):
        host = self.bootstrap()
        host.map_overlay.close()
        self.assertFalse(host.window._map_button.isChecked())
        host.shutdown()
        reopened = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        self.assertFalse(reopened.value("map/visible", True, type=bool))

    def test_bootstrap_and_shutdown_preserve_open_revenge_popout_state(self):
        saved = QRect(80, 60, 480, 420)
        self.settings.setValue("revenge/visible", True)
        self.settings.setValue("revenge/geometry", saved)
        self.settings.setValue("overlay/locked", False)
        self.settings.setValue("overlay/opacity", 0.42)
        host = self.bootstrap(Config(revenge_enabled=True))
        self.qt_app.processEvents()
        self.assertTrue(host.revenge_popout.isVisible())
        self.assertEqual(host.revenge_popout.geometry(), saved)
        self.assertFalse(host.revenge_popout.locked)
        self.assertAlmostEqual(host.revenge_popout.opacity, 0.42)
        host.shutdown()
        self.assertTrue(host.revenge_popout.isHidden())
        reopened = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        self.assertTrue(reopened.value("revenge/visible", False, type=bool))
        self.assertEqual(reopened.value("revenge/geometry", QRect()), saved)

    def test_hide_overlay_hides_and_restores_detached_windows_without_changing_preferences(self):
        self.settings.setValue("revenge/visible", True)
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True))
        host.overlay.timer_panel.set_always_show(True)
        host.overlay.attack_bar.enabled = True
        host.overlay.attack_bar.sync()
        host.overlay.timer_panel._docked = False
        host.overlay.attack_bar._docked = False
        self.assertTrue(host.overlay.timer_panel.isVisible())
        self.assertTrue(host.overlay.attack_bar.isVisible())
        self.assertTrue(host.map_overlay.isVisible())
        self.assertTrue(host.revenge_popout.isVisible())

        host.tray._overlay_action.trigger()
        for widget in (host.overlay, host.overlay.timer_panel, host.overlay.attack_bar,
                       host.map_overlay, host.revenge_popout):
            self.assertFalse(widget.isVisible(), widget.objectName())
        self.assertTrue(self.settings.value("map/visible", False, type=bool))
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        self.assertTrue(host.window._map_button.isChecked())

        host.tray._overlay_action.trigger()
        for widget in (host.overlay, host.overlay.timer_panel, host.overlay.attack_bar,
                       host.map_overlay, host.revenge_popout):
            self.assertTrue(widget.isVisible(), widget.objectName())

    def test_overlay_hidden_at_startup_keeps_saved_map_and_revenge_choices(self):
        self.settings.setValue("main/overlay_visible", False)
        self.settings.setValue("revenge/visible", True)
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True))
        self.assertFalse(host.map_overlay.isVisible())
        self.assertFalse(host.revenge_popout.isVisible())
        self.assertTrue(self.settings.value("map/visible", False, type=bool))
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        host.shutdown()
        self.assertFalse(self.settings.value("main/overlay_visible", True, type=bool))
        self.assertTrue(self.settings.value("map/visible", False, type=bool))
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))

    def test_global_show_preserves_individually_closed_map_and_revenge_list(self):
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True))
        host.show_revenge_popout()
        host.map_overlay.close()
        host.revenge_popout.hide()
        host.set_overlay_visible(False)
        host.set_overlay_visible(True)
        self.assertFalse(host.map_overlay.isVisible())
        self.assertFalse(host.revenge_popout.isVisible())
        self.assertFalse(self.settings.value("map/visible", True, type=bool))
        self.assertFalse(self.settings.value("revenge/visible", True, type=bool))

    def test_windows_requested_or_refreshed_while_hidden_wait_for_global_show(self):
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True))
        host.set_map_visible(False)
        host.set_overlay_visible(False)
        host.set_map_visible(True)
        host.show_revenge_popout()
        host.revenge.save_attacker("Bandit")
        host.revenge.prompts_changed.emit()
        host.revenge.set_config(replace(host.cfg, revenge_enabled=False))
        host.revenge.set_config(host.cfg)
        # Even a direct show request from an independent map control is gated.
        host.map_overlay.show()
        self.assertFalse(host.map_overlay.isVisible())
        self.assertFalse(host.revenge_popout.isVisible())
        host.set_overlay_visible(True)
        self.assertTrue(host.map_overlay.isVisible())
        self.assertTrue(host.revenge_popout.isVisible())

    def test_fullscreen_map_survives_global_hide_show_with_its_state_and_preference(self):
        from mnmparse.app.map_overlay import MapOverlay
        from mnmparse.maps import MapRepository

        repo = MapRepository(Path(self.temp.name) / "maps")
        host = self.bootstrap(map_factory=lambda settings: MapOverlay(settings, repo))
        host.map_overlay.toggle_fullscreen()
        self.qt_app.processEvents()
        self.assertTrue(host.map_overlay.isFullScreen())
        host.set_overlay_visible(False)
        self.assertFalse(host.map_overlay.isVisible())
        self.assertTrue(self.settings.value("map/fullscreen", False, type=bool))
        self.assertTrue(self.settings.value("map/visible", False, type=bool))
        host.set_overlay_visible(True)
        self.qt_app.processEvents()
        self.assertTrue(host.map_overlay.isVisible())
        self.assertTrue(host.map_overlay.isFullScreen())

    def test_hidden_overlay_stays_hidden_when_timers_revenge_alerts_and_prompts_arrive(self):
        from mnmparse.app.triggers_runtime import TriggerRunner
        from mnmparse.parser import parse_line
        from mnmparse.triggers import TriggerStore

        runner = TriggerRunner(TriggerStore(Path(self.temp.name) / "triggers.json"))
        runner.audio.run = Mock()
        runner.audio.voices = Mock(return_value=[])
        runner.audio.devices = Mock(return_value=[])
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True), trigger_runner=runner)
        host.set_overlay_visible(False)
        runner.start_one_time_timer("Root", 30)
        host._on_revenge_alert("Bandit")
        source = "Caster's Lesser Iceblast hits YOU for 177 points of Cold Damage!"
        host.revenge.on_message(SimpleNamespace(text=source), parse_line(source, 1, "Hero"))
        panel = host.overlay.timer_panel
        self.assertTrue(panel._popups)
        self.assertIn("caster", panel._revenge_buttons)
        self.assertFalse(panel.isVisible())
        self.assertFalse(host.overlay.attack_bar.isVisible())
        timer_start = runner.board.timers[0].start
        popup_start = panel._popups[-1].created_at
        host.set_overlay_visible(True)
        self.assertTrue(panel.isVisible())
        self.assertEqual(runner.board.timers[0].start, timer_start)
        self.assertEqual(panel._popups[-1].created_at, popup_start)

    def test_combat_overlay_header_hide_also_hides_independent_windows_and_flash(self):
        host = self.bootstrap(Config(player_name="Hero", revenge_enabled=True))
        host.show_revenge_popout()
        host._low_health_flash = Mock()
        host.overlay.hide()
        self.assertFalse(host.map_overlay.isVisible())
        self.assertFalse(host.revenge_popout.isVisible())
        host._low_health_flash.stop.assert_called_once()
        self.assertFalse(self.settings.value("main/overlay_visible", True, type=bool))

    def test_low_health_while_overlay_hidden_keeps_audio_but_does_not_create_flash(self):
        host = self.bootstrap(Config(player_status_enabled=True, low_health_enabled=True,
                                     low_health_flash=True, low_health_threshold=30))
        host.set_overlay_visible(False)
        host.triggers = SimpleNamespace(fire=Mock())
        try:
            with patch("mnmparse.app.low_health.LowHealthFlash") as flash:
                host._on_low_health(10)
            host.triggers.fire.assert_called_once()
            flash.assert_not_called()
        finally:
            host.triggers = None

    def test_bootstrap_uses_persisted_effective_lock_and_appearance(self):
        self.settings.setValue("overlay/locked", True)
        self.settings.setValue("overlay/opacity", 0.54)
        self.settings.setValue("overlay/font_scale", 1.3)
        host = self.bootstrap(Config(overlay_locked=False, overlay_opacity=0.8))
        self.assert_locked(True)
        self.assert_appearance(0.54, 1.3)

    def test_main_tray_settings_and_combat_header_share_the_lock(self):
        host = self.bootstrap()
        host.window._lock_switch.setChecked(True)
        self.assert_locked(True)
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
        self.assert_appearance(0.47, 1.25)
        host.overlay.set_opacity(0.64)
        host.overlay.set_font_scale(1.4)
        self.assert_appearance(0.64, 1.4)
        host.on_config_changed(replace(host.cfg, overlay_opacity=0.72, overlay_font_scale=1.15))
        self.assert_appearance(0.72, 1.15)

    def test_map_controls_work_when_combat_overlay_is_unavailable(self):
        self.settings.setValue("overlay/locked", False)
        self.settings.setValue("overlay/opacity", 0.6)
        host = self.bootstrap(combat_available=False)
        self.assert_locked(False)
        self.assert_appearance(0.6, 1.0)
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
        self.assert_appearance(0.35, 1.5)
        host.on_config_changed(replace(host.cfg, overlay_locked=False, overlay_opacity=0.72, overlay_font_scale=1.1))
        self.assert_locked(False)
        self.assert_appearance(0.72, 1.1)

    def test_revenge_sighting_uses_wired_trigger_notifications_and_settings_preview(self):
        from mnmparse.app.triggers_runtime import TriggerRunner
        from mnmparse.grammar import Event
        from mnmparse.tracker import Message
        from mnmparse.triggers import TriggerStore

        store = TriggerStore(Path(self.temp.name) / "triggers.json")
        store.save()
        saved = store.path.read_bytes()
        runner = TriggerRunner(store)
        runner.audio.run = Mock()
        runner.audio.voices = Mock(return_value=[])
        runner.audio.devices = Mock(return_value=[])
        cfg = Config(player_name="Hero", revenge_enabled=True, revenge_alert_volume=17,
                     revenge_alert_duration=6, revenge_alert_flash=True)
        host = self.bootstrap(cfg, trigger_runner=runner)
        matches = []
        runner.fired.connect(matches.append)
        self.assertTrue(host.revenge.save_attacker("{line}"))
        source = "{line} hits Hero for 99 damage."
        with patch("mnmparse.app.morality.reduced_motion", return_value=False):
            host.engine.message.emit(Message(source, 1, 1),
                                     Event(1, "melee_hit", source, actor="{line}", target="Hero", amount=99))

        self.assertEqual(len(matches), 1)
        popup = host.overlay._timer_panel._popups[-1]
        self.assertEqual((popup.label, popup.seconds, popup.flash), ("Revenge: {line} spotted", 6, True))
        recent = host.window.page("triggers").recent.item(0)
        self.assertIn("Revenge sighting", recent.text())
        self.assertEqual(recent.toolTip(), "{line}")
        self.assertEqual(runner.board.timers, [])
        self.assertEqual(store.triggers, [])
        self.assertEqual(store.path.read_bytes(), saved)
        runner.audio.run.assert_called_once_with("sound", sound="Dink", file="", speech="{line}",
                                                 volume=17, scope="")

        page = host.window.page("settings")
        page.revenge_alert_volume.set_value(27, emit=True)
        page.revenge_alert_preview.click()
        runner.audio.run.assert_called_with("sound", sound="Dink", volume=27)
        self.assertEqual(store.path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
