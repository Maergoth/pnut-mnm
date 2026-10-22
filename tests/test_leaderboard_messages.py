"""Optional public messages remain bounded text with a modest whole-word filter."""
from __future__ import annotations

import unittest

from mnmparse.leaderboard_messages import MAX_MESSAGE_CHARS, normalize_message, validate_message


class LeaderboardMessageTests(unittest.TestCase):
    def test_censors_case_punctuation_and_common_inflections(self):
        self.assertEqual(normalize_message("Well, SHIT! That fucking bastard."),
                         "Well, ****! That ******* *******.")
        self.assertEqual(normalize_message("assholes / motherfuckers / bullshit / bitching"),
                         "******** / ************* / ******** / ********")

    def test_does_not_filter_word_substrings(self):
        value = "A classic grassland cocktail at Scunthorpe, with a shell."
        self.assertEqual(normalize_message(value), value)
        self.assertEqual(normalize_message("SHİT shıt caféshit"), "SHİT shıt caféshit")

    def test_normalizes_spacing_controls_and_compatibility_characters(self):
        self.assertEqual(normalize_message("  ＳＨＩＴ\t\r\nhello\x00\u202eworld  "), "**** helloworld")
        self.assertEqual(normalize_message("f\u200buck"), "****")

    def test_plain_markup_and_unicode_are_not_interpreted(self):
        value = '<b>Victory</b> & "great" 🐉'
        self.assertEqual(normalize_message(value), value)
        self.assertIsNone(validate_message(value))

    def test_codepoint_limit_and_exact_signed_canonical_form(self):
        self.assertEqual(normalize_message("🐉" * MAX_MESSAGE_CHARS), "🐉" * MAX_MESSAGE_CHARS)
        for value in ("a" * (MAX_MESSAGE_CHARS + 1), None, 3):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                normalize_message(value)
        for value in (" SHIT ", "line\nbreak", "Ｆｕｌｌ", "uncensored shit"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_message(value)
        self.assertIsNone(validate_message(""))
        self.assertIsNone(validate_message("Already ****"))


if __name__ == "__main__":
    unittest.main()
