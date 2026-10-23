"""Observed undead-control spells credit landed utility, not failures or expiry."""
from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot
from mnmparse.interrupts import DEFAULT_CC, cc_categories
from mnmparse.parser import parse_line
from mnmparse.stats import Stats


PLAYER = "Fictional"
MOB = "a dusty skeleton"


def personal_row(lines):
    stats = Stats(player_name=PLAYER, encounter_timeout_s=60)
    stats.add(parse_line(f"You crush {MOB} for 10 points of damage.", 100, PLAYER))
    for offset, text in lines:
        stats.add(parse_line(text, 100 + offset, PLAYER))
    snapshot = build_snapshot(stats, stats.current(), PLAYER)
    return next(row for row in snapshot.rows if row.name == PLAYER)


class UndeadControlTests(unittest.TestCase):
    def test_builtin_registry_recognizes_undead_control_and_ranked_tiers(self):
        for spell, category in (("Snare the Dead", "snare"), ("Fear the Dead", "fear")):
            for name in (spell, spell + " II", "Lesser " + spell):
                with self.subTest(name=name):
                    self.assertEqual(cc_categories(name, DEFAULT_CC), frozenset({category}))

    def test_overcome_with_fear_and_expiration_have_distinct_kinds(self):
        for target, expected in ((MOB, MOB), ("You", PLAYER)):
            with self.subTest(target=target):
                verb = "are" if target == "You" else "is"
                landed = parse_line(f"{target} {verb} overcome with fear.", 100, PLAYER)
                faded = parse_line(f"{target} {verb} no longer fearful.", 101, PLAYER)
                self.assertEqual((landed.kind, landed.target, landed.outcome), ("cc", expected, "fear"))
                self.assertEqual((faded.kind, faded.target, faded.outcome), ("cc_fade", expected, "fear"))

    def test_observed_sequence_credits_only_two_snares_and_one_successful_fear(self):
        row = personal_row([
            (1, "You begin casting Snare the Dead."),
            (3, f"{MOB} is snared."),
            (5, "You begin casting Fear the Dead."),
            (7, f"{MOB} resists your Fear the Dead!"),
            (9, "You begin casting Fear the Dead."),
            (12, f"{MOB} is overcome with fear."),
            (14, "You begin casting Snare the Dead."),
            (14.1, "Your spell fizzles!"),
            (15, f"Your Snare the Dead wears off of {MOB}."),
            (16, "You begin casting Snare the Dead."),
            (18, f"{MOB} is snared."),
            (19, f"{MOB} is no longer fearful."),
        ])
        self.assertEqual((row.utility, row.cc, row.cc_attempts), (3, 3, 5))
        self.assertEqual(row.cc_types, {"snare": 2, "fear": 1})
        self.assertEqual(row.cc_skills, {"Snare the Dead": 2, "Fear the Dead": 1})
        self.assertEqual(row.debuffs, {})

    def test_resists_fizzles_and_wear_off_do_not_earn_utility(self):
        row = personal_row([
            (1, "You begin casting Fear the Dead."),
            (3, f"{MOB} resists your Fear the Dead!"),
            (4, "You begin casting Snare the Dead."),
            (4.1, "Your spell fizzles!"),
            (5, f"Your Fear the Dead wears off of {MOB}."),
            (6, f"{MOB} is no longer fearful."),
        ])
        self.assertEqual((row.utility, row.cc, row.cc_attempts), (0, 0, 2))

    def test_landed_effect_without_a_matching_cast_is_not_assigned_to_viewer(self):
        row = personal_row([(1, f"{MOB} is snared."), (2, f"{MOB} is overcome with fear.")])
        self.assertEqual((row.utility, row.cc), (0, 0))


if __name__ == "__main__":
    unittest.main()
