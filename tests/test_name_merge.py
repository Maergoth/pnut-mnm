"""OCR-noise name merging in stats (truncated reads and glyph confusions)."""

from __future__ import annotations

import unittest
from collections import Counter

from mnmparse.stats import canonical_names, name_similar


class NameMergeTests(unittest.TestCase):
    def test_glyph_confusions(self) -> None:
        self.assertTrue(name_similar("WulDliso", "Wululiso"))
        self.assertTrue(name_similar("Pioef", "Pidef"))
        self.assertTrue(name_similar("SDIito", "Sigito"))
        self.assertTrue(name_similar("Povevizu", "Povebizu"))

    def test_truncated_npc_read_is_a_prefix(self) -> None:
        self.assertTrue(name_similar("a skeletal", "a skeletal marksman"))
        self.assertFalse(name_similar("a skeletal marksman", "a skeletal warrior"))
        self.assertFalse(name_similar("a", "a skeletal warrior"))  # a bare article never merges

    def test_canonical_prefers_frequent_full_name(self) -> None:
        counts = Counter({"a skeletal marksman": 40, "a skeletal": 3, "Wululiso": 50, "WulDliso": 2})
        canon = canonical_names(counts)
        self.assertEqual(canon["a skeletal"], "a skeletal marksman")
        self.assertEqual(canon["WulDliso"], "Wululiso")
        self.assertEqual(canon["Wululiso"], "Wululiso")

    def test_two_frequent_similar_names_stay_apart(self) -> None:
        counts = Counter({"Tovozen": 30, "Tovozev": 25})
        canon = canonical_names(counts)
        self.assertEqual(canon["Tovozev"], "Tovozev")


if __name__ == "__main__":
    unittest.main()
