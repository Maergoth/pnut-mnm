"""XP rates weight recent thirty-second checkpoints; HP needs two low readings."""
import json
import unittest

from mnmparse.player_telemetry import PlayerTelemetry, XP_RATE_STALE_AFTER
from mnmparse.session import SessionStats


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

    def test_cadence_budget_is_bounded_and_missing_readings_still_reset(self):
        telemetry = PlayerTelemetry()
        for invalid in (0, -1, 10.01, float("inf"), float("nan"), True, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                telemetry.update(reading(hp=5, xp=.5), 1, max_gap=invalid)
        telemetry.update(reading(hp=5, xp=.5), 1, max_gap=4)
        telemetry.update(reading(hp=5, xp=.5), 4, max_gap=4)
        self.assertTrue(telemetry.snapshot()["hp_low"])
        self.assertIsNotNone(telemetry._xp_baseline)
        telemetry.update(None, 5, max_gap=4)
        telemetry.update(reading(hp=5, xp=.5), 7, max_gap=4)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        self.assertIsNone(telemetry._xp_baseline)
        telemetry.update(reading(hp=5, xp=.5), 12, max_gap=4)
        self.assertFalse(telemetry.snapshot()["hp_low"])
        self.assertIsNone(telemetry._xp_baseline)

    def test_no_gain_or_rate_until_thirty_seconds_after_initial_stability(self):
        telemetry = PlayerTelemetry()
        telemetry.update(reading(xp=.5), 0)
        self.assertIsNone(telemetry.snapshot()["xp_percent"])
        sample(telemetry, 1, 10)
        sample(telemetry, 11, 30, .52)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_percent"], 52)
        self.assertEqual(snap["xp_gain_points"], 0)
        self.assertEqual(snap["xp_observed_seconds"], 0)
        self.assertIsNone(snap["xp_points_per_hour"])
        telemetry.update(reading(xp=.52), 31)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 2)
        self.assertEqual(snap["xp_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], 240)

    def test_leading_zero_gain_window_does_not_dilute_first_gaining_interval(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 31)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)
        sample(telemetry, 32, 60, .54)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 30)
        telemetry.update(reading(xp=.54), 61)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 60)
        self.assertEqual(telemetry.snapshot()["xp_rate_observed_seconds"], 30)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 480)

    def test_half_hour_before_exp_does_not_dilute_five_minutes_of_actual_gains(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1801)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)
        self.assertEqual(telemetry.snapshot()["xp_rate_observed_seconds"], 1800)
        for checkpoint in range(1, 11):
            start = 1802 + (checkpoint - 1) * 30
            sample(telemetry, start, start + 29, .5 + checkpoint * .002,
                   xp_resolution_points=.05)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 2)
        self.assertEqual(snap["xp_observed_seconds"], 2100)
        self.assertEqual(snap["xp_rate_gain_points"], 2)
        self.assertEqual(snap["xp_rate_observed_seconds"], 300)
        self.assertEqual(snap["xp_points_per_hour"], 24)
        self.assertEqual(snap["xp_rate_window_seconds"], 3600)

    def test_twenty_percent_over_an_hour_is_twenty_per_hour_without_overlapping_counts(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        for checkpoint in range(1, 121):
            start = 2 + (checkpoint - 1) * 30
            sample(telemetry, start, start + 29, .5 + checkpoint / 600,
                   xp_resolution_points=.05)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 20)
        self.assertEqual(snap["xp_observed_seconds"], 3600)
        self.assertEqual(snap["xp_rate_gain_points"], 20)
        self.assertEqual(snap["xp_rate_observed_seconds"], 3600)
        self.assertEqual(snap["xp_points_per_hour"], 20)
        self.assertEqual(len(telemetry._xp_rate_intervals), 120)

    def test_speed_changes_use_last_hour_instead_of_all_session_time(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .1)
        for checkpoint in range(1, 151):
            start = 2 + (checkpoint - 1) * 30
            fraction = .1 + min(checkpoint, 120) * .001 + max(0, checkpoint - 120) * .002
            sample(telemetry, start, start + 29, fraction, xp_resolution_points=.05)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 18)
        self.assertEqual(snap["xp_observed_seconds"], 4500)
        self.assertEqual(snap["xp_rate_gain_points"], 15)
        self.assertEqual(snap["xp_rate_observed_seconds"], 3600)
        self.assertEqual(snap["xp_points_per_hour"], 15)
        self.assertEqual(len(telemetry._xp_rate_intervals), 120)

    def test_rolling_boundary_weights_partial_oldest_interval(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 30, .1)
        sample(telemetry, 31, 3600, .12)
        sample(telemetry, 3601, 3602, .16)
        snap = telemetry.snapshot()
        expected_gain = 2 * 30 / 31 + 4
        self.assertAlmostEqual(snap["xp_rate_gain_points"], expected_gain, places=6)
        self.assertEqual(snap["xp_rate_observed_seconds"], 3600)
        self.assertAlmostEqual(snap["xp_points_per_hour"], expected_gain, places=6)
        self.assertEqual(snap["xp_gain_points"], 6)
        self.assertEqual(snap["xp_observed_seconds"], 3601)
        self.assertLessEqual(len(telemetry._xp_rate_intervals), 121)

    def test_idle_decays_and_full_hour_without_gains_starts_fresh(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        sample(telemetry, 2, 31, .52)
        for start, end, rate in ((32, 301, 24), (302, 601, 12),
                                 (602, 3601, 2), (3602, 3631, 0)):
            sample(telemetry, start, end, .52)
            self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], rate)
        self.assertEqual(telemetry.snapshot()["xp_rate_gain_points"], 0)
        sample(telemetry, 3632, 3661, .54)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_points_per_hour"], 240)
        self.assertEqual(snap["xp_rate_observed_seconds"], 30)
        self.assertEqual(snap["xp_gain_points"], 4)
        self.assertEqual(snap["xp_observed_seconds"], 3660)

    def test_delayed_stability_expires_old_gain_before_deciding_resume_window(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        sample(telemetry, 2, 3601, .52)
        for ts in range(3602, 4202):
            telemetry.update(reading(xp=.55 if ts % 2 else .57), ts)
        sample(telemetry, 4202, 4203, .54)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_rate_gain_points"], 2)
        self.assertEqual(snap["xp_rate_observed_seconds"], 602)
        self.assertAlmostEqual(snap["xp_points_per_hour"], 2 * 3600 / 602, places=6)
        self.assertEqual(snap["xp_gain_points"], 4)
        self.assertEqual(snap["xp_observed_seconds"], 4202)

    def test_pixel_deadband_carries_small_gains_without_overstating_their_duration(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        for start, end, fraction in ((2, 31, .502), (32, 61, .504)):
            sample(telemetry, start, end, fraction, xp_resolution_points=.5)
            self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 62, 91, .506, xp_resolution_points=.5)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], .6)
        self.assertEqual(snap["xp_rate_observed_seconds"], 90)
        self.assertEqual(snap["xp_points_per_hour"], 24)
        sample(telemetry, 92, 121, .506, xp_resolution_points=.5)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], .6)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 18)

    def test_carried_pixel_gain_ages_out_over_its_original_measurement_span(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        for start, end, fraction in ((2, 31, .502), (32, 61, .504), (62, 91, .506)):
            sample(telemetry, start, end, fraction, xp_resolution_points=.5)
        for start, end, expected in ((92, 3631, .4), (3632, 3661, .2), (3662, 3691, 0)):
            sample(telemetry, start, end, .506, xp_resolution_points=.5)
            snap = telemetry.snapshot()
            self.assertAlmostEqual(snap["xp_rate_gain_points"], expected)
            self.assertEqual(snap["xp_gain_points"], .6)
            self.assertEqual(snap["xp_rate_observed_seconds"], 3600)

    def test_carried_pixel_gain_does_not_move_a_later_burst_into_earlier_intervals(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        for start, end, fraction in ((2, 31, .502), (32, 61, .504), (62, 91, .8)):
            sample(telemetry, start, end, fraction, xp_resolution_points=.5)
        sample(telemetry, 92, 3661, .8, xp_resolution_points=.5)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 30)
        self.assertEqual(snap["xp_rate_gain_points"], 29.6)
        self.assertEqual(snap["xp_points_per_hour"], 29.6)

    def test_unstable_bar_stops_showing_an_old_rate_without_inventing_idle(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        sample(telemetry, 2, 31, .52)
        history = list(telemetry._xp_rate_intervals)
        for ts in range(32, int(31 + XP_RATE_STALE_AFTER) + 1):
            telemetry.update(reading(xp=.55 if ts % 2 else .57), ts)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 240)
        telemetry.update(reading(xp=.57), 31 + XP_RATE_STALE_AFTER + 1)
        snap = telemetry.snapshot()
        self.assertIsNone(snap["xp_points_per_hour"])
        self.assertEqual(snap["xp_rate_gain_points"], 0)
        self.assertEqual(snap["xp_rate_observed_seconds"], 0)
        self.assertEqual(snap["xp_gain_points"], 2)
        self.assertEqual(telemetry._xp_rate_intervals, history)
        # Session serialization must not reconstruct the stale rate from totals.
        session = SessionStats("You", started=0)
        session.note_player_status(snap)
        self.assertIsNone(session.player_status["xp_points_per_hour"])
        sample(telemetry, 93, 94, .54)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 4)
        self.assertAlmostEqual(telemetry.snapshot()["xp_points_per_hour"], 4 * 3600 / 93, places=6)

    def test_rolling_projection_ages_at_current_capture_time_without_mutating_history(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1)
        sample(telemetry, 2, 3601, .52)
        history = list(telemetry._xp_rate_intervals)
        telemetry.update(reading(xp=.57), 3602)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_rate_observed_seconds"], 3599)
        self.assertAlmostEqual(snap["xp_rate_gain_points"], 2 * 29 / 30, places=6)
        self.assertEqual(telemetry.snapshot(), snap)
        self.assertEqual(telemetry._xp_rate_intervals, history)

    def test_one_pixel_jitter_ignored_and_blue_resolution_used(self):
        for resolution, expected in ((.5, 0), (.1, .2)):
            with self.subTest(resolution=resolution):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 10, .5, xp_resolution_points=.5)
                sample(telemetry, 11, 31, .502, xp_resolution_points=resolution)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], expected)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 30)

    def test_uncertain_one_pixel_movement_expires_instead_of_becoming_fresh_hourly_gain(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .5, xp_resolution_points=.5)
        sample(telemetry, 2, 3631, .505, xp_resolution_points=.5)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 3632, 3661, .510, xp_resolution_points=.5)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 3662, 3691, .515, xp_resolution_points=.5)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 1)
        self.assertEqual(snap["xp_rate_gain_points"], 1)
        self.assertEqual(snap["xp_rate_observed_seconds"], 60)
        self.assertEqual(snap["xp_points_per_hour"], 60)

    def test_unstable_last_reading_delays_comparison_until_confirmed(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 30)
        telemetry.update(reading(xp=.55), 31)
        self.assertIsNone(telemetry.snapshot()["xp_points_per_hour"])
        telemetry.update(reading(xp=.55), 32)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 5)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 31)

    def test_missing_panel_gap_and_width_change_discard_partial_windows(self):
        for interrupt in ("missing", "gap", "width"):
            with self.subTest(interrupt=interrupt):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 20)
                start, extra = 22, {}
                if interrupt == "missing":
                    telemetry.update(reading(hp=50), 21)
                    telemetry.update(reading(hp=50), 23)
                    start = 24
                elif interrupt == "gap":
                    start = 24
                else:
                    extra["xp_pixels"] = 400
                sample(telemetry, start, start + 30, .8, **extra)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)
                telemetry.update(reading(xp=.82, **extra), start + 31)
                telemetry.update(reading(xp=.82, **extra), start + 32)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 2)
                self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 31)

    def test_one_or_two_pixel_width_jitter_preserves_xp_comparisons(self):
        for pixels in (1, 2):
            with self.subTest(pixels=pixels):
                telemetry = PlayerTelemetry()
                for ts in range(63):
                    width = 200 if ts % 2 == 0 else 200 + (pixels if ts % 4 == 1 else -pixels)
                    telemetry.update(reading(xp=.5 if ts < 3 else .52, xp_pixels=width), ts)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 2)
                self.assertEqual(snap["xp_observed_seconds"], 60)
                self.assertEqual(snap["xp_points_per_hour"], 120)
                self.assertEqual(telemetry._xp_width, 200)

    def test_changed_width_cannot_invent_xp_from_constant_yellow_or_blue_fill(self):
        for fine in (False, True):
            with self.subTest(fine=fine):
                telemetry = PlayerTelemetry()
                for ts in range(63):
                    width = 200 if ts % 2 == 0 else 202 if ts % 4 == 1 else 198
                    fraction = .4 + .2 * 150 / width if fine else 198 / width
                    resolution = (20 if fine else 100) / width
                    telemetry.update(reading(xp=fraction, xp_pixels=width,
                                             xp_resolution_points=resolution), ts)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 0)
                self.assertEqual(snap["xp_observed_seconds"], 60)
                self.assertEqual(snap["xp_points_per_hour"], 0)

    def test_persistent_minor_width_change_reacquires_after_original_sample_gap(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 10, .5)
        for ts in (11, 12):
            telemetry.update(reading(xp=100 / 202, xp_pixels=202), ts)
            self.assertEqual(telemetry._xp_previous_ts, 10)
            self.assertEqual(telemetry._xp_baseline, (1, .5))
        telemetry.update(reading(xp=100 / 202, xp_pixels=202), 13)
        self.assertEqual(telemetry._xp_width, 202)
        self.assertIsNone(telemetry._xp_baseline)
        for ts in range(14, 45):
            telemetry.update(reading(xp=100 / 202, xp_pixels=202), ts)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 0)
        self.assertEqual(snap["xp_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], 0)

    def test_width_jitter_uses_stable_anchor_instead_of_accepting_cumulative_resize(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 10, .5)
        for ts, width in ((11, 201), (12, 202)):
            telemetry.update(reading(xp=.5, xp_pixels=width), ts)
            self.assertEqual(telemetry._xp_baseline, (1, .5))
        telemetry.update(reading(xp=.5, xp_pixels=203), 13)
        self.assertIsNone(telemetry._xp_baseline)
        self.assertEqual(telemetry._xp_width, 203)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)

    def test_brief_ocr_misses_preserve_xp_comparisons_but_require_fresh_health(self):
        for missing in ({}, reading(hp=50)):
            with self.subTest(missing=missing):
                telemetry = PlayerTelemetry()
                for ts in range(63):
                    if ts in (15, 30, 45):
                        telemetry.update(missing, ts)
                        self.assertIsNone(telemetry.snapshot()["xp_percent"])
                        self.assertIsNotNone(telemetry._xp_baseline)
                        self.assertFalse(telemetry.snapshot()["hp_low"])
                    else:
                        telemetry.update(reading(hp=5, xp=.5 if ts < 11 else .52), ts)
                        if ts in (16, 31, 46):
                            self.assertIsNone(telemetry.snapshot()["xp_percent"])
                            self.assertFalse(telemetry.snapshot()["hp_low"])
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_observed_seconds"], 61)
                self.assertEqual(snap["xp_gain_points"], 2)
                self.assertAlmostEqual(snap["xp_points_per_hour"], 2 * 3600 / 61, places=6)

    def test_repeated_ocr_misses_cannot_extend_last_valid_xp_budget(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 20)
        telemetry.update({}, 21)
        telemetry.update({}, 22)
        self.assertIsNotNone(telemetry._xp_baseline)
        telemetry.update({}, 22.01)
        self.assertIsNone(telemetry._xp_baseline)
        sample(telemetry, 23, 53, .8)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)
        telemetry.update(reading(xp=.82), 54)
        telemetry.update(reading(xp=.82), 55)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], round(2 * 3600 / 31, 6))

    def test_downward_jump_without_own_level_confirmation_omits_interval(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 20, .8)
        sample(telemetry, 21, 60, .2)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 0)
        self.assertIsNone(telemetry.snapshot()["xp_points_per_hour"])
        telemetry.update(reading(xp=.2), 61)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_observed_seconds"], 30)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)

    def test_level_up_before_or_after_bar_wrap_preserves_thirty_second_window(self):
        for event_first in (True, False):
            with self.subTest(event_first=event_first):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 24, .98)
                if event_first:
                    telemetry.note_level_up(25)
                sample(telemetry, 25, 26, .02)
                if not event_first:
                    telemetry.note_level_up(25)
                sample(telemetry, 27, 31, .02)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 4)
                self.assertEqual(snap["xp_observed_seconds"], 30)
                self.assertEqual(snap["xp_level_ups"], 1)
                self.assertEqual(snap["xp_points_per_hour"], 480)
                self.assertFalse(telemetry.note_level_up(25))

    def test_multiple_confirmed_levels_count_even_if_samples_missed_wraps(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 9, .1)
        telemetry.note_level_up(10)
        sample(telemetry, 10, 19, .15)
        telemetry.note_level_up(20)
        sample(telemetry, 20, 31, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 210)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 2)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 25200)

    def test_level_chat_after_checkpoint_frame_does_not_double_count(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 29, .1)
        sample(telemetry, 30, 31, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 10)
        self.assertFalse(telemetry.note_level_up(31))
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 10)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 1200)
        self.assertFalse(telemetry.note_level_up(31))
        sample(telemetry, 32, 61, .2)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 110)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 1)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 6600)

    def test_late_level_after_idle_checkpoint_excludes_leading_idle(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1801)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)
        self.assertFalse(telemetry.note_level_up(1801))
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 1802, 1831)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 100)
        self.assertEqual(snap["xp_observed_seconds"], 1830)
        self.assertEqual(snap["xp_rate_gain_points"], 100)
        self.assertEqual(snap["xp_rate_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], 12000)
        self.assertFalse(telemetry.note_level_up(1801))

    def test_expired_level_confirmation_and_levels_without_baseline_are_ignored(self):
        telemetry = PlayerTelemetry()
        self.assertFalse(telemetry.note_level_up(0))
        sample(telemetry, 0, 24, .98)
        sample(telemetry, 25, 36, .02)
        self.assertFalse(telemetry.note_level_up(25))
        sample(telemetry, 37, 65, .02)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)

    def test_pause_and_restore_keep_totals_without_live_readings_or_partial_time(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .5, hp=5)
        sample(telemetry, 2, 31, .52, hp=5)
        saved = json.loads(json.dumps(telemetry.snapshot(), allow_nan=False))
        self.assertEqual(saved["xp_gain_points"], 2)
        self.assertEqual(saved["xp_observed_seconds"], 30)
        telemetry.pause()
        restored = PlayerTelemetry(saved)
        for instance in (telemetry, restored):
            self.assertIsNone(instance.snapshot()["hp_percent"])
            self.assertIsNone(instance.snapshot()["xp_percent"])
            self.assertIsNone(instance.snapshot()["xp_points_per_hour"])
            self.assertEqual(instance.snapshot()["xp_rate_observed_seconds"], 0)
            sample(instance, 1000, 1031, .9, hp=5)
            self.assertEqual(instance.snapshot()["xp_gain_points"], 2)
            self.assertEqual(instance.snapshot()["xp_observed_seconds"], 60)
            self.assertEqual(instance.snapshot()["xp_points_per_hour"], 0)
            self.assertEqual(instance.snapshot()["hp_below10_episodes"], 2)

    def test_missing_panel_or_sample_gap_clears_recent_rate_but_keeps_totals(self):
        for missing in (True, False):
            with self.subTest(missing=missing):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 1)
                sample(telemetry, 2, 31, .52)
                self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 240)
                if missing:
                    telemetry.update(None, 32)
                else:
                    telemetry.update(reading(xp=.9), 35)
                self.assertIsNone(telemetry.snapshot()["xp_points_per_hour"])
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 2)
                sample(telemetry, 36, 67, .9)
                self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)


if __name__ == "__main__":
    unittest.main()
