"""Native handlers and logging setup are exercised only in disposable processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CrashDiagnosticsTests(unittest.TestCase):
    def run_child(self, source: str, directory: Path, *, expected_code: int = 0) -> subprocess.CompletedProcess:
        environment = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1")
        environment.pop("PYTHONFAULTHANDLER", None)
        # Windows CI can return a TEMP path containing an 8.3 alias. The public
        # initializer deliberately returns a resolved destination, so compare
        # it with the same canonical fixture directory inside the child.
        child = subprocess.run([sys.executable, "-B", "-c", textwrap.dedent(source), str(directory.resolve())],
                               cwd=ROOT, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, expected_code, child.stdout + "\n" + child.stderr)
        return child

    def test_real_thread_tracebacks_use_retained_descriptor_without_crashing(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler
                from pathlib import Path
                import sys, threading
                from mnmparse.app import crash_diagnostics as diagnostic
                path = diagnostic.enable_crash_diagnostics(Path(sys.argv[1]), version="fixture-version")
                assert path == Path(sys.argv[1]) / "native-crash.log"
                assert faulthandler.is_enabled()
                ready, stop = threading.Event(), threading.Event()
                def diagnostic_worker():
                    ready.set()
                    stop.wait(5)
                worker = threading.Thread(target=diagnostic_worker, name="synthetic-diagnostic-worker")
                worker.start()
                assert ready.wait(3)
                try:
                    faulthandler.dump_traceback(file=diagnostic._fd, all_threads=True)
                finally:
                    stop.set()
                    worker.join(3)
                assert not worker.is_alive()
            """, directory)
            trace = (directory / "native-crash.log").read_text(encoding="utf-8")
            self.assertIn("fixture-version", trace)
            self.assertIn("diagnostic_worker", trace)
            self.assertIn("Current thread", trace)
            self.assertIn("Python ", trace)

    def test_repeated_setup_logging_and_app_log_rotation_leave_native_destination_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, logging, logging.handlers, os, sys
                from pathlib import Path
                from mnmparse.app import main
                from mnmparse.app import crash_diagnostics as diagnostic
                directory = Path(sys.argv[1])
                main.setup_logging(False, directory)
                descriptor = diagnostic._fd
                for _ in range(3):
                    main.setup_logging(True, directory)
                    assert diagnostic._fd == descriptor
                handler = next(item for item in logging.getLogger().handlers
                               if isinstance(item, logging.handlers.RotatingFileHandler))
                logging.getLogger("mnmparse").warning("ordinary rotating application message")
                handler.doRollover()
                os.fstat(descriptor)
                faulthandler.dump_traceback(file=descriptor, all_threads=True)
                assert diagnostic.enable_crash_diagnostics(directory / "another-profile") == directory / "native-crash.log"
                assert not (directory / "another-profile").exists()
            """, directory)
            native = (directory / "native-crash.log").read_text(encoding="utf-8")
            self.assertEqual(native.count("=== PNUT native crash diagnostics"), 1)
            self.assertIn("Current thread", native)
            self.assertNotIn("ordinary rotating application message", native)
            logs = list(directory.glob("app.log*"))
            self.assertTrue(logs)
            for path in logs:
                self.assertNotIn("Current thread", path.read_text(encoding="utf-8"))

    def test_rotation_happens_only_before_first_open_and_keeps_one_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original = b"previous synthetic native evidence\n" * 20
            (directory / "native-crash.log").write_bytes(original)
            (directory / "native-crash.log.1").write_bytes(b"older evidence")
            self.run_child("""
                import os, sys
                from pathlib import Path
                from mnmparse.app import crash_diagnostics as diagnostic
                diagnostic.NATIVE_LOG_BYTES = 64
                directory = Path(sys.argv[1])
                path = diagnostic.enable_crash_diagnostics(directory)
                descriptor = diagnostic._fd
                backup = path.with_name(path.name + ".1").read_bytes()
                os.write(descriptor, b"active evidence\\n" * 20)
                assert diagnostic.enable_crash_diagnostics(directory) == path
                assert diagnostic._fd == descriptor
                assert path.with_name(path.name + ".1").read_bytes() == backup
            """, directory)
            self.assertEqual((directory / "native-crash.log.1").read_bytes(), original)
            self.assertIn(b"active evidence", (directory / "native-crash.log").read_bytes())
            self.assertEqual(len(list(directory.glob("native-crash.log*"))), 2)

    def test_history_remains_bounded_across_normal_process_launches(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for _ in range(4):
                self.run_child("""
                    from pathlib import Path
                    import sys
                    from mnmparse.app import crash_diagnostics as diagnostic
                    diagnostic.NATIVE_LOG_BYTES = 64
                    assert diagnostic.enable_crash_diagnostics(Path(sys.argv[1])) is not None
                """, directory)
            files = list(directory.glob("native-crash.log*"))
            self.assertEqual(len(files), 2)
            self.assertTrue(all(path.stat().st_size < 512 for path in files))

    def test_unavailable_directory_fails_open_and_can_retry_elsewhere(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, sys
                from pathlib import Path
                from mnmparse.app import crash_diagnostics as diagnostic
                directory = Path(sys.argv[1])
                blocked = directory / "not-a-directory"
                blocked.write_text("synthetic")
                before = faulthandler.is_enabled()
                assert diagnostic.enable_crash_diagnostics(blocked) is None
                assert diagnostic._fd is None
                assert faulthandler.is_enabled() == before
                assert diagnostic.enable_crash_diagnostics(directory / "working") is not None
            """, directory)

    def test_normal_logging_setup_still_completes_when_its_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_child("""
                from pathlib import Path
                import sys
                from mnmparse.app import main
                from mnmparse.app import crash_diagnostics as diagnostic
                blocked = Path(sys.argv[1]) / "not-a-directory"
                blocked.write_text("synthetic")
                assert main.setup_logging(False, blocked) is None
                assert diagnostic._fd is None
            """, Path(temporary))

    def test_header_write_failure_closes_only_the_not_installed_descriptor(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, os, sys
                from pathlib import Path
                from unittest.mock import patch
                from mnmparse.app import crash_diagnostics as diagnostic
                closed = []
                close = os.close
                def capture_close(descriptor):
                    closed.append(descriptor)
                    close(descriptor)
                with patch.object(diagnostic.os, "write", side_effect=OSError("synthetic full disk")), \\
                     patch.object(diagnostic.os, "close", side_effect=capture_close):
                    assert diagnostic.enable_crash_diagnostics(Path(sys.argv[1])) is None
                assert diagnostic._fd is None and len(closed) == 1
                try:
                    os.fstat(closed[0])
                except OSError:
                    pass
                else:
                    raise AssertionError("uninstalled descriptor leaked")
                assert not faulthandler.is_enabled()
                assert diagnostic.enable_crash_diagnostics(Path(sys.argv[1])) is not None
            """, directory)

    def test_enable_failure_retains_destination_for_safe_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, os, sys
                from pathlib import Path
                from unittest.mock import patch
                from mnmparse.app import crash_diagnostics as diagnostic
                with patch.object(diagnostic.faulthandler, "enable", side_effect=RuntimeError("unavailable")) as enable:
                    assert diagnostic.enable_crash_diagnostics(Path(sys.argv[1])) is None
                    descriptor = diagnostic._fd
                    os.fstat(descriptor)
                    enable.assert_called_once_with(file=descriptor, all_threads=True, c_stack=True)
                assert diagnostic.enable_crash_diagnostics(Path(sys.argv[1])) is not None
                assert diagnostic._fd == descriptor and faulthandler.is_enabled()
            """, directory)

    def test_real_worker_c_stack_probe_records_frames_or_explicit_platform_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, os, sys, threading
                from pathlib import Path
                from mnmparse.app import crash_diagnostics as diagnostic
                diagnostic.enable_crash_diagnostics(Path(sys.argv[1]))
                def native_stack_probe_worker():
                    os.write(diagnostic._fd, b"Safe worker C-stack probe\\n")
                    faulthandler.dump_c_stack(file=diagnostic._fd)
                    faulthandler.dump_traceback(file=diagnostic._fd, all_threads=True)
                worker = threading.Thread(target=native_stack_probe_worker)
                worker.start()
                worker.join(5)
                assert not worker.is_alive()
            """, directory)
            trace = (directory / "native-crash.log").read_text(encoding="utf-8")
            self.assertIn("Safe worker C-stack probe", trace)
            self.assertIn("native_stack_probe_worker", trace)
            self.assertIn("C stack", trace)
            self.assertTrue("Binary file" in trace or "cannot get C stack" in trace
                            or "not supported" in trace, trace)

    def test_early_source_entry_captures_qt_import_failure_before_app_logging(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            child = self.run_child("""
                import importlib.abc, runpy, sys
                from pathlib import Path
                from mnmparse.app import crash_diagnostics as diagnostic
                directory = Path(sys.argv[1])
                diagnostic.__file__ = str(directory / "mnmparse/app/crash_diagnostics.py")
                class BrokenQt(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "PySide6.QtCore":
                            assert diagnostic._enabled
                            raise ImportError("synthetic early Qt DLL failure")
                sys.meta_path.insert(0, BrokenQt())
                sys.argv = ["PNUT"]
                runpy.run_module("mnmparse.app", run_name="__main__")
            """, directory, expected_code=1)
            trace = (directory / "logs/native-crash.log").read_text(encoding="utf-8")
            self.assertIn("Uncaught exception", trace)
            self.assertIn("ImportError: synthetic early Qt DLL failure", trace)
            self.assertIn("Traceback", trace)
            self.assertIn("synthetic early Qt DLL failure", child.stderr)
            self.assertFalse((directory / "logs/app.log").exists())

    def test_early_frozen_entry_records_import_failure_with_no_stderr(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import importlib.abc, runpy, sys
                from pathlib import Path
                from mnmparse.app import launch_identity
                from mnmparse.app import crash_diagnostics as diagnostic
                directory = Path(sys.argv[1])
                launch_identity.launch_if_needed = lambda args, wait=False: None
                sys.frozen = True
                sys.executable = str(directory / "synthetic-session.exe")
                sys.stderr = None
                class BrokenQt(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "PySide6.QtCore":
                            assert diagnostic._enabled
                            raise ImportError("synthetic windowed Qt DLL failure")
                sys.meta_path.insert(0, BrokenQt())
                sys.argv = ["PNUT"]
                runpy.run_module("mnmparse.app", run_name="__main__")
            """, directory, expected_code=1)
            trace = (directory / "logs/native-crash.log").read_text(encoding="utf-8")
            self.assertIn("ImportError: synthetic windowed Qt DLL failure", trace)
            self.assertIn("frozen=True", trace)
            self.assertIn("synthetic-session.exe", trace)
            self.assertIn("PID ", trace)
            self.assertIn("Runtime: ", trace)

    def test_early_smoke_uses_report_destination_without_importing_qt(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import sys
                from pathlib import Path
                from mnmparse.app import launch_identity
                from mnmparse.app import crash_diagnostics as diagnostic
                from mnmparse.app.__main__ import main
                directory = Path(sys.argv[1])
                report = directory / "smoke.json"
                def stop_before_qt(args, wait=False):
                    assert diagnostic._path == directory / "smoke.json.crash-logs/native-crash.log"
                    assert not any(name.startswith("PySide6") for name in sys.modules)
                    return 0
                launch_identity.launch_if_needed = stop_before_qt
                assert main(["--smoke-test", "--report", str(report)]) == 0
            """, directory)
            self.assertTrue((directory / "smoke.json.crash-logs/native-crash.log").is_file())

    def test_selftest_uses_disposable_logs_even_when_normal_config_is_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import sys, types
                from pathlib import Path
                from unittest.mock import patch
                from mnmparse.app import crash_diagnostics as diagnostic
                from mnmparse.app.__main__ import main
                directory = Path(sys.argv[1])
                destination = directory / "disposable-logs"
                def run(args):
                    assert diagnostic.startup_log_directory() == destination
                    assert diagnostic._path == destination / "native-crash.log"
                    assert not any(name.startswith("PySide6") for name in sys.modules)
                    return 0
                sys.modules["mnmparse.app.main"] = types.SimpleNamespace(run=run)
                with patch.object(diagnostic.tempfile, "mkdtemp", return_value=str(destination)):
                    assert main(["--selftest-seconds=1", "--config", "config.json"]) == 0
            """, directory)
            self.assertTrue((directory / "disposable-logs/native-crash.log").is_file())

    def test_python_and_thread_exceptions_are_persisted_once_and_original_hooks_are_chained(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import sys, threading
                from pathlib import Path
                from mnmparse.app import main
                from mnmparse.app import crash_diagnostics as diagnostic
                calls = []
                sys.excepthook = lambda *args: calls.append("main")
                threading.excepthook = lambda args: calls.append("thread")
                directory = Path(sys.argv[1])
                main.setup_logging(False, directory)
                installed = (sys.excepthook, threading.excepthook)
                main.setup_logging(False, directory)
                assert installed == (sys.excepthook, threading.excepthook)
                try:
                    raise RuntimeError("synthetic uncaught main failure")
                except RuntimeError:
                    sys.excepthook(*sys.exc_info())
                def failing_worker():
                    raise ValueError("synthetic uncaught worker failure")
                worker = threading.Thread(target=failing_worker, name="fixture-worker")
                worker.start()
                worker.join(3)
                assert not worker.is_alive()
                assert calls == ["main", "thread"]
            """, directory)
            for filename in ("native-crash.log", "app.log"):
                trace = (directory / filename).read_text(encoding="utf-8")
                self.assertIn("RuntimeError: synthetic uncaught main failure", trace)
                self.assertIn("ValueError: synthetic uncaught worker failure", trace)
                self.assertEqual(trace.count("Uncaught exception in thread fixture-worker"), 1)

    def test_rotation_failure_preserves_evidence_and_still_enables_reporting(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.run_child("""
                import faulthandler, sys
                from pathlib import Path
                from unittest.mock import patch
                from mnmparse.app import crash_diagnostics as diagnostic
                directory = Path(sys.argv[1])
                path = directory / "native-crash.log"
                original = b"synthetic retained evidence\\n" * 20
                path.write_bytes(original)
                diagnostic.NATIVE_LOG_BYTES = 64
                with patch.object(Path, "replace", side_effect=PermissionError("synthetic sharing conflict")):
                    assert diagnostic.enable_crash_diagnostics(directory) == path
                assert faulthandler.is_enabled()
                assert path.read_bytes().startswith(original)
            """, directory)
