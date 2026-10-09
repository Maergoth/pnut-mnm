"""Clipboard summaries report the same merged owners as the combat meters."""

from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot, merge_snapshots
from mnmparse.export import ExportFormat, format_snapshot, has_people
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def snapshot(lines: list[str], pets: tuple[str, ...] = ("Kulepu",)):
    stats = Stats(player_name=PLAYER)
    for pet in pets:
        stats.roster.set_pet_owner(pet, PLAYER)
    for offset, line in enumerate(lines):
        stats.add(parse_line(line, 100.0 + offset, PLAYER))
    return build_snapshot(stats, stats.current(), PLAYER)


class PetRollupExportTests(unittest.TestCase):
    def test_damage_ranks_share_and_label_use_the_combined_parse(self):
        snap = snapshot([
            "Tamsin has joined the party.",
            "You crush a rat for 20 points of damage.",
            "Kulepu slashes a rat for 30 points of damage.",
            "Tamsin crushes a rat for 10 points of damage.",
        ])
        fmt = ExportFormat("{damage}: {actors}", "{rank}:{name}:{damage}:{share:.0f}", " | ")
        self.assertEqual(format_snapshot(snap, fmt),
                         "60: 1:Maergoth + Maergoth's Pet:50:83 | 2:Tamsin:10:17")

    def test_healing_export_includes_pet_heals_once(self):
        snap = snapshot([
            "You crush a rat for 20 points of damage.",
            "Your Healing Touch heals you for 22 Health.",
            "Kulepu's Heal heals Maergoth for 18 Health.",
        ])
        fmt = ExportFormat("{actors}", "{name} {heal}", sort="healing")
        self.assertEqual(format_snapshot(snap, fmt), "Maergoth + Maergoth's Pet 40")

    def test_pet_only_fight_lists_the_owner_and_multiple_pets_survive_zone_merge(self):
        one = snapshot(["Kulepu bites a rat for 7 points of damage."])
        two = snapshot(["Fizzy bites a rat for 11 points of damage."], pets=("Fizzy",))
        fmt = ExportFormat("{actors}", "{name} {damage}")
        self.assertTrue(has_people(one, fmt))
        self.assertEqual(format_snapshot(one, fmt), "Maergoth + Maergoth's Pet 7")
        summary = merge_snapshots([one, two], key="zone", label="zone")
        self.assertEqual(format_snapshot(summary, fmt), "Maergoth + Maergoth's Pets 18")
        self.assertEqual(summary.total_damage, 18)


if __name__ == "__main__":
    unittest.main()
