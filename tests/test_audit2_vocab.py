"""Learned spellings, second audit: names only from combat lines, clipped NPC words, and
:meth:`Vocabulary.distinct` (the veto against folding two different mobs together)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mnmparse import vocab as vocab_module
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.vocab import NAME_KINDS, Vocabulary, close_spellings, could_be_name, fold, observe_event


def _names(v: Vocabulary) -> dict[str, dict[str, int]]:
    return {cat: dict(v._names[cat]) for cat in ("player", "npc")}


class NamesFromCombatLinesOnlyTests(unittest.TestCase):
    """#30 / #8: status, /con, personal and unparsed lines teach no player or NPC names."""

    def test_non_combat_lines_teach_words_but_no_names(self) -> None:
        v = Vocabulary()
        for text in [
            "The call to arms fades.",
            "This ability is not available right now.",
            "Beginning to memorize Cudgel of Light...",
            "Your faction standing with Easthymn Freebooters got better.",
            "Your faction standing with Denizens of Wyrmsbane Tomb got worse.",
            "a caiman views you as a threat It might prove to be a diffcult battle.",
            "a crocodile seems indifferent to your presence Would you like to die?",
            "Tovozen Battle with them would be quite risky.",
            "a skeletal vicar is protected by holy focus.",
            "Someone is already looting that corpse.",
            "Hemobiru begins to sneak.",
        ]:
            observe_event(v, parse_line(text, 0.0, "Pidef"))
        self.assertEqual(_names(v), {"player": {}, "npc": {}})
        self.assertGreater(v.word_count("Freebooters"), 0, "the words still count as spelling evidence")

    def test_every_kind_outside_the_combat_kinds_is_ignored(self) -> None:
        v = Vocabulary()
        for kind in ("status", "consider", "personal", "unknown", "craft", "zone", "experience",
                     "coin_split", "reward", "some_future_kind"):
            self.assertNotIn(kind, NAME_KINDS)
            observe_event(v, Event(ts=0.0, kind=kind, text="", actor="Tovozen", target="a skeletal warrior"))
        self.assertEqual(_names(v), {"player": {}, "npc": {}})

    def test_combat_lines_still_teach_names(self) -> None:
        v = Vocabulary()
        for text in [
            "Tovozen slashes a skeletal warrior for 5 points of damage.",
            "a skeletal warrior tries to bite Wululiso, but misses!",
            "Gozif's Minor Heal heals Wululiso for 12 Health.",
            "Palidu begins casting Distress.",
            "a skeletal warrior has been slain by Tovozen!",
            "--Hemobiru loots [Bone Chips] from a skeletal marksman's corpse.--",
            "a skeletal defender is mesmerized.",
            "a skeletal knight looks angry at Wululiso.",
        ]:
            observe_event(v, parse_line(text, 0.0, "Pidef"))
        names = _names(v)
        self.assertEqual(set(names["player"]), {"Tovozen", "Wululiso", "Gozif", "Palidu", "Hemobiru"})
        self.assertEqual(
            set(names["npc"]),
            {"a skeletal warrior", "a skeletal marksman", "a skeletal defender", "a skeletal knight"},
        )
        self.assertEqual(dict(v._names["item"]), {"Bone Chips": 1})
        self.assertIn("Minor Heal", v._names["skill"])

    def test_a_combat_line_with_an_impossible_name_teaches_nothing(self) -> None:
        v = Vocabulary()
        # a kill line that swallowed the next message
        observe_event(v, Event(ts=0.0, kind="kill", text="", actor="Tovozen",
                               target="a skeletal warriorl You gain party experience"))
        self.assertEqual(_names(v), {"player": {"Tovozen": 1}, "npc": {}})


class CouldBeNameTests(unittest.TestCase):
    def test_names(self) -> None:
        for category, name in [
            ("player", "Tovozen"),
            ("player", "Toilmaster Verith"),
            ("player", "Theodric"),  # starts like "The", is not "The"
            ("npc", "a skeletal warrior"),
            ("npc", "a Wyrmsbane crusader"),
            ("npc", "an archaeologist"),
            ("npc", "the wisdom keeper"),
            ("item", "Bone Chips"),  # other categories are not checked
            ("zone", "Night Harbor (East)"),
        ]:
            self.assertTrue(could_be_name(category, name), f"{category}: {name}")

    def test_non_names(self) -> None:
        for category, name in [
            ("player", "Denizens of Wyrmsbane Tomb"),  # the grammar never reads a lowercase word in a name
            ("player", "Your Blind"),
            ("player", "Your Crusader Strike"),
            ("player", "Tovozen You"),
            ("player", "The"),
            ("player", "This"),
            ("player", "Beginning"),
            ("player", "Someone"),
            ("player", ""),
            ("npc", "a skeletal priest is"),
            ("npc", "a skeletal vicar is protected by holy"),
            ("npc", "a caiman views you"),
            ("npc", "Tovozen"),  # not the NPC shape
        ]:
            self.assertFalse(could_be_name(category, name), f"{category}: {name}")


class LoadDropsNonNamesTests(unittest.TestCase):
    PAYLOAD = {
        "version": 1,
        "names": {
            "player": {"Tovozen": 40, "The": 289, "Beginning": 40, "Denizens of Wyrmsbane Tomb": 289,
                       "Your Lesser Pacify": 9, "Easthymn Freebooters": 289},
            "npc": {"a skeletal warrior": 500, "a skeletal priest is": 4, "a caiman views you": 2},
            "item": {"Bone Chips": 12},
        },
        "words": {"freebooters": 289, "tovozen": 40},
    }

    def test_merge_drops_saved_entries_that_cannot_be_names(self) -> None:
        v = Vocabulary()
        with self.assertLogs("mnmparse.vocab", "INFO") as logs:
            v.merge_dict(self.PAYLOAD)
        names = _names(v)
        # "Easthymn Freebooters" (a faction) is shaped like a two-word NPC name: nothing in
        # the file tells it apart, so it stays; it is no longer learned (see above).
        self.assertEqual(names["player"], {"Tovozen": 40, "Easthymn Freebooters": 289})
        self.assertEqual(names["npc"], {"a skeletal warrior": 500})
        self.assertEqual(dict(v._names["item"]), {"Bone Chips": 12})
        self.assertEqual(v.word_count("freebooters"), 289, "words are kept")
        self.assertIn("dropped 6 saved entries", "\n".join(logs.output))

    def test_load_and_save_round_trip_without_the_junk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "vocabulary.json"
            path.write_text(json.dumps(self.PAYLOAD), encoding="utf-8")
            v = Vocabulary()
            self.assertTrue(v.load(path))
            v.save(path)
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("The", saved["names"]["player"])
        self.assertNotIn("a caiman views you", saved["names"]["npc"])
        self.assertEqual(saved["names"]["npc"], {"a skeletal warrior": 500})


class ClippedNpcWordTests(unittest.TestCase):
    """#30: the word after an NPC's article may lose its leading letters too."""

    def test_clipped_word_after_the_article_is_close(self) -> None:
        for a, b in [
            ("a eletal warrior", "a skeletal warrior"),
            ("a keletal monk", "a skeletal monk"),
            ("a eletal monk", "a skeletal monk"),
            ("a mbie laborer", "a zombie laborer"),
            ("the eletal warrior", "the skeletal warrior"),
        ]:
            self.assertTrue(close_spellings(fold(a), fold(b)), f"{a} ~ {b}")

    def test_within_the_edit_limits(self) -> None:
        for a, b in [
            ("a letal warrior", "a skeletal warrior"),  # three letters lost: over the limit
            ("a rat", "a brat"),  # short words still must match
            ("a ant", "a giant"),
            ("an eletal warrior", "a skeletal warrior"),  # the article must match
            ("a skeletal fighter", "a skeletal cleric"),
        ]:
            self.assertFalse(close_spellings(fold(a), fold(b)), f"{a} !~ {b}")

    def test_the_clipped_reading_maps_to_the_full_name(self) -> None:
        v = Vocabulary()
        v.observe("npc", "a skeletal warrior", 300)
        v.observe("npc", "a eletal warrior", 6)
        v.observe("npc", "a keletal monk", 3)
        v.observe("npc", "a skeletal monk", 90)
        self.assertEqual(v.canonical("npc", "a eletal warrior"), "a skeletal warrior")
        self.assertEqual(v.canonical("npc", "a keletal monk"), "a skeletal monk")


class DistinctTests(unittest.TestCase):
    """#27 contract: the vocabulary keeps two names apart, both well attested."""

    @staticmethod
    def _vocab() -> Vocabulary:
        v = Vocabulary()
        for name, n in [
            ("a skeletal fighter", 959), ("a skeletal fizhter", 38),
            ("a skeletal cleric", 1181),
            ("a skeletal warrior", 6667), ("a eletal warrior", 6),
            ("a jackal", 12), ("a jackal pup", 39),
            ("a crocodile", 4685), ("a crocodile hatchling", 39),
            ("a skeletal", 17),  # cut-off readings of the skeletal mobs
            ("a risen zombie", 155), ("a risen officer", 9),
        ]:
            v.observe("npc", name, n)
        for name, n in [("Tovozen", 14), ("Tovozan", 12), ("Wululiso", 80), ("Gozif", 3)]:
            v.observe("player", name, n)
        return v

    def test_different_mobs_are_distinct(self) -> None:
        v = self._vocab()
        for a, b in [
            ("a skeletal fighter", "a skeletal cleric"),
            ("a jackal", "a jackal pup"),
            ("a jackal pup", "a jackal"),
            ("a crocodile", "a crocodile hatchling"),
            ("a skeletal fizhter", "a skeletal cleric"),  # a variant of the fighter
            ("a skeletal warrior", "a skeletal fighter"),
        ]:
            self.assertTrue(v.distinct("npc", a, b), f"{a} / {b}")

    def test_variants_unknown_rare_and_cut_off_names_are_not(self) -> None:
        v = self._vocab()
        for a, b in [
            ("a eletal warrior", "a skeletal warrior"),  # a clipped reading of it
            ("a skeletal fizhter", "a skeletal fighter"),
            ("a skeletal fighter", "a skeletal fighter"),
            ("a skeletal warrior", "a skeletal knight"),  # never seen
            ("a risen zombie", "a risen officer"),  # 9 readings: too few to tell
            ("a skeletal", "a skeletal warrior"),  # cut off: begins far more common names
            ("a skeletal cleric", "a skeletal"),
            ("a jackal", ""),
        ]:
            self.assertFalse(v.distinct("npc", a, b), f"{a} / {b}")

    def test_close_spellings_without_a_winner_are_not_distinct(self) -> None:
        v = self._vocab()
        # one letter apart and similar counts: the vocabulary cannot tell which is right
        self.assertNotEqual(v.canonical("player", "Tovozan"), v.canonical("player", "Tovozen"))
        self.assertFalse(v.distinct("player", "Tovozan", "Tovozen"))
        self.assertTrue(v.distinct("player", "Tovozen", "Wululiso"))
        self.assertFalse(v.distinct("player", "Wululiso", "Gozif"), "Gozif was read 3 times")
        self.assertFalse(v.distinct("npc", "Tovozen", "Wululiso"), "categories are kept apart")

    def test_min_count(self) -> None:
        v = self._vocab()
        self.assertFalse(v.distinct("npc", "a jackal", "a jackal pup", min_count=20))
        self.assertTrue(v.distinct("npc", "a risen zombie", "a risen officer", min_count=5))

    def test_a_cut_off_name_counts_against_all_the_names_it_begins(self) -> None:
        v = Vocabulary()
        v.observe("npc", "a crocodile", 600)
        v.observe("npc", "a crocodile hatchling", 4000)
        self.assertTrue(v.distinct("npc", "a crocodile", "a crocodile hatchling"))
        v.observe("npc", "a crocodile matriarch", 30000)
        self.assertFalse(v.distinct("npc", "a crocodile", "a crocodile hatchling"))

    def test_counts_include_the_variants(self) -> None:
        v = Vocabulary()
        v.observe("npc", "a jackal", 6)
        v.observe("npc", "a jackai", 2)  # look-alike: folds into "a jackal"
        v.observe("npc", "a jackal pup", 30)
        self.assertEqual(v.canonical("npc", "a jackai"), "a jackal")
        self.assertTrue(v.distinct("npc", "a jackal", "a jackal pup", min_count=8), "6 + 2 readings")
        self.assertTrue(v.distinct("npc", "a jackai", "a jackal pup", min_count=8))
        self.assertFalse(v.distinct("npc", "a jackal", "a jackal pup", min_count=9))


class DistinctSpeedTests(unittest.TestCase):
    """It is called name pair by name pair while fights are merged."""

    def test_repeated_calls_build_nothing_again(self) -> None:
        v = DistinctTests._vocab()
        v.distinct("npc", "a jackal", "a jackal pup")
        with mock.patch.object(vocab_module, "close_spellings", wraps=vocab_module.close_spellings) as spy, \
                mock.patch.object(Vocabulary, "_build", wraps=Vocabulary._build) as build:
            for _ in range(200):
                v.distinct("npc", "a skeletal fighter", "a skeletal cleric")
                v.distinct("npc", "a jackal", "a jackal pup")
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(build.call_count, 0)

    def test_many_names_stay_cheap(self) -> None:
        v = Vocabulary()
        for i in range(300):
            v.observe("npc", f"a skeletal {chr(97 + i % 26)}{chr(97 + i // 26)}x{chr(97 + i % 7)}ward", 20 + i)
        v.observe("npc", "a skeletal", 15)
        v.distinct("npc", "a skeletal", "a skeletal aaxaward")  # builds the mapping once
        started = time.perf_counter()
        for _ in range(2000):
            v.distinct("npc", "a skeletal", "a skeletal aaxaward")
        self.assertLess(time.perf_counter() - started, 2.0)

    def test_thread_safe_while_observing(self) -> None:
        v = DistinctTests._vocab()
        errors: list[BaseException] = []
        stop = threading.Event()

        def observe() -> None:
            i = 0
            while not stop.is_set():
                v.observe("npc", f"a skeletal knight{'x' * (i % 5)}")
                i += 1

        worker = threading.Thread(target=observe)
        worker.start()
        try:
            for _ in range(300):
                try:
                    self.assertTrue(v.distinct("npc", "a skeletal fighter", "a skeletal cleric"))
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)
                    break
        finally:
            stop.set()
            worker.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
