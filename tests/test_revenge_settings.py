"""Revenge display preferences remain bounded and survive saved profiles."""
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from mnmparse.config import Config, load_config, save_config
from mnmparse.privacy import safe_config_data


class RevengeSettingsTests(unittest.TestCase):
    def test_defaults_and_unlimited_values(self):
        cfg = Config()
        self.assertFalse(cfg.revenge_enabled)
        self.assertEqual((cfg.revenge_days, cfg.revenge_entries), (30, 100))
        self.assertEqual(dataclasses.replace(cfg, revenge_days=0, revenge_entries=0).problems(), [])

    def test_preferences_round_trip_without_changing_privacy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            cfg = Config(revenge_enabled=True, revenge_days=7, revenge_entries=25)
            save_config(cfg, str(path))
            loaded = load_config(str(path))
            self.assertEqual((loaded.revenge_days, loaded.revenge_entries), (7, 25))
            self.assertTrue(loaded.casual_mode)
            self.assertFalse(loaded.casual_mode_confirmed)

    def test_invalid_limits_use_defaults_when_loaded(self):
        for bad in (-1, 99999, True, 2.5, "many", None):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps({"revenge_days": bad, "revenge_entries": bad}), encoding="utf-8")
                loaded = load_config(str(path))
                self.assertEqual((loaded.revenge_days, loaded.revenge_entries), (30, 100))
                self.assertEqual(loaded.problems(), [])

    def test_direct_validation_rejects_invalid_types_and_ranges(self):
        for field, bad in (("revenge_days", -1), ("revenge_days", 3651), ("revenge_days", False),
                           ("revenge_entries", 1001), ("revenge_entries", "10")):
            with self.subTest(field=field, bad=bad):
                issues = dataclasses.replace(Config(), **{field: bad}).problems()
                self.assertTrue(any(field in issue for issue in issues))

    def test_technical_backups_retain_display_limits(self):
        safe = safe_config_data(dataclasses.asdict(Config(revenge_days=14, revenge_entries=50,
                                                       player_name="PrivateName")))
        self.assertEqual((safe["revenge_days"], safe["revenge_entries"]), (14, 50))
        self.assertNotIn("player_name", safe)


if __name__ == "__main__":
    unittest.main()
