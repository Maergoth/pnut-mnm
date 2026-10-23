"""Options group related controls without changing how preferences are saved."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel

from mnmparse.app import theme
from mnmparse.app.main import _MissingEngine
from mnmparse.app.pages import SettingsPage
from mnmparse.config import Config, load_config
from tests.test_layout import clipped_widgets


class SettingsSectionsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.cfg = Config(player_name="Maergoth")
        self.engine = _MissingEngine(self.cfg)
        self.engine.config_path = self.path
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.page = SettingsPage(self.engine, self.cfg, self.settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def panels(self):
        return [self.page._content.itemAt(i).widget()
                for i in range(self.page._content.count())
                if self.page._content.itemAt(i).widget() is not None]

    def test_daily_options_precede_capture_tools_and_diagnostics(self):
        panels = self.panels()
        self.assertIs(panels[2], self.page.morality)
        expected = (
            "App updates", "Update maps", "General", "Overlay", "Considered NPC", "Revenge List",
            "Combat & session", "Mini stat window", "Clipboard", "Capture", "OCR", "Crop", "Logs & feed", "Status",
        )
        headings = []
        for panel in panels:
            if panel is self.page.morality or panel is self.page.profile_tools:
                continue
            heading = panel.findChild(QLabel, "Heading", Qt.FindChildOption.FindDirectChildrenOnly)
            if heading is not None:
                headings.append(heading.text())
        self.assertEqual(tuple(headings), expected)
        self.assertLess(panels.index(self.page.profile_tools),
                        next(i for i, p in enumerate(panels) if p.isAncestorOf(self.page._status_values["state"])))

    def test_controls_belong_to_their_expected_section(self):
        groups = {
            "General": (self.page.player_name, self.page.start_on_launch, self.page.minimize_to_tray,
                        self.page.reduced_motion),
            "Overlay": (self.page.overlay_enabled, self.page.overlay_locked, self.page.overlay_opacity),
            "Considered NPC": (self.page.npc_enabled, self.page.npc_level, self.page.npc_common_drops,
                               self.page.npc_unique_drops, self.page.npc_quests, self.page.npc_reputation),
            "Revenge List": (self.page.revenge_enabled, self.page.revenge_alert_sound,
                             self.page.revenge_alert_volume, self.page.revenge_days, self.page.revenge_entries),
            "Combat & session": (self.page.show_other_groups, self.page.include_personal,
                                 self.page.encounter_timeout, self.page.dummy_fix),
            "Mini stat window": (self.page.player_status_enabled, self.page.player_status_separate,
                                 self.page.player_status_picker, self.page.player_status_xp,
                                 self.page.low_health_enabled, self.page.low_health_flash,
                                 self.page.low_health_sound, self.page.low_health_volume),
            "Clipboard": (self.page.export_auto, self.page.export_sound, self.page.export_line),
            "Logs & feed": (self.page.log_dir, self.page.log_break, self.page.log_max, self.page.feed_max_lines),
        }
        for title, widgets in groups.items():
            with self.subTest(section=title):
                panel = next(p for p in self.panels()
                             if (label := p.findChild(QLabel, "Heading", Qt.FindChildOption.FindDirectChildrenOnly))
                             is not None and label.text() == title)
                self.assertTrue(all(panel.isAncestorOf(widget) for widget in widgets))

    def test_revenge_customization_follows_master_and_alert_toggles(self):
        self.assertTrue(self.page.revenge_options.isHidden())
        self.assertFalse(self.page.revenge_days.isEnabled())
        self.page.revenge_enabled.setChecked(True)
        self.assertFalse(self.page.revenge_options.isHidden())
        self.assertTrue(self.page.revenge_alert_enabled.isEnabled())
        self.assertTrue(self.page.revenge_alert_volume.isEnabled())
        self.page.revenge_alert_enabled.setChecked(False)
        self.assertFalse(self.page.revenge_alert_volume.isEnabled())
        self.assertFalse(self.page.revenge_alert_preview.isEnabled())
        self.assertTrue(self.page.revenge_days.isEnabled())
        self.page.revenge_enabled.setChecked(False)
        self.assertTrue(self.page.revenge_options.isHidden())
        self.assertFalse(self.page.revenge_entries.isEnabled())

    def test_save_and_revert_preserve_new_and_existing_preferences(self):
        cfg = dataclasses.replace(self.cfg, revenge_enabled=True, revenge_days=7, revenge_entries=25,
                                  revenge_alert_sound="Dink", revenge_alert_volume=15,
                                  revenge_alert_duration=6.0, revenge_alert_cooldown=45.0,
                                  include_personal=True, show_other_groups=True, feed_max_lines=250)
        self.page.load(cfg)
        self.assertEqual(self.page.form_config(), cfg)
        self.page.revenge_alert_volume.set_value(20)
        self.page.revenge_alert_cooldown.setValue(90)
        self.page.revenge_days.setValue(14)
        self.page.feed_max_lines.setValue(400)
        self.page.reduced_motion.setChecked(True)
        changed = self.page.form_config()
        self.assertNotEqual(changed, cfg)
        self.page.revert()
        self.assertEqual(self.page.form_config(), cfg)
        self.page.revenge_alert_volume.set_value(20)
        self.page.revenge_alert_cooldown.setValue(90)
        self.page.revenge_alert_sound.setCurrentText("None")
        expected = self.page.form_config()
        emitted = []
        self.page.config_changed.connect(emitted.append)
        self.page.save()
        self.assertEqual(load_config(str(self.path)), expected)
        self.assertEqual(emitted, [expected])
        self.page.revenge_alert_duration.setValue(10)
        self.page.revert()
        self.assertEqual(self.page.form_config(), expected)

    def test_alert_preview_uses_selected_sound_and_own_volume(self):
        requests = []
        self.page.revenge_alert_preview_requested.connect(lambda *args: requests.append(args))
        self.page.revenge_enabled.setChecked(True)
        self.page.revenge_alert_volume.set_value(13)
        self.page.revenge_alert_sound.setCurrentText("Dink")
        self.page.revenge_alert_preview.click()
        self.assertEqual(requests, [("Dink", 13)])
        self.page.revenge_alert_sound.setCurrentText("None")
        self.page.revenge_alert_preview.click()
        self.assertEqual(requests, [("Dink", 13)])
        self.page.revenge_alert_enabled.setChecked(False)
        self.page.revenge_alert_preview.click()
        self.assertEqual(requests, [("Dink", 13)])

    def test_enabled_revenge_options_fit_at_settings_minimum_and_wide_sizes(self):
        self.page.revenge_enabled.setChecked(True)
        self.page.show()
        for width in (900, 1400):
            with self.subTest(width=width):
                self.page.resize(width, 650)
                for _ in range(4):
                    self.app.processEvents()
                self.assertEqual(clipped_widgets(self.page), [])
                footer = self.page._save.parentWidget()
                self.assertFalse(self.page._scroll.isAncestorOf(footer))
                self.assertTrue(footer.isVisibleTo(self.page))


if __name__ == "__main__":
    unittest.main()
