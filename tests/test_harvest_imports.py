"""PNUT harvest history rebuilds and exports the same resource totals as live capture."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.config import Config
from mnmparse.importer import import_file, import_files, iter_jsonl_events
from mnmparse.logwriter import EQ_TIMESTAMP_FORMAT, LogWriter, raw_header
from mnmparse.parser import parse_line
from mnmparse.privacy import project_session
from mnmparse.session import SessionStats, filter_session
from mnmparse.tracker import Message


PLAYER = "HarvestFixture"
START = time.mktime((2026, 10, 26, 4, 40, 42, 0, 0, -1))
LINES = (
    (0, "You harvest [Wood]!"),
    (0, "You harvest [Resin]!"),
    (3, "You harvest [Wood] !"),
    (3, "You harvest [Resin) I"),
    (6, "You begin casting Chop."),
    (7, "There is nothing to forage here."),
    (8, "You harvest [Wood]!"),
    (9, "You loot [Bone Chips] from a rat's corpse."),
)
EXPECTED = [("Wood", 3), ("Resin", 2)]


def write_pair(directory):
    """Use PNUT's real writer, headers and timestamps without capture or OCR."""
    with LogWriter(directory) as writer:
        for offset, text in LINES:
            stamp = START + offset
            writer.write_raw(Message(text, stamp, 2))
            writer.write_event(parse_line(text, stamp, PLAYER))
    return writer.raw_path, writer.events_path


def write_historical_events(path):
    """Older PNUT versions classified harvests as personal with no actor."""
    rows = []
    for offset, text in LINES:
        event = dataclasses.asdict(parse_line(text, START + offset, PLAYER))
        if text.startswith("You harvest "):
            event.update(kind="personal", actor=None, raw_actor=None,
                         item="stale cached item spelling", amount=None)
        rows.append(event)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def write_raw(path, lines):
    body = [raw_header()]
    body.extend(f"[{time.strftime(EQ_TIMESTAMP_FORMAT, time.localtime(stamp))}] {text}"
                for stamp, text in lines)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


class HarvestImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def assert_resources(self, snapshot):
        self.assertEqual(snapshot.harvests, 5)
        self.assertEqual(snapshot.harvests_by_item, EXPECTED)
        self.assertEqual(snapshot.items, 1, "Harvests must not inflate corpse-loot totals")
        self.assertEqual(snapshot.items_by_name, [("Bone Chips", 1)])
        self.assertEqual(snapshot.crafts, 0)

    def test_real_raw_log_counts_both_same_timestamp_resources_and_later_identical_results(self):
        raw, _events = write_pair(self.root)
        for include_personal in (False, True):
            with self.subTest(include_personal=include_personal):
                result = import_file(raw, player_name=PLAYER, include_personal=include_personal, keep_events=True)
                self.assert_resources(result.session)
                self.assertEqual(result.counts.get("harvest"), 5)
                harvests = [event for event in result.events if event.kind == "harvest"]
                self.assertEqual([(event.item, event.ts) for event in harvests[:2]],
                                 [("Wood", START), ("Resin", START)])
                self.assertEqual([event.item for event in harvests], ["Wood", "Resin", "Wood", "Resin", "Wood"])

    def test_old_personal_jsonl_is_reparsed_from_text_without_trusting_stale_item_fields(self):
        path = write_historical_events(self.root / "events_2026-10-26_044042.jsonl")
        result = import_file(path, player_name=PLAYER, include_personal=False, keep_events=True)
        self.assert_resources(result.session)
        self.assertEqual(result.counts.get("harvest"), 5)
        self.assertNotIn("stale cached item spelling", json.dumps(dataclasses.asdict(result.session)))

    def test_historical_event_iterator_can_feed_the_same_session_aggregator(self):
        path = write_historical_events(self.root / "events_historical.jsonl")
        session = SessionStats(PLAYER, include_personal=False, started=START)
        events = list(iter_jsonl_events(path))
        self.assertEqual(sum(event.kind == "personal" and event.item is not None for event in events), 5)
        for event in events:
            session.add(event)
        self.assert_resources(session.snapshot(now=START + 10))

    def test_paired_raw_and_event_files_remain_independent_sessions_with_equal_totals(self):
        raw, events = write_pair(self.root)
        results = import_files([events, raw], player_name=PLAYER, include_personal=False)
        self.assertEqual(len(results), 2)
        self.assertEqual({result.kind for result in results}, {"log", "events"})
        for result in results:
            self.assert_resources(result.session)
            self.assertEqual(result.backlog_dropped, 0)

    def test_restart_backlog_excludes_old_harvests_but_keeps_new_later_resources(self):
        tail = ["You harvest [Wood]!", "You harvest [Resin]!",
                "There is nothing to forage here.", "You begin casting Chop."]
        first = write_raw(self.root / "combat_first.log", [(START + index, text) for index, text in enumerate(tail)])
        restart = START + 7
        second = write_raw(self.root / "combat_restarted.log",
                           [(restart, text) for text in tail] +
                           [(restart + 3, "You harvest [Wood]!"), (restart + 4, "You harvest [Resin]!")])
        before, after = import_files([second, first], player_name=PLAYER, keep_events=True)
        self.assertEqual((before.backlog_dropped, after.backlog_dropped), (0, len(tail)))
        self.assertEqual((before.session.harvests, after.session.harvests), (2, 2))
        self.assertEqual(after.session.harvests_by_item, [("Resin", 1), ("Wood", 1)])
        self.assertEqual([event.text for event in after.events],
                         ["You harvest [Wood]!", "You harvest [Resin]!"])

    def test_imported_resources_belong_only_to_the_imported_viewer_in_personal_projections(self):
        raw, _events = write_pair(self.root)
        snapshot = import_file(raw, player_name=PLAYER).session
        self.assert_resources(filter_session(snapshot, PLAYER.lower()))
        self.assertEqual(filter_session(snapshot, "DifferentViewer").harvests, 0)
        for viewer, count in ((PLAYER, 5), ("DifferentViewer", 0), ("", 0)):
            with self.subTest(viewer=viewer):
                projected = project_session(snapshot, Config(player_name=viewer))
                self.assertEqual(projected.harvests, count)
                self.assertEqual(projected.harvests_by_item, EXPECTED if count else [])
                self.assertEqual(snapshot.harvests, 5, "Projection must not mutate imported history")


class HarvestSessionExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_session_json_export_includes_count_and_item_breakdown_in_normal_and_casual_modes(self):
        from PySide6.QtCore import QCoreApplication, QEvent, QSettings
        from PySide6.QtTest import QTest
        from mnmparse.app.pages import SessionPage

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            raw, _events = write_pair(root / "input")
            snapshot = import_file(raw, player_name=PLAYER).session
            for casual in (False, True):
                with self.subTest(casual=casual):
                    cfg = Config(player_name=PLAYER, casual_mode=casual, casual_mode_confirmed=True,
                                 log_dir=str(root / ("casual-output" if casual else "normal-output")))
                    engine = SimpleNamespace(session_snapshot=lambda **_kwargs: snapshot, is_running=False)
                    settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
                    page = SessionPage(engine, cfg, settings)
                    try:
                        page._on_export()
                        deadline = time.monotonic() + 3
                        while getattr(page, "_session_export_job", None) is not None and time.monotonic() < deadline:
                            self.app.processEvents()
                            QTest.qWait(5)
                        self.assertIsNone(page._session_export_job, "Background session export did not finish")
                        self.assertIn("Exported", page._status.text())
                        exported = list((Path(cfg.log_dir) / "exports").rglob("session_*.json"))
                        self.assertEqual(len(exported), 1)
                        payload = json.loads(exported[0].read_text(encoding="utf-8"))
                        self.assertEqual(payload["harvests"], 5)
                        self.assertEqual(payload["harvests_by_item"], [["Wood", 3], ["Resin", 2]])
                        self.assertEqual(payload["items"], 1)
                        self.assertEqual(snapshot.harvests_by_item, EXPECTED)
                    finally:
                        page.close()
                        page.deleteLater()
                        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
