"""Established capture profiles bypass newly introduced first-run setup."""
import json
from pathlib import Path
import tempfile
import unittest

from mnmparse.config import load_config


class SetupMigrationTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            contents = json.dumps(data)
            path.write_text(contents, encoding="utf-8")
            cfg = load_config(str(path))
            self.assertEqual(path.read_text(encoding="utf-8"), contents)
            return cfg

    def test_existing_character_and_crop_migrate_without_rewriting_preferences(self):
        cfg = self.load({"player_name": "Hero", "crop": [0, 2, 761, 938]})
        self.assertTrue(cfg.setup_complete)
        self.assertEqual(cfg.crop, (0, 2, 761, 938))
        self.assertTrue(cfg.casual_mode)
        self.assertFalse(cfg.casual_mode_confirmed)

    def test_explicit_incomplete_setup_is_not_overridden(self):
        cfg = self.load({"player_name": "Hero", "crop": [0, 2, 761, 938], "setup_complete": False})
        self.assertFalse(cfg.setup_complete)

    def test_missing_malformed_or_empty_capture_setup_stays_incomplete(self):
        for data in ({}, {"player_name": "Hero"}, {"crop": [0, 2, 761, 938]},
                     {"player_name": "Hero", "crop": [0, 2, 0, 938]},
                     {"player_name": "Hero", "crop": "bad"},
                     {"player_name": "Hero", "crop": [0, 2, 761, 938], "window_title": " "},
                     {"player_name": "Hero", "crop": [0, 2, 761, 938], "capture_backend": "bad"}):
            with self.subTest(data=data):
                self.assertFalse(self.load(data).setup_complete)

    def test_missing_file_remains_a_fresh_install(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertFalse(load_config(str(Path(folder) / "missing.json")).setup_complete)


if __name__ == "__main__":
    unittest.main()
