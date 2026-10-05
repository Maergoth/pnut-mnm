"""Tests for mnmparse.tracker (SPEC sections 6 and 7).

The fixture ``tests/fixtures/burst_ocr.json`` holds 99 anonymized OCR frames (~4 fps) of the
combat window during a party fight.  ``EXPECTED`` below is the full message sequence
derived by hand from the fixture: walking the frames in order and noting every row the
moment it enters at the bottom of the window (new rows always appear at the bottom; the
clipped/garbled rows at the top are older copies of rows seen earlier).

These tests build line objects from a tiny local dataclass so they do not depend on
``mnmparse.ocr``.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse import tracker as trk  # noqa: E402
from mnmparse.tracker import Message, Tracker, normalize, replay_fixture, similar  # noqa: E402

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


def frame(texts: list[str], *, x: int = MARGIN_X) -> list[L]:
    """Synthetic frame: one clean row per text, top to bottom, at the real geometry."""
    return [L(x, TOP_Y + ROW_PITCH * i, LINE_H, t) for i, t in enumerate(texts)]


def load_frames() -> list[tuple[float, list[L]]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [
        (f["t_ms"] / 1000.0, [L(l["x"], l["y"], l["h"], l["text"]) for l in f["lines"]])
        for f in data["frames"]
    ]


def run(tracker: Tracker, frames: list[tuple[float, list[L]]]) -> list[Message]:
    out: list[Message] = []
    now = 0.0
    for now, lines in frames:
        out.extend(tracker.update(lines, now))
    out.extend(tracker.flush(now))
    return out


def texts(msgs: list[Message]) -> list[str]:
    return [m.text for m in msgs]


# Characters allowed in a clean message (the report's "garbled" criterion).
CLEAN_RE = re.compile(r"^[A-Za-z0-9 ,.'!?:;()\-]*$")

# Complete expected message sequence of the fixture (see module docstring).  Two
# entries keep the OCR's own spelling of a number ("1 1" = 11, "I" = 1): the fixture
# never shows them any other way, and parser.py normalizes them.
EXPECTED: list[str] = [
    "Starting to attack.",
    "You crush a stumbling zombie for 8 points of damage.",
    "Davaren crushes a stumbling zombie for 2 points of damage.",
    "Nirek appears.",
    "Nirek ambushes their victim!",
    "Nirek's Backstab hits a stumbling zombie for 24 points of damage.",
    "Nirek pierces a stumbling zombie for 1 point of damage.",
    "You begin casting Cudgel of Light.",
    "Your fervor subsides.",
    "Davaren's Holy Strike hits a stumbling zombie for 10 points of Holy Damage.",
    "Davaren's spiritual connection is severed.",
    "a stumbling zombie bites Talalino for 1 1 points of damage.",
    "Your Cudgel of Light hits a stumbling zombie for 3 points of Holy Damage.",
    "You crush a stumbling zombie for 2 points of damage.",
    "Talalino pierces a stumbling zombie for 1 point of damage.",
    "a stumbling zombie's armor breaks.",
    "Davaren crushes a stumbling zombie for 2 points of damage.",
    "Nirek's Sanctified Weapon hits a stumbling zombie for 2 points of Holy Damage.",
    "a stumbling zombie tries to bite Talalino, but Talalino dodges!",
    "Nirek's Slice hits a stumbling zombie for 3 points of Bleed Damage.",
    "a stumbling zombie is bleeding out.",
    "Nirek's Slice hits a stumbling zombie for 6 points of damage.",
    "Talalino pierces a stumbling zombie for 1 point of damage.",
    "Talalino's Sanctified Weapon hits a stumbling zombie for 1 point of Holy Damage.",
    "Nirek jabs a stumbling zombie.",
    "You try to crush a stumbling zombie, but miss!",
    "Your Sanctified Weapon hits a stumbling zombie for 2 points of Holy Damage.",
    "You let loose an empowering battle cry.",
    "Talalino lets loose an empowering battle cry.",
    "Davaren lets loose an empowering battle cry.",
    "Nirek lets loose an empowering battle cry.",
    "Nirek pierces a stumbling zombie for 12 points of damage.",
    "a stumbling zombie loses interest in Nirek.",
    "You begin casting Cudgel of Light.",
    "a stumbling zombie tries to bite YOU, but misses!",
    "a stumbling zombie bites YOU for 20 points of damage.",
    "Talalino's Rend hits a stumbling zombie for 20 points of damage.",
    "Nirek pierces a stumbling zombie for 3 points of damage.",
    "Talalino tries to pierce a stumbling zombie, but a stumbling zombie dodges!",
    "Your casting is interrupted.",
    "You crush a stumbling zombie for 1 point of damage.",
    "a stumbling zombie bites Talalino for 19 points of damage.",
    "Nirek pierces a stumbling zombie for 5 points of damage.",
    "a stumbling zombie's armor breaks.",
    "Talalino pierces a stumbling zombie for 12 points of damage.",
    "Nirek's Backstab hits a stumbling zombie for 13 points of damage.",
    "Davaren crushes a stumbling zombie for 8 points of damage.",
    "Davaren becomes spiritually connected to the divine.",
    "You try to attack, but you are too far away.",
    "Davaren's Holy Strike hits a stumbling zombie for 15 points of Holy Damage.",
    "a stumbling zombie bites Talalino for 20 points of damage.",
    "Nirek pierces a stumbling zombie for 2 points of damage.",
    "a stumbling zombie's Strike hits Talalino for 6 points of damage.",
    "You try to attack, but you must face your target.",
    "Davaren begins casting Heal.",
    "You try to attack, but you are too far away.",
    "Your party member Nirek has slain a stumbling zombie!",
    "Stopped attacking.",
    "Davaren's Heal heals Talalino for 56 Health.",
    "Davaren begins casting Heal.",
    "Davaren's Heal heals Talalino for 56 Health.",
    "You begin casting Cudgel of Light.",
]

MANDATORY = [
    "Nirek's Backstab hits a stumbling zombie for 24 points of damage.",
    "Your party member Nirek has slain a stumbling zombie!",
    "Talalino tries to pierce a stumbling zombie, but a stumbling zombie dodges!",
    "a stumbling zombie tries to bite Talalino, but Talalino dodges!",
]

CLIPPED_PREFIXES = ("tarting", "ou crush", "avaren", "ou try", "i rek", "irek ", "our ", "ou ")


# ======================================================================================
# normalize / similar
# ======================================================================================


class NormalizeTests(unittest.TestCase):
    def test_lowercase_whitespace_punctuation(self) -> None:
        self.assertEqual(normalize("  Nirek's   Backstab hits!  "), "nireks backstab hits")
        self.assertEqual(normalize("you-try to attack, but miss!"), "you try to attack but miss")

    def test_digit_lookalikes_only_next_to_digits(self) -> None:
        self.assertEqual(normalize("for 1l points"), "for 11 points")
        self.assertEqual(normalize("for 1O points"), "for 10 points")
        self.assertEqual(normalize("for lO points"), "for lo points")  # no digit nearby
        self.assertEqual(normalize("for I point"), "for i point")  # lone I is left alone
        self.assertEqual(normalize("Holy Damage"), "holy damage")  # 'o' and 'l' untouched

    def test_similar_is_symmetric_and_bounded(self) -> None:
        a = "Davaren crushes a stumbling zombie for 2 points of damage."
        b = "avaren cr�shes a stumbling zombie for 2 points of damage."
        self.assertAlmostEqual(similar(a, b), similar(b, a))
        self.assertGreater(similar(a, b), 0.9)
        self.assertEqual(similar(a, a), 1.0)
        self.assertEqual(similar("", a), 0.0)
        self.assertLess(similar(a, "Nirek appears."), 0.5)

    def test_is_terminated(self) -> None:
        self.assertTrue(trk.is_terminated("damage."))
        self.assertTrue(trk.is_terminated("dodges!"))
        self.assertTrue(trk.is_terminated('says "hi"'))
        self.assertFalse(trk.is_terminated("for 24 points of"))
        self.assertFalse(trk.is_terminated(""))


# ======================================================================================
# Fixture replay
# ======================================================================================


class FixtureReplayTests(unittest.TestCase):
    frames: list[tuple[float, list[L]]]
    msgs: list[Message]

    @classmethod
    def setUpClass(cls) -> None:
        cls.frames = load_frames()
        cls.msgs = run(Tracker(), cls.frames)

    def test_fixture_loaded(self) -> None:
        self.assertEqual(len(self.frames), 99)

    def test_mandatory_messages_exactly_once(self) -> None:
        got = texts(self.msgs)
        for text in MANDATORY:
            self.assertEqual(got.count(text), 1, text)

    def test_kill_message_once(self) -> None:
        kills = [t for t in texts(self.msgs) if "has slain" in t]
        self.assertEqual(kills, ["Your party member Nirek has slain a stumbling zombie!"])

    def test_no_clipped_fragments(self) -> None:
        for t in texts(self.msgs):
            self.assertFalse(t.startswith(CLIPPED_PREFIXES), t)
            self.assertTrue(t[0].isalnum() or t[0] == "'", t)

    def test_you_crush_count(self) -> None:
        # The fixture contains three "You crush …" messages: 8 points (frame 0), 2 points
        # (enters at the bottom in frame 6) and 1 point (enters in frame 34).  "ou crush"
        # rows are the clipped top-row copy of the first one.
        hits = [t for t in texts(self.msgs) if t.startswith("You crush a stumbling zombie for")]
        self.assertEqual(len(hits), 3, hits)

    def test_no_wrapped_halves_leak(self) -> None:
        for t in texts(self.msgs):
            self.assertTrue(trk.is_terminated(t), t)
            self.assertFalse(re.match(r"^(damage|Damage|Holy Damage|dodges!|zombie dodges!|point)", t), t)

    def test_full_expected_sequence(self) -> None:
        got = texts(self.msgs)
        self.assertEqual(len(got), len(EXPECTED), "\n".join(got))
        for i, (g, e) in enumerate(zip(got, EXPECTED)):
            self.assertGreaterEqual(similar(g, e), 0.95, f"#{i}: {g!r} != {e!r}")
        # Everything but the two OCR-spelled numbers must be verbatim.
        exact = [e for e in EXPECTED if " 1 1 " not in e and " I point" not in e]
        for e in exact:
            self.assertIn(e, got, e)

    def test_total_in_sane_range(self) -> None:
        self.assertTrue(55 <= len(self.msgs) <= 70, len(self.msgs))

    def test_consecutive_near_duplicates_only_where_the_window_shows_them(self) -> None:
        # Two consecutive emitted messages may be near-identical only if the fixture
        # itself shows two adjacent rows that similar in some frame (the window shows
        # the true message order, so a genuine repeat is visible as adjacent rows).
        genuine: set[tuple[str, str]] = set()
        for _, lines in self.frames:
            rows = sorted(lines, key=lambda l: l.y)
            for a, b in zip(rows, rows[1:]):
                if b.y - a.y > 30 and similar(a.text, b.text) >= 0.95:
                    genuine.add((normalize(a.text), normalize(b.text)))
        got = texts(self.msgs)
        for a, b in zip(got, got[1:]):
            if similar(a, b) >= 0.95:
                self.assertIn((normalize(a), normalize(b)), genuine, f"{a!r} / {b!r}")

    def test_garbled_rate_under_5_percent(self) -> None:
        garbled = [t for t in texts(self.msgs) if not CLEAN_RE.match(t)]
        self.assertLess(len(garbled) / len(self.msgs), 0.05, garbled)

    def test_first_seen_is_monotonic_and_matches_frame_times(self) -> None:
        times = {t for t, _ in self.frames}
        seen = [m.first_seen for m in self.msgs]
        self.assertEqual(seen, sorted(seen))
        for m in self.msgs:
            self.assertIn(m.first_seen, times)
            self.assertGreaterEqual(m.frames_seen, 1)
        backstab = next(m for m in self.msgs if m.text.startswith("Nirek's Backstab hits") and "24" in m.text)
        self.assertEqual(backstab.first_seen, 0.0)
        kill = next(m for m in self.msgs if "has slain" in m.text)
        self.assertEqual(kill.first_seen, 16.249)  # frame 64, where the row entered

    def test_no_window_jump_warnings(self) -> None:
        with self.assertNoLogs("mnmparse.tracker", level="WARNING"):
            run(Tracker(), self.frames)

    def test_replay_fixture_helper_matches_manual_feed(self) -> None:
        self.assertEqual(replay_fixture(FIXTURE), self.msgs)
        self.assertEqual(replay_fixture(str(FIXTURE)), self.msgs)

    def test_top_line_never_emitted_from_frame_zero(self) -> None:
        # frame 0's top row is the clipped tail "zombie for 45B?nts of" of an older message
        self.assertFalse(any("45B" in t for t in texts(self.msgs)))


# ======================================================================================
# Synthetic tests (SPEC section 7)
# ======================================================================================

WINDOW = [
    "Starting to attack.",
    "You crush a stumbling zombie for 8 points of damage.",
    "Davaren crushes a stumbling zombie for 2 points of damage.",
    "Nirek appears.",
    "Nirek ambushes their victim!",
    "Nirek pierces a stumbling zombie for 1 point of damage.",
    "You begin casting Cudgel of Light.",
    "Your fervor subsides.",
    "Davaren's spiritual connection is severed.",
    "a stumbling zombie bites Talalino for 11 points of damage.",
    "You crush a stumbling zombie for 2 points of damage.",
    "a stumbling zombie's armor breaks.",
]
NEW_LINES = [
    "Talalino pierces a stumbling zombie for 1 point of damage.",
    "Davaren crushes a stumbling zombie for 2 points of damage.",
    "Nirek jabs a stumbling zombie.",
    "You try to crush a stumbling zombie, but miss!",
    "You let loose an empowering battle cry.",
    "Talalino lets loose an empowering battle cry.",
    "Davaren lets loose an empowering battle cry.",
    "Nirek lets loose an empowering battle cry.",
    "Nirek pierces a stumbling zombie for 12 points of damage.",
    "a stumbling zombie loses interest in Nirek.",
    "a stumbling zombie tries to bite YOU, but misses!",
    "a stumbling zombie bites YOU for 20 points of damage.",
    "Your casting is interrupted.",
    "You crush a stumbling zombie for 1 point of damage.",
    "a stumbling zombie bites Talalino for 19 points of damage.",
]


def scrolled(history: list[str], size: int = 12) -> list[str]:
    """The last ``size`` entries: what a 12-row window shows."""
    return history[-size:]


LONG_HISTORY = list(WINDOW) + NEW_LINES + [
    f"Nirek pierces a stumbling zombie for {i} points of damage." if i % 2 else f"You crush a rotting corpse for {i} points of damage."
    for i in range(30, 75)
]


class ReplayTests(unittest.TestCase):
    """Old content shown again (chat scrolled back, re-rendered) is never emitted twice.

    Recorded on 2026-10-02: after a death and a zone change the whole window of a dozen
    lines was emitted again eight times, so one death counted ten times.
    """

    def feed_scrolling(self, t: Tracker, history: list[str], start: float = 0.0) -> tuple[list[Message], float]:
        """Scroll ``history`` through a 12-row window, one new line per frame (each shown twice)."""
        out: list[Message] = []
        now = start
        for end in range(12, len(history) + 1):
            for _ in range(2):
                out.extend(t.update(frame(history[end - 12 : end]), now))
                now += 0.2
        return out, now

    def test_scrolled_back_and_down_again_emits_nothing_twice(self) -> None:
        t = Tracker()
        out, now = self.feed_scrolling(t, LONG_HISTORY)
        # The user scrolls the chat back by four screens, reads, then scrolls down again.
        for end in list(range(len(LONG_HISTORY), 20, -3)) + list(range(20, len(LONG_HISTORY) + 1)):
            for _ in range(2):
                out.extend(t.update(frame(LONG_HISTORY[end - 12 : end]), now))
                now += 0.2
        out.extend(t.flush(now))
        self.assertEqual(texts(out), LONG_HISTORY[1:], "every line exactly once, in order")
        self.assertGreater(t.replays_suppressed, 0)

    def test_old_screen_after_a_garbage_frame_is_recognised(self) -> None:
        t = Tracker()
        out, now = self.feed_scrolling(t, LONG_HISTORY)
        junk = [f"a e{i} lt %& kgs {i}{i} wq" for i in range(12)]
        out.extend(t.update(frame(junk), now))
        now += 0.2
        old = LONG_HISTORY[20:32]  # 40+ rows back: beyond the old 30-row memory
        for _ in range(3):
            out.extend(t.update(frame(old), now))
            now += 0.2
        out.extend(t.flush(now))
        replayed = [m.text for m in out if m.text in old]
        self.assertEqual(len(replayed), len(set(replayed)), f"old rows emitted twice: {replayed}")
        self.assertEqual([m.text for m in out if m.text in LONG_HISTORY][: len(LONG_HISTORY) - 1], LONG_HISTORY[1:])

    def test_new_lines_after_the_old_screen_still_come_through(self) -> None:
        t = Tracker()
        out, now = self.feed_scrolling(t, LONG_HISTORY)
        old = LONG_HISTORY[20:32]
        for _ in range(3):
            out.extend(t.update(frame(old), now))
            now += 0.2
        # back at the bottom, then two genuinely new messages arrive
        tail = list(LONG_HISTORY)
        for _ in range(2):
            out.extend(t.update(frame(tail[-12:]), now))
            now += 0.2
        fresh = ["Talalino begins casting Holy Shield.", "Davaren's Heal heals Talalino for 31 Health."]
        for line in fresh:
            tail.append(line)
            for _ in range(2):
                out.extend(t.update(frame(tail[-12:]), now))
                now += 0.2
        out.extend(t.flush(now))
        for line in fresh:
            self.assertEqual(texts(out).count(line), 1, line)
        self.assertEqual(len(texts(out)), len(set(texts(out))) + _repeats(LONG_HISTORY[1:]))

    def test_jittery_row_waits_for_a_repeated_reading(self) -> None:
        good = "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--"
        bad = "--Abepulifif loots [Bone Ohips] from a skeletal marksman's corpse.--"
        base = WINDOW[:6]
        t = Tracker()
        out = t.update(frame(base + [bad]), 0.0)
        out += t.update(frame(base + [good]), 0.2)
        self.assertNotIn(bad, texts(out), "two different readings: not decided yet")
        out += t.update(frame(base + [good]), 0.4)
        out += t.flush(1.0)
        self.assertIn(good, texts(out))
        self.assertNotIn(bad, texts(out))


def _repeats(lines: list[str]) -> int:
    """How many entries of ``lines`` repeat an earlier entry (legitimate duplicates)."""
    return len(lines) - len(set(lines))


class SyntheticTests(unittest.TestCase):
    def feed(self, tracker: Tracker, frames: list[list[str]], dt: float = 0.25) -> list[Message]:
        out: list[Message] = []
        for i, f in enumerate(frames):
            out.extend(tracker.update(frame(f), i * dt))
        return out

    def test_static_window_emits_once_then_nothing(self) -> None:
        t = Tracker()
        first = t.update(frame(WINDOW), 0.0)
        self.assertEqual(first, [])  # one sighting is not enough
        second = t.update(frame(WINDOW), 0.25)
        self.assertEqual(texts(second), WINDOW[1:])  # the top row is unreliable
        for i in range(3, 20):
            self.assertEqual(t.update(frame(WINDOW), i * 0.25), [])
        self.assertEqual(t.flush(5.0), [])

    def test_static_window_top_line_trusted_when_configured(self) -> None:
        t = Tracker(ignore_top_line=False)
        t.update(frame(WINDOW), 0.0)
        self.assertEqual(texts(t.update(frame(WINDOW), 0.25)), WINDOW)

    def test_min_frames_one_emits_immediately(self) -> None:
        t = Tracker(min_frames=1)
        self.assertEqual(texts(t.update(frame(WINDOW), 0.0)), WINDOW[1:])
        self.assertEqual(t.update(frame(WINDOW), 0.25), [])

    def test_one_new_line_per_frame(self) -> None:
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        for line in NEW_LINES:
            history.append(line)
            frames.append(scrolled(history))
        frames.append(scrolled(history))
        t = Tracker()
        out = self.feed(t, frames)
        out.extend(t.flush(len(frames) * 0.25))
        self.assertEqual(texts(out), history[1:])
        # first_seen is the frame in which the row entered the window
        by_text = {m.text: m for m in out}
        for i, line in enumerate(NEW_LINES):
            self.assertAlmostEqual(by_text[line].first_seen, (2 + i) * 0.25)

    def test_three_new_lines_per_frame(self) -> None:
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        for k in range(0, len(NEW_LINES), 3):
            history.extend(NEW_LINES[k : k + 3])
            frames.append(scrolled(history))
        frames.append(scrolled(history))
        t = Tracker()
        out = self.feed(t, frames)
        out.extend(t.flush(len(frames) * 0.25))
        self.assertEqual(texts(out), history[1:])

    def test_whole_window_replaced_each_frame_emits_in_order(self) -> None:
        # 12 new lines per frame: nothing overlaps, so every frame is a "jump".
        t = Tracker()
        a = [f"Nirek pierces a stumbling zombie for {i} points of damage." for i in range(1, 13)]
        b = [f"Davaren crushes a stumbling zombie for {i} points of damage." for i in range(1, 13)]
        out = self.feed(t, [a, a, b, b])
        out.extend(t.flush(2.0))
        self.assertEqual(texts(out), a[1:] + b[1:])

    def test_identical_consecutive_messages_each_emitted(self) -> None:
        hit = "You crush a stumbling zombie for 1 point of damage."
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        for _ in range(4):
            history.append(hit)
            frames.append(scrolled(history))
        frames.append(scrolled(history))
        t = Tracker()
        out = self.feed(t, frames)
        out.extend(t.flush(len(frames) * 0.25))
        self.assertEqual(texts(out).count(hit), 4)
        self.assertEqual(texts(out), history[1:])

    def test_identical_messages_arriving_together(self) -> None:
        hit = "a stumbling zombie bites YOU for 20 points of damage."
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        history.extend([hit, hit, hit])
        frames.extend([scrolled(history)] * 2)
        t = Tracker()
        out = self.feed(t, frames)
        out.extend(t.flush(2.0))
        self.assertEqual(texts(out), history[1:])

    def test_window_clear(self) -> None:
        t = Tracker()
        out = self.feed(t, [WINDOW, WINDOW, WINDOW])
        self.assertEqual(texts(out), WINDOW[1:])
        # The chat is cleared and fills again with unrelated text.  The state change is
        # logged at INFO (rate-limited), not as a warning per frame.
        fresh = NEW_LINES[:5]
        with self.assertLogs("mnmparse.tracker", level="INFO") as cm:
            out2 = t.update(frame(fresh), 1.0)
        self.assertTrue(any("jumped" in rec for rec in cm.output))
        self.assertEqual(out2, [])
        out2 = t.update(frame(fresh), 1.25)
        self.assertEqual(texts(out2), fresh[1:])
        self.assertEqual(t.update(frame(fresh), 1.5), [])
        self.assertEqual(t.flush(2.0), [])

    def test_empty_frames_do_not_disturb(self) -> None:
        t = Tracker()
        t.update(frame(WINDOW), 0.0)
        self.assertEqual(t.update([], 0.25), [])
        self.assertEqual(texts(t.update(frame(WINDOW), 0.5)), WINDOW[1:])
        self.assertEqual(t.update([], 0.75), [])
        self.assertEqual(t.flush(1.0), [])

    def test_ocr_jitter_emits_once(self) -> None:
        clean = "You crush a stumbling zombie for 9 points of damage."
        jitter = "You crush a stumbling zombie for 9 polnts of damage."
        t = Tracker()
        base = WINDOW[2:8]  # contains no "You crush" line
        t.update(frame(base + [clean]), 0.0)
        out = t.update(frame(base + [jitter]), 0.25)
        out += t.update(frame(base + [clean]), 0.5)
        out += t.update(frame(base + [jitter]), 0.75)
        out += t.flush(1.0)
        crushes = [m for m in out if m.text.startswith("You crush")]
        self.assertEqual(len(crushes), 1)
        self.assertEqual(crushes[0].text, clean)
        # Emitted as soon as min_frames agreeing observations exist (2), so the
        # count reflects the frames seen at emission time, not all four.
        self.assertGreaterEqual(crushes[0].frames_seen, 2)
        self.assertEqual(crushes[0].first_seen, 0.0)

    def test_variant_voting_prefers_the_consensus(self) -> None:
        good = "Davaren crushes a stumbling zombie for 2 points of damage."
        bad = "Davaren crushes a stumbli mbie-for2points of Carnage."
        base = WINDOW[:6]
        t = Tracker()
        t.update(frame(base + [good]), 0.0)
        t.update(frame(base + [bad]), 0.25)
        out = t.update(frame(base + [good]), 0.5)
        out += t.flush(1.0)
        self.assertIn(good, texts(out))
        self.assertNotIn(bad, texts(out))

    def test_left_clipped_reading_waits_for_a_clean_one(self) -> None:
        clean = "Davaren begins casting Heal."
        clipped = "avaren begins casting Heal."
        base = WINDOW[:6]
        t = Tracker()
        t.update(frame(base), 0.0)
        t.update(frame(base), 0.25)
        for i in range(4):  # the first glyph is lost in four frames in a row
            lines = frame(base) + [L(MARGIN_X + 17, TOP_Y + ROW_PITCH * 6, LINE_H, clipped)]
            self.assertEqual(t.update(lines, 0.5 + i * 0.25), [])
        out = t.update(frame(base + [clean]), 1.5)
        out += t.update(frame(base + [clean]), 1.75)
        self.assertEqual(texts(out), [clean])
        self.assertEqual(out[0].first_seen, 0.5)
        self.assertEqual(t.flush(2.0), [])

    def test_fragmented_row_is_merged_and_not_duplicated(self) -> None:
        base = WINDOW[:6]
        whole = "Davaren's Heal heals Talalino for 56 Health."
        y = TOP_Y + ROW_PITCH * 6
        split = [L(MARGIN_X, y, LINE_H, "Davaren's Heal he s"), L(254, y, LINE_H, "'lalino for 56 Health.")]
        t = Tracker()
        out = t.update(frame(base) + split, 0.0)
        out += t.update(frame(base + [whole]), 0.25)
        out += t.update(frame(base + [whole]), 0.5)
        out += t.flush(1.0)
        self.assertEqual(texts(out), base[1:] + [whole])

    def test_wrapped_message_joined(self) -> None:
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        history += ["Nirek's Backstab hits a stumbling zombie for 24 points of", "damage."]
        frames += [scrolled(history), scrolled(history)]
        t = Tracker()
        out = self.feed(t, frames)
        out += t.flush(1.0)
        self.assertIn("Nirek's Backstab hits a stumbling zombie for 24 points of damage.", texts(out))
        self.assertNotIn("damage.", texts(out))
        joined = next(m for m in out if m.text.startswith("Nirek's Backstab"))
        self.assertAlmostEqual(joined.first_seen, 0.5)

    def test_wrapped_half_times_out(self) -> None:
        history = list(WINDOW)
        frames = [scrolled(history), scrolled(history)]
        history += ["Nirek's Backstab hits a stumbling zombie for 24 points of"]
        frames += [scrolled(history), scrolled(history)]
        t = Tracker(join_timeout_s=2.0)
        out = self.feed(t, frames)
        self.assertNotIn("Nirek's Backstab hits a stumbling zombie for 24 points of", texts(out))
        self.assertEqual(t.update(frame(scrolled(history)), 1.0), [])
        late = t.update(frame(scrolled(history)), 3.0)
        self.assertEqual(texts(late), ["Nirek's Backstab hits a stumbling zombie for 24 points of"])
        # a later message is not glued onto the timed-out half
        history.append("Nirek appears.")
        t.update(frame(scrolled(history)), 3.25)
        self.assertEqual(texts(t.update(frame(scrolled(history)), 3.5)), ["Nirek appears."])

    def test_flush_emits_pending_and_held(self) -> None:
        t = Tracker()
        history = list(WINDOW)
        t.update(frame(history), 0.0)
        t.update(frame(history), 0.25)
        history += ["Talalino's Rend hits a stumbling zombie for 20 points of", "damage."]
        self.assertEqual(t.update(frame(scrolled(history)), 0.5), [])  # seen once
        out = t.flush(0.75)
        self.assertEqual(texts(out), ["Talalino's Rend hits a stumbling zombie for 20 points of damage."])
        self.assertEqual(out[0].first_seen, 0.5)
        self.assertEqual(t.flush(1.0), [])

    def test_scroll_off_emits_single_sighting(self) -> None:
        # A row seen only once (cleanly) before a 12-line jump must still be emitted.
        t = Tracker()
        t.update(frame(WINDOW), 0.0)
        t.update(frame(WINDOW), 0.25)
        once = WINDOW[1:] + ["Nirek jabs a stumbling zombie."]
        self.assertEqual(t.update(frame(once), 0.5), [])
        replacement = [f"a stumbling zombie bites YOU for {i} points of damage." for i in range(1, 13)]
        out = t.update(frame(replacement), 0.75)
        out += t.update(frame(replacement), 1.0)
        out += t.flush(1.25)
        self.assertEqual(texts(out), ["Nirek jabs a stumbling zombie."] + replacement[1:])

    def test_junk_rows_are_ignored(self) -> None:
        t = Tracker()
        junk = [L(636, 263, 35, "i"), L(55, 456, 11, "n")]
        t.update(frame(WINDOW) + junk, 0.0)
        out = t.update(frame(WINDOW) + junk, 0.25)
        self.assertEqual(texts(out), WINDOW[1:])

    def test_geometry_is_estimated(self) -> None:
        t = Tracker()
        t.update(frame(WINDOW), 0.0)
        self.assertAlmostEqual(t.pitch, ROW_PITCH)
        self.assertEqual(t.margin, MARGIN_X)
        self.assertAlmostEqual(t.line_height, LINE_H)


if __name__ == "__main__":
    unittest.main()
