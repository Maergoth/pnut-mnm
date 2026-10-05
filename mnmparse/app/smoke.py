"""Report frozen startup failures before the normal, Qt-dependent entry point loads.

This module deliberately imports only the standard library at module scope. The check
uses offscreen windows and disposable settings; it never starts capture or map downloads.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from typing import Any


QT_MODULES = ("QtCore", "QtGui", "QtWidgets", "QtMultimedia", "QtTextToSpeech")
APP_MODULES = (
    "engine", "overlay", "widgets", "pages", "crop_picker", "models",
    "triggers_runtime", "triggers_page", "map_overlay", "map_downloads", "app_updates", "main",
)
NATIVE_MODULES = ("numpy", "cv2")
WINDOWS_MODULES = (
    "windows_capture", "mss.windows", "winrt.runtime", "winrt.system",
    "winrt.windows.media.ocr", "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams", "winrt.windows.globalization",
    "winrt.windows.foundation", "winrt.windows.foundation.collections",
)


def _exercise(report: dict[str, Any]) -> None:
    # Assignment, rather than setdefault, prevents a developer's platform setting from
    # turning the unattended build check into a visible desktop application.
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    for name in QT_MODULES:
        full_name = f"PySide6.{name}"
        report["stage"] = f"import {full_name}"
        importlib.import_module(full_name)
        report["imports"].append(full_name)
    for name in NATIVE_MODULES + (WINDOWS_MODULES if sys.platform == "win32" else ()):
        report["stage"] = f"import {name}"
        importlib.import_module(name)
        report["imports"].append(name)

    from PySide6.QtCore import QEventLoop, QSettings, qVersion
    from PySide6.QtWidgets import QApplication

    from mnmparse import config

    report["qt_version"] = qVersion()
    original_root, original_config = config.PROJECT_ROOT, config.DEFAULT_CONFIG_PATH
    with tempfile.TemporaryDirectory(prefix="pnut-smoke-") as temporary:
        config.PROJECT_ROOT = Path(temporary)
        config.DEFAULT_CONFIG_PATH = config.PROJECT_ROOT / "config.json"
        app = None
        windows = []
        try:
            for name in APP_MODULES:
                full_name = f"mnmparse.app.{name}"
                report["stage"] = f"import {full_name}"
                importlib.import_module(full_name)
                report["imports"].append(full_name)

            from mnmparse.app import APP_VERSION, main, theme
            from mnmparse.app.engine import Engine
            from mnmparse.app.map_overlay import MapOverlay
            from mnmparse.app.overlay import OverlayWindow
            from mnmparse.maps import MapRepository
            from mnmparse.triggers import TriggerStore

            if main._IMPORT_ERRORS:
                raise RuntimeError(f"Application imports failed: {main._IMPORT_ERRORS}")
            report["stage"] = "construct QApplication"
            app = QApplication.instance() or QApplication(["PNUT M&M smoke test"])
            app.setQuitOnLastWindowClosed(False)
            theme.apply_theme(app)
            settings = QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat)
            cfg = config.Config(start_capture_on_launch=False, minimize_to_tray=False)
            engine = Engine(cfg)

            report["stage"] = "construct application windows"
            overlay = OverlayWindow(settings, cfg)
            windows.append(overlay)
            window = main.MainWindow(engine, overlay, cfg, settings)
            report["app_version"] = APP_VERSION
            windows.append(window)
            window.prepare_quit()
            missing = [key for key, page in window._pages.items() if isinstance(page, main._MissingPage)]
            if missing:
                raise RuntimeError(f"Application pages fell back to placeholders: {missing}")
            settings_page = window.page("settings")
            if settings_page.app_update_on_startup.isChecked() or settings_page.map_download_on_startup.isChecked():
                raise RuntimeError("Startup updates must be opt-in on a fresh installation")
            map_window = MapOverlay(settings, MapRepository(Path(temporary) / "map_cache"))
            windows.append(map_window)
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            if engine.is_running or engine._source is not None:
                raise RuntimeError("Startup check unexpectedly started capture")
            report["windows"] = [type(window).__name__ for window in windows]
            report["pages"] = sorted(window._pages)

            report["stage"] = "load bundled presets"
            store = TriggerStore()
            store.install_presets()
            names = {trigger.name for trigger in store.triggers}
            if not {"Gatekick", "Healkick", "Invis Break"}.issubset(names):
                raise RuntimeError(f"Bundled trigger presets missing: {sorted(names)}")
            report["presets"] = sorted(names)
        finally:
            # Any geometry writes go to the temporary INI file, never the real registry.
            for window in reversed(windows):
                window.close()
                window.deleteLater()
            if app is not None:
                app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            config.PROJECT_ROOT, config.DEFAULT_CONFIG_PATH = original_root, original_config


def run_smoke_test(report_path: Path, *, timeout_seconds: float = 20.0) -> int:
    """Write success/failure JSON and exit without opening a startup error dialog.

    The build also imposes an external timeout, since a native DLL hang can prevent
    Python's watchdog from running. A missing or incomplete report is always a failure.
    """
    started = time.monotonic()
    report: dict[str, Any] = {
        "ok": False, "stage": "starting", "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable, "imports": [],
    }
    report_path = Path(report_path).resolve()

    def write_report() -> None:
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def timed_out() -> None:
        report["ok"] = False
        report["error"] = f"Startup check exceeded {timeout_seconds:g} seconds"
        try:
            write_report()
        finally:
            os._exit(124)

    watchdog = threading.Timer(timeout_seconds, timed_out)
    watchdog.daemon = True
    saved = True
    try:
        write_report()
        watchdog.start()
        _exercise(report)
        report.update(ok=True, stage="complete")
    except BaseException as exc:
        report.update(ok=False, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    finally:
        watchdog.cancel()
        try:
            write_report()
        except OSError:
            saved = False
    return 0 if report["ok"] and saved else 1
