"""XP uses continuous five-minute windows; HP alerts need two low readings."""
import json
import unittest

from mnmparse.player_telemetry import PlayerTelemetry


def reading(hp=None, xp=None, **extra):
    result = {}
    if hp is not None:
        result.update(hp_current=hp, hp_max=100)
    if xp is not None:
        result.update(xp_fraction=xp, xp_pixels=200)
    return dict(result, **extra)


def sample(telemetry, start, end, fraction=.5, **extra):
    for ts in range(start, end + 1):
        telemetry.update(reading(xp=fraction, **extra), ts)


class PlayerTelemetryTests(unittest.TestCase):
    def test_unknown_and_invalid_measurements_are_json_ready(self):
        telemetry = PlayerTelemetry()
        for key in ("hp_percent", "hp_lowest_percent", "xp_percent", "xp_gain_points",
                    "xp_observed_seconds", "xp_points_per_hour"):
            self.assertIsNone(telemetry.snapshot()[key])
        telemetry.update({"hp_current": 200, "hp_max": 100, "xp_fraction": float("nan")}, 1)
        telemetry.update({"hp_current": 5, "hp_max": 0, "xp_fraction": 2}, 2)
        self.assertIsNone(telemetry.snapshot()["xp_gain_points"])
        self.assertNotIn("xp_kill_gains", telemetry.snapshot())
        self.assertFalse(hasattr(telemetry, "note_kill"))
        json.dumps(telemetry.snapshot(), allow_nan=False)

    def test_health_alert_needs_two_valid_lows_and_recovery_before_next_alert(self):
        telemetry = PlayerTelemetry()
        telemetry.update(reading(hp=9), 1)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        telemetry.update(reading(hp=8), 2)
        self.assertTrue(telemetry.snapshot()["hp_low"])
        telemetry.update(reading(hp=1), 3)
        self.assertEqual(telemetry.snapshot()["hp_below10_episodes"], 1)
        telemetry.update(reading(hp=10), 4)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        telemetry.update(reading(hp=9), 5)
        telemetry.update(reading(hp=9), 6)
        self.assertEqual(telemetry.snapshot()["hp_below10_episodes"], 2)

    def test_missing_panel_gaps_and_repeated_timestamps_do_not_bridge_health(self):
        telemetry = PlayerTelemetry()
        telemetry.update(reading(hp=5), 1)
        self.assertFalse(telemetry.update(reading(hp=4), 1))
        telemetry.update(reading(hp=5), 3)
        telemetry.update(reading(hp=4), 6)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        telemetry.update(None, 7)
        telemetry.update(reading(hp=2), 8)
        telemetry.update(reading(hp=2), 9)
        snap = telemetry.snapshot()
        self.assertEqual(snap["hp_below10_episodes"], 2)
        self.assertEqual(snap["hp_observed_seconds"], 3)
        self.assertEqual(snap["hp_below10_seconds"], 3)
        self.assertEqual(snap["hp_lowest_percent"], 2)

    def test_no_gain_or_rate_until_five_minutes_after_initial_stability(self):
        telemetry = PlayerTelemetry()
        telemetry.update(reading(xp=.5), 0)
        self.assertIsNone(telemetry.snapshot()["xp_percent"])
        sample(telemetry, 1, 10)
        sample(telemetry, 11, 300, .52)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_percent"], 52)
        self.assertEqual(snap["xp_gain_points"], 0)
        self.assertEqual(snap["xp_observed_seconds"], 0)
        self.assertIsNone(snap["xp_points_per_hour"])
        telemetry.update(reading(xp=.52), 301)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 2)
        self.assertEqual(snap["xp_observed_seconds"], 300)
        self.assertEqual(snap["xp_points_per_hour"], 24)

    def test_zero_gain_window_counts_and_partial_next_window_does_not_dilute_rate(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 301)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)
        sample(telemetry, 302, 599, .54)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 300)
        sample(telemetry, 600, 601, .54)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 600)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 24)

    def test_one_pixel_jitter_ignored_and_blue_resolution_used_without_resetting_window(self):
        for resolution, expected in ((.5, 0), (.1, .2)):
            with self.subTest(resolution=resolution):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 100, .5, xp_resolution_points=.5)
                sample(telemetry, 101, 301, .502, xp_resolution_points=resolution)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], expected)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 300)

    def test_unstable_last_reading_delays_comparison_until_confirmed(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 300)
        telemetry.update(reading(xp=.55), 301)
        self.assertIsNone(telemetry.snapshot()["xp_points_per_hour"])
        telemetry.update(reading(xp=.55), 302)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 5)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 301)

    def test_missing_panel_gap_and_width_change_discard_partial_windows(self):
        for interrupt in ("missing", "gap", "width"):
            with self.subTest(interrupt=interrupt):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 200)
                start, extra = 202, {}
                if interrupt == "missing":
                    telemetry.update(reading(hp=50), 201)
                elif interrupt == "gap":
                    start = 204
                else:
                    extra["xp_pixels"] = 400
                sample(telemetry, start, start + 300, .8, **extra)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)
                telemetry.update(reading(xp=.82, **extra), start + 301)
                telemetry.update(reading(xp=.82, **extra), start + 302)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 2)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 301)

    def test_downward_jump_without_own_level_confirmation_omits_interval(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 250, .8)
        sample(telemetry, 251, 560, .2)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)
        self.assertIsNone(telemetry.snapshot()["xp_points_per_hour"])
        telemetry.update(reading(xp=.2), 561)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 300)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)

    def test_level_up_before_or_after_bar_wrap_preserves_five_minute_window(self):
        for event_first in (True, False):
            with self.subTest(event_first=event_first):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 249, .98)
                if event_first:
                    telemetry.note_level_up(250)
                sample(telemetry, 250, 251, .02)
                if not event_first:
                    telemetry.note_level_up(250)
                sample(telemetry, 252, 301, .02)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 4)
                self.assertEqual(snap["xp_observed_seconds"], 300)
                self.assertEqual(snap["xp_level_ups"], 1)
                self.assertFalse(telemetry.note_level_up(250))

    def test_multiple_confirmed_levels_count_even_if_samples_missed_wraps(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 99, .1)
        telemetry.note_level_up(100)
        sample(telemetry, 100, 199, .15)
        telemetry.note_level_up(200)
        sample(telemetry, 200, 301, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 210)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 2)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 2520)

    def test_level_chat_after_checkpoint_frame_does_not_double_count(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 299, .1)
        sample(telemetry, 300, 301, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 10)
        self.assertTrue(telemetry.note_level_up(301))
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 110)
        self.assertFalse(telemetry.note_level_up(301))
        sample(telemetry, 302, 601, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 110)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)

    def test_expired_level_confirmation_and_levels_without_baseline_are_ignored(self):
        telemetry = PlayerTelemetry()
        self.assertFalse(telemetry.note_level_up(0))
        sample(telemetry, 0, 249, .98)
        sample(telemetry, 250, 261, .02)
        self.assertFalse(telemetry.note_level_up(250))
        sample(telemetry, 262, 560, .02)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)

    def test_pause_and_json_restore_keep_totals_without_live_readings_or_partial_time(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 100, .5, hp=5)
        sample(telemetry, 101, 401, .52, hp=5)
        saved = json.loads(json.dumps(telemetry.snapshot(), allow_nan=False))
        self.assertEqual(saved["xp_gain_points"], 2)
        self.assertEqual(saved["xp_observed_seconds"], 300)
        telemetry.pause()
        restored = PlayerTelemetry(saved)
        for instance in (telemetry, restored):
            self.assertIsNone(instance.snapshot()["hp_percent"])
            self.assertIsNone(instance.snapshot()["xp_percent"])
            sample(instance, 1000, 1301, .9, hp=5)
            self.assertEqual(instance.snapshot()["xp_gain_points"], 2)
            self.assertEqual(instance.snapshot()["xp_observed_seconds"], 600)
            self.assertEqual(instance.snapshot()["xp_points_per_hour"], 12)
            self.assertEqual(instance.snapshot()["hp_below10_episodes"], 2)


if __name__ == "__main__":
    unittest.main()
