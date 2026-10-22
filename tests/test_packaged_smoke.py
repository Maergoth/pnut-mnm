"""Startup diagnostics must handle the import failure that broke the Windows release."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mnmparse.app.smoke import _exercise_leaderboard, run_smoke_test


class PackagedSmokeTests(unittest.TestCase):
    def test_leaderboard_storage_is_explicitly_skipped_off_windows(self):
        report = {}
        with tempfile.TemporaryDirectory() as temporary, patch("mnmparse.app.smoke.sys.platform", "linux"):
            _exercise_leaderboard(report, Path(temporary))
            self.assertEqual(list(Path(temporary).iterdir()), [])
        self.assertEqual(report, {"leaderboard": {"windows_storage_skipped": True}})

    @unittest.skipUnless(sys.platform == "win32", "Windows release dependencies")
    def test_missing_native_crypto_fails_the_leaderboard_smoke_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report_path = root / "report.json"
            with patch("mnmparse.app.smoke._exercise", side_effect=lambda report: _exercise_leaderboard(report, root)), \
                 patch("mnmparse.leaderboard.public_key", side_effect=ImportError("simulated native crypto failure")):
                self.assertEqual(run_smoke_test(report_path), 1)
            report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertFalse(report["ok"])
        self.assertIn("simulated native crypto failure", report["error"])
        self.assertNotIn("leaderboard", report)

    def test_failed_qt_import_is_reported_before_normal_startup(self):
        script = """
import importlib.abc
import runpy
import sys
class BrokenQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PySide6.QtCore':
            raise ImportError('DLL load failed while importing QtCore: simulated regression')
        if fullname == 'mnmparse.app.main':
            raise AssertionError('normal startup was imported before the Qt failure was caught')
sys.meta_path.insert(0, BrokenQt())
sys.argv = ['PNUT M&M', '--smoke-test', '--report', sys.argv[1]]
runpy.run_module('mnmparse.app', run_name='__main__')
"""
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            child = subprocess.run(
                [sys.executable, "-c", script, str(report_path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(child.returncode, 1, child.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertEqual(report["stage"], "import PySide6.QtCore")
            self.assertIn("simulated regression", report["error"])
            self.assertIn("ImportError", report["traceback"])
            self.assertNotIn("Traceback", child.stderr)

    def test_success_overwrites_previous_failure_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            report_path.write_text('{"ok": false}', encoding="utf-8")
            with patch("mnmparse.app.smoke._exercise"):
                self.assertEqual(run_smoke_test(report_path), 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["stage"], "complete")

    def test_stalled_startup_is_bounded_and_reports_failure(self):
        script = """
import sys
import time
from pathlib import Path
from unittest.mock import patch
from mnmparse.app.smoke import run_smoke_test
with patch('mnmparse.app.smoke._exercise', side_effect=lambda report: time.sleep(60)):
    sys.exit(run_smoke_test(Path(sys.argv[1]), timeout_seconds=0.1))
"""
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            child = subprocess.run(
                [sys.executable, "-c", script, str(report_path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(child.returncode, 124, child.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertFalse(report["ok"])
            self.assertIn("exceeded", report["error"])

    @unittest.skipUnless(sys.platform == "win32", "Windows release dependencies")
    def test_real_windows_startup_uses_disposable_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "report.json"
            child = subprocess.run(
                [sys.executable, "-m", "mnmparse.app", "--smoke-test", "--report", str(report_path)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(child.returncode, 0, report.get("traceback", child.stderr))
            self.assertTrue(report["ok"])
            self.assertIn("MapOverlay", report["windows"])
            self.assertIn("triggers", report["pages"])
            self.assertIn("leaderboard", report["pages"])
            self.assertEqual(report["leaderboard"], {
                "native_crypto": True, "windows_storage": True, "counter_roundtrip": True,
                "cache_roundtrip": True, "fresh_read_required": True,
                "clipboard": True, "numeric_sort": True,
            })
            self.assertTrue(all(type(value) is bool for value in report["leaderboard"].values()))
            self.assertNotIn("public_key", report["leaderboard"])
            self.assertNotIn("alias", report["leaderboard"])
            self.assertEqual(set(report["presets"]), {"Gatekick", "Healkick", "Invis Break"})
            self.assertGreater(report["chat_share_chars"], 0)
            self.assertLessEqual(report["chat_share_chars"], 255)
            self.assertGreaterEqual(len(report["window_titles"]), 8)
            self.assertEqual(report["pet_rollup"], {
                "label": "SmokeOwner + SmokeOwner's Pet", "overlay_label": "SmokeOwner + Pet", "damage": 50,
            })
            self.assertEqual(report["legal_documents"], ["open-source", "privacy", "terms"])
            self.assertTrue(report["image_processing"])
            self.assertEqual(set(report["player_status"]), {
                "reader", "dual_scale_xp", "fresh_hp", "thirty_second_rate", "carebear_own_xp", "passive_flash",
            })
            self.assertTrue(all(report["player_status"].values()))
            self.assertEqual(report["setup"]["style"], "Modern")
            self.assertIn("PNUT setup", report["setup"]["heading"])
            self.assertTrue(report["setup"]["casual"])
            self.assertFalse(report["setup"]["capture_started"])
            self.assertTrue(report["setup"]["local_calibration_preview"])
            self.assertTrue(report["setup"]["local_frame_access"])
            self.assertTrue(report["setup"]["calibration_cleared"])
            self.assertTrue(report["setup"]["mode_slider"])
            self.assertTrue(report["setup"]["crop_checkbox_removed"])
            self.assertTrue(report["setup"]["common_options"])
            self.assertTrue(all(report["revenge"].values()))
            for key in ("columns", "custom_width", "hover_pause", "timed_dismissal"):
                self.assertTrue(report["npc_reference"][key], key)
            self.assertEqual(report["elitist_badge"], "flames")
            self.assertTrue(all(max(rgb) < 70 for rgb in report["setup"]["background_rgb"]))
            for title in report["window_titles"]:
                self.assertRegex(title, r"^[0-9a-f]{24}$")


if __name__ == "__main__":
    unittest.main()
