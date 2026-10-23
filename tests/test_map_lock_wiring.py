"""Map and Revenge popout share overlay settings through application controls."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import inspect
import os
from pathlib import Path
import tempfile
import threading
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, QSettings, Signal, Slot
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
        self.source = "atlas"

    def set_source(self, source):
        self.source = source

    def set_locked(self, locked):
        self.locked = locked

    def set_appearance(self, opacity, font_scale):
        self.appearance = (opacity, font_scale)

    def on_message(self, *_args):
        pass

    def set_notable_npcs(self, *_args, **_kwargs):
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


class _HealthRelay(QObject):
    """A GUI QObject receiver gives worker signals real AutoConnection delivery."""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.values = []
        self.threads = []

    @Slot(bool)
    def deliver(self, active):
        self.values.append(active)
        self.threads.append(threading.get_ident())
        self.callback(active)


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
            read_area_gears = self.host.__dict__.get("_read_area_gears")
            if read_area_gears is not None:
                read_area_gears.shutdown()
            leaderboard_client = self.host.__dict__.get("_leaderboard_client")
            if leaderboard_client is not None:
                leaderboard_client.shutdown()
                leaderboard_client.deleteLater()
            for name in ("notable_kills", "npc_details"):
                controller = self.host.__dict__.get(name)
                if controller is not None:
                    controller.shutdown()
                    controller.deleteLater()
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
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.host = None
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

    def test_non_qobject_bootstrap_keeps_leaderboard_client_without_invalid_parent(self):
        host = self.bootstrap()
        self.assertIsNotNone(host._leaderboard_client)
        self.assertIsNone(host._leaderboard_client.parent())
        self.assertIs(host.window.page("leaderboard").client, host._leaderboard_client)
        self.assertFalse(host._leaderboard_client.busy)

    def test_download_ready_is_wired_to_global_countdown_and_tray_action(self):
        host = self.bootstrap()
        controller = host.app_updates
        controller.ready_package = Path(self.temp.name) / "verified-package"
        controller.ready_version = "9.8.7"
        controller.automatic_restart_allowed = Mock(return_value=True)
        controller.prepare_restart = Mock(return_value=True)
        controller.finished.emit("PNUT 9.8.7 downloaded.")
        controller.ready.emit("9.8.7")
        notice = host._update_notice
        try:
            self.assertTrue(notice.isVisible())
            self.assertIn("Restarting in 10 seconds", host.window._status_bar.currentMessage())
            self.assertTrue(host.window.page("settings")._app_update_ready)
            self.assertTrue(host.tray._update_action.isVisible())
            controller.ready.emit("9.8.7")
            self.assertIs(host._update_notice, notice)
            notice.later_button.click()
            self.assertFalse(notice.isVisible())
            controller.prepare_restart.assert_not_called()
            host.request_quit.assert_not_called()
        finally:
            notice.stop()
            notice.deleteLater()

    def test_overlay_gears_open_each_panels_settings_in_the_main_app(self):
        from PySide6.QtCore import QPoint

        host = self.bootstrap(Config(player_name="SampleHero", casual_mode=False, revenge_enabled=True))
        host.overlay.timer_panel.set_always_show(True)
        self.settings.setValue("attack_bar/weapon_delay", 36)
        host.overlay.attack_bar.reload_preferences()
        host.overlay.npc_panel.show_npc("Sample Keeper")
        host.revenge.save_attacker("FictionalRival")
        host.show_revenge_popout()
        self.assertEqual(len(host._overlay_settings_gears), 6)
        destinations = (
            (host.overlay, "overlay_enabled"),
            (host.overlay.timer_panel, "triggers"),
            (host.overlay.attack_bar, "weapon_delay"),
            (host.overlay.npc_panel, "npc_enabled"),
            (host.map_overlay, "map_download_button"),
            (host.revenge_popout, "revenge_enabled"),
        )
        with patch.object(host, "show_window") as show, \
                patch.object(host.window, "_open_settings_option") as settings, \
                patch.object(host.window, "show_page") as page:
            for panel, destination in destinations:
                with self.subTest(destination=destination):
                    show.reset_mock()
                    settings.reset_mock()
                    page.reset_mock()
                    panel.show()
                    gear = panel._settings_gear
                    position = panel.mapToGlobal(QPoint(10, 10))
                    with patch("mnmparse.app.overlay_settings.QCursor.pos", return_value=position):
                        gear.sync()
                        self.assertTrue(gear.isVisible())
                        gear.click()
                    show.assert_called_once()
                    if destination == "triggers":
                        page.assert_called_once_with("triggers")
                        settings.assert_not_called()
                    else:
                        settings.assert_called_once_with(destination)
                        page.assert_not_called()

    def test_combat_gear_hides_immediately_when_click_through_is_enabled(self):
        from PySide6.QtCore import QPoint

        host = self.bootstrap()
        gear = host.overlay._settings_gear
        with patch("mnmparse.app.overlay_settings.QCursor.pos", return_value=host.overlay.mapToGlobal(QPoint(10, 10))):
            gear.sync()
            self.assertTrue(gear.isVisible())
            host.overlay.set_click_through(True)
            self.assertFalse(gear.isVisible())

    def assert_appearance(self, opacity, font_scale):
        self.assertEqual(self.host.map_overlay.appearance, (opacity, font_scale))
        self.assertEqual((self.host.revenge_popout.opacity, self.host.revenge_popout.font_scale),
                         (opacity, font_scale))

    @contextmanager
    def health_warning(self, **overrides):
        options = dict(player_status_enabled=True, low_health_enabled=True,
                       low_health_flash=True, low_health_threshold=30, overlay_enabled=True)
        options.update(overrides)
        host = self.bootstrap(Config(**options))
        host.engine.state = "running"
        geometry = QRect(10, 20, 1280, 720)
        screen = SimpleNamespace(geometry=lambda: QRect(geometry))
        flash = Mock()
        with patch("mnmparse.app.low_health.LowHealthFlash", return_value=flash) as factory, \
             patch("mnmparse.capture.find_game_window_info", return_value=SimpleNamespace(rect=(20, 30, 420, 330))), \
             patch("mnmparse.app.main.QGuiApplication.screenAt", return_value=screen), \
             patch.object(host, "triggers", Mock()) as triggers:
            yield host, flash, factory, triggers, geometry

    @contextmanager
    def real_health_engine(self, host):
        from mnmparse.app.engine import Engine

        engine = Engine(replace(host.cfg, log_dir=self.temp.name))
        engine._state = "running"
        relay = _HealthRelay(host._on_low_health_changed)
        engine.low_health_changed.connect(relay.deliver)
        try:
            with patch.object(host, "engine", engine):
                yield engine, relay
        finally:
            engine.low_health_changed.disconnect(relay.deliver)
            relay.deleteLater()
            engine.deleteLater()

    def queue_health_state_from_worker(self, engine, active):
        errors = []

        def emit():
            try:
                with engine._lock:
                    engine._set_low_health_active(active)
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=emit, name="health-state-test", daemon=True)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "Health-state worker did not finish")
        self.assertEqual(errors, [])

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

    def test_default_map_choice_updates_both_maps_through_settings(self):
        host = self.bootstrap()
        options = host.window.page("settings")
        zones = host.window.page("zones")
        self.assertEqual(options.map_default_source.currentData(), "atlas")
        options.map_default_source.setCurrentIndex(options.map_default_source.findData("images"))
        self.assertEqual(host.map_overlay.source, "images")
        self.assertEqual(zones.map.source, "images")
        self.assertEqual(self.settings.value("map/default_source"), "images")
        options.map_default_source.setCurrentIndex(options.map_default_source.findData("atlas"))
        self.assertEqual(host.map_overlay.source, "atlas")
        self.assertEqual(zones.map.source, "atlas")

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

    def test_confirmed_low_health_signal_starts_and_stops_visual_without_audio(self):
        with self.health_warning(reduced_motion=True) as (host, flash, factory, triggers, geometry):
            host.engine.low_health_changed.emit(True)
            factory.assert_called_once_with()
            flash.flash.assert_called_with(geometry, reduced_motion=True)
            triggers.fire.assert_not_called()
            host.engine.low_health_changed.emit(False)
            flash.stop.assert_called()
            triggers.fire.assert_not_called()
            host.engine.low_health_changed.emit(True)
            self.assertEqual(factory.call_count, 1)
            self.assertGreaterEqual(flash.flash.call_count, 2)

    def test_audio_low_health_signal_remains_separate_from_persistent_visual_state(self):
        with self.health_warning() as (host, flash, factory, triggers, geometry):
            host.engine.low_health.emit(10.0)
            triggers.fire.assert_called_once()
            factory.assert_not_called()
            host.engine.low_health_changed.emit(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            self.assertEqual(triggers.fire.call_count, 1)

    def test_confirmed_low_health_survives_global_hide_show_and_hidden_recovery(self):
        with self.health_warning() as (host, flash, _factory, triggers, geometry):
            host.engine.low_health_changed.emit(True)
            host.set_overlay_visible(False)
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host.engine.low_health_changed.emit(True)
            flash.flash.assert_not_called()
            host.set_overlay_visible(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)

            host.set_overlay_visible(False)
            flash.flash.reset_mock()
            host.engine.low_health_changed.emit(False)
            host.set_overlay_visible(True)
            flash.flash.assert_not_called()
            triggers.fire.assert_not_called()

    def test_flash_preference_off_then_on_retains_the_confirmed_low_health_episode(self):
        with self.health_warning() as (host, flash, _factory, triggers, geometry):
            host.engine.low_health_changed.emit(True)
            host.on_config_changed(replace(host.cfg, low_health_flash=False))
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host._sync_low_health_flash()
            flash.flash.assert_not_called()
            host.on_config_changed(replace(host.cfg, low_health_flash=True))
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            triggers.fire.assert_not_called()

    def test_changed_low_health_threshold_waits_for_a_new_confirmation(self):
        with self.health_warning() as (host, flash, _factory, triggers, geometry):
            host.engine.low_health_changed.emit(True)
            host.on_config_changed(replace(host.cfg, low_health_threshold=40))
            flash.stop.assert_called()
            flash.flash.reset_mock()
            host._sync_low_health_flash()
            host.set_overlay_visible(False)
            host.set_overlay_visible(True)
            flash.flash.assert_not_called()
            host.engine.low_health_changed.emit(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            triggers.fire.assert_not_called()

    def test_paused_missing_window_and_stopped_clear_visual_until_fresh_confirmation(self):
        with self.health_warning() as (host, flash, _factory, triggers, geometry):
            for state in ("paused", "no_window", "stopped"):
                with self.subTest(state=state):
                    host.engine.state = "running"
                    host.engine.low_health_changed.emit(True)
                    host.engine.state = state
                    host.engine.state_changed.emit(state)
                    flash.stop.assert_called()
                    flash.flash.reset_mock()
                    host.engine.state = "running"
                    host.engine.state_changed.emit("running")
                    flash.flash.assert_not_called()
                    host.engine.low_health_changed.emit(True)
                    flash.flash.assert_called_with(geometry, reduced_motion=False)
            triggers.fire.assert_not_called()

    def test_low_health_signal_queued_during_pause_cannot_restart_visual_on_resume(self):
        with self.health_warning() as (host, flash, factory, triggers, geometry):
            host.engine.state = "paused"
            host.engine.state_changed.emit("paused")
            host.engine.low_health_changed.emit(True)
            factory.assert_not_called()
            host.engine.state = "running"
            host.engine.state_changed.emit("running")
            factory.assert_not_called()
            host.engine.low_health_changed.emit(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            triggers.fire.assert_not_called()

    def test_worker_queued_low_health_true_cannot_revive_flash_after_gui_threshold_reset(self):
        with self.health_warning() as (host, _flash, factory, triggers, _geometry), \
             self.real_health_engine(host) as (engine, relay):
            gui_thread = threading.get_ident()
            self.queue_health_state_from_worker(engine, True)
            self.assertEqual(relay.values, [], "Worker signal must stay queued until GUI events run")
            self.assertTrue(engine.low_health_active)

            changed = replace(engine._cfg, low_health_threshold=40)
            engine.update_config(changed)
            host.on_config_changed(changed)
            self.assertFalse(engine.low_health_active)
            self.assertEqual(relay.values, [False])
            self.qt_app.processEvents()

            self.assertEqual(relay.values, [False, True])
            self.assertEqual(relay.threads, [gui_thread, gui_thread])
            self.assertFalse(host._low_health_active)
            factory.assert_not_called()
            triggers.fire.assert_not_called()

    def test_worker_queued_recovery_cannot_clear_a_newer_confirmed_low_health_episode(self):
        with self.health_warning() as (host, flash, _factory, triggers, geometry), \
             self.real_health_engine(host) as (engine, relay):
            gui_thread = threading.get_ident()
            with engine._lock:
                engine._set_low_health_active(True)
            flash.flash.assert_called_with(geometry, reduced_motion=False)
            self.queue_health_state_from_worker(engine, False)
            self.assertEqual(relay.values, [True])

            with engine._lock:
                engine._set_low_health_active(True)
            self.assertTrue(engine.low_health_active)
            self.assertEqual(relay.values, [True, True])
            flash.stop.reset_mock()
            self.qt_app.processEvents()

            self.assertEqual(relay.values, [True, True, False])
            self.assertEqual(relay.threads, [gui_thread, gui_thread, gui_thread])
            self.assertTrue(host._low_health_active)
            flash.stop.assert_not_called()
            triggers.fire.assert_not_called()

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
