"""Regression tests for the tracker findings of the second audit.

* #24 / #31: a group spell prints the same block of lines on every cast ("Your
  Restorative Smite heals X for 10 Health." for each party member).  The replay check
  took every repeated block for re-shown old content and dropped it.
* #19: a coin split that wraps after "and you receive" was not joined with its second half.
* #26 / #32: a restarted app emitted the whole visible chat again (saved state, backlog flag).
* #35: consecutive "window jumped" frames emitted duplicates, and rows released after a
  jump or a scroll-back all got one timestamp (held rows, spread estimated times, INFO
  state logs).

Synthetic frames use the geometry of the real window (see ``tests/test_tracker.py``).
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse import tracker as trk  # noqa: E402
from mnmparse.tracker import Message, Tracker, replay_fixture  # noqa: E402

FIXTURE = _ROOT / "tests" / "fixtures" / "burst_ocr.json"


@dataclass
class L:
    """Stand-in for ``ocr.OcrLine``: only ``.x .y .h .text`` are used by the tracker."""

    x: int
    y: int
    h: int
    text: str


ROW_PITCH = 40
TOP_Y = 21
MARGIN_X = 21
LINE_H = 23
ROWS = 12  #: rows the synthetic window shows


def frame(texts: list[str], *, first_row: int = 0) -> list[L]:
    """One clean row per text, top to bottom, starting at window row ``first_row``."""
    return [L(MARGIN_X, TOP_Y + ROW_PITCH * (first_row + i), LINE_H, t) for i, t in enumerate(texts)]


def texts(msgs: list[Message]) -> list[str]:
    return [m.text for m in msgs]


class Chat:
    """A synthetic chat log scrolling through a 12-row window."""

    def __init__(self, tracker: Tracker | None = None, lines: list[str] | None = None, now: float = 0.0) -> None:
        self.t = tracker if tracker is not None else Tracker()
        self.lines: list[str] = list(lines or [])
        self.now = now
        self.out: list[Message] = []

    def feed(self, rows: list[L], frames: int = 1) -> list[Message]:
        got: list[Message] = []
        for _ in range(frames):
            got.extend(self.t.update(rows, self.now))
            self.now += 0.25
        self.out.extend(got)
        return got

    def view(self, end: int | None = None, frames: int = 2) -> list[Message]:
        """Show the window ending at line ``end`` (the bottom of the chat by default)."""
        end = len(self.lines) if end is None else end
        return self.feed(frame(self.lines[max(0, end - ROWS) : end]), frames)

    def add(self, *new: str, frames: int = 2) -> list[Message]:
        """New lines arrive at the bottom (all in one frame)."""
        self.lines.extend(new)
        return self.view(frames=frames)

    def scroll_in(self, lines: list[str]) -> None:
        """A full first window, then the other ``lines`` one per frame pair."""
        self.lines = list(lines[:ROWS])
        self.view()
        for line in lines[ROWS:]:
            self.add(line)

    def finish(self) -> list[Message]:
        self.out.extend(self.t.flush(self.now))
        return self.out


def filler(start: int, count: int) -> list[str]:
    """``count`` distinct ordinary combat lines."""
    return [
        f"Tovozen crushes a crocodile for {i} points of damage."
        if i % 2
        else f"Pidef's Backstab hits a crocodile for {i} points of damage."
        for i in range(start, start + count)
    ]


PARTY = ["you", "Pidef", "Tovozen", "Wululiso", "Gozif", "Palidu"]


def smite_hit(amount: int) -> str:
    return f"Your Restorative Smite hits a crocodile for {amount} points of damage."


def smite_heals(order: list[str]) -> list[str]:
    return [f"Your Restorative Smite heals {name} for 10 Health." for name in order]


#: Lines that arrive while the window is out of sync in the tests below.
NEW = [
    "Tovozen begins casting Lesser Heal.",
    "Wululiso's Rend hits a stumbling zombie for 21 points of damage.",
    "You try to crush a stumbling zombie, but miss!",
    "Gozif lets loose an empowering battle cry.",
    "Palidu's Slice hits a stumbling zombie for 3 points of Bleed Damage.",
]


# ======================================================================================
# #24 / #31: repeated group-heal blocks
# ======================================================================================


class GroupHealBlockTests(unittest.TestCase):
    def run_two_blocks(self, second_order: list[str], *, hits_first: bool) -> Chat:
        chat = Chat()
        chat.scroll_in(filler(1, 30))
        chat.add(smite_hit(9), *smite_heals(PARTY))
        for line in filler(40, 20):
            chat.add(line)
        backstab = "Gozif's Backstab hits a crocodile for 23 points of damage."
        if hits_first:
            # The hit row shows one frame before its heals: the row directly above the
            # heal burst is then not emitted yet.
            chat.add(smite_hit(12), frames=1)
            chat.add(*smite_heals(second_order), backstab)
        else:
            chat.add(smite_hit(12), *smite_heals(second_order), backstab)
        for line in filler(70, 5):
            chat.add(line)
        chat.finish()
        return chat

    def test_identical_heal_blocks_are_both_emitted(self) -> None:
        chat = self.run_two_blocks(PARTY, hits_first=False)
        self.assertEqual(texts(chat.out), chat.lines[1:], "every line exactly once, in order")
        self.assertEqual(sum("Smite heals" in t for t in texts(chat.out)), 12)
        self.assertEqual(chat.t.replays_suppressed, 0)

    def test_hits_row_one_frame_before_its_heals(self) -> None:
        chat = self.run_two_blocks(PARTY, hits_first=True)
        self.assertEqual(texts(chat.out), chat.lines[1:])
        self.assertEqual(sum("Smite heals" in t for t in texts(chat.out)), 12)

    def test_heal_block_in_another_order(self) -> None:
        for hits_first in (False, True):
            with self.subTest(hits_first=hits_first):
                chat = self.run_two_blocks(PARTY[::-1], hits_first=hits_first)
                self.assertEqual(texts(chat.out), chat.lines[1:])

    def test_heal_blocks_a_few_seconds_apart_with_little_in_between(self) -> None:
        chat = Chat()
        chat.scroll_in(filler(1, 20))
        for cast in range(3):
            chat.add(smite_hit(5 + cast), *smite_heals(PARTY))
            chat.add(f"Palidu slashes a crocodile for {30 + cast} points of damage.")
        chat.finish()
        self.assertEqual(texts(chat.out), chat.lines[1:])
        self.assertEqual(sum("Smite heals" in t for t in texts(chat.out)), 18)


# ======================================================================================
# #24 / #31: real scroll-backs are still recognised
# ======================================================================================


class ScrollBackTests(unittest.TestCase):
    def history(self) -> list[str]:
        return filler(1, 18) + [smite_hit(9), *smite_heals(PARTY)] + filler(30, 16)

    def test_scroll_back_then_wheel_steps_emits_nothing_twice(self) -> None:
        chat = Chat()
        chat.scroll_in(self.history())
        end = len(chat.lines)
        self.assertFalse(chat.t.scrolled_back)
        # Scrolled back by 20 rows (one jump), then down again 5 rows per wheel notch.
        view = end - 20
        self.assertEqual(chat.view(view, frames=3), [])
        self.assertTrue(chat.t.scrolled_back)
        while view < end:
            view = min(end, view + 5)
            self.assertEqual(chat.view(view, frames=3), [], f"wheel step to {view}")
            if view < end:
                self.assertTrue(chat.t.scrolled_back, f"still scrolled back at {view}")
        self.assertFalse(chat.t.scrolled_back, "back at the bottom")
        for line in filler(80, 5):
            chat.add(line)
        chat.finish()
        self.assertEqual(texts(chat.out), chat.lines[1:], "every line exactly once, in order")
        self.assertGreaterEqual(chat.t.replays_suppressed, 20)

    def test_replayed_block_is_suppressed_and_logged(self) -> None:
        # The first row a wheel notch brings back lost its number to OCR, so the row-by-row
        # check cannot follow it; the block check recognises the five rows as one block.
        chat = Chat()
        chat.scroll_in(self.history())
        end = len(chat.lines)
        view = end - 15
        chat.view(view, frames=3)
        shown = chat.lines[view + 5 - ROWS : view + 5]
        shown[-5] = shown[-5].replace(" 31 ", " ")
        self.assertNotEqual(shown[-5], chat.lines[view])
        with self.assertLogs("mnmparse.tracker", level="INFO") as cm:
            self.assertEqual(chat.feed(frame(shown), 3), [])
        logged = [r for r in cm.output if "re-show old content" in r]
        self.assertEqual(len(logged), 1, cm.output)
        self.assertIn("5 new rows", logged[0])
        self.assertIn(repr(chat.lines[view + 1]), logged[0])  # the first two texts are logged
        for v in (view + 10, end):
            self.assertEqual(chat.view(v, frames=3), [])
        chat.finish()
        self.assertEqual(texts(chat.out), chat.lines[1:])

    def test_faded_top_rows_showing_again_are_not_emitted_twice(self) -> None:
        # Idle, the chat fades out; three new lines show on their own (a jump), then the
        # older rows show again above them ("gap" rows).
        chat = Chat()
        chat.scroll_in(filler(1, 20))
        chat.lines += NEW[:3]
        chat.feed(frame(chat.lines[-3:], first_row=ROWS - 3), 3)
        chat.view()
        self.assertGreaterEqual(chat.t.replays_suppressed, ROWS - 3 - 1)
        chat.add("Wululiso begins casting Holy Shield.")
        chat.finish()
        # Every line exactly once.  (The top row of the faded frame is only emitted once
        # it is seen lower down, after the rows below it.)
        self.assertEqual(Counter(texts(chat.out)), Counter(chat.lines[1:]))


# ======================================================================================
# #19: wrapped coin split
# ======================================================================================


class CoinSplitJoinTests(unittest.TestCase):
    def joined(self, head: str, tail: str) -> list[str]:
        chat = Chat()
        chat.scroll_in(filler(1, 12))
        chat.add(head, tail)
        chat.add("Pidef begins casting Heal.")
        chat.finish()
        return texts(chat.out)

    def test_wrapped_coin_split_is_joined(self) -> None:
        head = "Pidef loots 43 copper coins from a dunes madman's corpse, and you receive"
        tail = "22 copper coins from a dunes madman's corpse as your split."
        got = self.joined(head, tail)
        self.assertIn(head + " " + tail, got)
        self.assertNotIn(head, got)
        self.assertNotIn(tail, got)

    def test_clipped_receive_and_misread_zero(self) -> None:
        head = "Gozif loots 1 copper coin from a rotting skeleton's corpse, and you receiv"
        tail = "O coins from a rotting skeleton's corpse as your split."
        got = self.joined(head, tail)
        self.assertIn(head + " " + tail, got)

    def test_receive_is_a_connector(self) -> None:
        head = "Palidu loots 8 copper coins from a caiman's corpse, and you"
        self.assertTrue(trk._ends_with_connector(head + " receive"))
        self.assertTrue(trk._ends_with_connector(head + " receiv"))
        self.assertGreaterEqual(trk.MAX_JOIN_PARTS, 3)


# ======================================================================================
# #26 / #32: restart
# ======================================================================================

WINDOW = filler(1, ROWS)


class SavedStateTests(unittest.TestCase):
    def first_run(self) -> tuple[Tracker, dict]:
        t = Tracker()
        for i in range(3):
            t.update(frame(WINDOW), i * 0.25)
        t.flush(1.0)
        return t, json.loads(json.dumps(t.export_state(now=100.0)))

    def test_export_holds_history_and_save_time(self) -> None:
        t, state = self.first_run()
        self.assertEqual(state["saved"], 100.0)
        self.assertEqual(state["next_seq"], ROWS - 1)
        self.assertEqual([norm for _seq, norm in state["history"]], t.emitted_tail)
        self.assertIsInstance(Tracker().export_state()["saved"], float)

    def test_same_screen_after_restart_emits_nothing_then_only_new_rows(self) -> None:
        _t, state = self.first_run()
        chat = Chat(now=130.0)
        self.assertTrue(chat.t.import_state(state, now=130.0))
        chat.lines = list(WINDOW)
        self.assertEqual(chat.view(frames=3), [])
        self.assertFalse(chat.t.scrolled_back)
        fresh = ["Wululiso begins casting Holy Shield.", "Gozif's Heal heals Wululiso for 31 Health."]
        for line in fresh:
            chat.add(line)
        chat.finish()
        self.assertEqual(texts(chat.out), fresh)
        self.assertFalse(any(m.backlog or m.estimated_ts for m in chat.out))

    def test_rows_that_arrived_while_down_are_new_not_backlog(self) -> None:
        _t, state = self.first_run()
        arrived = ["Palidu begins casting Mesmerize.", "a crocodile is mesmerized."]
        chat = Chat(now=160.0)
        self.assertTrue(chat.t.import_state(state, now=160.0))
        chat.lines = WINDOW + arrived
        chat.view(frames=3)
        chat.finish()
        self.assertEqual(texts(chat.out), arrived)
        self.assertFalse(any(m.backlog for m in chat.out))

    def test_stale_state_is_refused_and_the_cold_start_flags_backlog(self) -> None:
        _t, state = self.first_run()
        chat = Chat(now=100.0 + 601.0)
        self.assertFalse(chat.t.import_state(state, now=100.0 + 601.0))
        self.assertTrue(Tracker().import_state(state, now=100.0 + 599.0))
        chat.lines = list(WINDOW)
        chat.view(frames=3)
        chat.add("Wululiso begins casting Holy Shield.")
        chat.finish()
        self.assertEqual(texts(chat.out), WINDOW[1:] + ["Wululiso begins casting Holy Shield."])
        self.assertTrue(all(m.backlog for m in chat.out[:-1]))
        self.assertFalse(chat.out[-1].backlog)

    def test_malformed_state_is_refused(self) -> None:
        _t, state = self.first_run()
        bad = [
            {},
            [],
            dict(state, version=99),
            dict(state, history=[]),
            dict(state, history=[[5, "a"], [4, "b"]]),
            dict(state, next_seq=1),
            dict(state, history="nope"),
            dict(state, saved="later"),
        ]
        for value in bad:
            with self.subTest(value=str(value)[:60]):
                t = Tracker()
                self.assertFalse(t.import_state(value, now=130.0))  # type: ignore[arg-type]
                self.assertEqual(len(t.history), 0)
        t = Tracker()
        t.import_state({}, now=0.0)
        first = t.update(frame(WINDOW), 0.0) + t.update(frame(WINDOW), 0.25)
        self.assertTrue(first and all(m.backlog for m in first), "still a cold start")

    def test_cold_start_flags_only_the_first_frame(self) -> None:
        chat = Chat()
        chat.lines = list(WINDOW)
        chat.view()
        chat.add("Pidef appears.")
        chat.finish()
        self.assertEqual([m.backlog for m in chat.out], [True] * (ROWS - 1) + [False])


# ======================================================================================
# #35: jumps, scroll-back release, state
# ======================================================================================


class JumpTests(unittest.TestCase):
    def test_consecutive_jump_frames_emit_no_duplicates(self) -> None:
        # The chat fades to its last three lines while two more lines arrive between
        # frames: the three rows sit two rows higher, and only the top one (the least
        # trusted) matches, so the frame cannot be aligned.  Twice in a row.  The old
        # tracker emitted "You try to crush ..." twice here.
        chat = Chat()
        chat.lines = list(WINDOW)
        chat.view()
        chat.add(NEW[0], frames=1)  # seen once before the jumps
        chat.lines += NEW[1:3]
        before = chat.now
        self.assertEqual(chat.feed(frame(chat.lines[-3:], first_row=ROWS - 3)), [])
        self.assertTrue(chat.t.scrolled_back)
        chat.lines += NEW[3:5]
        self.assertEqual(chat.feed(frame(chat.lines[-3:], first_row=ROWS - 3)), [])
        chat.feed(frame(chat.lines[-3:], first_row=ROWS - 3))
        self.assertFalse(chat.t.scrolled_back)
        chat.view()
        chat.finish()
        self.assertEqual(texts(chat.out), WINDOW[1:] + NEW)
        released = chat.out[-len(NEW) :]
        self.assertFalse(released[0].estimated_ts, "seen before the jump: its own time")
        self.assertTrue(all(m.estimated_ts for m in released[1:]))
        times = [m.first_seen for m in released]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), len(times), "spread, not one stamp")
        self.assertTrue(all(before - 0.25 <= t <= before + 0.25 for t in times[1:3]))

    def test_garbage_frame_between_two_good_ones_is_dropped(self) -> None:
        # A frame of full-width nonsense (a blurred or half-drawn window) is a jump; the
        # next good frame lines up with the window before it, so the nonsense was seen
        # once and is dropped.  The old tracker emitted it.
        junk = [f"Hfx {i}{i} wq lt kgs zb{i} rmo plk vvt ostu nnf qa dorw yut emk." for i in range(ROWS)]
        for garbage_frames in (1, 2):
            with self.subTest(garbage_frames=garbage_frames):
                chat = Chat()
                chat.lines = list(WINDOW)
                chat.view()
                chat.add(NEW[0], frames=1)
                for k in range(garbage_frames):
                    chat.feed(frame([f"{j} {k}" for j in junk]))
                chat.add(NEW[1])
                chat.finish()
                self.assertEqual(texts(chat.out), WINDOW[1:] + NEW[:2])

    def test_rows_released_after_a_scroll_back_get_spread_times(self) -> None:
        chat = Chat()
        chat.scroll_in(filler(1, 30))
        end = len(chat.lines)
        last_live = chat.now - 0.25
        chat.view(end - 15, frames=4)  # the user reads older lines
        self.assertTrue(chat.t.scrolled_back)
        chat.lines += NEW[:4]  # meanwhile four lines arrive, out of sight
        chat.view(end - 15, frames=4)
        chat.view(end - 5, frames=2)  # one wheel notch: old rows only
        self.assertTrue(chat.t.scrolled_back)
        now = chat.now
        chat.view(frames=2)  # back at the bottom
        self.assertFalse(chat.t.scrolled_back)
        chat.finish()
        self.assertEqual(texts(chat.out), chat.lines[1:])
        released = chat.out[-4:]
        self.assertTrue(all(m.estimated_ts for m in released))
        times = [m.first_seen for m in released]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), 4)
        self.assertGreater(times[0], last_live)
        self.assertAlmostEqual(times[-1], now)
        self.assertFalse(any(m.estimated_ts for m in chat.out[:-4]))

    def test_rows_released_after_an_occlusion_get_spread_times(self) -> None:
        chat = Chat()
        chat.lines = list(WINDOW)
        chat.view()
        clipped = [L(MARGIN_X, TOP_Y + ROW_PITCH * i, LINE_H, t[:12]) for i, t in enumerate(WINDOW)]
        chat.feed(clipped, 3)
        self.assertTrue(chat.t.scrolled_back)
        chat.add(*NEW[:3], frames=2)
        self.assertFalse(chat.t.scrolled_back)
        chat.finish()
        self.assertEqual(texts(chat.out), WINDOW[1:] + NEW[:3])
        self.assertTrue(all(m.estimated_ts for m in chat.out[-3:]))
        times = [m.first_seen for m in chat.out[-3:]]
        self.assertEqual(times, sorted(times))
        self.assertEqual(len(set(times)), 3)

    def test_state_changes_are_logged_at_info_rate_limited(self) -> None:
        other = filler(100, ROWS)
        chat = Chat()
        chat.lines = list(WINDOW)
        chat.view()
        with self.assertLogs("mnmparse.tracker", level="INFO") as cm:
            chat.feed(frame(other), 2)  # jumped, then back in sync
            chat.feed(frame(WINDOW), 2)  # jumped again within the interval
        jumped = [r for r in cm.output if "chat window jumped" in r]
        self.assertEqual(len(jumped), 1, cm.output)
        self.assertTrue(all(r.startswith("INFO:") for r in jumped))
        chat.now += trk.STATE_LOG_INTERVAL_S
        with self.assertLogs("mnmparse.tracker", level="INFO") as cm:
            chat.feed(frame(other), 2)
        jumped = [r for r in cm.output if "chat window jumped" in r]
        self.assertEqual(len(jumped), 1, cm.output)
        self.assertIn("1 more such changes not logged", jumped[0])

    def test_no_warnings_for_jumps(self) -> None:
        chat = Chat()
        chat.lines = list(WINDOW)
        chat.view()
        with self.assertNoLogs("mnmparse.tracker", level="WARNING"):
            chat.feed(frame(filler(100, ROWS)), 2)


# ======================================================================================
# The published OCR recording still replays as before
# ======================================================================================


class FixtureFlagTests(unittest.TestCase):
    def test_only_first_frame_rows_are_backlog_and_nothing_is_estimated(self) -> None:
        msgs = replay_fixture(FIXTURE)
        backlog = [m for m in msgs if m.backlog]
        self.assertTrue(backlog)
        self.assertTrue(all(m.first_seen == 0.0 for m in backlog))
        self.assertEqual(backlog, msgs[: len(backlog)])
        self.assertFalse(any(m.estimated_ts for m in msgs))
        self.assertTrue(any("has slain" in m.text and not m.backlog for m in msgs))

    def test_tracker_ends_in_sync(self) -> None:
        t = Tracker()
        for now, lines in trk.load_fixture(FIXTURE):
            t.update(lines, now)
        self.assertFalse(t.scrolled_back)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    unittest.main()
