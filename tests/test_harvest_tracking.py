"""Harvest counters consume confirmed tracker messages, never raw OCR frames."""
from __future__ import annotations

import unittest

from mnmparse.parser import parse_line
from mnmparse.session import SessionStats
from mnmparse.tracker import Tracker
from tests.test_tracker import frame, scrolled


PLAYER = "HarvestFixture"
WOOD = "You harvest [Wood]!"
RESIN = "You harvest [Resin]!"


class HarvestTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tracker = Tracker()
        self.session = SessionStats(PLAYER, include_personal=False, started=100)
        self.history = ["You begin casting Chop.", "You begin casting Heal.",
                        "You have entered Night Harbor (East).", "Stopped attacking."]
        self.now = 100.0
        # Existing on-screen rows are startup backlog, as in Engine's live path.
        self.feed()
        self.feed()

    def feed(self):
        self.now += .25
        messages = self.tracker.update(frame(scrolled(self.history, size=6)), self.now)
        for message in messages:
            if not message.backlog:
                event = parse_line(message.text, message.first_seen, PLAYER)
                self.session.add(event)
        return messages

    def snapshot(self):
        return self.session.snapshot(now=self.now)

    def first_resources(self):
        self.history.extend([WOOD, RESIN])
        first = self.feed()
        self.assertFalse(any(message.text in (WOOD, RESIN) for message in first))
        self.assertEqual(self.snapshot().harvests, 0, "One OCR reading must not increment harvests")
        second = self.feed()
        confirmed = [message for message in second if message.text in (WOOD, RESIN)]
        self.assertEqual([message.text for message in confirmed], [WOOD, RESIN])
        self.assertTrue(all(message.frames_seen == 2 and not message.backlog for message in confirmed))
        self.assertEqual(self.snapshot().harvests, 2)
        self.assertEqual(self.snapshot().harvests_by_item, [("Resin", 1), ("Wood", 1)])

    def test_wood_and_resin_require_confirmation_and_unchanged_frames_do_not_repeat_them(self):
        self.first_resources()
        for _ in range(12):
            self.assertEqual(self.feed(), [], "An unchanged OCR window replayed a confirmed result")
            self.assertEqual(self.snapshot().harvests, 2)
        self.assertEqual(self.snapshot().harvests_by_item, [("Resin", 1), ("Wood", 1)])

    def test_a_later_identical_wood_result_counts_again_only_after_confirmation(self):
        self.first_resources()
        for _ in range(4):
            self.assertEqual(self.feed(), [])
        # The older Wood line is still visible. A new cast and Wood result scroll
        # into different rows; equal text must not conflate the two acquisitions.
        self.history.extend(["You begin casting Chop.", WOOD])
        first = self.feed()
        self.assertFalse(any(message.text == WOOD for message in first))
        self.assertEqual(self.snapshot().harvests, 2)
        second = self.feed()
        new_wood = [message for message in second if message.text == WOOD]
        self.assertEqual(len(new_wood), 1)
        self.assertEqual(new_wood[0].frames_seen, 2)
        self.assertFalse(new_wood[0].backlog)
        self.assertEqual(self.snapshot().harvests, 3)
        self.assertEqual(self.snapshot().harvests_by_item, [("Wood", 2), ("Resin", 1)])
        for _ in range(8):
            self.assertEqual(self.feed(), [])
        self.assertEqual(self.snapshot().harvests, 3)


if __name__ == "__main__":
    unittest.main()
