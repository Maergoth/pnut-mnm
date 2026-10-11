"""Separate mini-stat calibration remains independent of combat chat and old profiles."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app import theme
from mnmparse.app.main import _MissingEngine
from mnmparse.app.pages import SettingsPage
from mnmparse.config import Config, load_config, save_config
from mnmparse.ocr import OcrLine
from mnmparse.profiles import apply_profile, capture_profile, validate_profiles
from tests.test_layout import clipped_widgets


class PlayerStatusConfigTests(unittest.TestCase):
    def test_optional_stats_region_round_trip_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            cfg = Config(crop=(0, 60, 700, 600), player_status_crop=(1100, 50, 1400, 320))
            save_config(cfg, str(path))
            self.assertEqual(json.loads(path.read_text())["player_status_crop"], [1100, 50, 1400, 320])
            self.assertEqual(load_config(str(path)), cfg)
            path.write_text('{"crop": [10, 20, 600, 450]}')
            self.assertIsNone(load_config(str(path)).player_status_crop)

    def test_invalid_stats_crop_is_rejected_without_breaking_chat_configuration(self):
        invalid = ([], [0, 0, 2], "0,0,100,100", [True, 0, 100, 100], [0, 0, 2.5, 100],
                   [0, 0, "1.5", 100], [-1, 0, 100, 100], [100, 0, 50, 100], [0, 0, 100, 0], {})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in invalid:
                with self.subTest(value=value):
                    path.write_text(json.dumps({"crop": [10, 20, 600, 450], "player_status_crop": value}))
                    self.assertTrue(any("player_status_crop" in issue
                                        for issue in dataclasses.replace(Config(), player_status_crop=value).problems()))
                    with self.assertLogs("mnmparse.config", level="WARNING"):
                        loaded = load_config(str(path))
                    self.assertIsNone(loaded.player_status_crop)
                    self.assertEqual(loaded.crop, (10, 20, 600, 450))
                    self.assertEqual(loaded.problems(), [])
            path.write_text(json.dumps({"player_status_crop": ["1", "2", "300", "200"]}))
            self.assertEqual(load_config(str(path)).player_status_crop, (1, 2, 300, 200))

    def test_profiles_preserve_separate_region_and_old_profiles_reset_to_automatic(self):
        cfg = Config(player_status_crop=(1100, 50, 1400, 320), casual_mode=False, casual_mode_confirmed=True)
        profile = capture_profile(cfg, (1920, 1080))
        legacy = json.loads(json.dumps(profile))
        del legacy["settings"]["player_status_crop"]
        cfg = dataclasses.replace(cfg, capture_profiles={"New": profile, "Old": legacy})
        self.assertIs(validate_profiles(cfg.capture_profiles), cfg.capture_profiles)
        restored = apply_profile(dataclasses.replace(cfg, player_status_crop=None), "New")
        self.assertEqual(restored.player_status_crop, (1100, 50, 1400, 320))
        self.assertIsNone(apply_profile(cfg, "Old").player_status_crop)
        self.assertEqual(restored.crop, cfg.crop)
        self.assertFalse(restored.casual_mode)
        self.assertTrue(restored.casual_mode_confirmed)
        for value in ([False, 0, 100, 100], [0, 0, 100.5, 100], [100, 0, 50, 100]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_profiles({"Invalid": dict(profile, settings=dict(profile["settings"], player_status_crop=value))})


class StatusEngine(_MissingEngine):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.frame = np.full((600, 1200, 3), (30, 70, 120), dtype=np.uint8)
        self.ocr_calls = []

    def grab_frame(self):
        return self.frame

    def test_ocr(self, frame, crop, cfg):
        self.ocr_calls.append((frame, crop, cfg))
        return [OcrLine(0, 0, 12, "Health 10/100")]


class PlayerStatusSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "config.json"
        self.cfg = Config(crop=(10, 20, 600, 450))
        self.engine = StatusEngine(self.cfg)
        self.engine.config_path = self.path
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.page = SettingsPage(self.engine, self.cfg, self.settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def settle_job(self):
        deadline = time.monotonic() + 2
        while self.page.player_status_picker._job is not None and time.monotonic() < deadline:
            self.app.processEvents()
        self.assertIsNone(self.page.player_status_picker._job)

    def test_region_toggle_save_revert_and_master_toggle_preserve_chat(self):
        self.assertTrue(self.page.player_status_region.isHidden())
        self.page.player_status_separate.setChecked(True)
        self.assertFalse(self.page.player_status_region.isHidden())
        self.page.player_status_picker.set_crop((850, 50, 1150, 320))
        self.page.save()
        saved = load_config(str(self.path))
        self.assertEqual(saved.player_status_crop, (850, 50, 1150, 320))
        self.assertEqual(saved.crop, self.cfg.crop)
        self.assertEqual(self.engine._cfg.crop, self.cfg.crop)
        self.page.player_status_enabled.setChecked(False)
        self.assertTrue(self.page.player_status_region.isHidden())
        self.assertEqual(self.page.form_config().player_status_crop, saved.player_status_crop)
        self.page.player_status_enabled.setChecked(True)
        self.page.player_status_separate.setChecked(False)
        self.assertIsNone(self.page.form_config().player_status_crop)
        self.page.revert()
        self.assertEqual(self.page.form_config(), saved)
        self.assertFalse(self.page.player_status_region.isHidden())

    def test_stats_frame_and_ocr_use_full_frame_without_applying_chat_crop(self):
        self.page.player_status_separate.setChecked(True)
        picker = self.page.player_status_picker
        picker.set_crop((850, 50, 1150, 320))
        picker.capture_frame()
        self.settle_job()
        self.assertEqual(picker.canvas.frame_size(), (1200, 600))
        np.testing.assert_array_equal(picker._display_frame(), self.engine.frame)
        self.assertTrue(picker._cfg.casual_mode)
        picker.run_test_ocr()
        self.settle_job()
        self.assertEqual(len(self.engine.ocr_calls), 1)
        frame, crop, cfg = self.engine.ocr_calls[0]
        self.assertIs(frame, self.engine.frame)
        self.assertEqual(crop, (850, 50, 1150, 320))
        self.assertEqual(cfg.crop, crop)
        self.assertEqual(picker._lines.item(0).text(), "Health 10/100")
        self.assertEqual(self.engine._cfg, self.cfg)
        self.assertEqual(self.page.crop_picker.crop(), self.cfg.crop)

    def test_expanded_region_fits_settings_at_minimum_width(self):
        self.page.player_status_separate.setChecked(True)
        self.page.show()
        self.page.resize(900, 650)
        for _ in range(4):
            self.app.processEvents()
        self.assertEqual(clipped_widgets(self.page), [])


if __name__ == "__main__":
    unittest.main()
