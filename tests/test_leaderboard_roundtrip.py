"""Desktop HTTP adapter -> local service -> in-memory GitHub -> desktop ACK."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from leaderboard_service.server import GitHubStore, LeaderboardHTTPServer, SnapshotService
from mnmparse.app.leaderboard_client import LeaderboardClient
from mnmparse.leaderboard import parse_records, utc_stamp
from mnmparse.parser import parse_line
from mnmparse.protected_stats import EncryptedFile, ProtectedLedger
from tests.test_leaderboard_service import FakeGitHub


@unittest.skipUnless(os.name == "nt", "Windows desktop ledger")
class LeaderboardRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_loopback_submission_ack_and_daily_retry_preserve_local_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = ProtectedLedger(Path(directory) / "ledger.bin")
            ledger.set_capture_eligible(True)
            ledger.record_event(parse_line("You have slain a rat.", 100, "Owner"), "Owner")
            stats = dict(max_health=733, max_mana=570, captured_at=utc_stamp(), capture_backend="window")
            ledger.record_stats(stats)
            fake = FakeGitHub()
            server = LeaderboardHTTPServer(("127.0.0.1", 0), SnapshotService(GitHubStore("fixture-only", opener=fake)))
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            client = LeaderboardClient(EncryptedFile(Path(directory) / "cache.bin"),
                                       endpoint=f"http://127.0.0.1:{server.server_port}/snapshots")
            messages, accepted = [], []
            client.finished.connect(messages.append)
            client.submitted.connect(ledger.mark_submitted)
            client.submitted.connect(accepted.append)
            client.records_updated.connect(lambda records, _message: ledger.restore_history(records))
            try:
                def submit():
                    with patch("mnmparse.app.leaderboard_client.threading.Thread.start"):
                        self.assertTrue(client.submit(ledger.prepare_submission()))
                    client._worker.run()
                    client._drain()
                submit()
                self.assertEqual(len(accepted), 1, messages)
                self.assertEqual(accepted[0]["capture_backend"], "window")
                self.assertEqual(accepted[0]["max_health"], 733)
                self.assertEqual(accepted[0]["personal_kills"], 1)
                self.assertEqual(ledger.status()["personal_kills"], 1)
                ledger.record_event(parse_line("You have slain a rat.", 103, "Owner"), "Owner")
                ledger.record_stats({**stats, "captured_at": utc_stamp()})
                submit()  # A retry of the same identity/day returns the first public snapshot.
                self.assertEqual(len(accepted), 2, messages)
                self.assertEqual(accepted[1], accepted[0])
                self.assertEqual(ledger.status()["personal_kills"], 2)
                self.assertEqual(fake.puts, 1)
                self.assertEqual(len(parse_records(fake.data.decode("utf-8"))), 1)
                self.assertEqual(client.cache.load()["pending"], None)
                self.assertNotIn(b"personal_kills", client.cache.path.read_bytes())
            finally:
                client.shutdown()
                client.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
