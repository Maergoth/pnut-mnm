"""Portable release, owned retention, profile safety and real helper regressions."""
from __future__ import annotations

import io
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from mnmparse import __version__
from mnmparse import backup, storage
from mnmparse.app import app_updates as updates
from mnmparse.app.launch_identity import InstanceCoordinator, register_update_boot
from mnmparse.app.map_overlay import _Load
from mnmparse.config import Config
from mnmparse.maps import MapImage
from mnmparse.provenance import validate_build_info
from mnmparse.triggers import TriggerStore


def build(version="9.8.7", dirty=False):
    return {"schema": 1, "version": version, "commit": "a" * 40, "dirty": dirty,
            "built_at": "2026-10-10T12:00:00+00:00"}


class StorageTests(unittest.TestCase):
    def test_concurrent_writes_are_unique_and_leave_complete_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            with patch.object(storage.os, "replace", wraps=storage.os.replace) as replace:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    list(pool.map(lambda i: storage.atomic_json(path, {"index": i, "value": "x" * 1000}), range(40)))
            sources = [str(call.args[0]) for call in replace.call_args_list]
            self.assertEqual(len(set(sources)), 40)
            self.assertEqual(json.loads(path.read_text())["value"], "x" * 1000)
            self.assertEqual(list(Path(directory).glob(".pnut-write-*")), [])

    def test_failed_replace_preserves_old_data_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preferences.json"
            path.write_bytes(b"old")
            with patch.object(storage.os, "replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    storage.atomic_write(path, b"new")
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).glob(".pnut-write-*")), [])

    def test_retention_preserves_unknown_active_protected_and_other_kinds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            for name, kind, created, pinned, lease in (
                    ("new", "update-stage", now, False, 0), ("old", "update-stage", now - 10, False, 0),
                    ("protected", "update-stage", now - 1000000, False, 0),
                    ("active", "update-stage", now - 1000000, True, now + 100),
                    ("expired", "update-stage", now - 1000000, True, now - 100),
                    ("other", "application-backup", now - 1000000, False, 0)):
                child = root / name
                child.mkdir()
                storage.atomic_json(child / storage.OWNERSHIP_FILE,
                    {"schema": 1, "kind": kind, "created": created, "pinned": pinned, "lease_until": lease})
            (root / "unknown").mkdir()
            (root / "bad").mkdir()
            (root / "bad" / storage.OWNERSHIP_FILE).write_text("[]")
            removed = storage.prune_owned(root, "update-stage", keep=1, max_age_days=7, protected=(root / "protected",))
            self.assertEqual({p.name for p in removed}, {"old", "expired"})
            self.assertEqual({p.name for p in root.iterdir()}, {"new", "protected", "active", "other", "unknown", "bad"})

    def test_retention_refuses_nested_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            external = root / "external"
            external.mkdir()
            (external / "personal").write_bytes(b"keep")
            owned = root / "owned"
            owned.mkdir()
            storage.mark_owned(owned, "update-stage")
            try:
                (owned / "link").symlink_to(external, target_is_directory=True)
            except OSError:
                if sys.platform != "win32":
                    self.skipTest("This account cannot create symbolic links")
                powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
                command = "New-Item -ItemType Junction -Path " + updates._quote_ps(owned / "link") + " -Value " + updates._quote_ps(external)
                result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                        capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode != 0:
                    self.skipTest("This account cannot create links or junctions")
            self.assertEqual(storage.prune_owned(root, "update-stage", keep=0), [])
            self.assertEqual((external / "personal").read_bytes(), b"keep")


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.triggers = self.root / "triggers.json"
        self.settings = self.root / "settings.json"
        self.party = self.root / "party.json"
        self.vocab = self.root / "vocabulary.json"
        self.revenge = self.root / "revenge.json"
        self.archive = self.root / "preferences.zip"
        storage.atomic_json(self.config, dataclasses.asdict(Config()))
        storage.atomic_json(self.triggers, TriggerStore().to_dict())
        storage.atomic_json(self.settings, {"schema": 1, "values": {"map/locked": True}})
        storage.atomic_json(self.party, {"version": 4, "saved": time.time(), "seen": {},
                                        "manual_in": ["Teammate"], "manual_out": [], "pet_owners": {"Pet": "Teammate"}})
        storage.atomic_json(self.vocab, {"version": 1, "names": {"player": {"Teammate": 10}}, "words": {"teammate": 10}})
        storage.atomic_json(self.revenge, {"schema": 1, "names": ["Opponent"]})

    def export(self, sensitive=False):
        return backup.backup_profile(self.archive, self.config, self.triggers, self.settings,
                                     allow_sensitive=sensitive, party_path=self.party, vocab_path=self.vocab,
                                     revenge_path=self.revenge)

    def full_mode(self):
        data = dataclasses.asdict(Config(casual_mode=False, casual_mode_confirmed=True))
        storage.atomic_json(self.config, data)

    def test_casual_backup_omits_sensitive_preferences_and_resets_export_templates(self):
        cfg = dataclasses.asdict(Config(export_line="Teammate identity {damage}"))
        storage.atomic_json(self.config, cfg)
        manifest = self.export()
        self.assertFalse(manifest["sensitive"])
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "config.json"})
            self.assertNotIn(b"Teammate", archive.read("config.json"))

    def test_full_backup_requires_confirmation_and_restore_always_starts_casual(self):
        with self.assertRaises(backup.BackupError):
            self.export(True)
        self.full_mode()
        manifest = self.export(True)
        self.assertEqual(set(manifest["files"]), {"config.json", "triggers.json", "settings.json", "party.json", "vocabulary.json", "revenge.json"})
        self.party.write_text("old")
        self.vocab.write_text("old")
        self.revenge.write_text("old")
        backup.restore_profile(self.archive, self.config, self.triggers, self.settings,
                               party_path=self.party, vocab_path=self.vocab, revenge_path=self.revenge)
        config = json.loads(self.config.read_text())
        self.assertTrue(config["casual_mode"])
        self.assertFalse(config["casual_mode_confirmed"])
        self.assertEqual(json.loads(self.party.read_text())["manual_in"], ["Teammate"])
        self.assertEqual(json.loads(self.vocab.read_text())["words"], {"teammate": 10})
        self.assertEqual(json.loads(self.revenge.read_text())["names"], ["Opponent"])

    def test_malformed_payloads_are_rejected_before_any_restore_write(self):
        for bad in ({"casual_mode": "false"}, {"fps": float("nan")}, {"crop": [0, 0, 0, 0]}, {"config_schema": 2}, {"unknown": True},
                    {"revenge_days": -1}, {"revenge_days": 3651}, {"revenge_entries": True}, {"revenge_entries": 1001}):
            with self.subTest(bad=bad):
                data = json.dumps(bad).encode()
                manifest = {"schema": 1, "sensitive": False, "files": {"config.json": hashlib.sha256(data).hexdigest()}}
                with zipfile.ZipFile(self.archive, "w") as archive:
                    archive.writestr("manifest.json", json.dumps(manifest))
                    archive.writestr("config.json", data)
                old = self.config.read_bytes()
                with self.assertRaises(backup.BackupError):
                    backup.restore_profile(self.archive, self.config, self.triggers)
                self.assertEqual(self.config.read_bytes(), old)

    def test_path_and_checksum_rejection_preserve_profile(self):
        self.export()
        with zipfile.ZipFile(self.archive, "a") as archive:
            archive.writestr("../outside.txt", "bad")
        old = self.config.read_bytes()
        with self.assertRaises(backup.BackupError):
            backup.restore_profile(self.archive, self.config, self.triggers)
        self.assertEqual(self.config.read_bytes(), old)
        self.export()
        with zipfile.ZipFile(self.archive) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        files["config.json"] = b"{}"
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        with self.assertRaisesRegex(backup.BackupError, "checksum"):
            backup.restore_profile(self.archive, self.config, self.triggers)
        self.assertEqual(self.config.read_bytes(), old)

    def test_invalid_sensitive_schemas_are_rejected(self):
        self.full_mode()
        for path, bad in ((self.settings, {"schema": 1, "values": {"key": {"unsupported": 1}}}),
                          (self.party, {"version": 99}), (self.vocab, {"version": 1, "names": {}, "words": {"name": -1}}),
                          (self.revenge, {"schema": 1, "names": [""]}),
                          (self.revenge, {"schema": 1, "names": [None]}),
                          (self.revenge, {"schema": True, "names": ["Opponent"]})):
            with self.subTest(path=path.name):
                old = path.read_bytes()
                storage.atomic_json(path, bad)
                with self.assertRaises(backup.BackupError):
                    self.export(True)
                path.write_bytes(old)

    def test_failed_multi_file_restore_rolls_back_earlier_preferences(self):
        self.full_mode()
        self.export(True)
        self.config.write_bytes(b"original config")
        self.triggers.write_bytes(b"original triggers")
        original = backup.atomic_write
        def fail(path, data):
            if Path(path) == self.triggers:
                raise OSError("disk unavailable")
            original(path, data)
        with patch.object(backup, "atomic_write", side_effect=fail), self.assertRaises(OSError):
            backup.restore_profile(self.archive, self.config, self.triggers, self.settings)
        self.assertEqual(self.config.read_bytes(), b"original config")
        self.assertEqual(self.triggers.read_bytes(), b"original triggers")

    def test_dated_revenge_backup_preserves_all_entries_and_legacy_unknown_times(self):
        entries = [{"name": "Opponent", "added_at": None, "last_activity": None}]
        entries += [{"name": "Enemy" + chr(65 + i // 26) + chr(65 + i % 26),
                     "added_at": 0, "last_activity": 253402300799 if i == 0 else 1.5} for i in range(102)]
        payload = {"schema": 2, "entries": entries}
        for days, count in ((30, 100), (0, 0)):
            with self.subTest(days=days, count=count):
                cfg = Config(casual_mode=False, casual_mode_confirmed=True, revenge_days=days, revenge_entries=count)
                storage.atomic_json(self.config, dataclasses.asdict(cfg))
                storage.atomic_json(self.revenge, payload)
                self.export(True)
                self.revenge.write_bytes(b"prior saved list")
                backup.restore_profile(self.archive, self.config, self.triggers, revenge_path=self.revenge)
                self.assertEqual(json.loads(self.revenge.read_text()), payload,
                                 "Display age/count filters must not discard stored entries during backup/restore")
                config = json.loads(self.config.read_text())
                self.assertTrue(config["casual_mode"])
                self.assertFalse(config["casual_mode_confirmed"])
                self.assertEqual((config["revenge_days"], config["revenge_entries"]), (days, count))

    def test_casual_backup_excludes_dated_revenge_identities_and_times(self):
        storage.atomic_json(self.revenge, {"schema": 2, "entries": [
            {"name": "Opponent", "added_at": 100.0, "last_activity": 200.0}]})
        self.export()
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "config.json"})
            self.assertNotIn(b"Opponent", archive.read("config.json"))

    def test_manual_revenge_names_round_trip_without_content_restrictions_in_both_schemas(self):
        names = ["two words", "symbols: [] <> ! #", "Draíocht Ω 🐉", "You", "Yourself", "Self", "a", "An", "The",
                 "first line\nsecond\rline", "界" * 32767]
        payloads = ({"schema": 1, "names": names},
                    {"schema": 2, "entries": [{"name": name, "added_at": None, "last_activity": 200.5} for name in names]})
        for payload in payloads:
            with self.subTest(schema=payload["schema"]):
                self.full_mode()
                storage.atomic_json(self.revenge, payload)
                self.export(True)
                self.revenge.write_bytes(b"prior list")
                backup.restore_profile(self.archive, self.config, self.triggers, revenge_path=self.revenge)
                self.assertEqual(json.loads(self.revenge.read_text(encoding="utf-8")), payload)

    def test_invalid_dated_revenge_is_rejected_before_any_restore_write(self):
        valid = {"name": "Opponent", "added_at": 10.0, "last_activity": 20.0}
        invalid = [dict(valid, name=None), dict(valid, name=""), dict(valid, name=" \t "), dict(valid, name=[]),
                   dict(valid, name="x" * 32768),
                   dict(valid, raw="private line"),
                   {"name": "Opponent", "last_activity": 20.0}, "Opponent"]
        for field in ("added_at", "last_activity"):
            for value in (True, "20", -1, float("nan"), float("inf"), 253402300800, 10 ** 400):
                invalid.append(dict(valid, **{field: value}))
        bad_payloads = [{"schema": 2, "entries": [entry]} for entry in invalid]
        bad_payloads += [{"schema": 2, "entries": [valid, dict(valid, name="opponent")]},
                         {"schema": 2, "entries": [valid] * 1001},
                         {"schema": True, "entries": [valid]},
                         {"schema": 2, "entries": [valid], "unknown": True}]
        config_data = json.dumps(dataclasses.asdict(Config(casual_mode=False, casual_mode_confirmed=True))).encode()
        old = {path: path.read_bytes() for path in (self.config, self.triggers, self.revenge)}
        for index, payload in enumerate(bad_payloads):
            with self.subTest(index=index):
                data = json.dumps(payload).encode()
                files = {"config.json": config_data, "revenge.json": data}
                manifest = {"schema": 1, "sensitive": True,
                            "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()}}
                with zipfile.ZipFile(self.archive, "w") as archive:
                    archive.writestr("manifest.json", json.dumps(manifest))
                    for name, value in files.items():
                        archive.writestr(name, value)
                with patch.object(backup, "atomic_write") as write, self.assertRaises(backup.BackupError):
                    backup.restore_profile(self.archive, self.config, self.triggers, revenge_path=self.revenge)
                write.assert_not_called()
                self.assertEqual({path: path.read_bytes() for path in old}, old)


class QtReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_map_decode_occurs_on_worker_and_emits_qimage(self):
        entry = MapImage("map", "https://static.wikitide.net/map.png", "wiki")
        from PIL import Image
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "white").save(buffer, "PNG")
        png = buffer.getvalue()
        repo = Mock()
        repo.image.return_value = png
        worker = _Load(1, repo, "Night Harbor", entry, False, threading.Semaphore(1))
        results = []
        decoder_threads = []
        original = QImage.fromData
        def decode(data):
            decoder_threads.append(threading.get_ident())
            return original(data)
        worker.signal.ready.connect(lambda token, result, error: results.append((result, error)), Qt.ConnectionType.DirectConnection)
        thread = threading.Thread(target=worker.run)
        with patch("mnmparse.app.map_overlay.QImage.fromData", side_effect=decode):
            thread.start()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(decoder_threads, [thread.ident])
        self.assertNotEqual(thread.ident, threading.get_ident())
        self.assertIsInstance(results[0][0][1], QImage)
        self.assertFalse(results[0][0][1].isNull())
        self.assertEqual(results[0][1], "")

    def test_installation_and_profile_guards_activate_original_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            owner = InstanceCoordinator(root / "install", root / "profile")
            self.addCleanup(owner.close)
            calls = []
            owner.activation.connect(lambda: calls.append(True))
            self.assertTrue(owner.acquire())
            for installation, profile in ((root / "install", root / "other-profile"), (root / "other-install", root / "profile")):
                second = InstanceCoordinator(installation, profile)
                self.addCleanup(second.close)
                self.assertFalse(second.acquire())
                deadline = time.monotonic() + 2
                target = len(calls) + 1
                while len(calls) < target and time.monotonic() < deadline:
                    self.app.processEvents()
                    time.sleep(0.005)
                self.assertEqual(len(calls), target)
            independent = InstanceCoordinator(root / "independent", root / "independent-profile")
            self.addCleanup(independent.close)
            self.assertTrue(independent.acquire())
            owner.close()
            replacement = InstanceCoordinator(root / "install", root / "profile")
            self.addCleanup(replacement.close)
            self.assertTrue(replacement.acquire())


class UpdateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "App"
        self.install.mkdir()
        self.token = "b" * 32
        self.receipt = self.install / ".pnut-update.json"

    def record(self, status="pending", version=updates.APP_VERSION):
        data = {"schema": 1, "token": self.token, "status": status, "version": version,
                "previous_version": "0.9.1", "backup": str(self.install / (".pnut-backup-" + self.token)), "reported": False}
        storage.atomic_json(self.receipt, data)
        return data

    def test_health_ack_requires_target_version_and_matching_update_token(self):
        self.record()
        with patch.object(updates, "installed_directory", return_value=self.install), patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": "wrong"}):
            self.assertFalse(updates.acknowledge_startup())
        with patch.object(updates, "installed_directory", return_value=self.install), patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": self.token}):
            self.assertTrue(updates.acknowledge_startup())
        path = self.install / f".pnut-health-{self.token}.json"
        self.assertEqual(json.loads(path.read_text())["version"], updates.APP_VERSION)
        self.record(version="9.8.7")
        with patch.object(updates, "installed_directory", return_value=self.install), patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": self.token}):
            self.assertFalse(updates.acknowledge_startup())

    def test_pre_qt_boot_registration_records_actual_child_pid(self):
        self.record()
        executable = self.install / ("a" * 24 + ".exe")
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(executable)), patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": self.token}):
            register_update_boot()
        result = json.loads((self.install / f".pnut-boot-{self.token}.json").read_text())
        self.assertEqual(result["pid"], os.getpid())
        self.assertEqual(result["executable"], str(executable))

    def test_results_are_persisted_consumed_once_and_pending_recovery_is_visible(self):
        with patch.object(updates, "installed_directory", return_value=self.install), patch.object(updates, "_stage_root", return_value=self.root / "stages"), patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": ""}):
            self.record("healthy")
            self.assertIn("started successfully", updates.consume_update_result())
            self.assertEqual(updates.consume_update_result(), "")
            self.record("pending")
            self.assertIn("did not finish", updates.consume_update_result())
            self.assertEqual(updates.consume_update_result(), "")
            self.record("failed")
            self.assertIn("needs recovery", updates.consume_update_result())

    def test_retained_recovery_requires_matching_owned_backup_and_valid_files(self):
        record = self.record("healthy")
        path = Path(record["backup"])
        path.mkdir()
        (path / updates.EXECUTABLE).write_bytes(b"old")
        (path / "_internal").mkdir()
        self.assertIsNone(updates.recovery_package(self.install))
        storage.mark_owned(path, "application-backup")
        self.assertEqual(updates.recovery_package(self.install), path)
        record["backup"] = str(self.root / "outside")
        storage.atomic_json(self.receipt, record)
        self.assertIsNone(updates.recovery_package(self.install))

    def test_package_provenance_rejects_wrong_version_and_dirty_build(self):
        package = self.root / "package"
        (package / "_internal").mkdir(parents=True)
        metadata = package / "_internal" / "build-info.json"
        for info in (build("9.8.6"), build(dirty=True), {"schema": 99}):
            storage.atomic_json(metadata, info)
            with self.assertRaises(updates.UpdateError):
                updates.verify_package_version(package, "9.8.7")
        storage.atomic_json(metadata, build())
        self.assertEqual(updates.verify_package_version(package, "9.8.7"), build())

    def test_failed_update_keeps_complete_earlier_recovery_backup(self):
        record = self.record("rolled_back")
        empty = Path(record["backup"])
        empty.mkdir()
        storage.mark_owned(empty, "application-backup")
        older = self.install / (".pnut-backup-" + "d" * 32)
        (older / "_internal").mkdir(parents=True)
        (older / updates.EXECUTABLE).write_bytes(b"known good earlier release")
        storage.mark_owned(older, "application-backup")
        record.update(previous_backup=str(older), previous_backup_version="0.9.0")
        storage.atomic_json(self.receipt, record)
        with patch.object(updates, "installed_directory", return_value=self.install), patch.object(updates, "_stage_root", return_value=self.root / "stages"):
            self.assertEqual(updates.recovery_package(self.install), older)
            self.assertIn("recovered", updates.consume_update_result())
            self.assertEqual(updates.recovery_package(self.install), older)
            self.assertEqual((older / updates.EXECUTABLE).read_bytes(), b"known good earlier release")

    def test_recovery_uses_embedded_backup_version_and_preserves_profile_arguments(self):
        record = self.record("healthy")
        package = Path(record["backup"])
        (package / "_internal").mkdir(parents=True)
        (package / updates.EXECUTABLE).write_bytes(b"known good release")
        storage.mark_owned(package, "application-backup")
        storage.atomic_json(package / "_internal" / "build-info.json", build("0.8.9"))
        controller = updates.AppUpdateController()
        with patch.object(updates, "installed_directory", return_value=self.install), patch.object(updates, "_stage_root", return_value=self.root / "stages"), patch.object(updates.subprocess, "Popen") as start, patch.object(updates, "make_restart_script", wraps=updates.make_restart_script) as script:
            self.assertTrue(controller.prepare_recovery(arguments=["--config", "profile.json"]))
        self.assertEqual(script.call_args.kwargs["version"], "0.8.9")
        self.assertTrue(script.call_args.kwargs["health_required"])
        self.assertEqual(script.call_args.kwargs["arguments"], ["--config", "profile.json"])
        self.assertEqual(start.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_package_report_requires_exact_embedded_build_and_version(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "package_release.py"
        spec = importlib.util.spec_from_file_location("pnut_package_release", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        info = build(__version__)
        validate_build_info(info)
        report = {"ok": True, "frozen": True, "stage": "complete", "app_version": __version__, "build": info}
        module.validate_report(report, info)
        for wrong in (dict(report, app_version="0.0.1"), dict(report, build=dict(info, commit="b" * 40)), dict(report, frozen=False)):
            with self.assertRaises(RuntimeError):
                module.validate_report(wrong, info)


@unittest.skipUnless(sys.platform == "win32", "The deferred updater is a Windows helper")
class WindowsHelperTests(unittest.TestCase):
    def compile_probe(self, target: Path, body: str):
        code = 'public class Probe { public static void Main() { ' + body + ' } }'
        command = "Add-Type -TypeDefinition " + updates._quote_ps(code) + " -OutputAssembly " + updates._quote_ps(target) + " -OutputType WindowsApplication"
        process = subprocess.run([str(self.powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                 capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(process.returncode, 0, process.stderr)

    def exercise(self, healthy: bool):
        with tempfile.TemporaryDirectory(prefix="pnut-helper-test-") as directory:
            root = Path(directory).resolve()
            install = root / "App"
            package = root / "staging" / "package"
            install.mkdir()
            package.mkdir(parents=True)
            self.powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            self.compile_probe(install / updates.EXECUTABLE, 'System.Environment.Exit(0);')
            old_bytes = (install / updates.EXECUTABLE).read_bytes()
            if healthy:
                body = ('string token=System.Environment.GetEnvironmentVariable("_PNUT_UPDATE_TOKEN");'
                        'string dir=System.IO.Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);'
                        'System.IO.File.WriteAllText(System.IO.Path.Combine(dir,".pnut-health-"+token+".json"),'
                        '"{\\\"schema\\\":1,\\\"token\\\":\\\""+token+"\\\",\\\"version\\\":\\\"9.8.7\\\"}");')
            else:
                body = 'System.Environment.Exit(7);'
            self.compile_probe(package / updates.EXECUTABLE, body)
            for path, content in ((install / "_internal" / "marker", b"old library"),
                                  (package / "_internal" / "marker", b"new library"),
                                  (install / "config.json", b"personal config"), (install / "triggers.json", b"personal triggers"),
                                  (install / "logs" / "private.txt", b"personal logs")):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            token = "c" * 32
            prior_backup = install / (".pnut-backup-" + "d" * 32)
            (prior_backup / "_internal").mkdir(parents=True)
            (prior_backup / updates.EXECUTABLE).write_bytes(b"earlier complete application")
            storage.mark_owned(prior_backup, "application-backup")
            storage.atomic_json(install / ".pnut-update.json", {"schema": 1, "token": "d" * 32,
                "status": "healthy", "version": "0.9.1", "previous_version": "0.9.0", "backup": str(prior_backup), "reported": True})
            script = root / "helper.ps1"
            storage.atomic_write(script, updates.make_restart_script(install, package, 2147483647, token,
                                 version="9.8.7", health_timeout=2).encode("utf-8-sig"))
            process = subprocess.run([str(self.powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                     capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            receipt = json.loads((install / ".pnut-update.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(receipt["status"], "healthy" if healthy else "rolled_back", process.stderr)
            self.assertEqual(receipt["previous_backup"], str(prior_backup))
            self.assertEqual(receipt["previous_backup_version"], "0.9.0")
            self.assertEqual(process.returncode, 0 if healthy else 1, process.stderr)
            self.assertEqual((install / "_internal" / "marker").read_bytes(), b"new library" if healthy else b"old library")
            if not healthy:
                self.assertEqual((install / updates.EXECUTABLE).read_bytes(), old_bytes)
                self.assertIn("did not acknowledge", receipt["error"])
            for path, expected in (("config.json", b"personal config"), ("triggers.json", b"personal triggers"), ("logs/private.txt", b"personal logs")):
                self.assertEqual((install / path).read_bytes(), expected)

    def test_real_helper_requires_startup_ack_before_success(self):
        self.exercise(True)

    def test_real_helper_rolls_back_when_replacement_exits_without_ack(self):
        self.exercise(False)


if __name__ == "__main__":
    unittest.main()
