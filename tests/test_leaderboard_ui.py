"""Local Copy, explicit publication, stable retries and native-free worker teardown."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from mnmparse.app.leaderboard_client import DEFAULT_SUBMIT_URL, LeaderboardClient, LeaderboardError, submission_url
from mnmparse.app.leaderboard_page import LeaderboardModel, LeaderboardPage
from mnmparse.leaderboard import clipboard_text, dump_records, make_snapshot, utc_stamp
from mnmparse.protected_stats import EncryptedFile


def record(*, key=1, **changes):
    return make_snapshot({"max_health": 123, "max_mana": 50, "captured_at": utc_stamp(), "capture_backend": "window"},
                         {"personal_kills": 2, "personal_deaths": 1, "high_hit": 100, "coin_looted": 1200, **changes},
                         bytes([key]) * 32, 10)


class MemoryFile:
    def __init__(self, value=None):
        self.value = value
        self.saved = []

    def load(self):
        if self.value is None:
            raise FileNotFoundError
        return json.loads(json.dumps(self.value))

    def save(self, value):
        self.value = json.loads(json.dumps(value))
        self.saved.append(self.value)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.file = MemoryFile()
        self.fetch = Mock()
        self.client = LeaderboardClient(self.file, endpoint="http://127.0.0.1:8765/snapshots", fetch=self.fetch)
        self.history, self.messages, self.accepted = [], [], []
        self.client.records_updated.connect(lambda records, status: self.history.append((records, status)))
        self.client.finished.connect(self.messages.append)
        self.client.submitted.connect(self.accepted.append)

    def tearDown(self):
        self.client.shutdown()
        self.client.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def run_operation(self, action):
        with patch("mnmparse.app.leaderboard_client.threading.Thread.start"):
            self.assertTrue(action())
        self.client._worker.run()
        self.client._drain()

    def test_cache_only_never_contacts_network(self):
        cached = record()
        self.file.value = {"records": [cached], "pending": None}
        self.run_operation(self.client.load_cached)
        self.assertEqual(self.history[0][0], [cached])
        self.fetch.assert_not_called()
        self.assertFalse(self.client._timer.isActive())

    def test_refresh_presents_cache_before_failure_and_preserves_it(self):
        cached = record()
        self.file.value = {"records": [cached], "pending": None}
        self.fetch.side_effect = LeaderboardError("Service unavailable")
        self.run_operation(self.client.refresh)
        self.assertEqual(self.history[0][0], [cached])
        self.assertEqual(self.file.value["records"], [cached])
        self.assertEqual(self.messages, ["Service unavailable"])

    def test_refresh_validates_new_public_rows(self):
        fresh = record()
        self.fetch.return_value = dump_records([fresh]).encode()
        self.run_operation(self.client.refresh)
        self.assertEqual(self.file.value["records"], [fresh])
        self.assertEqual(self.history[-1][0], [fresh])

    def test_retry_uses_fresh_evidence_with_same_daily_id_until_matching_ack(self):
        original = record()
        self.fetch.side_effect = LeaderboardError("Timeout")
        self.run_operation(lambda: self.client.submit(original))
        self.assertEqual(self.file.value["pending"], original)
        newer = record(personal_kills=20)
        self.fetch.side_effect = None
        self.fetch.return_value = json.dumps({"snapshot": original, "created": False}).encode()
        self.run_operation(lambda: self.client.submit(newer))
        self.assertEqual(json.loads(self.fetch.call_args.args[1]), newer)
        self.assertEqual(newer["submission_id"], original["submission_id"])
        self.assertEqual(self.accepted, [original])
        self.assertIsNone(self.file.value["pending"])

    def test_mismatched_ack_does_not_mark_success(self):
        self.fetch.return_value = json.dumps({"snapshot": record(key=2)}).encode()
        self.run_operation(lambda: self.client.submit(record()))
        self.assertFalse(self.accepted)
        self.assertIsNotNone(self.file.value["pending"])

    def test_shutdown_discards_queued_completions(self):
        self.fetch.return_value = b""
        with patch("mnmparse.app.leaderboard_client.threading.Thread.start"):
            self.client.refresh()
        worker = self.client._worker
        worker.run()
        self.client.shutdown()
        self.client._drain()
        self.assertFalse(self.history or self.messages or self.accepted)
        self.assertTrue(worker.cancelled.is_set())

    def test_reentrant_refresh_survives_finished_callback(self):
        self.fetch.return_value = b""
        self.client.finished.connect(lambda _message: self.client.load_cached())
        with patch("mnmparse.app.leaderboard_client.threading.Thread.start"):
            self.client.refresh()
            self.client._worker.run()
            self.client._drain()
        self.assertTrue(self.client.busy)
        self.assertTrue(self.client._timer.isActive())

    def test_endpoint_is_https_or_loopback_without_credentials_or_redirect_target(self):
        for value in ("http://example.com/snapshots", "https://user:pass@example.com/snapshots", "https://example.com/other", "https://example.com/snapshots?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                submission_url(value)
        self.assertEqual(submission_url("https://example.com/snapshots"), "https://example.com/snapshots")

    def test_public_default_preserves_environment_override_and_explicit_disable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(submission_url(), DEFAULT_SUBMIT_URL)
            self.assertEqual(DEFAULT_SUBMIT_URL,
                             "https://pnut-leaderboard-upload.maergoth.chatgpt.site/snapshots")
            self.assertEqual(submission_url(""), "")
        with patch.dict(os.environ, {"PNUT_LEADERBOARD_SUBMIT_URL": "http://127.0.0.1:8765/snapshots"}):
            self.assertEqual(submission_url(), "http://127.0.0.1:8765/snapshots")
            self.assertEqual(submission_url(""), "")
        with patch.dict(os.environ, {"PNUT_LEADERBOARD_SUBMIT_URL": ""}):
            self.assertEqual(submission_url(), "")

    def test_default_endpoint_does_not_start_network_or_require_sign_in(self):
        fetch = Mock()
        with patch.dict(os.environ, {}, clear=True):
            client = LeaderboardClient(MemoryFile(), fetch=fetch)
        try:
            self.assertEqual(client.endpoint, DEFAULT_SUBMIT_URL)
            self.assertEqual(client.submission_unavailable, "")
            self.assertFalse(client.busy)
            fetch.assert_not_called()
        finally:
            client.shutdown()
            client.deleteLater()

    @unittest.skipUnless(os.name == "nt", "Windows user storage")
    def test_real_public_cache_is_not_plaintext(self):
        with tempfile.TemporaryDirectory() as temporary:
            file = EncryptedFile(Path(temporary) / "cache.bin")
            snapshot = record()
            file.save({"records": [snapshot], "pending": snapshot})
            raw = file.path.read_bytes()
            self.assertNotIn(snapshot["alias"].encode(), raw)
            self.assertNotIn(b"personal_kills", raw)
            self.assertEqual(file.load()["records"], [snapshot])


class PageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.snapshot = record()
        self.status = {**self.snapshot, "eligible": True, "copy_eligible": True, "submitted_day": "", "error": ""}
        self.engine = SimpleNamespace(leaderboard_status=lambda: self.status, leaderboard_submission=Mock(return_value=self.snapshot),
                                      leaderboard_copy_text=Mock(return_value=clipboard_text(self.snapshot)))
        self.ledger = SimpleNamespace(mark_submitted=Mock(), set_phrase=Mock(), restore_history=Mock())
        self.client = LeaderboardClient(MemoryFile(), endpoint="http://127.0.0.1:8765/snapshots", fetch=Mock())
        self.page = LeaderboardPage(self.engine, None, None)
        self.page.bind(self.ledger, self.client)

    def tearDown(self):
        self.client.shutdown()
        self.page.close()
        self.page.deleteLater()
        self.client.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_copy_is_local_only_and_does_not_consume_daily_slot(self):
        with patch.object(self.client, "submit") as submit:
            text = self.page.copy_stats()
        self.assertIn(self.snapshot["alias"], text)
        self.assertIn("Coin looted (copper): 1200", text)
        self.assertEqual(self.app.clipboard().text(), text)
        submit.assert_not_called()
        self.ledger.mark_submitted.assert_not_called()

    def test_daily_cap_blocks_submit_but_copy_remains_available(self):
        self.status["submitted_day"] = datetime.now(timezone.utc).date().isoformat()
        self.page.refresh_status()
        self.assertFalse(self.page.submit_button.isEnabled())
        self.assertTrue(self.page.copy_button.isEnabled())
        with patch.object(self.client, "submit") as submit:
            self.assertFalse(self.page.submit_stats())
        submit.assert_not_called()

    def test_desktop_can_copy_locally_but_cannot_submit_and_demo_blocks_both(self):
        self.status.update(eligible=False, reason="Leaderboard submission requires Window Capture")
        self.page.refresh_status()
        self.assertTrue(self.page.copy_button.isEnabled())
        self.assertFalse(self.page.submit_button.isEnabled())
        self.assertIn("Window Capture", self.page.eligibility.text())
        self.assertTrue(self.page.copy_stats())
        self.engine.leaderboard_submission.assert_not_called()
        self.status["copy_eligible"] = False
        self.assertEqual(self.page.copy_stats(), "")
        self.status["eligible"] = True
        self.page.set_demo_active(True)
        self.assertFalse(self.page.submit_stats())
        self.assertEqual(self.page.copy_stats(), "")

    def test_only_ack_marks_submitted_and_history_restores_before_submit(self):
        with patch.object(self.client, "submit", return_value=True) as submit:
            self.assertTrue(self.page.submit_stats())
        self.ledger.restore_history.assert_called()
        self.ledger.mark_submitted.assert_not_called()
        submit.assert_called_once_with(self.snapshot)
        self.client.submitted.emit(self.snapshot)
        self.ledger.mark_submitted.assert_called_once_with(self.snapshot)

    def test_plain_user_messages_hide_storage_failure_details(self):
        self.status.update(error="DPAPI protected payload error", eligible=False)
        self.page.refresh_status()
        self.assertEqual(self.page.eligibility.text(), "Stats history is unavailable.")

    def test_phrase_is_not_saved_in_settings_and_cancel_preserves_identity(self):
        with patch("mnmparse.app.leaderboard_page.QInputDialog.getText", return_value=("sample secret phrase", True)), \
             patch("mnmparse.app.leaderboard_page.QMessageBox.question", return_value=QMessageBox.StandardButton.Cancel):
            self.page.set_phrase()
        self.ledger.set_phrase.assert_not_called()
        with patch("mnmparse.app.leaderboard_page.QInputDialog.getText", return_value=("sample secret phrase", True)), \
             patch("mnmparse.app.leaderboard_page.QMessageBox.question", return_value=QMessageBox.StandardButton.Ok):
            self.page.set_phrase()
        self.ledger.set_phrase.assert_called_once_with("sample secret phrase")

    def test_numeric_and_timestamp_sort_use_values(self):
        model = LeaderboardModel()
        entries = [dict(self.snapshot, high_hit=9, captured_at="2026-10-20T23:00:00Z"),
                   dict(self.snapshot, high_hit=100, captured_at="2026-10-21T01:00:00Z")]
        model.set_records(entries)
        model.sort(5, Qt.SortOrder.DescendingOrder)
        self.assertEqual([entry["high_hit"] for entry in model.records], [100, 9])
        model.sort(7, Qt.SortOrder.AscendingOrder)
        self.assertEqual(model.records[0]["captured_at"], "2026-10-20T23:00:00Z")
        model.records[0]["alias"] = "Tidy Teal Tiger"
        self.assertEqual(model.data(model.index(0, 0)), "Tidy Teal Tiger")
