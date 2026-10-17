"""Level chat and bar frames must reconcile before crediting a whole XP level."""
from __future__ import annotations

import unittest

from mnmparse.player_telemetry import PlayerTelemetry


def sample(telemetry: PlayerTelemetry, start: int, end: int, fraction: float) -> None:
    for ts in range(start, end + 1):
        telemetry.update({"xp_fraction": fraction, "xp_pixels": 200}, ts)


class XPLevelTimingTests(unittest.TestCase):
    def test_chat_before_checkpoint_waits_for_delayed_visible_wrap(self):
        for level_ts in (30, 31):
            with self.subTest(level_ts=level_ts):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 29, .98)
                telemetry.note_level_up(level_ts)
                sample(telemetry, 30, 31, .98)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
                self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)
                sample(telemetry, 32, 33, .02)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
                self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)
                self.assertFalse(telemetry.note_level_up(level_ts))
                sample(telemetry, 34, 93, .02)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
                self.assertFalse(telemetry.note_level_up(level_ts))

    def test_chat_after_checkpoint_carries_to_next_verified_endpoints(self):
        for level_ts in (30, 31):
            with self.subTest(level_ts=level_ts):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 31, .98)
                telemetry.note_level_up(level_ts)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
                self.assertFalse(telemetry.note_level_up(level_ts))
                sample(telemetry, 32, 61, .02)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 4)
                self.assertEqual(snap["xp_level_ups"], 1)
                self.assertEqual(snap["xp_observed_seconds"], 60)
                self.assertEqual(snap["xp_points_per_hour"], 480)
                self.assertFalse(telemetry.note_level_up(level_ts))

    def test_chat_after_confirmed_low_bar_still_resolves_pending_drop(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 31, .98)
        sample(telemetry, 32, 33, .02)
        telemetry.note_level_up(30)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 34, 61, .02)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)

    def test_drop_grace_uses_first_low_frame_before_stability_confirmation(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 31, .98)
        telemetry.note_level_up(30)
        sample(telemetry, 32, 37, .98)
        sample(telemetry, 38, 39, .02)
        sample(telemetry, 40, 61, .02)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)

    def test_same_fraction_full_level_counts_after_transition_grace(self):
        for chat_before_checkpoint in (True, False):
            with self.subTest(chat_before_checkpoint=chat_before_checkpoint):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 29, .5)
                if chat_before_checkpoint:
                    telemetry.note_level_up(30)
                sample(telemetry, 30, 31, .5)
                if not chat_before_checkpoint:
                    telemetry.note_level_up(30)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
                sample(telemetry, 32, 61, .5)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 100)
                self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)
                sample(telemetry, 62, 100, .5)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 100)
                self.assertFalse(telemetry.note_level_up(30))

    def test_subpixel_gain_before_confirmed_wrap_is_counted_once(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .98)
        sample(telemetry, 2, 31, .982)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 32, 49, .982)
        telemetry.note_level_up(50)
        sample(telemetry, 50, 51, .002)
        sample(telemetry, 52, 61, .002)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 2.2)
        self.assertEqual(snap["xp_level_ups"], 1)
        self.assertEqual(snap["xp_points_per_hour"], 132)
        sample(telemetry, 62, 91, .002)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 2.2)


if __name__ == "__main__":
    unittest.main()
