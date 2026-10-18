"""Sanitized regressions for complete system messages found in the October 18 audit."""

from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot
from mnmparse.parser import parse_line, split_fused
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class ResourceMessageTests(unittest.TestCase):
    def test_mana_restoration_preserves_actor_spell_and_amount(self):
        for owner, actor in (("Aster's", "Aster"), ("Your", "Birch")):
            with self.subTest(owner=owner):
                text = f"{owner} Renew Spirit restores 12 of their Mana."
                ev = parse_line(text, 7, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.skill, ev.amount, ev.dtype),
                                 ("status", actor, "Renew Spirit", 12, "Mana"))
                self.assertEqual((ev.text, ev.ts, ev.target), (text, 7, None))

    def test_mana_is_excluded_from_damage_and_hp_healing_snapshot(self):
        stats = Stats()
        for ts, text in enumerate((
            "Aster hits a dusty skeleton for 10 points of damage.",
            "Aster's Renew Spirit restores 50 of their Mana.",
            "Aster's Heal heals Aster for 7 Health.",
            "Aster hits a dusty skeleton for 10 points of damage.",
        )):
            stats.add(parse_line(text, ts))
        snap = build_snapshot(stats, stats.current(), "Aster")
        row = next(row for row in snap.rows if row.name == "Aster")
        self.assertEqual((row.damage, row.heals, row.hps), (20, 7, 2.33))
        self.assertEqual([(skill.skill, skill.total) for skill in row.heal_skills], [("Heal", 7)])
        self.assertEqual(snap.total_damage, 20)

    def test_unreadable_resource_amount_is_not_guessed(self):
        ev = parse_line("Aster's Renew Spirit restores ? of their Mana.", 1)
        self.assertEqual((ev.kind, ev.amount), ("unknown", None))


class CompleteSystemMessageTests(unittest.TestCase):
    def test_dice_rolls_accept_zero_ocr_and_preserve_result(self):
        for text, actor, amount in (
            ("You throw the dice. Between O and 100, you roll a 42.", "Birch", 42),
            ("Aster throws the dice. Between 0 and 100, they roll a 7.", "Aster", 7),
        ):
            with self.subTest(text=text):
                ev = parse_line(text, 1, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.amount, ev.outcome, ev.skill),
                                 ("status", actor, amount, "roll", None))

    def test_hardcore_broadcasts_are_not_xp_levels_or_combat_deaths(self):
        for text in (
            "Magnificent Aster is now Hardcore Level 3!",
            "Magnificent Birch is now Hardcore Level 4",
            "Aster has died and joined the Heroes of Hardcore! (Level 7)",
        ):
            with self.subTest(text=text):
                ev = parse_line(text, 1)
                self.assertEqual(ev.kind, "status")
                stats = Stats()
                stats.add(ev)
                self.assertIsNone(stats.current())
                session = SessionStats("Aster", started=0)
                session.add(ev)
                self.assertEqual(session.snapshot(now=2).kills, 0)

    def test_complete_viewer_notifications_are_personal(self):
        for text in (
            "You are low on drink",
            "You glance back, certain you saw movement.",
            "Your pulse quickens, your mind uncertain of the cause.",
            "Unknown command: /example",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_line(text, 1).kind, "personal")

    def test_outgoing_trade_money_does_not_count_as_loot(self):
        ev = parse_line("You give 50 silver coins as part of the trade.", 1, "Aster")
        self.assertEqual((ev.kind, ev.actor, ev.amount, ev.dtype, ev.copper),
                         ("personal", "Aster", 50, "silver", None))
        session = SessionStats("Aster", started=0, include_personal=True)
        session.add(ev)
        snap = session.snapshot(now=2)
        self.assertEqual((snap.coin_total, snap.coin_received, snap.items), (0, 0, 0))

    def test_unknown_command_does_not_swallow_a_fused_damage_message(self):
        prefix = "Unknown command: /attack target"
        hit = "You crush a rat for 10 points of damage."
        parts = split_fused(prefix + " " + hit)
        self.assertEqual(parts, [prefix, hit])
        events = [parse_line(part, 1) for part in parts]
        self.assertEqual([(ev.kind, ev.amount) for ev in events],
                         [("unknown", None), ("melee_hit", 10)])

    def test_observed_skill_glyph_is_limited_to_skill_up_shape(self):
        ev = parse_line("Your •kill in Defense has increased! (21)", 1)
        self.assertEqual((ev.kind, ev.skill, ev.amount), ("personal", "Defense", 21))

    def test_pvp_consider_does_not_create_a_combat_event(self):
        ev = parse_line("You would almost certainly be crushed by them in battle. (PvP Aggro)", 1)
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.amount), ("consider", None, None, None))

    def test_unconfirmed_or_incomplete_text_remains_diagnostic(self):
        for text in (
            "Al.ice begins casting Root.",
            "Aster throws the dice. Between 0 and 100, they roll a",
            "You glance back, certain you saw",
            "Aster tells of sharing a ledge, with neither side moving till dawn.",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_line(text, 1).kind, "unknown")
        chat = parse_line('Aster says, "You are low on drink"', 1)
        self.assertEqual(chat.kind, "chat")


if __name__ == "__main__":
    unittest.main()
