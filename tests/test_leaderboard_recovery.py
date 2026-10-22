"""Only signed public history can seed a recovered protected identity."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from mnmparse.leaderboard import COUNTERS, MAX_NUMBER, make_snapshot
from mnmparse.parser import parse_line
from mnmparse.protected_stats import ProtectedLedger, ProtectionError


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class LeaderboardRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.bin"
        self.ledger = ProtectedLedger(self.path)
        self.assertEqual(self.ledger.error, "")
        self.ledger.set_capture_eligible(True)

    def event(self, text, at=100, **changes):
        event = replace(parse_line(text, at, "Owner"), **changes)
        self.assertNotEqual(event.kind, "unknown", text)
        self.ledger.record_event(event, "Owner")
        return event

    def stats(self):
        return {"max_health": 733, "max_mana": 570, "captured_at": "2026-10-22T14:00:00Z",
                "capture_backend": "window"}

    def public_record(self, *, sequence=40, key=None, stamp="2026-10-21T14:00:00Z", **changes):
        if key is None:
            key = bytes.fromhex(self.ledger.file.load()["key"])
        counters = {"personal_kills": 10, "personal_deaths": 2, "high_hit": 500, "coin_looted": 1200}
        counters.update(changes)
        return make_snapshot({**self.stats(), "captured_at": stamp}, counters, key, sequence)

    def local_events(self):
        self.event("You have slain a rat.")
        self.event("You have been slain by a rat.", 101)
        self.event("You pierce a rat for 100 points of Physical Damage.", 102)
        self.event("You loot 3 copper coins from a rat's corpse.", 103)

    def test_matching_history_merges_baseline_once_preserving_current_capture(self):
        self.local_events()
        self.ledger.record_stats(self.stats())
        local = self.ledger.file.load()
        record = self.public_record()
        self.assertTrue(self.ledger.restore_history([record]))
        value = self.ledger.file.load()
        self.assertEqual(value["counters"], {"personal_kills": 11, "personal_deaths": 3,
                                             "high_hit": 500, "coin_looted": 1203})
        self.assertEqual(value["stats"], local["stats"])
        self.assertEqual(value["sequence"], 40 + local["sequence"] + 1)
        self.assertEqual(value["history_floor"], 40)
        self.assertEqual(value["submitted_day"], "2026-10-21")
        self.assertTrue(value["history_restored"])
        self.assertFalse(self.ledger.restore_history([record]))
        restarted = ProtectedLedger(self.path)
        self.assertFalse(restarted.restore_history([record]))
        self.assertEqual(restarted.file.load()["counters"], value["counters"])
        self.assertNotIn(b"history_restored", self.path.read_bytes())

    def test_baseline_uses_each_maximum_and_latest_sequence_not_row_order(self):
        first = self.public_record(sequence=3, personal_kills=20, high_hit=800)
        second = self.public_record(sequence=9, stamp="2026-10-22T14:00:00Z", coin_looted=2000)
        self.ledger.restore_history([second, first])
        value = self.ledger.file.load()
        self.assertEqual(value["counters"], {"personal_kills": 20, "personal_deaths": 2,
                                             "high_hit": 800, "coin_looted": 2000})
        self.assertEqual(value["history_floor"], 9)
        self.assertGreater(value["sequence"], 9)
        self.assertEqual(value["submitted_day"], "2026-10-22")

    def test_invalid_unrelated_and_empty_history_do_not_mark_or_write_baseline(self):
        before = self.path.read_bytes()
        bad = self.public_record()
        bad["personal_kills"] = 999  # Existing signature cannot authorize a changed total.
        other = self.public_record(key=b"\x02" * 32)
        for records in ([], [None, "not a record", {}], [bad, other]):
            self.assertFalse(self.ledger.restore_history(records))
            self.assertFalse(self.ledger.file.load()["history_restored"])
            self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(self.ledger.restore_history([self.public_record()]))

    def test_damaged_local_file_is_never_replaced_by_public_history(self):
        record = self.public_record()
        damaged = b"not a protected ledger"
        self.path.write_bytes(damaged)
        with self.assertRaises(ProtectionError):
            self.ledger.restore_history([record])
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_first_published_prepared_snapshot_does_not_double_local_totals(self):
        self.local_events()
        self.ledger.record_stats(self.stats())
        record = self.ledger.prepare_submission()
        before = self.ledger.file.load()
        self.assertIn(record["signature"], before["prepared_signatures"])
        self.assertTrue(self.ledger.restore_history([record]))
        self.assertEqual(self.ledger.file.load()["counters"], before["counters"])
        self.ledger.mark_submitted(record)
        self.assertEqual(self.ledger.file.load()["counters"], before["counters"])
        self.assertEqual(self.ledger.status()["submitted_day"], record["captured_at"][:10])

    def test_timeout_restart_retains_prepared_signature_and_later_local_events(self):
        self.local_events()
        self.ledger.record_stats(self.stats())
        pending = self.ledger.prepare_submission()
        self.event("You have slain another rat.", 110)
        self.ledger.record_stats({**self.stats(), "captured_at": "2026-10-22T14:00:01Z"})
        self.assertNotEqual(self.ledger.prepare_submission()["signature"], pending["signature"])
        before = self.ledger.file.load()["counters"]
        restarted = ProtectedLedger(self.path)
        self.assertTrue(restarted.restore_history([pending]))
        self.assertEqual(restarted.file.load()["counters"], before)
        restarted.mark_submitted(pending)
        self.assertEqual(restarted.file.load()["counters"], before)

    def test_first_ack_before_public_refresh_does_not_double_its_local_values(self):
        self.local_events()
        self.ledger.record_stats(self.stats())
        record = self.ledger.prepare_submission()
        before = self.ledger.file.load()["counters"]
        self.ledger.mark_submitted(record)
        self.assertEqual(self.ledger.file.load()["counters"], before)
        self.assertFalse(self.ledger.restore_history([record]))

    def test_ack_without_prior_refresh_recovers_old_identity_before_marking_sent(self):
        record = self.public_record(stamp="2026-10-22T14:00:00Z")
        self.local_events()
        self.ledger.record_stats(self.stats())
        self.ledger.prepare_submission()  # A different signed record from this recovery installation.
        self.ledger.mark_submitted(record)
        value = self.ledger.file.load()
        self.assertEqual(value["counters"]["personal_kills"], 11)
        self.assertEqual(value["counters"]["coin_looted"], 1203)
        self.assertTrue(value["history_restored"])
        self.assertFalse(self.ledger.restore_history([record]))

    def test_recovery_caps_numbers_and_older_ledger_fields_migrate(self):
        value = self.ledger.file.load()
        for field in ("history_restored", "history_floor", "prepared_signatures"):
            value.pop(field)
        value["counters"] = dict.fromkeys(COUNTERS, 1)
        value["sequence"] = 1
        self.ledger.file.save(value)
        largest = self.public_record(sequence=MAX_NUMBER, **dict.fromkeys(COUNTERS, MAX_NUMBER))
        self.assertTrue(self.ledger.restore_history([largest]))
        restored = self.ledger.file.load()
        self.assertEqual(restored["counters"], dict.fromkeys(COUNTERS, MAX_NUMBER))
        self.assertEqual(restored["sequence"], MAX_NUMBER)
        self.ledger.record_stats(self.stats())
        self.assertEqual(self.ledger.file.load()["sequence"], MAX_NUMBER)

    def test_phrase_change_resets_history_and_prepared_signatures(self):
        self.ledger.restore_history([self.public_record()])
        self.ledger.record_stats(self.stats())
        self.ledger.prepare_submission()
        self.ledger.set_phrase("another quiet persistent identity")
        value = self.ledger.file.load()
        self.assertFalse(value["history_restored"])
        self.assertEqual(value["history_floor"], 0)
        self.assertEqual(value["prepared_signatures"], [])
        self.assertEqual(value["counters"], dict.fromkeys(COUNTERS, 0))

    def test_named_self_counts_while_other_and_pet_ownership_do_not(self):
        self.event("Owner pierces a rat for 75 points of Physical Damage.")
        self.event("Owner loots 7 copper coins from a rat's corpse.")
        self.event("Owner has slain a rat.")
        self.event("Owner has been slain by a rat.", 105)
        self.event("Other loots 900 copper coins from a rat's corpse.")
        self.event("Other has been slain by a rat.", 112)
        self.event("Your pet Owner pierces a rat for 999 points of Physical Damage.")
        self.event("Your pet Owner has been slain by a rat.", 120)
        value = self.ledger.status()
        self.assertEqual({field: value[field] for field in COUNTERS},
                         {"personal_kills": 1, "personal_deaths": 1, "high_hit": 75, "coin_looted": 7})

    def test_invalid_encrypted_state_is_rejected_without_assert_statements(self):
        value = self.ledger.file.load()
        value["sequence"] = True
        self.ledger.file.save(value)
        with self.assertRaises(ProtectionError):
            self.ledger._load()


if __name__ == "__main__":
    unittest.main()
