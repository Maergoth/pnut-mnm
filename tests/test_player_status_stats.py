"""Synthetic, deliberately fictional mini-stat layouts and bounded OCR retries."""
from __future__ import annotations

from dataclasses import asdict
import unittest
from unittest.mock import patch

import numpy as np

from mnmparse.ocr import OcrLine
from mnmparse.player_status import PlayerStatusReading, read_player_status, read_status_crop


ATTRIBUTES = ("strength", "stamina", "agility", "dexterity", "intelligence", "wisdom", "charisma")
RESISTS = ("resist_cold", "resist_corruption", "resist_disease", "resist_electricity",
           "resist_fire", "resist_holy", "resist_magic", "resist_poison")
VALUES = dict(zip((*ATTRIBUTES, "armor_class", *RESISTS),
                  (71, 63, 45, 29, 31, 57, 42, 369, 11, -3, 9, 4, 15, 2, 6, 0)))


def fictional_panel(*, inline=False):
    resources = [OcrLine(218, 80, 18, "HEALTH"), OcrLine(208, 100, 22, "402/645"),
                 OcrLine(224, 142, 18, "MANA"), OcrLine(216, 162, 22, "85/489"),
                 OcrLine(198, 205, 18, "ENDURANCE"), OcrLine(216, 226, 22, "74/110"),
                 OcrLine(138, 337, 18, "WEIGHT"), OcrLine(298, 337, 19, "40/160")]
    rows = [(48, 84 + 22 * index, 16, label, VALUES[field]) for index, (label, field) in enumerate(
        zip(("STR", "STA", "AGI", "DEX"), ATTRIBUTES[:4]))]
    rows += [(370, 91 + 26 * index, 17, label, VALUES[field]) for index, (label, field) in enumerate(
        zip(("INT", "WIS", "CHA"), ATTRIBUTES[4:]))]
    rows += [(47, 230 + 22 * index, 16, label, VALUES[field]) for index, (label, field) in enumerate(
        zip(("COLD", "CORR", "DIS", "ELEC"), RESISTS[:4]))]
    rows += [(370, 230 + 22 * index, 16, label, VALUES[field]) for index, (label, field) in enumerate(
        zip(("FIRE", "HOLY", "MAG", "POI"), RESISTS[4:]))]
    stats = []
    for x, y, height, label, value in rows:
        if inline:
            stats.append(OcrLine(x, y, height, f"{label}: {value}"))
        else:
            stats.extend((OcrLine(x, y, height, label), OcrLine(126 if x < 200 else 445, y + 1, height, str(value))))
    # AC uses a separate centered value below the label.
    stats.extend((OcrLine(246, 280, 18, "AC"), OcrLine(240, 305, 20, str(VALUES["armor_class"]))))
    return resources + stats


def transform(lines, scale=1, dx=0, dy=0):
    return [OcrLine(round(line.x * scale) + dx, round(line.y * scale) + dy,
                    max(1, round(line.h * scale)), line.text) for line in lines]


def synthetic_pixels():
    # Coordinate colors let the fake OCR locate a requested synthetic subregion.
    # There are no real names, images, numeric readings or OCR fixtures here.
    y, x = np.indices((440, 520))
    return np.stack(((x // 2) % 256, (y // 2) % 256, np.zeros_like(x)), axis=2).astype(np.uint8)


class FocusOcr:
    def __init__(self, *, scale=1, conflict=False, bare=False, fail=False, initial=None):
        self.scale = scale
        self.conflict, self.bare, self.fail = conflict, bare, fail
        self.complete = fictional_panel()
        self.initial = initial if initial is not None else [line for line in self.complete if line.text not in ("INT", "DEX")]
        self.calls = []

    def read(self, image):
        self.calls.append(image.shape)
        if len(self.calls) == 1:
            return transform(self.initial, max(1.5, self.scale) / self.scale)
        if self.fail:
            raise RuntimeError("optional OCR unavailable")
        origin_x, origin_y = int(image[0, 0, 0]) * 2, int(image[0, 0, 1]) * 2
        left = origin_x < 200
        rows = [line for line in self.complete if (line.x < 180 if left else line.x > 350) and line.y < 180]
        if self.bare:
            rows = [line for line in rows if line.text not in ("INT", "DEX")]
        if self.conflict:
            rows = [OcrLine(line.x, line.y, line.h, "999" if line.text in ("71", "57") else line.text) for line in rows]
        return [OcrLine(round((line.x - origin_x) * 2 / self.scale),
                        round((line.y - origin_y) * 2 / self.scale),
                        round(line.h * 2 / self.scale), line.text) for line in rows]


class MiniStatSpatialTests(unittest.TestCase):
    def test_existing_positional_constructor_keeps_all_new_fields_unknown(self):
        reading = PlayerStatusReading(4, 8, (0, 0, 30, 30), (), 2.5, None, None, 5, 6, 7)
        self.assertEqual(reading.mana_max, 7)
        self.assertTrue(all(getattr(reading, field) is None for field in VALUES))

    def test_all_explicit_split_and_inline_fields_and_below_ac(self):
        for inline in (False, True):
            with self.subTest(inline=inline):
                lines = fictional_panel(inline=inline)
                before = [asdict(line) for line in lines]
                reading = read_player_status(list(reversed(lines)))
                self.assertEqual({field: getattr(reading, field) for field in VALUES}, VALUES)
                self.assertEqual((reading.hp_max, reading.mana_max), (645, 489))
                self.assertEqual([asdict(line) for line in lines], before)

    def test_spatial_pairing_tracks_translation_and_font_size(self):
        for scale, dx, dy in ((.6, 0, 0), (1, 100, 70), (1.8, 50, 100)):
            with self.subTest(scale=scale):
                reading = read_player_status(transform(fictional_panel(), scale, dx, dy))
                self.assertEqual({field: getattr(reading, field) for field in VALUES}, VALUES)

    def test_missing_labels_never_inherit_a_bare_number(self):
        lines = [line for line in fictional_panel() if line.text not in ("INT", "DEX")]
        reading = read_player_status(lines)
        self.assertIsNone(reading.intelligence)
        self.assertIsNone(reading.dexterity)
        self.assertEqual(reading.strength, VALUES["strength"])

    def test_signed_bounds_zero_and_grouped_digits_are_explicit(self):
        for text, expected in (("−7", -7), ("+12", 12), ("0", 0), ("1,234", 1234),
                               ("-1,000,000,000", -1_000_000_000), ("1000000000", 1_000_000_000)):
            with self.subTest(text=text):
                lines = fictional_panel(inline=True)
                next(line for line in lines if line.text.startswith("STR:")).text = "STR: " + text
                self.assertEqual(read_player_status(lines).strength, expected)
        for text in ("1,,000", "1 000", "1000000001", "-1000000001", "--3", "O", "3/6"):
            with self.subTest(invalid=text):
                lines = fictional_panel(inline=True)
                next(line for line in lines if line.text.startswith("STR:")).text = "STR: " + text
                self.assertIsNone(read_player_status(lines).strength)

    def test_ambiguous_numbers_duplicate_labels_and_cross_field_reuse_are_unknown(self):
        variants = [fictional_panel() + [OcrLine(132, 85, 16, "88")],
                    fictional_panel() + [OcrLine(48, 84, 16, "STR")],
                    fictional_panel() + [OcrLine(48, 84, 16, "STR 71")]]
        for lines in variants:
            self.assertIsNone(read_player_status(lines).strength)
        lines = fictional_panel()
        next(line for line in lines if line.text == "STA").y = 84
        reading = read_player_status(lines)
        self.assertIsNone(reading.strength)
        self.assertIsNone(reading.stamina)

    def test_a_separately_ocr_read_minus_cannot_become_a_positive_highscore(self):
        lines = fictional_panel()
        lines.append(OcrLine(108, 85, 10, "−"))
        self.assertIsNone(read_player_status(lines).strength)

    def test_explicit_long_labels_are_accepted_but_similar_unrecognized_labels_are_not(self):
        lines = fictional_panel(inline=True)
        replacements = {"STR": "Strength", "STA": "Stamina", "AGI": "Agility", "DEX": "Dexterity",
                        "INT": "Intelligence", "WIS": "Wisdom", "CHA": "Charisma", "CORR": "Corruption",
                        "DIS": "Disease", "ELEC": "Electricity", "MAG": "Magic", "POI": "Poison"}
        for line in lines:
            label, separator, rest = line.text.partition(":")
            if label in replacements:
                line.text = replacements[label] + separator + rest
        reading = read_player_status(lines)
        self.assertEqual({field: getattr(reading, field) for field in VALUES}, VALUES)
        for line in lines:
            if line.text.startswith("Intelligence:"):
                line.text = "1NT: 999"
        self.assertIsNone(read_player_status(lines).intelligence)

    def test_no_unanchored_chat_or_outside_panel_values(self):
        self.assertIsNone(read_player_status(fictional_panel()[8:]))
        lines = fictional_panel() + [OcrLine(1, 84, 16, "STR 999"), OcrLine(5, 85, 16, "999"),
                                    OcrLine(48, 500, 16, "STR 999")]
        reading = read_player_status(lines)
        self.assertEqual(reading.strength, VALUES["strength"])
        self.assertTrue(all(index not in reading.panel_line_indices for index in range(len(lines) - 3, len(lines))))

    def test_attribute_boxes_above_resource_padding_exclude_only_verified_side_boxes(self):
        lines = fictional_panel()
        # Move only resource rows downward: the upper attribute boxes now lie
        # above the old Health-derived top, as in a differently arranged panel.
        for line in lines[:6]:
            line.y += 110
        # Keep Weight below every resource and make the resource stack compact.
        lines[6].y = lines[7].y = 390
        lines.extend((OcrLine(220, 70, 16, "STR"), OcrLine(280, 71, 16, "999"),
                      OcrLine(5, 90, 16, "STR 999")))
        reading = read_player_status(lines)
        self.assertIsNotNone(reading)
        self.assertLess(next(line.y for line in lines if line.text == "STR"), reading.panel_rect[1])
        self.assertEqual(reading.strength, VALUES["strength"])
        self.assertEqual(reading.intelligence, VALUES["intelligence"])
        for index, line in enumerate(lines[:-3]):
            if line.text in ("STR", "71", "INT", "31"):
                self.assertIn(index, reading.panel_line_indices)
        self.assertTrue(all(index not in reading.panel_line_indices for index in range(len(lines) - 3, len(lines))))


class MiniStatFocusedOcrTests(unittest.TestCase):
    def test_two_small_group_passes_recover_missing_labels_without_returning_extra_rows(self):
        for scale in (.5, 1, 1.5, 2):
            with self.subTest(base_scale=scale):
                ocr = FocusOcr(scale=scale)
                reading, returned = read_status_crop(ocr, synthetic_pixels(), "none", scale)
                self.assertEqual({field: getattr(reading, field) for field in VALUES}, VALUES)
                self.assertEqual([asdict(line) for line in returned], [asdict(line) for line in ocr.initial])
                self.assertEqual(len(ocr.calls), 3)
                self.assertLess(sum(height * width for height, width, _channels in ocr.calls[1:]), 200_000)
                self.assertEqual(reading.panel_line_indices, tuple(range(len(returned))))

    def test_complete_group_values_need_only_the_initial_pass(self):
        ocr = FocusOcr(initial=fictional_panel())
        reading, _ = read_status_crop(ocr, synthetic_pixels(), "none", 1)
        self.assertEqual(reading.dexterity, VALUES["dexterity"])
        self.assertEqual(len(ocr.calls), 1)

    def test_optional_pass_conflicts_bare_values_and_exceptions_preserve_primary_values(self):
        for options in ({"conflict": True}, {"bare": True}, {"fail": True}):
            with self.subTest(options=options):
                ocr = FocusOcr(**options)
                reading, _ = read_status_crop(ocr, synthetic_pixels(), "none", 1)
                self.assertIsNone(reading.intelligence)
                self.assertIsNone(reading.dexterity)
                self.assertEqual(reading.strength, VALUES["strength"])
                self.assertEqual(reading.wisdom, VALUES["wisdom"])
                self.assertEqual(reading.hp_max, 645)
                self.assertEqual(len(ocr.calls), 3)

    def test_single_remaining_label_cannot_establish_a_retry_group(self):
        initial = [line for line in fictional_panel() if line.text not in ("STR", "STA", "DEX", "INT", "WIS")]
        ocr = FocusOcr(initial=initial)
        reading, _ = read_status_crop(ocr, synthetic_pixels(), "none", 1)
        self.assertIsNone(reading.intelligence)
        self.assertIsNone(reading.dexterity)
        self.assertEqual(len(ocr.calls), 1)

    def test_between_call_deadline_stops_additional_retries(self):
        ocr = FocusOcr()
        with patch("mnmparse.player_status.time.monotonic", side_effect=(0, .01, .3)):
            reading, _ = read_status_crop(ocr, synthetic_pixels(), "none", 1)
        self.assertEqual(reading.dexterity, VALUES["dexterity"])
        self.assertIsNone(reading.intelligence)
        self.assertEqual(len(ocr.calls), 2)

    def test_total_pixel_budget_can_skip_all_optional_passes(self):
        ocr = FocusOcr()
        with patch("mnmparse.player_status._FOCUSED_OCR_MAX_PIXELS", 1):
            reading, _ = read_status_crop(ocr, synthetic_pixels(), "none", 1)
        self.assertIsNone(reading.dexterity)
        self.assertEqual(len(ocr.calls), 1)


if __name__ == "__main__":
    unittest.main()
