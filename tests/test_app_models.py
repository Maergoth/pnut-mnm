"""Tests for mnmparse.app.models: build_snapshot and snapshot_rows_for_tab (no Qt)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse.app import models  # noqa: E402
from mnmparse.app.models import (  # noqa: E402
    ActorRow,
    EncounterSnapshot,
    SkillRow,
    build_snapshot,
    snapshot_rows_for_tab,
)
from mnmparse.grammar import Event  # noqa: E402
from mnmparse.parser import parse_line  # noqa: E402
from mnmparse.stats import Encounter, Stats  # noqa: E402

ZOMBIE = "a stumbling zombie"
PLAYER = "Pidef"

# A synthetic fight with three actors (you, Tovozen, the zombie) plus your pet.
# "Wululiso" is a wrapped/misread name variant that must merge into "Wululiso".
FIGHT: list[tuple[float, str]] = [
    (0.0, "You crush a stumbling zombie for 10 points of damage."),
    (1.0, "You try to crush a stumbling zombie, but miss!"),
    (2.0, "Tovozen's Holy Strike hits a stumbling zombie for 15 points of Holy Damage."),
    (3.0, "a stumbling zombie bites YOU for 5 points of damage."),
    (3.5, "a stumbling zombie tries to bite Wululiso, but Wululiso absorbs the blow!"),
    (4.0, "Tovozen's Heal heals Wululiso for 20 Health."),
    (4.5, "Tovozen's Life Draw heals them for 12 Health."),  # heal to "them" = self heal
    (5.0, "Tovozen begins casting Heal."),  # non-combat, appended only
    (6.0, "Your pet Fluffy bites a stumbling zombie for 7 points of damage."),
    (7.0, "a stumbling zombie bites WulDliso for 9 points of damage."),  # OCR-noise variant
    (7.5, "a stumbling zombie bites Wululiso for 1 point of damage."),
    (8.0, "a stumbling zombie bites Wululiso for 1 point of damage."),
    (10.0, "You crush a stumbling zombie for 20 points of damage."),
    (10.5, "You kick a stumbling zombie for 4 points of damage."),
]
# Pidef:   damage 34 (10+20 crush, 4 kick), melee hits 3, misses 1 -> swings 4, hit% 75,
#          max 20, avg 34/3 = 11.33, taken 5, skills crush 30 (2 hits, 1 miss), kick 4
# Tovozen: damage 15 (ability), swings 0, heals 32 (20 + 12 self), healed 12
# zombie:  damage 5 + 9 + 1 + 1 = 16, hits 4, misses 1 (absorb) -> swings 5, hit% 80, max 9
# Fluffy:  pet, damage 7, hits 1
# Wululiso: taken 11 (9 + 1 + 1, with the WulDliso reading merged), healed 20
#: The header total is the group's own damage (Pidef, Tovozen, the pet Fluffy): the zombie's 16
#: is the enemy's and never counts.
TOTAL_DAMAGE = 34 + 15 + 7
ENEMY_DAMAGE = 16
DURATION = 10.5


def feed(stats: Stats, lines: list[tuple[float, str]], player_name: str = PLAYER) -> None:
    for ts, line in lines:
        stats.add(parse_line(line, ts, player_name))


def row(snap: EncounterSnapshot, name: str) -> ActorRow:
    for r in snap.rows:
        if r.name == name:
            return r
    raise AssertionError(f"no row for {name!r}; have {[r.name for r in snap.rows]}")


class BuildSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = Stats(encounter_timeout_s=12.0)
        self.stats.add(parse_line("Tovozen has joined the party.", -60.0, PLAYER))
        feed(self.stats, FIGHT)
        enc = self.stats.current()
        assert enc is not None
        self.enc = enc
        self.snap = build_snapshot(self.stats, self.enc, PLAYER)

    def test_header_fields(self) -> None:
        snap = self.snap
        self.assertEqual(snap.key, "0.000")
        self.assertEqual(snap.label, ZOMBIE)
        self.assertEqual(snap.start, 0.0)
        self.assertEqual(snap.end, 10.5)
        self.assertAlmostEqual(snap.duration, DURATION)
        self.assertFalse(snap.closed)
        self.assertEqual(snap.event_count, len(FIGHT))
        self.assertEqual(snap.total_damage, TOTAL_DAMAGE)
        self.assertAlmostEqual(snap.raid_dps, round(TOTAL_DAMAGE / DURATION, 2))
        self.assertEqual(snap.killed, [])

    def test_rows_present_and_sorted_by_damage(self) -> None:
        names = [r.name for r in self.snap.rows]
        self.assertEqual(names[:4], [PLAYER, ZOMBIE, "Tovozen", "Fluffy"])
        self.assertIn("Wululiso", names)
        self.assertNotIn("WulDliso", names)  # merged by canonical_map
        self.assertEqual(len(names), 5)

    def test_player_row(self) -> None:
        you = row(self.snap, PLAYER)
        self.assertTrue(you.is_you)
        self.assertFalse(you.is_npc)
        self.assertFalse(you.is_pet)
        self.assertEqual(you.damage, 34)
        self.assertEqual(you.taken, 5)
        self.assertEqual(you.heals, 0)
        self.assertEqual(you.healed, 0)
        self.assertEqual((you.swings, you.hits, you.misses), (4, 3, 1))
        self.assertAlmostEqual(you.hit_pct, 75.0)
        self.assertEqual(you.max_hit, 20)
        self.assertAlmostEqual(you.avg_hit, round(34 / 3, 2))
        self.assertAlmostEqual(you.dps, round(34 / DURATION, 2))
        self.assertAlmostEqual(you.dtps, round(5 / DURATION, 2))
        self.assertAlmostEqual(you.share, 34 / TOTAL_DAMAGE)

    def test_player_skill_breakdown(self) -> None:
        you = row(self.snap, PLAYER)
        self.assertEqual([s.skill for s in you.skills], ["crush", "kick"])
        crush = you.skills[0]
        self.assertIsInstance(crush, SkillRow)
        self.assertEqual((crush.hits, crush.misses, crush.count), (2, 1, 3))
        self.assertEqual(crush.total, 30)
        self.assertEqual(crush.max_hit, 20)
        self.assertAlmostEqual(crush.avg, 15.0)
        kick = you.skills[1]
        self.assertEqual((kick.hits, kick.misses, kick.total, kick.max_hit), (1, 0, 4, 4))

    def test_npc_row_counts_absorb_as_miss(self) -> None:
        zombie = row(self.snap, ZOMBIE)
        self.assertTrue(zombie.is_npc)
        self.assertFalse(zombie.is_you)
        self.assertFalse(zombie.is_pet)
        self.assertEqual(zombie.damage, 16)
        self.assertEqual((zombie.swings, zombie.hits, zombie.misses), (5, 4, 1))
        self.assertAlmostEqual(zombie.hit_pct, 80.0)
        self.assertEqual(zombie.max_hit, 9)
        self.assertAlmostEqual(zombie.avg_hit, 4.0)
        self.assertEqual(zombie.taken, 34 + 15 + 7)  # Pidef + Tovozen + Fluffy hit the zombie
        self.assertEqual([s.skill for s in zombie.skills], ["bite"])
        self.assertEqual(zombie.skills[0].misses, 1)

    def test_healer_row_and_self_heal(self) -> None:
        tovozen = row(self.snap, "Tovozen")
        self.assertEqual(tovozen.damage, 15)
        self.assertEqual(tovozen.swings, 0)
        self.assertAlmostEqual(tovozen.hit_pct, 0.0)
        self.assertEqual(tovozen.heals, 32)
        self.assertEqual(tovozen.healed, 12)  # "heals them" -> self
        self.assertAlmostEqual(tovozen.hps, round(32 / DURATION, 2))
        self.assertEqual([s.skill for s in tovozen.skills], ["Holy Strike"])

    def test_pet_row(self) -> None:
        pet = row(self.snap, "Fluffy")
        self.assertTrue(pet.is_pet)
        self.assertFalse(pet.is_npc)
        self.assertFalse(pet.is_you)
        self.assertEqual(pet.damage, 7)
        self.assertEqual((pet.swings, pet.hits, pet.misses), (1, 1, 0))

    def test_wrapped_name_variant_merged_into_taken(self) -> None:
        wululiso = row(self.snap, "Wululiso")
        self.assertEqual(wululiso.taken, 11)
        self.assertEqual(wululiso.healed, 20)
        self.assertEqual(wululiso.damage, 0)
        self.assertEqual(wululiso.swings, 0)

    def test_share_sums_to_one_over_damage_rows(self) -> None:
        snap = self.snap
        group = [r for r in snap.rows if r.damage > 0 and r.in_group]
        self.assertAlmostEqual(sum(r.share for r in group), 1.0, msg="the group's shares add up to 100 %")
        zombie = row(snap, ZOMBIE)
        self.assertTrue(zombie.is_enemy and not zombie.in_group)
        self.assertAlmostEqual(zombie.share, ENEMY_DAMAGE / (TOTAL_DAMAGE + ENEMY_DAMAGE))
        for r in snap.rows:
            self.assertGreaterEqual(r.share, 0.0)
            self.assertLessEqual(r.share, 1.0)

    def test_colors_are_hex_and_stable(self) -> None:
        for r in self.snap.rows:
            self.assertRegex(r.color, r"^#[0-9a-fA-F]{6}$")
        again = build_snapshot(self.stats, self.enc, PLAYER)
        self.assertEqual([r.color for r in again.rows], [r.color for r in self.snap.rows])
        self.assertNotEqual(row(self.snap, PLAYER).color, row(self.snap, ZOMBIE).color)
        self.assertNotEqual(row(self.snap, "Fluffy").color, row(self.snap, "Tovozen").color)

    def test_rows_for_tabs(self) -> None:
        damage = [r.name for r in snapshot_rows_for_tab(self.snap, "damage")]
        self.assertEqual(damage, [PLAYER, ZOMBIE, "Tovozen", "Fluffy"])
        healing = [r.name for r in snapshot_rows_for_tab(self.snap, "healing")]
        self.assertEqual(healing, ["Tovozen"])
        taken = [r.name for r in snapshot_rows_for_tab(self.snap, "taken")]
        self.assertEqual(taken, [ZOMBIE, "Wululiso", PLAYER])
        self.assertEqual(len(snapshot_rows_for_tab(self.snap, "bogus")), len(self.snap.rows))

    def test_now_extends_open_duration_but_not_closed(self) -> None:
        live = build_snapshot(self.stats, self.enc, PLAYER, now=30.5)
        self.assertAlmostEqual(live.duration, 30.5)
        self.assertAlmostEqual(live.raid_dps, round(TOTAL_DAMAGE / 30.5, 2))
        self.assertAlmostEqual(row(live, PLAYER).dps, round(34 / 30.5, 2))
        earlier = build_snapshot(self.stats, self.enc, PLAYER, now=2.0)
        self.assertAlmostEqual(earlier.duration, DURATION)  # never shrinks below the events

        closed = self.stats.expire(100.0)
        self.assertIs(closed, self.enc)
        snap = build_snapshot(self.stats, closed, PLAYER, now=500.0)
        self.assertTrue(snap.closed)
        self.assertAlmostEqual(snap.duration, DURATION)

    def test_kill_is_listed_and_the_timeout_closes(self) -> None:
        stats = Stats()
        feed(stats, FIGHT)
        stats.add(parse_line("You have slain a stumbling zombie!", 11.0, PLAYER))
        self.assertIsNotNone(stats.current(), "a kill does not end the fight")
        self.assertEqual(build_snapshot(stats, stats.current(), PLAYER, now=11.5).killed, [ZOMBIE])
        stats.expire(11.0 + stats.encounter_timeout_s + 1.0)
        snap = build_snapshot(stats, stats.history[-1], PLAYER)
        self.assertTrue(snap.closed)
        self.assertEqual(snap.killed, [ZOMBIE])
        self.assertAlmostEqual(snap.duration, 10.5, msg="ends at the last swing, not at the kill or the timeout")

    def test_you_row_without_player_name(self) -> None:
        stats = Stats()
        feed(stats, FIGHT, player_name="")
        enc = stats.current()
        assert enc is not None
        snap = build_snapshot(stats, enc, "")
        you = row(snap, "You")
        self.assertTrue(you.is_you)
        self.assertEqual(you.damage, 34)


class EmptyAndEdgeCaseTests(unittest.TestCase):
    def test_empty_encounter(self) -> None:
        stats = Stats()
        enc = Encounter(start=5.0, end=5.0)
        snap = build_snapshot(stats, enc, PLAYER)
        self.assertEqual(snap.rows, [])
        self.assertEqual(snap.total_damage, 0)
        self.assertEqual(snap.raid_dps, 0.0)
        self.assertEqual(snap.duration, 1.0)  # never below one second
        self.assertEqual(snap.label, "unknown")
        self.assertEqual(snap.key, "5.000")
        for tab in ("damage", "healing", "taken"):
            self.assertEqual(snapshot_rows_for_tab(snap, tab), [])

    def test_only_non_combat_events(self) -> None:
        stats = Stats()
        enc = Encounter(start=1.0, end=3.0)
        enc.events.append(Event(ts=1.0, kind="cast", text="Tovozen begins casting Heal.", actor="Tovozen"))
        enc.events.append(Event(ts=3.0, kind="unknown", text="garbled"))
        snap = build_snapshot(stats, enc, PLAYER)
        self.assertEqual(snap.rows, [])
        self.assertEqual(snap.event_count, 2)

    def test_damage_without_amount_is_safe(self) -> None:
        stats = Stats()
        enc = Encounter(start=0.0, end=2.0)
        enc.events.append(Event(ts=0.0, kind="melee_hit", text="x", actor="Tovozen", target=ZOMBIE, amount=None))
        enc.events.append(Event(ts=1.0, kind="melee_miss", text="y", actor=None, target=ZOMBIE))
        snap = build_snapshot(stats, enc, PLAYER)
        tovozen = row(snap, "Tovozen")
        self.assertEqual(tovozen.damage, 0)
        self.assertEqual(tovozen.hits, 1)
        self.assertEqual(tovozen.share, 0.0)
        self.assertEqual(tovozen.avg_hit, 0.0)

    def test_fallback_palette_tokens(self) -> None:
        fn = models._fallback_actor_color
        self.assertEqual(fn("Pidef", is_you=True, is_npc=False, is_pet=False), "#ffd166")
        self.assertEqual(fn(ZOMBIE, is_you=False, is_npc=True, is_pet=False), "#d9655b")
        self.assertEqual(fn("Fluffy", is_you=False, is_npc=False, is_pet=True), "#c084fc")
        party = fn("Tovozen", is_you=False, is_npc=False, is_pet=False)
        self.assertIn(party, models._PARTY_PALETTE)
        self.assertEqual(party, fn("tovozen", is_you=False, is_npc=False, is_pet=False))

    def test_module_is_qt_free(self) -> None:
        source = Path(models.__file__).read_text(encoding="utf-8")
        self.assertNotIn("PySide6", source)
        self.assertNotIn("PyQt", source)


if __name__ == "__main__":
    unittest.main()
