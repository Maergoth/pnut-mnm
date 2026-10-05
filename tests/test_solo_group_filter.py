"""Party filtering from real chat lines, including solo support and pet participation."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from mnmparse.app.models import build_snapshot
from mnmparse.importer import import_file
from mnmparse.parser import parse_line
from mnmparse.stats import Stats


PLAYER = "Maergoth"


def feed(stats: Stats, lines: list[tuple[float, str]]) -> None:
    for ts, text in lines:
        event = parse_line(text, ts, PLAYER)
        if event.kind == "unknown":
            raise AssertionError(f"Regression fixture did not parse: {text}")
        stats.add(event)


def snapshot(stats: Stats):
    encounter = stats.current()
    if encounter is None:
        raise AssertionError("Regression fixture did not open an encounter")
    return build_snapshot(stats, encounter, PLAYER)


class SoloGroupFilterTests(unittest.TestCase):
    def test_solo_fighting_same_named_enemy_does_not_count_nearby_player(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (1, "You crush a rat for 10 points of damage."),
            (2, "Tamsin slashes a rat for 90 points of damage."),
            (3, "a rat bites YOU for 2 points of damage."),
        ])
        snap = snapshot(stats)
        rows = {row.name: row for row in snap.rows}
        self.assertTrue(snap.ours)
        self.assertEqual(snap.total_damage, 10)
        self.assertEqual(rows[PLAYER].share, 1.0)
        self.assertFalse(rows["Tamsin"].in_group)
        self.assertFalse(rows["Tamsin"].is_enemy)
        self.assertEqual(stats.party, set())

    def test_incidental_self_heal_and_cast_do_not_claim_nearby_fight(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (1, "Tamsin slashes a rat for 90 points of damage."),
            (2, "Your Healing Touch heals you for 10 Health."),
            (3, "You begin casting Holy Armor."),
            (4, "a rat bites Tamsin for 2 points of damage."),
        ])
        snap = snapshot(stats)
        self.assertFalse(snap.ours)
        self.assertEqual(snap.total_damage, 0)
        self.assertEqual(stats.party, set())

    def test_healing_an_active_outsider_is_participation_but_not_membership(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (1, "Tamsin slashes a rat for 90 points of damage."),
            (2, "a rat bites Tamsin for 2 points of damage."),
            (3, "Your Healing Touch heals Tamsin for 10 Health."),
        ])
        snap = snapshot(stats)
        rows = {row.name: row for row in snap.rows}
        self.assertTrue(snap.ours)
        self.assertEqual(rows[PLAYER].heals, 10)
        self.assertFalse(rows["Tamsin"].in_group)
        self.assertEqual(snap.total_damage, 0)
        self.assertEqual(stats.party, set())

    def test_healing_an_idle_bystander_does_not_claim_nearby_fight(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (1, "Tamsin slashes a rat for 90 points of damage."),
            (2, "Your Healing Touch heals Brannoc for 10 Health."),
        ])
        self.assertFalse(snapshot(stats).ours)

    def test_confirmed_member_taking_hits_counts_even_without_group_damage(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (0, "Tamsin has joined the party."),
            (1, "a rat bites Tamsin for 20 points of damage."),
            (2, "Your Healing Touch heals Tamsin for 10 Health."),
        ])
        snap = snapshot(stats)
        self.assertTrue(snap.ours)
        self.assertEqual(snap.total_damage, 0)
        self.assertTrue(next(row for row in snap.rows if row.name == "Tamsin").in_group)

    def test_kill_line_can_prove_participation_when_the_hit_was_not_captured(self) -> None:
        for line in (
            "You have slain a rat!",
            "Your party member Brannoc has slain a rat!",
            "Your party member Brannoc has been slain by a rat!",
        ):
            with self.subTest(line=line):
                stats = Stats(8.0, player_name=PLAYER)
                feed(stats, [(1, "Tamsin slashes a rat for 90 points of damage."), (2, line)])
                self.assertTrue(snapshot(stats).ours)

    def test_taunt_and_credited_crowd_control_keep_support_players_fights(self) -> None:
        actions = [
            [(2, "You taunt a rat.")],
            [(2, "You begin casting Mesmerize."), (3, "a rat is mesmerized.")],
        ]
        for action in actions:
            with self.subTest(action=action):
                stats = Stats(8.0, player_name=PLAYER)
                feed(stats, [(1, "Tamsin slashes a rat for 90 points of damage."), *action])
                snap = snapshot(stats)
                self.assertTrue(snap.ours)
                self.assertEqual(snap.total_damage, 0)
                self.assertGreater(next(row for row in snap.rows if row.name == PLAYER).utility, 0)

    def test_resisted_spell_with_known_target_still_counts_as_participation(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (1, "Tamsin slashes a rat for 90 points of damage."),
            (2, "a rat resists your Mesmerize."),
        ])
        self.assertTrue(snapshot(stats).ours)
        self.assertEqual(snapshot(stats).total_damage, 0)

    def test_pet_only_fighting_counts_for_self_and_confirmed_member(self) -> None:
        for owner in (PLAYER, "Tamsin"):
            with self.subTest(owner=owner):
                stats = Stats(8.0, player_name=PLAYER)
                if owner != PLAYER:
                    feed(stats, [(0, "Tamsin has joined the party.")])
                stats.roster.set_pet_owner("Fluffy", owner)
                feed(stats, [(1, "Fluffy bites a rat for 7 points of damage.")])
                snap = snapshot(stats)
                self.assertTrue(snap.ours)
                self.assertEqual(snap.total_damage, 7)
                pet = next(row for row in snap.rows if row.name == "Fluffy")
                self.assertTrue(pet.in_group)
                self.assertEqual(pet.pet_owner, owner)

    def test_leaving_party_returns_new_fights_to_solo_filter(self) -> None:
        stats = Stats(8.0, player_name=PLAYER)
        feed(stats, [
            (0, "Tamsin has joined the party."),
            (1, "Tamsin slashes a rat for 90 points of damage."),
        ])
        self.assertTrue(snapshot(stats).ours)
        stats.expire(15)
        feed(stats, [
            (20, "Your party has been disbanded."),
            (21, "Tamsin slashes a rat for 90 points of damage."),
            (22, "Your Healing Touch heals you for 10 Health."),
        ])
        snap = snapshot(stats)
        self.assertFalse(snap.ours)
        self.assertEqual(snap.total_damage, 0)
        self.assertEqual(stats.party, set())

    def test_import_does_not_add_idle_nearby_fights_to_session_time(self) -> None:
        lines = [
            (1, "Tamsin slashes a rat for 90 points of damage."),
            (2, "Your Healing Touch heals you for 10 Health."),
            (3, "a rat bites Tamsin for 2 points of damage."),
            (30, "You crush a beetle for 10 points of damage."),
            (32, "You crush a beetle for 10 points of damage."),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events_solo.jsonl"
            path.write_text("\n".join(json.dumps(asdict(parse_line(text, ts, PLAYER)))
                                       for ts, text in lines) + "\n", encoding="utf-8")
            result = import_file(path, player_name=PLAYER, encounter_timeout_s=8.0, drop_replays=False)
        self.assertEqual([snap.ours for snap in result.encounters], [False, True])
        self.assertEqual([snap.total_damage for snap in result.encounters], [0, 20])
        self.assertEqual(result.session.encounters, 1)
        self.assertEqual(result.session.combat_seconds, 2.0)
        self.assertEqual(result.session.party, [])


if __name__ == "__main__":
    unittest.main()
