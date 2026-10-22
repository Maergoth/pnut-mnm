"""Application updates must preserve local data and reject untrusted packages."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app import app_updates as updates


def release_payload(version="9.8.7"):
    tag = "v" + version
    name = f"PNUT-MnM-{version}-windows.zip"
    return {"tag_name": tag, "draft": False, "prerelease": False,
            "assets": [{"name": asset, "size": size,
                        "browser_download_url": f"https://github.com/Maergoth/pnut-mnm/releases/download/{tag}/{asset}"}
                       for asset, size in ((name, 100), (name + ".sha256", 94))]}


def write_package(path: Path, extras=()):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(updates.EXECUTABLE, b"MZnot-an-executable")
        archive.writestr("_internal/python.dll", b"library")
        archive.writestr("START HERE.txt", b"readme")
        archive.writestr("_internal/build-info.json", json.dumps({"schema": 1, "version": "9.8.7", "commit": "a" * 40,
                                                                  "dirty": False, "built_at": "2026-10-10T12:00:00+00:00"}))
        for name, data in extras:
            if isinstance(name, str) and "\\" in name:
                info = zipfile.ZipInfo("placeholder")
                info.filename = name  # Bypass zipfile's Windows separator normalization.
                name = info
            archive.writestr(name, data)


class ReleaseTests(unittest.TestCase):
    def test_previous_update_version_only_uses_current_successful_update_receipt(self):
        current = "1.0.7"
        valid = {"schema": 1, "token": "a" * 32, "status": "healthy",
                 "version": current, "previous_version": "1.0.5"}
        missing_previous = dict(valid)
        missing_previous.pop("previous_version")
        cases = (
            ("healthy", valid, "1.0.5"),
            ("pending", {**valid, "status": "pending", "previous_version": "1.0.6"}, "1.0.6"),
            ("stale receipt", {**valid, "version": "1.0.6"}, ""),
            ("rolled back", {**valid, "status": "rolled_back"}, ""),
            ("failed", {**valid, "status": "failed"}, ""),
            ("missing previous", missing_previous, ""),
            ("null previous", {**valid, "previous_version": None}, ""),
            ("malformed previous", {**valid, "previous_version": "1.0"}, ""),
            ("nontext previous", {**valid, "previous_version": 12}, ""),
            ("same previous", {**valid, "previous_version": current}, ""),
            ("future previous", {**valid, "previous_version": "1.0.8"}, ""),
            ("invalid receipt schema", {**valid, "schema": 2}, ""),
            ("invalid receipt token", {**valid, "token": "invalid"}, ""),
        )
        with tempfile.TemporaryDirectory() as temp:
            install = Path(temp)
            receipt = install / ".pnut-update.json"
            with patch.object(updates, "installed_directory", return_value=install), \
                    patch.object(updates, "APP_VERSION", current):
                self.assertEqual(updates.previous_update_version(), "")
                for label, record, expected in cases:
                    with self.subTest(case=label):
                        receipt.write_text(json.dumps(record), encoding="utf-8")
                        self.assertEqual(updates.previous_update_version(), expected)
                receipt.write_text("not json", encoding="utf-8")
                self.assertEqual(updates.previous_update_version(), "")
            with patch.object(updates, "installed_directory", side_effect=updates.UpdateError("Source build")):
                self.assertEqual(updates.previous_update_version(), "")

    def test_numeric_versions_and_no_downgrades(self):
        self.assertGreater(updates.version_tuple("v1.10.0"), updates.version_tuple("1.9.9"))
        self.assertIsNone(updates.select_release(release_payload("0.1.1"), "0.1.2"))
        self.assertIsNone(updates.select_release(release_payload("0.1.2"), "0.1.2"))
        self.assertEqual(updates.select_release(release_payload("0.2.0"), "0.1.2").version, "0.2.0")
        for value in ("1.2", "1.2.3-beta", "01.2.3", "v1.2.3;anything", "1.2.3.4", None):
            with self.subTest(value=value), self.assertRaises(updates.UpdateError):
                updates.version_tuple(value)

    def test_only_exact_release_assets_are_accepted(self):
        payload = release_payload()
        payload["assets"][0]["browser_download_url"] = "https://github.com/other/project/releases/download/v9.8.7/update.zip"
        with self.assertRaisesRegex(updates.UpdateError, "unexpected"):
            updates.select_release(payload)
        payload = release_payload()
        payload["assets"].pop()
        with self.assertRaisesRegex(updates.UpdateError, "missing"):
            updates.select_release(payload)
        payload = release_payload()
        payload["assets"].append(payload["assets"][0].copy())
        with self.assertRaises(updates.UpdateError):
            updates.select_release(payload)
        payload = release_payload()
        payload["prerelease"] = True
        with self.assertRaises(updates.UpdateError):
            updates.select_release(payload)

    def test_size_and_redirect_bounds(self):
        payload = release_payload()
        payload["assets"][0]["size"] = updates.MAX_DOWNLOAD + 1
        with self.assertRaises(updates.UpdateError):
            updates.select_release(payload)
        for url in ("http://github.com/download", "https://github.com.evil.test/file",
                    "https://github.com@evil.test/file", "https://github.com:444/file", "file:///C:/download"):
            self.assertFalse(updates._trusted_url(url), url)
            with self.assertRaises(updates.UpdateError):
                updates._Redirects().redirect_request(None, None, 302, "", {}, url)


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archive = self.root / "package.zip"
        self.cancelled = threading.Event()

    def tearDown(self):
        self.temp.cleanup()

    def test_checksum_must_match_data_and_asset_filename(self):
        write_package(self.archive)
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        checksum = f"{digest}  package.zip\n".encode()
        updates.verify_archive(self.archive, checksum, "package.zip")
        for bad in (checksum.replace(b"package.zip", b"other.zip"), b"0" * 64 + b"  package.zip", checksum + b"extra"):
            with self.assertRaises(updates.UpdateError):
                updates.verify_archive(self.archive, bad, "package.zip")

    def test_valid_package_extracts_only_application_files(self):
        write_package(self.archive, (("_internal/package/deep/data.bin", b"data"),))
        destination = self.root / "payload"
        updates.extract_archive(self.archive, destination, self.cancelled)
        self.assertEqual((destination / "_internal/package/deep/data.bin").read_bytes(), b"data")
        self.assertTrue((destination / updates.EXECUTABLE).is_file())

    def test_dangerous_windows_paths_are_rejected_before_extracting(self):
        for index, name in enumerate(("../config.json", "/absolute", "C:/path", "_internal/../config.json",
                                      "_internal\\escape.dll", "_internal/CON.txt", "_internal/foo:stream",
                                      "_internal/file. ", "_internal//empty", "config.json",
                                      "_internal/PYTHON.dll")):
            with self.subTest(name=name):
                write_package(self.archive, ((name, b"bad"),))
                destination = self.root / f"bad-{index}"
                with self.assertRaises(updates.UpdateError):
                    updates.extract_archive(self.archive, destination, self.cancelled)
                self.assertEqual(list(destination.iterdir()), [])

    def test_symbolic_links_and_expansion_limits_are_rejected(self):
        info = zipfile.ZipInfo("_internal/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        write_package(self.archive, ((info, b"../../config.json"),))
        with self.assertRaises(updates.UpdateError):
            updates.extract_archive(self.archive, self.root / "links", self.cancelled)
        write_package(self.archive)
        with patch.object(updates, "MAX_EXPANDED", 4), self.assertRaises(updates.UpdateError):
            updates.extract_archive(self.archive, self.root / "large", self.cancelled)

    def test_cancelled_download_never_stages_a_runnable_package(self):
        write_package(self.archive)
        self.cancelled.set()
        with self.assertRaises(updates.UpdateError):
            updates.extract_archive(self.archive, self.root / "cancelled", self.cancelled)
        self.assertFalse((self.root / "cancelled" / updates.EXECUTABLE).exists())

    def test_download_stage_verifies_all_content_without_execution(self):
        write_package(self.archive)
        data = self.archive.read_bytes()
        payload = release_payload()
        name = payload["assets"][0]["name"]
        digest = hashlib.sha256(data).hexdigest()
        def fetch(url, limit, cancelled, destination=None, progress=None):
            if destination:
                destination.write_bytes(data)
                return b""
            if url.endswith(".sha256"):
                return f"{digest}  {name}\n".encode()
            return json.dumps(payload).encode()
        with patch.object(updates, "_fetch", side_effect=fetch), patch.object(updates, "_stage_root", return_value=self.root / "updates"), patch.object(updates.subprocess, "Popen") as process:
            version, package = updates.download_update(self.cancelled, lambda message: None)
        self.assertEqual(version, "9.8.7")
        self.assertTrue((package / updates.EXECUTABLE).is_file())
        process.assert_not_called()


class ControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_installed_directory_accepts_canonical_and_validated_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            canonical = root / updates.EXECUTABLE
            canonical.write_bytes(b"application")
            (root / "_internal").mkdir()
            session = root / ("a" * 24 + ".exe")
            session.write_bytes(b"application")
            with patch.object(updates.sys, "frozen", True, create=True), \
                 patch.object(updates.sys, "platform", "win32"):
                with patch.object(updates.sys, "executable", str(canonical)):
                    self.assertEqual(updates.installed_directory(), root)
                with patch.object(updates.sys, "executable", str(session)), \
                     patch.object(updates, "is_session_executable", return_value=True) as validate:
                    self.assertEqual(updates.installed_directory(), root)
                    validate.assert_called_once_with(session)

    def test_unowned_or_incomplete_session_cannot_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            canonical = root / updates.EXECUTABLE
            canonical.write_bytes(b"application")
            (root / "_internal").mkdir()
            session = root / ("a" * 24 + ".exe")
            session.write_bytes(b"foreign application")
            with patch.object(updates.sys, "frozen", True, create=True), \
                 patch.object(updates.sys, "platform", "win32"), \
                 patch.object(updates.sys, "executable", str(session)):
                with self.assertRaises(updates.UpdateError):
                    updates.installed_directory()
                canonical.unlink()
                with patch.object(updates, "is_session_executable", return_value=True), \
                     self.assertRaises(updates.UpdateError):
                    updates.installed_directory()

    def test_source_mode_reports_requirement_without_network(self):
        controller = updates.AppUpdateController()
        messages = []
        controller.finished.connect(messages.append)
        with patch.object(updates.sys, "frozen", False, create=True), patch.object(updates, "download_update") as download:
            self.assertFalse(controller.start())
        self.assertIn("installed Windows build", messages[0])
        download.assert_not_called()

    def test_automatic_restart_suppresses_only_matching_confirmed_failed_release(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        valid = {"schema": 1, "token": "a" * 32, "status": "rolled_back", "version": "9.8.7"}
        cases = (
            ("rolled back", valid, False),
            ("failed", {**valid, "status": "failed"}, False),
            ("already reported", {**valid, "reported": True}, False),
            ("v-prefixed receipt", {**valid, "version": "v9.8.7"}, False),
            ("different failed target", {**valid, "version": "9.8.6"}, True),
            ("healthy", {**valid, "status": "healthy"}, True),
            ("pending", {**valid, "status": "pending"}, True),
            ("invalid schema", {**valid, "schema": 2}, True),
            ("invalid token", {**valid, "token": "invalid"}, True),
            ("missing version", {key: value for key, value in valid.items() if key != "version"}, True),
            ("malformed version", {**valid, "version": "9.8"}, True),
            ("nontext version", {**valid, "version": 9}, True),
        )
        with tempfile.TemporaryDirectory() as temp:
            installation = Path(temp)
            receipt = installation / ".pnut-update.json"
            with patch.object(updates, "installed_directory", return_value=installation):
                self.assertTrue(controller.automatic_restart_allowed("9.8.7"))
                for label, record, expected in cases:
                    with self.subTest(case=label):
                        receipt.write_text(json.dumps(record), encoding="utf-8")
                        self.assertEqual(controller.automatic_restart_allowed("9.8.7"), expected)
                receipt.write_text(json.dumps(valid), encoding="utf-8")
                self.assertFalse(controller.automatic_restart_allowed("v9.8.7"))
                receipt.write_text("not json", encoding="utf-8")
                self.assertTrue(controller.automatic_restart_allowed("9.8.7"))
                for unsupported in ("9.8", "arbitrary", None):
                    self.assertFalse(controller.automatic_restart_allowed(unsupported))
            with patch.object(updates, "installed_directory", side_effect=updates.UpdateError("Source build")):
                self.assertFalse(controller.automatic_restart_allowed("9.8.7"))

    def test_concurrent_starts_and_late_shutdown_results_are_ignored(self):
        controller = updates.AppUpdateController()
        ready = []
        controller.ready.connect(ready.append)
        with patch.object(updates, "installed_directory", return_value=Path.cwd()), patch.object(updates.threading, "Thread") as thread:
            self.assertTrue(controller.start())
            self.assertFalse(controller.start())
            self.assertTrue(controller.is_running)
            worker = controller._worker
            controller.shutdown()
            self.assertTrue(worker.cancelled.is_set())
            controller._on_completed(("9.8.7", Path("package")), "ready")
            self.assertFalse(controller.start())
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(ready, [])
        self.assertIsNone(controller.ready_package)

    def test_worker_results_are_delivered_on_gui_thread_and_polling_stops_when_idle(self):
        from PySide6.QtCore import QThread
        from PySide6.QtTest import QTest

        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        events, download_threads = [], []
        controller.progress.connect(lambda message: events.append(("progress", QThread.currentThread())))
        controller.finished.connect(lambda message: events.append(("finished", QThread.currentThread())))
        controller.ready.connect(lambda version: events.append(("ready", QThread.currentThread())))
        self.assertFalse(controller._poll_timer.isActive())

        def download(cancelled, progress, **kwargs):
            download_threads.append(threading.get_ident())
            progress("Downloading sample update")
            return "9.8.7", Path("sample-package")

        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "restore_pending", return_value=False), \
             patch.object(updates, "download_update", side_effect=download):
            self.assertTrue(controller.start())
            self.assertTrue(controller._poll_timer.isActive())
            deadline = time.monotonic() + 2
            while controller.is_running and time.monotonic() < deadline:
                QTest.qWait(5)
        self.assertFalse(controller.is_running)
        self.assertFalse(controller._poll_timer.isActive())
        self.assertEqual([kind for kind, _ in events], ["progress", "finished", "ready"])
        self.assertTrue(all(thread == self.app.thread() for _, thread in events))
        self.assertNotEqual(download_threads, [threading.get_ident()])
        self.assertEqual(controller.ready_version, "9.8.7")

    def test_shutdown_and_qt_deletion_during_download_suppress_queued_and_late_results(self):
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtTest import QTest

        controller = updates.AppUpdateController()
        started, released = threading.Event(), threading.Event()
        threads, messages = [], []
        controller.progress.connect(lambda message: messages.append("progress"))
        controller.finished.connect(lambda message: messages.append("finished"))
        controller.ready.connect(lambda version: messages.append("ready"))

        def download(cancelled, progress, **kwargs):
            threads.append(threading.current_thread())
            progress("Queued before shutdown")
            started.set()
            released.wait(2)
            progress("Late progress after shutdown")
            return "9.8.7", Path("sample-package")

        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "restore_pending", return_value=False), \
             patch.object(updates, "download_update", side_effect=download):
            try:
                self.assertTrue(controller.start())
                self.assertTrue(started.wait(2))
                worker = controller._worker
                controller.shutdown()
                self.assertTrue(worker.cancelled.is_set())
                self.assertFalse(controller._poll_timer.isActive())
                self.assertIsNone(controller._worker)
                controller.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            finally:
                released.set()
                for thread in threads:
                    thread.join(2)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            QTest.qWait(30)
        self.assertEqual(messages, [])

    def test_completed_callback_can_start_another_check_without_stopping_its_timer(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        messages = []
        controller.finished.connect(messages.append)
        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "restore_pending", return_value=False), \
             patch.object(updates.threading, "Thread"):
            self.assertTrue(controller.start())
            first = controller._worker
            first._post("completed", None, "First check complete")
            controller.finished.connect(lambda message: controller.start() if message == "First check complete" else None)
            controller._drain()
            self.assertIsNot(controller._worker, first)
            self.assertTrue(controller.is_running)
            self.assertTrue(controller._poll_timer.isActive())
            controller._worker._post("completed", None, "Second check complete")
            controller._drain()
        self.assertEqual(messages, ["First check complete", "Second check complete"])
        self.assertFalse(controller.is_running)
        self.assertFalse(controller._poll_timer.isActive())

    def test_finished_callback_shutdown_suppresses_ready_from_the_same_completion(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        ready = []
        controller.finished.connect(lambda message: controller.shutdown())
        controller.ready.connect(ready.append)
        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "restore_pending", return_value=False), \
             patch.object(updates.threading, "Thread"):
            self.assertTrue(controller.start())
            controller._worker._post("completed", ("9.8.7", Path("sample-package")), "Update ready")
            controller._drain()
        self.assertEqual(ready, [])
        self.assertFalse(controller._poll_timer.isActive())

    def test_progress_callback_shutdown_discards_completion_waiting_in_the_queue(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        messages = []
        controller.progress.connect(lambda message: controller.shutdown())
        controller.finished.connect(lambda message: messages.append("finished"))
        controller.ready.connect(lambda version: messages.append("ready"))
        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "restore_pending", return_value=False), \
             patch.object(updates.threading, "Thread"):
            self.assertTrue(controller.start())
            controller._worker._post("progress", "Downloading")
            controller._worker._post("completed", ("9.8.7", Path("sample-package")), "Update ready")
            controller._drain()
        self.assertEqual(messages, [])
        self.assertFalse(controller._poll_timer.isActive())

    def test_already_ready_notification_also_rechecks_shutdown_after_finished(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        controller._on_completed(("9.8.7", Path("sample-package")), "Update ready")
        ready = []
        controller.finished.connect(lambda message: controller.shutdown())
        controller.ready.connect(ready.append)
        with patch.object(updates, "installed_directory", return_value=Path.cwd()), \
             patch.object(controller, "automatic_restart_allowed", return_value=True):
            self.assertFalse(controller.start())
        self.assertEqual(ready, [])

    def test_deliberate_downgrade_defers_reapplying_the_version_just_replaced(self):
        controller = updates.AppUpdateController()
        self.addCleanup(controller.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            installation = Path(temp)
            receipt = installation / ".pnut-update.json"
            with patch.object(updates, "installed_directory", return_value=installation):
                for status in ("healthy", "pending"):
                    with self.subTest(status=status):
                        # pending covers the short interval after acknowledgement
                        # before the helper records the restored app as healthy.
                        receipt.write_text(json.dumps({"schema": 1, "token": "b" * 32, "status": status,
                            "version": "9.8.6", "previous_version": "9.8.7", "reported": True}))
                        self.assertFalse(controller.automatic_restart_allowed("9.8.7"))
                        self.assertFalse(controller.automatic_restart_allowed("v9.8.7"))
                        self.assertTrue(controller.automatic_restart_allowed("9.8.8"))
                receipt.write_text(json.dumps({"schema": 1, "token": "b" * 32, "status": "healthy",
                    "version": "9.8.7", "previous_version": "9.8.6"}))
                self.assertTrue(controller.automatic_restart_allowed("9.8.7"))

    def test_helper_is_prepared_without_changing_install_and_starts_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "App"
            package = root / "updates" / "9.8.7-download" / "package"
            install.mkdir()
            package.mkdir(parents=True)
            (package / "_internal").mkdir()
            (package / updates.EXECUTABLE).write_bytes(b"MZnew")
            (package / "_internal" / "build-info.json").write_text(json.dumps({"schema": 1, "version": "9.8.7",
                "commit": "a" * 40, "dirty": False, "built_at": "2026-10-10T12:00:00+00:00"}))
            (install / "config.json").write_text("personal")
            archive = package.parent / "PNUT-MnM-9.8.7-windows.zip"
            write_package(archive)
            (package.parent / "release.sha256").write_text(
                hashlib.sha256(archive.read_bytes()).hexdigest() + "  " + archive.name + "\n")
            controller = updates.AppUpdateController()
            controller._on_completed(("9.8.7", package), "ready")
            with patch.object(updates, "installed_directory", return_value=install), patch.object(updates, "_stage_root", return_value=root / "updates"), patch.object(updates.subprocess, "Popen") as process:
                updates.persist_pending_update(install, package.parent, updates.select_release(release_payload()))
                self.assertTrue(controller.prepare_restart())
                self.assertFalse(controller.prepare_restart())
            process.assert_called_once()
            self.assertIn("-NonInteractive", process.call_args.args[0])
            self.assertEqual(process.call_args.kwargs["creationflags"], updates.subprocess.CREATE_NO_WINDOW)
            self.assertEqual((install / "config.json").read_text(), "personal")
            self.assertFalse((install / updates.EXECUTABLE).exists())
            self.assertTrue((package.parent / "install.ps1").is_file())

    def test_helper_uses_literal_paths_and_has_bounded_moves_no_deletes(self):
        root = Path(tempfile.gettempdir()).resolve() / "M&M's $directory"
        script = updates.make_restart_script(root / "App", root / "staging", 123, "a" * 32)
        self.assertIn("M&M''s $directory", script)
        self.assertIn("-LiteralPath $source", script)
        self.assertIn("Assert-PlainPath $source $install", script)
        self.assertIn("WaitForExit(120000)", script)
        self.assertIn("$movedOld", script)
        self.assertNotIn("Remove-Item", script)
        self.assertNotIn("config.json", script)
        self.assertNotIn("triggers.json", script)
        # Only the updater helper stays hidden; both successful and rollback app
        # launches must display the interactive window.
        launches = [line for line in script.splitlines() if "Start-Process" in line]
        self.assertEqual(len(launches), 2)
        for launch in launches:
            self.assertIn("-WindowStyle Normal", launch)
            self.assertNotIn("-WindowStyle Hidden", launch)
        with self.assertRaises(updates.UpdateError):
            updates.make_restart_script(Path("relative"), root, 123, "a" * 32)


if __name__ == "__main__":
    unittest.main()
