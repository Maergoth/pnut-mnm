"""Capture-only cumulative values survive restart without a plaintext save file."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mnmparse.leaderboard import (COUNTERS, alias_for_key, clipboard_text, daily_id,
    dump_records, make_snapshot, parse_records, phrase_key, public_key, utc_stamp, validate_snapshot)
from mnmparse.parser import parse_line
from mnmparse.protected_stats import EncryptedFile, ProtectedLedger, ProtectionError, WindowsProtector


@unittest.skipUnless(os.name == "nt", "Windows DPAPI")
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "stats.bin"
        self.ledger = ProtectedLedger(self.path)
        self.assertEqual(self.ledger.error, "")
        self.ledger.set_capture_eligible(True)

    def stats(self, **changes):
        return dict(max_health=1234567, max_mana=7654321, captured_at=utc_stamp(), capture_backend="window", **changes)

    def event(self, text, ts=100, **changes):
        event = parse_line(text, ts, "Owner")
        self.assertNotEqual(event.kind, "unknown", text)
        event = replace(event, **changes)
        self.ledger.record_event(event)
        return event

    def test_real_dpapi_restart_and_no_plaintext_values_or_key(self):
        self.ledger.record_stats(self.stats())
        self.event("You pierce a rat for 123456 points of Physical Damage.")
        self.event("You have slain a rat.")
        self.event("You loot 1234 copper coins from a rat's corpse.")
        before = self.ledger.prepare_submission()
        raw = self.path.read_bytes()
        for text in (b"1234567", b"7654321", b"123456", b"personal_kills", before["public_key"].encode()):
            self.assertNotIn(text, raw)
        restarted = ProtectedLedger(self.path)
        self.assertEqual(restarted.status()["alias"], before["alias"])
        self.assertEqual(restarted.status()["personal_kills"], 1)
        self.assertEqual(restarted.status()["high_hit"], 123456)
        self.assertEqual(restarted.status()["coin_looted"], 1234)
        self.assertFalse(restarted.status()["eligible"])
        with self.assertRaises(ProtectionError):
            restarted.prepare_submission()
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

    def test_damage_mutations_and_plain_json_fail_closed_without_overwrite(self):
        original = self.path.read_bytes()
        damaged = bytearray(original)
        damaged[-12] ^= 1
        self.path.write_bytes(damaged)
        restarted = ProtectedLedger(self.path)
        self.assertTrue(restarted.error)
        self.assertEqual(self.path.read_bytes(), bytes(damaged))
        with self.assertRaises(ProtectionError):
            restarted.prepare_submission()
        self.path.write_text(json.dumps({"personal_kills": 100000}), encoding="utf-8")
        self.assertTrue(ProtectedLedger(self.path).error)

    def test_only_exact_personal_events_and_gross_corpse_coins(self):
        first = self.event("You pierce a rat for 22 points of Physical Damage.")
        self.ledger.record_event(first)
        self.event("You pierce a rat for 22 points of Physical Damage.", 100.1)
        self.event("Other pierces a rat for 999 points of Physical Damage.")
        self.event("You pierce a rat for 999 points of Physical Damage.", estimated=True)
        self.event("Your pet Toto pierces a rat for 999 points of Physical Damage.")
        self.event("You have slain a rat.", 101)
        self.event("a rat has been slain by You.", 101.1)
        self.event("You have slain a rat.", 101.2)  # another identical mob remains a new kill
        self.event("You have been slain by a rat.", 102)
        self.event("a rat has slain You.", 102.1)
        self.event("You loot 40 copper coins from a rat's corpse, and you receive 10 copper coins as your split.")
        self.event("Other loots 800 copper coins from a rat's corpse, and you receive 100 copper coins as your split.")
        self.event("You sell Bone Chips for 9 gold coins.")
        status = self.ledger.status()
        self.assertEqual({k: status[k] for k in COUNTERS},
                         dict(personal_kills=2, personal_deaths=1, high_hit=22, coin_looted=40))

    def test_non_window_capture_cannot_add_or_submit_and_needs_fresh_read_after_switch(self):
        self.ledger.record_stats(self.stats())
        self.assertTrue(self.ledger.status()["eligible"])
        self.ledger.set_capture_eligible(False)
        self.event("You have slain a rat.")
        self.ledger.record_stats({**self.stats(), "capture_backend": "screen", "max_health": 999})
        self.assertEqual(self.ledger.status()["personal_kills"], 0)
        with self.assertRaises(ProtectionError):
            self.ledger.prepare_submission()
        self.ledger.set_capture_eligible(True)
        with self.assertRaises(ProtectionError):
            self.ledger.prepare_submission()
        self.ledger.record_stats(self.stats())
        self.assertEqual(self.ledger.prepare_submission()["max_health"], 1234567)

    def test_stale_read_is_not_uploadable_and_mark_sent_only_on_ack(self):
        self.ledger.record_stats(self.stats())
        record = self.ledger.prepare_submission()
        self.assertEqual(self.ledger.status()["submitted_day"], "")
        self.ledger.mark_submitted(record)
        self.assertEqual(self.ledger.status()["submitted_day"], record["captured_at"][:10])
        with patch("mnmparse.protected_stats.time.monotonic", return_value=self.ledger._stats_live_at + 11):
            with self.assertRaises(ProtectionError):
                self.ledger.prepare_submission()

    def test_phrase_identity_repeats_without_storing_phrase_and_change_resets_counts(self):
        phrase = "seven silver sleeping swans"
        self.ledger.set_phrase(phrase)
        alias = self.ledger.status()["alias"]
        self.event("You have slain a rat.")
        self.ledger.set_phrase(phrase)
        self.assertEqual(self.ledger.status()["personal_kills"], 1)
        other = ProtectedLedger(Path(self.tmp.name) / "another.bin")
        other.set_phrase(phrase)
        self.assertEqual(other.status()["alias"], alias)
        self.assertNotIn(phrase.encode(), self.path.read_bytes())
        self.assertNotIn(phrase, json.dumps(self.ledger.file.load()))
        self.ledger.set_phrase("a different secret phrase")
        self.assertEqual(self.ledger.status()["personal_kills"], 0)

    def test_public_cache_also_uses_encrypted_file(self):
        cache = EncryptedFile(Path(self.tmp.name) / "cache.bin")
        content = {"etag": "test", "body": "max_health: 1234567"}
        cache.save(content)
        self.assertEqual(cache.load(), content)
        self.assertNotIn(b"1234567", cache.path.read_bytes())


class SignedModelTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.stats = dict(max_health=733, max_mana=570, captured_at="2026-10-22T08:00:00Z", capture_backend="window")
        self.counters = dict.fromkeys(COUNTERS, 0)
        self.record = make_snapshot(self.stats, self.counters, self.key, 1)

    def test_mutations_unsigned_fields_and_other_capture_are_rejected(self):
        for changes in ({"max_health": 9000}, {"personal_kills": 10}, {"alias": "Brave Blue Bear"},
                        {"character_name": "Owner"}, {"capture_backend": "screen"}, {"max_mana": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_snapshot({**self.record, **changes})
        with self.assertRaises(ValueError):
            make_snapshot({**self.stats, "capture_backend": "screen"}, self.counters, self.key, 2)

    def test_alias_and_daily_id_depend_on_public_key_without_character_name(self):
        key = public_key(self.key)
        self.assertEqual(self.record["alias"], alias_for_key(key))
        self.assertEqual(self.record["submission_id"], daily_id(key, self.stats["captured_at"]))
        same_day = make_snapshot({**self.stats, "captured_at": "2026-10-22T23:00:00Z"}, self.counters, self.key, 2)
        self.assertEqual(same_day["submission_id"], self.record["submission_id"])
        self.assertNotIn("Owner", json.dumps(self.record))

    def test_jsonl_roundtrip_dedup_signature_and_clipboard_scope(self):
        text = dump_records([self.record])
        self.assertEqual(parse_records(text + text), [self.record])
        self.assertEqual(parse_records(text + '{"bad":1}\n'), [self.record])
        with self.assertRaises(ValueError):
            parse_records(text + '{"bad":1}\n', strict=True)
        copied = clipboard_text(self.record)
        self.assertIn("Max Health: 733", copied)
        self.assertIn("Max Mana: 570", copied)
        for unwanted in ("Weight", "Endurance", "signature", "public_key", "current"):
            self.assertNotIn(unwanted, copied)
