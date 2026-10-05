"""Overview-tab crowd-control score: attempts, landed effects and crediting."""

from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from mnmparse.app.models import build_snapshot, snapshot_rows_for_tab
from mnmparse.interrupts import canonical_skill, cc_categories, is_cc_skill, is_kick, load_cc_table
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def _run(lines):
    stats = Stats(encounter_timeout_s=12.0)
    for ts, text in lines:
        stats.add(parse_line(text, ts, PLAYER))
    enc = stats.current() or stats.history[-1]
    return stats, enc


class RegistryTests(unittest.TestCase):
    def test_canonical_strips_rank(self) -> None:
        self.assertEqual(canonical_skill("Shield Bash II"), "shield bash")

    def test_categories(self) -> None:
        table = load_cc_table()
        self.assertIn("interrupt", cc_categories("kick", table))
        self.assertIn("interrupt", cc_categories("Shield Bash III", table))
        self.assertIn("interrupt", cc_categories("Lesser Gust of Wind", table))
        self.assertIn("stun", cc_categories("Scintillating Shock", table))
        self.assertIn("mez", cc_categories("Mesmerize", table))
        self.assertIn("root", cc_categories("Root", table))
        self.assertEqual(cc_categories("Net Shot", table), frozenset({"interrupt", "root"}))
        self.assertFalse(is_cc_skill("Crusader Strike", table))
        self.assertFalse(is_cc_skill(None, table))
        self.assertTrue(is_kick("kick"))
        self.assertFalse(is_kick("Sweeping Kick"))

    def test_json_file_extends_defaults(self) -> None:
        table = load_cc_table()
        self.assertIn("windblast", table["interrupt"])
        self.assertIn("ogre smash", table["stun"])

    def test_built_in_registry_matches_shipped_registry_without_data_files(self) -> None:
        """A frozen install has the full shipped CC behavior without cc.json beside it."""
        shipped = json.loads((Path(__file__).resolve().parents[1] / "cc.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as root:
            built_in = load_cc_table(Path(root))
        combined = load_cc_table()
        for category, names in shipped.items():
            if category.startswith("_"):
                continue
            for name in names:
                with self.subTest(name=name, category=category):
                    self.assertIn(category, cc_categories(name, built_in))
                    self.assertEqual(cc_categories(name, built_in), cc_categories(name, combined))


class CcScoreTests(unittest.TestCase):
    def test_interrupts_credit_and_window(self) -> None:
        stats, enc = _run([
            (0.0, "You crush a skeletal cleric for 5 points of damage."),
            (0.5, "Dogabetarolem kicks a skeletal cleric."),
            (1.0, "a skeletal cleric's casting is interrupted."),
            (2.0, "Ena's Shield Slam hits a skeletal cleric for 3 points of damage."),
            (2.4, "a skeletal cleric's casting is interrupted."),
            (3.0, "You kick a skeletal cleric."),
            (9.0, "a skeletal cleric's casting is interrupted."),  # 6 s later: nobody credited
        ])
        rows = {r.name: r for r in build_snapshot(stats, enc, PLAYER).rows}
        self.assertEqual((rows["Dogabetarolem"].cc_attempts, rows["Dogabetarolem"].cc), (1, 1))
        self.assertEqual((rows["Ena"].cc_attempts, rows["Ena"].cc), (1, 1))
        self.assertEqual((rows[PLAYER].cc_attempts, rows[PLAYER].cc), (1, 0))
        self.assertEqual(rows["Dogabetarolem"].cc_types, {"interrupt": 1})

    def test_mez_stun_root_from_casts(self) -> None:
        stats, enc = _run([
            (0.0, "You crush a skeletal defender for 5 points of damage."),
            (1.0, "Palidu begins casting Mesmerize."),
            (2.5, "a skeletal defender is mesmerized."),
            (4.0, "Hokabibuve begins casting Stun."),
            (5.0, "a skeletal defender is stunned."),
            (6.0, "Mimokufu begins casting Root."),
            (7.0, "a skeletal defender is rooted to the ground."),
            (8.0, "a skeletal defender is no longer rooted."),
            (10.0, "Bitivaz begins casting Scintillating Shock."),  # area stun credits several victims
            (11.0, "a skeletal knight is stunned by scintillating lights."),
            (11.1, "a skeletal vicar is stunned by scintillating lights."),
        ])
        rows = {r.name: r for r in build_snapshot(stats, enc, PLAYER).rows}
        self.assertEqual(rows["Palidu"].cc_types, {"mez": 1})
        self.assertEqual(rows["Hokabibuve"].cc_types, {"stun": 1})
        self.assertEqual(rows["Mimokufu"].cc_types, {"root": 1})
        self.assertEqual(rows["Bitivaz"].cc, 2)
        self.assertEqual(rows["Bitivaz"].cc_attempts, 1)

    def test_npc_cc_on_players_is_credited_to_the_npc(self) -> None:
        stats, enc = _run([
            (0.0, "a skeletal cleric hits Fozo for 5 points of damage."),
            (1.0, "a skeletal cleric begins casting Stun."),
            (2.0, "Fozo is stunned."),
            (6.0, "Fozo is no longer stunned."),
        ])
        snap = build_snapshot(stats, enc, PLAYER)
        rows = {r.name: r for r in snap.rows}
        self.assertEqual(rows["a skeletal cleric"].cc, 1)
        self.assertIn("a skeletal cleric", [r.name for r in snapshot_rows_for_tab(snap, "overview")])

    def test_uppercut_counts_as_attempt_without_result(self) -> None:
        stats, enc = _run([
            (0.0, "You crush a skeletal cleric for 5 points of damage."),
            (0.5, "Pidef uppercuts a skeletal cleric."),
        ])
        rows = {r.name: r for r in build_snapshot(stats, enc, PLAYER).rows}
        self.assertEqual((rows["Pidef"].cc_attempts, rows["Pidef"].cc), (1, 0))


if __name__ == "__main__":
    unittest.main()
