"""Garbled numbers (Dummy Fix), fused lines and the dropped-apostrophe repair (2026-10-02)."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from mnmparse.app.models import build_snapshot
from mnmparse.importer import import_file
from mnmparse.logwriter import LogWriter
from mnmparse.parser import parse_garbled_amount, parse_line, split_fused
from mnmparse.stats import Stats
from mnmparse.tracker import Tracker

PLAYER = "Maergoth"


class ApostropheTests(unittest.TestCase):
    def test_righteous_is_not_a_possessive(self) -> None:
        ev = parse_line("Your Righteous Smite hits a skeletal warrior for 57 points of Holy Damage.", 0.0, PLAYER)
        self.assertEqual((ev.kind, ev.actor, ev.skill, ev.amount), ("ability_hit", PLAYER, "Righteous Smite", 57))
        ev = parse_line("Epugekinu's Righteous Strike hits a skeletal fighter for 41 points of Holy Damage.", 0.0, PLAYER)
        self.assertEqual(ev.skill, "Righteous Strike")

    def test_a_dropped_apostrophe_is_still_repaired(self) -> None:
        ev = parse_line("Povebizus Slice hits a skeletal warrior for 4 points of Bleed Damage.", 0.0, PLAYER)
        self.assertEqual((ev.actor, ev.skill), ("Povebizu", "Slice"))


class FusedLineTests(unittest.TestCase):
    def kinds(self, text: str) -> list[tuple[str, str | None, int | None]]:
        return [
            (ev.kind, ev.actor, ev.amount)
            for ev in (parse_line(part, 0.0, PLAYER) for part in split_fused(text, PLAYER))
        ]

    def test_two_messages_run_together_are_split(self) -> None:
        self.assertEqual(
            self.kinds("Funavabebozeses's Sanctified Weapon hits a skeletal warrior for 2 points of Ho: "
                       "a skeletal warrior's Strike hits YOU for 12 points of damage."),
            [("ability_hit", "Funavabebozeses", 2), ("ability_hit", "a skeletal warrior", 12)],
            "the 12 damage to you is the skeletal warrior's, not Funavabebozeses's",
        )
        self.assertEqual(
            self.kinds("a skeletal cleric's Shock hits Cigezisi for 3 points of Holy a skeletal cleric begins casting Stun."),
            [("ability_hit", "a skeletal cleric", 3), ("cast", "a skeletal cleric", None)],
        )

    def test_a_cut_off_first_half_keeps_its_number(self) -> None:
        self.assertEqual(
            self.kinds("Epugekinu's Righteous Strike hits a skeletal fighter for 41 a skeletal fighter hits Epugekinu for 3 points of damage."),
            [("ability_hit", "Epugekinu", 41), ("melee_hit", "a skeletal fighter", 3)],
        )

    def test_normal_lines_are_not_split(self) -> None:
        for text in [
            "Your Righteous Smite hits a skeletal warrior for 57 points of Holy Damage.",
            "a skeletal warrior has been slain by Povebizu!",
            "--Gozif loots [Bone Chips] from a skeletal marksman's corpse.--",
            "Tovozen's Heal heals Wululiso for 56 Health.",
            "You try to crush a skeletal warrior, but miss!",
        ]:
            self.assertEqual(split_fused(text, PLAYER), [text], text)

    def test_tracker_does_not_join_a_new_message_onto_a_cut_off_line(self) -> None:
        t = Tracker()
        self.assertFalse(t._is_continuation("a skeletal cleric's Shock hits Cigezisi for 3 points of Holy",
                                            "a skeletal cleric begins casting Stun."))
        self.assertFalse(t._is_continuation("Your Crusader Strike hits a skeletal marksman for 5 points of", "Starting to attack."))
        self.assertTrue(t._is_continuation("Tovozen's Holy Strike hits a stumbling zombie for 15 points of Holy", "Damage."))
        self.assertTrue(t._is_continuation("a skeletal warrior has been slain by", "Povebizu!"))
        self.assertTrue(t._is_continuation("Funavabebozeses's", "Shock hits a skeletal warrior for 3 points of damage."))


class DummyFixTests(unittest.TestCase):
    def test_junk_around_a_readable_number_is_dropped(self) -> None:
        ev = parse_line("Pesefu's Holy Strike hits a skeletal defender for' 14 points of Holy Damage.", 0.0, PLAYER)
        self.assertEqual((ev.kind, ev.amount), ("ability_hit", 14))
        ev = parse_line("Your Lesser Healing Touch heals Povebizu for '2 Health.", 0.0, PLAYER)
        self.assertEqual((ev.kind, ev.amount), ("heal", 2))

    def test_an_unreadable_number_is_recognised(self) -> None:
        text = "Gozif pierces a skeletal marksman for $ points of damage."
        self.assertEqual(parse_line(text, 0.0, PLAYER).kind, "unknown")
        guess = parse_garbled_amount(text, 1.0, PLAYER)
        self.assertEqual((guess.kind, guess.actor, guess.amount, guess.estimated), ("melee_hit", "Gozif", None, True))
        self.assertEqual(guess.text, text, "the line keeps what was read")
        self.assertIsNone(parse_garbled_amount("Gozif begins casting Heal.", 1.0, PLAYER))
        self.assertIsNone(parse_garbled_amount("Gozif pierces a skeletal marksman for 7 points of damage.", 1.0, PLAYER))

    def test_estimate_is_the_zone_average_of_the_same_attack(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You have entered Night Harbor (West).", 0.0, PLAYER))
        for ts, amount in [(1.0, 6), (2.0, 10), (3.0, 8)]:
            stats.add(parse_line(f"Gozif pierces a skeletal marksman for {amount} points of damage.", ts, PLAYER))
        stats.add(parse_line("Gozif slashes a skeletal marksman for 30 points of damage.", 4.0, PLAYER))
        guess = parse_garbled_amount("Gozif pierces a skeletal marksman for $ points of damage.", 5.0, PLAYER)
        self.assertTrue(stats.estimate_amount(guess))
        self.assertEqual(guess.amount, 8, "Gozif's pierce average in this zone, not their slashes")
        stranger = parse_garbled_amount("Povebizu slashes a skeletal marksman for ? points of damage.", 6.0, PLAYER)
        self.assertTrue(stats.estimate_amount(stranger))
        self.assertEqual(stranger.amount, 30, "nobody else's slash read yet: the slash average")

    def test_a_new_zone_starts_its_own_averages(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You have entered Night Harbor (West).", 0.0, PLAYER))
        stats.add(parse_line("Gozif pierces a skeletal marksman for 6 points of damage.", 1.0, PLAYER))
        stats.add(parse_line("You have entered Tomb of the Last Wyrmsbane.", 30.0, PLAYER))
        stats.add(parse_line("Gozif pierces a skeletal marksman for 20 points of damage.", 31.0, PLAYER))
        guess = parse_garbled_amount("Gozif pierces a skeletal marksman for ( points of damage.", 32.0, PLAYER)
        stats.estimate_amount(guess)
        self.assertEqual(guess.amount, 20)

    def test_nothing_to_average_leaves_the_line_out(self) -> None:
        guess = parse_garbled_amount("Gozif pierces a skeletal marksman for $ points of damage.", 1.0, PLAYER)
        self.assertFalse(Stats(12.0).estimate_amount(guess))

    def test_estimates_count_but_are_never_the_max_hit(self) -> None:
        stats = Stats(12.0)
        for ts, amount in [(0.0, 4), (1.0, 6)]:
            stats.add(parse_line(f"Gozif pierces a skeletal marksman for {amount} points of damage.", ts, PLAYER))
        guess = parse_garbled_amount("Gozif pierces a skeletal marksman for $ points of damage.", 2.0, PLAYER)
        stats.estimate_amount(guess)
        stats.add(guess)
        snap = build_snapshot(stats, stats.current(), PLAYER)
        gozif = next(r for r in snap.rows if r.name == "Gozif")
        self.assertEqual((gozif.damage, gozif.max_hit, gozif.estimated), (15, 6, 1))


@dataclass
class _Msg:
    text: str
    first_seen: float
    frames_seen: int = 2
    fragment: bool = False


class ImportDummyFixTests(unittest.TestCase):
    def test_import_with_and_without_dummy_fix(self) -> None:
        t0 = time.mktime((2026, 10, 2, 9, 0, 0, 0, 0, -1))
        lines = [
            "Gozif has joined the party.",
            "Gozif pierces a skeletal marksman for 6 points of damage.",
            "Gozif pierces a skeletal marksman for 10 points of damage.",
            "Gozif pierces a skeletal marksman for $ points of damage.",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp) as w:
                for i, text in enumerate(lines):
                    w.write_raw(_Msg(text, t0 + i))
            raw = w.raw_path
            off = import_file(raw, player_name=PLAYER)
            on = import_file(raw, player_name=PLAYER, dummy_fix=True)
        self.assertEqual(off.encounters[0].total_damage, 16)
        self.assertEqual(on.encounters[0].total_damage, 24)
        gozif = next(r for r in on.encounters[0].rows if r.name == "Gozif")
        self.assertEqual(gozif.estimated, 1)


if __name__ == "__main__":
    unittest.main()
