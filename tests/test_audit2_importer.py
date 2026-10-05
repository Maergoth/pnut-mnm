"""Second audit: importing logs (mnmparse.importer) and finding re-read lines (mnmparse.replay).

The replay filter now runs only on logs written before the tracker recognised a re-shown
screen itself (no format header, started before 2026-10-02 16:44), and there it drops a run
only when it looks like a screen read in one go.  The lines the game naturally repeats (a
kill, "Stopped attacking.", party experience; a group heal; a proc chain) are kept.
:func:`import_files` imports a stretch of play: the party roster goes on from file to file,
and the lines a restarted app logged a second time at the top of the next file are left out.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from mnmparse.importer import (
    REPLAY_AWARE_SINCE,
    import_file,
    import_files,
    iter_jsonl_events,
    iter_log_events,
)
from mnmparse.logwriter import (
    LOG_FORMAT,
    LogWriter,
    events_header,
    file_format,
    is_header,
    raw_header,
)
from mnmparse.parser import parse_line
from mnmparse.replay import MIN_RUN, find_backlog, find_replays

VIEWER = "Kebatu"
#: Before the cutoff: the replay filter applies (when the file has no header).
T_OLD = time.mktime((2026, 10, 1, 20, 0, 0, 0, 0, -1))
#: After it: logs are taken as they are.
T_NEW = time.mktime((2026, 10, 3, 2, 0, 0, 0, 0, -1))


@dataclass
class Msg:
    text: str
    first_seen: float
    frames_seen: int = 2
    fragment: bool = False


def _stamp(ts: float) -> str:
    return time.strftime("[%a %b %d %H:%M:%S %Y]", time.localtime(ts))


def _write_log(path: Path, lines: list[tuple[float, str]], *, header: bool = False) -> Path:
    """A raw log as an older version wrote it (no header), or with today's header."""
    body = "".join(f"{_stamp(ts)} {text}\n" for ts, text in lines)
    path.write_text((raw_header() + "\n" if header else "") + body, encoding="utf-8")
    return path


def _you(result, attr: str) -> int:
    """``attr`` of the viewer's rows, summed over the result's fights."""
    return sum(getattr(r, attr) for s in result.encounters for r in s.rows if r.is_you)


FIGHT_END = ["You have slain a caiman!", "Stopped attacking.", "You gain party experience!"]

#: A group heal: the same spell heals every member at once, with the same amount.
SMITE = [
    "Your Restorative Smite hits a crocodile for 9 points of damage.",
    "Your Restorative Smite heals you for 10 Health.",
    "Your Restorative Smite heals Pidef for 10 Health.",
    "Your Restorative Smite heals Tovozen for 10 Health.",
    "Your Restorative Smite heals Wululiso for 10 Health.",
    "Your Restorative Smite heals Gozif for 10 Health.",
    "Your Restorative Smite heals Palidu for 10 Health.",
]

#: Eight lines of a fight, one a second; a re-shown screen prints them again in one go.
SCREEN = [
    "You slash a skeletal fighter for 9 points of damage.",
    "a skeletal fighter slashes YOU for 6 points of damage.",
    "Pidef crushes a skeletal fighter for 14 points of damage.",
    "You try to slash a skeletal fighter, but miss!",
    "Your Crusader Strike hits a skeletal fighter for 12 points of damage.",
    "a skeletal fighter's Strike hits YOU for 2 points of damage!",
    "Tovozen pierces a skeletal fighter for 5 points of damage.",
    "You slash a skeletal fighter for 3 points of damage.",
]


class ReplayFilterTests(unittest.TestCase):
    """Old logs (no header, before the cutoff): only screens read in one go are dropped."""

    def test_a_kill_that_recurs_is_kept(self) -> None:
        # Two caimans 17 s apart: the second kill, "Stopped attacking." and the experience
        # line are the first ones word for word, and must all count.
        lines: list[tuple[float, str]] = []
        for t, hit in ((T_OLD, 9), (T_OLD + 17, 14)):
            lines += [
                (t, "Starting to attack."),
                (t + 1, f"You slash a caiman for {hit} points of damage."),
                (t + 2, "a caiman bites YOU for 4 points of damage."),
                (t + 3, f"You slash a caiman for {hit - 2} points of damage."),
            ]
            lines += [(t + 4, text) for text in FIGHT_END]
        with tempfile.TemporaryDirectory() as tmp:
            result = import_file(_write_log(Path(tmp) / "combat_old.log", lines), player_name=VIEWER)
        self.assertEqual(result.replays_dropped, 0)
        self.assertEqual(result.session.kills, 2)
        self.assertEqual([s.killed for s in result.encounters], [["a caiman"], ["a caiman"]])

    def test_identical_runs_that_arrive_at_game_pace_are_kept(self) -> None:
        # Copy and original both arrive within one second: the copy is not squeezed.
        run = ["You slash a caiman for 9 points of damage."] + FIGHT_END + SMITE[:3]
        items = [(0.0, text) for text in run] + [(17.0, text) for text in run]
        self.assertGreaterEqual(len(run), MIN_RUN)
        self.assertEqual(find_replays(items), set())

    def test_a_group_heal_that_recurs_is_kept(self) -> None:
        lines = [(T_OLD, text) for text in SMITE] + [(T_OLD + 40, text) for text in SMITE]
        with tempfile.TemporaryDirectory() as tmp:
            result = import_file(_write_log(Path(tmp) / "combat_old.log", lines), player_name=VIEWER)
        self.assertEqual(result.replays_dropped, 0)
        self.assertEqual(result.messages, 2 * len(SMITE))
        self.assertEqual(_you(result, "heals"), 2 * 6 * 10, "both casts healed six players for 10")

    def test_a_squeezed_reread_is_still_dropped(self) -> None:
        lines = [(T_OLD - 2, "Pidef has joined the party."),
                 (T_OLD - 1, "Tovozen has joined the party.")]
        lines += [(T_OLD + i, text) for i, text in enumerate(SCREEN)]
        lines += [(T_OLD + 27, text) for text in SCREEN]  # the same screen again, read at once
        with tempfile.TemporaryDirectory() as tmp:
            result = import_file(_write_log(Path(tmp) / "combat_old.log", lines), player_name=VIEWER)
        self.assertEqual(result.replays_dropped, len(SCREEN))
        self.assertEqual(result.messages, len(SCREEN) + 2)
        self.assertEqual(len(result.encounters), 1, "no second fight from the copy")
        self.assertEqual(result.encounters[0].total_damage, 9 + 14 + 12 + 5 + 3)

    def test_numbers_are_compared_word_by_word(self) -> None:
        # "(Block 1 1)" and "(Block 11)" carry different numbers, though their digits run
        # together the same: the run breaks there and what is left is too short.
        original = list(SCREEN[:6])
        original[2] = "a skeletal fighter hits YOU for 5 points of damage. (Block 1 1)"
        copy = list(original)
        copy[2] = "a skeletal fighter hits YOU for 5 points of damage. (Block 11)"
        items = [(float(i), text) for i, text in enumerate(original)]
        self.assertEqual(find_replays(items + [(30.0, text) for text in copy]), set())
        self.assertEqual(find_replays(items + [(30.0, text) for text in original]), set(range(6, 12)))

    def test_kill_lines_must_match_exactly(self) -> None:
        original = SCREEN[:5] + ["You have slain a jackal pup!"]
        items = [(float(i), text) for i, text in enumerate(original)]
        fuzzy = SCREEN[:5] + ["You have slain a jackal!"]  # close, but another kill
        self.assertEqual(find_replays(items + [(30.0, text) for text in fuzzy]), set())
        self.assertEqual(find_replays(items + [(30.0, text) for text in original]), set(range(6, 12)))

    def test_the_original_must_be_close(self) -> None:
        screen = [(float(i), text) for i, text in enumerate(SCREEN)]
        filler = [(10.0 + 2 * i, f"Gozif's Torment hits a large rat for {i + 1} points of damage.") for i in range(50)]
        far = screen + filler + [(120.0, text) for text in SCREEN]  # 50 lines and 100 s later
        self.assertEqual(find_replays(far), set())
        near = screen + filler[:10] + [(40.0, text) for text in SCREEN]
        self.assertEqual(find_replays(near), set(range(18, 18 + len(SCREEN))))

    def test_dropped_lines_still_teach_the_roster(self) -> None:
        # The party disbands after the original screen. A genuine rejoin can match an
        # older screen, so explicit membership lines in dropped copies still teach it.
        original = [
            "You slash a skeletal fighter for 9 points of damage.",
            "Pidef has joined the party.",
            "a skeletal fighter slashes YOU for 6 points of damage.",
            "Tovozen has joined the party.",
            "Pidef crushes a skeletal fighter for 14 points of damage.",
            "Tovozen pierces a skeletal fighter for 5 points of damage.",
        ]
        lines = [(T_OLD + 2 * i, text) for i, text in enumerate(original)]
        lines += [(T_OLD + 12, "Your party has been disbanded.")]
        lines += [(T_OLD + 30, text) for text in original]
        with tempfile.TemporaryDirectory() as tmp:
            result = import_file(_write_log(Path(tmp) / "combat_old.log", lines), player_name=VIEWER)
        self.assertEqual(result.replays_dropped, len(original))
        self.assertEqual(result.messages, len(original) + 1)
        self.assertEqual(result.stats.roster.members(), {"Pidef", "Tovozen"})


class FormatHeaderTests(unittest.TestCase):
    def _write(self, tmp: str, t0: float, lines: list[str], *, spacing: float = 1.0) -> LogWriter:
        with LogWriter(tmp) as w:
            for i, text in enumerate(lines):
                w.write_raw(Msg(text, t0 + i * spacing))
                w.write_event(parse_line(text, t0 + i * spacing, VIEWER))
        return w

    def test_new_files_start_with_a_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = self._write(tmp, T_NEW, SCREEN[:2])
            raw = w.raw_path.read_text(encoding="utf-8").splitlines()
            events = w.events_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(raw[0], raw_header())
            self.assertEqual(events[0], events_header())
            self.assertEqual(json.loads(events[0]), {"mnmparse": "events", "format": LOG_FORMAT})
            self.assertEqual((len(raw), len(events)), (3, 3))
            self.assertTrue(is_header(raw[0]) and is_header(events[0]))
            self.assertFalse(is_header(raw[1]) or is_header(events[1]))
            self.assertEqual(file_format(w.raw_path), LOG_FORMAT)
            self.assertEqual(file_format(w.events_path), LOG_FORMAT)

    def test_appending_to_a_file_adds_no_second_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, T_NEW, SCREEN[:1])
            w = self._write(tmp, T_NEW, SCREEN[1:2])  # same second: the same file names
            raw = w.raw_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(is_header(line) for line in raw), 1)
            self.assertEqual(len(raw), 3)

    def test_files_without_a_header_are_format_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_log(Path(tmp) / "combat_old.log", [(T_OLD, SCREEN[0])])
            self.assertEqual(file_format(path), 1)
            self.assertEqual(file_format(Path(tmp) / "missing.log"), 1)

    def test_readers_skip_the_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            w = self._write(tmp, T_NEW, SCREEN)
            for path in (w.raw_path, w.events_path):
                result = import_file(path, player_name=VIEWER)
                self.assertEqual(result.messages, len(SCREEN), path.name)
                self.assertNotIn("unknown", result.counts, path.name)
            self.assertEqual([ev.text for ev in iter_log_events(w.raw_path, VIEWER)], SCREEN)
            self.assertEqual([ev.text for ev in iter_jsonl_events(w.events_path)], SCREEN)

    def test_the_header_turns_the_replay_filter_off(self) -> None:
        # Dated before the cutoff, but written by a tracker that suppresses re-shown screens:
        # the same lines twice are two real sets of lines.
        lines = SCREEN + SCREEN
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp) as w:
                for i, text in enumerate(lines):
                    w.write_raw(Msg(text, T_OLD + (i if i < len(SCREEN) else 27)))
            result = import_file(w.raw_path, player_name=VIEWER)
        self.assertEqual(result.replays_dropped, 0)
        self.assertEqual(result.messages, 2 * len(SCREEN))

    def test_logs_started_after_the_cutoff_are_not_filtered(self) -> None:
        self.assertLess(T_OLD, REPLAY_AWARE_SINCE)
        self.assertGreater(T_NEW, REPLAY_AWARE_SINCE)
        lines = [(T_NEW + i, text) for i, text in enumerate(SCREEN)] + [(T_NEW + 27, text) for text in SCREEN]
        with tempfile.TemporaryDirectory() as tmp:
            result = import_file(_write_log(Path(tmp) / "combat_new.log", lines), player_name=VIEWER)
        self.assertEqual(result.replays_dropped, 0)
        self.assertEqual(result.messages, 2 * len(SCREEN))


#: The end of a run of the app: a fight still going when it stopped.
TAIL = [
    "Starting to attack.",
    "You slash a crocodile for 11 points of damage.",
    "a crocodile bites YOU for 7 points of damage.",
    "Pidef crushes a crocodile for 6 points of damage.",
    "Your Crusader Strike hits a crocodile for 13 points of damage.",
    "Tovozen pierces a crocodile for 4 points of damage.",
    "a crocodile tries to bite YOU, but misses!",
    "You slash a crocodile for 8 points of damage.",
]
#: What happened while the app was down: on screen at the restart, below the old lines.
GAP = [
    "Pidef crushes a crocodile for 9 points of damage.",
    "Your party member Pidef has slain a crocodile!",
    "Stopped attacking.",
]


class ImportFilesTests(unittest.TestCase):
    def _session(self, tmp: str, restart_after: float) -> tuple[Path, Path]:
        """Run A, then run B ``restart_after`` seconds later, opening with A's last six lines."""
        first = [(T_NEW - 2, "Pidef has joined the party."),
                 (T_NEW - 1, "Tovozen has joined the party.")]
        first += [(T_NEW + i, text) for i, text in enumerate(TAIL)]
        start = T_NEW + len(TAIL) - 1 + restart_after
        second = [(start, text) for text in TAIL[-6:] + GAP]  # the first frame, all at once
        second += [(start + 20, "You have entered Night Harbor (East).")]
        a = _write_log(Path(tmp) / "combat_a.log", first, header=True)
        b = _write_log(Path(tmp) / "combat_b.log", second, header=True)
        return a, b

    def test_a_restart_backlog_is_dropped_and_new_lines_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._session(tmp, restart_after=4.0)
            results = import_files([b, a], player_name=VIEWER, keep_events=True)
            alone = import_file(b, player_name=VIEWER)
        self.assertEqual([r.path.name for r in results], ["combat_a.log", "combat_b.log"], "time order")
        first, second = results
        self.assertEqual((first.backlog_dropped, second.backlog_dropped), (0, 6))
        self.assertEqual([ev.text for ev in second.events], GAP + ["You have entered Night Harbor (East)."])
        self.assertEqual(second.session.kills, 1, "the kill that came during the restart counts")
        damage = sum(s.total_damage for r in results for s in r.encounters)
        self.assertEqual(damage, 11 + 6 + 13 + 4 + 8 + 9, "every hit once")
        self.assertEqual(alone.backlog_dropped, 0, "import_file knows no previous file")
        self.assertEqual(alone.messages, 6 + len(GAP) + 1)

    def test_no_backlog_after_a_long_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._session(tmp, restart_after=11 * 60.0)
            results = import_files([a, b], player_name=VIEWER)
        self.assertEqual(results[1].backlog_dropped, 0)

    def test_find_backlog_needs_the_lines_to_line_up(self) -> None:
        opening = [(0.0, text) for text in TAIL[-6:] + GAP]
        self.assertEqual(find_backlog(TAIL, opening), set(range(6)))
        shuffled = [(0.0, text) for text in [TAIL[-1], TAIL[-3], TAIL[-5], TAIL[-2], TAIL[-4]] + GAP]
        self.assertLess(len(find_backlog(TAIL, shuffled)), 3)
        spam = ["This ability is not available right now."] * 5
        self.assertEqual(find_backlog(spam, [(0.0, text) for text in spam]), set(), "one line said over and over")

    def _party_session(self, tmp: str, gap: float) -> tuple[Path, Path]:
        first = [
            (T_NEW, "Pidef has joined the party."),
            (T_NEW + 1, "You slash a large rat for 4 points of damage."),
            (T_NEW + 2, "You have slain a large rat!"),
        ]
        t = T_NEW + 2 + gap
        second = [
            (t, "You slash a crocodile for 11 points of damage."),
            (t + 1, "Pidef crushes a crocodile for 6 points of damage."),
            (t + 2, "Gozif pierces a crocodile for 5 points of damage."),
        ]
        a = _write_log(Path(tmp) / "combat_a.log", first, header=True)
        b = _write_log(Path(tmp) / "combat_b.log", second, header=True)
        return a, b

    @staticmethod
    def _sides(result) -> dict[str, bool]:
        return {r.name: r.in_group for r in result.encounters[0].rows if not r.is_npc}

    def test_the_roster_goes_on_from_file_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._party_session(tmp, gap=300.0)
            results = import_files([a, b], player_name=VIEWER)
            alone = import_file(b, player_name=VIEWER)
        self.assertIs(results[0].stats.roster, results[1].stats.roster)
        self.assertEqual(self._sides(results[1]), {VIEWER: True, "Pidef": True, "Gozif": False})
        self.assertEqual(self._sides(alone), {VIEWER: True, "Pidef": False, "Gozif": False},
                         "no party evidence in this file: only the viewer counts")

    def test_the_roster_starts_over_after_hours(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = self._party_session(tmp, gap=7 * 3600.0)
            results = import_files([a, b], player_name=VIEWER)
        self.assertEqual(self._sides(results[1]), {VIEWER: True, "Pidef": False, "Gozif": False})

    def test_a_missing_file_fails_before_anything_is_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, _b = self._party_session(tmp, gap=60.0)
            with self.assertRaises(FileNotFoundError):
                import_files([a, Path(tmp) / "missing.log"])


class SessionTests(unittest.TestCase):
    def _import(self, lines: list[tuple[float, str]]):
        with tempfile.TemporaryDirectory() as tmp:
            return import_file(_write_log(Path(tmp) / "combat_new.log", lines, header=True), player_name=VIEWER)

    def test_other_groups_fights_are_not_time_in_combat(self) -> None:
        result = self._import([
            (T_NEW, "You slash a crocodile for 11 points of damage."),
            (T_NEW + 4, "You slash a crocodile for 6 points of damage."),
            (T_NEW + 40, "Gozif slashes a large rat for 5 points of damage."),
            (T_NEW + 43, "Gozif slashes a large rat for 2 points of damage."),
        ])
        self.assertEqual([s.ours for s in result.encounters], [True, False])
        self.assertEqual(result.session.encounters, 1)
        self.assertAlmostEqual(result.session.combat_seconds, result.encounters[0].duration)

    def test_the_viewer_is_never_a_party_member(self) -> None:
        result = self._import([
            (T_NEW, "Tovozen has joined the party."),
            (T_NEW + 1, "--You loot [Bone Chips] from a crocodile's corpse.--"),
            (T_NEW + 2, "You loot 3 copper coins from a crocodile's corpse."),
            (T_NEW + 3, "Pidef loots 5 copper coins from a crocodile's corpse."),
        ])
        self.assertEqual(result.stats.player_name, VIEWER)
        self.assertEqual(result.stats.roster.members(), {"Tovozen"})
        self.assertNotIn("Pidef", result.session.party, "plain loot does not establish membership")
        self.assertNotIn(VIEWER, result.session.party)
        self.assertIn("Tovozen", result.session.party, "the session takes the party from the same roster")

    def test_a_wrapped_coin_split_is_joined(self) -> None:
        result = self._import([
            (T_NEW, "Pidef loots 43 copper coins from a dunes madman's corpse, and you receive"),
            (T_NEW, "22 copper coins from a dunes madman's corpse as your split."),
            (T_NEW + 30, "Pidef loots 1 copper coin from a rotting skeleton's corpse, and you receive"),
            (T_NEW + 30, "O coins from a rotting skeleton's corpse as your split."),
        ])
        self.assertEqual(result.session.coin_split, 22)
        self.assertNotIn("unknown", result.counts)


if __name__ == "__main__":
    unittest.main()
