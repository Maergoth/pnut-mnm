"""Bare NPC titles and evidence-based repair of the Keeper kill's OCR tail."""

from __future__ import annotations

import re
import unittest
from dataclasses import dataclass

from mnmparse.grammar import PLAYER, starts_message
from mnmparse.parser import NameCompleter, parse_line, split_fused
from mnmparse.tracker import Tracker


KEEPER = "Keeper of the Road"


class NamedNpcConnectorTests(unittest.TestCase):
    def assert_event(self, text: str, **expected: object):
        event = parse_line(text, 12.5, "Aster")
        self.assertEqual((event.ts, event.text, event.estimated), (12.5, text, False))
        for key, value in expected.items():
            self.assertEqual(getattr(event, key), value, (text, key))
        return event

    def test_all_existing_kill_forms_accept_the_complete_bare_title(self):
        for text, actor, target in (
            (f"You have slain {KEEPER}!", "Aster", KEEPER),
            (f"Your party member Birch has slain {KEEPER}!", "Birch", KEEPER),
            (f"{KEEPER} has been slain by Birch!", "Birch", KEEPER),
            (f"Your party member Birch has been slain by {KEEPER}!", KEEPER, "Birch"),
            (f"{KEEPER} has slain YOU!", KEEPER, "Aster"),
        ):
            with self.subTest(text=text):
                self.assert_event(text, kind="kill", actor=actor, target=target)

    def test_title_actors_targets_and_possessive_abilities_keep_damage(self):
        for text, actor, target, amount, skill in (
            (f"Birch slashes {KEEPER} for 78 points of damage.", "Birch", KEEPER, 78, "slash"),
            (f"{KEEPER} crushes YOU for 12 points of damage.", KEEPER, "Aster", 12, "crush"),
            (f"Your Vengeful Smite hits {KEEPER} for 26 points of Holy Damage.",
             "Aster", KEEPER, 26, "Vengeful Smite"),
            (f"{KEEPER}'s Strike hits YOU for 9 points of damage.", KEEPER, "Aster", 9, "Strike"),
        ):
            with self.subTest(text=text):
                self.assert_event(text, kind="ability_hit" if " hits " in text else "melee_hit",
                                  actor=actor, target=target, amount=amount, skill=skill)

    def test_name_stops_before_weapon_and_miss_clauses(self):
        self.assert_event(f"Birch slashes {KEEPER} with their offhand for 7 points of damage.",
                          kind="melee_hit", actor="Birch", target=KEEPER, amount=7, weapon="offhand")
        self.assert_event(f"You try to slash {KEEPER}, but {KEEPER} parries!",
                          kind="melee_miss", actor="Aster", target=KEEPER, outcome="parry")

    def test_loot_and_coin_stop_before_the_corpse_possessive(self):
        self.assert_event(f"--You loot [Worn Key] from {KEEPER}'s corpse.--",
                          kind="loot", actor="Aster", target=KEEPER, item="Worn Key")
        self.assert_event(f"Birch loots 12 copper coins from {KEEPER}'s corpse.",
                          kind="coin", actor="Birch", target=KEEPER, amount=12, copper=12)

    def test_consider_reaction_and_difficulty_preserve_the_title(self):
        for name in (KEEPER, "Keeper the Elder", "Prince in Exile", "Daughter of Ash and Ember"):
            with self.subTest(name=name):
                text = name + " scowls at you, ready to attack. Would you like to die?"
                self.assert_event(text, kind="consider", target=name, outcome="npc")
                self.assertEqual(split_fused(text), [text])
        # NPC titles do not broaden player-only party/who fields.
        self.assertIsNone(re.fullmatch(PLAYER, KEEPER))

    def test_incomplete_connectors_and_sentence_words_are_not_names(self):
        for name in ("Keeper of the", "Keeper of the road", "Keeper for Damage", "Keeper with Their Offhand"):
            with self.subTest(name=name):
                event = parse_line(f"You have slain {name}!", 0)
                self.assertNotEqual(event.kind, "kill")
        for text in ("Keeper of the Road for 7 points of damage.",
                     "Corruption Tok'Nor bites Birch for 7 points of damage.",
                     f"Corruption {KEEPER} bites Birch for 7 points of damage."):
            with self.subTest(text=text):
                self.assertFalse(starts_message(text))

    def test_fused_titles_do_not_swallow_following_combat_or_experience(self):
        for head, tail in (
            (f"You have slain {KEEPER}!", "You gain party experience!"),
            ("Your Strike hits a test rat for 7 points of", f"{KEEPER} crushes YOU for 12 points of damage."),
            (f"{KEEPER} scowls at you, ready to attack.", "Birch hits a test rat for 3 points of damage."),
        ):
            with self.subTest(head=head, tail=tail):
                self.assertEqual(split_fused(head + " " + tail), [head, tail])
        text = f"Your Strike hits {KEEPER} for 7 points of damage."
        self.assertEqual(split_fused(text), [text])

    def test_damage_tail_prefixes_do_not_become_part_of_the_next_title_actor(self):
        for dtype in ("Holy", "Corruption", "Poison", "Disease", "Electricity", "Magic", "Cold", "Fire"):
            head = f"Your Vengeful Smite hits a rat for 26 points of {dtype} Damage"
            for tail, kind in ((f"{KEEPER} slashes YOU for 12 points of damage.", "melee_hit"),
                               (f"{KEEPER}'s Strike hits YOU for 12 points of damage.", "ability_hit")):
                with self.subTest(dtype=dtype, tail=tail):
                    self.assertFalse(starts_message(f"{dtype} Damage " + tail))
                    self.assertFalse(starts_message("Damage " + tail))
                    self.assertFalse(starts_message("Damag " + tail))
                    self.assertTrue(starts_message(tail))
                    parts = split_fused(head + " " + tail)
                    self.assertEqual(parts, [head, tail])
                    first, second = [parse_line(part, 0) for part in parts]
                    self.assertEqual((first.kind, first.target, first.amount, first.dtype),
                                     ("ability_hit", "a rat", 26, dtype + " Damage"))
                    self.assertEqual((second.kind, second.actor, second.target, second.amount),
                                     (kind, KEEPER, "You", 12))

    def test_damage_words_remain_valid_in_general_names(self):
        self.assert_event("Holy Knight of the Vale slashes YOU for 12 points of damage.",
                          kind="melee_hit", actor="Holy Knight of the Vale", target="Aster", amount=12)
        self.assert_event("You have slain Corruption of the Void!", kind="kill", target="Corruption of the Void")

    def test_authored_chat_never_becomes_a_kill(self):
        text = f'Birch says, "You have slain {KEEPER}! You gain party experience!"'
        self.assert_event(text, kind="chat", actor="Birch")
        self.assertEqual(split_fused(text), [text])

    def test_prior_combat_repairs_the_observed_final_l_without_changing_raw_text(self):
        names = NameCompleter()
        names.observe(parse_line(f"Birch slashes {KEEPER} for 78 points of damage.", 0))
        names.observe(parse_line(f"Your Vengeful Smite hits {KEEPER} for 26 points of Holy Damage.", 1))
        raw = f"You have slain {KEEPER}l"
        for ts in (2, 3):
            event = parse_line(raw, ts, "Aster")
            names.observe(event)  # Same repair path the live engine uses before kill dispatch.
            self.assertEqual((event.kind, event.actor, event.target, event.text, event.estimated),
                             ("kill", "Aster", KEEPER, raw, False))
        # The parser does not guess away a potentially legitimate final letter.
        self.assertEqual(parse_line(raw, 0).target, KEEPER + "l")

    def test_unknown_and_independently_known_l_names_are_preserved(self):
        names = NameCompleter()
        names.observe(parse_line("Birch hits So for 1 point of damage.", 0))
        names.observe(parse_line("Birch hits Sol for 1 point of damage.", 1))
        for name in ("Sol", "Sentinel of the Crystal", KEEPER + "l"):
            with self.subTest(name=name):
                event = parse_line(f"You have slain {name}", 2)
                names.observe(event)
                self.assertEqual((event.kind, event.target), ("kill", name))

    def test_tracker_keeps_the_title_actor_separate_from_previous_wrapped_damage(self):
        @dataclass
        class Row:
            x: int
            y: int
            h: int
            text: str

        head = "Your Strike hits a test rat for 7 points of"
        tail = f"{KEEPER} crushes YOU for 12 points of damage."
        tracker = Tracker(ignore_top_line=False)
        rows = [Row(10, 10 + index * 25, 21, text) for index, text in enumerate((head, tail))]
        messages = tracker.update(rows, 0) + tracker.update(rows, 0.25) + tracker.flush(1)
        self.assertEqual([message.text for message in messages], [head, tail])
        self.assertTrue(messages[0].fragment)
        self.assertFalse(messages[1].fragment)
        event = parse_line(messages[1].text, messages[1].first_seen)
        self.assertEqual((event.kind, event.actor, event.target, event.amount),
                         ("melee_hit", KEEPER, "You", 12))


if __name__ == "__main__":
    unittest.main()
