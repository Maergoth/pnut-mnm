"""Real Windows/Qt regression checks on a desktop that is never shown to the user.

The ordinary test suite uses Qt's offscreen platform. These subprocesses must use
the Windows platform because the launcher/Qt visibility mismatch is a native
ShowWindow interaction, not an offscreen widget state.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import site
import subprocess
import sys
import tempfile
import unittest
import uuid


def _native_probe(scenario: str, output: Path) -> None:
    """Run the actual shell with temporary settings and no capture or controllers."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtWidgets import QApplication

    from mnmparse.app.main import MainWindow, _MissingEngine
    from mnmparse.config import Config

    app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    cfg = Config(start_capture_on_launch=False)
    settings = QSettings(str(output.with_suffix(".ini")), QSettings.Format.IniFormat)
    window = MainWindow(_MissingEngine(cfg), None, cfg, settings)
    native = ctypes.WinDLL("user32", use_last_error=True)
    native.IsWindowVisible.argtypes = [wintypes.HWND]
    native.IsWindowVisible.restype = wintypes.BOOL
    native.IsZoomed.argtypes = [wintypes.HWND]
    native.IsZoomed.restype = wintypes.BOOL
    native.IsIconic.argtypes = [wintypes.HWND]
    native.IsIconic.restype = wintypes.BOOL
    native.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    native.ShowWindow.restype = wintypes.BOOL
    observations = {}

    def record(name: str) -> None:
        hwnd = int(window.winId())
        observations[name] = {
            "qt_visible": window.isVisible(),
            "native_visible": bool(native.IsWindowVisible(hwnd)),
            "qt_maximized": window.isMaximized(),
            "native_maximized": bool(native.IsZoomed(hwnd)),
            "qt_minimized": window.isMinimized(),
            "native_minimized": bool(native.IsIconic(hwnd)),
        }

    def finish() -> None:
        record("final")
        window.prepare_quit()
        window.close()
        settings.sync()
        output.write_text(json.dumps(observations), encoding="utf-8")
        app.quit()

    def reveal_native_hidden() -> None:
        window.showMaximized()
        # Recreate the Qt-visible/native-hidden mismatch after startup has been
        # repaired, to exercise the actual tray-facing reveal() entry point.
        native.ShowWindow(int(window.winId()), 0)
        record("before_reveal")
        window.reveal()
        record("after_reveal")
        QTimer.singleShot(50, finish)

    def reveal_minimized() -> None:
        window.setWindowState(Qt.WindowState.WindowMaximized | Qt.WindowState.WindowMinimized)
        record("before_reveal")
        window.reveal()
        record("after_reveal")
        QTimer.singleShot(50, finish)

    window.show()
    record("initial")
    if scenario == "intentional_hide":
        # The zero-delay repair callback is still queued. An explicit tray hide
        # before it runs must not be undone by native visibility reconciliation.
        window.hide()
        record("after_hide")
        QTimer.singleShot(75, finish)
    elif scenario == "reveal_hidden_maximized":
        QTimer.singleShot(75, reveal_native_hidden)
    elif scenario == "reveal_minimized_maximized":
        QTimer.singleShot(75, reveal_minimized)
    elif scenario == "startup":
        QTimer.singleShot(75, finish)
    else:
        raise ValueError(f"Unknown native visibility scenario: {scenario}")
    app.exec()


@unittest.skipUnless(sys.platform == "win32", "Native launcher visibility requires Windows")
class NativeWindowVisibilityTests(unittest.TestCase):
    def run_probe(self, scenario: str) -> dict:
        # A venv's pythonw.exe is itself a launcher and may consume or rewrite
        # SW_HIDE. Start the real GUI interpreter, with the current packages.
        pythonw = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("Native hidden-launch probe requires pythonw.exe beside the test interpreter")
        native = ctypes.WinDLL("user32", use_last_error=True)
        native.CreateDesktopW.argtypes = [
            wintypes.LPWSTR, wintypes.LPWSTR, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        ]
        native.CreateDesktopW.restype = wintypes.HANDLE
        native.CloseDesktop.argtypes = [wintypes.HANDLE]
        native.CloseDesktop.restype = wintypes.BOOL
        name = "PNUTVisibilityTest" + uuid.uuid4().hex
        desktop = native.CreateDesktopW(name, None, None, 0, 0x01FF, None)
        if not desktop:
            error = ctypes.get_last_error()
            self.skipTest(f"Cannot create isolated Windows desktop: {ctypes.FormatError(error)} ({error})")
        try:
            with tempfile.TemporaryDirectory(prefix="pnut-window-visibility-") as folder:
                output = Path(folder) / "observations.json"
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0  # SW_HIDE, as supplied by the old updater.
                startup.lpDesktop = name
                env = dict(os.environ)
                env["QT_QPA_PLATFORM"] = "windows"
                env["PYTHONPATH"] = os.pathsep.join(site.getsitepackages())
                result = subprocess.run(
                    [str(pythonw), str(Path(__file__).resolve()), "--native-probe", scenario, str(output)],
                    cwd=folder, env=env, startupinfo=startup,
                    capture_output=True, text=True, timeout=25,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue(output.is_file(), f"Probe produced no result: {result.stderr}")
                return json.loads(output.read_text(encoding="utf-8"))
        finally:
            native.CloseDesktop(desktop)

    def test_hidden_old_updater_launch_is_automatically_repaired(self):
        states = self.run_probe("startup")
        self.assertTrue(states["initial"]["qt_visible"])
        self.assertFalse(states["initial"]["native_visible"], "Probe must reproduce the native startup mismatch")
        self.assertTrue(states["final"]["qt_visible"])
        self.assertTrue(states["final"]["native_visible"])

    def test_tray_reveal_repairs_native_hidden_window_and_keeps_maximized(self):
        states = self.run_probe("reveal_hidden_maximized")
        self.assertTrue(states["before_reveal"]["qt_visible"])
        self.assertFalse(states["before_reveal"]["native_visible"])
        self.assertTrue(states["before_reveal"]["qt_maximized"])
        for stage in ("after_reveal", "final"):
            self.assertTrue(states[stage]["qt_visible"])
            self.assertTrue(states[stage]["native_visible"])
            self.assertTrue(states[stage]["qt_maximized"])
            self.assertTrue(states[stage]["native_maximized"])

    def test_tray_reveal_restores_minimized_window_to_maximized(self):
        states = self.run_probe("reveal_minimized_maximized")
        self.assertTrue(states["before_reveal"]["qt_minimized"])
        self.assertTrue(states["before_reveal"]["native_minimized"])
        self.assertTrue(states["final"]["qt_visible"])
        self.assertTrue(states["final"]["native_visible"])
        self.assertTrue(states["final"]["qt_maximized"])
        self.assertTrue(states["final"]["native_maximized"])
        self.assertFalse(states["final"]["qt_minimized"])
        self.assertFalse(states["final"]["native_minimized"])

    def test_intentional_hide_before_deferred_repair_stays_hidden(self):
        states = self.run_probe("intentional_hide")
        self.assertTrue(states["initial"]["qt_visible"])
        for stage in ("after_hide", "final"):
            self.assertFalse(states[stage]["qt_visible"])
            self.assertFalse(states[stage]["native_visible"])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--native-probe":
        _native_probe(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
