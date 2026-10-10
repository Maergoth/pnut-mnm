"""Executable handoff, update recognition, and bounded cleanup regressions."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from mnmparse.app import launch_identity as identity


class LaunchIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.canonical = self.root / identity.EXECUTABLE
        self.canonical.write_bytes(b"MZ-test-application")
        (self.root / "_internal").mkdir()
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop(identity._HANDOFF, None)

    def frozen(self, executable=None):
        stack = ExitStack()
        stack.enter_context(patch.object(identity.sys, "frozen", True, create=True))
        stack.enter_context(patch.object(identity.sys, "platform", "win32"))
        stack.enter_context(patch.object(identity.sys, "executable", str(executable or self.canonical)))
        return stack

    def child(self, exit_code=0):
        child = Mock()
        child.wait.return_value = exit_code
        child.poll.return_value = exit_code
        return child

    def test_source_runs_do_not_copy_or_spawn(self):
        with patch.object(identity.sys, "frozen", False, create=True), patch.object(identity, "_copy_session") as copy, patch.object(identity.subprocess, "Popen") as spawn:
            self.assertIsNone(identity.launch_if_needed([]))
        copy.assert_not_called()
        spawn.assert_not_called()

    def test_non_windows_frozen_runs_do_not_spawn(self):
        with self.frozen(), patch.object(identity.sys, "platform", "linux"), patch.object(identity.subprocess, "Popen") as spawn:
            self.assertIsNone(identity.launch_if_needed([]))
        spawn.assert_not_called()

    def test_each_launch_creates_distinct_real_copy_and_preserves_args_and_cwd(self):
        args = ["--config", "relative folder/config.json", "--verbose"]
        cwd = Path.cwd()
        with self.frozen(), patch.object(identity.subprocess, "Popen", return_value=self.child()) as spawn:
            self.assertEqual(identity.launch_if_needed(args), 0)
            # A real running image cannot be removed on Windows. Keep the first
            # mocked child protected while proving a second launch has a new name.
            first = Path(spawn.call_args.args[0][0])
            remove = identity._remove_session
            with patch.object(identity, "_remove_session", side_effect=lambda path: False if path == first else remove(path)):
                self.assertEqual(identity.launch_if_needed(args), 0)
        paths = [Path(call.args[0][0]) for call in spawn.call_args_list]
        self.assertNotEqual(paths[0], paths[1])
        for path, call in zip(paths, spawn.call_args_list):
            self.assertRegex(path.name, r"^[0-9a-f]{24}\.exe$")
            self.assertEqual(path.parent, self.root)
            self.assertEqual(path.read_bytes(), self.canonical.read_bytes())
            self.assertFalse(path.samefile(self.canonical))
            self.assertTrue(identity.is_session_executable(path))
            self.assertEqual(call.args[0][1:], args)
            self.assertNotIn("cwd", call.kwargs)
            self.assertEqual(call.kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertEqual(call.kwargs["env"][identity._HANDOFF], str(path))
            self.assertEqual(call.kwargs["startupinfo"].wShowWindow, 1)
            self.assertTrue(call.kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(Path.cwd(), cwd)
        self.assertNotIn(identity._HANDOFF, os.environ)

    def test_child_consumes_handoff_and_does_not_relaunch(self):
        alias = identity._copy_session(self.canonical)
        os.environ[identity._HANDOFF] = str(alias)
        with self.frozen(alias), patch.object(identity.subprocess, "Popen") as spawn, patch.object(identity.atexit, "register") as register:
            self.assertIsNone(identity.launch_if_needed([]))
        spawn.assert_not_called()
        self.assertNotIn(identity._HANDOFF, os.environ)
        self.assertTrue(identity.is_session_executable(alias))
        register.assert_called_once_with(identity._schedule_session_cleanup, alias)

    def test_direct_alias_launch_cannot_reuse_old_identity(self):
        alias = identity._copy_session(self.canonical)
        with self.frozen(alias):
            with self.assertRaisesRegex(identity.LaunchError, "Start the application using"):
                identity.launch_if_needed([])

    def test_diagnostic_child_does_not_schedule_a_competing_exit_helper(self):
        alias = identity._copy_session(self.canonical)
        os.environ[identity._HANDOFF] = str(alias)
        with self.frozen(alias), patch.object(identity.atexit, "register") as register:
            self.assertIsNone(identity.launch_if_needed(["--smoke-test", "--report", "report.json"]))
        register.assert_not_called()

    def test_handoff_for_another_alias_is_rejected_and_consumed(self):
        alias = identity._copy_session(self.canonical)
        os.environ[identity._HANDOFF] = str(self.root / ("f" * 24 + ".exe"))
        with self.frozen(alias):
            with self.assertRaises(identity.LaunchError):
                identity.launch_if_needed([])
        self.assertNotIn(identity._HANDOFF, os.environ)

    def test_old_alias_cannot_run_against_updated_support_files(self):
        alias = identity._copy_session(self.canonical)
        self.canonical.write_bytes(b"MZ-new-version")
        os.environ[identity._HANDOFF] = str(alias)
        with self.frozen(alias):
            with self.assertRaises(identity.LaunchError):
                identity.launch_if_needed([])
        # Still recognizable as our old copy for bounded cleanup after updates.
        self.assertTrue(identity.is_session_executable(alias))

    def test_alias_validation_rejects_foreign_modified_and_escaping_records(self):
        self.assertFalse(identity.is_session_executable(self.canonical))
        alias = identity._copy_session(self.canonical)
        original = alias.read_bytes()
        alias.write_bytes(b"modified")
        self.assertFalse(identity.is_session_executable(alias))
        alias.write_bytes(original)
        marker = identity._marker(alias)
        record = json.loads(marker.read_text())
        record["executable"] = "../outside.exe"
        marker.write_text(json.dumps(record))
        self.assertFalse(identity.is_session_executable(alias))
        marker.write_text("not json")
        self.assertFalse(identity.is_session_executable(alias))

    def test_alias_validation_refuses_linked_files_and_directories(self):
        alias = identity._copy_session(self.canonical)
        for linked in (alias, identity._marker(alias), self.root, self.root.parent):
            with self.subTest(linked=linked), patch.object(identity, "_linked", side_effect=lambda p: p == linked):
                self.assertFalse(identity.is_session_executable(alias))

    def test_packaged_smoke_waits_for_child_and_removes_its_owned_files(self):
        child = self.child(exit_code=7)
        with self.frozen(), patch.object(identity.subprocess, "Popen", return_value=child) as spawn:
            self.assertEqual(identity.launch_if_needed(["--smoke-test", "--report", "report.json"], wait=True), 7)
        child.wait.assert_called_once_with(timeout=25)
        alias = Path(spawn.call_args.args[0][0])
        self.assertFalse(alias.exists())
        self.assertFalse(identity._marker(alias).exists())
        self.assertEqual({p.name for p in self.root.iterdir()}, {identity.EXECUTABLE, "_internal"})

    def test_diagnostic_timeout_kills_child_before_owned_cleanup(self):
        child = self.child()
        child.wait.side_effect = [subprocess.TimeoutExpired("child", 25), 1]
        with self.frozen(), patch.object(identity.subprocess, "Popen", return_value=child):
            with self.assertRaisesRegex(identity.LaunchError, "timed out"):
                identity.launch_if_needed(["--smoke-test"], wait=True)
        child.kill.assert_called_once()
        self.assertEqual(len(list(self.root.glob("*.exe"))), 1)

    def test_failed_child_termination_leaves_owned_files_for_later_cleanup(self):
        child = self.child()
        child.wait.side_effect = subprocess.TimeoutExpired("child", 25)
        child.kill.side_effect = PermissionError("locked")
        child.poll.return_value = None
        with self.frozen(), patch.object(identity.subprocess, "Popen", return_value=child):
            with self.assertRaises(identity.LaunchError):
                identity.launch_if_needed(["--smoke-test"], wait=True)
        aliases = [p for p in self.root.glob("*.exe") if p != self.canonical]
        self.assertEqual(len(aliases), 1)
        self.assertTrue(identity.is_session_executable(aliases[0]))

    def test_spawn_failure_is_explicit_and_removes_only_new_copy(self):
        unrelated = self.root / ("a" * 24 + ".exe")
        unrelated.write_bytes(b"unrelated")
        with self.frozen(), patch.object(identity.subprocess, "Popen", side_effect=OSError("failed spawn")):
            with self.assertRaisesRegex(identity.LaunchError, "failed spawn"):
                identity.launch_if_needed([])
        self.assertEqual(unrelated.read_bytes(), b"unrelated")
        self.assertEqual(set(self.root.glob("*.exe")), {self.canonical, unrelated})

    def test_copy_failure_does_not_leave_partial_executable(self):
        with self.frozen(), patch.object(identity.shutil, "copyfileobj", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(identity.LaunchError, "disk full"):
                identity.launch_if_needed([])
        self.assertEqual(list(self.root.glob("*.exe")), [self.canonical])

    def test_existing_filename_collision_is_preserved(self):
        first = "a" * 24
        second = "b" * 24
        collision = self.root / f"{first}.exe"
        collision.write_bytes(b"unrelated")
        with patch.object(identity.secrets, "token_hex", side_effect=[first, second]):
            result = identity._copy_session(self.canonical)
        self.assertEqual(result.stem, second)
        self.assertEqual(collision.read_bytes(), b"unrelated")

    def test_cleanup_removes_owned_same_day_copies_even_after_update(self):
        old = identity._copy_session(self.canonical)
        fresh = identity._copy_session(self.canonical)
        unrelated = self.root / ("c" * 24 + ".exe")
        unrelated.write_bytes(b"personal file")
        now = time.time()
        for path in (old, identity._marker(old), unrelated):
            os.utime(path, (now - 172800, now - 172800))
        self.canonical.write_bytes(b"MZ-updated-version")
        identity.cleanup_sessions(self.root, now=now)
        self.assertFalse(old.exists())
        self.assertFalse(identity._marker(old).exists())
        self.assertFalse(fresh.exists(), "Inactive copies need no one-day delay")
        self.assertTrue(unrelated.exists())

    def test_cleanup_keeps_locked_copy_and_ownership_record(self):
        alias = identity._copy_session(self.canonical)
        now = time.time()
        for path in (alias, identity._marker(alias)):
            os.utime(path, (now - 172800, now - 172800))
        unlink = Path.unlink
        def locked(path, *args, **kwargs):
            if path == alias:
                raise PermissionError("still running")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", locked):
            identity.cleanup_sessions(self.root, now=now)
        self.assertTrue(identity.is_session_executable(alias))

    def test_cleanup_refuses_modified_copy_and_linked_directory(self):
        alias = identity._copy_session(self.canonical)
        now = time.time()
        for path in (alias, identity._marker(alias)):
            os.utime(path, (now - 172800, now - 172800))
        with patch.object(identity, "_linked", side_effect=lambda path: path == self.root):
            identity.cleanup_sessions(self.root, now=now)
        self.assertTrue(alias.exists())
        alias.write_bytes(b"unrelated replacement")
        os.utime(alias, (now - 172800, now - 172800))
        identity.cleanup_sessions(self.root, now=now)
        self.assertTrue(alias.exists())
        self.assertTrue(identity._marker(alias).exists())

    def test_missing_support_files_fail_before_spawn(self):
        (self.root / "_internal").rmdir()
        with self.frozen(), patch.object(identity.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(identity.LaunchError, "application files"):
                identity.launch_if_needed([])
        spawn.assert_not_called()

    def test_entry_point_reports_launch_failure_without_qt(self):
        from mnmparse.app.__main__ import main
        report = self.root / "diagnostics" / "smoke.json"
        with patch.object(identity, "launch_if_needed", side_effect=identity.LaunchError("not writable")):
            self.assertEqual(main(["--smoke-test", "--report", str(report)]), 1)
        result = json.loads(report.read_text())
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "launcher_error")
        self.assertIn("not writable", result["error"])


if __name__ == "__main__":
    unittest.main()
