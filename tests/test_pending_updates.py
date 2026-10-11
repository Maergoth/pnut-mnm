"""Ready updates survive normal quit and are verified again before installation."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app import app_updates as updates
from mnmparse.storage import atomic_json, mark_owned


class PendingUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.install = self.root / "App"
        self.install.mkdir()
        (self.install / updates.EXECUTABLE).write_bytes(b"original application")
        (self.install / "_internal").mkdir()
        (self.install / "config.json").write_text("personal configuration")
        self.stage_root = self.root / "updates"
        self.receipt = self.install / ".pnut-pending-update.json"
        self.version = "9.8.7"
        self.archive_name = f"PNUT-MnM-{self.version}-windows.zip"
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(updates.EXECUTABLE, b"verified replacement application")
            archive.writestr("_internal/python.dll", b"replacement support library")
            archive.writestr("_internal/build-info.json", json.dumps({
                "schema": 1, "version": self.version, "commit": "a" * 40,
                "dirty": False, "built_at": "2026-10-10T12:00:00+00:00",
            }))
            archive.writestr("START HERE.txt", b"instructions")
        self.archive_data = buffer.getvalue()
        self.checksum = (hashlib.sha256(self.archive_data).hexdigest()
                         + "  " + self.archive_name + "\n").encode("ascii")
        self.payload = {
            "tag_name": "v" + self.version, "draft": False, "prerelease": False,
            "assets": [{
                "name": name, "size": size,
                "browser_download_url": ("https://github.com/Maergoth/pnut-mnm/releases/download/v"
                                         + self.version + "/" + name),
            } for name, size in ((self.archive_name, len(self.archive_data)),
                                 (self.archive_name + ".sha256", len(self.checksum)))],
        }
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(updates, "installed_directory", return_value=self.install))
        self.patches.enter_context(patch.object(updates, "_stage_root", return_value=self.stage_root))
        self.patches.enter_context(patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": ""}))
        self.fetch = self.patches.enter_context(patch.object(updates, "_fetch", side_effect=self._fetch))

    def _fetch(self, url, limit, cancelled, destination=None, progress=None):
        if destination is not None:
            destination.write_bytes(self.archive_data)
            return b""
        if url.endswith(".sha256"):
            return self.checksum
        return json.dumps(self.payload).encode("utf-8")

    def download(self):
        result = updates.download_update(threading.Event(), lambda message: None,
                                         installation=self.install)
        self.assertEqual(result[0], self.version)
        self.package = result[1]
        self.stage = self.package.parent
        return result

    def restore(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        self.assertTrue(controller.restore_pending())
        self.assertEqual(controller._ready_version, self.version)
        self.assertIsNotNone(controller.ready_package)
        return controller

    def assert_rejected(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        messages = []
        controller.finished.connect(messages.append)
        with patch.object(updates.subprocess, "Popen") as process:
            self.assertFalse(controller.restore_pending())
            self.assertFalse(controller.prepare_restart())
        self.assertIsNone(controller.ready_package)
        process.assert_not_called()
        return messages

    def rewrite_receipt(self, **changes):
        record = json.loads(self.receipt.read_text(encoding="utf-8"))
        record.update(changes)
        atomic_json(self.receipt, record)

    def recovery_backup(self):
        backup = self.install / (".pnut-backup-" + "c" * 32)
        (backup / "_internal").mkdir(parents=True)
        (backup / updates.EXECUTABLE).write_bytes(b"known good earlier release")
        atomic_json(backup / "_internal" / "build-info.json", {
            "schema": 1, "version": "0.9.9", "commit": "b" * 40,
            "dirty": False, "built_at": "2026-10-09T12:00:00+00:00",
        })
        mark_owned(backup, "application-backup")
        atomic_json(self.install / ".pnut-update.json", {
            "schema": 1, "token": "c" * 32, "status": "healthy",
            "version": updates.APP_VERSION, "previous_version": "0.9.9",
            "backup": str(backup), "reported": True,
        })
        return backup

    def test_download_persists_readiness_before_queued_result_can_be_lost(self):
        result = self.download()
        self.assertTrue(self.receipt.is_file())
        self.assertTrue((self.stage / self.archive_name).is_file())
        self.assertEqual((self.stage / "release.sha256").read_bytes(), self.checksum)
        # Quit before the worker result reaches the UI. The next process still
        # recovers the completed download without another network request.
        closing = updates.AppUpdateController()
        closing.shutdown()
        closing._on_completed(result, "ready")
        self.assertIsNone(closing.ready_package)
        self.fetch.reset_mock()
        self.restore()
        self.fetch.assert_not_called()
        self.assertEqual((self.install / updates.EXECUTABLE).read_bytes(), b"original application")

    def test_ready_download_survives_controller_shutdown_and_new_controller(self):
        self.download()
        first = self.restore()
        first.shutdown()
        second = self.restore()
        self.assertEqual(second._ready_version, first._ready_version)
        self.assertEqual((self.install / "config.json").read_text(), "personal configuration")

    def test_next_launch_prepares_once_with_requested_profile_arguments(self):
        self.download()
        arguments = ["--config", str(self.root / "My character's profile.json")]
        with patch.object(updates, "make_restart_script", wraps=updates.make_restart_script) as script, \
             patch.object(updates.subprocess, "Popen") as process:
            prepared, message = updates.prepare_pending_update(arguments)
            self.assertTrue(prepared, message)
            self.assertFalse(self.receipt.exists())
            self.assertFalse(updates.prepare_pending_update(arguments)[0])
        process.assert_called_once()
        self.assertEqual(script.call_args.kwargs["arguments"], arguments)
        self.assertEqual(script.call_args.kwargs["version"], self.version)
        self.assertEqual((self.install / updates.EXECUTABLE).read_bytes(), b"original application")

    def test_replacement_startup_token_never_starts_another_update(self):
        self.download()
        with patch.dict(os.environ, {"_PNUT_UPDATE_TOKEN": "b" * 32}), \
             patch.object(updates.AppUpdateController, "restore_pending") as restore, \
             patch.object(updates.subprocess, "Popen") as process:
            self.assertEqual(updates.prepare_pending_update(), (False, ""))
        restore.assert_not_called()
        process.assert_not_called()
        self.assertTrue(self.receipt.is_file())

    def test_source_startup_does_not_inspect_downloads_or_launch_helpers(self):
        with patch.object(updates, "installed_directory", side_effect=updates.UpdateError("source build")), \
             patch.object(updates.AppUpdateController, "restore_pending") as restore, \
             patch.object(updates.subprocess, "Popen") as process:
            self.assertEqual(updates.prepare_pending_update(), (False, ""))
        restore.assert_not_called()
        process.assert_not_called()
        self.fetch.assert_not_called()

    def test_archive_tampering_after_quit_cannot_launch_helper(self):
        self.download()
        with (self.stage / self.archive_name).open("ab") as archive:
            archive.write(b"changed after download")
        self.assertTrue(self.assert_rejected())

    def test_checksum_tampering_after_quit_cannot_launch_helper(self):
        self.download()
        (self.stage / "release.sha256").write_bytes(b"0" * 64 + b"  " + self.archive_name.encode())
        self.assertTrue(self.assert_rejected())

    def test_archive_is_reverified_between_restore_and_restart(self):
        self.download()
        controller = self.restore()
        with (self.stage / self.archive_name).open("ab") as archive:
            archive.write(b"changed after restore")
        with patch.object(updates.subprocess, "Popen") as process:
            self.assertFalse(controller.prepare_restart())
        process.assert_not_called()

    def test_missing_receipt_after_restore_cannot_bypass_archive_verification(self):
        self.download()
        controller = self.restore()
        self.receipt.unlink()
        (self.package / updates.EXECUTABLE).write_bytes(b"changed extracted executable")
        with patch.object(updates.subprocess, "Popen") as process:
            self.assertFalse(controller.prepare_restart())
        process.assert_not_called()

    def test_helper_uses_freshly_extracted_archive_instead_of_changed_payload(self):
        self.download()
        controller = self.restore()
        (self.package / updates.EXECUTABLE).write_bytes(b"changed extracted executable")
        with patch.object(updates, "make_restart_script", wraps=updates.make_restart_script) as script, \
             patch.object(updates.subprocess, "Popen") as process:
            self.assertTrue(controller.prepare_restart())
        fresh_package = script.call_args.args[1]
        self.assertNotEqual(fresh_package, self.package)
        self.assertEqual((fresh_package / updates.EXECUTABLE).read_bytes(), b"verified replacement application")
        process.assert_called_once()

    def test_pending_receipt_is_consumed_before_helper_and_cannot_retry_after_rollback(self):
        self.download()
        controller = self.restore()
        def spawn(*args, **kwargs):
            self.assertFalse(self.receipt.exists())
        with patch.object(updates.subprocess, "Popen", side_effect=spawn) as process:
            self.assertTrue(controller.prepare_restart())
            self.assertFalse(controller.prepare_restart())
        process.assert_called_once()
        # A helper rollback restarts the old version. It must not automatically
        # attempt the same failed update again on that launch.
        after_rollback = updates.AppUpdateController()
        self.addCleanup(after_rollback.shutdown)
        self.assertFalse(after_rollback.restore_pending())

    def test_failed_helper_spawn_restores_pending_download_for_retry(self):
        self.download()
        controller = self.restore()
        with patch.object(updates.subprocess, "Popen", side_effect=OSError("helper unavailable")):
            self.assertFalse(controller.prepare_restart())
        self.assertTrue(self.receipt.is_file())
        retry = self.restore()
        with patch.object(updates.subprocess, "Popen") as process:
            self.assertTrue(retry.prepare_restart())
        process.assert_called_once()

    def test_successful_explicit_recovery_cancels_ready_update_after_helper_spawn(self):
        self.download()
        controller = self.restore()
        self.recovery_backup()
        def spawn(*args, **kwargs):
            self.assertTrue(self.receipt.is_file())
            self.assertIsNotNone(controller.ready_package)
        with patch.object(updates.subprocess, "Popen", side_effect=spawn) as process:
            self.assertTrue(controller.prepare_recovery())
        process.assert_called_once()
        self.assertFalse(self.receipt.exists())
        self.assertIsNone(controller.ready_package)
        self.assertEqual(controller._ready_version, "")
        after_recovery = updates.AppUpdateController()
        self.addCleanup(after_recovery.shutdown)
        self.assertFalse(after_recovery.restore_pending())

    def test_failed_recovery_helper_spawn_preserves_ready_update(self):
        self.download()
        controller = self.restore()
        self.recovery_backup()
        with patch.object(updates.subprocess, "Popen", side_effect=OSError("helper unavailable")):
            self.assertFalse(controller.prepare_recovery())
        self.assertTrue(self.receipt.is_file())
        self.assertIsNotNone(controller.ready_package)
        self.assertEqual(controller._ready_version, self.version)
        self.restore()

    def test_malformed_pending_update_does_not_block_explicit_recovery(self):
        self.download()
        self.receipt.write_bytes(b"{malformed pending receipt")
        self.recovery_backup()
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        with patch.object(updates.subprocess, "Popen") as process:
            self.assertTrue(controller.prepare_recovery())
        process.assert_called_once()
        self.assertFalse(self.receipt.exists())

    def test_current_or_older_pending_versions_cannot_be_installed(self):
        for running_version in (self.version, "9.9.0"):
            with self.subTest(running_version=running_version):
                self.download()
                with patch.object(updates, "APP_VERSION", running_version):
                    self.assert_rejected()
                self.assertFalse(self.receipt.exists())

    def test_pending_receipt_belongs_only_to_its_installation(self):
        self.download()
        self.rewrite_receipt(installation=str(self.root / "Other application"))
        self.assertTrue(self.assert_rejected())

    def test_stage_and_archive_paths_cannot_escape_staging(self):
        self.download()
        original = self.receipt.read_bytes()
        changes = ({"stage": "../outside"}, {"stage": str(self.root / "outside")},
                   {"stage": self.version + "-nested/child"}, {"archive": "../application.zip"})
        for change in changes:
            with self.subTest(change=change):
                self.receipt.write_bytes(original)
                self.rewrite_receipt(**change)
                self.assertTrue(self.assert_rejected())

    def test_malformed_receipt_cannot_prepare_an_update(self):
        self.download()
        for content in (b"{", b"[]", b"null", b"{}", b" " * 4097):
            with self.subTest(content=content[:30]):
                self.receipt.write_bytes(content)
                self.assertTrue(self.assert_rejected())

    def test_linked_receipt_archive_or_stage_ancestor_cannot_prepare_update(self):
        self.download()
        original = self.receipt.read_bytes()
        real_is_link = updates._is_link
        for linked in (self.receipt, self.stage / self.archive_name, self.stage_root):
            with self.subTest(linked=linked):
                self.receipt.write_bytes(original)
                with patch.object(updates, "_is_link", side_effect=lambda path: Path(path) == linked or real_is_link(path)):
                    self.assertTrue(self.assert_rejected())

    def test_pending_staging_remains_available_after_retention_age(self):
        self.download()
        marker = self.stage / ".pnut-owned.json"
        record = json.loads(marker.read_text(encoding="utf-8"))
        record["created"] = time.time() - 20 * 86400
        atomic_json(marker, record)
        updates.consume_update_result()
        self.assertTrue(self.stage.is_dir())
        self.assertTrue(self.receipt.is_file())
        self.restore()


class PendingUpdateStartupTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import Mock
        from mnmparse.app import main as entry
        from mnmparse.app import launch_identity

        self.entry = entry
        self.events = []
        self.config_path = Path(tempfile.gettempdir()) / "PNUT pending update profile.json"
        self.app = Mock()
        self.app.exec.return_value = 0
        self.app.bootstrap.side_effect = lambda *args, **kwargs: self.events.append("bootstrap")
        self.app.shutdown.side_effect = lambda: self.events.append("shutdown")
        self.app._on_app_update_status.side_effect = lambda message: self.events.append(("status", message))
        self.coordinator = Mock()
        self.coordinator.acquire.side_effect = lambda: self.events.append("acquire") or True
        config = Mock()
        config.problems.return_value = []
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(entry, "App", return_value=self.app))
        stack.enter_context(patch.object(entry, "setup_logging", return_value=Path("unused.log")))
        stack.enter_context(patch.object(entry, "load_config", return_value=config))
        stack.enter_context(patch.object(entry, "_ensure_std_streams"))
        stack.enter_context(patch.object(entry, "_IMPORT_ERRORS", {}))
        self.coordinator_factory = stack.enter_context(
            patch.object(launch_identity, "InstanceCoordinator", return_value=self.coordinator))
        self.prepare = stack.enter_context(patch.object(updates, "prepare_pending_update"))

    def run_app(self, *extra):
        return self.entry.run(["--console", "--config", str(self.config_path), *extra])

    def test_ready_update_is_prepared_after_instance_lock_and_before_bootstrap(self):
        self.prepare.side_effect = lambda **kwargs: self.events.append("prepare") or (True, "ready")
        self.assertEqual(self.run_app(), 0)
        self.assertEqual(self.events, ["acquire", "prepare", "shutdown"])
        self.prepare.assert_called_once_with(arguments=["--config", str(self.config_path.resolve())])
        self.app.bootstrap.assert_not_called()
        self.app.exec.assert_not_called()

    def test_instance_conflict_skips_update_and_capture_startup(self):
        self.coordinator.acquire.side_effect = lambda: self.events.append("acquire") or False
        self.assertEqual(self.run_app(), 0)
        self.assertEqual(self.events, ["acquire"])
        self.prepare.assert_not_called()
        self.app.bootstrap.assert_not_called()
        self.app.exec.assert_not_called()

    def test_selftest_skips_pending_update_and_instance_coordination(self):
        self.assertEqual(self.run_app("--selftest-seconds", "0"), 0)
        self.coordinator_factory.assert_not_called()
        self.prepare.assert_not_called()
        self.assertEqual(self.app.bootstrap.call_args.kwargs["selftest_seconds"], 0)
        self.assertEqual(self.events, ["bootstrap", "shutdown"])

    def test_unavailable_saved_update_reports_error_and_opens_normal_application(self):
        message = "Could not restore the saved update: checksum mismatch."
        self.prepare.side_effect = lambda **kwargs: self.events.append("prepare") or (False, message)
        self.assertEqual(self.run_app(), 0)
        self.assertEqual(self.events, ["acquire", "prepare", "bootstrap", ("status", message), "shutdown"])
        self.app.exec.assert_called_once()


if __name__ == "__main__":
    unittest.main()
