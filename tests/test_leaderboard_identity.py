"""A leaderboard entry belongs to a signed identity, not an upload day or alias."""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app.leaderboard_client import _Worker
from mnmparse.app.leaderboard_page import LeaderboardModel, LeaderboardPage
from mnmparse.leaderboard import (COUNTERS, LEGACY_FIELDS, dump_records, latest_records,
                                 make_personal_best_snapshot, make_snapshot, parse_records,
                                 phrase_key, signing_bytes, validate_snapshot)
from tests.test_leaderboard_service import FakeGitHub, GitHubStore


COLLIDING_KEYS = (
    bytes.fromhex("8bcbb4c131df56f7c79066016241cc4bdf4e58db55c4f674e88b22365bd2e2ad"),
    bytes.fromhex("0bba869d7f392cbcaca6b8935ddc7fc3a8c50846d884959333fb7da475957511"),
)


def record(*, key=1, day=22, sequence=2, legacy=False, **changes):
    private_key = bytes([key]) * 32 if isinstance(key, int) else key
    stats = {"captured_at": f"2026-10-{day:02d}T14:00:00Z", "capture_backend": "window",
             "max_health": 100, "max_mana": 50}
    counters = {"personal_kills": 10, "personal_deaths": 1, "high_hit": 100, "coin_looted": 200}
    counters.update(changes)
    row = make_snapshot(stats, counters, private_key, sequence)
    if legacy:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        row = {name: value for name, value in row.items() if name in LEGACY_FIELDS}
        row["signature"] = Ed25519PrivateKey.from_private_bytes(private_key).sign(signing_bytes(row)).hex()
    return validate_snapshot(row)


class MemoryFile:
    def __init__(self, records):
        self.value = {"records": records, "pending": None}

    def load(self):
        return json.loads(json.dumps(self.value))

    def save(self, value):
        self.value = json.loads(json.dumps(value))


def run_client(records, operation="cache", incoming=None, accepted=None, public=None):
    file = MemoryFile(records)
    fetch = Mock(return_value=(dump_records(public).encode() if public is not None else
                              json.dumps({"snapshot": accepted}).encode()))
    worker = _Worker(file, operation, incoming, "https://service.invalid/snapshots", fetch)
    worker.run()
    messages = []
    while True:
        try:
            messages.append(worker.messages.get_nowait())
        except Empty:
            return file.value, messages, fetch


class LatestIdentityTests(unittest.TestCase):
    def test_historical_parser_keeps_signed_history_and_selector_keeps_original_latest_payload(self):
        old = record(day=21, sequence=1, legacy=True)
        new = record()
        history = parse_records(dump_records([new, old]), strict=True)
        self.assertEqual(history, [new, old])
        self.assertEqual(latest_records(history), [new])
        self.assertIs(latest_records(history)[0], history[0])
        self.assertNotIn("schema_version", old)
        self.assertEqual(validate_snapshot(new), new)

    def test_sequence_order_prevents_rollback_even_if_input_or_timestamp_order_differs(self):
        higher = record(day=21, sequence=10)
        lower = record(day=22, sequence=9)
        self.assertEqual(latest_records([higher, lower]), [higher])
        self.assertEqual(latest_records([lower, higher]), [higher])
        same_sequence_later = record(day=23, sequence=10)
        self.assertEqual(latest_records([same_sequence_later, higher]), [same_sequence_later])

    def test_real_alias_collision_does_not_merge_different_signed_identities(self):
        first, second = [record(key=key) for key in COLLIDING_KEYS]
        self.assertEqual(first["alias"], second["alias"])
        self.assertNotEqual(first["public_key"], second["public_key"])
        self.assertEqual(latest_records([first, second]), [first, second])

    def test_old_invalid_record_is_checked_before_latest_selection(self):
        old = record(day=21, sequence=1)
        invalid = {**old, "personal_kills": 999}
        raw = json.dumps(invalid) + "\n" + dump_records([record()])
        with self.assertRaises(ValueError):
            latest_records(parse_records(raw, strict=True))

    def test_cache_load_keeps_validated_signed_history_without_network(self):
        old, new = record(day=21, sequence=1, legacy=True), record()
        _state, messages, fetch = run_client([old, new, old])
        self.assertEqual(messages[0], ("records", ([old, new], "Cached public entries")))
        fetch.assert_not_called()

    def test_refresh_keeps_complete_validated_history_for_recovery(self):
        old, new = record(day=21, sequence=1), record()
        state, messages, _fetch = run_client([old], "refresh", public=[new, old])
        self.assertEqual(state["records"], [new, old])
        rows = [args[0] for kind, args in messages if kind == "records"]
        self.assertEqual(rows, [[old], [new, old]])

    def test_ack_keeps_previous_day_for_recovery_and_preserves_signed_payloads(self):
        old, new = record(day=21, sequence=1), record()
        state, messages, _fetch = run_client([old], "submit", incoming=new, accepted=new)
        self.assertEqual(state["records"], [old, new])
        self.assertIsNone(state["pending"])
        self.assertIn(("submitted", (new,)), messages)

    def test_stale_ack_cannot_overwrite_a_newer_cached_identity_row(self):
        old, new = record(day=21, sequence=1), record()
        state, messages, _fetch = run_client([new], "submit", incoming=old, accepted=old)
        self.assertEqual(state["records"], [new, old])
        self.assertIn(("submitted", (old,)), messages)
        self.assertEqual([args[0] for kind, args in messages if kind == "records"], [[new], [new, old]])
        model = LeaderboardModel()
        model.set_records(state["records"])
        self.assertEqual(model.records, [new])

    def test_client_does_not_collapse_distinct_keys_with_same_alias(self):
        first, second = [record(key=key) for key in COLLIDING_KEYS]
        _state, messages, _fetch = run_client([first, second])
        self.assertEqual(messages[0][1][0], [first, second])


class IdentityStoreTests(unittest.TestCase):
    def test_new_day_replaces_all_older_rows_for_key_and_preserves_other_identity(self):
        old, previous = record(day=20, sequence=1), record(day=21, sequence=2)
        other, new = record(key=2), record(sequence=3)
        fake = FakeGitHub([old, other, previous])
        self.assertEqual(GitHubStore("synthetic-token", opener=fake).append(new), (new, True))
        self.assertEqual(parse_records(fake.data.decode(), strict=True), [other, new])

    def test_historical_counter_floor_is_checked_before_old_rows_are_removed(self):
        old = record(day=20, sequence=1, personal_kills=100)
        previous = record(day=21, sequence=2, personal_kills=10)
        fake = FakeGitHub([old, previous])
        with self.assertRaises(ValueError):
            GitHubStore("synthetic-token", opener=fake).append(record(sequence=3, personal_kills=20))
        self.assertEqual(fake.puts, 0)
        self.assertEqual(parse_records(fake.data.decode(), strict=True), [old, previous])

    def test_real_alias_collision_stays_separate_during_identity_upsert(self):
        first, second = [record(key=key, day=21, sequence=1) for key in COLLIDING_KEYS]
        updated = record(key=COLLIDING_KEYS[0], sequence=2)
        fake = FakeGitHub([first, second])
        GitHubStore("synthetic-token", opener=fake).append(updated)
        self.assertEqual(parse_records(fake.data.decode(), strict=True), [second, updated])

    def test_unrelated_upload_keeps_history_and_cannot_erase_another_identity_counter_floor(self):
        old = record(day=20, sequence=1, personal_kills=100)
        latest = record(day=21, sequence=2, personal_kills=10)
        other = record(key=2)
        fake = FakeGitHub([old, latest])
        store = GitHubStore("synthetic-token", opener=fake)
        store.append(other)
        self.assertEqual(parse_records(fake.data.decode(), strict=True), [old, latest, other])
        with self.assertRaises(ValueError):
            store.append(record(sequence=3, personal_kills=20))
        self.assertEqual(fake.puts, 1)
        self.assertEqual(parse_records(fake.data.decode(), strict=True), [old, latest, other])

    def test_replacing_an_identity_at_record_capacity_does_not_need_an_extra_slot(self):
        previous, other = record(day=21, sequence=1), record(key=2)
        updated = record()
        fake = FakeGitHub([previous, other])
        with patch("mnmparse.leaderboard.MAX_RECORDS", 2):
            self.assertEqual(GitHubStore("synthetic-token", opener=fake).append(updated), (updated, True))
            self.assertEqual(parse_records(fake.data.decode(), strict=True), [other, updated])


class IdentityRecoveryViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    @unittest.skipUnless(os.name == "nt", "Windows protected identity storage")
    def test_phrase_recovery_keeps_old_counter_and_stat_bests_while_view_shows_one_entry(self):
        from mnmparse.protected_stats import ProtectedLedger
        with tempfile.TemporaryDirectory() as folder:
            ledger = ProtectedLedger(Path(folder) / "identity.bin")
            phrase = "a fictional recovery identity phrase"
            key = phrase_key(phrase)
            old = make_personal_best_snapshot(
                {"captured_at": "2026-10-21T14:00:00Z", "capture_backend": "window"},
                {"max_health": {"value": 100}, "max_mana": {"value": 50}, "strength": {"value": 999}},
                {**dict.fromkeys(COUNTERS, 0), "personal_kills": 100}, key, 1)
            new = record(key=key, sequence=2, personal_kills=10)
            engine = SimpleNamespace(leaderboard_status=lambda: ledger.status())
            page = LeaderboardPage(engine, None, None)
            page.ledger = ledger
            try:
                _state, messages, _fetch = run_client([], "refresh", public=[old, new])
                for kind, args in messages:
                    if kind == "records":
                        page._records(*args)
                self.assertEqual(page.model.rowCount(), 1)
                self.assertEqual(page.model.records, [new])
                self.assertEqual(page._records_history, [old, new])
                self.assertEqual(ledger.status()["personal_kills"], 0)
                # Selecting a phrase after the table was populated must use
                # complete signed history rather than only its visible row.
                ledger.set_phrase(phrase)
                page._restore_history()
                self.assertEqual(ledger.status()["personal_kills"], 100)
                self.assertEqual(ledger.status()["records"]["strength"]["value"], 999)
                self.assertEqual(ledger.file.load()["history_floor"], 2)
                self.assertIn("1 entries", page.message.text())
            finally:
                page.close()
                page.deleteLater()
