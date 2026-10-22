"""Overburdened visual preference is strict, durable and enabled for old saves."""
import dataclasses
import json
from pathlib import Path
import tempfile
import unittest

from mnmparse.config import Config, load_config, save_config


class OverburdenedConfigTests(unittest.TestCase):
    def test_default_legacy_save_and_disabled_round_trip(self):
        self.assertTrue(Config().overburdened_flash)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text('{}', encoding="utf-8")
            self.assertTrue(load_config(str(path)).overburdened_flash)
            cfg = dataclasses.replace(Config(), overburdened_flash=False)
            save_config(cfg, str(path))
            self.assertFalse(load_config(str(path)).overburdened_flash)

    def test_invalid_values_are_rejected_and_valid_boolean_strings_restore(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (None, [], {}, 2, 0.5, "maybe"):
                with self.subTest(value=value):
                    self.assertTrue(any("overburdened_flash" in issue for issue in
                                        dataclasses.replace(Config(), overburdened_flash=value).problems()))
                    path.write_text(json.dumps({"overburdened_flash": value}), encoding="utf-8")
                    with self.assertLogs("mnmparse.config", level="WARNING"):
                        self.assertTrue(load_config(str(path)).overburdened_flash)
            for value in (False, 0, "false", "off"):
                path.write_text(json.dumps({"overburdened_flash": value}), encoding="utf-8")
                self.assertFalse(load_config(str(path)).overburdened_flash)
