"""Attributed pets occupy one owner row without losing controls or breakdowns."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QMenu

from mnmparse.app.models import build_snapshot, snapshot_rows_for_tab
from mnmparse.app.overlay import OverlayWindow, add_pet_entries
from mnmparse.app.pages import _MeterPane, export_csv, export_json
from mnmparse.app.widgets import MeterTable, _cell_tooltip, _row_tooltip, actor_display_name, zone_tooltip
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def attributed_fight(owner: str = PLAYER, *, two_pets: bool = False, pet_only: bool = False):
    stats = Stats(player_name=PLAYER)
    lines = ["Tamsin has joined the party."]
    if not pet_only:
        lines.extend(["You crush a rat for 20 points of damage.",
                      "Tamsin crushes a rat for 10 points of damage."])
    lines.append("Kulepu slashes a rat for 30 points of damage.")
    if two_pets:
        lines.append("Ralu bites a rat for 15 points of damage.")
    stats.roster.set_pet_owner("Kulepu", owner)
    if two_pets:
        stats.roster.set_pet_owner("Ralu", owner)
    for offset, line in enumerate(lines):
        stats.add(parse_line(line, 100 + offset, PLAYER))
    return build_snapshot(stats, stats.current(), PLAYER, now=106)


class PetRollupUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.widgets = []

    def tearDown(self) -> None:
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def keep(self, widget):
        self.widgets.append(widget)
        return widget

    def test_app_and_overlay_table_show_owner_with_combined_numbers(self) -> None:
        snap = attributed_fight()
        for compact in (False, True):
            table = self.keep(MeterTable(compact=compact))
            table.set_rows(snapshot_rows_for_tab(snap, "damage"), "damage")
            model = table._model
            self.assertEqual({row.name for row in model._rows}, {PLAYER, "Tamsin"})
            index = next(i for i, row in enumerate(model._rows) if row.name == PLAYER)
            label = model.data(model.index(index, model.column_index("name")))
            self.assertEqual(label, f"{PLAYER} + {PLAYER}'s Pet")
            self.assertEqual(model.data(model.index(index, model.column_index("damage"))), "50")
            source = model.index(index, 0)
            table.view.selectRow(table._proxy.mapFromSource(source).row())
            self.assertEqual(table.selected_row().name, PLAYER, "selection retains the owner's identity")

    def test_clicking_combined_owner_opens_full_breakdown_and_survives_clear(self) -> None:
        pane = self.keep(_MeterPane("test", self.settings))
        snap = attributed_fight("Tamsin")
        pane.set_snapshot(snap)
        model = pane.table._model
        index = next(i for i, row in enumerate(model._rows) if row.name == "Tamsin")
        pane.table.view.selectRow(pane.table._proxy.mapFromSource(model.index(index, 0)).row())
        pane._on_row_selected(pane.table.selected_row())
        self.assertEqual(pane._selected_name, "Tamsin")
        self.assertEqual(pane.details._name.text(), "Tamsin + Tamsin's Pet")
        self.assertEqual(pane.details._cells["damage"].text(), "40")
        self.assertIn("Kulepu", pane.details._role.text())
        skills = [pane.details._skills.item(i, 0).text() for i in range(pane.details._skills.rowCount())]
        self.assertTrue(any(skill.startswith("Kulepu: ") for skill in skills))
        no_pet = replace(snap, rows=[replace(row, attributed_pets=[]) for row in snap.rows])
        pane.set_snapshot(no_pet)
        self.assertEqual(pane.details._name.text(), "Tamsin")
        self.assertEqual(pane._selected_name, "Tamsin")

    def test_owner_menu_reassigns_and_clears_each_included_pet_including_yours(self) -> None:
        for owner in (PLAYER, "Tamsin"):
            snap = attributed_fight(owner, two_pets=True)
            row = next(row for row in snap.rows if row.name == owner)
            menu = self.keep(QMenu())
            calls = []
            handlers = add_pet_entries(menu, row, snap, lambda *args: calls.append(args))
            top = next(action.menu() for action in menu.actions() if action.menu() is not None)
            self.assertEqual(top.title(), "Manage included pets")
            self.assertEqual({action.text() for action in top.actions()}, {"Kulepu", "Ralu"})
            for action in top.actions():
                pet = action.text()
                choices = action.menu().actions()
                assigned = next(choice for choice in choices if choice.text() == owner)
                self.assertTrue(assigned.isChecked())
                other = "Tamsin" if owner == PLAYER else PLAYER
                reassign = next(choice for choice in choices if choice.text() == other)
                handlers[reassign]()
                clear = next(choice for choice in choices if choice.text() == "Clear pet assignment")
                handlers[clear]()
                self.assertEqual(calls[-2:], [(pet, other), (pet, None)])

    def test_unassigned_stub_combatant_can_still_be_assigned(self) -> None:
        snap = SimpleNamespace(rows=[SimpleNamespace(name=PLAYER, is_you=True)], group_members=[PLAYER, "Wenna"])
        row = SimpleNamespace(name="a charmed rat", is_npc=True)
        menu = self.keep(QMenu())
        calls = []
        handlers = add_pet_entries(menu, row, snap, lambda *args: calls.append(args))
        action = next(action for action in handlers if action.text() == "Wenna")
        handlers[action]()
        self.assertEqual(calls, [("a charmed rat", "Wenna")])
        self.assertEqual(actor_display_name(row), "a charmed rat")

    def test_tooltips_and_csv_name_the_combined_parse_json_keeps_identity(self) -> None:
        snap = attributed_fight(two_pets=True)
        row = next(row for row in snap.rows if row.name == PLAYER)
        label = f"{PLAYER} + {PLAYER}'s Pets"
        self.assertIn(label, _row_tooltip(row))
        self.assertIn("Maergoth + Maergoth&#x27;s Pets", _cell_tooltip(row, "damage"))
        self.assertIn("65", _cell_tooltip(row, "damage"))
        self.assertIn("Maergoth + Maergoth&#x27;s Pets", zone_tooltip(row, zone="Night Harbor", fights=1, combat_s=6))
        csv_path = export_csv(snap, Path(self.tmp.name) / "parse.csv")
        with csv_path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        owner = next(item for item in rows if item["name"] == label)
        self.assertEqual(owner["damage"], "65")
        self.assertFalse(any(item["name"] in ("Kulepu", "Ralu") for item in rows))
        json_path = export_json(snap, Path(self.tmp.name) / "parse.json")
        data = json.loads(json_path.read_text(encoding="utf-8"))
        owner = next(item for item in data["rows"] if item["name"] == PLAYER)
        self.assertEqual(owner["attributed_pets"], ["Kulepu", "Ralu"])
        self.assertEqual(owner["damage"], 65)

    def test_overlay_zone_hover_counts_pet_only_fights_for_owner(self) -> None:
        overlay = self.keep(OverlayWindow(self.settings, Config(player_name=PLAYER)))
        overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        first = replace(attributed_fight(pet_only=True), key="one", closed=True, zone="Night Harbor", zone_since=90)
        second = replace(attributed_fight(), key="two", closed=True, zone="Night Harbor", zone_since=90)
        overlay.set_history([first, second])
        overlay.set_snapshot(second)
        overlay._flush_snapshot()
        row = next(row for row in second.rows if row.name == PLAYER)
        tooltip = overlay._name_tooltip(row)
        self.assertIn("in 2 fights", tooltip)
        self.assertIn("80", tooltip)
        self.assertIn("Maergoth + Maergoth&#x27;s Pet", tooltip)
        overlay.copy_current()


if __name__ == "__main__":
    unittest.main()
