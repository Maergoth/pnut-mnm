"""Temporary-only proofs for inactive executable copies and post-exit cleanup."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from mnmparse.app import launch_identity as identity


class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "spaces & apostrophe's"
        self.root.mkdir()
        self.canonical = self.root / identity.EXECUTABLE
        self.canonical.write_bytes(b"MZ-current-test-version")
        (self.root / "_internal").mkdir()

    def test_current_runtime_unknown_copies_and_user_files_are_preserved(self):
        current = identity._copy_session(self.canonical)
        old = identity._copy_session(self.canonical)
        # Old session hashes are valid even after the canonical version changes.
        self.canonical.write_bytes(b"MZ-new-test-version")
        unknown = self.root / ("a" * 24 + ".exe")
        unknown.write_bytes(self.canonical.read_bytes())
        personal = self.root / "my application.exe"
        personal.write_bytes(b"personal executable")
        with patch.object(identity.sys, "executable", str(current)):
            identity.cleanup_sessions(self.root)
        self.assertFalse(old.exists())
        self.assertFalse(identity._marker(old).exists())
        self.assertTrue(current.exists())
        self.assertTrue(identity._marker(current).exists())
        self.assertTrue(unknown.exists(), "A matching hex filename/content alone is not ownership")
        self.assertEqual(personal.read_bytes(), b"personal executable")
        self.assertEqual(self.canonical.read_bytes(), b"MZ-new-test-version")

    def test_cleanup_is_bounded_and_later_startup_can_finish(self):
        aliases = [identity._copy_session(self.canonical) for _ in range(3)]
        with patch.object(identity, "_MAX_CLEANUP_FILES", 1):
            identity.cleanup_sessions(self.root)
        self.assertEqual(sum(alias.exists() for alias in aliases), 2)
        identity.cleanup_sessions(self.root)
        self.assertFalse(any(alias.exists() for alias in aliases))

    def test_post_exit_cleanup_is_hidden_encoded_and_specific(self):
        alias = identity._copy_session(self.canonical)
        with patch.object(identity.subprocess, "Popen") as spawn:
            identity._schedule_session_cleanup(alias)
        args = spawn.call_args.args[0]
        self.assertIn("-NoProfile", args)
        self.assertIn("-NonInteractive", args)
        self.assertEqual(spawn.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)
        script = base64.b64decode(args[-1]).decode("utf-16le")
        self.assertIn("WaitForExit(60000)", script)
        self.assertIn(identity._guard_name(self.root), script)
        self.assertIn(str(alias).replace("'", "''"), script)
        self.assertNotIn("-Recurse", script)
        self.assertIn("[IO.File]::OpenRead($exe)", script)
        self.assertIn("$hasher.ComputeHash($stream)", script)

    @unittest.skipUnless(sys.platform == "win32", "Windows launch mutex")
    def test_cleanup_cannot_enter_between_copy_and_createprocess(self):
        copied = threading.Event()
        finish_spawn = threading.Event()
        cleaning = threading.Event()
        errors = []
        paths = []

        class Child:
            def poll(self):
                return None

        def spawn(args, **kwargs):
            paths.append(Path(args[0]))
            copied.set()
            if not finish_spawn.wait(2):
                raise RuntimeError("Test did not release the launcher")
            return Child()

        def launch():
            try:
                identity.launch_if_needed([])
            except Exception as error:
                errors.append(error)

        def clean():
            cleaning.set()
            identity.cleanup_sessions(self.root)

        with patch.object(identity.sys, "frozen", True, create=True), \
                patch.object(identity.sys, "executable", str(self.canonical)), \
                patch.object(identity.subprocess, "Popen", side_effect=spawn):
            launcher = threading.Thread(target=launch)
            launcher.start()
            try:
                self.assertTrue(copied.wait(2))
                cleaner = threading.Thread(target=clean)
                cleaner.start()
                try:
                    self.assertTrue(cleaning.wait(2))
                    time.sleep(.05)
                    self.assertTrue(cleaner.is_alive(), "Cleanup waits on the launch mutex")
                    self.assertTrue(paths[0].exists())
                    self.assertTrue(identity._marker(paths[0]).exists())
                finally:
                    finish_spawn.set()
                    cleaner.join(3)
            finally:
                finish_spawn.set()
                launcher.join(3)
        self.assertFalse(launcher.is_alive())
        self.assertFalse(cleaner.is_alive())
        self.assertEqual(errors, [])


@unittest.skipUnless(sys.platform == "win32", "Windows post-exit helper")
class WindowsRuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        RuntimeCleanupTests.setUp(self)
        self.powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

    def run_helper(self, alias, digest, owner_pid):
        script = identity._cleanup_script(alias, digest, owner_pid)
        process = subprocess.run(
            [str(self.powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(script.encode("utf-16le")).decode("ascii")],
            capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")

    def test_real_hidden_helper_waits_for_exit_then_removes_only_verified_pair(self):
        alias = identity._copy_session(self.canonical)
        unknown = self.root / ("f" * 24 + ".exe")
        unknown.write_bytes(self.canonical.read_bytes())
        owner = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(1)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.run_helper(alias, identity._digest(alias), owner.pid)
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.wait(timeout=5)
        self.assertFalse(alias.exists())
        self.assertFalse(identity._marker(alias).exists())
        self.assertTrue(unknown.exists())
        self.assertTrue(self.canonical.exists())
        self.assertTrue((self.root / "_internal").is_dir())

    def test_real_helper_preserves_modified_executable_or_malformed_marker(self):
        for damaged in ("executable", "marker", "hash", "linked"):
            with self.subTest(damaged=damaged):
                alias = identity._copy_session(self.canonical)
                digest = identity._digest(alias)
                marker = identity._marker(alias)
                if damaged == "executable":
                    alias.write_bytes(b"personal replacement")
                elif damaged == "marker":
                    marker.write_text("not JSON")
                elif damaged == "hash":
                    record = json.loads(marker.read_text())
                    record["sha256"] = "a" * 64
                    marker.write_text(json.dumps(record))
                else:
                    # Ancestor reparse points are checked in the real helper too.
                    external = self.root.parent / "external"
                    external.mkdir(exist_ok=True)
                    link = self.root / "linked"
                    command = "New-Item -ItemType Junction -Path '" + str(link).replace("'", "''") + "' -Value '" + str(external).replace("'", "''") + "' | Out-Null"
                    created = subprocess.run([str(self.powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                             capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(created.returncode, 0, created.stderr)
                    linked_alias = link / alias.name
                    alias.replace(external / alias.name)
                    marker.replace(external / marker.name)
                    alias, marker = linked_alias, identity._marker(linked_alias)
                self.run_helper(alias, digest, 2147483647)
                self.assertTrue(alias.exists())
                self.assertTrue(marker.exists())

    def test_startup_preserves_currently_mapped_image_then_retries_after_exit(self):
        alias = self.root / ("e" * 24 + ".exe")
        source = 'class Probe { static void Main() { System.Threading.Thread.Sleep(10000); } }'
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = "Add-Type -TypeDefinition " + quote(source) + " -OutputAssembly " + quote(alias) + " -OutputType WindowsApplication"
        compiled = subprocess.run([str(self.powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                                  capture_output=True, text=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(compiled.returncode, 0, compiled.stderr)
        marker = identity._marker(alias)
        marker.write_text(json.dumps({"schema": 1, "executable": alias.name, "sha256": identity._digest(alias)}))
        process = subprocess.Popen([str(alias)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertIsNone(process.poll())
            identity.cleanup_sessions(self.root)
            self.assertTrue(alias.exists(), "The running mapped image must be preserved")
            self.assertTrue(marker.exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        identity.cleanup_sessions(self.root)
        self.assertFalse(alias.exists())
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
