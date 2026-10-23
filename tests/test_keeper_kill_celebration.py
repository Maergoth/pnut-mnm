"""The observed Keeper kill reaches fanfare dispatch without weakening capture evidence."""
from __future__ import annotations

import dataclasses
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.app.notable_kills import NotableKillController
from mnmparse.capture import WgcWindowSource
from mnmparse.config import Config
from mnmparse.npc_wiki import NotableNpcs
from mnmparse.ocr import OcrLine
from mnmparse.stats import Stats
from mnmparse.tracker import Tracker


KEEPER = "Keeper of the Road"
UNKNOWN = "Wanderer of the Roadl"
ZONE = "Caves of Irem"


class _CachedDetails(QObject):
    notables_updated = Signal(str, object)

    def __init__(self):
        super().__init__()
        record = NotableNpcs(ZONE, (KEEPER, UNKNOWN))
        self.repo = SimpleNamespace(cached_notables=lambda _zone: record)
        self.panel = SimpleNamespace(enabled=True)
        self.show_npc = Mock()
        self.ensure_notables = Mock()


class KeeperKillCelebrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name="FixtureOwner", log_dir=self.temporary.name,
                          player_status_enabled=False)
        self.engine = Engine(self.cfg)
        self.engine._state = "running"
        self.engine._window_found = True
        self.engine._source = object.__new__(WgcWindowSource)
        self.engine._writer = Mock()
        self.engine._stats = Stats(player_name=self.cfg.player_name)
        self.engine._stats.zone_changes.append((0, ZONE))
        self.ledger = Mock()
        self.engine.set_protected_ledger(self.ledger)
        self.details = _CachedDetails()
        self.controller = NotableKillController(
            self.details, current_zone=lambda: self.engine.current_zone,
            active=lambda: self.engine.state == "running")
        self.engine.live_group_kill.connect(self.controller.on_kill)
        self.engine.live_group_activity.connect(self.controller.on_activity)
        self.celebrated, self.events, self.kills = [], [], []
        self.controller.celebrated.connect(self.celebrated.append)
        self.engine.message.connect(lambda message, event: self.events.append((message, event)))
        self.engine.live_group_kill.connect(lambda event, zone: self.kills.append((event, zone)))

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        self.details.deleteLater()
        self.engine._source = None  # No native capture was opened by this fixture.
        self.engine._writer = None
        self.engine.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temporary.cleanup()

    def replay(self, kill_text, *, transform=None):
        tracker = Tracker(ignore_top_line=False)
        initial = ["Starting to attack.", "You begin casting Vengeful Smite II."]
        combat = [
            f"Ogskywalker crushes {KEEPER} for 19 points of damage.",
            f"Fate slashes {KEEPER} for 37 points of damage.",
            f"You crush {KEEPER} for 16 points of damage.",
            f"You crush {KEEPER} for 4 points of damage.",
        ]
        after_kill = initial + combat + [kill_text, "Stopped attacking.", "You gain party experience!"]
        frames = [(100, initial), (100.25, initial),
                  (101, initial + combat), (101.25, initial + combat)]
        frames.extend((102 + tick / 4, after_kill) for tick in range(12))
        for now, texts in frames:
            rows = [OcrLine(10, 20 + index * 30, 22, text) for index, text in enumerate(texts)]
            for message in tracker.update(rows, now):
                if message.text == kill_text and transform is not None:
                    message = transform(message)
                with self.engine._lock:
                    self.engine._handle_message(message, self.cfg)
        return tracker

    def captured_kill(self):
        kills = [(message, event) for message, event in self.events if event.kind == "kill"]
        self.assertEqual(len(kills), 1)
        return kills[0]

    def protected_kills(self):
        return [call.args[0] for call in self.ledger.record_event.call_args_list
                if call.args[0].kind == "kill"]

    def test_observed_keeper_ocr_tail_celebrates_once_after_two_reads(self):
        raw = f"You have slain {KEEPER}l"
        tracker = self.replay(raw)
        message, event = self.captured_kill()
        self.assertEqual((message.text, event.text, event.target), (raw, raw, KEEPER))
        self.assertTrue(message.fragment)
        self.assertFalse(message.backlog)
        self.assertFalse(message.estimated_ts)
        self.assertGreaterEqual(message.frames_seen, 2)
        self.assertEqual(tracker.min_frames, 2)
        self.assertEqual(self.celebrated, [KEEPER])
        self.assertEqual(len(self.kills), 1)
        self.details.show_npc.assert_called_once_with(KEEPER)
        self.assertEqual(self.protected_kills(), [], "fragment repair must not relax leaderboard evidence")

    def test_correct_exclamation_kill_still_celebrates_once_and_remains_complete(self):
        self.replay(f"You have slain {KEEPER}!")
        message, event = self.captured_kill()
        self.assertEqual(event.target, KEEPER)
        self.assertFalse(message.fragment)
        self.assertGreaterEqual(message.frames_seen, 2)
        self.assertEqual(self.celebrated, [KEEPER])
        self.assertEqual(len(self.protected_kills()), 1)

    def test_unproven_final_l_name_is_not_repaired_or_celebrated_even_when_notable(self):
        self.replay(f"You have slain {UNKNOWN}")
        message, event = self.captured_kill()
        self.assertEqual(event.target, UNKNOWN)
        self.assertTrue(message.fragment)
        self.assertEqual(self.celebrated, [])
        self.assertEqual(self.kills, [])
        self.assertEqual(self.protected_kills(), [])

    def test_generic_unterminated_kill_fragment_does_not_receive_the_exception(self):
        self.replay(f"You have slain {KEEPER}")
        message, event = self.captured_kill()
        self.assertEqual(event.target, KEEPER)
        self.assertTrue(message.fragment)
        self.assertEqual(self.celebrated, [])
        self.assertEqual(self.kills, [])

    def test_repaired_tail_with_only_one_observation_does_not_celebrate(self):
        self.replay(f"You have slain {KEEPER}l",
                    transform=lambda message: dataclasses.replace(message, frames_seen=1))
        message, event = self.captured_kill()
        self.assertEqual(event.target, KEEPER)
        self.assertTrue(message.fragment)
        self.assertEqual(message.frames_seen, 1)
        self.assertEqual(self.celebrated, [])
        self.assertEqual(self.protected_kills(), [])

    def test_repaired_tail_with_estimated_timestamp_does_not_celebrate(self):
        self.replay(f"You have slain {KEEPER}l",
                    transform=lambda message: dataclasses.replace(message, estimated_ts=True))
        message, event = self.captured_kill()
        self.assertEqual(event.target, KEEPER)
        self.assertTrue(message.fragment)
        self.assertTrue(event.estimated_ts)
        self.assertEqual(self.celebrated, [])
        self.assertEqual(self.protected_kills(), [])


if __name__ == "__main__":
    unittest.main()
