"""Literal named-pet subjects and manual party coin redistribution."""
from __future__ import annotations

import unittest

from mnmparse.parser import parse_line, split_fused
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class NamedPetParserTests(unittest.TestCase):
    def test_named_npc_pet_ability_keeps_the_pet_as_the_damage_source(self):
        event = parse_line("Stone Keeper's pet's Strike hits Adventurer for 5 points of damage.", 1)
        self.assertEqual(event.kind, "ability_hit")
        self.assertEqual(event.actor, "Stone Keeper's pet")
        self.assertEqual(event.raw_actor, "Stone Keeper's pet")
        self.assertEqual(event.target, "Adventurer")
        self.assertEqual((event.skill, event.amount, event.dtype), ("Strike", 5, "damage"))
        self.assertFalse(event.is_pet)  # This flag identifies only the viewer's own pet.

    def test_named_pet_melee_target_and_cast_share_one_literal_combatant_name(self):
        for text, kind, actor, target in (
            ("Stone Keeper's pet claws Adventurer for 7 points of damage.",
             "melee_hit", "Stone Keeper's pet", "Adventurer"),
            ("You crush Stone Keeper's pet for 9 points of damage.",
             "melee_hit", "Hero", "Stone Keeper's pet"),
            ("Stone Keeper's pet begins casting Strike.",
             "cast", "Stone Keeper's pet", None),
        ):
            with self.subTest(text=text):
                event = parse_line(text, 1, "Hero")
                self.assertEqual((event.kind, event.actor, event.target), (kind, actor, target))
                self.assertFalse(event.is_pet)

    def test_ordinary_article_led_and_viewers_own_pets_keep_existing_semantics(self):
        for text, actor, own_pet in (
            ("Stone Keeper's Strike hits Adventurer for 5 points of damage.", "Stone Keeper", False),
            ("a forest keeper's pet's Strike hits Adventurer for 5 points of damage.",
             "a forest keeper's pet", False),
            ("Your pet Sprout's Strike hits a rat for 5 points of damage.", "Sprout", True),
        ):
            with self.subTest(text=text):
                event = parse_line(text, 1, "Hero")
                self.assertEqual(event.kind, "ability_hit")
                self.assertEqual(event.actor, actor)
                self.assertEqual(event.skill, "Strike")
                self.assertEqual(event.is_pet, own_pet)

    def test_unobserved_pet_synonyms_are_not_added_to_the_literal_actor_name(self):
        event = parse_line("Stone Keeper's familiar's Strike hits Adventurer for 5 points of damage.", 1)
        self.assertEqual(event.actor, "Stone Keeper")
        self.assertEqual(event.skill, "familiar's Strike")
        self.assertFalse(event.is_pet)

    def test_pet_damage_is_not_credited_to_owner_and_quoted_chat_is_not_combat(self):
        stats = Stats(player_name="Adventurer")
        event = parse_line("Stone Keeper's pet's Strike hits Adventurer for 5 points of damage.", 1, "Adventurer")
        stats.add(event)
        encounter = stats.current()
        self.assertIsNotNone(encounter)
        actors = {row["actor"]: row for row in stats.actor_table(encounter)}
        self.assertEqual(actors["Stone Keeper's pet"]["damage"], 5)
        self.assertNotIn("Stone Keeper", actors)
        quote = parse_line('Storyteller says, "Stone Keeper\'s pet\'s Strike hits Adventurer for 5 points of damage."', 2)
        self.assertEqual(quote.kind, "chat")


class ManualPartySplitTests(unittest.TestCase):
    MESSAGE = "You split 1200 copper coins with your party, and receive 300 copper coins as your split."

    def test_manual_redistribution_is_personal_and_carries_no_looted_coin_or_payout(self):
        event = parse_line(self.MESSAGE, 2, "Hero")
        self.assertEqual((event.kind, event.actor, event.outcome), ("personal", "Hero", "split"))
        self.assertEqual((event.amount, event.dtype), (1200, "copper"))
        self.assertIsNone(event.copper)
        self.assertIsNone(event.split_copper)
        self.assertIsNone(event.item)
        self.assertIsNone(event.skill)
        silver = parse_line("You split 3 silver coins with your party, and receive 1 silver coin as your split.", 2)
        self.assertEqual((silver.kind, silver.amount, silver.dtype), ("personal", 3, "silver"))

    def test_nearby_loot_is_not_given_the_manual_payout_and_totals_do_not_change(self):
        for include_personal in (False, True):
            with self.subTest(include_personal=include_personal):
                session = SessionStats("Hero", include_personal=include_personal, started=0)
                session.add(parse_line("You loot 1000 copper coins from a sample bandit's corpse.", 1, "Hero"))
                before = session.snapshot(now=2)
                session.add(parse_line(self.MESSAGE, 2, "Hero"))
                after = session.snapshot(now=3)
                self.assertEqual((after.coin_total, after.coin_received, after.coin_split),
                                 (before.coin_total, before.coin_received, before.coin_split))
                self.assertEqual((after.coin_total, after.coin_received, after.coin_split), (1000, 1000, 0))
                self.assertEqual(after.items, 0)
                # The actual wrapped corpse announcement can still complete its own record.
                session.add(parse_line("50 copper coins from a sample bandit's corpse as your split.", 3, "Hero"))
                final = session.snapshot(now=4)
                self.assertEqual((final.coin_total, final.coin_received, final.coin_split), (1000, 50, 50))

    def test_quoted_incomplete_and_other_actor_text_cannot_invent_party_coin(self):
        quote = parse_line('Storyteller says, "' + self.MESSAGE + '"', 1)
        self.assertEqual(quote.kind, "chat")
        for text in (
            "You split 1200 copper coins with your party.",
            "Hero splits 1200 copper coins with your party, and receives 300 copper coins as your split.",
            "You split 1200 copper coins with your party, and receive copper coins as your split.",
        ):
            with self.subTest(text=text):
                event = parse_line(text, 1)
                self.assertNotIn(event.kind, ("coin", "coin_split", "reward"))
                self.assertIsNone(event.copper)
                self.assertIsNone(event.split_copper)

    def test_fused_following_damage_is_separated_and_counted_independently(self):
        combined = self.MESSAGE + " You crush a sample rat for 9 points of damage."
        self.assertEqual(parse_line(combined, 1).kind, "unknown")
        pieces = split_fused(combined, "Hero")
        self.assertEqual(len(pieces), 2)
        events = [parse_line(piece, 1, "Hero") for piece in pieces]
        self.assertEqual([event.kind for event in events], ["personal", "melee_hit"])
        self.assertEqual(events[1].amount, 9)
        self.assertIsNone(events[0].copper)
        self.assertIsNone(events[0].split_copper)


if __name__ == "__main__":
    unittest.main()
