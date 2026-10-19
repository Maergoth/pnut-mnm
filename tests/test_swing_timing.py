"""One timing observation per attack round, with an optional initial weapon delay."""
from __future__ import annotations

from types import SimpleNamespace
import unittest

from mnmparse.swing import HISTORY_S, PERIOD_SWINGS, SAME_FRAME_S, SwingTracker, estimate_delay


def _swing(ts: float, *, skill: str = "slash", weapon: str | None = None,
           kind: str = "melee_hit") -> SimpleNamespace:
    return SimpleNamespace(ts=ts, actor="You", skill=skill, weapon=weapon, kind=kind, is_pet=False)


class SwingRoundTests(unittest.TestCase):
    def test_double_attacks_do_not_shift_the_bar_or_inflate_swing_count(self):
        tracker = SwingTracker(weapon_delay=3.0)
        for ts in (100.0, 103.0, 106.0):
            self.assertTrue(tracker.observe(_swing(ts)))
            original = tracker.hands(ts)[0]
            for offset in (0.0, 0.03, SAME_FRAME_S):
                self.assertFalse(tracker.observe(_swing(ts + offset, kind="melee_miss")))
                hand = tracker.hands(ts + offset)[0]
                self.assertEqual((hand.last, hand.swings, hand.delay),
                                 (original.last, original.swings, original.delay))
                self.assertAlmostEqual(hand.progress(ts + offset), offset / 3.0)
        self.assertEqual(tracker.hands(106.05)[0].swings, 3)

    def test_duplicate_matches_its_hand_even_when_other_lines_intervene(self):
        tracker = SwingTracker()
        self.assertTrue(tracker.observe(_swing(100.0)))
        self.assertTrue(tracker.observe(_swing(100.2, weapon="offhand")))
        self.assertFalse(tracker.observe(_swing(100.03)))
        self.assertTrue(tracker.observe(_swing(100.1)))

    def test_simultaneous_main_offhand_and_bow_remain_separate(self):
        tracker = SwingTracker()
        for ts in (100.0, 103.0, 106.0, 109.0, 112.0, 115.0):
            for weapon in (None, "offhand", "bow"):
                self.assertTrue(tracker.observe(_swing(ts, weapon=weapon)))
                self.assertFalse(tracker.observe(_swing(ts + 0.02, weapon=weapon)))
        self.assertEqual([(h.label, h.swings, h.delay) for h in tracker.hands(115.1)],
                         [("slash", 6, 3.0), ("off hand", 6, 3.0), ("bow", 6, 3.0)])

    def test_distinct_weapon_verbs_and_real_same_verb_rhythms_survive(self):
        different = SwingTracker()
        same = SwingTracker()
        for i in range(8):
            ts = 100.0 + i * 3.0
            different.observe(_swing(ts))
            different.observe(_swing(ts, skill="pierce"))
            same.observe(_swing(ts))
            same.observe(_swing(ts + 0.3))
        self.assertEqual([(h.label, h.delay) for h in different.hands(121.5)],
                         [("slash", 3.0), ("pierce", 3.0)])
        self.assertEqual([(h.label, h.delay) for h in same.hands(121.5)],
                         [("hand 1", 3.0), ("hand 2", 3.0)])


class WeaponDelaySeedTests(unittest.TestCase):
    def test_seed_waits_for_a_real_swing_and_counts_as_one_interval(self):
        tracker = SwingTracker(weapon_delay=3.6)
        self.assertEqual(tracker.hands(99.0), [])
        tracker.observe(_swing(100.0))
        initial = tracker.hands(100.0)[0]
        self.assertEqual((initial.last, initial.swings, initial.delay), (100.0, 1, 3.6))
        tracker.observe(_swing(103.0))
        # One seed and one real gap: median(3.6, 3.0), with equal weight.
        self.assertEqual(tracker.hands(103.0)[0].delay, 3.3)
        for now in (103.0, 103.5, 104.0):
            self.assertEqual(tracker.hands(now)[0].delay, 3.3)
        tracker.observe(_swing(106.4))
        # The seed was not added again: median(3.6, 3.0, 3.4).
        self.assertEqual(tracker.hands(106.4)[0].delay, 3.4)

    def test_real_intervals_roll_the_seed_out_of_the_existing_window(self):
        tracker = SwingTracker(weapon_delay=3.6)
        times = [100.0]
        tracker.observe(_swing(times[-1]))
        tracker.hands(times[-1])
        for number in range(1, 26):
            times.append(times[-1] + (3.1 if number % 2 else 3.5))
            tracker.observe(_swing(times[-1]))
            hand = tracker.hands(times[-1])[0]
        # The last 23 intervals contain twelve 3.1s and eleven 3.5s.
        # Keeping the 3.6s seed would produce 3.3s instead of 3.1s.
        self.assertEqual(hand.delay, estimate_delay(times[-PERIOD_SWINGS:]))
        self.assertEqual(hand.delay, 3.1)

    def test_editing_seed_does_not_replace_existing_seed_or_reset_progress(self):
        tracker = SwingTracker(weapon_delay=3.6)
        tracker.observe(_swing(100.0))
        tracker.hands(100.0)
        tracker.set_weapon_delay(2.4)
        hand = tracker.hands(101.0)[0]
        self.assertEqual((hand.last, hand.swings, hand.delay), (100.0, 1, 3.6))
        self.assertAlmostEqual(hand.progress(101.0), 1.0 / 3.6)
        tracker.observe(_swing(103.0))
        self.assertEqual(tracker.hands(103.0)[0].delay, 3.3)

    def test_real_timing_reference_prevents_a_late_seed(self):
        tracker = SwingTracker()
        tracker.observe(_swing(100.0))
        tracker.observe(_swing(103.0))
        self.assertIsNone(tracker.hands(103.0)[0].delay)
        tracker.set_weapon_delay(6.0)
        self.assertIsNone(tracker.hands(104.0)[0].delay)
        tracker.observe(_swing(106.0))
        self.assertEqual(tracker.hands(106.0)[0].delay, 3.0)
        tracker.set_weapon_delay(2.0)
        hand = tracker.hands(107.0)[0]
        self.assertEqual((hand.last, hand.delay), (106.0, 3.0))

    def test_clear_and_expired_history_allow_a_fresh_seed(self):
        for expiration in ("clear", "empty tick", "new swing"):
            with self.subTest(expiration=expiration):
                tracker = SwingTracker(weapon_delay=3.6)
                for ts in (100.0, 103.0, 106.0):
                    tracker.observe(_swing(ts))
                    tracker.hands(ts)
                tracker.set_weapon_delay(4.2)
                now = 106.0 + HISTORY_S + 1.0
                if expiration == "clear":
                    tracker.clear()
                elif expiration == "empty tick":
                    self.assertEqual(tracker.hands(now), [])
                tracker.observe(_swing(now))
                hand = tracker.hands(now)[0]
                self.assertEqual((hand.last, hand.swings, hand.delay), (now, 1, 4.2))

    def test_no_seed_keeps_the_existing_learning_threshold(self):
        tracker = SwingTracker()
        for ts in (100.0, 103.0):
            tracker.observe(_swing(ts))
            self.assertIsNone(tracker.hands(ts)[0].delay)
        tracker.observe(_swing(106.0))
        self.assertEqual(tracker.hands(106.0)[0].delay, 3.0)

    def test_only_positive_finite_seed_seconds_are_accepted(self):
        for delay in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(delay=delay):
                with self.assertRaises(ValueError):
                    SwingTracker(weapon_delay=delay)
        tracker = SwingTracker(weapon_delay=99.9)
        tracker.set_weapon_delay(None)
        tracker.observe(_swing(100.0))
        self.assertIsNone(tracker.hands(100.0)[0].delay)


if __name__ == "__main__":
    unittest.main()
