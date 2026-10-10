"""Bare named-combatant regressions, using existing combat-message shapes."""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse.grammar import starts_message  # noqa: E402
from mnmparse.parser import NameCompleter, parse_line, split_fused  # noqa: E402
from mnmparse.tracker import Message, Tracker  # noqa: E402
from mnmparse.vocab import could_be_name  # noqa: E402


NAMED = "Tok'Nor"


class NamedCombatantParserTests(unittest.TestCase):
    def test_your_plural_ranked_skill_is_not_changed_to_a_possessive(self) -> None:
        line = "Your Thorns II hits Tok'Nor for 6 points of damage."
        event = parse_line(line, 0, "Mitch")
        self.assertEqual((event.kind, event.actor, event.target, event.skill, event.amount),
                         ("ability_hit", "Mitch", "Tok'Nor", "Thorns II", 6))
        self.assertEqual(event.text, line)

    def assert_event(self, line: str, **expected: object) -> None:
        event = parse_line(line, 12.5)
        self.assertEqual((event.ts, event.text), (12.5, line))
        for field, value in expected.items():
            self.assertEqual(getattr(event, field), value, (line, field))

    def test_melee_actor_and_target_preserve_name_and_amount(self) -> None:
        for line, actor, target, amount in (
            (f"{NAMED} bites Wululiso for 20 points of damage.", NAMED, "Wululiso", 20),
            (f"Mitch pierces {NAMED} for 46 points of damage.", "Mitch", NAMED, 46),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="melee_hit", actor=actor, target=target, amount=amount)

    def test_possessive_ability_actor_and_named_target(self) -> None:
        for line, actor, target, skill, amount, dtype in (
            (f"{NAMED}'s Strike hits Wululiso for 6 points of damage.",
             NAMED, "Wululiso", "Strike", 6, "damage"),
            (f"Mitch's Vampirism hits {NAMED} for 20 points of Corruption Damage.",
             "Mitch", NAMED, "Vampirism", 20, "Corruption Damage"),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="ability_hit", actor=actor, target=target,
                                  skill=skill, amount=amount, dtype=dtype)

    def test_curly_internal_and_possessive_apostrophes(self) -> None:
        for quote in ("’", "‘"):
            for line, actor, target, kind in (
                (f"Tok{quote}Nor bites Wululiso for 6 points of damage.", NAMED, "Wululiso", "melee_hit"),
                (f"Mitch pierces Tok{quote}Nor for 6 points of damage.", "Mitch", NAMED, "melee_hit"),
                (f"Tok{quote}Nor{quote}s Strike hits Wululiso for 6 points of damage.",
                 NAMED, "Wululiso", "ability_hit"),
            ):
                with self.subTest(line=line):
                    self.assert_event(line, kind=kind, actor=actor, target=target, amount=6)

    def test_existing_kill_shapes_preserve_killer_and_victim(self) -> None:
        for line, actor, target in (
            (f"Your party member Pidef has slain {NAMED}!", "Pidef", NAMED),
            (f"Your party member Pidef has been slain by {NAMED}!", NAMED, "Pidef"),
            (f"{NAMED} has been slain by Pidef!", "Pidef", NAMED),
            (f"{NAMED} has slain Pidef!", NAMED, "Pidef"),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="kill", actor=actor, target=target, amount=None)

    def test_loot_target_excludes_the_corpse_possessive(self) -> None:
        self.assert_event(f"--Mitch loots [Bone Chips] from {NAMED}'s corpse.--",
                          kind="loot", actor="Mitch", target=NAMED, item="Bone Chips", amount=None)
        self.assert_event(f"Mitch loots 12 copper coins from {NAMED}'s corpse.",
                          kind="coin", actor="Mitch", target=NAMED, amount=12, dtype="copper")

    def test_named_looter_in_existing_loot_and_coin_shapes(self) -> None:
        self.assert_event(f"--{NAMED} loots [Bone Chips] from a stumbling zombie's corpse.--",
                          kind="loot", actor=NAMED, target="a stumbling zombie", item="Bone Chips")
        self.assert_event(f"{NAMED} loots 12 copper coins from a stumbling zombie's corpse.",
                          kind="coin", actor=NAMED, target="a stumbling zombie", amount=12)

    def test_possessive_heals_with_named_actor_or_target(self) -> None:
        for line, actor, target in (
            (f"{NAMED}'s Heal heals Wululiso for 56 Health.", NAMED, "Wululiso"),
            (f"Tovozen's Heal heals {NAMED} for 56 Health.", "Tovozen", NAMED),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="heal", actor=actor, target=target,
                                  amount=56, skill="Heal", dtype="Health")

    def test_cast_and_existing_status_shapes(self) -> None:
        for line, expected in (
            (f"{NAMED} begins casting Heal.", dict(kind="cast", actor=NAMED, skill="Heal")),
            (f"{NAMED} appears.", dict(kind="status", actor=NAMED)),
            (f"{NAMED} begins to sneak.", dict(kind="status", actor=NAMED)),
            (f"{NAMED}'s armor breaks.", dict(kind="status", actor=NAMED)),
            (f"{NAMED} loses interest in Pidef.", dict(kind="status", actor=NAMED, target="Pidef")),
            (f"Pidef jabs {NAMED}.", dict(kind="status", actor="Pidef", target=NAMED, skill="jab")),
            (f"{NAMED} is bleeding out.", dict(kind="damage_effect", target=NAMED, outcome="bleeding")),
        ):
            with self.subTest(line=line):
                self.assert_event(line, amount=None, **expected)

    def test_resist_actor_and_named_target(self) -> None:
        for line, actor, target in (
            (f"{NAMED} tries to cast Stun on Pidef, but is resisted!", NAMED, "Pidef"),
            (f"Pidef tries to cast Stun on {NAMED}, but is resisted!", "Pidef", NAMED),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="resist", actor=actor, target=target, skill="Stun", amount=None)

    def test_weapon_modifier_and_miss_clause_do_not_enter_name(self) -> None:
        self.assert_event(f"Mitch pierces {NAMED} with their offhand for 46 points of damage.",
                          kind="melee_hit", actor="Mitch", target=NAMED, amount=46, weapon="offhand")
        self.assert_event(f"Mitch tries to pierce {NAMED}, but {NAMED} dodges!",
                          kind="melee_miss", actor="Mitch", target=NAMED, skill="pierce", outcome="dodge")

    def test_incomplete_named_abilities_do_not_invent_target_or_damage(self) -> None:
        for line, actor, skill in (
            (f"{NAMED}'s Strike hits Wululiso for unreadable points of damage.", NAMED, "Strike"),
            (f"{NAMED}'s Strike hits a for 3+oints,ofrBleed Damag", NAMED, "Strike"),
            (f"Mitch's Vampirism hits {NAMED} for unreadable points of Corruption Damage.",
             "Mitch", "Vampirism"),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="ability_partial", actor=actor, skill=skill,
                                  target=None, amount=None)

    def test_ocr_possessive_repairs_keep_the_internal_apostrophe(self) -> None:
        for possessor in ("Tok'Nors", "Tok'Nor s", "Tok'Nor'"):
            with self.subTest(possessor=possessor):
                self.assert_event(f"{possessor} Strike hits Wululiso for 6 points of damage.",
                                  kind="ability_hit", actor=NAMED, target="Wululiso", skill="Strike", amount=6)

    def test_plain_players_and_article_npcs_remain_supported(self) -> None:
        for line, kind, actor, target, amount in (
            ("Tovozen crushes a stumbling zombie for 2 points of damage.",
             "melee_hit", "Tovozen", "a stumbling zombie", 2),
            ("a stumbling zombie bites Wululiso for 20 points of damage.",
             "melee_hit", "a stumbling zombie", "Wululiso", 20),
            ("Toilmaster Verith bites Wululiso for 20 points of damage.",
             "melee_hit", "Toilmaster Verith", "Wululiso", 20),
            ("Mitch pierces a Mur'Hua scavenger for 46 points of damage.",
             "melee_hit", "Mitch", "a Mur'Hua scavenger", 46),
            ("Tovozen's Heal heals Wululiso for 56 Health.",
             "heal", "Tovozen", "Wululiso", 56),
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind=kind, actor=actor, target=target, amount=amount)

    def test_invalid_names_and_clipped_heal_stay_unknown(self) -> None:
        for line in (
            "Mitch pierces Tok/Nor for 46 points of damage.",
            "Mitch pierces Tok42Nor for 46 points of damage.",
            "Mitch pierces Tok_Nor for 46 points of damage.",
            "Heal heals Wululiso for 56 Health.",
        ):
            with self.subTest(line=line):
                self.assert_event(line, kind="unknown", amount=None)

    def test_fused_clipped_hit_keeps_following_named_hit_separate(self) -> None:
        head = "Mitch's Vampirism hits a skeletal warrior for 20 points of"
        tail = f"{NAMED} bites Wululiso for 3 points of damage."
        parts = split_fused(f"{head} {tail}")
        self.assertEqual(parts, [head, tail])
        events = [parse_line(part, 0) for part in parts]
        self.assertEqual([(e.kind, e.actor, e.target, e.amount) for e in events],
                         [("ability_hit", "Mitch", "a skeletal warrior", 20),
                          ("melee_hit", NAMED, "Wululiso", 3)])

    def test_fused_garbled_prefix_keeps_following_named_ability(self) -> None:
        head = "Gozif's Slice hits a for 3+oints,ofrBleed Damag"
        tail = f"{NAMED}'s Strike hits Wululiso for 6 points of damage."
        parts = split_fused(f"{head} {tail}")
        self.assertEqual(parts, [head, tail])
        first, second = [parse_line(part, 0) for part in parts]
        self.assertEqual((first.kind, first.actor, first.target, first.amount),
                         ("ability_partial", "Gozif", None, None))
        self.assertEqual((second.kind, second.actor, second.target, second.amount),
                         ("ability_hit", NAMED, "Wululiso", 6))

    def test_fused_clipped_possessive_keeps_following_named_loot(self) -> None:
        head = "You loot [Bone Chips] from a skeletal monk's"
        tail = f"{NAMED} loots 12 copper coins from a stumbling zombie's corpse."
        parts = split_fused(f"{head} {tail}")
        self.assertEqual(parts, [head, tail])
        event = parse_line(parts[1], 0)
        self.assertEqual((event.kind, event.actor, event.target, event.amount),
                         ("coin", NAMED, "a stumbling zombie", 12))


class NamedCombatantLearningTests(unittest.TestCase):
    def test_clipped_actor_is_completed_only_after_two_observations(self) -> None:
        names = NameCompleter()
        clipped = "ok'Nor bites Wululiso for 6 points of damage."
        names.observe(parse_line(f"{NAMED} bites Wululiso for 20 points of damage.", 0))
        self.assertIsNone(names.complete(clipped))
        names.observe(parse_line(f"{NAMED} bites Wululiso for 7 points of damage.", 1))
        self.assertEqual(names.complete(clipped), f"{NAMED} bites Wululiso for 6 points of damage.")
        self.assertIsNone(names.complete("nor bites Wululiso for 6 points of damage."))
        self.assertIsNone(names.complete(f"{NAMED} bites Wululiso for 6 points of damage."))

    def test_named_targets_also_teach_clipped_name_completion(self) -> None:
        names = NameCompleter()
        for ts, amount in enumerate((20, 7)):
            names.observe(parse_line(f"Mitch pierces {NAMED} for {amount} points of damage.", ts))
        clipped = "ok'Nor's Strike hits Wululiso for 6 points of damage."
        self.assertEqual(names.complete(clipped), f"{NAMED}'s Strike hits Wululiso for 6 points of damage.")

    def test_vocabulary_uses_the_shared_bare_name_shape(self) -> None:
        for name in (NAMED, "TokNor", "Toilmaster Verith", "Mitch"):
            with self.subTest(name=name):
                self.assertTrue(could_be_name("player", name))
        self.assertTrue(could_be_name("npc", "a Mur'Hua scavenger"))
        # Vocabulary's category describes the article-led shape, not allegiance.
        self.assertFalse(could_be_name("npc", NAMED))

    def test_vocabulary_rejects_modifiers_and_possessive_suffix_as_names(self) -> None:
        for name in ("Tok/Nor", "Tok42Nor", "Tok_Nor", f"{NAMED}'s", f"{NAMED} with their offhand"):
            with self.subTest(name=name):
                self.assertFalse(could_be_name("player", name))


@dataclass
class OcrRow:
    x: int
    y: int
    h: int
    text: str


class NamedCombatantTrackerTests(unittest.TestCase):
    def read_rows(self, rows: list[str]) -> list[Message]:
        tracker = Tracker(ignore_top_line=False)
        frame = [OcrRow(10, 10 + i * 25, 21, text) for i, text in enumerate(rows)]
        messages = tracker.update(frame, 0)
        messages += tracker.update(frame, 0.25)
        messages += tracker.flush(1)
        return messages

    def test_bare_named_combatants_start_new_messages(self) -> None:
        for name in (NAMED, "Tok’Nor", "Tok‘Nor"):
            for line in (
                f"{name} bites Wululiso for 20 points of damage.",
                f"{name} begins casting Heal.",
                f"{name}'s Strike hits Wululiso for 6 points of damage.",
                f"{name} loots 12 copper coins from a stumbling zombie's corpse.",
            ):
                with self.subTest(line=line):
                    self.assertTrue(starts_message(line))

    def test_plain_message_starts_and_wrapped_tail_boundaries(self) -> None:
        for line in (
            "Mitch pierces a stumbling zombie for 46 points of damage.",
            "Tovozen's Heal heals Wululiso for 56 Health.",
            "a Mur'Hua scavenger claws Snagason for 3 points of damage.",
        ):
            with self.subTest(line=line):
                self.assertTrue(starts_message(line))
        for line in (
            NAMED,
            "Corruption Damage.",
            "Health.",
            "Tok'Nor for 20 points of damage.",
            "Damag Tok'Nor's Strike hits Wululiso for 6 points of damage.",
            "Corruption Tok'Nor bites Wululiso for 6 points of damage.",
        ):
            with self.subTest(line=line):
                self.assertFalse(starts_message(line))

    def test_wrapped_hit_with_named_target_is_joined_and_parsed(self) -> None:
        head = f"Mitch's Vampirism hits {NAMED} for 20 points of"
        messages = self.read_rows([head, "Corruption Damage."])
        self.assertEqual([m.text for m in messages], [f"{head} Corruption Damage."])
        self.assertFalse(messages[0].fragment)
        event = parse_line(messages[0].text, messages[0].first_seen)
        self.assertEqual((event.kind, event.actor, event.target, event.amount, event.dtype),
                         ("ability_hit", "Mitch", NAMED, 20, "Corruption Damage"))

    def test_new_named_message_is_not_joined_to_clipped_damage(self) -> None:
        head = "Mitch's Vampirism hits a skeletal warrior for 20 points of"
        for tail, kind, amount in (
            (f"{NAMED} bites Wululiso for 3 points of damage.", "melee_hit", 3),
            (f"{NAMED}'s Strike hits Wululiso for 6 points of damage.", "ability_hit", 6),
        ):
            with self.subTest(tail=tail):
                messages = self.read_rows([head, tail])
                self.assertEqual([m.text for m in messages], [head, tail])
                self.assertTrue(messages[0].fragment)
                self.assertFalse(messages[1].fragment)
                event = parse_line(messages[1].text, messages[1].first_seen)
                self.assertEqual((event.kind, event.actor, event.target, event.amount),
                                 (kind, NAMED, "Wululiso", amount))


if __name__ == "__main__":
    unittest.main()
