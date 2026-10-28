"""Stable XP losses and resurrection gains share signed session/rate totals."""
from __future__ import annotations

import json
import unittest

from mnmparse.player_telemetry import PlayerTelemetry
from tests.test_player_telemetry import reading, sample


class PlayerXPLossTests(unittest.TestCase):
    def death(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 20, .8)
        sample(telemetry, 21, 31, .6)
        return telemetry

    def test_twenty_point_death_is_a_signed_thirty_second_measurement(self):
        telemetry = self.death()
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_percent"], 60)
        self.assertEqual(snap["xp_gain_points"], -20)
        self.assertEqual(snap["xp_rate_gain_points"], -20)
        self.assertEqual(snap["xp_observed_seconds"], 30)
        self.assertEqual(snap["xp_rate_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], -2400)
        self.assertEqual(snap["xp_level_ups"], 0)

    def test_exact_twenty_five_point_loss_and_one_pixel_allowance_are_accepted(self):
        for fraction, expected in ((.55, -25), (.545, -25.5)):
            with self.subTest(fraction=fraction):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 20, .8)
                sample(telemetry, 21, 31, fraction)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], expected)
                self.assertEqual(snap["xp_rate_gain_points"], expected)
                self.assertEqual(snap["xp_observed_seconds"], 30)
                self.assertEqual(snap["xp_level_ups"], 0)

    def test_larger_unconfirmed_drop_rebaselines_instead_of_inventing_a_loss_or_level(self):
        for fraction, extra in ((.54, {}), (.5499, {"xp_resolution_points": .001})):
            with self.subTest(fraction=fraction, extra=extra):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 20, .8, **extra)
                sample(telemetry, 21, 100, fraction, **extra)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 0)
                self.assertEqual(snap["xp_rate_gain_points"], 0)
                self.assertEqual(snap["xp_points_per_hour"], 0)
                self.assertEqual(snap["xp_level_ups"], 0)

    def test_two_twenty_point_deaths_in_one_checkpoint_are_not_one_invalid_forty_point_jump(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 9, .8)
        sample(telemetry, 10, 11, .6)
        sample(telemetry, 12, 31, .4)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], -40)
        self.assertEqual(snap["xp_rate_gain_points"], -40)
        self.assertEqual(snap["xp_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], -4800)
        self.assertEqual(snap["xp_level_ups"], 0)

    def test_resurrection_after_loss_restores_partial_then_full_net_xp(self):
        telemetry = self.death()
        sample(telemetry, 32, 61, .7)
        partial = telemetry.snapshot()
        self.assertEqual(partial["xp_gain_points"], -10)
        self.assertEqual(partial["xp_rate_gain_points"], -10)
        self.assertEqual(partial["xp_observed_seconds"], 60)
        self.assertEqual(partial["xp_rate_observed_seconds"], 60)
        self.assertEqual(partial["xp_points_per_hour"], -600)
        sample(telemetry, 62, 91, .8)
        full = telemetry.snapshot()
        self.assertEqual(full["xp_gain_points"], 0)
        self.assertEqual(full["xp_rate_gain_points"], 0)
        self.assertEqual(full["xp_observed_seconds"], 90)
        self.assertEqual(full["xp_rate_observed_seconds"], 90)
        self.assertEqual(full["xp_points_per_hour"], 0)
        self.assertEqual(full["xp_level_ups"], 0)

    def test_resurrection_before_drop_grace_expires_is_measured_as_net_movement(self):
        for restored, expected in ((.7, -10), (.8, 0)):
            with self.subTest(restored=restored):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 20, .8)
                sample(telemetry, 21, 22, .6)
                sample(telemetry, 23, 31, restored)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], expected)
                self.assertEqual(snap["xp_rate_gain_points"], expected)
                self.assertEqual(snap["xp_observed_seconds"], 30)
                self.assertEqual(snap["xp_level_ups"], 0)

    def test_positive_and_negative_checkpoints_cancel_without_erasing_measurement_time(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .4)
        sample(telemetry, 2, 31, .6)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 20)
        sample(telemetry, 32, 61, .4)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 0)
        self.assertEqual(snap["xp_rate_gain_points"], 0)
        self.assertEqual(snap["xp_rate_observed_seconds"], 60)
        self.assertEqual(snap["xp_points_per_hour"], 0)

    def test_first_loss_after_long_idle_excludes_only_the_leading_idle_from_rate(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1801, .8)
        sample(telemetry, 1802, 1831, .6)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], -20)
        self.assertEqual(snap["xp_observed_seconds"], 1830)
        self.assertEqual(snap["xp_rate_gain_points"], -20)
        self.assertEqual(snap["xp_rate_observed_seconds"], 30)
        self.assertEqual(snap["xp_points_per_hour"], -2400)

    def test_genuine_level_wrap_takes_precedence_over_unconfirmed_loss(self):
        for chat_first in (False, True):
            with self.subTest(chat_first=chat_first):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 24, .98)
                if chat_first:
                    telemetry.note_level_up(25)
                sample(telemetry, 25, 26, .02)
                if not chat_first:
                    telemetry.note_level_up(25)
                sample(telemetry, 27, 31, .02)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 4)
                self.assertEqual(snap["xp_level_ups"], 1)
                self.assertEqual(snap["xp_points_per_hour"], 480)
                self.assertFalse(telemetry.note_level_up(25))

    def test_plausible_loss_becomes_positive_wrap_when_level_chat_arrives_during_grace(self):
        for arrival in (26, 33):
            with self.subTest(arrival=arrival):
                telemetry = PlayerTelemetry()
                sample(telemetry, 0, 24, .9)
                sample(telemetry, 25, arrival, .7)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
                telemetry.note_level_up(25)
                sample(telemetry, arrival + 1, 61, .7)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], 80)
                self.assertEqual(snap["xp_rate_gain_points"], 80)
                self.assertEqual(snap["xp_level_ups"], 1)
                self.assertFalse(telemetry.note_level_up(25))

    def test_genuine_wrap_after_a_completed_death_preserves_the_prior_loss(self):
        telemetry = self.death()
        sample(telemetry, 32, 54, .98)
        telemetry.note_level_up(55)
        sample(telemetry, 55, 61, .02)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], 22)
        self.assertEqual(snap["xp_rate_gain_points"], 22)
        self.assertEqual(snap["xp_rate_observed_seconds"], 60)
        self.assertEqual(snap["xp_level_ups"], 1)

    def test_single_bad_low_read_and_duplicate_timestamps_do_not_count_a_loss(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 30, .8)
        self.assertFalse(telemetry.update(reading(xp=.6), 30))
        self.assertFalse(telemetry.update(reading(xp=.6), 29))
        telemetry.update(reading(xp=.6), 31)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_percent"], 80)
        sample(telemetry, 32, 80, .8)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_level_ups"], 0)

    def test_loss_requires_two_reads_and_is_not_recounted_from_unchanged_low_bar(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 20, .8)
        telemetry.update(reading(xp=.6), 21)
        self.assertEqual(telemetry.snapshot()["xp_percent"], 80)
        telemetry.update(reading(xp=.6), 22)
        self.assertEqual(telemetry.snapshot()["xp_percent"], 60)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 23, 181, .6)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], -20)
        self.assertEqual(snap["xp_observed_seconds"], 180)
        self.assertEqual(snap["xp_points_per_hour"], -400)
        self.assertEqual(snap["xp_level_ups"], 0)

    def test_subpixel_negative_movement_accumulates_symmetrically_without_recounting(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .5, xp_resolution_points=.5)
        for start, end, fraction in ((2, 31, .498), (32, 61, .496)):
            sample(telemetry, start, end, fraction, xp_resolution_points=.5)
            self.assertEqual(telemetry.snapshot()["xp_gain_points"], 0)
        sample(telemetry, 62, 91, .494, xp_resolution_points=.5)
        snap = telemetry.snapshot()
        self.assertEqual(snap["xp_gain_points"], -.6)
        self.assertEqual(snap["xp_rate_observed_seconds"], 90)
        self.assertEqual(snap["xp_points_per_hour"], -24)
        sample(telemetry, 92, 121, .494, xp_resolution_points=.5)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], -.6)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], -18)

    def test_negative_rolling_interval_ages_proportionally_and_eventually_expires(self):
        telemetry = self.death()
        sample(telemetry, 32, 3601, .6)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], -20)
        telemetry.update(reading(xp=.6), 3602)
        snap = telemetry.snapshot()
        self.assertAlmostEqual(snap["xp_rate_gain_points"], -20 * 29 / 30, places=6)
        self.assertEqual(snap["xp_rate_observed_seconds"], 3599)
        self.assertEqual(snap["xp_gain_points"], -20)
        self.assertEqual(telemetry.snapshot(), snap, "Taking a snapshot must not mutate rate history")
        sample(telemetry, 3603, 3631, .6)
        self.assertEqual(telemetry.snapshot()["xp_rate_gain_points"], 0)
        self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)
        self.assertEqual(telemetry.snapshot()["xp_gain_points"], -20)

    def test_carried_subpixel_loss_ages_over_the_original_measurement_span(self):
        telemetry = PlayerTelemetry()
        sample(telemetry, 0, 1, .5, xp_resolution_points=.5)
        for start, end, fraction in ((2, 31, .498), (32, 61, .496), (62, 91, .494)):
            sample(telemetry, start, end, fraction, xp_resolution_points=.5)
        for start, end, expected in ((92, 3631, -.4), (3632, 3661, -.2), (3662, 3691, 0)):
            sample(telemetry, start, end, .494, xp_resolution_points=.5)
            snap = telemetry.snapshot()
            self.assertAlmostEqual(snap["xp_rate_gain_points"], expected)
            self.assertEqual(snap["xp_gain_points"], -.6)
            self.assertEqual(snap["xp_rate_observed_seconds"], 3600)

    def test_signed_totals_survive_json_restore_and_resume_without_bridging_offline_xp(self):
        telemetry = self.death()
        saved = json.loads(json.dumps(telemetry.snapshot(), allow_nan=False))
        self.assertEqual(saved["xp_gain_points"], -20)
        telemetry.pause()
        restored = PlayerTelemetry(saved)
        for instance in (telemetry, restored):
            with self.subTest(restored=instance is restored):
                before = instance.snapshot()
                self.assertEqual(before["xp_gain_points"], -20)
                self.assertEqual(before["xp_observed_seconds"], 30)
                self.assertIsNone(before["xp_percent"])
                self.assertIsNone(before["xp_points_per_hour"])
                sample(instance, 1000, 1031, .9)
                snap = instance.snapshot()
                self.assertEqual(snap["xp_gain_points"], -20)
                self.assertEqual(snap["xp_observed_seconds"], 60)
                self.assertEqual(snap["xp_points_per_hour"], 0)
                sample(instance, 1032, 1061, .95)
                self.assertEqual(instance.snapshot()["xp_gain_points"], -15)

    def test_pause_missing_panel_and_capture_gaps_keep_completed_losses_but_clear_live_rate(self):
        for interrupt in ("pause", "missing", "gap", "resize"):
            with self.subTest(interrupt=interrupt):
                telemetry = self.death()
                if interrupt == "pause":
                    telemetry.pause()
                elif interrupt == "missing":
                    telemetry.update(None, 32)
                elif interrupt == "gap":
                    telemetry.update(reading(xp=.4), 35)
                else:
                    telemetry.update(reading(xp=.4, xp_pixels=400), 32)
                snap = telemetry.snapshot()
                self.assertEqual(snap["xp_gain_points"], -20)
                self.assertIsNone(snap["xp_points_per_hour"])
                self.assertEqual(snap["xp_rate_observed_seconds"], 0)
                sample(telemetry, 1000, 1031, .4)
                self.assertEqual(telemetry.snapshot()["xp_gain_points"], -20)
                self.assertEqual(telemetry.snapshot()["xp_points_per_hour"], 0)


if __name__ == "__main__":
    unittest.main()
