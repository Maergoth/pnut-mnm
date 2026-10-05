"""App and map update controls persist preferences without starting network work."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from mnmparse.app.main import _MissingEngine
from mnmparse.app.pages import SettingsPage
from mnmparse.config import Config


class MapSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.temp.name) / "settings.ini")
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        cfg = Config(player_name="Maergoth")
        self.page = SettingsPage(_MissingEngine(cfg), cfg, self.settings)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_startup_preference_applies_without_saving_config(self):
        self.assertFalse(self.page.map_download_on_startup.isChecked())
        self.assertFalse(self.page.app_update_on_startup.isChecked())
        self.page.map_download_on_startup.setChecked(True)
        self.page.app_update_on_startup.setChecked(True)
        self.settings.sync()
        reloaded = QSettings(self.settings_path, QSettings.Format.IniFormat)
        cfg = Config(player_name="Maergoth")
        reopened = SettingsPage(_MissingEngine(cfg), cfg, reloaded)
        try:
            self.assertTrue(reopened.map_download_on_startup.isChecked())
            self.assertTrue(reopened.app_update_on_startup.isChecked())
            reopened.map_download_on_startup.setChecked(False)
            reopened.app_update_on_startup.setChecked(False)
            self.assertFalse(reloaded.value("map/download_on_startup", True, type=bool))
            self.assertFalse(reloaded.value("app/update_on_startup", True, type=bool))
        finally:
            reopened.close()
            reopened.deleteLater()

    def test_download_request_is_disabled_until_completion(self):
        requests = []
        self.page.map_download_requested.connect(lambda: requests.append(True))
        self.page.map_download_button.click()
        self.assertEqual(requests, [True])
        self.assertTrue(self.page.map_download_status.isHidden())

        self.page.set_map_download_status("Downloading maps…", running=True)
        self.page.map_download_button.click()
        self.assertEqual(requests, [True])
        self.assertFalse(self.page.map_download_status.isHidden())
        self.assertEqual(self.page.map_download_status.text(), "Downloading maps…")

        self.page.set_map_download_status("Maps updated.", running=False)
        self.page.map_download_button.click()
        self.assertEqual(len(requests), 2)
        self.assertEqual(self.page.map_download_status.text(), "Maps updated.")
        self.page.set_map_download_status("", running=False)
        self.assertTrue(self.page.map_download_status.isHidden())

    def test_app_update_then_restart_requests_and_running_state(self):
        requests, restarts = [], []
        self.page.app_update_requested.connect(lambda: requests.append(True))
        self.page.app_restart_requested.connect(lambda: restarts.append(True))
        self.assertEqual(self.page.app_update_button.text(), "Update")
        self.assertTrue(self.page.app_update_status.isHidden())
        self.page.app_update_button.click()
        self.assertEqual(requests, [True])
        self.assertEqual(restarts, [])

        self.page.set_app_update_status("Downloading app update…", running=True)
        self.page.app_update_button.click()
        self.assertEqual(requests, [True])
        self.assertFalse(self.page.app_update_status.isHidden())

        self.page.set_app_update_status("Update ready.", running=False, ready=True)
        self.assertEqual(self.page.app_update_button.text(), "Restart to update")
        self.page.app_update_button.click()
        self.assertEqual(requests, [True])
        self.assertEqual(restarts, [True])
        self.assertEqual(self.page.app_update_status.text(), "Update ready.")

        self.page.set_app_update_status("Could not restart. Try again.", running=False)
        self.assertEqual(self.page.app_update_button.text(), "Update")
        self.page.app_update_button.click()
        self.assertEqual(len(requests), 2)
        self.assertEqual(restarts, [True])
        self.page.set_app_update_status("", running=False)
        self.assertTrue(self.page.app_update_status.isHidden())

    def test_app_and_map_controls_are_first_and_wiki_link_is_external(self):
        top_panel = self.page._content.itemAt(0).widget()
        map_panel = self.page._content.itemAt(1).widget()
        self.assertTrue(top_panel.isAncestorOf(self.page.app_update_button))
        self.assertTrue(top_panel.isAncestorOf(self.page.app_update_on_startup))
        self.assertTrue(map_panel.isAncestorOf(self.page.map_download_button))
        self.assertTrue(map_panel.isAncestorOf(self.page.map_download_on_startup))
        self.assertTrue(self.page.map_download_source.openExternalLinks())
        self.assertIn("/wiki/Category:Zones", self.page.map_download_source.text())


if __name__ == "__main__":
    unittest.main()
