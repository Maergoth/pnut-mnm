"""Only reliable chat evidence may add an automatic party member."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster


class PartyEvidenceTests(unittest.TestCase):
    def test_nearby_loot_and_coin_do_not_add_members(self) -> None:
        roster = PartyRoster("Viewer")
        for text in (
            "--Pidef loots [Bone Chips] from a rat's corpse.--",
            "Pidef loots 7 copper coins from a rat's corpse.",
            "Pidef loots 7 copper coins from a rat's corpse, and you receive",
            "Pidef loots 7 copper coins from a rat's corpse, and you receive 12 copper coins as your split.",
        ):
            with self.subTest(text=text):
                event = parse_line(text, 1.0, "Viewer")
                self.assertIn(event.kind, {"loot", "coin"})
                self.assertIsNone(event.split_copper)
                self.assertFalse(roster.observe(event))
                self.assertEqual(roster.members(), set())

    def test_valid_coin_split_identifies_member_even_when_share_is_zero(self) -> None:
        for share in (0, 2):
            with self.subTest(share=share):
                roster = PartyRoster("Viewer")
                event = parse_line(
                    f"Pidef loots 7 copper coins from a rat's corpse, and you receive {share} copper coins as your split.",
                    1.0, "Viewer",
                )
                self.assertEqual(event.split_copper, share)
                self.assertTrue(roster.observe(event))
                self.assertEqual(roster.members(), {"Pidef"})

    def test_explicit_party_messages_add_member(self) -> None:
        for text in (
            "Pidef has joined the party.",
            "Pidef is now the leader of the party.",
            "Your party member Pidef has slain a rat!",
            "Your party member Pidef has been slain by a rat!",
        ):
            with self.subTest(text=text):
                roster = PartyRoster("Viewer")
                self.assertTrue(roster.observe(parse_line(text, 1.0, "Viewer")))
                self.assertEqual(roster.members(), {"Pidef"})

    def test_unaccepted_invitation_is_not_membership(self) -> None:
        roster = PartyRoster("Viewer")
        self.assertFalse(roster.observe(parse_line("Pidef has invited you to their party.", 1.0, "Viewer")))
        self.assertEqual(roster.members(), set())

    def test_disband_clears_pending_invitation(self) -> None:
        roster = PartyRoster("Viewer")
        for ts, text in enumerate((
            "Pidef has invited you to their party.",
            "Your party has been disbanded.",
            "You have joined the party.",
        ), 1):
            roster.observe(parse_line(text, float(ts), "Viewer"))
        self.assertEqual(roster.members(), set())

    def test_manual_choices_override_explicit_evidence_and_survive_disband(self) -> None:
        roster = PartyRoster("Viewer")
        roster.set_manual("Pidef", False)
        roster.set_manual("Tovozen", True)
        roster.set_pet_owner("a charmed rat", "Tovozen")
        roster.observe(parse_line("Pidef has joined the party.", 1.0, "Viewer"))
        self.assertEqual(roster.members(), {"Tovozen"})
        roster.observe(parse_line("Your party has been disbanded.", 2.0, "Viewer"))
        self.assertEqual(roster.members(), {"Tovozen"})
        self.assertIs(roster.manual_state("Pidef"), False)
        self.assertEqual(roster.pet_owners(), {"a charmed rat": "Tovozen"})


class PartyMigrationTests(unittest.TestCase):
    def test_all_older_formats_drop_automatic_members_but_keep_manual_choices(self) -> None:
        now = time.time()
        for version in (None, 1, 2, 3):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as tmp:
                old = {
                    "saved": now,
                    "seen": {"Pidef": now},
                    "inferred": {"Gozif": now},
                    "shared": {"Zabomir": [2, now]},
                    "manual_in": ["Tovozen"],
                    "manual_out": ["Kulepu"],
                    "pet_owners": {"a charmed rat": "Tovozen", "Fetuvo": "Viewer"},
                }
                if version is not None:
                    old["version"] = version
                path = Path(tmp) / "party.json"
                path.write_text(json.dumps(old), encoding="utf-8")
                roster = PartyRoster("Viewer")
                self.assertTrue(roster.load(path))
                self.assertEqual(roster.seen(), set())
                self.assertEqual(roster.inferred(), set())
                self.assertEqual(roster.members(), {"Tovozen"})
                self.assertIs(roster.manual_state("Kulepu"), False)
                self.assertEqual(roster.pet_owners(), old["pet_owners"])
                roster.note_fight(["Zabomir"], now)
                self.assertEqual(roster.members(), {"Tovozen"})
                roster.save(path)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(saved["version"], 4)
                self.assertNotIn("inferred", saved)
                self.assertNotIn("shared", saved)
                restored = PartyRoster("Viewer")
                self.assertTrue(restored.load(path))
                self.assertEqual(restored.members(), roster.members())
                self.assertEqual(restored.pet_owners(), roster.pet_owners())

    def test_shared_fight_refreshes_only_previously_explicit_members(self) -> None:
        now = time.time()
        roster = PartyRoster("Viewer")
        for name in ("Pidef", "Tovozen"):
            roster.observe(parse_line(f"{name} has joined the party.", now - 7 * 3600, "Viewer"))
        self.assertFalse(roster.note_fight(["Pidef", "Stranger"], now))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "party.json"
            roster.save(path)
            restored = PartyRoster("Viewer")
            self.assertTrue(restored.load(path))
        self.assertEqual(restored.members(), {"Pidef"})


if __name__ == "__main__":
    unittest.main()
