"""Successful harvesting is gathering bookkeeping, separate from personal notices."""
from __future__ import annotations

import unittest

from mnmparse import grammar
from mnmparse.parser import parse_line, split_fused


class HarvestParserTests(unittest.TestCase):
    def test_screenshot_successes_preserve_item_and_viewer_identity(self):
        for item in ("Wood", "Resin"):
            for player in ("", "Birch"):
                text = f"You harvest [{item}]!"
                with self.subTest(item=item, player=player):
                    event = parse_line(text, 42.5, player)
                    self.assertEqual((event.kind, event.actor, event.raw_actor, event.item),
                                     ("harvest", player or "You", "You", item))
                    self.assertEqual((event.text, event.ts), (text, 42.5))
                    self.assertIsNone(event.amount)
                    self.assertIsNone(event.copper)
        self.assertIn("harvest", grammar.KINDS)

    def test_unrelated_personal_item_messages_remain_personal(self):
        for text in ("You eat Ration.", "You drink Water.",
                     "--You loot [Wood] from your corpse.--", "You forage Roots."):
            with self.subTest(text=text):
                self.assertEqual(parse_line(text, 1, "Birch").kind, "personal")

    def test_failure_and_other_player_text_do_not_create_successes(self):
        for text in ("You cannot harvest [Wood]!", "You fail to harvest [Resin]!",
                     "You harvest []!", "Birch harvests [Wood]!", "You begin harvesting [Wood]."):
            with self.subTest(text=text):
                self.assertNotEqual(parse_line(text, 1, "Birch").kind, "harvest")

    def test_adjacent_successes_stay_separate_messages(self):
        parts = split_fused("You harvest [Wood]! You harvest [Resin]!", "Birch")
        events = [parse_line(part, 1, "Birch") for part in parts]
        self.assertEqual([(event.kind, event.item) for event in events],
                         [("harvest", "Wood"), ("harvest", "Resin")])


if __name__ == "__main__":
    unittest.main()
