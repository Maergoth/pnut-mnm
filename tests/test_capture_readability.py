"""Capture warnings should describe lost information, not a missing final period.

The engine receives synthetic tracker messages and writes only to a temporary folder.
No window capture or real OCR runs in these tests.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False

from mnmparse.config import Config  # noqa: E402
from mnmparse.tracker import Message, Tracker  # noqa: E402


class FakeOcr:
    def read(self, _image: Any) -> list[Any]:
        return []


@dataclass
class OcrRow:
    x: int
    y: int
    h: int
    text: str


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class CaptureReadabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def setUp(self) -> None:
        from mnmparse.app.engine import Engine

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch("mnmparse.app.engine._load_vocab_once", lambda _cfg: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = Config(player_name="Mitch", log_dir=folder.name)
        self.engine = Engine(self.cfg)
        self.engine._prepare_run(self.cfg, FakeOcr())
        self.addCleanup(self._close_writer)
        self.events: list[Any] = []
        self.engine.message.connect(lambda _message, event: self.events.append(event))
        self.statuses: list[dict[str, Any]] = []
        self.engine.status.connect(self.statuses.append)
        self.now = time.time()

    def _close_writer(self) -> None:
        if self.engine._writer is not None:
            self.engine._writer.close()

    def _feed(self, text: str, *, fragment: bool = False) -> None:
        self.now += 0.1
        self.engine._handle_message(Message(text, self.now, 2, fragment=fragment), self.engine.config)

    def _status(self) -> dict[str, Any]:
        self.engine._emit_status()
        return self.statuses[-1]

    def test_readable_hits_without_periods_do_not_warn_for_ordinary_mobs(self) -> None:
        mobs = ("a wolf", "a crocodile", "a stumbling zombie", "a skeletal warrior")
        for i in range(24):
            mob = mobs[i % len(mobs)]
            if i % 2:
                text = f"Mitch's Vampirism hits {mob} for 20 points of Corruption Damage"
                kind, amount = "ability_hit", 20
            else:
                text = f"Mitch pierces {mob} for 46 points of damage"
                kind, amount = "melee_hit", 46
            self._feed(text, fragment=True)
            event = self.events[-1]
            self.assertEqual((event.kind, event.target, event.amount), (kind, mob, amount))
        status = self._status()
        self.assertEqual(status["unreadable_pct"], 0.0)
        self.assertFalse(status["garbled"])

    def test_tracker_fragment_can_still_be_a_readable_numbered_hit(self) -> None:
        tracker = Tracker(ignore_top_line=False)

        def rows(texts: list[str]) -> list[OcrRow]:
            return [OcrRow(10, 10 + i * 25, 21, text) for i, text in enumerate(texts)]

        base = ["Starting to attack."]
        tracker.update(rows(base), self.now)
        tracker.update(rows(base), self.now + 0.25)
        text = "Mitch pierces a wolf for 46 points of damage"
        window = base + [text, "Mitch's Vampirism hits a wolf for 20 points of Corruption Damage."]
        messages = tracker.update(rows(window), self.now + 0.5)
        messages += tracker.update(rows(window), self.now + 0.75)
        messages += tracker.flush(self.now + 1)
        self.assertEqual([message.text for message in messages], window[1:])
        self.assertTrue(messages[0].fragment)
        self.assertFalse(messages[0].backlog)
        for message in messages:
            self.engine._handle_message(message, self.cfg)
        self.assertEqual(
            [(event.kind, event.amount) for event in self.events],
            [("melee_hit", 46), ("ability_hit", 20)],
        )
        self.assertEqual(self._status()["unreadable_pct"], 0.0)

    def test_unknown_text_still_raises_a_readability_warning(self) -> None:
        for i in range(24):
            self._feed(f"xq zzv qqq bbbb kkk {i}")
        self.assertTrue(all(event.kind == "unknown" for event in self.events))
        status = self._status()
        self.assertEqual(status["unreadable_pct"], 100.0)
        self.assertTrue(status["garbled"])

    def test_abilities_with_unreadable_target_and_amount_raise_a_warning(self) -> None:
        for _ in range(24):
            self._feed("Mitch's Vampirism hits a wolf for $ points of Corruption Damage.")
        self.assertTrue(all(
            event.kind == "ability_partial" and event.target is None and event.amount is None
            for event in self.events
        ))
        status = self._status()
        self.assertEqual(status["unreadable_pct"], 100.0)
        self.assertTrue(status["garbled"])

    def test_estimated_damage_still_counts_as_unreadable(self) -> None:
        self.engine.update_config(Config(player_name="Mitch", log_dir=self.cfg.log_dir, dummy_fix=True))
        self._feed("Mitch pierces a wolf for 8 points of damage.")
        for _ in range(24):
            self._feed("Mitch pierces a wolf for $ points of damage.")
        self.assertTrue(all(
            event.kind == "melee_hit" and event.amount == 8 and event.estimated
            for event in self.events[1:]
        ))
        status = self._status()
        self.assertEqual(status["unreadable_pct"], 96.0)
        self.assertTrue(status["garbled"])

    def test_new_capture_run_discards_the_previous_readability_window(self) -> None:
        for i in range(40):
            self._feed(f"xq zzv qqq bbbb kkk {i}")
        self.assertTrue(self._status()["garbled"])
        self.engine._finish()
        self.engine._prepare_run(self.cfg, FakeOcr())
        status = self._status()
        self.assertEqual(status["unreadable_pct"], 0.0)
        self.assertFalse(status["garbled"])
        self._feed("Mitch pierces a wolf for 46 points of damage.")
        self.assertEqual(self._status()["unreadable_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
