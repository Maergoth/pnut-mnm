"""Tests for mnmparse.stats: encounter boundaries and per-actor numbers."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse.grammar import Event  # noqa: E402
from mnmparse.parser import parse_line  # noqa: E402
from mnmparse.stats import Encounter, Stats  # noqa: E402

ZOMBIE = "a stumbling zombie"

# A synthetic fight: (ts, line).  Expected numbers are worked out by hand below.
FIGHT: list[tuple[float, str]] = [
    (0.0, "You crush a stumbling zombie for 10 points of damage."),
    (1.0, "You try to crush a stumbling zombie, but miss!"),
    (2.0, "Tovozen's Holy Strike hits a stumbling zombie for 10 points of Holy Damage."),
    (3.0, "a stumbling zombie bites YOU for 5 points of damage."),
    (4.0, "Tovozen's Heal heals Wululiso for 20 Health."),
    (5.0, "Tovozen begins casting Heal."),  # non-combat, appended only
    (10.0, "You crush a stumbling zombie for 20 points of damage."),
]
# You:     damage 30 (10 + 20), hits 2, misses 1 -> swings 3, hit% 66.67, max 20, avg 15, dps 30/10 = 3.0
# Tovozen: damage 10 (ability), swings 0, hit% 0, max 10, avg 10, heals 20, dps 1.0
# zombie:  damage 5, hits 1, swings 1, hit% 100, max 5, avg 5, dps 0.5


def feed(stats: Stats, lines: list[tuple[float, str]], player_name: str = "") -> None:
    for ts, line in lines:
        stats.add(parse_line(line, ts, player_name))


def row(table: list[dict], actor: str) -> dict:
    for r in table:
        if r["actor"] == actor:
            return r
    raise AssertionError(f"no row for {actor!r} in {table!r}")


class ActorTableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = Stats(encounter_timeout_s=12.0)
        feed(self.stats, FIGHT)
        self.enc = self.stats.current()
        self.assertIsNotNone(self.enc)
        self.table = self.stats.actor_table(self.enc)

    def test_encounter_bounds_and_targets(self) -> None:
        self.assertEqual(self.enc.start, 0.0)
        self.assertEqual(self.enc.end, 10.0)
        self.assertAlmostEqual(self.enc.duration, 10.0)
        self.assertEqual(self.enc.target_names, {ZOMBIE})
        self.assertEqual(len(self.enc.events), len(FIGHT))
        self.assertFalse(self.enc.closed)
        self.assertEqual(self.stats.history, [])

    def test_sorted_by_damage_desc(self) -> None:
        self.assertEqual([r["actor"] for r in self.table], ["You", "Tovozen", ZOMBIE])

    def test_you_row(self) -> None:
        r = row(self.table, "You")
        self.assertEqual(r["damage"], 30)
        self.assertAlmostEqual(r["dps"], 3.0)
        self.assertEqual(r["swings"], 3)
        self.assertEqual(r["hits"], 2)
        self.assertEqual(r["misses"], 1)
        self.assertAlmostEqual(r["hit_pct"], 66.67, places=2)
        self.assertEqual(r["max_hit"], 20)
        self.assertAlmostEqual(r["avg_hit"], 15.0)
        self.assertEqual(r["heals"], 0)

    def test_tovozen_row_ability_damage_and_heals(self) -> None:
        r = row(self.table, "Tovozen")
        self.assertEqual(r["damage"], 10)
        self.assertAlmostEqual(r["dps"], 1.0)
        self.assertEqual(r["swings"], 0)
        self.assertEqual(r["hits"], 0)
        self.assertEqual(r["misses"], 0)
        self.assertEqual(r["hit_pct"], 0.0)
        self.assertEqual(r["max_hit"], 10)
        self.assertAlmostEqual(r["avg_hit"], 10.0)
        self.assertEqual(r["heals"], 20)

    def test_npc_row(self) -> None:
        r = row(self.table, ZOMBIE)
        self.assertEqual(r["damage"], 5)
        self.assertAlmostEqual(r["dps"], 0.5)
        self.assertEqual(r["swings"], 1)
        self.assertEqual(r["hits"], 1)
        self.assertAlmostEqual(r["hit_pct"], 100.0)
        self.assertEqual(r["max_hit"], 5)
        self.assertAlmostEqual(r["avg_hit"], 5.0)

    def test_row_keys(self) -> None:
        expected = {"actor", "damage", "dps", "swings", "hits", "misses", "hit_pct", "max_hit", "avg_hit", "heals"}
        for r in self.table:
            self.assertEqual(set(r), expected)

    def test_dps_uses_at_least_one_second(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 7 points of damage.", 5.0))
        enc = stats.current()
        self.assertEqual(enc.duration, 0.0)
        self.assertAlmostEqual(row(stats.actor_table(enc), "You")["dps"], 7.0)

    def test_player_name_rows(self) -> None:
        stats = Stats(12.0)
        feed(stats, FIGHT, player_name="Maergoth")
        names = [r["actor"] for r in stats.actor_table(stats.current())]
        self.assertIn("Maergoth", names)
        self.assertNotIn("You", names)


class EncounterBoundaryTests(unittest.TestCase):
    def test_kill_is_recorded_but_does_not_end_the_fight(self) -> None:
        stats = Stats(12.0)
        feed(stats, FIGHT)
        stats.add(parse_line("Your party member Pidef has slain a stumbling zombie!", 11.0))
        enc = stats.current()
        self.assertIsNotNone(enc, "the fight goes on until the timeout")
        self.assertEqual(enc.killed_names, {ZOMBIE})
        self.assertEqual(enc.events[-1].kind, "kill")
        # A hit right after the kill (the next mob of a chain pull) is the same fight.
        stats.add(parse_line("You crush a stumbling zombie for 3 points of damage.", 12.0))
        self.assertIs(stats.current(), enc)
        closed = stats.expire(12.0 + 12.0 + 0.5)
        self.assertIs(closed, enc)
        self.assertTrue(enc.closed)
        self.assertEqual(enc.end, 12.0, "it ends at the last swing")

    def test_fight_ends_only_after_the_timeout(self) -> None:
        stats = Stats(8.0)
        stats.add(parse_line("You crush a stumbling zombie for 10 points of damage.", 0.0))
        stats.add(parse_line("You have slain a stumbling zombie!", 1.0))
        self.assertIsNone(stats.expire(7.9), "still inside the timeout")
        self.assertIsNotNone(stats.current())
        self.assertIsNotNone(stats.expire(8.1))
        self.assertIsNone(stats.current())
        self.assertEqual(len(stats.history), 1)

    def test_every_kill_of_a_chain_pull_is_listed(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("You crush a giant rat for 5 points of damage.", 1.0))
        self.assertEqual(stats.current().target_names, {ZOMBIE, "a giant rat"})
        stats.add(parse_line("You have slain a stumbling zombie!", 2.0))
        stats.add(parse_line("You have slain a giant rat!", 3.0))
        self.assertIsNotNone(stats.current(), "all dead, but the fight ends at the timeout")
        stats.expire(30.0)
        self.assertEqual(stats.history[0].killed_names, {ZOMBIE, "a giant rat"})

    def test_unrelated_kill_does_not_close(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("Your party member Pidef has slain a giant rat!", 1.0))
        self.assertIsNotNone(stats.current())

    def test_kill_without_open_encounter_is_ignored(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("Your party member Pidef has slain a stumbling zombie!", 1.0))
        self.assertIsNone(stats.current())
        self.assertEqual(stats.history, [])

    def test_timeout_splits_encounters(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 5.0))
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 17.5))  # 12.5 s gap
        self.assertEqual(len(stats.history), 1)
        first = stats.history[0]
        self.assertEqual((first.start, first.end), (0.0, 5.0))
        self.assertEqual(len(first.events), 2)
        second = stats.current()
        self.assertIsNotNone(second)
        self.assertEqual(second.start, 17.5)
        self.assertEqual(len(second.events), 1)

    def test_gap_equal_to_timeout_does_not_split(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 12.0))
        self.assertEqual(stats.history, [])
        self.assertEqual(len(stats.current().events), 2)

    def test_misses_keep_encounter_alive(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("You try to crush a stumbling zombie, but miss!", 10.0))
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 20.0))
        self.assertEqual(stats.history, [])
        self.assertEqual(len(stats.current().events), 3)

    def test_non_damage_events_do_not_extend_timeout(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("Tovozen begins casting Heal.", 10.0))
        stats.add(parse_line("Tovozen's Heal heals Wululiso for 20 Health.", 11.0))
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 13.0))  # 13 s since damage
        self.assertEqual(len(stats.history), 1)
        self.assertEqual(stats.history[0].end, 0.0)
        self.assertEqual(stats.current().start, 13.0)

    def test_heal_alone_does_not_open_an_encounter(self) -> None:
        # Out-of-combat heals made one-second "encounters" with no damage (2026-10-02 logs).
        stats = Stats(12.0)
        stats.add(parse_line("Tovozen's Heal heals Wululiso for 56 Health.", 3.0))
        self.assertIsNone(stats.current())
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 5.0))
        stats.add(parse_line("Tovozen's Heal heals Wululiso for 40 Health.", 6.0))
        enc = stats.current()
        self.assertEqual(enc.start, 5.0, "the fight starts with the first swing, not the earlier heal")
        self.assertEqual([e.kind for e in enc.events], ["melee_hit", "heal"], "heals during the fight count")

    def test_encounter_without_damage_is_dropped(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("Fipuduzuleg crushes a skeletal priest but they absorb the attack.", 0.0))
        self.assertIsNotNone(stats.current())
        self.assertIsNone(stats.expire(20.0), "nothing to announce")
        self.assertEqual(stats.history, [])
        self.assertEqual(stats.discarded, 1)

    def test_a_misread_name_does_not_change_when_the_fight_ends(self) -> None:
        stats = Stats(12.0)
        for ts, line in [
            (0.0, "You crush a skeletal fighter for 5 points of damage."),
            (1.0, "a skeletal fizhter hits YOU for 3 points of damage."),  # one misread sighting
            (2.0, "You crush a skeletal fighter for 7 points of damage."),
            (3.0, "You have slain a skeletal fighter!"),
        ]:
            stats.add(parse_line(line, ts))
        self.assertIsNotNone(stats.current())
        self.assertIsNotNone(stats.expire(2.0 + 12.0 + 0.1), "the timeout ends it, misread or not")
        self.assertEqual(len(stats.history), 1)

    def test_non_combat_events_do_not_open(self) -> None:
        stats = Stats(12.0)
        for line in [
            "Starting to attack.",
            "Tovozen begins casting Heal.",
            "Your casting is interrupted.",
            "You try to attack, but you are too far away.",
            "zomme ror 1",
        ]:
            stats.add(parse_line(line, 1.0))
        self.assertIsNone(stats.current())
        self.assertEqual(stats.history, [])

    def test_context_events_are_appended_to_open_encounter(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        stats.add(parse_line("Starting to attack.", 1.0))
        stats.add(parse_line("You try to attack, but you are too far away.", 2.0))
        kinds = [e.kind for e in stats.current().events]
        self.assertEqual(kinds, ["melee_hit", "status", "cannot_attack"])

    def test_expire(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("You crush a stumbling zombie for 5 points of damage.", 0.0))
        self.assertIsNone(stats.expire(5.0))
        self.assertIsNotNone(stats.current())
        closed = stats.expire(20.0)
        self.assertIsNotNone(closed)
        self.assertTrue(closed.closed)
        self.assertIsNone(stats.current())
        self.assertEqual(stats.history, [closed])
        self.assertIsNone(stats.expire(30.0))

    def test_npc_attacking_player_sets_target(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("a stumbling zombie bites Wululiso for 20 points of damage.", 0.0))
        self.assertEqual(stats.current().target_names, {ZOMBIE})

    def test_duel_without_npc_uses_attacked_player(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("Pidef pierces Tovozen for 4 points of damage.", 0.0))
        self.assertEqual(stats.current().target_names, {"Tovozen"})


class ZoneTests(unittest.TestCase):
    def test_zone_line_ends_the_fight_and_tags_later_ones(self) -> None:
        stats = Stats(encounter_timeout_s=12.0)
        self.assertEqual(stats.zone_at(0.0), "")
        feed(stats, [(0.0, f"You crush {ZOMBIE} for 5 points of damage.")])
        feed(stats, [(3.0, "You have entered Night Harbor (East).")])
        self.assertIsNone(stats.current(), "a zone line closes the open encounter")
        self.assertEqual(len(stats.history), 1)
        self.assertEqual(stats.history[0].end, 0.0, "closed at its last activity, not at the zone line")
        feed(stats, [(4.0, "Entering Night Harbor (East).")])  # the second form of the same change
        self.assertEqual(stats.zone_changes, [(3.0, "Night Harbor (East)")])
        feed(stats, [(10.0, "You crush a giant rat for 5 points of damage.")])
        feed(stats, [(30.0, "You have entered Wyrmsbane Tomb.")])
        feed(stats, [(31.0, "Loading, please wait...")])  # no target: ignored
        self.assertEqual(stats.zone_changes, [(3.0, "Night Harbor (East)"), (30.0, "Wyrmsbane Tomb")])
        self.assertEqual(stats.zone_at(0.0), "")
        self.assertEqual(stats.zone_at(10.0), "Night Harbor (East)")
        self.assertEqual(stats.zone_at(31.0), "Wyrmsbane Tomb")
        self.assertEqual(len(stats.history), 2)


class ZoneVisitTests(unittest.TestCase):
    def test_zone_lines_and_misread_variants_are_one_change(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line(f"You crush {ZOMBIE} for 5 points of damage.", 0.0))
        stats.add(parse_line("Entering Night Harbor (East).", 2.0))
        self.assertIsNone(stats.current(), "the first zone line ends the fight")
        stats.add(parse_line("Loading, please wait...", 2.0))
        stats.add(parse_line("You have entered Night HarbOF (East).", 9.0))
        self.assertEqual(stats.zone_changes, [(2.0, "Night Harbor (East)")])
        self.assertEqual(stats.zone_visit_at(10.0), ("Night Harbor (East)", 2.0))

    def test_vocabulary_names_the_visit_with_the_common_spelling(self) -> None:
        from mnmparse.vocab import Vocabulary

        vocab = Vocabulary()
        stats = Stats(12.0, vocab=vocab)
        stats.add(parse_line("You have entered Tomb of the Last Wvrmsbane.", 0.0))
        for i in range(20):  # the word is common in other lines
            stats.add(parse_line("a Wyrmsbane crusader hits YOU for 3 points of damage.", 1.0 + i))
        stats.add(parse_line("You have entered Night Harbor (West).", 40.0))
        stats.add(parse_line("You have entered Tomb of the Last Wyrmsbane.", 80.0))
        visits = stats.zone_visits()
        self.assertEqual([z for _ts, z in visits], ["Tomb of the Last Wyrmsbane", "Night Harbor (West)", "Tomb of the Last Wyrmsbane"])


class RenderAndJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = Stats(12.0)
        feed(self.stats, FIGHT)
        self.enc = self.stats.current()

    def test_render_header_and_columns(self) -> None:
        out = self.stats.render(self.enc)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("Encounter vs a stumbling zombie - 00:10"), lines[0])
        for col in ("Actor", "Damage", "DPS", "Swings", "Hits", "Miss", "Hit%", "Max", "Avg", "Heals"):
            self.assertIn(col, lines[1])
        self.assertEqual(len(lines), 2 + 3)
        self.assertTrue(lines[2].startswith("You"))
        self.assertIn("66.7%", lines[2])
        self.assertIn("30", lines[2])
        # Fixed width: every data row has the same length as the column header.
        widths = {len(ln) for ln in lines[1:]}
        self.assertEqual(len(widths), 1, lines)

    def test_render_mmss_for_long_fights(self) -> None:
        enc = Encounter(start=0.0, end=125.0)
        out = self.stats.render(enc)
        self.assertIn("02:05", out.splitlines()[0])
        self.assertIn("Encounter vs unknown", out)

    def test_render_empty_encounter(self) -> None:
        enc = Encounter(start=0.0, end=0.0)
        out = self.stats.render(enc)
        self.assertIn("00:00", out)
        self.assertGreaterEqual(len(out.splitlines()), 3)

    def test_to_json_roundtrips(self) -> None:
        data = self.stats.to_json(self.enc)
        json.dumps(data)  # must be serialisable
        self.assertEqual(data["start"], 0.0)
        self.assertEqual(data["end"], 10.0)
        self.assertEqual(data["duration_s"], 10.0)
        self.assertEqual(data["targets"], [ZOMBIE])
        self.assertEqual(data["killed"], [])
        self.assertFalse(data["closed"])
        self.assertEqual(data["event_count"], len(FIGHT))
        self.assertEqual(data["actors"], self.stats.actor_table(self.enc))

    def test_actor_table_ignores_events_without_actor(self) -> None:
        enc = Encounter(start=0.0, end=1.0)
        enc.events.append(Event(ts=0.0, kind="melee_hit", text="x", actor=None, amount=5))
        enc.events.append(Event(ts=0.0, kind="unknown", text="y"))
        self.assertEqual(self.stats.actor_table(enc), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
