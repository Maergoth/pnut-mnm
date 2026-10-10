"""Non-damage player actions default to utility without crediting damage or crossfire."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mnmparse.app.models import build_snapshot, merge_snapshots
from mnmparse.grammar import Event
from mnmparse.importer import iter_jsonl_events
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"
MOB = "a dusty skeleton"


def snapshot(lines: list[tuple[float, str]]):
    stats = Stats(player_name=PLAYER)
    for ts, line in lines:
        stats.add(parse_line(line, 100.0 + ts, PLAYER))
    return build_snapshot(stats, stats.current(), PLAYER)


def person(snap, name: str = PLAYER):
    return next(row for row in snap.rows if row.name == name)


class UtilityDefaultTests(unittest.TestCase):
    def test_new_targeted_non_damage_action_counts_without_a_registry(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You befuddle a dusty skeleton."),
            (2, "You befuddle a dusty skeleton."),
        ])
        me = person(snap)
        self.assertEqual((me.damage, me.utility), (10, 2))
        self.assertEqual(me.debuff_skills, {"befuddle": 2})
        summary = merge_snapshots([snap, snap], key="zone", label="zone")
        self.assertEqual((person(summary).damage, person(summary).utility), (20, 4))

    def test_unknown_target_and_buff_prose_do_not_add_utility(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You feel the touch of earth."),
            (2, "You weaves an eldritch ward."),
            (3, "You missed a note."),
            (3.5, "a friendly merchant greets Maergoth."),
            (4, "You befuddle a friendly merchant."),
            (5, "You hail a dusty skeleton."),
        ])
        self.assertEqual(person(snap).utility, 0)

    def test_known_attacks_and_unreadable_damage_do_not_add_utility(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You kicks a dusty skeleton."),
            (2, "You uppercuts a dusty skeleton."),
            (3, "You slashes a dusty skeleton."),
            (4, "You bleeds a dusty skeleton."),
            (5, "Your Slice hits a for 3+oints,ofrBleed Damag"),
            (5, "a dusty skeleton begins to bleed profusely."),
        ])
        self.assertEqual((person(snap).damage, person(snap).utility), (10, 0))

    def test_an_observed_damaging_skill_is_not_counted_from_its_status_line(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You sears a dusty skeleton."),
            (2, "Your sear hits a dusty skeleton for 5 points of Fire Damage."),
        ])
        self.assertEqual((person(snap).damage, person(snap).utility), (15, 0))

    def test_confirmed_cc_is_not_counted_again_as_default_utility(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You intimidates a dusty skeleton."),
            (1, "a dusty skeleton is intimidated."),
            (8, "You intimidates a dusty skeleton."),
        ])
        me = person(snap)
        self.assertEqual((me.cc, me.utility), (1, 2))
        self.assertEqual(me.cc_types, {"fear": 1})

    def test_weakness_credits_exposing_shot_once_and_preserves_other_actions(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You fires an exposing shot at a dusty skeleton."),
            (1.1, "a dusty skeleton looks weak."),
            (8, "You fires an exposing shot at a dusty skeleton."),
        ])
        me = person(snap)
        self.assertEqual(me.utility, 2)
        self.assertEqual(me.debuff_skills, {"Exposing Shot": 2})
        self.assertEqual(me.debuffs, {"weakened": 1, "other utility": 1})

    def test_weakness_from_an_untargeted_cast_is_attributed(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "Tamsin begins casting Omen of Enfeeblement."),
            (3, "a dusty skeleton looks weak."),
        ])
        self.assertEqual(person(snap, "Tamsin").debuff_skills, {"Omen of Enfeeblement": 1})

    def test_failed_non_damage_attempt_does_not_add_utility(self):
        stats = Stats(player_name=PLAYER)
        for ev in [
            parse_line("You crush a dusty skeleton for 10 points of damage.", 100, PLAYER),
            Event(101, "status", "attempt", PLAYER, MOB, skill="Befuddle"),
            Event(101.1, "resist", "resisted", PLAYER, MOB, skill="Befuddle", outcome="resist"),
            Event(102, "status", "attempt", PLAYER, MOB, skill="Confound"),
            Event(102.1, "status", "immune", MOB, PLAYER, outcome="immune"),
        ]:
            stats.add(ev)
        self.assertEqual(person(build_snapshot(stats, stats.current(), PLAYER)).utility, 0)

    def test_a_successful_dispel_is_not_suppressed_by_another_immune_ability(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "You dispel magic from a dusty skeleton. (Holy Armor)"),
            (1.1, "a dusty skeleton is temporarily IMMUNE to Maergoth's Snaring Shot!"),
        ])
        self.assertEqual(person(snap).debuff_skills, {"Dispel Magic": 1})

    def test_non_damage_action_on_a_confirmed_named_enemy_counts(self):
        snap = snapshot([
            (0, "You crush Toilmaster Verith for 10 points of damage."),
            (1, "You befuddle Toilmaster Verith."),
        ])
        self.assertEqual(person(snap).debuff_skills, {"befuddle": 1})

    def test_non_damage_mob_actions_on_a_player_and_fades_are_not_defaults(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "a dusty skeleton befuddles YOU."),
            (2, "a dusty skeleton breaks free of the webs."),
            (3, "a dusty skeleton is no longer mesmerized."),
        ])
        self.assertEqual(person(snap).utility, 0)
        self.assertEqual(person(snap, MOB).utility, 0)

    def test_damage_skills_keep_their_separate_non_damage_secondary_effect(self):
        snap = snapshot([
            (0, "You crush a dusty skeleton for 10 points of damage."),
            (1, "Your Distress hits a dusty skeleton for 5 points of Magic Damage."),
            (1.1, "a dusty skeleton's magical resistance frays."),
        ])
        self.assertEqual((person(snap).damage, person(snap).utility), (15, 1))
        self.assertEqual(person(snap).debuff_skills, {"Distress": 1})

    def test_legacy_bleed_events_are_damage_effects_on_import_and_never_utility(self):
        events = [
            parse_line("You crush a dusty skeleton for 10 points of damage.", 100, PLAYER),
            parse_line("Your Slice hits a dusty skeleton for 5 points of Bleed Damage.", 101, PLAYER),
            Event(101.1, "debuff", "a dusty skeleton is bleeding out.", target=MOB, outcome="bleeding"),
            Event(101.2, "debuff", "a dusty skeleton is struck by a barbed arrow.", target=MOB, outcome="barbed arrow"),
        ]
        stats = Stats(player_name=PLAYER)
        for ev in events:
            stats.add(ev)
        self.assertEqual((person(build_snapshot(stats, stats.current(), PLAYER)).damage,
                          person(build_snapshot(stats, stats.current(), PLAYER)).utility), (15, 0))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.jsonl"
            import dataclasses
            path.write_text("\n".join(json.dumps(dataclasses.asdict(ev)) for ev in events), encoding="utf-8")
            imported = list(iter_jsonl_events(path))
        self.assertEqual([ev.kind for ev in imported[-2:]], ["damage_effect", "damage_effect"])
        self.assertTrue(all(ev.amount is None for ev in imported[-2:]))


if __name__ == "__main__":
    unittest.main()
