"""Damage-only effect announcements are recognized without inventing damage or utility."""

from __future__ import annotations

import unittest

from mnmparse.grammar import KINDS
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.vocab import Vocabulary, observe_event


class DamageEffectTests(unittest.TestCase):
    def test_effect_announcements_have_no_damage_amount_or_unproven_actor(self) -> None:
        self.assertIn("damage_effect", KINDS)
        for text, target, outcome in (
            ("a skeletal fighter is bleeding out.", "a skeletal fighter", "bleeding"),
            ("a skeletal fighter is struck by a barbed arrow.", "a skeletal fighter", "barbed arrow"),
            ("You are bleeding out.", "Maergoth", "bleeding"),
        ):
            with self.subTest(text=text):
                event = parse_line(text, 1.0, "Maergoth")
                self.assertEqual((event.kind, event.target, event.outcome), ("damage_effect", target, outcome))
                self.assertIsNone(event.amount)
                self.assertIsNone(event.actor)

    def test_announcements_do_not_start_fights_but_join_the_next_damage(self) -> None:
        stats = Stats(player_name="Maergoth")
        announcement = parse_line("a skeletal fighter is bleeding out.", 1.0, "Maergoth")
        stats.add(announcement)
        self.assertIsNone(stats.current())
        stats.add(parse_line("Your Slice hits a skeletal fighter for 4 points of Bleed Damage.", 2.0, "Maergoth"))
        encounter = stats.current()
        self.assertIsNotNone(encounter)
        self.assertEqual(encounter.events[0], announcement)
        self.assertEqual(encounter.damage_events, 1)
        self.assertEqual(stats.actor_table(encounter)[0]["damage"], 4)

    def test_effect_announcements_still_teach_combatant_spelling(self) -> None:
        vocabulary = Vocabulary()
        observe_event(vocabulary, parse_line("a skeletal fighter is bleeding out.", 1.0, "Maergoth"))
        self.assertEqual(vocabulary._names["npc"]["a skeletal fighter"], 1)


if __name__ == "__main__":
    unittest.main()
