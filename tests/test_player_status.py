"""Anchored local health and pixel-based dual-scale XP recognition."""
from __future__ import annotations

from dataclasses import asdict
import unittest

import numpy as np

from mnmparse.ocr import OcrLine
from mnmparse.player_status import read_player_status


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

    def test_unanchored_chat_invalid_health_and_ambiguous_panels_are_ignored(self):
        inputs = [
            [OcrLine(10, 20, 10, "My Health 10/100, mana 60/200")],
            [OcrLine(10, 20, 10, "Health 10/100")],
            panel("120/100", inline=True), panel("10/0", inline=True),
            panel("1,,0/100", inline=True), panel("9" * 5000 + "/100", inline=True),
            panel(inline=True) + [OcrLine(line.x + 350, line.y, line.h, line.text) for line in panel(inline=True)],
        ]
        for lines in inputs:
            with self.subTest(text=lines[0].text[:80]):
                self.assertIsNone(read_player_status(lines))
        # A foreign ratio adjacent to HEALTH is ambiguous, not a fallback.
        self.assertIsNone(read_player_status(panel() + [OcrLine(181, 42, 12, "10/100")]))

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


if __name__ == "__main__":
    unittest.main()
