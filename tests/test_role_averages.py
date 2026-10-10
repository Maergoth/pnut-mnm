"""Protected averages compare active owners with the same combat role."""
from __future__ import annotations

import dataclasses
import json
import unittest

from mnmparse.app.models import ActorRow, EncounterSnapshot, merge_snapshots, snapshot_rows_for_tab
from mnmparse.config import Config
from mnmparse.privacy import metric_available, project_encounter


CFG = Config(player_name="Owner")


def actor(name: str, damage: int = 0, heals: int = 0, **changes) -> ActorRow:
    return ActorRow(
        name=name, damage=damage, dps=damage / 10, taken=30, dtps=3,
        heals=heals, hps=heals / 10, healed=20, swings=0, hits=0, misses=0,
        hit_pct=0, max_hit=0, avg_hit=0, share=0, color="#ffffff",
        is_you=name == "Owner", is_npc=False, is_pet=False, **changes,
    )


def encounter(rows: list[ActorRow], members: list[str] | None = None) -> EncounterSnapshot:
    return EncounterSnapshot(
        key="100", label="Enemy", start=100, end=110, duration=10,
        active_duration=10, closed=True, event_count=40,
        total_damage=sum(r.damage for r in rows), raid_dps=0, killed=[],
        rows=rows, group_members=members if members is not None else [r.name for r in rows],
    )


def group(snap: EncounterSnapshot) -> ActorRow:
    return next(r for r in snap.rows if not r.is_you)


class RoleAverageTests(unittest.TestCase):
    def test_each_role_has_its_own_mean_and_denominator(self):
        raw = encounter([
            actor("Owner", 100, 5), actor("DamageTwo", 200, 6), actor("DamageThree", 400, 7),
            actor("HealerOne", 1, 500), actor("HealerTwo", 2, 1000),
            actor("HealerThree", 3, 2000), actor("HealerFour", 4, 4000),
        ])
        safe = project_encounter(raw, CFG)
        average = group(safe)
        self.assertEqual(average.damage, 230)  # (100 + 200 + 400) / 3, rounded
        self.assertEqual(average.dps, 23)
        self.assertEqual(average.heals, 1900)  # (500 + 1000 + 2000 + 4000) / 4
        self.assertEqual(average.hps, 190)
        self.assertEqual(average.average_counts, {"damage": 3, "heals": 4})
        self.assertEqual(average.taken, 30)  # Other group metrics still use the whole group.
        self.assertEqual(safe.rows[0].damage, 100)
        self.assertEqual(safe.rows[0].heals, 5)
        self.assertEqual(raw.rows[-1].heals, 4000)
        self.assertNotIn("HealerFour", json.dumps(dataclasses.asdict(safe)))

    def test_average_is_a_mean_in_a_skewed_group(self):
        safe = project_encounter(encounter([
            actor("Owner", 100), actor("Two", 100), actor("Three", 1000),
        ]), CFG)
        self.assertEqual(group(safe).damage, 400)  # Median would be 100.

    def test_positive_ties_count_in_both_roles(self):
        safe = project_encounter(encounter([
            actor("Owner", 100, 100), actor("Two", 200, 200), actor("Three", 300, 300),
            actor("Idle"),
        ]), CFG)
        average = group(safe)
        self.assertEqual((average.damage, average.heals), (200, 200))
        self.assertEqual(average.average_counts, {"damage": 3, "heals": 3})

    def test_small_role_is_withheld_without_suppressing_the_other_role(self):
        safe = project_encounter(encounter([
            actor("Owner", 100), actor("DamageTwo", 200), actor("DamageThree", 300),
            actor("HealerOne", 1, 500), actor("HealerTwo", 2, 1000),
        ]), CFG)
        average = group(safe)
        self.assertEqual(average.damage, 200)
        self.assertTrue(metric_available(average, "damage"))
        self.assertTrue(metric_available(average, "dps"))
        self.assertFalse(metric_available(average, "heals"))
        self.assertFalse(metric_available(average, "hps"))
        self.assertFalse(metric_available(average, "heal"))
        self.assertTrue(metric_available(average, "taken"))
        self.assertTrue(metric_available(safe.rows[0], "heals"))
        self.assertEqual((average.heals, average.hps), (0, 0))
        self.assertEqual(average.average_counts["heals"], 0)  # Withhold small-cohort counts too.
        self.assertIn(average, snapshot_rows_for_tab(safe, "healing"))

    def test_absent_and_inactive_roster_members_do_not_clear_privacy_minimum(self):
        safe = project_encounter(encounter(
            [actor("Owner", 100), actor("Idle")], ["Owner", "Idle", "Absent"]), CFG)
        average = group(safe)
        self.assertEqual(average.average_counts, {"damage": 0, "heals": 0})
        self.assertEqual(average.damage, 0)
        self.assertIn(average, snapshot_rows_for_tab(safe, "damage"))

    def test_combined_pet_totals_determine_the_owners_role(self):
        raw = encounter([
            actor("Owner", 10, 50), actor("Pet", 90, pet_owner="Owner"),
            actor("Two", 200), actor("Three", 300),
            actor("HealerOne", 1, 200), actor("HealerTwo", 1, 300), actor("HealerThree", 1, 400),
        ], ["Owner", "Two", "Three", "HealerOne", "HealerTwo", "HealerThree"])
        raw.rows[1].is_pet = True
        safe = project_encounter(raw, CFG)
        self.assertEqual((safe.rows[0].damage, safe.rows[0].heals), (100, 50))
        self.assertEqual(safe.rows[0].attributed_pets, ["Pet"])
        self.assertEqual((group(safe).damage, group(safe).heals), (200, 300))
        self.assertEqual(group(safe).average_counts, {"damage": 3, "heals": 3})

    def test_only_group_player_owners_contribute(self):
        rows = [actor("Owner", 100), actor("Two", 200), actor("Three", 300),
                actor("Outsider", 99999), actor("Enemy", 77777), actor("Npc", 66666),
                actor("UnownedPet", 55555)]
        rows[-3].is_enemy = True
        rows[-2].is_npc = True
        rows[-1].is_pet = True
        safe = project_encounter(encounter(
            rows, ["Owner", "Two", "Three", "Enemy", "Npc", "UnownedPet"]), CFG)
        self.assertEqual(group(safe).damage, 200)
        self.assertEqual(group(safe).average_counts["damage"], 3)

    def test_roster_lookup_is_case_insensitive_and_deduplicated(self):
        safe = project_encounter(encounter([
            actor("Owner", 100), actor("Two", 200), actor("Three", 300),
        ], ["OWNER", "TWO", "three", "Two"]), CFG)
        self.assertEqual(group(safe).damage, 200)
        self.assertEqual(group(safe).average_counts["damage"], 3)
        self.assertEqual(safe.group_average_count, 3)

    def test_pet_only_owner_with_different_roster_case_still_contributes(self):
        raw = encounter([
            actor("Pet", 100, pet_owner="Owner"), actor("Two", 200), actor("Three", 300),
        ], ["OWNER", "Two", "Three"])
        raw.rows[0].is_pet = True
        safe = project_encounter(raw, CFG)
        self.assertEqual(safe.rows[0].damage, 100)
        self.assertEqual(safe.rows[0].attributed_pets, ["Pet"])
        self.assertEqual(group(safe).damage, 200)
        self.assertEqual(group(safe).average_counts["damage"], 3)

    def test_zone_summary_classifies_using_merged_owner_totals(self):
        first = encounter([actor("Owner", 100, 200), actor("Two", 100), actor("Three", 100)])
        second = encounter([actor("Owner", 400), actor("Two", 100), actor("Three", 100)])
        raw = merge_snapshots([first, second], key="zone", label="Zone")
        safe = project_encounter(raw, CFG)
        self.assertEqual(group(safe).damage, 300)  # (500 + 200 + 200) / 3
        self.assertEqual(group(safe).dps, 15)
        self.assertFalse(metric_available(group(safe), "heals"))

    def test_merging_protected_rows_preserves_unavailable_metrics(self):
        first = project_encounter(encounter([
            actor("Owner", 100), actor("Two", 200), actor("Three", 300)]), CFG)
        second = project_encounter(encounter([
            actor("Owner", 100, 200), actor("Two", 200), actor("Three", 300)]), CFG)
        safe = merge_snapshots([first, second], key="merged", label="Your encounters")
        self.assertFalse(metric_available(group(safe), "damage"))
        self.assertEqual(group(safe).damage, 0)
        self.assertIs(project_encounter(safe, CFG), safe)

    def test_confirmed_full_mode_keeps_original_metrics(self):
        raw = encounter([actor("Owner", 100, 200), actor("Peer", 98765)])
        cfg = dataclasses.replace(CFG, casual_mode=False, casual_mode_confirmed=True)
        self.assertIs(project_encounter(raw, cfg), raw)


if __name__ == "__main__":
    unittest.main()
