"""Carebear changes performance presentation without changing ordinary input tools."""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.pages import FeedPage
from mnmparse.app.timer_panel import TriggerPopup
from mnmparse.config import Config


class FeedInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.cfg = Config(player_name="Mogmo")
        self.full = dataclasses.replace(self.cfg, casual_mode=False, casual_mode_confirmed=True)

    def test_feed_retains_raw_lines_search_and_clipboard_across_modes(self):
        page = FeedPage(None, self.cfg, self.settings)
        self.addCleanup(page.close)
        lines = ["Tamsin crushes a rat for 321 points of damage.",
                 "There are 43 players in Fallen Pass who match your search.",
                 "Your Armor spell has worn off."]
        for ts, line in enumerate(lines, 100):
            page.append(line, "personal", False, ts)
        page._search.setText("Armor")
        for cfg in (self.full, self.cfg):
            page.set_config(cfg)
            self.assertEqual([entry.text for entry in page.view.entries()], lines)
            self.assertEqual(page._search.text(), "Armor")
            self.assertEqual(page.view._search, "Armor")
            page.copy_all()
            self.assertTrue(all(line in self.app.clipboard().text() for line in lines))

    def test_overlay_feed_and_trigger_popup_survive_mode_changes(self):
        overlay = OverlayWindow(self.settings, self.cfg)
        self.addCleanup(overlay.close)
        line = "Tamsin tells the party, 'rebuff now'"
        overlay.append_feed(line, "personal", False)
        popup = TriggerPopup("Rebuff", time.monotonic(), "#ffffff")
        overlay._timer_panel._popups.append(popup)
        for cfg in (self.full, self.cfg):
            overlay.set_config(cfg)
            self.assertEqual([entry.text for entry in overlay._feed.entries()], [line])
            self.assertIn(popup, overlay._timer_panel._popups)


if __name__ == "__main__":
    unittest.main()
