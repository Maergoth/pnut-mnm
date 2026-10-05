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
        for name, data in extras:
            if isinstance(name, str) and "\\" in name:
                info = zipfile.ZipInfo("placeholder")
                info.filename = name  # Bypass zipfile's Windows separator normalization.
                name = info
            archive.writestr(name, data)


class ReleaseTests(unittest.TestCase):
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

    def test_source_mode_reports_requirement_without_network(self):
        controller = updates.AppUpdateController()
        messages = []
        controller.finished.connect(messages.append)
        with patch.object(updates.sys, "frozen", False, create=True), patch.object(updates, "download_update") as download:
            self.assertFalse(controller.start())
        self.assertIn("installed Windows build", messages[0])
        download.assert_not_called()

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

    def test_helper_is_prepared_without_changing_install_and_starts_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install = root / "App"
            package = root / "updates" / "download" / "package"
            install.mkdir()
            package.mkdir(parents=True)
            (package / "_internal").mkdir()
            (package / updates.EXECUTABLE).write_bytes(b"MZnew")
            (install / "config.json").write_text("personal")
            controller = updates.AppUpdateController()
            controller._on_completed(("9.8.7", package), "ready")
            with patch.object(updates, "installed_directory", return_value=install), patch.object(updates, "_stage_root", return_value=root / "updates"), patch.object(updates.subprocess, "Popen") as process:
                self.assertTrue(controller.prepare_restart())
                self.assertFalse(controller.prepare_restart())
            process.assert_called_once()
            self.assertIn("-NonInteractive", process.call_args.args[0])
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
        with self.assertRaises(updates.UpdateError):
            updates.make_restart_script(Path("relative"), root, 123, "a" * 32)


if __name__ == "__main__":
    unittest.main()
