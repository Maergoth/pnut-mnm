"""Casual projections protect every nested field, not just visible actor names."""
from __future__ import annotations
import dataclasses
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QDialog, QCheckBox, QPushButton
from PySide6.QtCore import QSettings, QTimer
from mnmparse.config import Config, load_config, save_config
from mnmparse.app.models import build_snapshot
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.session import SessionStats
from mnmparse.privacy import project_encounter, project_session, safe_event_text, casual_enabled

FULL = Config(player_name="Mitch", casual_mode=False, casual_mode_confirmed=True)
CASUAL = Config(player_name="Mitch")

def encounter(group=3):
    stats = Stats(player_name="Mitch")
    for member in ["Tamsin", "Vesper"][:group - 1]:
        stats.roster.set_manual(member, True)
    stats.roster.set_pet_owner("Sprout", "Mitch")
    for index, text in enumerate([
        "You crush a rat for 120 points of damage.",
        "Sprout bites a rat for 17 points of damage.",
        "Tamsin crushes a rat for 54321 points of damage.",
        "Vesper crushes a rat for 12345 points of damage.",
        "Outsider crushes a rat for 99999 points of damage."]):
        stats.add(parse_line(text, 100 + index, "Mitch"))
    return build_snapshot(stats, stats.current(), "Mitch")

class CasualProjectionTests(unittest.TestCase):
    def test_new_and_old_configuration_default_on(self):
        self.assertTrue(casual_enabled(Config()))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"casual_mode":false}', encoding="utf-8")
            self.assertTrue(load_config(str(path)).casual_mode)
            save_config(FULL, str(path))
            self.assertFalse(casual_enabled(load_config(str(path))))

    def test_encounter_keeps_self_pets_and_rounded_average_only(self):
        raw = encounter()
        safe = project_encounter(raw, CASUAL)
        encoded = json.dumps(dataclasses.asdict(safe))
        for secret in ("Tamsin", "Vesper", "Outsider", "54321", "12345", "99999"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(safe.rows[0].damage, 137)
        self.assertEqual(safe.group_average_count, 3)
        self.assertEqual(len(safe.rows), 2)
        self.assertEqual(safe.total_damage, 137)
        self.assertEqual(raw.rows[0].damage, 99999)  # raw collection was not changed
        self.assertIs(project_encounter(safe, CASUAL), safe)
        self.assertIs(project_encounter(raw, FULL), raw)

    def test_small_or_unknown_group_never_exposes_average(self):
        for group in (1, 2):
            safe = project_encounter(encounter(group), CASUAL)
            self.assertEqual(safe.group_average_count, 0)
            self.assertEqual([r.name for r in safe.rows], ["Mitch"])
        safe = project_encounter(encounter(), Config())
        self.assertEqual(safe.rows, [])

    def test_session_nested_peer_data_and_feed_are_removed(self):
        stats = SessionStats(player_name="Mitch", started=100)
        lines = ["--You loot [Bone Chips] from a rat's corpse.--",
                 "--Tamsin loots [Secret Diamond] from a rat's corpse.--",
                 "Tamsin has joined the party.", "Vesper has joined the party.",
                 "Your party member Tamsin has slain a rat!",
                 "Tamsin has been slain by a rat!"]
        for index, text in enumerate(lines):
            stats.add(parse_line(text, 101 + index, "Mitch"))
        safe = project_session(stats.snapshot(110), CASUAL)
        encoded = json.dumps(dataclasses.asdict(safe))
        self.assertNotIn("Tamsin", encoded)
        self.assertNotIn("Vesper", encoded)
        self.assertNotIn("Secret Diamond", encoded)
        self.assertEqual(safe.party, ["Mitch"])
        self.assertEqual(safe.items, 1)

    def test_unknown_and_peer_raw_text_fail_closed(self):
        peer = parse_line("Tamsin crushes a rat for 54321 points of damage.", 100, "Mitch")
        self.assertIsNone(safe_event_text(peer, CASUAL))
        own = parse_line("Your Heal heals Tamsin for 57 Health.", 100, "Mitch")
        self.assertEqual(safe_event_text(own, CASUAL), "You healed (57)")
        unknown = parse_line("Secret: Tamsin did 54321", 100, "Mitch")
        self.assertIsNone(safe_event_text(unknown, CASUAL))

class CasualUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_files_and_existing_ui_are_scrubbed_on_mode_change(self):
        from mnmparse.app.pages import LivePage, FeedPage, export_csv, export_json
        from mnmparse.app.overlay import OverlayWindow
        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "settings.ini"), QSettings.Format.IniFormat)
            page = LivePage(SimpleNamespace(), FULL, settings)
            overlay = OverlayWindow(settings, FULL)
            raw = encounter()
            page.set_snapshot(raw)
            overlay.set_snapshot(raw)
            overlay._flush_snapshot()
            page.set_config(CASUAL)
            overlay.set_config(CASUAL)
            encoded = json.dumps(dataclasses.asdict(page.selected()))
            self.assertNotIn("Tamsin", encoded)
            self.assertNotIn("Tamsin", json.dumps(dataclasses.asdict(overlay._snap)))
            for fmt, function in (("csv", export_csv), ("json", export_json)):
                path = Path(tmp) / f"export.{fmt}"
                function(raw, path, CASUAL)
                self.assertNotIn("Tamsin", path.read_text(encoding="utf-8"))
            feed = FeedPage(None, FULL, settings)
            feed.append("Tamsin crushes a rat for 54321 points of damage.", "melee_hit", False, 100)
            feed.set_config(CASUAL)
            feed.copy_all()
            self.assertNotIn("Tamsin", self.app.clipboard().text())
            page.close(); overlay.close(); feed.close()

    def test_switch_rejection_and_pledge_acceptance(self):
        from mnmparse.app.morality import MoralityPanel
        panel = MoralityPanel(dataclasses.replace(CASUAL, reduced_motion=True))
        requests = []
        panel.mode_requested.connect(requests.append)
        panel.request_toggle()
        self.assertTrue(panel.switch.isChecked())
        QTimer.singleShot(0, lambda: panel._dialog.reject() if panel._dialog else None)
        self.app.processEvents()
        self.assertEqual(requests, [])
        panel._pending = True
        def accept():
            dialog = panel._dialog
            dialog.findChild(QCheckBox, "moralityPledge").setChecked(True)
            dialog.findChild(QPushButton, "moralityAgree").click()
        QTimer.singleShot(0, accept)
        panel._confirm()
        self.assertEqual(requests, [False])
        panel.set_config(FULL)
        panel.request_toggle()
        self.assertEqual(requests, [False, True])
        panel.close()

    def test_casual_diagnostics_omit_ocr_pixels_and_peer_fields(self):
        from mnmparse.diagnostics import write_ocr_diagnosis
        fake = SimpleNamespace(config=CASUAL,
            ocr_diagnosis=lambda: {"saved_settings":dataclasses.asdict(CASUAL),
                "effective_settings":dataclasses.asdict(CASUAL), "recent_messages":[{"text":"Tamsin 54321"}],
                "ocr_rows":[{"text":"Tamsin"}], "limitations":[]},
            grab_frame=lambda: self.fail("Casual diagnostics must not capture images"))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_ocr_diagnosis(Path(tmp) / "safe.zip", fake, CASUAL)
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(archive.namelist(), ["report.json"])
                self.assertNotIn("Tamsin", archive.read("report.json").decode())

if __name__ == "__main__":
    unittest.main()
