"""NPC settings persist and drive the real app's detachable overlay immediately."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from mnmparse.app.tray import TrayIcon
from mnmparse.config import Config, load_config, save_config
from mnmparse.parser import parse_line
from mnmparse.tracker import Message
from tests.test_map_lock_wiring import _AppHarness, _DownloadController, _MapWindow


NPC_DEFAULTS = {"enabled": True, "level": True, "common_drops": False,
                "unique_drops": True, "quests": False, "reputation": False}


class NpcConfigTests(unittest.TestCase):
    def test_new_and_legacy_configs_default_to_level_and_unique_drops(self):
        defaults = Config()
        self.assertEqual({key: getattr(defaults, f"npc_{key}") for key in NPC_DEFAULTS}, NPC_DEFAULTS)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("{}", encoding="utf-8")
            loaded = load_config(str(path))
            self.assertEqual({key: getattr(loaded, f"npc_{key}") for key in NPC_DEFAULTS}, NPC_DEFAULTS)

    def test_all_npc_preferences_roundtrip_as_booleans(self):
        options = {f"npc_{key}": not value for key, value in NPC_DEFAULTS.items()}
        cfg = replace(Config(), **options)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            save_config(cfg, str(path))
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({key: raw[key] for key in options}, options)
            self.assertTrue(all(type(raw[key]) is bool for key in options))
            self.assertEqual(load_config(str(path)), cfg)


class NpcSettingsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.temp.name) / "settings.ini")
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        self.settings.setValue("main/overlay_visible", True)
        self.hosts = []
        self.patches = ExitStack()
        # Exercise the real controller/wiring while forbidding asynchronous wiki work.
        self.request = self.patches.enter_context(patch("mnmparse.app.npc_details.NpcDetailsController._request"))

    def tearDown(self):
        for host in self.hosts:
            self.dispose(host)
        self.patches.close()
        self.app.processEvents()
        self.temp.cleanup()

    def bootstrap(self, cfg=None, *, combat_available=True):
        host = _AppHarness(self.settings, combat_available=combat_available)
        self.hosts.append(host)
        with patch("mnmparse.app.main.app_icon.ensure_icon_file"), \
             patch("mnmparse.app.main.QSystemTrayIcon.isSystemTrayAvailable", return_value=False), \
             patch("mnmparse.app.main.TrayIcon", side_effect=lambda icon, _parent: TrayIcon(icon)), \
             patch("mnmparse.app.map_overlay.MapOverlay", _MapWindow), \
             patch("mnmparse.app.map_downloads.MapDownloadController", side_effect=lambda *_: _DownloadController()), \
             patch("mnmparse.app.app_updates.AppUpdateController", side_effect=lambda *_: _DownloadController()):
            host.bootstrap(replace(cfg or Config(), start_capture_on_launch=False, setup_complete=True),
                           config_path=Path(self.temp.name) / "config.json")
        self.app.processEvents()
        return host

    def dispose(self, host):
        host._shut_down = True
        host.window.prepare_quit()
        controller = host.__dict__.get("npc_details")
        if controller is not None:
            controller.shutdown()
            controller.deleteLater()
        if host.overlay is not None:
            for panel in (host.overlay.timer_panel, host.overlay.attack_bar, host.overlay.npc_panel):
                panel.close()
                panel.deleteLater()
        for widget in (host.window, host.overlay, host.map_overlay, host.revenge_popout):
            if widget is not None:
                widget.close()
                widget.deleteLater()
        host.tray.hide()
        host.tray.deleteLater()
        for name in ("_timer", "_save_timer", "_activity_refresh", "_display_timer"):
            getattr(host.revenge, name).stop()
        host.revenge.deleteLater()
        self.app.processEvents()

    def test_controls_emit_once_and_apply_every_option_without_save(self):
        host = self.bootstrap()
        page, panel = host.window.page("settings"), host.overlay.npc_panel
        seen = []
        page.overlay_setting_changed.connect(lambda key, value: seen.append((key, value)))
        self.assertEqual(panel.options, NPC_DEFAULTS)
        for key, initial in NPC_DEFAULTS.items():
            with self.subTest(key=key):
                control = getattr(page, f"npc_{key}")
                self.assertEqual(control.isChecked(), initial)
                control.setChecked(not initial)
                self.assertEqual(seen[-1], (f"npc_{key}", not initial))
                self.assertEqual(panel.options[key], not initial)
                self.assertEqual(self.settings.value(f"npc/{key}", type=bool), not initial)
        self.assertEqual(len(seen), len(NPC_DEFAULTS))

    def test_live_settings_reopen_from_qsettings_before_config_is_saved(self):
        first = self.bootstrap()
        page = first.window.page("settings")
        desired = {key: not initial for key, initial in NPC_DEFAULTS.items()}
        for key, value in desired.items():
            getattr(page, f"npc_{key}").setChecked(value)
        self.settings.sync()
        self.dispose(first)
        self.hosts.remove(first)
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        second = self.bootstrap()
        self.assertEqual(second.overlay.npc_panel.options, desired)
        reopened = second.window.page("settings")
        self.assertEqual({key: getattr(reopened, f"npc_{key}").isChecked() for key in desired}, desired)

    def test_saved_choices_reopen_even_when_combat_overlay_is_unavailable(self):
        self.settings.setValue("npc/level", False)
        self.settings.setValue("npc/common_drops", True)
        host = self.bootstrap(combat_available=False)
        page = host.window.page("settings")
        self.assertFalse(page.npc_level.isChecked())
        self.assertTrue(page.npc_common_drops.isChecked())
        page.npc_unique_drops.setChecked(False)
        self.assertFalse(self.settings.value("npc/unique_drops", True, type=bool))
        self.assertFalse(page.form_config().npc_unique_drops)

    def test_save_records_selected_sections_in_config_and_revert_keeps_live_preferences(self):
        host = self.bootstrap()
        page = host.window.page("settings")
        desired = {**NPC_DEFAULTS, "common_drops": True, "quests": True, "reputation": True}
        for key, value in desired.items():
            getattr(page, f"npc_{key}").setChecked(value)
        page.revert()
        self.assertTrue(page.npc_common_drops.isChecked())
        page.save()
        saved = load_config(str(host.config_path))
        self.assertEqual({key: getattr(saved, f"npc_{key}") for key in desired}, desired)
        self.assertEqual(host.overlay.npc_panel.options, desired)
        self.assertEqual({key: getattr(host.cfg, f"npc_{key}") for key in desired}, desired)

    def test_consider_message_and_notable_selection_use_same_visible_panel(self):
        host = self.bootstrap()
        source = "a Wyrmsbane crusader seethes at you, ready to strike"
        event = parse_line(source, 1, "Hero")
        host.engine.message.emit(Message(source, 1, 1), event)
        panel = host.overlay.npc_panel
        self.assertEqual(panel._name, "a Wyrmsbane crusader")
        self.assertTrue(panel.isVisible())
        self.request.assert_any_call("npc", "a Wyrmsbane crusader")
        host.map_overlay.npc_selected.emit("Sample Keeper")
        self.assertEqual(panel._name, "Sample Keeper")
        self.assertTrue(panel.isVisible())
        self.request.assert_any_call("npc", "Sample Keeper")

    def test_detached_panel_follows_global_hide_without_losing_name_or_geometry(self):
        host = self.bootstrap()
        panel = host.overlay.npc_panel
        host.map_overlay.npc_selected.emit("Sample Keeper")
        panel._docked = False
        panel._free_geometry = QRect(80, 70, 300, panel.height())
        panel.sync()
        expected = panel.geometry()
        host.set_overlay_visible(False)
        self.assertFalse(panel.isVisible())
        self.assertEqual(panel._name, "Sample Keeper")
        host.set_overlay_visible(True)
        self.assertTrue(panel.isVisible())
        self.assertFalse(panel.docked)
        self.assertEqual(panel.geometry(), expected)
        host.overlay.move(130, 120)
        self.app.processEvents()
        self.assertEqual(panel.geometry(), expected)

    def test_lock_and_click_through_reach_npc_panel_without_taking_focus(self):
        host = self.bootstrap()
        host.map_overlay.npc_selected.emit("Sample Keeper")
        panel = host.overlay.npc_panel
        host.set_overlay_locked(True)
        self.assertEqual(panel.cursor().shape(), Qt.CursorShape.ArrowCursor)
        start = panel.geometry()
        event = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(10, 10),
                            QPointF(panel.mapToGlobal(QPoint(10, 10))), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        panel.mousePressEvent(event)
        self.assertIsNone(panel._drag_offset)
        self.assertEqual(panel.geometry(), start)
        self.assertTrue(panel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        panel._press = None
        host.overlay.set_click_through(True)
        self.assertTrue(panel.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        host.overlay.set_click_through(False)
        self.assertFalse(panel.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        host.set_overlay_locked(False)
        self.assertEqual(panel.cursor().shape(), Qt.CursorShape.SizeAllCursor)

    def test_panel_hide_action_updates_settings_control_and_can_be_reenabled(self):
        host = self.bootstrap()
        page, panel = host.window.page("settings"), host.overlay.npc_panel
        host.map_overlay.npc_selected.emit("Sample Keeper")
        panel._hide_panel()
        self.assertFalse(panel.isVisible())
        self.assertFalse(page.npc_enabled.isChecked())
        self.assertFalse(self.settings.value("npc/enabled", True, type=bool))
        page.npc_enabled.setChecked(True)
        self.assertTrue(panel.isVisible())
        self.assertEqual(panel._name, "Sample Keeper")


if __name__ == "__main__":
    unittest.main()
