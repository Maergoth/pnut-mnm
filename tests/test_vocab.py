"""Learned spellings (mnmparse.vocab) and replayed-block removal for imports (mnmparse.replay)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mnmparse.replay import find_replays
from mnmparse.vocab import Vocabulary, close_spellings, fold


class CloseSpellingTests(unittest.TestCase):
    def test_one_or_two_characters_inside_a_word(self) -> None:
        for a, b in [
            ("Bone Ohips", "Bone Chips"),
            ("Night HarbOF (East)", "Night Harbor (East)"),
            ("Dogabetarozem", "Dogabetarolem"),
            ("Tomb of the Last Wvrmsbane", "Tomb of the Last Wyrmsbane"),
            ("Night Harbor (East).-", "Night Harbor (East)"),  # junk at the end
            ("Night Harbor (Éast)", "Night Harbor (East)"),  # accent
            ("ght Harbor (East)", "Night Harbor (East)"),  # leading letters clipped
            ("Funavabeb0zeses", "Funavabebozeses"),  # look-alike digits
        ]:
            self.assertTrue(close_spellings(fold(a), fold(b)), f"{a} ~ {b}")

    def test_different_names_stay_apart(self) -> None:
        for a, b in [
            ("Tattered Rawhide Cap", "Tattered Rawhide Cape"),  # short words must match
            ("Rusty Mace", "Rusty Axe"),
            ("Tattered Cloth Robe", "Tattered Cloth Belt"),
            ("Tattered Rawhide Vest", "Tattered Rawhide Mask"),
            ("Night Harbor (West)", "Night Harbor (East)"),
            ("Gozif", "Gnzf"),
        ]:
            self.assertFalse(close_spellings(fold(a), fold(b)), f"{a} !~ {b}")


class VocabularyTests(unittest.TestCase):
    def test_rare_reading_maps_to_the_frequent_one(self) -> None:
        v = Vocabulary()
        for _ in range(12):
            v.observe("item", "Bone Chips")
        v.observe("item", "Bone Ohips")
        self.assertEqual(v.canonical("item", "Bone Ohips"), "Bone Chips")
        self.assertEqual(v.canonical("item", "Bone Chips"), "Bone Chips")
        self.assertEqual(v.canonical("item", "Worn Bow"), "Worn Bow", "unknown names pass through")

    def test_common_words_decide_the_spelling(self) -> None:
        v = Vocabulary()
        v.observe("zone", "Tomb of the Last Wvrmsbane", 2)  # the zone line was misread twice...
        v.observe("zone", "Tomb of the Last Wyrmsbane", 1)
        for _ in range(40):  # ...but the word is common elsewhere
            v.observe_text("a Wyrmsbane crusader hits YOU for 4 points of damage.")
        for _ in range(3):
            v.observe_text("You have entered Tomb of the Last Wvrmsbane.")
        self.assertEqual(v.canonical("zone", "Tomb of the Last Wvrmsbane"), "Tomb of the Last Wyrmsbane")

    def test_two_real_names_one_letter_apart_stay_apart(self) -> None:
        v = Vocabulary()
        v.observe("item", "Tattered Cloth Cape", 3)
        v.observe("item", "Tattered Cloth Cap", 2)
        self.assertEqual(v.canonical("item", "Tattered Cloth Cap"), "Tattered Cloth Cap")

    def test_categories_do_not_mix(self) -> None:
        v = Vocabulary()
        v.observe("player", "Povebizu", 20)
        v.observe("item", "Povebizc")
        self.assertEqual(v.canonical("item", "Povebizc"), "Povebizc")

    def test_save_and_load(self) -> None:
        v = Vocabulary()
        v.observe("player", "Dogabetarolem", 30)
        v.observe_text("Dogabetarolem crushes a skeletal warrior for 8 points of damage.")
        v.observe_text("Dogabetarolem crushes a skeletal warrior for 8 points of damage.")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vocabulary.json"
            v.save(path)
            loaded = Vocabulary()
            self.assertTrue(loaded.load(path))
        loaded.observe("player", "Dogabetarozem")
        self.assertEqual(loaded.canonical("player", "Dogabetarozem"), "Dogabetarolem")


class VocabularySpeedTests(unittest.TestCase):
    """Rebuilding a mapping compares only new names (it ran on every snapshot of a fight)."""

    @staticmethod
    def _vocab() -> Vocabulary:
        v = Vocabulary()
        base = ["Brannoc", "Tamsin", "Wenna", "Corvath", "Ilsabet", "Morwen", "Dravik", "Holloway",
                "Quillon", "Sabeth", "Torvald", "Ysolde", "Kestrel", "Fennick", "Gorran"]
        for i, name in enumerate(base):
            v.observe("player", name, 40 + i)
            v.observe("player", name[:-1] + "l", 2)  # OCR misreads
            v.observe("player", name[1:], 1)  # clipped first letter
            v.observe_text(f"{name} hits a rat for 3 points of damage.")
        return v

    def test_neighbour_clustering_matches_comparing_every_pair(self) -> None:
        v = self._vocab()
        counts = dict(v._names["player"])
        pairwise = Vocabulary._build(counts, v._words)
        self.assertEqual(v._mapping("player"), pairwise)
        self.assertEqual(v.canonical("player", "Brannol"), "Brannoc")
        self.assertEqual(v.canonical("player", "amsin"), "Tamsin")

    def test_rebuild_after_new_sightings_compares_no_old_pairs(self) -> None:
        from unittest import mock

        from mnmparse import vocab as vocab_module

        v = self._vocab()
        v._mapping("player")
        v.observe("player", "Tamsin", 3)  # counts change, no new spelling
        v.observe_text("Tamsin heals Wenna for 20 Health.")
        with mock.patch.object(vocab_module, "close_spellings", wraps=vocab_module.close_spellings) as spy:
            v._mapping("player")
            self.assertEqual(spy.call_count, 0, "known names are never compared again")
            v.observe("player", "Tamsln", 1)  # one new spelling: compared with the known names once
            v._mapping("player")
            self.assertEqual(spy.call_count, len(v._names["player"]) - 1)
        self.assertEqual(v.canonical("player", "Tamsln"), "Tamsin")


class ReplayFilterTests(unittest.TestCase):
    BLOCK = [
        "a skeletal fighter slashes YOU for 6 points of damage.",
        "You try to attack, but you must face your target.",
        "a skeletal fighter's Strike hits YOU for 2 points of damage!",
        "Dogabetarolem has been slain by a skeletal warrior!",
        "Gozif crushes a skeletal warrior for 9 points of damage.",
        "a skeletal warrior hits Gozif for 4 points of damage.",
    ]

    def test_a_reshown_block_is_found(self) -> None:
        items = [(float(i), text) for i, text in enumerate(self.BLOCK)]
        items += [(30.0, "Gozif begins casting Heal.")]
        replay = ["a skeletal fizhter slashes YOU for 6 points of damage."] + self.BLOCK[1:]  # first line misread
        items += [(72.0, text) for text in replay]  # one screen, read at once
        self.assertEqual(find_replays(items), set(range(7, 13)))

    def test_repeated_lines_spread_over_time_are_kept(self) -> None:
        # The same rotation twice, but line by line over seconds: a real second fight.
        items = [(float(i * 3), text) for i, text in enumerate(self.BLOCK * 2)]
        self.assertEqual(find_replays(items), set())

    def test_spam_of_one_line_is_kept(self) -> None:
        line = "You try to attack, but you must face your target."
        items = [(0.0, line), (1.0, line), (2.0, line), (40.0, line), (40.0, line), (40.0, line)]
        self.assertEqual(find_replays(items), set(), "a run needs two different lines")


if __name__ == "__main__":
    unittest.main()
