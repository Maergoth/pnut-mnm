"""Regression tests for NPC names with internal capitals and apostrophes."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse.grammar import starts_message  # noqa: E402
from mnmparse.parser import parse_line, split_fused  # noqa: E402
from mnmparse.tracker import Message, Tracker  # noqa: E402


MOB = "a Mur'Hua scavenger"


class NpcNameParserTests(unittest.TestCase):
    def test_screenshot_melee_lines_preserve_names_and_amounts(self) -> None:
        for line, actor, target, amount in [
            (f"{MOB} claws Snagason for 3 points of damage.", MOB, "Snagason", 3),
            (f"Mitch pierces {MOB} for 46 points of damage.", "Mitch", MOB, 46),
        ]:
            with self.subTest(line=line):
                event = parse_line(line, 12.5)
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.amount),
                    ("melee_hit", actor, target, amount),
                )
                self.assertEqual((event.ts, event.text), (12.5, line))

    def test_vampirism_preserves_target_and_corruption_damage(self) -> None:
        line = f"Mitch's Vampirism hits {MOB} for 20 points of Corruption Damage."
        event = parse_line(line, 0)
        self.assertEqual(
            (event.kind, event.actor, event.target, event.skill, event.amount, event.dtype),
            ("ability_hit", "Mitch", MOB, "Vampirism", 20, "Corruption Damage"),
        )

    def test_internal_capitals_do_not_require_an_apostrophe(self) -> None:
        mob = "a MurHua scavenger"
        for line, actor, target in [
            (f"{mob} claws Snagason for 3 points of damage.", mob, "Snagason"),
            (f"Mitch pierces {mob} for 3 points of damage.", "Mitch", mob),
        ]:
            with self.subTest(line=line):
                event = parse_line(line, 0)
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.amount),
                    ("melee_hit", actor, target, 3),
                )

    def test_curly_apostrophes_are_normalized_but_raw_text_is_kept(self) -> None:
        line = "a Mur’Hua scavenger’s Strike hits Snagason for 7 points of damage."
        event = parse_line(line, 0)
        self.assertEqual(
            (event.kind, event.actor, event.target, event.skill, event.amount),
            ("ability_hit", MOB, "Snagason", "Strike", 7),
        )
        self.assertEqual(event.text, line)

    def test_internal_apostrophes_and_possessive_ability_are_distinct(self) -> None:
        for mob in (MOB, "a MurHua scavenger", "a qu'lar hunter"):
            with self.subTest(mob=mob):
                event = parse_line(f"{mob}'s Strike hits Snagason for 7 points of damage.", 0)
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.skill, event.amount),
                    ("ability_hit", mob, "Snagason", "Strike", 7),
                )

    def test_npc_pet_keeps_its_identity(self) -> None:
        pet = f"{MOB}'s pet"
        for line, kind, actor, target in [
            (f"{pet} hits Snagason for 4 points of damage.", "melee_hit", pet, "Snagason"),
            (f"Mitch's Strike hits {pet} for 4 points of damage.", "ability_hit", "Mitch", pet),
            (f"{pet}'s Strike hits Snagason for 4 points of damage.", "ability_hit", pet, "Snagason"),
        ]:
            with self.subTest(line=line):
                event = parse_line(line, 0)
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.amount),
                    (kind, actor, target, 4),
                )

    def test_loot_and_coin_corpse_possessives_are_not_part_of_the_name(self) -> None:
        for mob in (MOB, f"{MOB}'s pet"):
            with self.subTest(mob=mob):
                loot = parse_line(f"--Mitch loots [Bone Chips] from {mob}'s corpse.--", 0)
                self.assertEqual(
                    (loot.kind, loot.actor, loot.target, loot.item),
                    ("loot", "Mitch", mob, "Bone Chips"),
                )
                coin = parse_line(f"Mitch loots 12 copper coins from {mob}'s corpse.", 0)
                self.assertEqual(
                    (coin.kind, coin.actor, coin.target, coin.amount, coin.dtype),
                    ("coin", "Mitch", mob, 12, "copper"),
                )

    def test_dropped_possessive_is_repaired_after_an_internal_apostrophe(self) -> None:
        event = parse_line("a Mur'Hua scavengers Strike hits YOU for 7 points of damage.", 0, "Mitch")
        self.assertEqual(
            (event.kind, event.actor, event.target, event.skill, event.amount),
            ("ability_hit", MOB, "Mitch", "Strike", 7),
        )

    def test_partial_ability_preserves_npc_identity_without_inventing_damage(self) -> None:
        event = parse_line(f"{MOB}'s Strike hits Snagason for unreadable points of damage.", 0)
        self.assertEqual(
            (event.kind, event.actor, event.skill, event.target, event.amount),
            ("ability_partial", MOB, "Strike", None, None),
        )

    def test_weapon_modifier_is_not_part_of_the_npc_name(self) -> None:
        for modifier in ("offhand", "bow"):
            with self.subTest(modifier=modifier):
                event = parse_line(
                    f"Mitch pierces {MOB} with their {modifier} for 46 points of damage.", 0,
                )
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.amount, event.weapon),
                    ("melee_hit", "Mitch", MOB, 46, modifier),
                )

    def test_miss_clause_is_not_part_of_the_npc_name(self) -> None:
        event = parse_line(f"Mitch tries to pierce {MOB}, but {MOB} dodges!", 0)
        self.assertEqual(
            (event.kind, event.actor, event.target, event.skill, event.outcome),
            ("melee_miss", "Mitch", MOB, "pierce", "dodge"),
        )

    def test_invalid_name_punctuation_does_not_become_a_numbered_hit(self) -> None:
        for mob in ("a Mur/Hua scavenger", "a Mur42Hua scavenger", "a Mur_Hua scavenger"):
            with self.subTest(mob=mob):
                event = parse_line(f"Mitch pierces {mob} for 46 points of damage.", 0)
                self.assertEqual(event.kind, "unknown")
                self.assertIsNone(event.amount)

    def test_following_npc_message_is_split_and_keeps_its_own_amount(self) -> None:
        head = "Mitch's Vampirism hits a skeletal warrior for 20 points of"
        tail = f"{MOB} claws Snagason for 3 points of damage."
        parts = split_fused(f"{head} {tail}")
        self.assertEqual(parts, [head, tail])
        events = [parse_line(part, 0) for part in parts]
        self.assertEqual(
            [(event.kind, event.actor, event.target, event.amount) for event in events],
            [("ability_hit", "Mitch", "a skeletal warrior", 20),
             ("melee_hit", MOB, "Snagason", 3)],
        )


@dataclass
class OcrRow:
    x: int
    y: int
    h: int
    text: str


class NpcNameTrackerTests(unittest.TestCase):
    def _read_rows(self, rows: list[str]) -> list[Message]:
        tracker = Tracker(ignore_top_line=False)
        frame = [OcrRow(10, 10 + i * 25, 21, text) for i, text in enumerate(rows)]
        messages = tracker.update(frame, 0)
        messages += tracker.update(frame, 0.25)
        messages += tracker.flush(1)
        return messages

    def test_npc_messages_are_recognized_as_new_messages(self) -> None:
        for mob in (MOB, "a MurHua scavenger", "a Mur’Hua scavenger"):
            for message in (
                f"{mob} claws Snagason for 3 points of damage.",
                f"{mob} begins casting Strike.",
                f"{mob}'s Strike hits Snagason for 7 points of damage.",
            ):
                with self.subTest(message=message):
                    self.assertTrue(starts_message(message))

    def test_name_or_damage_tail_does_not_start_a_message(self) -> None:
        for tail in (MOB, "Mur'Hua scavenger for 20 points of damage.", "Corruption Damage."):
            with self.subTest(tail=tail):
                self.assertFalse(starts_message(tail))

    def test_wrapped_damage_line_is_joined_and_parsed(self) -> None:
        head = f"Mitch's Vampirism hits {MOB} for 20 points of"
        messages = self._read_rows([head, "Corruption Damage."])
        self.assertEqual([message.text for message in messages], [f"{head} Corruption Damage."])
        event = parse_line(messages[0].text, messages[0].first_seen)
        self.assertEqual(
            (event.kind, event.actor, event.target, event.amount, event.dtype),
            ("ability_hit", "Mitch", MOB, 20, "Corruption Damage"),
        )
        self.assertFalse(messages[0].fragment)

    def test_new_npc_message_is_not_joined_to_a_clipped_line(self) -> None:
        head = "Mitch's Vampirism hits a skeletal warrior for 20 points of"
        for mob in (MOB, "a MurHua scavenger"):
            tail = f"{mob} claws Snagason for 3 points of damage."
            with self.subTest(mob=mob):
                messages = self._read_rows([head, tail])
                self.assertEqual([message.text for message in messages], [head, tail])
                self.assertTrue(messages[0].fragment)
                self.assertFalse(messages[1].fragment)
                event = parse_line(messages[1].text, messages[1].first_seen)
                self.assertEqual(
                    (event.kind, event.actor, event.target, event.amount),
                    ("melee_hit", mob, "Snagason", 3),
                )


if __name__ == "__main__":
    unittest.main()
