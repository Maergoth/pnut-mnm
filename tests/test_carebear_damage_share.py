"""Carebear percentages use actual group damage, independent of rounded averages."""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.models import ActorRow, EncounterSnapshot
from mnmparse.app.pages import _DetailsPanel, export_csv, export_json
from mnmparse.app.widgets import MeterTable, SORT_ROLE, _cell_tooltip
from mnmparse.config import Config
from mnmparse.export import ExportFormat, format_snapshot
from mnmparse.privacy import project_encounter


CFG = Config(player_name="Owner")


def encounter() -> EncounterSnapshot:
    rows = [ActorRow(
        name=name, damage=damage, dps=damage / 10, taken=0, dtps=0,
        heals=heals, hps=heals / 10, healed=0, swings=0, hits=0,
        misses=0, hit_pct=0, max_hit=0, avg_hit=0, share=damage / 1000,
        color="#ffffff", is_you=name == "Owner", is_npc=False, is_pet=False,
    ) for name, damage, heals in (
        ("Owner", 250, 0), ("DamageTwo", 100, 0), ("DamageThree", 100, 0),
        ("Healer", 550, 1000),
    )]
    return EncounterSnapshot(
        key="100", label="Enemy", start=100, end=110, duration=10,
        active_duration=10, closed=True, event_count=40, total_damage=1000,
        raid_dps=100, killed=[], rows=rows, group_members=[row.name for row in rows],
    )


class CarebearDamageShareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_main_and_compact_damage_meters_keep_actual_shares(self):
        raw = encounter()
        safe = project_encounter(raw, CFG)
        self.assertEqual((safe.rows[0].damage, safe.rows[1].damage), (250, 150))
        for compact in (False, True):
            with self.subTest(compact=compact):
                table = MeterTable(compact=compact)
                try:
                    table.set_rows(safe.rows, metric="damage")
                    share_column = table._model.column_index("share")
                    own = table._model.index(0, share_column)
                    rest = table._model.index(1, share_column)
                    self.assertEqual(own.data(), "25.0%")
                    self.assertEqual(rest.data(), "75.0%")
                    self.assertEqual(own.data(SORT_ROLE), .25)
                    self.assertEqual(rest.data(SORT_ROLE), .75)
                    table.set_sort("share", True)
                    self.assertEqual(table._proxy.index(0, share_column).data(), "75.0%")
                    self.assertEqual(table._proxy.index(1, share_column).data(), "25.0%")
                finally:
                    table.deleteLater()
        self.assertEqual(raw.total_damage, 1000)
        self.assertEqual(raw.rows[0].share, .25)

    def test_details_and_tooltips_report_own_and_remaining_shares(self):
        safe = project_encounter(encounter(), CFG)
        panel = _DetailsPanel()
        try:
            for row, expected in zip(safe.rows, ("25.0%", "75.0%")):
                panel.set_actor(row)
                self.assertEqual(panel._cells["share"].text(), expected)
                self.assertIn(expected, _cell_tooltip(row, "share"))
            self.assertIn("Remaining group damage", panel._chip_widgets["share"].toolTip())
        finally:
            panel.deleteLater()

    def test_remaining_share_is_visible_when_role_average_is_unavailable(self):
        safe = project_encounter(encounter(), CFG)
        average = dataclasses.replace(safe.rows[1], damage=0, dps=0,
                                      average_counts={"damage": 0, "heals": 0})
        table = MeterTable()
        try:
            table.set_rows([safe.rows[0], average], metric="damage")
            share = table._model.index(1, table._model.column_index("share"))
            damage = table._model.index(1, table._model.column_index("damage"))
            self.assertEqual(damage.data(), "—")
            self.assertEqual(share.data(), "75.0%")
            self.assertIn("75.0%", share.data(Qt.ItemDataRole.ToolTipRole))
            self.assertNotIn("Damage 0", share.data(Qt.ItemDataRole.ToolTipRole))
        finally:
            table.deleteLater()

    def test_custom_clipboard_and_file_exports_preserve_actual_shares(self):
        raw = encounter()
        safe = project_encounter(raw, CFG)
        fmt = ExportFormat("{actors}", "{name}|{damage}|{share:.0f}%", separator=" ; ")
        self.assertEqual(format_snapshot(safe, fmt),
                         "Owner|250|25% ; Group average (4)|150|75%")
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = export_csv(raw, Path(tmp) / "fight.csv", CFG)
            json_path = export_json(raw, Path(tmp) / "fight.json", CFG)
            with csv_path.open(encoding="utf-8", newline="") as stream:
                csv_rows = list(csv.DictReader(stream))
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))["rows"]
            self.assertEqual([float(row["share"]) for row in csv_rows], [.25, .75])
            self.assertEqual([row["share"] for row in json_rows], [.25, .75])
            self.assertNotIn("Healer", json_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
