"""Capture profiles and portable backup controls use only disposable preferences."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QRect, QSettings, Signal
from PySide6.QtWidgets import QApplication

from mnmparse.app.profile_tools import ProfileTools, apply_settings, settings_payload
from mnmparse.backup import BackupError
from mnmparse.config import Config, save_config
from mnmparse.profiles import PROFILE_FIELDS, capture_profile, validate_profiles
from mnmparse.storage import atomic_json
from mnmparse.triggers import TriggerStore


class EngineStub(QObject):
    state_changed = Signal(str)
    is_running = False
    state = "stopped"
    flushed = 0

    def flush_preferences(self):
        self.flushed += 1
        return True


class ProfileToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "config.json"
        self.current = Config(log_dir=str(self.root / "logs"), player_name="Self")
        save_config(self.current, str(self.path))
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.engine = EngineStub()
        self.widget = ProfileTools(self.current, self.path, self.settings, self.engine)
        self.addCleanup(self.widget.close)
        self.widget.set_config_getter(lambda: self.current)
        self.widget.config_changed.connect(lambda cfg: setattr(self, "current", cfg))
        atomic_json(self.root / "triggers.json", TriggerStore().to_dict())

    def test_named_profile_captures_technical_fields_calibration_and_preserves_mode(self):
        self.current = dataclasses.replace(self.current, crop=(1, 2, 500, 400), fps=5,
                                           casual_mode=False, casual_mode_confirmed=True)
        self.widget.set_dimensions_getter(lambda: (1920, 1080))
        self.assertTrue(self.widget.save_profile("Character at 1080p"))
        profile = self.current.capture_profiles["Character at 1080p"]
        self.assertEqual(set(profile["settings"]), set(PROFILE_FIELDS))
        self.assertEqual(profile["calibration"], {"width": 1920, "height": 1080})
        self.current = dataclasses.replace(self.current, crop=(0, 60, 700, 600), fps=2,
                                           casual_mode=True, casual_mode_confirmed=False)
        self.assertTrue(self.widget.apply_capture_profile("Character at 1080p"))
        self.assertEqual(self.current.crop, (1, 2, 500, 400))
        self.assertEqual(self.current.fps, 5)
        self.assertTrue(self.current.casual_mode)
        self.assertFalse(self.current.casual_mode_confirmed)

    def test_profile_apply_and_restore_require_fully_stopped_capture(self):
        self.assertTrue(self.widget.save_profile("Default"))
        backup = self.root / "preferences.zip"
        self.widget.create_backup(backup)
        before = self.path.read_bytes()
        self.engine.is_running = True
        self.engine.state = "running"
        self.engine.state_changed.emit("running")
        self.assertFalse(self.widget.apply_button.isEnabled())
        self.assertFalse(self.widget.restore_button.isEnabled())
        self.assertFalse(self.widget.apply_capture_profile("Default"))
        with self.assertRaisesRegex(ValueError, "Stop capture"):
            self.widget.restore_backup(backup)
        self.assertEqual(self.path.read_bytes(), before)
        self.engine.is_running = False
        self.engine.state = "stopping"
        with self.assertRaisesRegex(ValueError, "Stop capture"):
            self.widget.restore_backup(backup)

    def test_backup_flushes_trigger_edits_and_casual_archive_uses_generic_profile_names(self):
        self.assertTrue(self.widget.save_profile("Teammate custom label"))
        calls = []
        self.widget.set_trigger_flush_callback(lambda: calls.append(True))
        path = self.root / "preferences.zip"
        self.widget.create_backup(path)
        self.assertEqual(calls, [True])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "config.json"})
            data = json.loads(archive.read("config.json"))
        self.assertEqual(set(data["capture_profiles"]), {"Profile 1"})
        self.assertNotIn("Teammate", json.dumps(data))

    def test_full_backup_restores_with_current_log_directory_and_refresh_signal(self):
        self.current = dataclasses.replace(self.current, casual_mode=False, casual_mode_confirmed=True)
        self.widget.load(self.current)
        self.settings.setValue("overlay/opacity", .7)
        self.settings.setValue("triggers/private-editor", "Teammate")
        data_root = Path(self.current.log_dir)
        atomic_json(data_root / "party.json", {"version": 4, "manual_in": ["Teammate"], "manual_out": [], "pet_owners": {}, "seen": {}})
        atomic_json(data_root / "vocabulary.json", {"version": 1, "words": {"teammate": 3}, "names": {"player": {"Teammate": 3}}})
        path = self.root / "preferences.zip"
        self.widget.create_backup(path, allow_sensitive=True)
        self.assertEqual(self.engine.flushed, 1)
        with zipfile.ZipFile(path) as archive:
            self.assertNotIn("triggers/private-editor", json.loads(archive.read("settings.json"))["values"])
        current_dir = str(self.root / "current-logs")
        self.current = dataclasses.replace(self.current, log_dir=current_dir)
        self.settings.setValue("overlay/opacity", .4)
        refreshed = []
        self.widget.restored.connect(refreshed.append)
        cfg = self.widget.restore_backup(path)
        self.assertEqual(cfg.log_dir, current_dir)
        self.assertTrue(cfg.casual_mode)
        self.assertFalse(cfg.casual_mode_confirmed)
        self.assertEqual(self.settings.value("overlay/opacity", type=float), .7)
        self.assertEqual(json.loads((Path(current_dir) / "party.json").read_text())["manual_in"], ["Teammate"])
        self.assertEqual(refreshed, [cfg])

    def test_full_backup_flushes_latest_revenge_activity_before_reading_saved_list(self):
        self.current = dataclasses.replace(self.current, casual_mode=False, casual_mode_confirmed=True)
        self.widget.load(self.current)
        path = self.root / "preferences.zip"
        saved = {"schema": 2, "entries": [{"name": "Opponent Ω with symbols!", "added_at": 100.0, "last_activity": 100.0}]}
        atomic_json(self.root / "revenge.json", saved)
        pending = {"schema": 2, "entries": [{"name": "Opponent Ω with symbols!", "added_at": 100.0, "last_activity": 250.0}]}
        flushed = []
        def flush():
            atomic_json(self.root / "revenge.json", pending)
            flushed.append(True)
            return True
        self.widget.set_revenge_flush_callback(flush)
        self.widget.create_backup(path, allow_sensitive=True)
        self.assertEqual(flushed, [True])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(json.loads(archive.read("revenge.json")), pending)

    def test_failed_revenge_flush_preserves_existing_backup_and_casual_never_calls_it(self):
        path = self.root / "preferences.zip"
        path.write_bytes(b"previous good backup")
        calls = []
        self.widget.set_revenge_flush_callback(lambda: calls.append(True) or False)
        self.widget.create_backup(path, allow_sensitive=False)
        self.assertEqual(calls, [])
        casual_archive = path.read_bytes()
        self.current = dataclasses.replace(self.current, casual_mode=False, casual_mode_confirmed=True)
        self.widget.load(self.current)
        with self.assertRaisesRegex(BackupError, "latest Revenge List activity"):
            self.widget.create_backup(path, allow_sensitive=True)
        self.assertEqual(calls, [True])
        self.assertEqual(path.read_bytes(), casual_archive)

    def test_registry_payload_and_restore_use_only_declared_primitive_keys(self):
        self.settings.setValue("main/rect", QRect(1, 2, 800, 600))
        self.settings.setValue("map/view", {"peer": "Teammate"})
        payload = settings_payload(self.settings)
        self.assertEqual(payload["values"]["main/rect"], [1, 2, 800, 600])
        self.assertNotIn("map/view", payload["values"])
        payload["values"]["arbitrary/identity"] = "Teammate"
        apply_settings(self.settings, payload)
        self.assertFalse(self.settings.contains("arbitrary/identity"))
        self.assertEqual(self.settings.value("main/rect"), QRect(1, 2, 800, 600))

    def test_profile_schema_rejects_wrong_fields_numbers_and_dimensions(self):
        profile = capture_profile(self.current)
        for bad in (dict(profile, schema=2), dict(profile, calibration={"width": -1, "height": 1}),
                    dict(profile, settings=dict(profile["settings"], fps=float("nan"))),
                    dict(profile, settings=dict(profile["settings"], casual_mode=False))):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_profiles({"Profile": bad})
        self.assertFalse(self.widget.save_profile(""))


if __name__ == "__main__":
    unittest.main()
