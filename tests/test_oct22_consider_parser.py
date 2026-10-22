"""Fictional NPC faction and player difficulty consider regressions."""
from __future__ import annotations

import unittest

from mnmparse.parser import parse_line, split_fused
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class ConsiderIdentityTests(unittest.TestCase):
    def test_kindness_retains_named_and_article_npcs_with_complete_difficulty(self):
        for name in ("Guard Amber", "a watchman", "Keeper the Elder"):
            for suffix in (
                "",
                " Would you like to die?",
                " Would you like to die? You would almost certainly be defeated in battle.",
                " -- Would you like to die? You would almost certainly be defeated in battle.",
                ". You would likely emerge victorious in battle with them.",
            ):
                with self.subTest(name=name, suffix=suffix):
                    text = name + " considers you with kindness" + suffix
                    event = parse_line(text, 12, "Cedar")
                    self.assertEqual((event.kind, event.target, event.outcome), ("consider", name, "npc"))
                    self.assertEqual((event.actor, event.amount, event.skill), (None, None, None))
                    self.assertEqual((event.text, event.ts), (text, 12))
                    self.assertEqual(split_fused(text, "Cedar"), [text])

    def test_existing_faction_and_difficulty_forms_keep_npc_identity(self):
        for body in (
            "seems indifferent to your presence Would you like to die? You would almost certainly be defeated in battle.",
            "seethes at you, ready to strike. Would you like to die?",
            "scowls at you, ready to attack.",
            "glares at you threateningly. Battle would be quite risky.",
            "glowers at you dubiously.",
            "looks at you apprehensively.",
            "looks at you amiably.",
            "views you as a threat It might prove to be a diffcult battle.",
            "appears uneasy with you Battle would be quite risky.",
        ):
            with self.subTest(body=body):
                event = parse_line("Guard Amber " + body, 0)
                self.assertEqual((event.kind, event.target, event.outcome), ("consider", "Guard Amber", "npc"))

    def test_player_name_directly_following_separator_or_difficulty_stays_player(self):
        for name in ("Birch", "birch", "Redoubtable Aster"):
            for separator in (" ", " -- ", "--"):
                for body in (
                    "They look weak. You would likely emerge victorious in battle with them.",
                    "Battle with them would be quite risky. (PvP Aggro)",
                    "Battle with them would be as close to even as you can get.",
                    "You would probably be defeated by them in battle.",
                ):
                    with self.subTest(name=name, separator=separator, body=body):
                        event = parse_line(name + separator + body, 0, "Cedar")
                        self.assertEqual((event.kind, event.target, event.outcome), ("consider", name, "player"))
                        self.assertEqual((event.actor, event.amount, event.skill), (None, None, None))

    def test_standalone_difficulty_has_no_invented_target(self):
        for text in (
            "Would you like to die? You would almost certainly be defeated in battle.",
            "They look weak. You would likely emerge victorious in battle with them.",
            "You would almost certainly be crushed by them in battle. (PvP Aggro)",
            "Battle with them would be fairly in your favor.",
        ):
            with self.subTest(text=text):
                event = parse_line(text, 0)
                self.assertEqual((event.kind, event.target, event.outcome), ("consider", None, None))

    def test_real_weakness_debuff_is_distinct_from_player_consider(self):
        event = parse_line("Birch looks weak.", 0)
        self.assertEqual((event.kind, event.target), ("debuff", "Birch"))

    def test_incomplete_or_unknown_prose_does_not_invent_consider(self):
        for text in (
            "Guard Amber considers you with",
            "Guard Amber considers you with kindness You would likely",
            "Guard Amber considers you with kindness unrelated words.",
            "birch They look",
            "Birch You would arbitrarily emerge victorious.",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(parse_line(text, 0).kind, "consider")

    def test_quoted_chat_preserves_authored_consider_and_combat_text(self):
        text = ('Aster says, "Guard Amber considers you with kindness Would you like to die? '
                'You slash a test rat for 7 points of damage. You gain experience!"')
        self.assertEqual(parse_line(text, 0).kind, "chat")
        self.assertEqual(split_fused(text), [text])

    def test_complete_consider_does_not_swallow_fused_combat_or_experience(self):
        for reference in (
            "Guard Amber considers you with kindness.",
            "Guard Amber considers you with kindness Would you like to die? You would almost certainly be defeated in battle.",
            "Guard Amber considers you with kindness -- Would you like to die? You would almost certainly be defeated in battle.",
            "birch--They look weak. You would likely emerge victorious in battle with them.",
        ):
            for following, kind in (("Aster hits a test rat for 7 points of damage.", "melee_hit"),
                                    ("You gain experience!", "experience")):
                with self.subTest(reference=reference, following=following):
                    parts = split_fused(reference + " " + following, "Cedar")
                    self.assertEqual(parts, [reference, following])
                    self.assertEqual([parse_line(part, 0, "Cedar").kind for part in parts], ["consider", kind])

    def test_only_article_corpse_has_positive_npc_identity(self):
        for name, outcome in (("a test rat", "npc"), ("Birch", None)):
            event = parse_line(name + " is dead. Their corpse will decay in 5 days and 19 hours.", 0)
            self.assertEqual((event.kind, event.target, event.outcome), ("consider", name, outcome))

    def test_considers_never_change_combat_or_session_totals(self):
        stats = Stats()
        session = SessionStats("Cedar", started=0, include_personal=True)
        for text in (
            "Guard Amber considers you with kindness Would you like to die? You would almost certainly be defeated in battle.",
            "birch They look weak. You would likely emerge victorious in battle with them.",
            "a test rat is dead. Their corpse will decay in 5 days and 19 hours.",
        ):
            event = parse_line(text, 1, "Cedar")
            stats.add(event)
            session.add(event)
        self.assertIsNone(stats.current())
        snapshot = session.snapshot(now=2)
        self.assertEqual((snapshot.kills, snapshot.deaths, snapshot.xp_ticks,
                          snapshot.items, snapshot.coin_total, snapshot.cc_total), (0, 0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
