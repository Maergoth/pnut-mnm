"""Importing raw logs and events files back into encounters and a session."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from mnmparse.importer import import_file
from mnmparse.logwriter import LogWriter
from mnmparse.parser import parse_line

PLAYER = "Maergoth"
T0 = time.mktime((2026, 10, 2, 1, 57, 1, 0, 0, -1))

LINES = [
    (0, "Starting to attack."),
    (1, "You crush a crocodile hatchling for 22 points of damage."),
    (2, "a crocodile hatchling bites YOU for 5 points of damage."),
    (3, "Your Crusader Strike hits a crocodile hatchling for 12 points of damage."),
    (4, "You have slain a crocodile hatchling!"),
    (5, "--You loot [Bone Chips] from a crocodile hatchling's corpse.--"),
    (6, "You loot 3 copper coins from a crocodile hatchling's corpse."),
    (20, "You have entered Night Harbor (East)."),
    (40, "Starting to attack."),
    (41, "You crush a skeletal cleric for 9 points of damage."),
    (42, "Dogabetarolem kicks a skeletal cleric."),
    (43, "a skeletal cleric's casting is interrupted."),
    (44, "Your party member Dogabetarolem has slain a skeletal cleric!"),
]


@dataclass
class Msg:
    text: str
    first_seen: float
    frames_seen: int = 2
    fragment: bool = False


def _write_files(tmp: str) -> tuple[Path, Path]:
    with LogWriter(tmp) as w:
        for offset, text in LINES:
            w.write_raw(Msg(text, T0 + offset))
            w.write_event(parse_line(text, T0 + offset, PLAYER))
    return w.raw_path, w.events_path


class CoinRecountTests(unittest.TestCase):
    def test_old_events_files_get_todays_coin_ratios(self) -> None:
        """Files from before the ratio fix stored 2 silver as 20 copper; the import uses the text."""
        import json

        line = {
            "ts": T0, "kind": "coin", "actor": "You", "amount": 2, "dtype": "silver", "copper": 20,
            "split_copper": None, "text": "You loot 2 silver coins from a goblin scout's corpse.",
        }
        split = {
            "ts": T0 + 1, "kind": "coin", "actor": "Povebizu", "amount": 1, "dtype": "gold", "copper": 100,
            "split_copper": 10,
            "text": "Povebizu loots 1 gold coin from a goblin scout's corpse, and you receive "
                    "1 silver coin from a goblin scout's corpse as your split.",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events_2026-10-01.jsonl"
            path.write_text(json.dumps(line) + "\n" + json.dumps(split) + "\n", encoding="utf-8")
            result = import_file(path, player_name=PLAYER)
        self.assertEqual(result.session.coin_total, 200 + 10_000)
        self.assertEqual(result.session.coin_split, 100)


class ImporterTests(unittest.TestCase):
    def _check(self, result) -> None:
        self.assertEqual(result.messages, len(LINES))
        self.assertEqual(len(result.encounters), 2)
        first, second = result.encounters
        self.assertEqual(first.label, "a crocodile hatchling")
        self.assertEqual(first.total_damage, 22 + 12, "the group's damage; the hatchling's 5 is not counted")
        self.assertEqual(first.killed, ["a crocodile hatchling"])
        self.assertEqual(second.label, "a skeletal cleric")
        self.assertEqual((first.zone, second.zone), ("", "Night Harbor (East)"), "tagged with the zone at start")
        rows = {r.name: r for r in second.rows}
        self.assertEqual(rows["Dogabetarolem"].cc, 1)
        s = result.session
        self.assertEqual((s.items, s.coin_total, s.kills, s.cc_total), (1, 3, 2, 1))
        self.assertEqual(s.encounters, 2)
        self.assertAlmostEqual(result.started, T0)
        self.assertAlmostEqual(result.ended, T0 + 44)

    def test_raw_log_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw, _events = _write_files(tmp)
            result = import_file(raw, player_name=PLAYER, encounter_timeout_s=12.0)
            self.assertEqual(result.kind, "log")
            self._check(result)

    def test_events_file_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _raw, events = _write_files(tmp)
            result = import_file(events, player_name=PLAYER, encounter_timeout_s=12.0, keep_events=True)
            self.assertEqual(result.kind, "events")
            self.assertEqual(len(result.events), len(LINES))
            self._check(result)

    def test_old_daily_log_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "combat_2026-10-01.log"
            body = "﻿[Thu Oct 01 17:11:03 2026] You crush a stumbling zombie for 8 points of damage.\n" \
                   "[Thu Oct 01 17:11:04 2026] Your party member Pidef has slain a stumbling zombie!\n"
            path.write_text(body, encoding="utf-8")
            result = import_file(path, player_name=PLAYER)
            self.assertEqual(result.messages, 2)
            self.assertEqual(len(result.encounters), 1)
            self.assertEqual(result.encounters[0].killed, ["a stumbling zombie"])

    def test_missing_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            import_file(Path("does-not-exist.log"))


if __name__ == "__main__":
    unittest.main()
