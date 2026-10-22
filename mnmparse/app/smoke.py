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


QT_MODULES = ("QtCore", "QtGui", "QtWidgets", "QtNetwork", "QtMultimedia", "QtTextToSpeech")
APP_MODULES = (
    "engine", "overlay", "widgets", "pages", "crop_picker", "models",
    "triggers_runtime", "triggers_page", "trigger_share_dialog", "trigger_help", "legal_dialog",
    "map_overlay", "map_downloads", "app_updates", "main",
    "profile_tools", "setup_wizard", "low_health", "npc_panel", "npc_panel_lifetime", "npc_details",
    "leaderboard_page", "leaderboard_client",
)
NATIVE_MODULES = ("numpy", "cv2", "regex", "regex._regex")
WINDOWS_MODULES = (
    "windows_capture", "mss.windows", "winrt.runtime", "winrt.system",
    "winrt.windows.media.ocr", "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams", "winrt.windows.globalization",
    "winrt.windows.foundation", "winrt.windows.foundation.collections",
)


def _exercise_leaderboard(report: dict[str, Any], temporary_root: Path) -> None:
    """Run real native crypto/storage on synthetic data, without capture or network."""
    if sys.platform != "win32":
        report["leaderboard"] = {"windows_storage_skipped": True}
        return

    from PySide6.QtCore import Qt

    from mnmparse.app.leaderboard_page import LeaderboardModel
    from mnmparse.leaderboard import clipboard_text, dump_records, parse_records, utc_stamp, validate_snapshot
    from mnmparse.parser import parse_line
    from mnmparse.protected_stats import EncryptedFile, ProtectedLedger, ProtectionError

    report["imports"].extend(("mnmparse.leaderboard", "mnmparse.protected_stats"))
    directory = Path(temporary_root) / "leaderboard-smoke"
    path = directory / "state.bin"
    ledger = ProtectedLedger(path)
    if ledger.error:
        raise RuntimeError("Packaged leaderboard storage could not initialize")
    # Exercises the bundled scrypt/OpenSSL path as well as Ed25519 below.
    ledger.set_phrase("Synthetic startup smoke phrase")
    ledger.set_capture_eligible(True)
    ledger.record_stats({"max_health": 1234567, "max_mana": 7654321,
                         "captured_at": utc_stamp(), "capture_backend": "window"})
    event = parse_line("You have slain a smoke rat.", 100, "SmokeOwner")
    if event is None or event.kind != "kill":
        raise RuntimeError("Synthetic leaderboard kill did not parse")
    ledger.record_event(event)
    record = validate_snapshot(ledger.prepare_submission())
    if (record["max_health"] != 1234567 or record["max_mana"] != 7654321
            or record["personal_kills"] != 1):
        raise RuntimeError("Packaged leaderboard did not retain synthetic reader values and personal kill")
    text = clipboard_text(record)
    if ("Max Health: 1234567" not in text or "Max Mana: 7654321" not in text
            or "Kills: 1" not in text or "SmokeOwner" in text):
        raise RuntimeError("Packaged leaderboard clipboard did not retain its anonymous fields")
    if parse_records(dump_records([record]), strict=True) != [record]:
        raise RuntimeError("Packaged leaderboard signature or public record round-trip failed")
    cache = EncryptedFile(directory / "cache.bin")
    state = {"records": [record], "pending": None}
    cache.save(state)
    if cache.load() != state:
        raise RuntimeError("Packaged leaderboard cache round-trip failed")
    for file in (path, cache.path):
        raw = file.read_bytes()
        if any(value in raw for value in (b"personal_kills", b"1234567", b"7654321", record["public_key"].encode("ascii"))):
            raise RuntimeError("Packaged leaderboard wrote a plaintext numeric record")
    restarted = ProtectedLedger(path)
    status = restarted.status()
    if (restarted.error or status.get("alias") != record["alias"]
            or status.get("personal_kills") != 1 or status.get("eligible")):
        raise RuntimeError("Packaged leaderboard restart did not preserve totals or clear eligibility")
    try:
        restarted.prepare_submission()
    except ProtectionError:
        pass
    else:
        raise RuntimeError("Packaged leaderboard reused pre-restart reader eligibility")
    model = LeaderboardModel()
    # Presentation records are intentionally synthetic: a lexicographic sort
    # would put 9 ahead of 100 and fail this native-widget check.
    model.set_records([dict(record, high_hit=9), dict(record, high_hit=100)])
    model.sort(5, Qt.SortOrder.DescendingOrder)
    if [entry["high_hit"] for entry in model.records] != [100, 9]:
        raise RuntimeError("Packaged leaderboard numeric sorting failed")
    report["leaderboard"] = {"native_crypto": True, "windows_storage": True, "counter_roundtrip": True,
                             "cache_roundtrip": True, "fresh_read_required": True,
                             "clipboard": True, "numeric_sort": True}


def _exercise_player_status(report: dict[str, Any], windows: list[Any]) -> None:
    """Use generated pixels and readings only; never opens a capture source."""
    import numpy as np
    from PySide6.QtCore import QRect, Qt

    from mnmparse.app.low_health import LowHealthFlash
    from mnmparse.config import Config
    from mnmparse.ocr import OcrLine
    from mnmparse.player_status import read_player_status
    from mnmparse.player_telemetry import PlayerTelemetry, XP_INTERVAL
    from mnmparse.privacy import project_session
    from mnmparse.session import SessionStats

    report["imports"].extend(("mnmparse.player_status", "mnmparse.player_telemetry"))
    lines = [OcrLine(180, 30, 10, "Health 8/100"), OcrLine(180, 65, 10, "Mana 50/100"),
             OcrLine(170, 100, 10, "Endurance 100/100"), OcrLine(130, 170, 10, "Weight 20/50")]
    image = np.full((260, 400, 3), 90, dtype=np.uint8)
    image[196:216, 90:330] = 8
    image[196:216, 90:266] = (0, 188, 255)
    image[213:216, 90:248] = (255, 80, 0)
    reading = read_player_status(lines, image, xp_bar_confirmed=True)
    if (reading is None or reading.hp_percent != 8 or reading.xp_percent is None
            or abs(reading.xp_percent - 73.2) > .2
            or reading.xp_resolution_points is None or reading.xp_resolution_points > .1):
        raise RuntimeError("Packaged mini-stat reading did not recognize personal HP and dual-scale XP")
    telemetry = PlayerTelemetry()
    measurement = {"hp_current": reading.hp_current, "hp_max": reading.hp_max,
                   "xp_fraction": reading.xp_percent / 100,
                   "xp_resolution_points": reading.xp_resolution_points}
    telemetry.update(measurement, 100)
    if telemetry.snapshot()["hp_low"]:
        raise RuntimeError("Personal low HP did not require a second fresh reading")
    telemetry.update(measurement, 100.25)
    if not telemetry.snapshot()["hp_low"] or telemetry.snapshot()["hp_below10_episodes"] != 1:
        raise RuntimeError("Personal low HP could not confirm two fresh readings")
    if telemetry.snapshot()["xp_points_per_hour"] is not None:
        raise RuntimeError("Packaged XP rate was available before its first complete sample")
    interval = int(XP_INTERVAL)
    for second in range(1, interval + 1):
        fraction = measurement["xp_fraction"] + (.02 if second >= interval - 1 else 0)
        telemetry.update(dict(measurement, xp_fraction=fraction), 100.25 + second)
        if second < interval and telemetry.snapshot()["xp_points_per_hour"] is not None:
            raise RuntimeError("Packaged XP rate used an incomplete observation")
    summary = telemetry.snapshot()
    expected_rate = 2.0 * 3600.0 / XP_INTERVAL
    if summary["xp_points_per_hour"] is None or abs(summary["xp_points_per_hour"] - expected_rate) > .1:
        raise RuntimeError("Packaged XP rate did not use a completed observation")
    personal = SessionStats("SmokeOwner", started=100)
    personal.note_player_status(summary)
    protected = project_session(personal.snapshot(now=401), Config(player_name="SmokeOwner"))
    if protected.player_status.get("xp_points_per_hour") != summary["xp_points_per_hour"]:
        raise RuntimeError("Carebear Mode hid the owner's XP rate")
    if any(key.startswith("hp_") for key in protected.player_status):
        raise RuntimeError("Personal HP alerts became session statistics")
    flash = LowHealthFlash()
    windows.append(flash)
    flash.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    flash.flash(QRect(0, 0, 400, 250), reduced_motion=True)
    flash._started -= 60
    flash._tick()
    preview = flash.grab().toImage()
    if (preview.pixelColor(200, 125).alpha() != 0 or preview.pixelColor(1, 125).alpha() == 0
            or preview.pixelColor(20, 125).alpha() == 0 or not flash.active or flash.isHidden()
            or not flash.windowFlags() & Qt.WindowType.WindowTransparentForInput
            or not flash.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)):
        raise RuntimeError("The low-HP flash did not preserve a transparent, passive game view")
    flash.stop()
    if not flash.isHidden() or flash._timer.isActive():
        raise RuntimeError("The low-HP flash remained active after stopping")
    report["player_status"] = {"reader": True, "dual_scale_xp": True, "fresh_hp": True,
                               "thirty_second_rate": True, "carebear_own_xp": True, "passive_flash": True}


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
    from PySide6.QtWidgets import QApplication, QCheckBox, QWizard

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
            cfg = config.Config(start_capture_on_launch=False, minimize_to_tray=False,
                                casual_mode=False, casual_mode_confirmed=True)
            engine = Engine(cfg)

            report["stage"] = "construct application windows"
            overlay = OverlayWindow(settings, cfg)
            windows.append(overlay)
            window = main.MainWindow(engine, overlay, cfg, settings)
            report["app_version"] = APP_VERSION
            windows.append(window)
            if window._mode_button.text() != "🔥 Elitist Scumbag Mode 🔥":
                raise RuntimeError("Full mode is missing its flame badge")
            report["elitist_badge"] = "flames"
            window.prepare_quit()
            missing = [key for key, page in window._pages.items() if isinstance(page, main._MissingPage)]
            if missing:
                raise RuntimeError(f"Application pages fell back to placeholders: {missing}")
            report["stage"] = "exercise anonymous leaderboard native dependencies"
            _exercise_leaderboard(report, Path(temporary))
            settings_page = window.page("settings")
            if settings_page.app_update_on_startup.isChecked() or settings_page.map_download_on_startup.isChecked():
                raise RuntimeError("Startup updates must be opt-in on a fresh installation")
            report["stage"] = "exercise fresh setup appearance"
            from mnmparse.app.setup_wizard import SetupWizard

            setup = SetupWizard(engine, config.Config(start_capture_on_launch=False), window)
            windows.append(setup)
            setup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            setup.show()
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            heading = setup.currentPage().title()
            if setup.wizardStyle() != QWizard.WizardStyle.ModernStyle or "PNUT setup" not in heading:
                raise RuntimeError("Setup must use the themed Modern wizard and readable content heading")
            image = setup.grab().toImage()
            viewport = setup.character_page.body_scroll.viewport().grab().toImage()
            colors = (image.pixelColor(image.width() - 15, 15),
                      image.pixelColor(image.width() // 2, image.height() - 4),
                      viewport.pixelColor(viewport.width() // 2, viewport.height() - 20))
            rgb = [[color.red(), color.green(), color.blue()] for color in colors]
            if image.isNull() or viewport.isNull() or any(max(color) >= 70 for color in rgb):
                raise RuntimeError("Setup header, footer or content rendered white instead of the dark theme")
            if not setup.character_page.morality.switch.isChecked():
                raise RuntimeError("The setup mode slider must default to Carebear Mode")
            if setup.crop_page.findChildren(QCheckBox):
                raise RuntimeError("Crop validation must not require a confirmation checkbox")
            import numpy as np
            from mnmparse.app.crop_picker import CropPicker

            setup.character_page.name.setText("SmokeOwner")
            setup.next()
            frame = np.full((720, 1280, 3), 99, dtype=np.uint8)
            setup.crop_picker.set_frame(frame)
            pixel = setup.crop_picker.canvas._pixmap.toImage().pixelColor(10, 10)
            if pixel.red() != 99 or setup.crop_picker.frame() is not frame:
                raise RuntimeError("Setup must show the local calibration image and provide the original frame")
            standard_picker = CropPicker(engine, config.Config(), window)
            standard_picker.set_frame(frame)
            if (standard_picker.canvas._pixmap.toImage().pixelColor(10, 10).red() != 99
                    or standard_picker.frame() is not frame):
                raise RuntimeError("Carebear Mode hid the local calibration image or frame")
            standard_picker.set_frame(None)
            standard_picker.deleteLater()
            if setup.crop_page.isComplete():
                raise RuntimeError("Setup advanced without testing the selected chat crop")
            setup._begin_ocr()
            setup._ocr_completed(3)
            setup.next()
            if setup.currentId() != 2 or "3 · Common options" not in setup.currentPage().title():
                raise RuntimeError("Setup step 3 must show common options")
            common = setup.options_page
            if (common.revenge_enabled.isChecked() or common.export_auto.isChecked()
                    or not common.attack_bar.isChecked() or not common.display_map.isChecked()):
                raise RuntimeError("Fresh common setup options have incorrect defaults")
            common.revenge_enabled.setChecked(True)
            common.attack_bar.setChecked(False)
            common.export_auto.setChecked(True)
            common.display_map.setChecked(False)
            setup.next()
            if (not setup._draft.revenge_enabled or setup._draft.attack_bar
                    or not setup._draft.export_auto or setup.display_map):
                raise RuntimeError("Common setup choices were not staged")
            setup.reject()
            if setup.crop_picker._frame is not None or setup.crop_picker.canvas.has_frame():
                raise RuntimeError("Closed setup retained its calibration image")
            if engine.is_running or engine._source is not None:
                raise RuntimeError("Setup appearance check unexpectedly started capture")
            report["setup"] = {"style": "Modern", "heading": heading, "background_rgb": rgb,
                               "casual": setup._draft.casual_mode, "capture_started": False,
                               "local_calibration_preview": True, "local_frame_access": True,
                               "calibration_cleared": True, "mode_slider": True,
                               "crop_checkbox_removed": True, "common_options": True}
            report["stage"] = "exercise Revenge pop-out and display limits"
            import dataclasses
            from mnmparse.app.revenge import RevengeController, RevengeList, RevengePopoutWindow

            now = [time.time()]
            revenge_cfg = dataclasses.replace(cfg, player_name="SmokeOwner", revenge_enabled=True,
                                              revenge_entries=1)
            revenge = RevengeController(revenge_cfg, Path(temporary) / "revenge.json", window,
                                        clock=lambda: now[0])
            popout = RevengePopoutWindow(revenge, settings)
            popout.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            windows.append(popout)
            if not revenge.save_attacker("SmokeBandit"):
                raise RuntimeError("Could not add a player to Revenge List")
            now[0] += 1
            if not revenge.save_attacker("SmokeRaider"):
                raise RuntimeError("Could not add a newer Revenge List entry")
            popout.show()
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            lists = popout.findChildren(RevengeList)
            if len(lists) != 1 or revenge.saved_names != ("SmokeRaider",) or set(lists[0]._rows) != {"SmokeRaider"}:
                raise RuntimeError("Revenge pop-out did not show the newest filtered entry")
            revenge.set_config(dataclasses.replace(revenge_cfg, revenge_entries=0))
            if len(revenge.saved_names) != 2:
                raise RuntimeError("Revenge display limits discarded saved entries")
            revenge.set_config(dataclasses.replace(revenge_cfg, casual_mode=True, casual_mode_confirmed=False))
            if revenge.saved_names != ("SmokeRaider",) or set(lists[0]._rows) != {"SmokeRaider"}:
                raise RuntimeError("Carebear Mode hid the enabled Revenge List")
            revenge.set_config(dataclasses.replace(revenge_cfg, revenge_enabled=False))
            if not popout.isHidden():
                raise RuntimeError("Disabled Revenge List left the pop-out visible")
            revenge.flush_pending_changes()
            report["revenge"] = {"popout": True, "newest_first": True, "limits_preserve_saved": True,
                                 "casual_available": True, "disabled_hidden": True}
            map_window = MapOverlay(settings, MapRepository(Path(temporary) / "map_cache"))
            windows.append(map_window)
            report["stage"] = "exercise cached NPC reference and item tooltip"
            from mnmparse.app.npc_details import NpcDetailsController
            from mnmparse.npc_wiki import WikiRepository, parse_npc, parse_item, parse_notables
            from mnmparse.parser import parse_line

            # A real disk round-trip with synthetic records; no wiki or game access.
            class CachedReference(WikiRepository):
                refresh_npc = WikiRepository.cached_npc
                refresh_item = WikiRepository.cached_item
                refresh_notables = WikiRepository.cached_notables

            reference = CachedReference(Path(temporary) / "npc_cache")
            npc = parse_npc('<p>Level: 12–15</p><h2>Unique drops</h2>'
                            '<ul><li><a href="/wiki/Smoke_Staff">Smoke Staff</a></li></ul>', "Smoke Keeper")
            item = parse_item('<p>MAGIC<br>Slot: PRIMARY SECONDARY<br>INT: +6<br>Race: ALL</p>', "Smoke Staff")
            notable = parse_notables('<h2>Notable NPCs</h2><ul><li><a href="/wiki/Smoke_Keeper">'
                                     'Smoke Keeper</a></li></ul>', "Night Harbor")
            for kind, record, title in (("npc", npc, npc.name), ("item", item, item.name),
                                        ("notables", notable, notable.zone)):
                reference._save(kind, title, title, "42:", record)
            reference = CachedReference(reference.cache_dir)
            controller = NpcDetailsController(reference, overlay.npc_panel, map_window, window)
            try:
                controller.on_message(None, parse_line("Smoke Keeper seems indifferent to your presence.", 0))
                if overlay.npc_panel._details != npc or not overlay.npc_panel.options["unique_drops"]:
                    raise RuntimeError("Cached NPC details did not display immediately")
                map_window.set_zone("Night Harbor")
                controller.show_notables("Night Harbor")
                actions = [a for a in map_window.notable_npcs_menu.actions() if a.isEnabled()]
                if not actions or actions[0].data() != "Smoke Keeper":
                    raise RuntimeError("Cached notable NPCs did not populate the monster menu")
                actions[0].trigger()
                controller.show_item("Smoke Staff")
                if overlay.npc_panel._items.get("smoke staff") != item:
                    raise RuntimeError("Cached item statistics did not reach the tooltip")
                deadline = time.monotonic() + 2
                while controller._pending and time.monotonic() < deadline:
                    app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
                if controller._pending:
                    raise RuntimeError("Cached NPC worker results did not reach the GUI")
            finally:
                controller.shutdown()
            report["npc_reference"] = {"disk_cache": True, "consider": True, "map_menu": True,
                                       "item_tooltip": True, "offline": True}
            report["stage"] = "exercise NPC columns and timed dismissal"
            from mnmparse.npc_wiki import NpcDetails

            panel = overlay.npc_panel
            clock = [100.0]
            original_clock = panel._lifetime._clock
            panel._lifetime._clock = lambda: clock[0]
            try:
                overlay.show()
                overlay.set_click_through(False)
                panel.configure(enabled=True)
                panel.set_panel_width(700)
                panel.show_npc("Smoke columns")
                panel.set_details(NpcDetails("Smoke columns", unique_drops=tuple(f"Drop {i}" for i in range(40))))
                if panel._column_count < 2 or not panel.isVisible():
                    raise RuntimeError("Packaged NPC panel did not flow into additional columns")
                panel._lifetime.set_hovered(True)
                clock[0] += 20
                panel.sync()
                if not panel.isVisible():
                    raise RuntimeError("Packaged NPC panel expired during hover")
                panel._lifetime.set_hovered(False)
                clock[0] += 5
                panel.sync()
                panel.set_status("Background result")
                if not panel._lifetime.expired or panel.isVisible():
                    raise RuntimeError("Packaged NPC panel revived after its five-second dismissal")
                panel.show_npc("Smoke columns")
                if not panel.isVisible():
                    raise RuntimeError("Packaged NPC panel did not reopen for another consider")
            finally:
                panel._lifetime._clock = original_clock
                panel._lifetime.stop()
            report["npc_reference"].update(columns=True, custom_width=True, hover_pause=True, timed_dismissal=True)
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            if engine.is_running or engine._source is not None:
                raise RuntimeError("Startup check unexpectedly started capture")
            report["windows"] = [type(window).__name__ for window in windows]
            report["pages"] = sorted(window._pages)

            report["stage"] = "exercise merged pet attribution"
            from mnmparse.app.models import build_snapshot, owner_row
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
            expected_overlay_label = "SmokeOwner + Pet"
            for table, display_label in ((window.page("live")._pane.table, expected_label),
                                         (overlay._table, expected_overlay_label)):
                model = table._model
                names = [model.data(model.index(index, model.column_index("name")), Qt.ItemDataRole.DisplayRole)
                         for index in range(model.rowCount())]
                if names.count(display_label) != 1 or "SmokePet" in names:
                    raise RuntimeError("The packaged meter did not display one combined owner row")
            report["pet_rollup"] = {"label": expected_label, "overlay_label": expected_overlay_label,
                                    "damage": owner.damage}

            report["stage"] = "exercise fresh Carebear views and export"
            from mnmparse.export import format_from_config, format_snapshot
            from mnmparse.privacy import CASUAL_LABEL, project_encounter

            casual_cfg = config.Config(player_name="SmokeOwner", start_capture_on_launch=False, minimize_to_tray=False)
            if not casual_cfg.casual_mode or casual_cfg.casual_mode_confirmed:
                raise RuntimeError("Fresh preferences must default to unconfirmed Carebear Mode")
            damage_peers = ("Falcon", "Briar")
            for peer in damage_peers:
                stats.roster.set_manual(peer, True)
                stats.add(parse_line(f"{peer} crushes a rat for 40 points of damage.", 103.0, "SmokeOwner"))
            healers = ("Iris", "Cobalt", "Velvet")
            for index, peer in enumerate(healers, 1):
                stats.roster.set_manual(peer, True)
                stats.add(parse_line(f"{peer} crushes a rat for 1 points of damage.", 103.0 + index, "SmokeOwner"))
                stats.add(parse_line(f"{peer}'s Heal heals you for {index * 100} Health.", 103.0 + index, "SmokeOwner"))
            snapshot = build_snapshot(stats, stats.current(), "SmokeOwner")
            casual_settings = QSettings(str(Path(temporary) / "casual-settings.ini"), QSettings.Format.IniFormat)
            casual_engine = Engine(casual_cfg)
            casual_overlay = OverlayWindow(casual_settings, casual_cfg)
            casual_window = main.MainWindow(casual_engine, casual_overlay, casual_cfg, casual_settings)
            windows.extend((casual_overlay, casual_window))
            casual_window.prepare_quit()
            if casual_window._mode_button.text() != CASUAL_LABEL or "Carebear" not in CASUAL_LABEL:
                raise RuntimeError("The protected mode badge is missing its Carebear label")
            casual_window.show()
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            casual_window.page("live").set_snapshot(snapshot)
            casual_overlay.set_snapshot(snapshot)
            casual_overlay._flush_snapshot()
            for table in (casual_window.page("live")._pane.table, casual_overlay._table):
                model = table._model
                labels = [str(model.data(model.index(i, model.column_index("name")), Qt.ItemDataRole.DisplayRole))
                          for i in range(model.rowCount())]
                if any(peer in label for peer in (*damage_peers, *healers) for label in labels):
                    raise RuntimeError("Fresh Carebear meter exposed a teammate identity")
                if not any("SmokeOwner" in label for label in labels):
                    raise RuntimeError("Fresh Carebear meter hid the viewer's own result")
            protected = project_encounter(snapshot, casual_cfg)
            average = next(row for row in protected.rows if not row.is_you)
            if (average.damage != 43 or average.heals != 200
                    or average.average_counts != {"damage": 3, "heals": 3}):
                raise RuntimeError("The protected role averages included the opposite role")
            if (protected.rows[0].share != owner_row(snapshot, "SmokeOwner").share
                    or abs(protected.rows[0].share + average.share - 1.0) > 1e-9):
                raise RuntimeError("The protected damage percentages were derived from averages")
            report["role_averages"] = {"damage": average.damage, "heals": average.heals,
                                       "contributors": average.average_counts}
            exported = format_snapshot(protected, format_from_config(casual_cfg))
            if any(peer in exported for peer in (*damage_peers, *healers)) or "SmokeOwner" not in exported:
                raise RuntimeError("Fresh Carebear export did not enforce the shared projection")
            report["casual_projection"] = True

            report["stage"] = "exercise personal rebuff title"
            from mnmparse.app.triggers_runtime import TriggerRunner
            from mnmparse.triggers import Trigger

            rebuff_store = TriggerStore(Path(temporary) / "rebuff-triggers.json")
            rebuff_store.triggers = [Trigger(name="Rebuff", pattern="Your Armor wears off",
                                            action="none", timer=True)]
            rebuff_runner = TriggerRunner(rebuff_store)
            try:
                rebuff_runner.set_config(casual_cfg)
                matched = rebuff_runner.observe("Your Armor wears off.", now=100)
                if (len(matched) != 1 or rebuff_runner.board.timers[0].label != "Rebuff"
                        or matched[0].trigger.name != "Rebuff" or matched[0].groups
                        or matched[0].line != "Your Armor wears off."):
                    raise RuntimeError("Carebear Mode changed the user's rebuff trigger or its source line")
                rebuff_runner.set_casual_mode(False)
                rebuff_runner.set_config(casual_cfg)
                if rebuff_runner.board.timers[0].label != "Rebuff":
                    raise RuntimeError("Mode changes lost the personal rebuff title")
                report["personal_rebuff_title"] = True
            finally:
                rebuff_runner._clock.stop()
                rebuff_runner.audio.stop()
                rebuff_runner.deleteLater()

            report["stage"] = "load bundled presets"
            store = TriggerStore()
            store.install_presets()
            names = {trigger.name for trigger in store.triggers}
            if not {"Gatekick", "Healkick", "Invis Break", "Resist"}.issubset(names):
                raise RuntimeError(f"Bundled trigger presets missing: {sorted(names)}")
            report["presets"] = sorted(names)

            report["stage"] = "exercise timer sharing"
            from mnmparse.trigger_chat import ChatShareAssembler, encode_trigger
            from mnmparse.trigger_exchange import export_trigger_code, parse_trigger_text
            from mnmparse.app.trigger_share_dialog import (
                TriggerChatExportDialog, TriggerSharePrompt, TriggerTextImportDialog,
            )

            trigger = store.triggers[0]
            code = encode_trigger(trigger)
            received = ChatShareAssembler().feed(code, sender="Smoke test")
            if len(received) != 1 or received[0].trigger.to_dict() != trigger.to_dict():
                raise RuntimeError("Packaged timer sharing did not preserve the definition")
            windows.append(TriggerChatExportDialog(trigger, code, window))
            windows.append(TriggerSharePrompt(received[0].trigger, received[0].sender, window))
            report["chat_share_chars"] = len(code)
            compact_code = export_trigger_code([trigger])
            compact_received = parse_trigger_text(compact_code)
            if len(compact_received) != 1 or compact_received[0].to_dict() != trigger.to_dict():
                raise RuntimeError("Packaged compact timer sharing did not preserve the definition")
            import_dialog = TriggerTextImportDialog(window)
            windows.append(import_dialog)
            import_dialog.text.setPlainText(compact_code)
            if not import_dialog.import_button.isEnabled():
                raise RuntimeError("The packaged timer import dialog did not accept pasted data")
            report["compact_share_chars"] = len(compact_code)
            report["trigger_text_import"] = True

            report["stage"] = "open bundled trigger guide"
            from mnmparse.app.trigger_help import TriggerHelpDialog

            guide = TriggerHelpDialog(window)
            windows.append(guide)
            guide_text = guide.browser.toPlainText()
            if "Table of contents" not in guide_text or "Righteous Smite II: 154 damage" not in guide_text:
                raise RuntimeError("The packaged trigger guide is missing its content or examples")
            report["trigger_help"] = True

            report["stage"] = "open bundled legal documents"
            from PySide6.QtCore import QUrl
            from mnmparse.app.legal_dialog import LegalDialog

            legal = LegalDialog("privacy", window)
            windows.append(legal)
            for section, expected in (("privacy", "Privacy Policy"), ("terms", "Terms"),
                                      ("open-source", "LGPL")):
                if expected not in legal.browsers[section].toPlainText():
                    raise RuntimeError(f"The packaged legal document is missing: {section}")
            legal.browsers["open-source"].anchorClicked.emit(QUrl("licenses/PNUT-MIT.txt"))
            if "MIT License" not in legal.tabs.currentWidget().toPlainText():
                raise RuntimeError("The packaged MIT license could not be opened offline")
            report["legal_documents"] = sorted(legal.browsers)

            report["stage"] = "exercise bundled image processing"
            import cv2
            import numpy as np
            from mnmparse.ocr import preprocess

            image = np.zeros((4, 4, 3), dtype=np.uint8)
            image[1, 1] = (10, 200, 30)
            processed = preprocess(image, "maxchannel", 2.0)
            encoded_ok, encoded = cv2.imencode(".png", processed)
            if (processed.shape != (8, 8, 3) or not encoded_ok
                    or not np.array_equal(cv2.imdecode(encoded, cv2.IMREAD_COLOR), processed)):
                raise RuntimeError("Bundled OpenCV image processing or PNG encoding failed")
            report["image_processing"] = True
            report["stage"] = "exercise personal mini stats and passive HP flash"
            _exercise_player_status(report, windows)
            if engine.is_running or engine._source is not None:
                raise RuntimeError("Mini-stat startup check unexpectedly started capture")
            report["window_titles"] = [item.windowTitle() for item in [*windows, overlay.timer_panel, overlay.attack_bar,
                                                                      overlay.npc_panel]]
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
        from mnmparse.storage import atomic_json
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_json(report_path, report)

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
        from mnmparse.provenance import build_info
        report["build"] = build_info()
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
