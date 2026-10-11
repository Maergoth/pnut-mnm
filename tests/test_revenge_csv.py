"""CSV exchange is literal, per-list, and merges an entire valid batch atomically."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.revenge import RevengeController, RevengeList
from mnmparse.config import Config
from mnmparse.grammar import Event
from mnmparse.revenge_csv import CsvEntry, MAX_CSV_BYTES, MAX_ENTRIES, MAX_NAME_CHARS, RevengeCsvError, format_csv, parse_csv, parse_names

SAMPLE = """Type,Name
Guild,Top Comp
Guild,Be Nice
Guild,Polar
Guild,Fyr
Guild,Full Lute
Guild,Bad Company
Player,Peedur
Player,Akias
Player,Hollieght
Player,Yami
Player,Nightshade
Player,Faceless
Player,Tooth
Player,Saosin
Player,Silky
Player,Dio
Player,Biggins
Player,Buunkah
Player,Raze
Player,Bazooka
Player,Remibaptiste
Player,Nice
"""


class RevengeCsvTests(unittest.TestCase):
    def test_user_sample_and_literal_round_trip(self):
        entries = parse_csv(SAMPLE)
        self.assertEqual(sum(entry.kind == "Guild" for entry in entries), 6)
        self.assertEqual(sum(entry.kind == "Player" for entry in entries), 16)
        for kind in ("Player", "Guild"):
            with self.subTest(kind=kind):
                literal = tuple(entry for entry in entries if entry.kind == kind) + (
                    CsvEntry('Éowyn 🦋, "quoted"', kind), CsvEntry("a named NPC", kind),
                    CsvEntry("Line\nBreak", kind), CsvEntry("[Symbols]+{name}", kind))
                self.assertEqual(parse_csv(format_csv(literal), default_kind=kind), literal)
                simple = (CsvEntry("First", kind), CsvEntry('Second, "quoted"', kind))
                self.assertEqual(format_csv(simple).rstrip("\r\n"), 'First,"Second, ""quoted"""')

    def test_header_bom_case_and_name_only_defaults(self):
        self.assertEqual(parse_csv('\ufeff nAmE , TYPE\r\n"Name, with comma",pLaYeR\r\nGuild Name,gUiLd\r\n'),
                         (CsvEntry("Name, with comma"), CsvEntry("Guild Name", "Guild")))
        self.assertEqual(parse_csv("Name\nManual name\n"), (CsvEntry("Manual name"),))
        self.assertEqual(parse_csv("Name\nGuild name\n", default_kind="Guild"), (CsvEntry("Guild name", "Guild"),))
        self.assertEqual(parse_names("Nameless\nName with symbols []\n", kind="Guild"),
                         (CsvEntry("Nameless", "Guild"), CsvEntry("Name with symbols []", "Guild")))
        self.assertEqual(parse_csv('First, Second, "Name, with comma"', default_kind="Guild"),
                         (CsvEntry("First", "Guild"), CsvEntry("Second", "Guild"), CsvEntry("Name, with comma", "Guild")))
        self.assertEqual(parse_csv("Name"), (CsvEntry("Name"),))
        self.assertEqual(parse_csv("Type,Name"), (CsvEntry("Type"), CsvEntry("Name")))
        self.assertEqual(parse_csv("First,Second\nThird,Fourth", default_kind="Guild"),
                         tuple(CsvEntry(name, "Guild") for name in ("First", "Second", "Third", "Fourth")))

    def test_malformed_or_oversized_input_is_rejected_as_a_whole(self):
        for text in ("", "Bandit,,Raider", 'Bandit,"unterminated',
                     "Type,Name\nPlayer,Bandit\nOther,Unknown", "Type,Name\nPlayer,Bandit\nPlayer,",
                     "Type,Name\nPlayer,Bandit\nPlayer,Too,Many", 'Type,Name\nPlayer,"unterminated'):
            with self.subTest(text=text):
                with self.assertRaises(RevengeCsvError):
                    parse_csv(text)
        with self.assertRaises(RevengeCsvError):
            parse_csv("Name\n" + "x" * (MAX_NAME_CHARS + 1))
        with self.assertRaises(RevengeCsvError):
            parse_csv("Name\n" + "x\n" * (MAX_ENTRIES + 1))
        with self.assertRaises(RevengeCsvError):
            parse_csv(" " * (MAX_CSV_BYTES + 1))


class RevengeCsvIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.controller = RevengeController(Config(player_name="Hero", revenge_enabled=True),
                                            Path(self.tmp.name) / "revenge.json", clock=lambda: self.now)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        for timer in (self.controller._timer, self.controller._save_timer,
                      self.controller._activity_refresh, self.controller._display_timer):
            timer.stop()
        self.controller.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_per_list_import_filters_mixed_csv_and_export_ignores_display_limits(self):
        self.assertTrue(self.controller.import_csv(SAMPLE, "Player"))
        self.assertEqual(self.controller.saved_total, 16)
        self.assertTrue(all(entry.kind == "Player" for entry in self.controller._saved.values()))
        self.assertTrue(self.controller.import_csv(SAMPLE, "Guild"))
        self.assertEqual(self.controller.saved_total, 22)
        self.controller._cfg.revenge_entries = 1
        self.assertEqual(len(self.controller.entries_for("Player")), 1)
        self.assertEqual(len(self.controller.entries_for("Guild")), 1)
        players = parse_csv(self.controller.export_csv("Player"), default_kind="Player")
        guilds = parse_csv(self.controller.export_csv("Guild"), default_kind="Guild")
        self.assertEqual(len(players), 16)
        self.assertEqual(len(guilds), 6)
        self.assertTrue(all(entry.kind == "Player" for entry in players))
        self.assertTrue(all(entry.kind == "Guild" for entry in guilds))

    def test_deduplication_and_existing_dates_survive_import_and_reload(self):
        self.controller.save_attacker("Bandit")
        original = self.controller._saved["bandit"]
        self.now += 100
        self.assertTrue(self.controller.import_csv("BANDIT,New,new", "Player"))
        self.assertEqual(self.controller._saved["bandit"], original)
        self.assertEqual(self.controller._saved["new"].added_at, 200)
        self.assertEqual(self.controller.saved_total, 2)
        self.assertTrue(self.controller.import_csv("Bandit", "Guild"))
        self.assertEqual(self.controller._saved[("Guild", "bandit")].kind, "Guild")
        self.controller._load()
        self.assertEqual(self.controller._saved["bandit"], original)
        self.assertEqual(self.controller.saved_total, 3)
        self.assertTrue(self.controller.remove_saved("Bandit", kind="Guild"))
        self.assertIn("bandit", self.controller._saved)
        self.assertNotIn(("Guild", "bandit"), self.controller._saved)
        rows = json.loads(self.controller.path.read_text())["entries"]
        self.assertTrue(all(set(row) == {"name", "added_at", "last_activity"} for row in rows))

    def test_guild_reminder_never_alerts_or_suppresses_player_candidate(self):
        self.assertTrue(self.controller.import_csv("Type,Name\nGuild,Bandit\n", "Guild"))
        self.assertEqual(self.controller.observe_saved("Bandit hits Hero."), ())
        self.assertTrue(self.controller.observe(Event(self.now, "melee_hit", "", actor="Bandit", target="Hero")))
        self.assertTrue(self.controller.save_attacker("Bandit", from_prompt=True))
        self.assertEqual(self.controller.observe_saved("Bandit hits Hero."), ("Bandit",))

    def test_invalid_batch_capacity_or_failed_write_never_partially_imports(self):
        self.controller.save_attacker("Original")
        before = self.controller.path.read_bytes()
        for text in ("Type,Name\nPlayer,New\nWrong,Other", "New,,Other"):
            self.assertFalse(self.controller.import_csv(text, "Player"))
            self.assertEqual(self.controller.path.read_bytes(), before)
            self.assertEqual(self.controller.saved_names, ("Original",))
        with patch("mnmparse.app.revenge.atomic_json", side_effect=PermissionError("Locked")):
            self.assertFalse(self.controller.import_csv("Type,Name\nPlayer,New", "Player"))
        self.assertEqual(self.controller.path.read_bytes(), before)
        self.assertEqual(self.controller.saved_names, ("Original",))
        with patch("mnmparse.app.revenge.MAX_SAVED_NAMES", 1):
            self.assertFalse(self.controller.import_csv("Type,Name\nPlayer,New", "Player"))
        self.assertEqual(self.controller.path.read_bytes(), before)

    def test_clipboard_preview_requires_add_and_selects_only_chosen_list(self):
        widget = RevengeList(self.controller, compact=True)
        self.widgets.append(widget)
        QApplication.clipboard().setText(SAMPLE)
        widget._open_manual()
        dialog = widget._manual_dialog
        dialog.from_clipboard.click()
        self.assertEqual(self.controller.saved_total, 0)
        self.assertIn("16 player", dialog.summary.text())
        self.assertIn("6 other", dialog.summary.text())
        dialog.add_button.click()
        self.assertIsNone(widget._manual_dialog)
        self.assertEqual(self.controller.saved_total, 16)
        widget._open_guild_manual()
        dialog = widget._manual_dialog
        dialog.from_clipboard.click()
        self.assertIn("6 guild", dialog.summary.text())
        self.assertIn("16 other", dialog.summary.text())
        dialog.reject()
        self.assertEqual(self.controller.saved_total, 16)
        self.assertEqual(dialog.textValue(), "")
        widget._export_csv()
        self.assertEqual(len(parse_csv(QApplication.clipboard().text())), 16)
        self.assertEqual(widget.status.text(), "Player CSV copied.")
        self.assertFalse(widget.status.isHidden())
        widget._export_guild_csv()
        self.assertEqual(widget.guild_status.text(), "Guild CSV copied.")
        self.assertFalse(widget.guild_status.isHidden())
        widget.refresh()
        self.assertTrue(widget.status.isHidden())
        self.assertEqual(widget.guild_status.text(), "No guilds to show.")


if __name__ == "__main__":
    unittest.main()
