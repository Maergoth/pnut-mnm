"""The mode badge stays readable and honors animation preferences."""
import dataclasses
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from mnmparse.app.mode_button import ModeButton
from mnmparse.config import Config


class ModeButtonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_fire_badge_returns_to_flowers_when_casual(self):
        cfg = Config(casual_mode=False, casual_mode_confirmed=True, reduced_motion=True)
        badge = ModeButton(cfg)
        self.addCleanup(badge.close)
        self.assertEqual(badge.text(), "🔥 Elitist Scumbag Mode 🔥")
        self.assertEqual(badge.accessibleName(), "Elitist Scumbag Mode")
        badge.set_config(dataclasses.replace(cfg, casual_mode=True, casual_mode_confirmed=False))
        self.assertEqual(badge.text(), "🌼 Carebear Mode 🌼")
        self.assertEqual(badge.accessibleName(), "Carebear Mode")

    def test_animation_only_runs_for_visible_full_mode_without_reduced_motion(self):
        cfg = Config(casual_mode=False, casual_mode_confirmed=True)
        with patch("mnmparse.app.mode_button.reduced_motion", side_effect=lambda c: c.reduced_motion):
            badge = ModeButton(cfg)
            self.addCleanup(badge.close)
            self.assertFalse(badge._fire_timer.isActive())
            badge.show()
            self.app.processEvents()
            self.assertTrue(badge._fire_timer.isActive())
            badge.set_config(dataclasses.replace(cfg, reduced_motion=True))
            self.assertFalse(badge._fire_timer.isActive())
            self.assertIn("🔥", badge.text())
            badge.set_config(cfg)
            self.assertTrue(badge._fire_timer.isActive())
            badge.hide()
            self.assertFalse(badge._fire_timer.isActive())


if __name__ == "__main__":
    unittest.main()
