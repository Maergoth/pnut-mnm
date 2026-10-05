"""Regression coverage for Intimidate and the October 5 unknown-line audit."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mnmparse.app.models import build_snapshot
from mnmparse.interrupts import cc_categories, load_cc_table
from mnmparse.parser import normalize_ocr, parse_line
from mnmparse.stats import Stats
from mnmparse.vocab import could_be_name


class IntimidateTests(unittest.TestCase):
    def test_intimidate_is_available_without_an_installed_cc_file(self):
        with tempfile.TemporaryDirectory() as root:
            table = load_cc_table(Path(root))
        for skill in ("Intimidate", "Intimidation", "Intimidation II"):
            self.assertIn("fear", cc_categories(skill, table))

    def test_result_and_fade_are_distinct(self):
        for subject in ("a Bloodynose frightener", "a Bloodynose frightener's pet", "You"):
            for verb, kind in (("is", "cc"), ("is no longer", "cc_fade")):
                ev = parse_line(f"{subject} {verb} intimidated.", 1, "Aster")
                self.assertEqual((ev.kind, ev.target, ev.outcome),
                                 (kind, "Aster" if subject == "You" else subject, "fear"))

    def test_landed_intimidate_counts_as_utility_but_fade_does_not(self):
        stats = Stats()
        for ts, line in enumerate([
            "Aster hits a Bloodynose frightener for 5 points of damage.",
            "Aster begins casting Intimidation.",
            "a Bloodynose frightener is intimidated.",
            "a Bloodynose frightener's pet is intimidated.",
            "a Bloodynose frightener is no longer intimidated.",
            "a Bloodynose frightener's pet is no longer intimidated.",
        ]):
            stats.add(parse_line(line, ts))
        row = next(r for r in build_snapshot(stats, stats.current(), "Aster").rows if r.name == "Aster")
        self.assertEqual((row.cc_attempts, row.utility, row.cc_types), (1, 2, {"fear": 2}))
        self.assertEqual(row.cc_skills, {"Intimidation": 2})

    def test_unattributed_result_is_not_credited_to_a_nearby_attack(self):
        stats = Stats()
        stats.add(parse_line("Aster hits a Bloodynose frightener for 5 points of damage.", 0))
        stats.add(parse_line("a Bloodynose frightener is intimidated.", 1))
        row = next(r for r in build_snapshot(stats, stats.current(), "Aster").rows if r.name == "Aster")
        self.assertEqual(row.utility, 0)


class UnknownLineAuditTests(unittest.TestCase):
    def test_npc_pets_keep_their_own_identity(self):
        pet = "a Bloodynose frightener's pet"
        for line, kind, actor, target in [
            (f"{pet} hits YOU for 4 points of damage.", "melee_hit", pet, "Aster"),
            (f"{pet} tries to hit YOU, but misses!", "melee_miss", pet, "Aster"),
            (f"Aster's Strike hits {pet} for 3 points of damage.", "ability_hit", "Aster", pet),
            (f"Aster tries to cast Root on {pet}, but is resisted!", "resist", "Aster", pet),
            (f"{pet}'s casting is interrupted.", "interrupt", pet, None),
        ]:
            with self.subTest(line=line):
                ev = parse_line(line, 0, "Aster")
                self.assertEqual((ev.kind, ev.actor, ev.target), (kind, actor, target))
        self.assertTrue(could_be_name("npc", pet))

    def test_multiple_hit_markers_preserve_damage_and_mitigation(self):
        for prefix, kind in (("a Bloodynose captain slashes", "melee_hit"),
                             ("a Bloodynose captain's Strike hits", "ability_hit")):
            for suffix in ("(2 absorbed). (Block 10)", "(Block 10). (2 absorbed)"):
                ev = parse_line(f"{prefix} Aster for 22 points of damage {suffix}", 0)
                self.assertEqual((ev.kind, ev.amount, ev.absorbed, ev.blocked), (kind, 22, 2, 10))
        ev = parse_line("Aster hits a bandit for 9 points of damage. (Critical). (2 absorbed). (Block 1)", 0)
        self.assertEqual((ev.amount, ev.outcome, ev.absorbed, ev.blocked), (9, "critical", 2, 1))

    def test_missing_space_after_actor(self):
        ev = parse_line("Asterbegins casting Chop.", 0)
        self.assertEqual((ev.kind, ev.actor, ev.skill), ("cast", "Aster", "Chop"))
        ev = parse_line("--Asterloots [Crocodile Tooth] from a caiman's corpse.--", 0)
        self.assertEqual((ev.kind, ev.actor, ev.item), ("loot", "Aster", "Crocodile Tooth"))
        # Only repair a message boundary supported by a cast/loot shape.
        self.assertEqual(normalize_ocr("Asterbegins hits a bandit for 5 points of damage."),
                         "Asterbegins hits a bandit for 5 points of damage.")

    def test_dispel_parenthetical(self):
        ev = parse_line("Aster dispels magic from Birch. (Root)", 0)
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.outcome), ("status", "Aster", "Birch", "root"))

    def test_noncombat_messages(self):
        for line, kind in [
            ("Aster places some more wood on the campfire. It burns brighter than before.", "status"),
            ("You cannot cast spells from that school of magicl", "personal"),
            ("You have everything you need, but something doesn't belong.", "personal"),
            ("You have everything you need, but the composition isn't quite right.", "personal"),
            ("It is locked, and you are not holding the key.", "personal"),
        ]:
            with self.subTest(line=line):
                self.assertEqual(parse_line(line, 0).kind, kind)


if __name__ == "__main__":
    unittest.main()
