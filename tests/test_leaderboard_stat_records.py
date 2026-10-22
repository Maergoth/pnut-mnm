"""Current signed observations and protected local bests remain separate."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from leaderboard_service.server import GitHubStore
from mnmparse import leaderboard as model
from mnmparse.protected_stats import ProtectedLedger
from tests.test_leaderboard_service import FakeGitHub


FIRST = "2026-10-22T14:00:00Z"
SECOND = "2026-10-22T14:01:00Z"
THIRD = "2026-10-22T14:02:00Z"
KEY = bytes(range(32))


def observation(stamp=FIRST, **changes):
    return {"max_health": 733, "max_mana": 570, "captured_at": stamp,
            "capture_backend": "window", **dict.fromkeys(model.READING_FIELDS, 1), **changes}


def signed(stats=None, *, legacy=False, sequence=1, message=""):
    record = model.make_snapshot(stats or observation(), dict.fromkeys(model.COUNTERS, 0), KEY, sequence,
                                 message=message)
    if legacy:
        record = {name: value for name, value in record.items() if name in model.LEGACY_FIELDS}
        record = resign(record)
    return record


def resign(record):
    record = dict(record)
    record["signature"] = Ed25519PrivateKey.from_private_bytes(KEY).sign(model.signing_bytes(record)).hex()
    return record


class StatSchemaTests(unittest.TestCase):
    def test_legacy_signatures_and_bytes_roundtrip_without_adding_defaults(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] /
                              "leaderboard_worker/test/python-fixtures.json").read_text(encoding="utf-8"))
        legacy = fixture["records"]["current"]
        self.assertEqual(set(legacy), model.LEGACY_FIELDS)
        before = model.signing_bytes(legacy)
        decoded = model.parse_records(model.dump_records([legacy]), strict=True)[0]
        self.assertEqual(decoded, legacy)
        self.assertEqual(model.signing_bytes(decoded), before)
        self.assertEqual(decoded["signature"], legacy["signature"])
        self.assertNotIn("schema_version", decoded)
        self.assertIsNone(decoded.get("strength"))

    def test_version_two_contains_only_allowed_stats_and_independent_exact_totals(self):
        current = signed(observation(strength=0, armor_class=-5, resist_cold=-3))
        self.assertEqual(set(current), model.FIELDS)
        self.assertEqual(current["schema_version"], 2)
        self.assertEqual(current["stat_total"], 6)
        self.assertEqual(current["resist_total"], 4)
        self.assertEqual(current["armor_class"], -5)
        self.assertEqual(model.parse_records(model.dump_records([current]), strict=True), [current])
        for field in ("player_name", "weight", "current_health", "records"):
            self.assertNotIn(field, current)

    def test_partial_observation_does_not_borrow_missing_fields_or_sum_unreadable_groups(self):
        current = signed(observation(wisdom=None, resist_fire=None, armor_class=999999))
        self.assertIsNone(current["stat_total"])
        self.assertIsNone(current["resist_total"])
        zero = signed(observation(**dict.fromkeys(model.READING_FIELDS, 0)))
        self.assertEqual((zero["stat_total"], zero["resist_total"]), (0, 0))

    def test_even_resigned_invalid_totals_and_types_cannot_enter_public_history(self):
        current = signed()
        for changes in ({"stat_total": 9999}, {"resist_total": 9999}, {"wisdom": None},
                        {"strength": True}, {"strength": 1.0}, {"armor_class": model.MAX_STAT + 1},
                        {"resist_cold": -model.MAX_STAT - 1}, {"schema_version": True},
                        {"schema_version": 4}, {"character_name": "fictional"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                model.validate_snapshot(resign({**current, **changes}))
        with self.assertRaises(ValueError):
            model.validate_snapshot({**current, "strength": 2})  # Otherwise-valid unsigned mutation.
        with self.assertRaises(ValueError):
            model.validate_snapshot(resign({name: value for name, value in current.items() if name != "armor_class"}))

    def test_python_service_preserves_legacy_daily_id_and_accepts_both_schema_directions(self):
        legacy = signed(legacy=True)
        modern = signed(sequence=2)
        fake = FakeGitHub([legacy])
        self.assertEqual(GitHubStore("fixture", opener=fake).append(modern), (legacy, False))
        self.assertEqual(fake.puts, 0)
        next_day = signed(observation("2026-10-23T14:00:00Z"), sequence=2)
        self.assertEqual(GitHubStore("fixture", opener=fake).append(next_day), (next_day, True))
        old_client = signed(observation("2026-10-24T14:00:00Z"), legacy=True, sequence=3)
        self.assertEqual(GitHubStore("fixture", opener=fake).append(old_client), (old_client, True))
        self.assertEqual(model.parse_records(fake.data.decode(), strict=True), [legacy, next_day, old_client])

    def test_personal_best_schema_preserves_totals_instead_of_summing_individual_records(self):
        values = {**observation(), "strength": 100, "stamina": 100, "resist_cold": 100,
                  "resist_corruption": 100, "stat_total": 106, "resist_total": 107}
        highs = {field: {"value": values[field], "captured_at": FIRST} for field in model.RECORD_FIELDS}
        current = observation(SECOND, max_health=100, max_mana=None)
        record = model.make_personal_best_snapshot(current, highs, dict.fromkeys(model.COUNTERS, 0), KEY, 2)
        self.assertEqual(record["schema_version"], 3)
        self.assertEqual((record["stat_total"], record["resist_total"]), (106, 107))
        self.assertEqual((record["strength"], record["stamina"], record["max_health"], record["max_mana"]),
                         (100, 100, 733, 570))
        self.assertEqual(record["captured_at"], SECOND)
        self.assertEqual(model.parse_records(model.dump_records([record]), strict=True), [record])
        for changes in ({"stat_total": 206}, {"resist_total": 207}, {"wisdom": None},
                        {"stat_total": True}, {"stat_total": -7 * model.MAX_STAT - 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                model.validate_snapshot(resign({**record, **changes}))
        absent_totals = resign({**record, "stat_total": None, "resist_total": None})
        self.assertEqual(model.validate_snapshot(absent_totals), absent_totals)
        with self.assertRaises(ValueError):
            model.validate_snapshot(resign({**record, "schema_version": 2}))

    def test_public_message_is_normalized_before_signing_then_immutable_for_the_day(self):
        first = signed(message='  <b>SHIT!</b> Café 🐉 & "great"  ')
        self.assertEqual(first["message"], '<b>****!</b> Café 🐉 & "great"')
        self.assertIn(b"Caf\\u00e9", model.signing_bytes(first))
        self.assertIn(b"\\ud83d\\udc09", model.signing_bytes(first))
        for message in ("uncensored shit", " two spaces ", "line\nbreak", None):
            with self.subTest(message=message), self.assertRaises(ValueError):
                model.validate_snapshot(resign({**first, "message": message}))
        with self.assertRaises(ValueError):
            model.validate_snapshot({**first, "message": "Unsigned changed text"})
        fake = FakeGitHub([first])
        retry = signed(sequence=2, message="A replacement message")
        self.assertEqual(retry["submission_id"], first["submission_id"])
        self.assertEqual(GitHubStore("fixture", opener=fake).append(retry), (first, False))
        self.assertEqual(fake.puts, 0)


@unittest.skipUnless(os.name == "nt", "Windows encrypted local records")
class LocalStatRecordTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "records.bin"
        self.ledger = ProtectedLedger(self.path)
        self.assertFalse(self.ledger.error)
        self.ledger.set_capture_eligible(True)

    def test_total_highs_come_from_one_read_never_sum_individual_maxima(self):
        first = observation(strength=100, resist_cold=100)
        second = observation(SECOND, stamina=100, resist_corruption=100, max_health=700, max_mana=500)
        self.ledger.record_stats(first)
        self.ledger.record_stats(second)
        highs = self.ledger.status()["records"]
        self.assertEqual(highs["strength"], {"value": 100, "captured_at": FIRST})
        self.assertEqual(highs["stamina"], {"value": 100, "captured_at": SECOND})
        self.assertEqual(highs["stat_total"], {"value": 106, "captured_at": FIRST})
        self.assertEqual(highs["resist_total"], {"value": 107, "captured_at": FIRST})
        self.assertEqual(highs["max_health"]["value"], 733)
        self.assertEqual(highs["max_mana"]["value"], 570)
        best = self.ledger.prepare_submission()
        self.assertEqual(best["schema_version"], 3)
        self.assertEqual((best["strength"], best["stamina"], best["max_health"], best["max_mana"]),
                         (100, 100, 733, 570))
        self.assertEqual((best["stat_total"], best["resist_total"]), (106, 107))
        self.assertEqual(best["captured_at"], SECOND)
        partial = observation(THIRD, **dict.fromkeys(model.READING_FIELDS, None))
        partial.update(strength=999999, resist_fire=888888)
        self.ledger.record_stats(partial)
        highs = self.ledger.status()["records"]
        self.assertEqual(highs["strength"]["value"], 999999)
        self.assertEqual(highs["resist_fire"]["value"], 888888)
        self.assertEqual(highs["stat_total"]["value"], 106)
        self.assertEqual(highs["resist_total"]["value"], 107)
        self.assertEqual(self.ledger.prepare_submission()["stat_total"], 106)
        self.assertEqual(self.ledger.prepare_submission()["resist_total"], 107)

    def test_individual_partial_records_never_invent_a_complete_total(self):
        for index, field in enumerate(model.READING_FIELDS):
            partial = observation(**dict.fromkeys(model.READING_FIELDS, None))
            partial[field] = index
            self.ledger.record_stats(partial)
        best = self.ledger.prepare_submission()
        self.assertTrue(all(best[field] is not None for field in model.READING_FIELDS))
        self.assertIsNone(best["stat_total"])
        self.assertIsNone(best["resist_total"])
        self.assertNotIn("stat_total", self.ledger.status()["records"])

    def test_restart_preserves_bests_but_requires_fresh_evidence_before_submitting(self):
        self.ledger.record_stats(observation(strength=100))
        restarted = ProtectedLedger(self.path)
        restarted.set_capture_eligible(True)
        with self.assertRaises(ValueError):
            restarted.prepare_submission()
        restarted.record_stats(observation(SECOND, strength=1, max_health=100, max_mana=None))
        best = restarted.prepare_submission()
        self.assertEqual((best["strength"], best["max_health"], best["max_mana"], best["stat_total"]),
                         (100, 733, 570, 106))
        self.assertEqual(best["captured_at"], SECOND)

    def test_zero_negative_and_timestamped_records_survive_only_encrypted_storage(self):
        self.ledger.record_stats(observation(strength=0, armor_class=-7, resist_fire=-8))
        restarted = ProtectedLedger(self.path)
        self.assertEqual(restarted.status()["records"]["strength"], {"value": 0, "captured_at": FIRST})
        self.assertEqual(restarted.status()["records"]["armor_class"]["value"], -7)
        self.assertFalse(restarted.status()["eligible"])
        raw = self.path.read_bytes()
        for private in (b"records", b"stat_total", FIRST.encode(), b"strength"):
            self.assertNotIn(private, raw)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_prepared_public_message_is_optional_and_not_written_as_plaintext(self):
        self.ledger.record_stats(observation())
        self.assertEqual(self.ledger.prepare_submission()["message"], "")
        record = self.ledger.prepare_submission(message="A fictional 🐉 SHIT victory")
        self.assertEqual(record["message"], "A fictional 🐉 **** victory")
        self.assertNotIn(b"fictional", self.path.read_bytes())

    def test_legacy_encrypted_ledger_migrates_without_seeding_old_stats_as_records(self):
        value = self.ledger.file.load()
        value.pop("records")
        value["stats"] = observation(strength=999999)
        self.ledger.file.save(value)
        restarted = ProtectedLedger(self.path)
        self.assertEqual(restarted.status()["records"], {})
        restarted.set_capture_eligible(True)
        restarted.record_stats(observation(strength=2))
        self.assertEqual(restarted.status()["records"]["strength"]["value"], 2)

    def test_mss_ineligible_read_and_identity_change_never_reuse_records(self):
        self.ledger.record_stats(observation(strength=3))
        self.ledger.record_stats(observation(SECOND, strength=999, capture_backend="screen"))
        self.ledger.set_capture_eligible(False)
        self.ledger.record_stats(observation(SECOND, strength=888))
        self.assertEqual(self.ledger.status()["records"]["strength"]["value"], 3)
        self.ledger.set_phrase("a completely new fictional identity")
        self.assertEqual(self.ledger.status()["records"], {})

    def test_corrupt_encrypted_record_fields_fail_closed_without_overwriting(self):
        for bad in ({"strength": {"value": True, "captured_at": FIRST}},
                    {"strength": {"value": 1, "captured_at": "bad"}},
                    {"unknown": {"value": 1, "captured_at": FIRST}}):
            with self.subTest(bad=bad):
                value = self.ledger.file.load()
                value["records"] = bad
                self.ledger.file.save(value)
                before = self.path.read_bytes()
                self.assertTrue(ProtectedLedger(self.path).error)
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
