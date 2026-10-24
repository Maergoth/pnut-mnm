from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from mnmparse import leaderboard
from leaderboard_service.server import (
    API_ROOT, BRANCH, CONTENTS_URL, FILE_PATH, MAX_REQUEST_BYTES,
    GitHubStore, LeaderboardHTTPServer, Settings, SnapshotService, StorageError,
)


NOW = datetime(2026, 10, 22, 14, 0, tzinfo=timezone.utc)


def snapshot(identifier="1" * 32, *, at=NOW, **overrides):
    stats = {"captured_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "max_health": 321,
             "max_mana": 123, "capture_backend": "window"}
    counters = {"personal_kills": 4, "personal_deaths": 1, "high_hit": 123, "coin_looted": 456}
    sequence = overrides.pop("sequence", 1)
    stats.update({key: value for key, value in overrides.items() if key not in counters})
    counters.update({key: value for key, value in overrides.items() if key in counters})
    return leaderboard.make_snapshot(stats, counters, bytes.fromhex(identifier) * 2, sequence)


def blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw).hexdigest()


class FakeGitHub:
    def __init__(self, records=(), *, missing=False):
        self.data = None if missing else leaderboard.dump_records(list(records)).encode("utf-8")
        self.branch_exists = True
        self.requests = []
        self.puts = 0
        self.conflicts = []
        self.encoding_none = False
        self.metadata_overrides = {}
        self.error_status = 0
        self.put_error_status = 0
        self.bad_put_confirmation = False

    def _error(self, request, status):
        raise HTTPError(request.full_url, status, "Fake upstream error", {}, io.BytesIO(b"private upstream detail"))

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        if self.error_status:
            self._error(request, self.error_status)
        if request.method == "GET" and request.full_url == CONTENTS_URL + "?ref=" + BRANCH:
            if self.data is None:
                self._error(request, 404)
            result = {"type": "file", "sha": blob_sha(self.data), "size": len(self.data),
                      "encoding": "none" if self.encoding_none else "base64",
                      "content": "" if self.encoding_none else base64.b64encode(self.data).decode("ascii"),
                      "download_url": "https://untrusted.invalid/never-follow-this"}
            result.update(self.metadata_overrides)
        elif request.method == "GET" and request.full_url == API_ROOT + "/branches/" + BRANCH:
            if not self.branch_exists:
                self._error(request, 404)
            result = {"name": BRANCH}
        elif request.method == "GET" and request.full_url == API_ROOT + "/git/blobs/" + blob_sha(self.data):
            result = {"sha": blob_sha(self.data), "encoding": "base64", "size": len(self.data),
                      "content": base64.b64encode(self.data).decode("ascii")}
        elif request.method == "PUT" and request.full_url == CONTENTS_URL:
            self.puts += 1
            if self.put_error_status:
                self._error(request, self.put_error_status)
            if self.conflicts:
                action = self.conflicts.pop(0)
                if callable(action):
                    action(self)
                self._error(request, 409)
            payload = json.loads(request.data)
            expected_sha = None if self.data is None else blob_sha(self.data)
            if payload.get("sha") != expected_sha:
                self._error(request, 409)
            if payload["branch"] != BRANCH:
                raise AssertionError("Wrong destination branch")
            self.data = base64.b64decode(payload["content"], validate=True)
            result = {"content": {"sha": blob_sha(self.data)}, "commit": {"sha": "f" * 40}}
            if self.bad_put_confirmation:
                result = {}
        else:
            raise AssertionError("Unexpected GitHub request: " + request.full_url)
        return io.BytesIO(json.dumps(result).encode("utf-8"))


class GitHubStoreTests(unittest.TestCase):
    def store(self, fake):
        return GitHubStore("test-server-secret", opener=fake)

    def test_new_snapshot_appends_to_fixed_path_with_sha_and_server_token(self):
        first, second = snapshot(), snapshot("2" * 32)
        fake = FakeGitHub([first])
        original_sha = blob_sha(fake.data)
        self.assertEqual(self.store(fake).append(second), (second, True))
        self.assertEqual(leaderboard.parse_records(fake.data.decode(), strict=True), [first, second])
        put = fake.requests[-1]
        payload = json.loads(put.data)
        self.assertEqual(payload["sha"], original_sha)
        self.assertEqual(payload["branch"], "leaderboard-data")
        self.assertEqual(put.full_url, CONTENTS_URL)
        self.assertTrue(put.full_url.endswith("/" + FILE_PATH))
        self.assertEqual(put.get_header("Authorization"), "Bearer test-server-secret")
        self.assertNotIn("test-server-secret", fake.data.decode())
        self.assertEqual(fake.puts, 1)

    def test_same_identifier_returns_existing_public_record_without_rewriting(self):
        first = snapshot(at=NOW - timedelta(hours=2))
        fake = FakeGitHub([first])
        incoming = snapshot(max_health=999)
        self.assertEqual(self.store(fake).append(incoming), (first, False))
        self.assertEqual(fake.puts, 0)

    def test_conflicting_writer_is_preserved_after_retry(self):
        first, concurrent, incoming = snapshot(), snapshot("2" * 32), snapshot("3" * 32)
        fake = FakeGitHub([first])
        fake.conflicts = [lambda api: setattr(api, "data", leaderboard.dump_records([first, concurrent]).encode())]
        self.assertEqual(self.store(fake).append(incoming), (incoming, True))
        self.assertEqual(leaderboard.parse_records(fake.data.decode(), strict=True), [first, concurrent, incoming])
        self.assertEqual(fake.puts, 2)

    def test_conflict_that_already_saved_same_id_returns_idempotent_success(self):
        record = snapshot()
        fake = FakeGitHub()
        fake.conflicts = [lambda api: setattr(api, "data", leaderboard.dump_records([record]).encode())]
        self.assertEqual(self.store(fake).append(record), (record, False))
        self.assertEqual(fake.puts, 1)

    def test_conflict_retry_is_bounded(self):
        fake = FakeGitHub()
        fake.conflicts = [None] * 10
        with self.assertRaises(StorageError):
            self.store(fake).append(snapshot())
        self.assertEqual(fake.puts, 3)
        self.assertEqual(fake.data, b"")

    def test_missing_file_is_created_only_on_an_existing_fixed_branch(self):
        fake = FakeGitHub(missing=True)
        record = snapshot()
        self.assertEqual(self.store(fake).append(record), (record, True))
        self.assertIn("/branches/leaderboard-data", fake.requests[1].full_url)
        self.assertNotIn("sha", json.loads(fake.requests[-1].data))
        missing_branch = FakeGitHub(missing=True)
        missing_branch.branch_exists = False
        with self.assertRaises(StorageError):
            self.store(missing_branch).append(record)
        self.assertEqual(missing_branch.puts, 0)

    def test_omitted_large_contents_reads_immutable_blob_not_download_url(self):
        first = snapshot()
        fake = FakeGitHub([first])
        fake.encoding_none = True
        original_sha = blob_sha(fake.data)
        self.assertEqual(self.store(fake).append(first), (first, False))
        self.assertEqual(fake.requests[1].full_url, API_ROOT + "/git/blobs/" + original_sha)
        self.assertEqual(fake.requests[0].get_header("Accept"), "application/vnd.github.object+json")
        self.assertEqual(fake.requests[1].get_header("Accept"), "application/vnd.github+json")
        self.assertTrue(all(request.full_url.startswith(API_ROOT) for request in fake.requests))

    def test_invalid_existing_history_metadata_and_checksum_never_overwrite(self):
        for overrides, raw in (({}, b'{"player_name":"private"}\n'),
                               ({"size": leaderboard.MAX_BYTES + 1}, b""),
                               ({"sha": "f" * 40}, b""),
                               ({"type": "symlink"}, b"")):
            with self.subTest(overrides=overrides, raw=raw):
                fake = FakeGitHub()
                fake.data, fake.metadata_overrides = raw, overrides
                with self.assertRaises(StorageError):
                    self.store(fake).append(snapshot())
                self.assertEqual(fake.puts, 0)

    def test_capacity_and_nonconflict_upstream_failures_do_not_retry_put(self):
        fake = FakeGitHub()
        with patch("leaderboard_service.server.dump_records", side_effect=ValueError("full")):
            with self.assertRaises(StorageError):
                self.store(fake).append(snapshot())
        self.assertEqual(fake.puts, 0)
        fake.put_error_status = 403
        with self.assertRaises(StorageError) as caught:
            self.store(fake).append(snapshot())
        self.assertEqual(fake.puts, 1)
        self.assertNotIn("test-server-secret", str(caught.exception))
        self.assertNotIn("private upstream detail", str(caught.exception))

    def test_truncated_upstream_http_response_is_reported_as_storage_unavailable(self):
        from http.client import IncompleteRead
        def incomplete(request, *, timeout):
            raise IncompleteRead(b"upstream body", 99)
        with self.assertRaises(StorageError):
            GitHubStore("test-secret", opener=incomplete).append(snapshot())

    def test_unconfirmed_write_is_not_reported_as_published_and_retry_is_idempotent(self):
        fake = FakeGitHub()
        fake.bad_put_confirmation = True
        record = snapshot()
        with self.assertRaises(StorageError):
            self.store(fake).append(record)
        self.assertEqual(self.store(fake).append(record), (record, False))
        self.assertEqual(fake.puts, 1)

    def test_next_daily_record_requires_strictly_increasing_sequence_and_nondecreasing_totals(self):
        old = snapshot(at=NOW - timedelta(days=1), sequence=7)
        for values in ({"sequence": 7}, {"sequence": 6}, {"sequence": 8, "personal_kills": 3},
                       {"sequence": 8, "personal_deaths": 0}, {"sequence": 8, "high_hit": 122},
                       {"sequence": 8, "coin_looted": 455}):
            with self.subTest(values=values):
                fake = FakeGitHub([old])
                with self.assertRaises(ValueError):
                    self.store(fake).append(snapshot(**values))
                self.assertEqual(fake.puts, 0)
        fake = FakeGitHub([old])
        incoming = snapshot(sequence=8, personal_kills=5, max_health=300)
        self.assertEqual(self.store(fake).append(incoming), (incoming, True))
        self.assertEqual(leaderboard.parse_records(fake.data.decode(), strict=True), [incoming])

    def test_history_limits_are_per_key_and_daily_duplicate_keeps_original_even_if_newer(self):
        old = snapshot(at=NOW - timedelta(days=1), sequence=7, personal_kills=100)
        current = snapshot("2" * 32)
        fake = FakeGitHub([old, current])
        different_key = snapshot("3" * 32, personal_kills=0)
        self.assertEqual(self.store(fake).append(different_key), (different_key, True))
        repeated_today = snapshot("2" * 32, sequence=2, personal_kills=500)
        self.assertEqual(self.store(fake).append(repeated_today), (current, False))
        self.assertEqual(fake.puts, 1)

    def test_retry_rechecks_sequence_against_the_newly_read_history(self):
        older = snapshot(at=NOW - timedelta(days=2), sequence=1)
        newer = snapshot(at=NOW - timedelta(days=1), sequence=2)
        incoming = snapshot(sequence=2)
        fake = FakeGitHub([older])
        fake.conflicts = [lambda api: setattr(api, "data", leaderboard.dump_records([older, newer]).encode())]
        with self.assertRaises(ValueError):
            self.store(fake).append(incoming)
        self.assertEqual(fake.puts, 1)
        self.assertEqual(leaderboard.parse_records(fake.data.decode(), strict=True), [older, newer])


class SnapshotServiceTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        self.service = SnapshotService(GitHubStore("test-token", opener=self.api), clock=lambda: NOW)

    def test_fresh_boundary_and_same_day_are_required_before_any_github_request(self):
        for offset in (-301, 301):
            with self.subTest(offset=offset):
                with self.assertRaises(ValueError):
                    self.service.submit(snapshot(at=NOW + timedelta(seconds=offset)))
        midnight = datetime(2026, 10, 23, 0, 1, tzinfo=timezone.utc)
        midnight_service = SnapshotService(GitHubStore("test-token", opener=self.api), clock=lambda: midnight)
        with self.assertRaises(ValueError):
            midnight_service.submit(snapshot(at=midnight - timedelta(minutes=2)))
        self.assertEqual(self.api.requests, [])
        for offset, identifier in ((-300, "2" * 32), (300, "3" * 32)):
            self.assertTrue(self.service.submit(snapshot(identifier, at=NOW + timedelta(seconds=offset)))[1])

    def test_extra_private_fields_and_invalid_types_rejected_without_storage(self):
        for key, value in (("player_name", "Never publish"), ("GITHUB_TOKEN", "client-secret"),
                           ("path", "main/config.json"), ("max_health", True)):
            record = snapshot()
            record[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.service.submit(record)
        self.assertEqual(self.api.requests, [])

    def test_numeric_mutation_invalid_signature_and_signed_nonwindow_payload_are_rejected(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        changed = snapshot()
        changed["personal_kills"] += 1  # Signature belongs to the original numeric values.
        bad_signature = snapshot()
        bad_signature["signature"] = "0" * 128
        nonwindow = snapshot()
        nonwindow["capture_backend"] = "mss"
        nonwindow["signature"] = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("1" * 32) * 2).sign(
            leaderboard.signing_bytes(nonwindow)).hex()
        for record in (changed, bad_signature, nonwindow):
            with self.subTest(record=record["capture_backend"]), self.assertRaises(ValueError):
                self.service.submit(record)
        self.assertEqual(self.api.requests, [])

    def test_configuration_keeps_token_out_of_repr_and_requires_server_secret(self):
        with self.assertRaises(ValueError):
            Settings.from_env({})
        settings = Settings.from_env({"GITHUB_TOKEN": "hidden-secret"})
        self.assertEqual((settings.bind, settings.port), ("127.0.0.1", 8765))
        self.assertNotIn("hidden-secret", repr(settings))
        with self.assertRaises(ValueError):
            Settings.from_env({"GITHUB_TOKEN": "secret", "PNUT_LEADERBOARD_PORT": "0"})


class LeaderboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()
        service = SnapshotService(GitHubStore("test-token", opener=self.api), clock=lambda: NOW)
        self.server = LeaderboardHTTPServer(("127.0.0.1", 0), service)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def post(self, record):
        return self.request("POST", "/snapshots", json.dumps(record), {"Content-Type": "application/json"})

    def test_new_and_duplicate_http_responses_match_client_contract(self):
        record = snapshot()
        status, body, headers = self.post(record)
        self.assertEqual((status, body), (201, {"snapshot": record, "created": True}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.post(snapshot(max_health=999))[:2], (200, {"snapshot": record, "created": False}))
        self.assertEqual(self.api.puts, 1)

    def test_private_extra_field_and_duplicate_json_key_are_rejected(self):
        record = snapshot()
        record["player_name"] = "Never publish"
        self.assertEqual(self.post(record)[0], 400)
        duplicate = json.dumps(snapshot())[:-1] + ',"max_health":999}'
        self.assertEqual(self.request("POST", "/snapshots", duplicate, {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.api.requests, [])

    def test_body_and_endpoint_constraints_do_not_reach_github(self):
        self.assertEqual(self.request("POST", "/snapshots", "x" * (MAX_REQUEST_BYTES + 1),
                                      {"Content-Type": "application/json"})[0], 413)
        self.assertEqual(self.request("POST", "/snapshots", "{}", {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/snapshots?path=elsewhere", "{}")[0], 404)
        self.assertEqual(self.request("GET", "/snapshots")[0], 404)
        self.assertEqual(self.request("GET", "/health")[:2], (200, {"status": "ok"}))
        self.assertEqual(self.api.requests, [])

    def test_upstream_error_returns_generic_unavailable_without_upstream_detail(self):
        self.api.error_status = 403
        status, body, _ = self.post(snapshot())
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "Leaderboard storage is temporarily unavailable"})
        self.assertNotIn("test-token", json.dumps(body))

    def test_ambiguous_lengths_and_transfer_encoding_are_rejected_without_reading_body(self):
        for headers in ((('Content-Length', '2'), ('Content-Length', '2')),
                        (('Content-Length', '2'), ('Transfer-Encoding', 'chunked')),
                        (('Content-Length', '-1'),), ()):
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
                try:
                    connection.putrequest("POST", "/snapshots")
                    connection.putheader("Content-Type", "application/json")
                    for key, value in headers:
                        connection.putheader(key, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                finally:
                    connection.close()
        self.assertEqual(self.api.requests, [])


if __name__ == "__main__":
    unittest.main()
