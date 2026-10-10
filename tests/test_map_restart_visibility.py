"""Map startup and restart checks with a real Qt event loop on Windows."""
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
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid


def _probe(scenario: str, output: Path) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PySide6.QtCore import QSettings, QTimer
    from mnmparse.app.main import App, MainWindow, _MissingEngine
    from mnmparse.app.map_overlay import MapOverlay
    from mnmparse.config import Config

    settings = QSettings(str(output.with_suffix(".ini")), QSettings.Format.IniFormat)
    with patch("mnmparse.app.main.QSettings", return_value=settings):
        app = App([])
    cfg = Config(start_capture_on_launch=False)
    app.engine = _MissingEngine(cfg)
    app.window = MainWindow(app.engine, None, cfg, settings)
    app.map_overlay = MapOverlay(settings)
    app.map_overlay.visibility_changed.connect(app._on_map_visibility)
    app.window.map_toggled.connect(app.set_map_visible)
    app.app_updates = SimpleNamespace(prepare_restart=lambda **kwargs: True, shutdown=lambda: None)
    native = ctypes.WinDLL("user32", use_last_error=True)
    native.IsWindowVisible.argtypes = [wintypes.HWND]
    native.IsWindowVisible.restype = wintypes.BOOL
    observations = {"events": []}

    def record(name: str) -> None:
        observations[name] = {
            "qt_visible": app.map_overlay.isVisible(),
            "native_visible": bool(native.IsWindowVisible(int(app.map_overlay.winId()))),
            "preference": settings.value("map/visible", True, type=bool),
            "shutdown": app._shut_down,
            "fullscreen": app.map_overlay.isFullScreen(),
        }

    def visibility(visible: bool) -> None:
        observations["events"].append(["map", visible, app._shut_down])

    app.map_overlay.visibility_changed.connect(visibility)
    app.aboutToQuit.connect(lambda: observations["events"].append(["aboutToQuit", app._shut_down]))
    app.window.show()
    app.set_map_visible(scenario != "closed")
    if scenario == "fullscreen":
        app.map_overlay.toggle_fullscreen()
    record("initial")

    def finish() -> None:
        record("before_restart")
        if scenario == "quit":
            app.request_quit()
        elif scenario == "restart_failed":
            app.app_updates.prepare_restart = lambda **kwargs: False
            app.restart_for_update()
            record("after_failed_restart")
            app.app_updates.prepare_restart = lambda **kwargs: True
            app.restart_for_update()
        else:
            app.restart_for_update()

    QTimer.singleShot(150, finish)
    # A broken callback must never leave an interactive test process running.
    QTimer.singleShot(3000, app.request_quit)
    app.exec()
    record("after_restart")
    settings.sync()
    reopened = QSettings(settings.fileName(), QSettings.Format.IniFormat)
    observations["saved_visible"] = reopened.value("map/visible", True, type=bool)
    observations["saved_fullscreen"] = reopened.value("map/fullscreen", False, type=bool)
    output.write_text(json.dumps(observations), encoding="utf-8")


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("PNUT_NATIVE_UI_TESTS") == "1",
                     "Native Windows UI checks require explicit PNUT_NATIVE_UI_TESTS=1")
class MapRestartVisibilityTests(unittest.TestCase):
    def run_probe(self, scenario: str, *, hidden_launch: bool) -> dict:
        pythonw = Path(getattr(sys, "_base_executable", sys.executable)).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("Native map probe requires pythonw.exe beside the interpreter")
        native = ctypes.WinDLL("user32", use_last_error=True)
        native.CreateDesktopW.argtypes = [wintypes.LPWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                          wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        native.CreateDesktopW.restype = wintypes.HANDLE
        native.CloseDesktop.argtypes = [wintypes.HANDLE]
        native.CloseDesktop.restype = wintypes.BOOL
        name = "PNUTMapRestartTest" + uuid.uuid4().hex
        desktop = native.CreateDesktopW(name, None, None, 0, 0x01FF, None)
        if not desktop:
            self.skipTest(f"Cannot create isolated desktop: {ctypes.WinError()}")
        try:
            with tempfile.TemporaryDirectory(prefix="pnut-map-restart-") as folder:
                output = Path(folder) / "observations.json"
                startup = subprocess.STARTUPINFO()
                startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startup.wShowWindow = 0 if hidden_launch else 1
                startup.lpDesktop = name
                env = dict(os.environ, QT_QPA_PLATFORM="windows")
                env["PYTHONPATH"] = os.pathsep.join(site.getsitepackages())
                process = subprocess.Popen(
                    [str(pythonw), str(Path(__file__).resolve()), "--probe", scenario, str(output)],
                    cwd=folder, env=env, startupinfo=startup,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    stdout, stderr = process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
                    if process.stdout is not None:
                        process.stdout.close()
                    if process.stderr is not None:
                        process.stderr.close()
                self.assertEqual(process.returncode, 0, stderr or stdout)
                self.assertTrue(output.is_file(), f"Probe produced no result: {stderr}")
                return json.loads(output.read_text(encoding="utf-8"))
        finally:
            native.CloseDesktop(desktop)

    def test_open_map_survives_hidden_old_updater_launch(self):
        observed = self.run_probe("open", hidden_launch=True)
        self.assertTrue(observed["before_restart"]["qt_visible"], observed)
        self.assertTrue(observed["before_restart"]["native_visible"], observed)
        self.assertTrue(observed["saved_visible"], observed)

    def test_normal_restart_preserves_open_map(self):
        observed = self.run_probe("open", hidden_launch=False)
        self.assertTrue(observed["before_restart"]["qt_visible"], observed)
        self.assertTrue(observed["before_restart"]["native_visible"], observed)
        self.assertTrue(observed["saved_visible"], observed)

    def test_fullscreen_map_stays_open_and_fullscreen_on_restart(self):
        observed = self.run_probe("fullscreen", hidden_launch=False)
        self.assertTrue(observed["before_restart"]["qt_visible"], observed)
        self.assertTrue(observed["before_restart"]["fullscreen"], observed)
        self.assertTrue(observed["saved_visible"], observed)
        self.assertTrue(observed["saved_fullscreen"], observed)

    def test_regular_quit_also_preserves_open_map(self):
        observed = self.run_probe("quit", hidden_launch=False)
        self.assertTrue(observed["saved_visible"], observed)

    def test_failed_update_restart_keeps_app_and_map_running(self):
        observed = self.run_probe("restart_failed", hidden_launch=False)
        state = observed["after_failed_restart"]
        self.assertTrue(state["qt_visible"], observed)
        self.assertTrue(state["native_visible"], observed)
        self.assertTrue(state["preference"], observed)
        self.assertFalse(state["shutdown"], observed)
        self.assertTrue(observed["saved_visible"], observed)

    def test_closed_map_stays_closed_during_hidden_launcher_restart(self):
        observed = self.run_probe("closed", hidden_launch=True)
        self.assertFalse(observed["before_restart"]["qt_visible"], observed)
        self.assertFalse(observed["before_restart"]["native_visible"], observed)
        self.assertFalse(observed["saved_visible"], observed)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--probe":
        _probe(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
