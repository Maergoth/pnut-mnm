"""Only independently confirmed complete spellings can explain a false bang."""

from __future__ import annotations

import unittest

from mnmparse.terminal_context import TerminalContext


class TerminalContextTests(unittest.TestCase):
    def setUp(self):
        self.context = TerminalContext()

    def learn(self, text, **flags):
        self.context.observe(text, trustworthy=True, **flags)

    def test_resist_uses_full_clean_spell_without_changing_input(self):
        self.learn("You begin casting Beset the Dead.")
        for glyph in "lI1|":
            text = f"a gorged ghoul resists your Beset the Dead{glyph}"
            with self.subTest(glyph=glyph):
                self.assertTrue(self.context.is_terminal(text))
                self.assertEqual(text[-1], glyph)

    def test_spell_sources_are_complete_cast_hit_heal_and_resist(self):
        for source in (
            "You begin casting Blind.",
            "Your Blind hits a gorged ghoul for 17 points of Magic Damage.",
            "Your Blind heals You for 17 Health.",
            "a gorged ghoul resists your Blind!",
        ):
            with self.subTest(source=source):
                context = TerminalContext()
                context.observe(source, trustworthy=True)
                self.assertTrue(context.is_terminal("a gorged ghoul resists your Blindl"))

    def test_actual_punctuation_does_not_need_contextual_classification(self):
        self.learn("You begin casting Blind.")
        for suffix in ("!", ".", "?", ":", ",", "", " l", " I", " 1", " |", "i", "ll"):
            with self.subTest(suffix=suffix):
                self.assertFalse(self.context.is_terminal("a ghoul resists your Blind" + suffix))

    def test_cannot_learn_from_unterminated_or_false_bang_line(self):
        for source in ("You begin casting Blind", "You begin casting Blindl",
                       "You begin casting Blind,", "a ghoul resists your Blindl"):
            with self.subTest(source=source):
                context = TerminalContext()
                context.observe(source, trustworthy=True)
                self.assertFalse(context.is_terminal("a ghoul resists your Blindl"))

    def test_rejects_untrusted_backlog_estimated_fragment_and_joined_evidence(self):
        for flags in (
            {"trustworthy": False}, {"backlog": True}, {"estimated_ts": True},
            {"fragment": True}, {"joined": True},
        ):
            with self.subTest(flags=flags):
                context = TerminalContext()
                context.observe("You begin casting Blind.", **({"trustworthy": True} | flags))
                self.assertFalse(context.is_terminal("a ghoul resists your Blindl"))

    def test_existing_complete_spelling_wins(self):
        self.learn("You begin casting Blind.")
        self.learn("You begin casting Blindl.")
        self.assertFalse(self.context.is_terminal("a ghoul resists your Blindl"))

    def test_rank_ambiguity_is_preserved(self):
        for rank in ("I", "II", "IV", "VI", "1"):
            with self.subTest(rank=rank):
                context = TerminalContext()
                context.observe(f"You begin casting Holy Shield {rank}.", trustworthy=True)
                for glyph in "lI1|":
                    self.assertFalse(context.is_terminal(f"a ghoul resists your Holy Shield {rank}{glyph}"))
        self.learn("You begin casting HealI.")
        self.assertFalse(self.context.is_terminal("a ghoul resists your HealII"))

    def test_known_longer_spell_keeps_possible_wrap(self):
        self.learn("You begin casting Fear.")
        self.learn("You begin casting Fear the Dead.")
        self.assertFalse(self.context.is_terminal("a ghoul resists your Fearl"))
        self.assertFalse(self.context.is_terminal("a ghoul resists your Fear the"))
        self.assertFalse(self.context.is_terminal("Deadl"))

    def test_other_message_shapes_and_other_players_resist_are_excluded(self):
        self.learn("You begin casting Blind.")
        for text in (
            "You begin casting Blindl", "a ghoul resists Birch's Blindl",
            "Birch says, a ghoul resists your Blindl", "Blindl",
            "a ghoul resists your Unknownl", "a ghoul resists your Blindl more",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.context.is_terminal(text))

    def test_active_kill_uses_complete_combat_name(self):
        self.learn("Keeper of the Road hits YOU for 17 points of damage.")
        for prefix in ("You have slain ", "Your party member Birch has slain ", "Birch has slain "):
            for glyph in "lI1|":
                with self.subTest(prefix=prefix, glyph=glyph):
                    self.assertTrue(self.context.is_terminal(prefix + "Keeper of the Road" + glyph))
        self.assertFalse(self.context.is_terminal("You have been slain by Keeper of the Roadl"))

    def test_real_letter_name_and_known_longer_name_win(self):
        self.learn("So hits YOU for 1 point of damage.")
        self.learn("Sol hits YOU for 1 point of damage.")
        self.assertFalse(self.context.is_terminal("You have slain Sol"))
        self.learn("Keeper of the Road hits YOU for 1 point of damage.")
        self.learn("Keeper of the Road Beyond hits YOU for 1 point of damage.")
        self.assertFalse(self.context.is_terminal("You have slain Keeper of the Roadl"))

    def test_player_pronouns_are_not_learned_names(self):
        self.learn("a ghoul hits YOU for 1 point of damage.")
        self.assertFalse(self.context.is_terminal("Birch has slain YOUl"))

    def test_each_tracker_context_starts_empty_and_storage_is_bounded(self):
        self.learn("You begin casting Blind.")
        self.learn("Keeper of the Road hits YOU for 1 point of damage.")
        self.assertFalse(TerminalContext().is_terminal("a ghoul resists your Blindl"))
        for index in range(self.context.MAX_VALUES + 5):
            self.learn(f"You begin casting Skill {index}.")
        self.assertLessEqual(len(self.context._spells), self.context.MAX_VALUES)
        self.assertIn("Blind", self.context._spells)
        self.assertFalse(self.context.is_terminal("a ghoul resists your Blindl"))
        self.learn("You begin casting Blight.")
        self.assertFalse(self.context.is_terminal("a ghoul resists your Blightl"))
        self.assertTrue(self.context.is_terminal("You have slain Keeper of the Roadl"))


if __name__ == "__main__":
    unittest.main()
