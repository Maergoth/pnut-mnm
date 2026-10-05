"""Clipboard export: the one-line format, the settings, the overlay button, the Live menu and
the automatic copy when a fight ends."""

from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse.config import Config
from mnmparse.export import PRESETS, ExportFormat, format_from_config, format_snapshot, preset_for, render


def _row(name: str, damage: int, *, dps: float = 0.0, share: float = 0.0, heals: int = 0, hps: float = 0.0,
         taken: int = 0, utility: int = 0, npc: bool = False) -> SimpleNamespace:
    return SimpleNamespace(name=name, damage=damage, dps=dps, share=share, max_hit=damage // 10, hit_pct=72.6,
                           heals=heals, hps=hps, taken=taken, utility=utility, is_npc=npc)


def _snap(**kw) -> SimpleNamespace:
    rows = [
        _row("Maergoth", 1200, dps=24.0, share=0.4, taken=300, utility=2),
        _row("Brannoc", 1500, dps=30.0, share=0.5, taken=900, utility=5),
        _row("Tamsin", 300, dps=6.0, share=0.1, heals=900, hps=18.0),
        _row("a skeletal knight", 700, dps=14.0, taken=3000, npc=True),
    ]
    base = dict(label="a skeletal knight", zone="Wyrmsbane Tomb", duration=50.0, start=time.time(),
                total_damage=3000, raid_dps=60.0, killed=["a skeletal knight"], encounters=1, rows=rows,
                ours=True, key="1.000", closed=True)
    base.update(kw)
    return SimpleNamespace(**base)


class FormatTests(unittest.TestCase):
    def test_default_preset(self) -> None:
        text = format_snapshot(_snap(), PRESETS["DPS"])
        self.assertEqual(text, "a skeletal knight [0:50] 60.0 DPS - Brannoc 30.0, Maergoth 24.0, Tamsin 6.0")

    def test_enemies_are_never_listed_and_the_measure_decides_who_is(self) -> None:
        healing = format_snapshot(_snap(), PRESETS["Healing"])
        self.assertEqual(healing, "a skeletal knight [0:50] healing - Tamsin 18.0 HPS")
        self.assertNotIn("skeletal knight 14", format_snapshot(_snap(), PRESETS["Everything"]))

    def test_fields_specs_and_unknown_fields(self) -> None:
        fmt = ExportFormat("{title}|{dps:.0f}|{nope}|{kills} {killed}|{actors}", "#{rank} {name} {share}% {dps:x}", ";", "damage", 2)
        self.assertEqual(format_snapshot(_snap(), fmt), "a skeletal knight|60|{nope}|1 a skeletal knight|#1 Brannoc 50% 30.0;#2 Maergoth 40% 24.0")

    def test_always_one_line(self) -> None:
        fmt = ExportFormat("{title}\n\n{actors}", "{name}\t{dps}", "\n")
        self.assertEqual(format_snapshot(_snap(), fmt), "a skeletal knight Brannoc 30.0 Maergoth 24.0 Tamsin 6.0")

    def test_render_formats_numbers(self) -> None:
        self.assertEqual(render("{a} {b} {c:,} {d}", {"a": 12345, "b": 2.25, "c": 9999, "d": "x"}), "12,345 2.2 9,999 x")
        self.assertEqual(render("{{a}}", {"a": 1}), "{1}")

    def test_preset_names_and_config(self) -> None:
        self.assertEqual(preset_for(PRESETS["Healing"]), "Healing")
        self.assertEqual(preset_for(ExportFormat("{title}", "{name}")), "Custom")
        self.assertEqual(format_from_config(Config()), PRESETS["DPS"])
        cfg = dataclasses.replace(Config(), export_sort="mana", export_max_actors=0, export_line="a\nb")
        problems = " ".join(cfg.problems())
        self.assertIn("export_sort", problems)
        self.assertIn("export_max_actors", problems)
        self.assertIn("one line", problems)
        self.assertEqual(format_from_config(cfg).max_actors, 1)


@unittest.skipUnless(sys.platform == "win32", "Qt is exercised on the Windows build")
class ExportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def _settings(self, tmp: str):
        from PySide6.QtCore import QSettings

        return QSettings(str(Path(tmp) / "s.ini"), QSettings.Format.IniFormat)

    def test_overlay_copy_glyph_and_flash(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            ov = OverlayWindow(self._settings(tmp), Config())
            for w in (ov, ov.attack_bar, ov.timer_panel):
                w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            got = []
            ov.copy_requested.connect(got.append)
            try:
                ov.resize(420, 260)
                ov.show()
                snap = _snap()
                ov.set_snapshot(snap)
                ov._flush_snapshot()
                header = ov._header
                header.grab()  # paint once: lays out the glyph
                self.assertFalse(header._copy_rect.isEmpty())
                centre = header._copy_rect.center().toPoint()
                QTest.mouseClick(header, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
                self.assertEqual(got, [snap])
                ov.flash_copied()
                self.assertGreater(header._copied_until, time.monotonic())
                header.grab()
                from PySide6.QtCore import QPointF

                self.assertFalse(header._on_copy(QPointF(5, 5)), "the rest of the header is not the copy glyph")
                self.assertTrue(header._on_copy(header._copy_rect.center()))
                self.assertEqual(len(got), 1)
            finally:
                ov.close()
                ov.deleteLater()

    def test_live_menu_resolves_fights_and_zone_summaries(self) -> None:
        from PySide6.QtCore import Qt

        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import LivePage
        try:
            from tests.test_app_widgets import _actor, _snapshot
        except ImportError:  # discovered with -s tests
            from test_app_widgets import _actor, _snapshot

        with tempfile.TemporaryDirectory() as tmp:
            page = LivePage(_MissingEngine(Config()), Config(), self._settings(tmp))
            page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            page.show()
            try:
                first = _snapshot(100.0, [_actor("Brannoc", 34)])
                page.add_encounter(first)
                zone = page._tree.topLevelItem(0)
                self.assertIs(page.snapshot_for_item(zone.child(0)), page._snaps[first.key])
                summary = page.snapshot_for_item(zone)
                self.assertIsNotNone(summary)
                self.assertEqual(summary.total_damage, first.total_damage)
                self.assertIsNone(page.snapshot_for_item(None))
            finally:
                page.deleteLater()

    def test_settings_presets_custom_and_round_trip(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import SettingsPage

        with tempfile.TemporaryDirectory() as tmp:
            page = SettingsPage(_MissingEngine(Config()), Config(player_name="Maergoth"), self._settings(tmp))
            try:
                self.assertEqual(page.export_preset.currentText(), "DPS")
                self.assertIn("Maergoth 24.1", page.export_preview.text())
                page.export_preset.setCurrentText("Healing")
                self.assertEqual(page.export_actor.text(), PRESETS["Healing"].actor)
                self.assertTrue(page.export_preview.text().startswith("a skeletal knight [0:48] healing - Tamsin"))
                page.export_actor.setText("{name}={dps:.0f}")
                self.assertEqual(page.export_preset.currentText(), "Custom")
                page.export_sound.setCurrentText("None")
                cfg = page.form_config()
                self.assertEqual((cfg.export_actor, cfg.export_sort, cfg.export_sound), ("{name}={dps:.0f}", "healing", ""))
                page.load(cfg)
                self.assertEqual(page.export_preset.currentText(), "Custom")
                self.assertEqual(page.export_sound.currentText(), "None")
            finally:
                page.deleteLater()

    def test_finished_fight_is_copied_only_when_ours(self) -> None:
        from PySide6.QtGui import QGuiApplication

        from mnmparse.app.main import App

        app = SimpleNamespace(cfg=Config(), overlay=None, window=None, triggers=None)
        app.copy_snapshot = lambda snap, automatic=False: App.copy_snapshot(app, snap, automatic=automatic)
        app.play_sound = lambda name: None
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("before")
        App._on_encounter_closed_export(app, _snap(ours=False))
        self.assertEqual(clipboard.text(), "before", "another group's fight is not copied")
        App._on_encounter_closed_export(app, _snap())
        self.assertTrue(clipboard.text().startswith("a skeletal knight [0:50] 60.0 DPS - Brannoc 30.0"))
        clipboard.setText("before")
        app.cfg = dataclasses.replace(app.cfg, export_auto=False)
        App._on_encounter_closed_export(app, _snap())
        self.assertEqual(clipboard.text(), "before", "auto copy can be turned off")


if __name__ == "__main__":
    unittest.main()
