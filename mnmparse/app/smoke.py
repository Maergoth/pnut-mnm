"""Report frozen startup failures before the normal, Qt-dependent entry point loads.

This module deliberately imports only the standard library at module scope. The check
uses offscreen windows and disposable settings; it never starts capture or map downloads.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import traceback
from typing import Any


QT_MODULES = ("QtCore", "QtGui", "QtWidgets", "QtMultimedia", "QtTextToSpeech")
APP_MODULES = (
    "engine", "overlay", "widgets", "pages", "crop_picker", "models",
    "triggers_runtime", "triggers_page", "trigger_share_dialog", "trigger_help", "map_overlay", "map_downloads", "app_updates", "main",
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

    from PySide6.QtCore import QEventLoop, QSettings, Qt, qVersion
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

            report["stage"] = "exercise merged pet attribution"
            from mnmparse.app.models import build_snapshot
            from mnmparse.parser import parse_line
            from mnmparse.stats import Stats

            stats = Stats(player_name="SmokeOwner")
            stats.roster.set_pet_owner("SmokePet", "SmokeOwner")
            for offset, line in enumerate((
                "You crush a rat for 20 points of damage.",
                "SmokePet bites a rat for 30 points of damage.",
            )):
                stats.add(parse_line(line, 100.0 + offset, "SmokeOwner"))
            snapshot = build_snapshot(stats, stats.current(), "SmokeOwner")
            owner = next(row for row in snapshot.rows if row.name == "SmokeOwner")
            expected_label = "SmokeOwner + SmokeOwner's Pet"
            if (owner.damage != 50 or owner.display_name != expected_label
                    or any(row.name == "SmokePet" for row in snapshot.rows)
                    or snapshot.total_damage != 50):
                raise RuntimeError("The packaged pet attribution did not merge into the owner")
            window.page("live")._pane.set_snapshot(snapshot)
            overlay.set_snapshot(snapshot)
            overlay._flush_snapshot()
            for table in (window.page("live")._pane.table, overlay._table):
                model = table._model
                names = [model.data(model.index(index, model.column_index("name")), Qt.ItemDataRole.DisplayRole)
                         for index in range(model.rowCount())]
                if names.count(expected_label) != 1 or "SmokePet" in names:
                    raise RuntimeError("The packaged meter did not display one combined owner row")
            report["pet_rollup"] = {"label": expected_label, "damage": owner.damage}

            report["stage"] = "load bundled presets"
            store = TriggerStore()
            store.install_presets()
            names = {trigger.name for trigger in store.triggers}
            if not {"Gatekick", "Healkick", "Invis Break"}.issubset(names):
                raise RuntimeError(f"Bundled trigger presets missing: {sorted(names)}")
            report["presets"] = sorted(names)

            report["stage"] = "exercise timer sharing"
            from mnmparse.trigger_chat import ChatShareAssembler, encode_trigger
            from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog, TriggerSharePrompt

            trigger = store.triggers[0]
            code = encode_trigger(trigger)
            received = ChatShareAssembler().feed(code, sender="Smoke test")
            if len(received) != 1 or received[0].trigger.to_dict() != trigger.to_dict():
                raise RuntimeError("Packaged timer sharing did not preserve the definition")
            windows.append(TriggerChatExportDialog(trigger, code, window))
            windows.append(TriggerSharePrompt(received[0].trigger, received[0].sender, window))
            report["chat_share_chars"] = len(code)

            report["stage"] = "open bundled trigger guide"
            from mnmparse.app.trigger_help import TriggerHelpDialog

            guide = TriggerHelpDialog(window)
            windows.append(guide)
            guide_text = guide.browser.toPlainText()
            if "Table of contents" not in guide_text or "Righteous Smite II: 154 damage" not in guide_text:
                raise RuntimeError("The packaged trigger guide is missing its content or examples")
            report["trigger_help"] = True
            report["window_titles"] = [item.windowTitle() for item in [*windows, overlay.timer_panel, overlay.attack_bar]]
            if not all(re.fullmatch(r"[0-9a-f]{24}", title) for title in report["window_titles"]):
                raise RuntimeError("An application window is missing its randomized session title")
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
        if report["frozen"]:
            report["stage"] = "verify session executable"
            from mnmparse.app.launch_identity import is_session_executable
            from mnmparse.app.app_updates import installed_directory

            report["randomized_executable"] = is_session_executable(Path(sys.executable))
            if not report["randomized_executable"]:
                raise RuntimeError("The packaged application did not launch with a session executable name")
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
            kernel32.GetModuleFileNameW.restype = wintypes.DWORD
            filename = ctypes.create_unicode_buffer(32768)
            length = kernel32.GetModuleFileNameW(None, filename, len(filename))
            if not length or length >= len(filename):
                raise ctypes.WinError(ctypes.get_last_error())
            report["native_executable"] = filename.value
            if Path(filename.value).name != Path(sys.executable).name:
                raise RuntimeError("The native process image does not match the session executable")
            report["update_directory"] = str(installed_directory())
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
