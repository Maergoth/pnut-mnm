"""Anchored local health and pixel-based dual-scale XP recognition."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import unittest
from unittest.mock import Mock

import cv2
import numpy as np

from mnmparse.ocr import OcrLine
from mnmparse.player_status import PlayerStatusReading, read_player_status, read_status_crop


def panel(hp: str = "601 / 733", *, inline: bool = False) -> list[OcrLine]:
    if inline:
        return [OcrLine(180, 30, 10, "Health " + hp), OcrLine(180, 65, 10, "Mana 88/570"),
                OcrLine(170, 100, 10, "Endurance 100/100"), OcrLine(130, 170, 10, "Weight 120/122")]
    return [OcrLine(180, 30, 10, "HEALTH"), OcrLine(175, 41, 12, hp),
            OcrLine(180, 65, 10, "MANA"), OcrLine(175, 76, 12, "88 / 570"),
            OcrLine(170, 100, 10, "ENDURANCE"), OcrLine(175, 111, 12, "100 /100"),
            OcrLine(130, 170, 10, "WEIGHT"), OcrLine(210, 170, 12, "120 /122")]


def pixels(percent: float, *, fine: bool = True, green: bool = False) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    image = np.full((260, 400, 3), 90, dtype=np.uint8)
    rect = (90, 196, 330, 216)
    left, top, right, bottom = rect
    image[top:bottom, left:right] = 8
    fill = round((right - left) * percent / 100)
    image[top:bottom, left:left + fill] = (0, 188, 255) if not green else (0, 255, 0)
    if fine:
        remaining = percent % 20
        fine_fill = round((right - left) * remaining / 20)
        image[bottom - 3:bottom, left:left + fine_fill] = (255, 80, 0)
    # Thin divider marks should not be mistaken for breaks in the fill.
    for x in range(left + 48, right, 48):
        image[top:bottom - 3, x] = 25
    return image, rect


class PlayerStatusTests(unittest.TestCase):
    def test_split_and_inline_health_need_the_stat_panel_and_preserve_input(self):
        for inline in (False, True):
            with self.subTest(inline=inline):
                lines = panel(inline=inline)
                before = [asdict(line) for line in lines]
                reading = read_player_status(lines)
                self.assertIsNotNone(reading)
                self.assertEqual((reading.hp_current, reading.hp_max), (601, 733))
                self.assertAlmostEqual(reading.hp_percent, 81.99, places=2)
                self.assertIsNone(reading.xp_percent)
                self.assertEqual((reading.weight_current, reading.weight_max), (120, 122))
                self.assertFalse(reading.overburdened)
                self.assertEqual([asdict(line) for line in lines], before)

    def test_low_zero_health_and_overweight_are_not_other_resource_ratios(self):
        for current in (0, 9, 10):
            with self.subTest(current=current):
                lines = panel(f"{current}/100", inline=True)
                lines[-1].text = "Weight 150/122"
                reading = read_player_status(lines)
                self.assertIsNotNone(reading)
                self.assertEqual(reading.hp_percent, current)
                self.assertEqual(reading.hp_current, current)
                self.assertEqual((reading.weight_current, reading.weight_max), (150, 122))
                self.assertTrue(reading.overburdened)

    def test_shuffled_ocr_uses_geometry_for_split_health_and_xp(self):
        lines = panel()
        image, rect = pixels(73.2)
        for order in ((7, 6, 5, 4, 3, 2, 1, 0), (3, 6, 1, 4, 0, 7, 5, 2)):
            with self.subTest(order=order):
                # Keep x/y geometry while moving HEALTH away from its ratio in list order.
                shuffled = [lines[index] for index in order]
                reading = read_player_status(shuffled, image, xp_bar_confirmed=True)
                self.assertIsNotNone(reading)
                self.assertEqual((reading.hp_current, reading.hp_max), (601, 733))
                self.assertAlmostEqual(reading.hp_percent, 81.99, places=2)
                self.assertEqual(reading.xp_bar_rect, rect)
                self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.1)

    def test_unreadable_health_ratio_does_not_hide_independently_verified_xp(self):
        image, rect = pixels(73.2)
        for hp in ("707 1767", "707 767", "120/100", "10/0", "garbled"):
            with self.subTest(hp=hp):
                lines = panel(hp)
                before = [asdict(line) for line in lines]
                reading = read_player_status(lines, image, xp_bar_confirmed=True)
                self.assertIsNotNone(reading)
                self.assertIsNone(reading.hp_current)
                self.assertIsNone(reading.hp_max)
                self.assertIsNone(reading.hp_percent)
                self.assertEqual(reading.xp_bar_rect, rect)
                self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.1)
                self.assertEqual([asdict(line) for line in lines], before)
                for weight_only in (read_player_status(lines, image), read_player_status(lines)):
                    self.assertIsNone(weight_only.hp_current)
                    self.assertIsNone(weight_only.xp_percent)
                    self.assertEqual((weight_only.weight_current, weight_only.weight_max), (120, 122))
        # An isolated XP-looking track without verified stat labels is still ignored.
        self.assertIsNone(read_player_status(panel("707 1767")[:2], image, xp_bar_confirmed=True))

    def test_unanchored_chat_invalid_health_and_ambiguous_panels_are_ignored(self):
        inputs = [
            [OcrLine(10, 20, 10, "My Health 10/100, mana 60/200")],
            [OcrLine(10, 20, 10, "Health 10/100")],
            panel(inline=True) + [OcrLine(line.x + 350, line.y, line.h, line.text) for line in panel(inline=True)],
        ]
        for lines in inputs:
            with self.subTest(text=lines[0].text[:80]):
                self.assertIsNone(read_player_status(lines))
        # Ambiguous HP never becomes a guessed value; independently anchored
        # Weight remains readable in that same otherwise verified panel.
        reading = read_player_status(panel() + [OcrLine(181, 42, 12, "10/100")])
        self.assertIsNone(reading.hp_current)
        self.assertFalse(reading.overburdened)

    def test_weight_is_validated_and_independent_of_unreadable_health(self):
        for value, expected in (("150/122", True), ("122/122", False), ("0/122", False),
                                ("1,500/1,220", True)):
            with self.subTest(value=value):
                lines = panel("garbled")
                lines[-1].text = value
                reading = read_player_status(lines)
                self.assertIsNone(reading.hp_current)
                self.assertIs(reading.overburdened, expected)
        for value in ("150/0", "-5/122", "150/12O", "1,,50/122", "9" * 11 + "/122"):
            with self.subTest(invalid=value):
                lines = panel(inline=True)
                lines[-1].text = "Weight " + value
                self.assertIsNone(read_player_status(lines))
        legacy = PlayerStatusReading(10, 100, (0, 0, 10, 10), ())
        self.assertIsNone(legacy.overburdened)

    def test_panel_exclusions_do_not_remove_a_chat_line_on_the_same_row(self):
        lines = panel() + [OcrLine(8, 41, 12, "You slash a skeleton for 601 points of damage."),
                           OcrLine(10, 210, 12, "Your Health ability restores 733 points of health.")]
        reading = read_player_status(lines)
        self.assertIsNotNone(reading)
        self.assertIn(1, reading.panel_line_indices)
        self.assertNotIn(len(lines) - 2, reading.panel_line_indices)
        self.assertNotIn(len(lines) - 1, reading.panel_line_indices)

    def test_confirmed_bar_uses_blue_refinement_and_continuous_yellow_bucket(self):
        for percent in (0, 3.5, 19.95, 20, 39.95, 40, 73.2, 94.8, 99.5):
            with self.subTest(percent=percent):
                image, rect = pixels(percent)
                reading = read_player_status(panel(), image, xp_bar_confirmed=True)
                self.assertIsNotNone(reading)
                self.assertEqual(reading.xp_bar_rect, rect)
                self.assertAlmostEqual(reading.xp_percent, percent, delta=0.5)
                expected = (20 if percent % 20 else 100) / (rect[2] - rect[0])
                self.assertAlmostEqual(reading.xp_resolution_points, expected)

    def test_real_animated_orange_bar_keeps_its_full_track_and_stable_reading(self):
        readings = []
        for frame in (1, 10, 30):
            with self.subTest(frame=frame):
                # Only the framed XP track is retained from these live captures;
                # player names, text, other stats and chat are excluded.
                path = Path(__file__).parent / "fixtures" / f"xp-animated-bar-{frame}.png"
                track = cv2.imread(str(path))
                self.assertIsNotNone(track)
                image = np.full((260, 400, 3), 90, dtype=np.uint8)
                image[191:220, 82:340] = track
                reading = read_player_status(panel(), image, xp_bar_confirmed=True)
                self.assertIsNotNone(reading)
                self.assertEqual(reading.xp_bar_rect, (90, 196, 331, 215))
                self.assertAlmostEqual(reading.xp_percent, 88.7, delta=.1)
                self.assertAlmostEqual(reading.xp_resolution_points, 20 / 241)
                readings.append(reading.xp_percent)
        self.assertEqual(len(set(readings)), 1, "Animation must not change the measured bar position")

    def test_scaled_live_bevel_uses_one_interior_width_for_both_xp_tracks(self):
        # Only the framed XP track is retained: no stat labels, values or chat.
        track = cv2.imread(str(Path(__file__).parent / "fixtures" / "xp-scaled-bevel-bar.png"))
        self.assertIsNotNone(track)
        self.assertEqual(track.shape, (48, 396, 3))
        image = np.full((290, 448, 3), 90, dtype=np.uint8)
        image[188:236, 20:416] = track
        # This UI scale has larger stat-label anchors and a wider bevel.
        lines = [replace(line, h=18) for line in panel()]
        before = image.copy()
        reading = read_player_status(lines, image, xp_bar_confirmed=True)
        self.assertIsNotNone(reading)
        self.assertEqual(reading.xp_bar_rect, (28, 196, 412, 227))
        # The outer detection is 384 pixels wide; the shared interior is 370.
        # An internal neutral divider must not truncate that interior.
        self.assertAlmostEqual(reading.xp_percent, 67.1351351351, places=6)
        self.assertAlmostEqual(reading.xp_resolution_points, 20 / 370)
        np.testing.assert_array_equal(image, before)

    def test_scaled_live_bevel_still_rejects_disagreeing_blue_refinement(self):
        track = cv2.imread(str(Path(__file__).parent / "fixtures" / "xp-scaled-bevel-bar.png"))
        image = np.full((290, 448, 3), 90, dtype=np.uint8)
        image[188:236, 20:416] = track
        # Keep both bevels and the yellow fill. Change only the thin blue
        # refinement to disagree with the coarse track by several percent.
        image[222:225, 32:402] = 8
        image[222:225, 32:208] = (255, 80, 0)
        reading = read_player_status([replace(line, h=18) for line in panel()], image,
                                     xp_bar_rect=(28, 196, 412, 227))
        self.assertIsNotNone(reading)
        self.assertIsNone(reading.xp_percent)

    def test_bevel_normalization_requires_two_bounded_outer_rails(self):
        from mnmparse.player_status import _xp_horizontal_interior

        for kind in ("none", "left only", "right only", "interior dividers", "too wide"):
            with self.subTest(kind=kind):
                track = np.full((30, 384, 3), 8, dtype=np.uint8)
                if kind == "left only":
                    track[:, 1:4] = 90
                elif kind == "right only":
                    track[:, 374:376] = 90
                elif kind == "interior dividers":
                    track[:, 74:78] = track[:, 302:306] = 90
                elif kind == "too wide":
                    track[:, :20] = track[:, -20:] = 90
                self.assertIs(_xp_horizontal_interior(track), track)

    def test_orange_text_below_weight_is_not_an_xp_track(self):
        image = np.full((260, 400, 3), 90, dtype=np.uint8)
        cv2.putText(image, "Experience gained", (90, 212), cv2.FONT_HERSHEY_SIMPLEX,
                    .5, (0, 90, 255), 1, cv2.LINE_AA)
        reading = read_player_status(panel(), image, xp_bar_confirmed=True)
        self.assertIsNotNone(reading)
        self.assertIsNone(reading.xp_percent)
        self.assertIsNone(reading.xp_bar_rect)

    def test_unconfirmed_bar_and_invalid_calibration_do_not_create_xp(self):
        image, rect = pixels(73.2)
        reading = read_player_status(panel(), image)
        self.assertEqual(reading.xp_bar_rect, rect)
        self.assertIsNone(reading.xp_percent)
        self.assertIsNone(reading.xp_resolution_points)
        calibrated = read_player_status(panel(), image, xp_bar_rect=rect)
        self.assertAlmostEqual(calibrated.xp_percent, 73.2, delta=0.1)
        invalid = read_player_status(panel(), image, xp_bar_rect=(-1, 196, 330, 216))
        self.assertIsNone(invalid.xp_percent)

    def test_yellow_only_is_lower_resolution_and_wrong_color_is_unknown(self):
        image, rect = pixels(73.2, fine=False)
        reading = read_player_status(panel(), image, xp_bar_confirmed=True)
        self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.5)
        self.assertAlmostEqual(reading.xp_resolution_points, 100 / 240)
        image, _ = pixels(73.2, green=True)
        self.assertIsNone(read_player_status(panel(), image, xp_bar_rect=rect).xp_percent)
        # A blue fill in the main bar is not the documented thin refinement.
        image, _ = pixels(73.2)
        image[198:211, 90:260] = (255, 80, 0)
        self.assertIsNone(read_player_status(panel(), image, xp_bar_rect=rect).xp_percent)

    def test_blue_and_yellow_disagreement_cannot_report_a_plausible_wrong_bucket(self):
        image, rect = pixels(73.2)
        image[213:216, 90:330] = 8
        image[213:216, 90:120] = (255, 80, 0)
        reading = read_player_status(panel(), image, xp_bar_rect=rect)
        self.assertIsNone(reading.xp_percent)


class StatsCropOcrTests(unittest.TestCase):
    @staticmethod
    def enlarged(lines, extra):
        return [OcrLine(round(line.x * extra), round(line.y * extra), round(line.h * extra), line.text)
                for line in lines]

    def test_readable_health_uses_one_enlarged_ocr_pass(self):
        image, rect = pixels(73.2)
        expected = panel()
        ocr = Mock(read=Mock(return_value=self.enlarged(expected, 1.5)))
        reading, returned = read_status_crop(ocr, image, "maxchannel", 1.0)
        self.assertEqual(reading.hp_current, 601)
        self.assertEqual(reading.xp_bar_rect, rect)
        self.assertEqual([asdict(line) for line in returned], [asdict(line) for line in expected])
        ocr.read.assert_called_once()
        self.assertEqual(ocr.read.call_args.args[0].shape, (390, 600, 3))

    def test_unscales_only_additional_factor_for_existing_ocr_scale(self):
        image, rect = pixels(73.2)
        for base_scale in (0.5, 1.0, 1.25, 1.5, 2.0):
            with self.subTest(scale=base_scale):
                expected = panel()
                effective = max(base_scale, 1.5)
                input_lines = self.enlarged(expected, effective / base_scale)
                before = [asdict(line) for line in input_lines]
                ocr = Mock(read=Mock(return_value=input_lines))
                reading, returned = read_status_crop(ocr, image, "gray", base_scale)
                self.assertEqual((reading.hp_current, reading.hp_max), (601, 733))
                self.assertEqual(reading.xp_bar_rect, rect)
                self.assertEqual([asdict(line) for line in returned], [asdict(line) for line in expected])
                self.assertEqual([asdict(line) for line in input_lines], before)
                self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.1)
                ocr.read.assert_called_once()
                self.assertEqual(ocr.read.call_args.args[0].shape,
                                 (round(260 * effective), round(400 * effective), 3))
                if base_scale >= 1.5:
                    self.assertIs(returned, input_lines)

    def test_xp_uses_original_color_pixels_and_returned_rows_have_correct_indices(self):
        image, rect = pixels(73.2)
        original = image.copy()
        expected = [OcrLine(5, 2, 8, "Unrelated"), *panel()]
        ocr = Mock(read=Mock(return_value=self.enlarged(expected, 1.5)))
        reading, returned = read_status_crop(ocr, image, "gray", 1.0)
        self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.1)
        self.assertEqual(reading.xp_bar_rect, rect)
        self.assertEqual(reading.panel_line_indices, tuple(range(1, len(expected))))
        self.assertEqual([asdict(line) for line in returned], [asdict(line) for line in expected])
        np.testing.assert_array_equal(image, original)
        # The OCR image is grayscale and enlarged, while XP remained readable.
        processed = ocr.read.call_args.args[0]
        np.testing.assert_array_equal(processed[:, :, 0], processed[:, :, 1])
        ocr.read.assert_called_once()

    def test_unknown_health_keeps_independently_verified_xp_without_second_pass(self):
        image, rect = pixels(73.2)
        expected = panel("601 1733")
        ocr = Mock(read=Mock(return_value=self.enlarged(expected, 1.5)))
        reading, returned = read_status_crop(ocr, image, "maxchannel", 1.0)
        self.assertIsNone(reading.hp_percent)
        self.assertEqual(reading.xp_bar_rect, rect)
        self.assertAlmostEqual(reading.xp_percent, 73.2, delta=0.1)
        self.assertEqual([asdict(line) for line in returned], [asdict(line) for line in expected])
        ocr.read.assert_called_once()

    def test_accidentally_large_regions_are_not_additionally_enlarged(self):
        image = np.zeros((1000, 1000, 3), dtype=np.uint8)
        ocr = Mock(read=Mock(return_value=[]))
        reading, returned = read_status_crop(ocr, image, "maxchannel", 1.0)
        self.assertIsNone(reading)
        self.assertEqual(returned, [])
        self.assertEqual(ocr.read.call_args.args[0].shape, image.shape)
        ocr.read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
