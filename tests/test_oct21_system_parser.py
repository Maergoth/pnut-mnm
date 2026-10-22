"""Synthetic regressions for the October 21 system-message audit."""
from __future__ import annotations

import unittest

from mnmparse.parser import parse_line, split_fused
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class Oct21SystemParserTests(unittest.TestCase):
    def test_complete_who_summary_accepts_observed_numeric_ocr_without_amount(self):
        for text in (
            "There are IO players in Monsters & Memories who match your search.",
            "There are 10 players in Shaded Dunes who match your search.",
            "There is 1 player in Night Harbor who match your search.",
            "Players in Monsters & Memories",
        ):
            with self.subTest(text=text):
                event = parse_line(text, 12, "Birch")
                self.assertEqual((event.kind, event.amount, event.actor, event.target),
                                 ("personal", None, None, None))
                self.assertEqual((event.text, event.ts), (text, 12))

    def test_incomplete_or_non_numeric_who_summary_is_not_guessed(self):
        for text in (
            "There are many players in Monsters & Memories who match your search.",
            "There are 1Q players in Monsters & Memories who match your search.",
            "There are 10 players in Monsters & Memories who match",
            "Players in Monsters gain a new skill.",
        ):
            with self.subTest(text=text):
                event = parse_line(text, 0)
                self.assertNotEqual(event.kind, "personal")
                self.assertIsNone(event.amount)

    def test_hardcore_spaced_score_remains_status_and_preserves_actor(self):
        for score, amount in (("1 1 !", 11), ("11!", 11), ("1", 1)):
            text = f"Redoubtable Aster is now Hardcore Level {score}"
            event = parse_line(text, 12, "Birch")
            self.assertEqual((event.kind, event.actor, event.amount),
                             ("status", "Redoubtable Aster", amount))
            self.assertEqual((event.text, event.ts, event.skill), (text, 12, None))

    def test_hardcore_malformed_score_is_not_guessed(self):
        for score in ("?", "1 Q", "1x1", "1.1", "I O"):
            with self.subTest(score=score):
                event = parse_line(f"Redoubtable Aster is now Hardcore Level {score}!", 0)
                self.assertIn(event.kind, ("unknown", "status"))
                self.assertIsNone(event.amount)

    def test_complete_guild_motd_requires_exact_header(self):
        for text in ("GUILD MOTD: Meet at the example camp.", "GUILD MOTD:"):
            self.assertEqual(parse_line(text, 0).kind, "personal")
        for text in ("GUILD MOTD? Meet at the example camp.", "A guild MOTD follows here"):
            self.assertEqual(parse_line(text, 0).kind, "unknown")

    def test_system_counts_and_hardcore_score_do_not_change_session_totals(self):
        stats = Stats()
        session = SessionStats("Birch", started=0, include_personal=True)
        for text in (
            "There are IO players in Monsters & Memories who match your search.",
            "Redoubtable Aster is now Hardcore Level 1 1 !",
            "Aster has died and joined the Heroes of Hardcore! (Level 7)",
            "GUILD MOTD: Meet at the example camp.",
        ):
            event = parse_line(text, 1, "Birch")
            stats.add(event)
            session.add(event)
        self.assertIsNone(stats.current())
        snapshot = session.snapshot(now=2)
        self.assertEqual((snapshot.kills, snapshot.deaths, snapshot.xp_ticks,
                          snapshot.items, snapshot.coin_total, snapshot.cc_total), (0, 0, 0, 0, 0, 0))

    def test_who_and_hardcore_do_not_swallow_actual_fused_messages(self):
        hit = "You slash a test rat for 5 points of damage."
        xp = "You gain experience!"
        for prefix, expected in (
            ("There are IO players in Monsters & Memories who match your search.", "personal"),
            ("Redoubtable Aster is now Hardcore Level 1 1 !", "status"),
        ):
            for following, kind in ((hit, "melee_hit"), (xp, "experience")):
                with self.subTest(prefix=prefix, following=following):
                    parts = split_fused(prefix + " " + following, "Birch")
                    self.assertEqual(parts, [prefix, following])
                    self.assertEqual([parse_line(part, 0, "Birch").kind for part in parts], [expected, kind])

    def test_complete_quoted_chat_stays_chat_with_embedded_system_or_xp_text(self):
        for body in (
            "There are IO players in Monsters & Memories who match your search.",
            "Redoubtable Aster is now Hardcore Level 1 1 !",
            "GUILD MOTD: You gain experience!",
        ):
            text = f'Birch says, "{body}"'
            self.assertEqual(parse_line(text, 0).kind, "chat")
            self.assertEqual(split_fused(text), [text])

    def test_guild_motd_embedded_combat_and_experience_are_only_authored_text(self):
        for body in (
            "You gain experience!",
            "Reminder: You gain experience!",
            '"You gain experience!"',
            '"You slash a test rat for 5 points of damage."',
            "You slash a test rat for 5 points of damage. You gain experience!",
        ):
            text = "GUILD MOTD: " + body
            parts = split_fused(text, "Birch")
            self.assertEqual(parts, [text])
            stats = Stats()
            session = SessionStats("Birch", started=0, include_personal=True)
            for part in parts:
                event = parse_line(part, 0, "Birch")
                self.assertEqual(event.kind, "personal")
                stats.add(event)
                session.add(event)
            self.assertIsNone(stats.current())
            snapshot = session.snapshot(now=1)
            self.assertEqual((snapshot.kills, snapshot.deaths, snapshot.xp_ticks,
                              snapshot.items, snapshot.coin_total), (0, 0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
