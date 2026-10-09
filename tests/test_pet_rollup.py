"""Attributed pets are part of one owner parse, including all detailed statistics."""

from __future__ import annotations

import dataclasses
import unittest

from mnmparse.app.models import actor_color, build_snapshot, merge_snapshots, owner_row, self_rows, snapshot_rows_for_tab
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def parse_fight(lines: list[str], *, owner: str | None = None, pet: str = "Fluffy") -> Stats:
    stats = Stats(player_name=PLAYER)
    if owner is not None:
        stats.roster.set_pet_owner(pet, owner)
    for index, line in enumerate(lines):
        stats.add(parse_line(line, 100 + index, PLAYER))
    return stats


def snapshot(stats: Stats):
    return build_snapshot(stats, stats.current(), PLAYER)


class PetRollupTests(unittest.TestCase):
    def test_every_metric_and_breakdown_is_combined_once(self) -> None:
        stats = Stats(player_name=PLAYER)
        events = [
            Event(100, "melee_hit", "owner hit", PLAYER, "a rat", 20, "crush"),
            Event(101, "melee_hit", "pet hit", "Fluffy", "a rat", 30, "bite"),
            Event(102, "melee_miss", "pet miss", "Fluffy", "a rat", skill="bite", outcome="miss"),
            Event(103, "melee_hit", "owner attacked", "a rat", PLAYER, 5, "bite", blocked=2, absorbed=3),
            Event(104, "melee_hit", "pet attacked", "a rat", "Fluffy", 7, "bite", blocked=4, absorbed=1),
            Event(105, "heal", "owner heal", PLAYER, "Fluffy", 12, "Heal"),
            Event(106, "heal", "pet heal", "Fluffy", PLAYER, 18, "Mend"),
            Event(107, "heal", "enemy heal", "Fluffy", "a rat", 9, "Mend"),
            Event(108, "status", "owner taunt", PLAYER, "a rat", skill="Taunt"),
            Event(109, "status", "pet taunt", "Fluffy", "a rat", skill="Taunt"),
            Event(110, "cast", "pet stun", "Fluffy", skill="Stun"),
            Event(111, "cc", "stun lands", target="a rat", outcome="stun"),
            Event(112, "ability_hit", "pet slice", "Fluffy", "a rat", 4, "Slice", estimated=True),
            Event(113, "debuff", "bleed lands", target="a rat", outcome="bleeding"),
            Event(114, "env_damage", "owner falls", target=PLAYER, amount=6, skill="falling"),
            Event(115, "env_damage", "pet falls", target="Fluffy", amount=8, skill="falling"),
            Event(116, "kill", "owner dies", "a rat", PLAYER),
            Event(117, "kill", "pet dies", "a rat", "Fluffy"),
        ]
        for event in events:
            stats.add(event)
        raw = snapshot(stats)
        sources = [row for row in raw.rows if row.name in (PLAYER, "Fluffy")]
        self.assertEqual(len(sources), 2)
        stats.roster.set_pet_owner("Fluffy", PLAYER)
        snap = snapshot(stats)
        merged = owner_row(snap, PLAYER)
        self.assertIs(merged, next(row for row in snap.rows if row.name == PLAYER))
        self.assertNotIn("Fluffy", [row.name for row in snap.rows])
        for field in (
            "damage", "taken", "heals", "healed", "swings", "hits", "misses", "cc",
            "cc_attempts", "utility", "aggro", "prevented", "estimated", "taunts", "enemy_heals", "deaths",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(merged, field), sum(getattr(row, field) for row in sources))
        self.assertEqual((merged.damage, merged.heals, merged.taken), (54, 30, 26))
        self.assertEqual((merged.cc, merged.taunts, merged.utility, merged.deaths), (1, 2, 4, 2))
        self.assertEqual(merged.debuffs, {"bleeding": 1})
        self.assertEqual(merged.cc_skills, {"Fluffy: Stun": 1})
        self.assertEqual(merged.debuff_skills, {"Fluffy: Slice": 1})
        self.assertEqual(merged.killed_by, {"a rat": 2})
        self.assertEqual(merged.max_hit, max(row.max_hit for row in sources))
        self.assertEqual(merged.max_heal, max(row.max_heal for row in sources))
        self.assertEqual((merged.dps, merged.dtps, merged.hps), tuple(round(value / snap.duration, 2) for value in (54, 26, 30)))
        self.assertEqual(merged.share, 1.0)
        self.assertEqual(snap.total_damage, 54)
        self.assertTrue(any(skill.skill == "Fluffy: bite" for skill in merged.skills))
        self.assertTrue(any(skill.skill == "Fluffy: Mend" for skill in merged.heal_skills))
        self.assertTrue(any(skill.skill == "Fluffy: a rat: bite" for skill in merged.taken_from))

    def test_multiple_pets_one_owner_rank_and_label(self) -> None:
        stats = parse_fight([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Fluffy bites a rat for 15 points of damage.",
            "Kulepu slashes a rat for 25 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
        ], owner="Tamsin")
        stats.roster.set_pet_owner("Kulepu", "Tamsin")
        snap = snapshot(stats)
        rows = snapshot_rows_for_tab(snap, "damage")
        self.assertEqual([row.name for row in rows], ["Tamsin", PLAYER])
        combined = rows[0]
        self.assertEqual(combined.damage, 50)
        self.assertEqual(combined.attributed_pets, ["Fluffy", "Kulepu"])
        self.assertEqual(combined.display_name, "Tamsin + Tamsin's Pets")
        self.assertAlmostEqual(sum(row.share for row in rows), 1.0)
        self.assertEqual(snap.total_damage, 70)

    def test_owner_without_activity_gets_owner_identity_and_style(self) -> None:
        for owner in (PLAYER, "Tamsin"):
            with self.subTest(owner=owner):
                stats = parse_fight([
                    "Tamsin has joined the party.",
                    "Fluffy bites a rat for 15 points of damage.",
                ], owner=owner)
                row = owner_row(snapshot(stats), owner)
                self.assertEqual(row.name, owner)
                self.assertEqual(row.display_name, f"{owner} + {owner}'s Pet")
                self.assertEqual(row.damage, 15)
                self.assertTrue(row.in_group)
                self.assertEqual(row.is_you, owner == PLAYER)
                self.assertFalse(row.is_pet or row.is_npc or row.is_enemy)
                self.assertEqual(row.color, actor_color(owner, is_you=owner == PLAYER, is_npc=False, is_pet=False))

    def test_reassigning_and_clearing_rebuilds_each_parse(self) -> None:
        stats = parse_fight([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Fluffy bites a rat for 30 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
        ], owner=PLAYER)
        first = snapshot(stats)
        self.assertEqual(owner_row(first, PLAYER).damage, 50)
        stats.roster.set_pet_owner("Fluffy", "Tamsin")
        second = snapshot(stats)
        self.assertEqual(owner_row(second, PLAYER).damage, 20)
        self.assertEqual(owner_row(second, "Tamsin").damage, 40)
        self.assertEqual(second.total_damage, first.total_damage)
        stats.roster.set_pet_owner("Fluffy", None)
        cleared = snapshot(stats)
        pet = next(row for row in cleared.rows if row.name == "Fluffy")
        self.assertFalse(pet.is_pet or pet.in_group)
        self.assertEqual(cleared.total_damage, 30)
        self.assertEqual(owner_row(cleared, "Tamsin").display_name, "Tamsin")
        self.assertEqual(owner_row(first, PLAYER).damage, 50, "older snapshot is unchanged")

    def test_zone_summary_unions_pets_without_duplication_or_reprefixing(self) -> None:
        snaps = [snapshot(parse_fight([
            "You crush a rat for 20 points of damage.",
            f"{pet} bites a rat for 30 points of damage.",
        ], owner=PLAYER, pet=pet)) for pet in ("Fluffy", "Kulepu")]
        summary = merge_snapshots(snaps, key="zone", label="zone")
        row = owner_row(summary, PLAYER)
        self.assertEqual(row.attributed_pets, ["Fluffy", "Kulepu"])
        self.assertEqual(row.damage, 100)
        self.assertEqual(summary.total_damage, 100)
        self.assertEqual(sum(r.damage for r in summary.rows if r.in_group), 100)
        self.assertIs(owner_row(summary, PLAYER), row)
        skill_rows = self_rows(summary, "damage", PLAYER)
        self.assertEqual({r.name for r in skill_rows}, {"crush", "Fluffy: bite", "Kulepu: bite"})
        self.assertEqual(sum(r.damage for r in skill_rows), 100)

    def test_outsider_owner_share_retains_whole_fight_denominator(self) -> None:
        stats = parse_fight([
            "You crush a rat for 20 points of damage.",
            "Tamsin crushes a rat for 15 points of damage.",
            "Fluffy bites a rat for 5 points of damage.",
            "a rat bites YOU for 7 points of damage.",
        ], owner="Tamsin")
        snap = snapshot(stats)
        row = owner_row(snap, "Tamsin")
        self.assertFalse(row.in_group)
        self.assertEqual(row.damage, 20)
        self.assertAlmostEqual(row.share, 20 / 47)
        self.assertEqual(snap.total_damage, 20)

    def test_raw_snapshot_support_and_complete_miss_visibility(self) -> None:
        stats = parse_fight([
            "You crush a rat for 20 points of damage.",
            "Fluffy bites a rat for 30 points of damage.",
        ])
        snap = snapshot(stats)
        pet = next(row for row in snap.rows if row.name == "Fluffy")
        raw = dataclasses.replace(snap, rows=[
            dataclasses.replace(row, pet_owner=PLAYER, is_pet=True) if row is pet else row for row in snap.rows
        ])
        combined = owner_row(raw, PLAYER)
        self.assertEqual(combined.damage, 50)
        self.assertFalse(combined.misses_shown, "unknown pet misses prevent a complete combined hit rate")
        self.assertIn("Fluffy: bite", [skill.skill for skill in combined.skills])
        hidden = dataclasses.replace(combined, misses_shown=False)
        visible = dataclasses.replace(combined, misses_shown=True)
        summary = merge_snapshots([dataclasses.replace(raw, rows=[hidden]), dataclasses.replace(raw, rows=[visible])], key="zone", label="zone")
        self.assertFalse(summary.rows[0].misses_shown)

    def test_explicit_ownership_preserves_similar_pet_and_owner_names(self) -> None:
        stats = parse_fight([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
            "Tamsinn bites a rat for 30 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
        ], owner="Tamsin", pet="Tamsinn")
        self.assertEqual(stats.canonical_map(stats.current())["Tamsinn"], "Tamsin", "fixture exercises an OCR collision")
        snap = snapshot(stats)
        merged = owner_row(snap, "Tamsin")
        self.assertEqual(merged.damage, 50)
        self.assertEqual(merged.attributed_pets, ["Tamsinn"])
        self.assertIn("Tamsinn: bite", [skill.skill for skill in merged.skills])
        self.assertTrue(merged.in_group)
        self.assertEqual(snap.total_damage, 70)
        self.assertEqual(sum(row.damage for row in snap.rows if row.in_group), 70)
        self.assertAlmostEqual(sum(row.share for row in snap.rows if row.in_group), 1.0)
        stats.roster.set_pet_owner("Tamsinn", None)
        cleared = snapshot(stats)
        self.assertEqual(owner_row(cleared, "Tamsin").damage, 50, "normal OCR merge remains after clearing ownership")
        self.assertEqual(owner_row(cleared, "Tamsin").attributed_pets, [])

    def test_pet_specific_group_override_yields_to_owner_then_returns_on_clear(self) -> None:
        stats = parse_fight([
            "You crush a rat for 20 points of damage.",
            "Fluffy bites a rat for 30 points of damage.",
        ], owner=PLAYER)
        stats.roster.set_manual("Fluffy", False)
        snap = snapshot(stats)
        merged = owner_row(snap, PLAYER)
        self.assertEqual((snap.total_damage, merged.damage, merged.share), (50, 50, 1.0))
        stats.roster.set_pet_owner("Fluffy", None)
        cleared = snapshot(stats)
        pet = next(row for row in cleared.rows if row.name == "Fluffy")
        self.assertFalse(pet.in_group)
        self.assertEqual(cleared.total_damage, 20)
        self.assertIn("Fluffy", stats.roster.manual_out)

    def test_pet_specific_include_cannot_override_excluded_owner(self) -> None:
        stats = parse_fight([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
            "Fluffy bites a rat for 30 points of damage.",
        ], owner="Tamsin")
        stats.roster.set_manual("Tamsin", False)
        stats.roster.set_manual("Fluffy", True)
        snap = snapshot(stats)
        merged = owner_row(snap, "Tamsin")
        self.assertFalse(merged.in_group)
        self.assertEqual(snap.total_damage, 20)
        self.assertEqual(merged.damage, 40)
        stats.roster.set_pet_owner("Fluffy", None)
        cleared = snapshot(stats)
        self.assertTrue(next(row for row in cleared.rows if row.name == "Fluffy").in_group)
        self.assertEqual(cleared.total_damage, 50)

    def test_invalid_raw_self_owner_does_not_double_count_direct_row(self) -> None:
        stats = parse_fight(["You crush a rat for 20 points of damage."])
        snap = snapshot(stats)
        direct = owner_row(snap, PLAYER)
        direct.pet_owner = PLAYER
        self.assertIs(owner_row(snap, PLAYER), direct)
        self.assertEqual(direct.damage, 20)

    def test_similar_pet_and_viewer_names_do_not_remove_group_damage(self) -> None:
        stats = Stats(player_name="Tamsin")
        stats.roster.set_pet_owner("Tamsln", "Tamsin")
        for index, line in enumerate([
            "You crush a rat for 20 points of damage.",
            "You crush a rat for 10 points of damage.",
            "Tamsln bites a rat for 30 points of damage.",
        ]):
            stats.add(parse_line(line, 100 + index, "Tamsin"))
        snap = build_snapshot(stats, stats.current(), "Tamsin")
        owner = owner_row(snap, "Tamsin")
        self.assertEqual(snap.total_damage, 60)
        self.assertEqual(owner.damage, 60)
        self.assertEqual(owner.attributed_pets, ["Tamsln"])
        self.assertTrue(owner.is_you and owner.in_group)
        self.assertEqual(owner.share, 1.0)

    def test_similar_names_cannot_create_a_nested_pet_owner_chain(self) -> None:
        stats = parse_fight([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Kulepu slashes a rat for 30 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
            "Tamsinn bites a rat for 10 points of damage.",
        ], owner="Tamsin", pet="Kulepu")
        stats.roster.set_pet_owner("Tamsinn", PLAYER)
        snap = snapshot(stats)
        self.assertEqual(snap.total_damage, 80)
        self.assertEqual(owner_row(snap, PLAYER).damage, 30)
        self.assertEqual(owner_row(snap, "Tamsin").damage, 50)
        self.assertEqual(owner_row(snap, PLAYER).attributed_pets, ["Tamsinn"])
        self.assertEqual(owner_row(snap, "Tamsin").attributed_pets, ["Kulepu"])
        self.assertEqual(sum(row.damage for row in snap.rows if row.in_group), 80)


if __name__ == "__main__":
    unittest.main()
