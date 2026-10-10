"""Unavailable role averages stay distinct from measured zero across presentation."""
from __future__ import annotations

import csv
import dataclasses
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.models import ActorRow, EncounterSnapshot
from mnmparse.app.pages import _DetailsPanel, export_csv, export_json
from mnmparse.app.widgets import SORT_ROLE, _MeterModel, _cell_tooltip, _row_tooltip, zone_tooltip
from mnmparse.config import Config
from mnmparse.export import ExportFormat, PRESETS, format_snapshot
from mnmparse.privacy import AVERAGE_UNAVAILABLE_TEXT


def average(*, damage_available: bool = False, heals_available: bool = True) -> ActorRow:
    return ActorRow(
        name="Group average (6)", damage=120 if damage_available else 0,
        dps=12.0 if damage_available else 0.0, taken=40, dtps=4.0,
        heals=300 if heals_available else 0, hps=30.0 if heals_available else 0.0,
        healed=20, swings=0, hits=0, misses=0, hit_pct=0.0, max_hit=0,
        avg_hit=0.0, share=0.0, color="#86b998", is_you=False, is_npc=False,
        is_pet=False, average_counts={"damage": 3 if damage_available else 0,
                                    "heals": 3 if heals_available else 0},
    )


def snapshot(row: ActorRow) -> EncounterSnapshot:
    return EncounterSnapshot(
        key="100.000", label="Your encounter", start=100.0, end=110.0,
        duration=10.0, active_duration=10.0, closed=True, event_count=0,
        total_damage=0, raid_dps=0.0, killed=[], rows=[row], privacy_mode="casual",
    )


class RoleAveragePresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def test_meter_shows_dash_without_changing_numeric_sort(self) -> None:
        row = average()
        model = _MeterModel()
        model.set_rows([row])
        for metric, key in (("damage", "damage"), ("damage", "dps"),
                            ("overview", "dps")):
            model.set_metric(metric)
            index = model.index(0, model.column_index(key))
            self.assertEqual(index.data(), "—")
            self.assertEqual(index.data(SORT_ROLE), 0)
            self.assertIn(AVERAGE_UNAVAILABLE_TEXT, index.data(Qt.ItemDataRole.ToolTipRole))
        model.set_metric("healing")
        self.assertEqual(model.index(0, model.column_index("heals")).data(), "300")
        model.set_rows([dataclasses.replace(row, average_counts={})])
        model.set_metric("damage")
        self.assertEqual(model.index(0, model.column_index("damage")).data(), "0")

    def test_tooltips_and_inspector_keep_unavailable_distinct_from_zero(self) -> None:
        row = average()
        self.assertNotIn("Damage 0", _row_tooltip(row))
        self.assertIn("Damage —", _row_tooltip(row))
        self.assertEqual(_cell_tooltip(row, "damage").count(AVERAGE_UNAVAILABLE_TEXT), 1)
        self.assertIn("—", _cell_tooltip(row, "overview"))
        self.assertIn(AVERAGE_UNAVAILABLE_TEXT, zone_tooltip(
            row, zone="", fights=1, combat_s=10.0))
        panel = _DetailsPanel()
        try:
            panel.set_actor(row)
            for key in ("damage", "dps"):
                self.assertEqual(panel._cells[key].text(), "—")
                self.assertEqual(panel._chip_widgets[key].toolTip(), AVERAGE_UNAVAILABLE_TEXT)
            self.assertIn("300", panel._cells["heals"].text())
            panel.set_actor(dataclasses.replace(row, average_counts={}))
            self.assertEqual(panel._cells["damage"].text(), "0")
            self.assertEqual(panel._chip_widgets["damage"].toolTip(), "")
            panel.set_actor(average(damage_available=True, heals_available=False))
            self.assertEqual(panel._cells["heals"].text(), "—")
        finally:
            panel.deleteLater()

    def test_clipboard_templates_render_dash_with_and_without_format_specs(self) -> None:
        snap = snapshot(average())
        self.assertIn("Group average (6) —", format_snapshot(snap, PRESETS["DPS"]))
        fmt = ExportFormat("{actors}", "{name}|{damage:,}|{dps:.0f}|{heal:,}|{hps:.0f}")
        self.assertEqual(format_snapshot(snap, fmt), "Group average (6)|—|—|300|30")
        snap = snapshot(average(damage_available=True, heals_available=False))
        self.assertEqual(format_snapshot(snap, fmt), "Group average (6)|120|12|—|—")

    def test_file_exports_encode_missing_values_and_leave_source_numeric(self) -> None:
        row = average()
        snap = snapshot(row)
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = export_csv(snap, Path(tmp) / "fight.csv")
            json_path = export_json(snap, Path(tmp) / "fight.json")
            with csv_path.open(encoding="utf-8", newline="") as stream:
                encoded_csv = next(csv.DictReader(stream))
            encoded_json = json.loads(json_path.read_text(encoding="utf-8"))["rows"][0]
            for key in ("damage", "dps"):
                self.assertEqual(encoded_csv[key], "")
                self.assertIsNone(encoded_json[key])
            self.assertEqual(encoded_csv["heals"], "300")
            self.assertEqual(encoded_json["heals"], 300)
            self.assertEqual(encoded_json["average_counts"], {"damage": 0, "heals": 3})
        self.assertEqual(row.damage, 0)
        self.assertEqual(row.dps, 0.0)

    def test_cli_summary_does_not_report_suppressed_average_as_zero(self) -> None:
        from mnmparse.cli import _render_encounter

        with patch("mnmparse.app.models.build_snapshot", return_value=snapshot(average())):
            text = _render_encounter(object(), object(), Config(player_name="Mogmo"))
        average_line = next(line for line in text.splitlines() if "Group average" in line)
        self.assertEqual(average_line.count("—"), 2)
        self.assertIn("300", average_line)


if __name__ == "__main__":
    unittest.main()
