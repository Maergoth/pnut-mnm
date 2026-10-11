"""Separate-capture migration and configurable own-health alerts."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mnmparse.backup import _config_bytes
from mnmparse.config import Config, load_config, save_config
from mnmparse.player_telemetry import PlayerTelemetry
from mnmparse.privacy import safe_config_data
from mnmparse.profiles import apply_profile, capture_profile, validate_profiles


class PlayerStatusBackendTests(unittest.TestCase):
    def test_new_and_completed_legacy_configs_keep_their_intended_capture_mode(self):
        chat_crop = [0, 20, 600, 450]
        stats_crop = [850, 50, 1150, 320]
        cases = (
            ({}, True, False),
            ({"setup_complete": False}, True, False),
            ({"setup_complete": True}, False, True),
            ({"setup_complete": True, "player_status_crop": stats_crop}, True, True),
            ({"player_name": "Hero", "crop": chat_crop}, False, True),
            ({"setup_complete": True, "player_status_separate": True}, True, True),
            ({"setup_complete": True, "player_status_crop": stats_crop, "player_status_separate": False}, False, True),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for data, separate, complete in cases:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data))
                    cfg = load_config(str(path))
                    self.assertEqual(cfg.player_status_separate, separate)
                    self.assertEqual(cfg.setup_complete, complete)
                    self.assertEqual(cfg.player_status_crop, tuple(data["player_status_crop"])
                                     if data.get("player_status_crop") else None)
                    backup = json.loads(_config_bytes(data))
                    self.assertEqual(backup["player_status_separate"], separate)
                    self.assertEqual(backup["setup_complete"], complete)
            cfg = Config(player_status_separate=True, low_health_threshold=27)
            save_config(cfg, str(path))
            self.assertEqual(load_config(str(path)), cfg)

    def test_threshold_coercion_rejects_out_of_range_and_noninteger_inputs(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for value in (0, -1, 101, True, 25.5, "25.5", "bad", None):
                with self.subTest(value=value):
                    path.write_text(json.dumps({"low_health_threshold": value}))
                    with self.assertLogs("mnmparse.config", level="WARNING"):
                        self.assertEqual(load_config(str(path)).low_health_threshold, 10)
                    self.assertTrue(any("low_health_threshold" in issue for issue in
                                        dataclasses.replace(Config(), low_health_threshold=value).problems()))
            for value, expected in ((1, 1), (100, 100), ("27", 27)):
                path.write_text(json.dumps({"low_health_threshold": value}))
                self.assertEqual(load_config(str(path)).low_health_threshold, expected)
            report = safe_config_data(dataclasses.asdict(Config(low_health_threshold=27)))
            self.assertEqual(report["low_health_threshold"], 27)
            self.assertTrue(report["player_status_separate"])

    def test_profiles_preserve_pending_mode_and_infer_legacy_capture_modes(self):
        pending = capture_profile(Config())
        cropped = capture_profile(Config(player_status_crop=(850, 50, 1150, 320)))
        legacy_stats = json.loads(json.dumps(cropped))
        del legacy_stats["settings"]["player_status_separate"]
        legacy_shared = json.loads(json.dumps(pending))
        del legacy_shared["settings"]["player_status_separate"]
        del legacy_shared["settings"]["player_status_crop"]
        cfg = Config(capture_profiles={"Pending": pending, "Stats": legacy_stats, "Shared": legacy_shared})
        self.assertIs(validate_profiles(cfg.capture_profiles), cfg.capture_profiles)
        self.assertTrue(apply_profile(cfg, "Pending").player_status_separate)
        self.assertIsNone(apply_profile(cfg, "Pending").player_status_crop)
        self.assertTrue(apply_profile(cfg, "Stats").player_status_separate)
        self.assertEqual(apply_profile(cfg, "Stats").player_status_crop, (850, 50, 1150, 320))
        self.assertFalse(apply_profile(cfg, "Shared").player_status_separate)
        self.assertIsNone(apply_profile(cfg, "Shared").player_status_crop)
        with self.assertRaises(ValueError):
            validate_profiles({"Invalid": dict(pending, settings=dict(pending["settings"],
                                                                       player_status_separate=1))})

    def test_health_threshold_is_strict_and_changes_keep_thirty_second_xp_window(self):
        telemetry = PlayerTelemetry(low_health_threshold=25)
        for ts in range(0, 21):
            telemetry.update({"hp_current": 25, "hp_max": 100, "xp_fraction": .5}, ts)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        telemetry.update({"hp_current": 24, "hp_max": 100, "xp_fraction": .5}, 21)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        telemetry.set_health_threshold(30)
        for ts in range(22, 32):
            telemetry.update({"hp_current": 25, "hp_max": 100, "xp_fraction": .52}, ts)
            if ts == 22:
                self.assertFalse(telemetry.snapshot()["hp_low"])
        snap = telemetry.snapshot()
        self.assertTrue(snap["hp_low"])
        self.assertEqual(snap["hp_below10_episodes"], 1)
        self.assertEqual(snap["xp_observed_seconds"], 30)
        self.assertAlmostEqual(snap["xp_points_per_hour"], 240)

    def test_health_notification_reports_actual_percentage_and_rejects_stale_threshold(self):
        from mnmparse.app.main import App

        shell = SimpleNamespace(cfg=Config(low_health_threshold=25, low_health_flash=False),
                                _selftest=False, _shut_down=False, triggers=Mock())
        App._on_low_health(shell, 23.4)
        self.assertEqual(shell.triggers.fire.call_args.args[0].trigger.timer_label, "Low HP: 23.4%")
        shell.cfg = dataclasses.replace(shell.cfg, low_health_threshold=10)
        App._on_low_health(shell, 23.4)
        self.assertEqual(shell.triggers.fire.call_count, 1)
        shell.triggers.audio.run.reset_mock()
        App._preview_revenge_alert(shell, "Alert", 43)
        shell.triggers.audio.run.assert_called_once_with("sound", sound="Alert", volume=43)
        self.assertEqual(shell.triggers.fire.call_count, 1)


if __name__ == "__main__":
    unittest.main()
