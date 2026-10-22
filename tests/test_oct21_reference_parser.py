"""Fictional October 21 regressions for references, attack prose and text payloads."""
from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot
from mnmparse.parser import parse_line, split_fused
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class ReferenceParserTests(unittest.TestCase):
    def test_kindness_retains_considered_npc_without_combat_fields(self):
        for npc in ("an apprentice blacksmith", "a master blacksmith", "Master Ironhand"):
            with self.subTest(npc=npc):
                text = f"{npc} considers you with kindness"
                event = parse_line(text, 7, "Birch")
                self.assertEqual((event.kind, event.target, event.actor, event.amount, event.skill),
                                 ("consider", npc, None, None, None))
                self.assertEqual((event.text, event.ts), (text, 7))

    def test_kindness_does_not_consume_following_combat(self):
        reference = "an apprentice blacksmith considers you with kindness."
        hit = "Aster hits a large rat for 7 points of damage."
        self.assertEqual(split_fused(reference + " " + hit), [reference, hit])

    def test_quoted_kindness_and_incomplete_text_are_not_considers(self):
        self.assertEqual(parse_line('Aster says, "a blacksmith considers you with kindness"', 1).kind, "chat")
        for text in ("a blacksmith considers you with", "considers you with kindness"):
            self.assertNotEqual(parse_line(text, 1).kind, "consider")

    def test_swoop_retains_actor_and_target_without_damage_or_utility(self):
        text = "a desert bat screeches as it swoops in to attack Birch"
        event = parse_line(text, 1, "Birch")
        self.assertEqual((event.kind, event.actor, event.target, event.skill, event.amount),
                         ("status", "a desert bat", "Birch", None, None))
        stats = Stats(player_name="Birch")
        for ts, message in enumerate(("Birch hits a desert bat for 7 points of damage.", text)):
            stats.add(parse_line(message, ts, "Birch"))
        snapshot = build_snapshot(stats, stats.current(), "Birch")
        self.assertEqual(snapshot.total_damage, 7)
        self.assertTrue(all(row.utility == 0 and row.aggro == 0 for row in snapshot.rows))

    def test_swoop_does_not_consume_following_hit(self):
        text = "a desert bat screeches as it swoops in to attack Birch."
        hit = "a desert bat hits Birch for 2 points of damage."
        self.assertEqual(split_fused(text + " " + hit), [text, hit])
        self.assertEqual(parse_line('Aster says, "' + text + '"', 1).kind, "chat")


class PayloadBoundaryTests(unittest.TestCase):
    def test_quoted_chat_never_invents_experience_from_payload(self):
        for text in ('Aster says, "You gain party experience!"',
                     'You say, "Remember: You gain experience!"'):
            with self.subTest(text=text):
                self.assertEqual(split_fused(text, "Birch"), [text])
                event = parse_line(text, 1, "Birch")
                self.assertEqual(event.kind, "chat")
                session = SessionStats("Birch", started=0, include_personal=True)
                session.add(event)
                self.assertEqual(session.snapshot(now=2).xp_ticks, 0)

    def test_guild_payload_never_invents_experience_or_damage(self):
        for payload in ("You gain experience!", "Remember: You gain party experience!",
                        "Aster hits a large rat for 100 points of damage."):
            with self.subTest(payload=payload):
                text = "GUILD MOTD: " + payload
                self.assertEqual(split_fused(text, "Birch"), [text])

    def test_real_fused_faction_experience_still_splits(self):
        faction = "Your faction standing with Sample Watch got better."
        xp = "You gain party experience!"
        self.assertEqual(split_fused(faction + " " + xp), [faction, xp])


if __name__ == "__main__":
    unittest.main()
