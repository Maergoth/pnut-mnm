"""Shapes found in the 2026-10-02 log audit: proper-noun NPC names, spacing noise,
personal/consider/zone lines, clipped-name completion and the small-font row guard."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from mnmparse.parser import NameCompleter, parse_line
from mnmparse.tracker import Tracker


class AuditGrammarTests(unittest.TestCase):
    def test_proper_noun_npc_names(self) -> None:
        ev = parse_line("a Wyrmsbane crusader hits YOU for 3 points of damage.", 0.0, "Maergoth")
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.amount), ("melee_hit", "a Wyrmsbane crusader", "Maergoth", 3))
        ev = parse_line("a Wyrmsbane crusader's Slam hits YOU for 4 points of damage!", 0.0, "Maergoth")
        self.assertEqual((ev.kind, ev.actor, ev.skill), ("ability_hit", "a Wyrmsbane crusader", "Slam"))
        ev = parse_line("Povebizu tries to cast Interdiction on a Wyrmsbane crusader, but is resisted!", 0.0)
        self.assertEqual((ev.kind, ev.target), ("resist", "a Wyrmsbane crusader"))

    def test_space_before_comma(self) -> None:
        ev = parse_line("a skeletal warrior tries to hit YOU , but misses!", 0.0, "Maergoth")
        self.assertEqual((ev.kind, ev.outcome), ("melee_miss", "miss"))

    def test_cooldown_spam_is_personal(self) -> None:
        for text in (
            "This ability is not available right now.",
            "This ability is not available rizht now.",
            "You are already casting another ability...",
            "Your faction standing with Denizens of Wyrmsbane Tomb cannot possibly get any worse.",
            "Your skin tingles.",
        ):
            self.assertEqual(parse_line(text, 0.0).kind, "personal", text)

    def test_consider_and_zone(self) -> None:
        self.assertEqual(parse_line("a Wyrmsbane crusader seethes at you, ready to strike", 0.0).kind, "consider")
        self.assertEqual(parse_line("Would you like to die?", 0.0).kind, "consider")
        ev = parse_line("You have entered Night Harbor (East).", 0.0)
        self.assertEqual((ev.kind, ev.target), ("zone", "Night Harbor (East)"))
        self.assertEqual(parse_line("Loading, please wait...", 0.0).kind, "zone")

    def test_fused_casting_and_misreads(self) -> None:
        ev = parse_line("Povebizu bezins castinz Distress.", 0.0)
        self.assertEqual((ev.kind, ev.skill), ("cast", "Distress"))
        ev = parse_line("Gozif beginsgasting Lesser Heal.", 0.0)
        self.assertEqual((ev.kind, ev.skill), ("cast", "Lesser Heal"))
        ev = parse_line("Kulepu slashes a skeletal warrior for 4 points of damaze.", 0.0)
        self.assertEqual((ev.kind, ev.dtype), ("melee_hit", "damage"))


class NameCompleterTests(unittest.TestCase):
    def test_player_and_npc_completion(self) -> None:
        names = NameCompleter()
        for text in (
            "Abepulifif pierces a skeletal defender for 9 points of damage.",
            "Abepulifif pierces a skeletal defender for 7 points of damage.",
            "a skeletal defender hits Povebizu for 2 points of damage.",
        ):
            names.observe(parse_line(text, 0.0, "Maergoth"))
        self.assertEqual(
            names.complete("bepulifif pierces a skeletal defender for 5 points of damage."),
            "Abepulifif pierces a skeletal defender for 5 points of damage.",
        )
        self.assertEqual(
            names.complete("skeletal defender hits Povebizu for 3 points of damage."),
            "a skeletal defender hits Povebizu for 3 points of damage.",
        )
        self.assertIsNone(names.complete("points of damage."))  # not a name tail
        self.assertIsNone(names.complete("a skeletal defender hits Povebizu."))  # already complete


@dataclass
class L:
    x: int
    y: int
    h: int
    text: str


class SmallFontRowGuardTests(unittest.TestCase):
    def test_two_margin_lines_with_overlapping_boxes_stay_separate(self) -> None:
        t = Tracker()
        base = [L(21, 20 + i * 30, 22, f"Line number {i} of the window is complete.") for i in range(6)]
        t.update(base, 0.0)  # learn the margin
        out = t.update(base, 0.25)
        texts = [m.text for m in out]
        self.assertEqual(len(texts), 5)  # the clipped top row is held back; nothing was glued
        self.assertTrue(all(" window is complete. Line number" not in x for x in texts))


if __name__ == "__main__":
    unittest.main()
