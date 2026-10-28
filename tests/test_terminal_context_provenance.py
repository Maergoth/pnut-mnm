"""Only independent clean confirmations may teach contextual terminal punctuation."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from mnmparse import tracker as trk


CLEAN = "a test dummy resists your Blind!"


class RecordingContext:
    """Record integration provenance independently of the helper's parser rules."""

    def __init__(self):
        self.observations = []

    def observe(self, text, **provenance):
        self.observations.append((text, provenance))

    def is_terminal(self, text):
        return False


def finalized(tracker, tiers, *, backlog=False, estimated=False):
    line = trk._PendingLine(first_seen=1, y=40, backlog=backlog, estimated=estimated)
    for tier in tiers:
        line.observe(CLEAN, trk.normalize(CLEAN), tier)
    tracker.pending = [line]
    return tracker._finalize(line, "provenance test")[0]


class TerminalContextProvenanceTests(unittest.TestCase):
    def tracker(self, **kwargs):
        tracker = trk.Tracker(row_pitch=20, margin_x=10, **kwargs)
        tracker._terminal_context = RecordingContext()
        return tracker

    def test_two_total_sightings_with_only_one_clean_read_do_not_train(self):
        for second_tier in (trk.TIER_TOP, trk.TIER_WEAK):
            with self.subTest(second_tier=second_tier):
                tracker = self.tracker()
                message = finalized(tracker, (trk.TIER_GOOD, second_tier))
                self.assertEqual(message.frames_seen, 2)
                self.assertFalse(message._context_trusted)
                self.assertEqual(tracker._join(message, 2), [message])
                _text, provenance = tracker._terminal_context.observations[-1]
                self.assertFalse(provenance["trustworthy"])

    def test_two_matching_clean_reads_can_train_a_literal_complete_message(self):
        tracker = self.tracker()
        message = finalized(tracker, (trk.TIER_GOOD, trk.TIER_GOOD))
        self.assertTrue(message._context_trusted)
        self.assertEqual(tracker._join(message, 2), [message])
        text, provenance = tracker._terminal_context.observations[-1]
        self.assertEqual(text, CLEAN)
        self.assertTrue(provenance["trustworthy"])
        self.assertFalse(provenance["backlog"])
        self.assertFalse(provenance["estimated_ts"])
        self.assertFalse(provenance["fragment"])
        self.assertFalse(provenance["joined"])

    def test_backlog_and_estimated_rows_cannot_teach_even_with_two_clean_reads(self):
        for flags in ({"backlog": True}, {"estimated": True}):
            with self.subTest(flags=flags):
                tracker = self.tracker()
                message = finalized(tracker, (trk.TIER_GOOD, trk.TIER_GOOD), **flags)
                self.assertFalse(message._context_trusted)
                tracker._join(message, 2)
                _text, provenance = tracker._terminal_context.observations[-1]
                self.assertFalse(provenance["trustworthy"])
                self.assertEqual(provenance["backlog"], flags.get("backlog", False))
                self.assertEqual(provenance["estimated_ts"], flags.get("estimated", False))

    def test_wrapped_message_cannot_promote_an_untrusted_constituent(self):
        head_text = "a remarkably prolonged test dummy resists your"
        for head_trusted, tail_trusted in ((True, False), (False, True), (False, False)):
            with self.subTest(head=head_trusted, tail=tail_trusted):
                tracker = self.tracker()
                head = trk.Message(head_text, 1, 2, _context_trusted=head_trusted)
                tail = trk.Message("Blind!", 1.25, 2, _context_trusted=tail_trusted)
                self.assertEqual(tracker._join(head, 1.5), [])
                messages = tracker._join(tail, 1.75)
                self.assertEqual(len(messages), 1)
                self.assertEqual(messages[0].text, head_text + " Blind!")
                self.assertFalse(messages[0]._context_trusted)
                _text, provenance = tracker._terminal_context.observations[-1]
                self.assertTrue(provenance["joined"])
                self.assertFalse(provenance["trustworthy"])

    def test_min_frames_one_does_not_allow_single_read_context_training(self):
        tracker = self.tracker(min_frames=1)
        message = finalized(tracker, (trk.TIER_GOOD,))
        self.assertEqual(message.frames_seen, 1)
        self.assertFalse(message._context_trusted)
        tracker._join(message, 2)
        self.assertFalse(tracker._terminal_context.observations[-1][1]["trustworthy"])

    def test_state_roundtrip_does_not_restore_context_from_normalized_history(self):
        with patch.object(trk, "TerminalContext", RecordingContext):
            original = trk.Tracker(row_pitch=20, margin_x=10)
            message = finalized(original, (trk.TIER_GOOD, trk.TIER_GOOD))
            original._join(message, 2)
            original.prev = [CLEAN]
            self.assertTrue(original._terminal_context.observations)
            state = original.export_state(now=2)
            self.assertNotIn("terminal_context", state)
            self.assertNotIn("_terminal_context", state)
            restored = trk.Tracker(row_pitch=20, margin_x=10)
            self.assertTrue(restored.import_state(state, now=3))
            self.assertTrue(restored.history)
            self.assertEqual(restored.prev, [CLEAN])
            self.assertEqual(restored._terminal_context.observations, [])


if __name__ == "__main__":
    unittest.main()
