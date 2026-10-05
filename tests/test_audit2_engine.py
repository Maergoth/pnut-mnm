"""Regression tests for the second audit: the live engine and the UI wiring.

* #11 / #20 / #26 / #32: a restart must not log the visible chat again.  The tracker goes on
  over Stop/Start, its state is saved next to the logs (on stop and every
  ``STATE_SAVE_MESSAGES`` messages) and taken back on start; lines flagged as backlog (a cold
  start) are neither logged nor counted; a restart without a usable state drops the first
  frame's lines that repeat the end of the newest log.  The zone and the party roster carry
  over, and the zone is saved for a restart of the app.
* #10 (c): when the party roster learns a member, the recent closed fights are counted again
  (only ever adding members to the group), without a second auto-copy.
* #22: only the group's own fights count in the session's encounters.
* #35: the chat scrolled up shows as a warning, holds the encounter timeout, and a short crop
  is warned about once.
* #7: everyday game messages (vendor, corpse loot, rewards, level-ups ...) are not unreadable;
  every kind has a feed group and a colour.
* #36: a short fight is still copied automatically (its rates use the group's fighting time).

No capture or OCR runs here: the pipeline objects are installed directly, the OCR is a stand-in
that returns prepared lines, and every file goes to a temporary folder.  Qt is only needed for
the signals and the two views (offscreen platform).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # real Windows fonts on the offscreen platform (realistic text widths)
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    import numpy as np
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False

from mnmparse import grammar  # noqa: E402
from mnmparse.config import Config  # noqa: E402
from mnmparse.logwriter import LogWriter, eq_timestamp, raw_header  # noqa: E402
from mnmparse.parser import parse_line  # noqa: E402
from mnmparse.tracker import Message, Tracker  # noqa: E402

PLAYER = "Pidef"


@dataclass
class L:
    """Stand-in for ``ocr.OcrLine``: only ``.x .y .h .text`` are used by the tracker."""

    x: int
    y: int
    h: int
    text: str


def rows(texts: list[str]) -> list[L]:
    """One clean OCR line per text, top to bottom, 40 px apart."""
    return [L(21, 21 + 40 * i, 23, t) for i, t in enumerate(texts)]


def lines(start: int, count: int) -> list[str]:
    """``count`` distinct ordinary combat lines."""
    return [
        f"Tovozen crushes a crocodile for {i} points of damage."
        if i % 2
        else f"Gozif's Backstab hits a crocodile for {i} points of damage."
        for i in range(start, start + count)
    ]


class _FakeOcr:
    """OCR stand-in: ``read`` returns whatever the test put in ``lines``."""

    def __init__(self) -> None:
        self.lines: list[L] = []

    def read(self, _img: Any) -> list[L]:
        return list(self.lines)


def _logged(folder: str) -> list[str]:
    """The texts of every combat_*.log line in ``folder`` (headers skipped), file by file."""
    out: list[str] = []
    for path in sorted(Path(folder).glob("combat_*.log")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            if raw.startswith("["):
                out.append(raw.split("] ", 1)[1])
    return out


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class _EngineCase(unittest.TestCase):
    """An Engine on a temporary log folder, its pipeline installed without capture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        # No background seeding of the learned spellings from the (empty) test folder.
        patcher = mock.patch("mnmparse.app.engine._load_vocab_once", lambda cfg: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engines: list[Any] = []

    def tearDown(self) -> None:
        for engine in self.engines:
            if engine._writer is not None:
                engine._writer.close()
        self._tmp.cleanup()

    def cfg(self, **kw: Any) -> Config:
        base = dict(player_name=PLAYER, log_dir=self.tmp, encounter_timeout_s=12.0)
        base.update(kw)
        return Config(**base)

    def engine(self, cfg: Config | None = None, *, run: bool = True) -> Any:
        """A new Engine (a new app process, as far as the engine knows), started when ``run``."""
        from mnmparse.app.engine import Engine

        engine = Engine(cfg or self.cfg())
        self.engines.append(engine)
        if run:
            engine._prepare_run(engine.config, _FakeOcr())
        return engine

    @staticmethod
    def show(engine: Any, texts: list[str], frames: int = 2) -> None:
        """Show ``texts`` as the chat window for ``frames`` frames."""
        engine._ocr.lines = rows(texts)
        for _ in range(frames):
            engine._process_frame(np.zeros((8, 8, 3), np.uint8), engine.config, cropped=True)

    @staticmethod
    def feed(engine: Any, *items: tuple[float, str]) -> None:
        """Hand ``(ts, text)`` messages to the engine as the tracker would."""
        for ts, text in items:
            engine._handle_message(Message(text, ts, 2), engine.config)


# ======================================================================================
# #11 / #20 / #26 / #32: restarts
# ======================================================================================


class BacklogTests(_EngineCase):
    def test_backlog_lines_are_not_logged_counted_or_announced(self) -> None:
        engine = self.engine()
        seen: list[Any] = []
        engine.message.connect(lambda msg, ev: seen.append(msg.text))
        stale = Message("You crush a crocodile for 10 points of damage.", 100.0, 2, backlog=True)
        engine._handle_message(stale, engine.config)
        self.assertEqual(seen, [], "no feed, triggers or overlay for a backlog line")
        self.assertIsNone(engine._stats.current(), "no fight opened from it")
        self.assertEqual(engine.session_snapshot().kills, 0)
        self.assertIsNone(engine._writer.raw_path, "nothing written")
        self.assertEqual(engine._backlog_dropped, 1)
        self.feed(engine, (101.0, "You crush a crocodile for 11 points of damage."))
        self.assertEqual(seen, ["You crush a crocodile for 11 points of damage."])
        engine._writer.close()
        self.assertEqual(_logged(self.tmp), ["You crush a crocodile for 11 points of damage."])

    def test_cold_start_logs_only_what_arrives_after_the_first_frame(self) -> None:
        engine = self.engine()
        window = lines(1, 8)
        self.show(engine, window)
        self.show(engine, window[1:] + ["Wululiso begins casting Holy Shield."])
        engine._finish()
        self.assertEqual(_logged(self.tmp), ["Wululiso begins casting Holy Shield."])
        self.assertGreaterEqual(engine._backlog_dropped, 7)

    def test_restart_with_the_saved_state_logs_nothing_twice(self) -> None:
        first = self.engine()
        window = lines(1, 8)
        self.show(first, window)
        window = window[1:] + ["Wululiso begins casting Holy Shield."]
        self.show(first, window)
        first._finish()
        state = json.loads((Path(self.tmp) / "tracker_state.json").read_text(encoding="utf-8"))
        self.assertTrue(state["history"], "the tracker's rows are saved on stop")
        self.assertFalse(list(Path(self.tmp).glob("*.tmp")), "written through a temporary file")

        second = self.engine()  # a new app: only the saved file connects the two runs
        self.assertFalse(second._tracker._cold, "the saved state was taken")
        self.show(second, window, frames=3)
        arrived = ["Palidu begins casting Mesmerize.", "a crocodile is mesmerized."]
        window = window[2:] + arrived
        self.show(second, window)
        second._finish()
        self.assertEqual(_logged(self.tmp), ["Wululiso begins casting Holy Shield."] + arrived)

    def test_stop_start_keeps_the_tracker(self) -> None:
        engine = self.engine()
        self.show(engine, lines(1, 6))
        tracker = engine._tracker
        engine._finish()
        engine._prepare_run(engine.config, _FakeOcr())
        self.assertIs(engine._tracker, tracker, "the same tracker goes on after Stop/Start")
        self.assertIsNone(engine._restart, "its history is complete: no tail check")
        engine._finish()
        engine.update_config(self.cfg(crop=(0, 60, 700, 700)))
        engine._prepare_run(engine.config, _FakeOcr())
        self.assertIsNot(engine._tracker, tracker, "another crop: a new tracker ...")
        self.assertEqual(list(engine._tracker.history), list(tracker.history), "... with the rows remembered")

    def test_stale_or_missing_state_means_a_cold_start(self) -> None:
        from mnmparse.app import engine as engine_mod

        first = self.engine()
        self.show(first, lines(1, 6))
        first._finish()
        path = Path(self.tmp) / engine_mod.TRACKER_STATE_FILE
        state = json.loads(path.read_text(encoding="utf-8"))
        state["saved"] = time.time() - engine_mod.STATE_MAX_AGE_S - 60.0
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(self.engine()._tracker._cold, "too old: refused")
        path.unlink()
        self.assertTrue(self.engine()._tracker._cold, "no state at all")

    def test_state_is_saved_while_capturing(self) -> None:
        from mnmparse.app import engine as engine_mod

        engine = self.engine()
        path = Path(self.tmp) / engine_mod.TRACKER_STATE_FILE
        window = lines(1, 8)
        self.show(engine, window)
        self.assertFalse(path.exists())
        engine._since_state_save = engine_mod.STATE_SAVE_MESSAGES - 1
        self.show(engine, window[1:] + ["Wululiso begins casting Holy Shield."])
        self.assertTrue(path.exists(), "saved after STATE_SAVE_MESSAGES messages (an app killed never stops)")
        self.assertEqual(engine._since_state_save, 0)


class RestartTailTests(_EngineCase):
    """Without a usable state, the first frame's lines that repeat the newest log's end are dropped."""

    TAIL = lines(1, 10)

    def write_log(self, stamp: float, texts: list[str]) -> Path:
        path = Path(self.tmp) / "combat_2026-10-03_020229.log"
        body = [raw_header()] + [f"{eq_timestamp(stamp)} {t}" for t in texts]
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def test_the_newest_log_tail_is_read_when_recent(self) -> None:
        from mnmparse.app.engine import _previous_log_tail

        self.assertIsNone(_previous_log_tail(Path(self.tmp), time.time()), "no log yet")
        self.write_log(time.time() - 30.0, self.TAIL)
        self.assertEqual(_previous_log_tail(Path(self.tmp), time.time()), self.TAIL)
        self.write_log(time.time() - 700.0, self.TAIL)
        self.assertIsNone(_previous_log_tail(Path(self.tmp), time.time()), "ended too long ago")

    def test_a_restart_soon_after_reads_the_tail(self) -> None:
        self.write_log(time.time() - 30.0, self.TAIL)
        engine = self.engine()
        self.assertIsNotNone(engine._restart)
        self.assertEqual(engine._restart.tail, self.TAIL)

    def restarted(self) -> Any:
        from mnmparse.app.engine import _RestartTail

        engine = self.engine()
        engine._restart = _RestartTail(list(self.TAIL))
        return engine

    def test_repeated_opening_lines_are_dropped_and_new_ones_kept(self) -> None:
        engine = self.restarted()
        new = "Wululiso begins casting Holy Shield."
        opening = [Message(t, 500.0, 2) for t in self.TAIL[-5:] + [new]]
        self.assertEqual(engine._filter_restart(opening, 500.25), [], "held until the chat scrolls")
        later = Message("Palidu begins casting Mesmerize.", 505.0, 2)
        out = engine._filter_restart([later], 505.0)
        self.assertEqual([m.text for m in out], [new, later.text])
        self.assertIsNone(engine._restart, "checked once")
        self.assertEqual(engine._backlog_dropped, 5)

    def test_the_hold_ends_after_a_few_seconds(self) -> None:
        from mnmparse.app.engine import RESTART_HOLD_S

        engine = self.restarted()
        opening = [Message(t, 500.0, 2) for t in self.TAIL[-4:]]
        self.assertEqual(engine._filter_restart(opening, 500.0), [])
        self.assertEqual(engine._filter_restart([], 500.0 + RESTART_HOLD_S + 0.1), [], "all four repeat the tail")
        self.assertEqual(engine._backlog_dropped, 4)

    def test_lines_that_do_not_line_up_are_kept(self) -> None:
        engine = self.restarted()
        opening = [Message(t, 500.0, 2) for t in lines(40, 4)]
        engine._filter_restart(opening, 500.0)
        out = engine._filter_restart([], 504.0)
        self.assertEqual([m.text for m in out], lines(40, 4))
        self.assertEqual(engine._backlog_dropped, 0)

    def test_stop_settles_the_held_lines(self) -> None:
        engine = self.restarted()
        new = "Wululiso begins casting Holy Shield."
        engine._filter_restart([Message(t, 500.0, 2) for t in self.TAIL[-3:] + [new]], 500.0)
        engine._finish()
        self.assertEqual(_logged(self.tmp), [new])


class CarryOverTests(_EngineCase):
    """The zone and the party roster survive Stop/Start (#11/#32's dead carry-over)."""

    ZONE = "You have entered Night Harbor (East)."

    def test_zone_and_party_carry_over_stop_start(self) -> None:
        engine = self.engine()
        self.feed(engine, (90.0, self.ZONE), (91.0, "Tovozen has joined the party."))
        roster = engine._stats.roster
        engine._finish()
        self.assertIsNone(engine._stats)
        engine._prepare_run(engine.config, _FakeOcr())
        self.assertEqual(engine._stats.zone_at(5000.0), "Night Harbor (East)")
        self.assertIs(engine._stats.roster, roster)
        self.assertIn("Tovozen", engine._stats.party)
        self.assertIs(engine._session_stats.roster, roster, "the session counts the same party")
        engine.reset_session()
        self.assertIs(engine._session_stats.roster, roster)

    def test_a_restart_soon_after_takes_the_saved_zone(self) -> None:
        from mnmparse.app import engine as engine_mod

        first = self.engine()
        self.feed(first, (90.0, self.ZONE))
        first._finish()
        second = self.engine()
        self.assertEqual(second._stats.zone_at(5000.0), "Night Harbor (East)")
        path = Path(self.tmp) / engine_mod.SESSION_STATE_FILE
        data = json.loads(path.read_text(encoding="utf-8"))
        data["saved"] = time.time() - engine_mod.STATE_MAX_AGE_S - 60.0
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.engine()._stats.zone_at(5000.0), "", "too old: not taken")

    def test_the_viewer_name_reaches_the_meter_and_the_party(self) -> None:
        engine = self.engine()
        self.assertEqual(engine._stats.player_name, PLAYER)
        self.assertEqual(engine._stats.roster.player_name, PLAYER)
        engine.update_config(self.cfg(player_name="Gozif"))
        self.assertEqual(engine._stats.player_name, "Gozif")
        self.assertEqual(engine._stats.roster.player_name, "Gozif")
        self.feed(engine, (100.0, "Gozif has joined the party."))
        self.assertNotIn("Gozif", engine._stats.party, "the viewer is never on their own roster")

    def test_a_change_by_hand_while_stopped_is_kept(self) -> None:
        engine = self.engine()
        engine._finish()
        engine.set_group_override("Palidu", True)
        engine._prepare_run(engine.config, _FakeOcr())
        self.assertIn("Palidu", engine._stats.party)


# ======================================================================================
# #10 (c) and #22: recent fights counted again; only our fights in the session
# ======================================================================================

FIGHT = (
    (100.0, "You crush a rat for 10 points of damage."),
    (101.0, "Wululiso slashes a rat for 6 points of damage."),
    (102.0, "Tovozen crushes a rat for 2 points of damage."),
)


class RecentFightTests(_EngineCase):
    def setUp(self) -> None:
        super().setUp()
        self.engine_ = self.engine()
        self.closed: list[Any] = []
        self.updated: list[Any] = []
        self.engine_.encounter_closed.connect(self.closed.append)
        self.engine_.encounter_updated.connect(self.updated.append)
        self.feed(self.engine_, (90.0, "Tovozen has joined the party."))

    def fight(self, items: tuple[tuple[float, str], ...] = FIGHT, *, end: float = 200.0) -> Any:
        self.feed(self.engine_, *items)
        self.engine_._expire(end)
        return self.closed[-1]

    @staticmethod
    def group(snap: Any) -> set[str]:
        return {r.name for r in snap.rows if r.in_group}

    def test_a_member_recognised_late_moves_into_the_group(self) -> None:
        engine = self.engine_
        snap = self.fight()
        self.assertEqual(self.group(snap), {PLAYER, "Tovozen"}, "not known yet: an outsider")
        self.feed(engine, (210.0, "Wululiso has joined the party."))
        updated = engine._maybe_rebuild_recent(now=215.0)
        self.assertEqual([s.key for s in updated], [snap.key])
        self.assertEqual(self.group(updated[0]), {PLAYER, "Tovozen", "Wululiso"})
        self.assertEqual(updated[0].total_damage, 18, "their damage counts now")
        self.assertEqual(len(self.updated), 1, "announced as an update ...")
        self.assertEqual(len(self.closed), 1, "... not as a new fight (no auto-copy, no sound)")
        self.assertIs(engine.history()[-1], updated[0])
        self.assertEqual(engine._maybe_rebuild_recent(now=216.0), [], "nothing changed since")

    def test_a_member_who_leaves_stays_in_their_fights(self) -> None:
        engine = self.engine_
        self.feed(engine, (95.0, "Wululiso has joined the party."))
        engine._maybe_rebuild_recent(now=96.0)
        snap = self.fight()
        self.feed(engine, (210.0, "Wululiso has left the party."))
        self.assertEqual(engine._maybe_rebuild_recent(now=215.0), [])
        self.assertEqual(self.group(engine.history()[-1]), self.group(snap))
        self.assertIn("Wululiso", self.group(snap))

    def test_fights_over_fifteen_minutes_ago_are_left(self) -> None:
        engine = self.engine_
        self.fight()  # its last blow at 102
        self.feed(engine, (2000.0, "Wululiso has joined the party."))
        self.assertEqual(engine._maybe_rebuild_recent(now=2000.0), [])

    def test_fights_before_the_zone_line_are_left(self) -> None:
        engine = self.engine_
        self.fight()
        self.feed(engine, (300.0, "You have entered Night Harbor (East)."))
        self.feed(engine, (310.0, "Wululiso has joined the party."))
        self.assertEqual(engine._maybe_rebuild_recent(now=310.0), [])

    def test_a_change_by_hand_counts_the_recent_fights_again(self) -> None:
        engine = self.engine_
        snap = self.fight()
        engine.set_group_override("Wululiso", True)
        updated = engine._maybe_rebuild_recent(now=150.0)
        self.assertEqual([s.key for s in updated], [snap.key])
        self.assertIn("Wululiso", self.group(updated[0]))
        engine.set_group_override("Wululiso", False)
        updated = engine._maybe_rebuild_recent(now=151.0)
        self.assertNotIn("Wululiso", self.group(updated[0]), "by hand, out works too")

    def test_only_our_fights_count_in_the_session(self) -> None:
        engine = self.engine_
        other = self.fight(((300.0, "Wululiso slashes a rat for 6 points of damage."),
                            (301.0, "Wululiso slashes a rat for 5 points of damage.")), end=400.0)
        self.assertFalse(other.ours)
        self.assertEqual(engine.session_snapshot().encounters, 0, "another group's fight (#22)")
        self.feed(engine, (410.0, "Wululiso has joined the party."))
        updated = engine._maybe_rebuild_recent(now=410.0)
        self.assertTrue(updated and updated[0].ours, "it was ours after all")
        self.assertEqual(engine.session_snapshot().encounters, 1)


# ======================================================================================
# #35: the chat scrolled up, and a short crop
# ======================================================================================


class WindowStateTests(_EngineCase):
    def test_scrolled_back_shows_after_a_second_and_is_logged(self) -> None:
        engine = self.engine()
        fake = SimpleNamespace(scrolled_back=True, prev=[], pitch=40.0, occluded_frames=0, replays_suppressed=0)
        engine._state = "running"
        with self.assertLogs("mnmparse.app.engine", "INFO") as logs:
            engine._note_window_state(fake, 100.0)
            self.assertFalse(engine._scrolled_shown, "a passing jump does not show")
            engine._note_window_state(fake, 101.5)
            fake.scrolled_back = False
            engine._note_window_state(fake, 103.0)
        self.assertIn("scrolled up", logs.output[0])
        self.assertIn("newest line again", logs.output[1])
        statuses: list[dict] = []
        engine.status.connect(statuses.append)
        engine._tracker = fake
        fake.scrolled_back = True
        engine._note_window_state(fake, 110.0)
        engine._note_window_state(fake, 112.0)
        engine._emit_status()
        self.assertTrue(statuses[-1]["scrolled_back"])

    def test_the_fight_waits_while_the_chat_is_scrolled_up(self) -> None:
        from mnmparse.app.engine import SCROLLED_BACK_HOLD_MAX_S

        engine = self.engine()
        closed: list[Any] = []
        engine.encounter_closed.connect(closed.append)
        self.feed(engine, *FIGHT)
        fake = SimpleNamespace(scrolled_back=True, prev=[])
        engine._tracker = fake
        engine._note_window_state(fake, 103.0)
        engine._expire(120.0)
        self.assertEqual(closed, [], "its lines may still be coming")
        engine._expire(103.0 + SCROLLED_BACK_HOLD_MAX_S + 1.0)
        self.assertEqual(len(closed), 1, "but not for ever")

    def test_the_fight_times_out_once_the_chat_is_back(self) -> None:
        engine = self.engine()
        closed: list[Any] = []
        engine.encounter_closed.connect(closed.append)
        self.feed(engine, *FIGHT)
        fake = SimpleNamespace(scrolled_back=True, prev=[])
        engine._tracker = fake
        engine._note_window_state(fake, 103.0)
        engine._expire(120.0)
        fake.scrolled_back = False
        engine._note_window_state(fake, 121.0)
        engine._expire(121.0)
        self.assertEqual(len(closed), 1)

    def test_a_short_crop_is_warned_about_once(self) -> None:
        from mnmparse.app.engine import CROP_ROWS_FRAMES

        engine = self.engine()
        notes: list[str] = []
        engine.notice.connect(notes.append)
        fake = SimpleNamespace(prev=["x"] * 6, pitch=40.0)
        short = self.cfg(crop=(0, 60, 700, 420))  # 360 px: 9 rows
        with self.assertLogs("mnmparse.app.engine", "WARNING"):
            for _ in range(CROP_ROWS_FRAMES * 2):
                engine._check_crop_rows(short, fake)
        self.assertEqual(len(notes), 1)
        self.assertIn("9 chat lines", notes[0])
        engine.update_config(self.cfg(crop=(0, 60, 700, 660)))  # 600 px: 15 rows
        for _ in range(CROP_ROWS_FRAMES * 2):
            engine._check_crop_rows(engine.config, fake)
        self.assertEqual(len(notes), 1)

    def test_warning_text_and_overlay_pill(self) -> None:
        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.app.widgets import capture_warning

        short, detail = capture_warning({"scrolled_back": True})
        self.assertIn("scrolled up", short)
        self.assertIn("covered", capture_warning({"scrolled_back": True, "occluded_recent": True})[0].lower())
        settings = QSettings(str(Path(self.tmp) / "settings.ini"), QSettings.Format.IniFormat)
        overlay = OverlayWindow(settings, Config())
        try:
            overlay.set_status({"state": "running", "scrolled_back": True})
            self.assertTrue(overlay.warning_visible())
            overlay.set_status({"state": "running", "scrolled_back": False})
            self.assertFalse(overlay.warning_visible())
        finally:
            overlay.close()
            overlay.deleteLater()
            self.app.processEvents()

    def test_main_window_shows_it_in_the_status_bar_not_as_a_problem(self) -> None:
        from mnmparse.app.main import MainWindow, _MissingEngine

        settings = QSettings(str(Path(self.tmp) / "settings.ini"), QSettings.Format.IniFormat)
        win = MainWindow(_MissingEngine(Config()), None, Config(), settings)
        try:
            win.on_state("running")
            win.on_status({"state": "running", "scrolled_back": True})
            self.assertIn("scrolled up", win._state_label.text())
            self.assertFalse(win.warning_visible(), "no banner for it")
            win.on_status({"state": "running", "scrolled_back": False})
            self.assertNotIn("scrolled up", win._state_label.text())
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()


# ======================================================================================
# #7: everyday messages are not unreadable; every kind has a group and a colour
# ======================================================================================


class UnreadableTests(_EngineCase):
    EVERYDAY = (
        "You sell Cracked Staff for 5 silver coins.",
        "--You loot [Patched Rawhide Gloves] from your corpse.--",
        "You receive Ancient Chant from Elder Mirabeth.",
        "Gozif has leveled up! They are now level 7!",
        "a watchman says, \"Hail! How may I assist you?\"",
        "22 copper coins from a dunes madman's corpse as your split.",
        "Someone in your party is too high level for you to receive experience from this",
    )

    def test_everyday_messages_do_not_raise_the_banner(self) -> None:
        engine = self.engine()
        for i in range(30):
            self.feed(engine, (100.0 + i, self.EVERYDAY[i % len(self.EVERYDAY)]))
        statuses: list[dict] = []
        engine.status.connect(statuses.append)
        engine._emit_status()
        self.assertEqual(statuses[-1]["unreadable_pct"], 0.0)
        self.assertFalse(statuses[-1]["garbled"])
        for i in range(30):
            self.feed(engine, (200.0 + i, f"xq zzv qqq bbbb kkk {i}"))
        engine._emit_status()
        self.assertTrue(statuses[-1]["garbled"], "real garbage still does")

    def test_every_kind_has_a_feed_group_and_a_colour(self) -> None:
        from mnmparse.app import theme
        from mnmparse.app.pages import FEED_GROUPS

        grouped = [kind for _label, kinds in FEED_GROUPS for kind in kinds]
        self.assertEqual(len(grouped), len(set(grouped)), "each kind in one group")
        for kind in grammar.KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, grouped)
                self.assertIn(kind, theme.KIND_COLORS)


# ======================================================================================
# #36: short fights are copied too
# ======================================================================================


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class AutoCopyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    @staticmethod
    def snap(active: float) -> SimpleNamespace:
        row = SimpleNamespace(name=PLAYER, damage=40, dps=40.0, share=1.0, max_hit=20, hit_pct=100.0, heals=0,
                              hps=0.0, taken=0, utility=0, is_npc=False, in_group=True, is_you=True)
        return SimpleNamespace(label="a crocodile", zone="", duration=max(1.0, active), start=time.time(),
                               total_damage=40, raid_dps=40.0, killed=[], encounters=1, rows=[row], ours=True,
                               key="1.000", closed=True, active_duration=active, kills=0)

    def test_a_short_fight_is_copied_automatically(self) -> None:
        from PySide6.QtGui import QGuiApplication

        from mnmparse.app.main import App

        app = SimpleNamespace(cfg=Config(), overlay=None, window=None, triggers=None)
        app.copy_snapshot = lambda snap, automatic=False: App.copy_snapshot(app, snap, automatic=automatic)
        app.play_sound = lambda name: None
        clipboard = QGuiApplication.clipboard()
        clipboard.setText("before")
        App._on_encounter_closed_export(app, self.snap(1.0))
        self.assertTrue(clipboard.text().startswith("a crocodile"), "a quick kill is a real fight")
        clipboard.setText("before")
        App._on_encounter_closed_export(app, self.snap(6.0))
        self.assertTrue(clipboard.text().startswith("a crocodile"))


# ======================================================================================
# The views take a fight counted again; the Import page imports files together
# ======================================================================================


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class ViewUpdateTests(_EngineCase):
    def snaps(self) -> tuple[Any, Any]:
        engine = self.engine()
        closed: list[Any] = []
        engine.encounter_closed.connect(closed.append)
        self.feed(engine, (90.0, "Tovozen has joined the party."), *FIGHT)
        engine._expire(200.0)
        self.feed(engine, (210.0, "Wululiso has joined the party."))
        return closed[0], engine._maybe_rebuild_recent(now=215.0)[0]

    def test_live_page_replaces_the_row_and_the_zone_summary(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import LivePage

        old, new = self.snaps()
        settings = QSettings(str(Path(self.tmp) / "settings.ini"), QSettings.Format.IniFormat)
        page = LivePage(_MissingEngine(Config()), Config(), settings)
        try:
            page.show()
            page.add_encounter(old)
            page.select_zone(0)
            self.assertEqual(page.selected().total_damage, old.total_damage)
            page.update_encounter(new)
            page.select_key(new.key)
            self.assertEqual(page.selected().total_damage, 18)
            page.select_zone(0)
            self.assertEqual(page.selected().total_damage, 18, "the zone summary is not the cached one")
            self.assertEqual(page.listed_keys(), [new.key], "the same fight, not a second one")
        finally:
            page.close()
            page.deleteLater()
            self.app.processEvents()

    def test_overlay_replaces_the_shown_fight(self) -> None:
        from mnmparse.app.overlay import OverlayWindow

        old, new = self.snaps()
        settings = QSettings(str(Path(self.tmp) / "settings.ini"), QSettings.Format.IniFormat)
        overlay = OverlayWindow(settings, Config())
        try:
            overlay.set_snapshot(old)
            overlay._flush_snapshot()
            overlay.update_encounter(new)
            self.assertIs(overlay._snap, new)
            self.assertEqual([s.key for s in overlay.history()], [new.key])
            self.assertIs(overlay.history()[0], new)
        finally:
            overlay.close()
            overlay.deleteLater()
            self.app.processEvents()

    def test_import_page_imports_several_files_together(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import LivePage

        settings = QSettings(str(Path(self.tmp) / "settings.ini"), QSettings.Format.IniFormat)
        page = LivePage(_MissingEngine(Config()), Config(), settings)
        try:
            with mock.patch("mnmparse.importer.import_files", return_value=[]) as together, \
                    mock.patch("mnmparse.importer.import_file") as alone:
                page.import_files(["a.log", "b.log"])
            together.assert_called_once()
            self.assertEqual(together.call_args.args[0], ["a.log", "b.log"])
            alone.assert_not_called()
            with mock.patch("mnmparse.importer.import_files", side_effect=ValueError("bad")), \
                    mock.patch("mnmparse.importer.import_file", side_effect=OSError("gone")) as alone, \
                    self.assertLogs("mnmparse.app.pages", "ERROR"):
                page.import_files(["a.log", "b.log"])
            self.assertEqual(alone.call_count, 2, "falls back to one file at a time")
        finally:
            page.close()
            page.deleteLater()
            self.app.processEvents()


class SeedVocabTests(unittest.TestCase):
    def test_header_lines_are_not_learned(self) -> None:
        from mnmparse.app import engine as engine_mod

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "combat_2026-10-03_020229.log"
            log.write_text(raw_header() + "\n" + eq_timestamp(time.time()) + " Gozif has joined the party.\n",
                           encoding="utf-8")
            seen: list[str] = []
            with mock.patch("mnmparse.vocab.observe_event", lambda vocab, ev: seen.append(ev.text)), \
                    mock.patch.object(engine_mod.VOCAB, "save"):
                engine_mod._seed_vocab(Path(tmp) / engine_mod.VOCAB_FILE)
            self.assertEqual(seen, ["Gozif has joined the party."])


class CliParseTests(unittest.TestCase):
    def test_several_files_are_imported_together(self) -> None:
        import contextlib
        import io

        from mnmparse import cli, importer

        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, text in enumerate(("You crush a rat for 10 points of damage.", "You crush a rat for 12 points of damage.")):
                path = Path(tmp) / f"combat_2026-10-03_02022{i}.log"
                path.write_text(raw_header() + "\n" + eq_timestamp(1_790_000_000 + 60 * i) + " " + text + "\n",
                                encoding="utf-8")
                paths.append(str(path))
            args = cli.build_parser().parse_args(["parse", "--config", str(Path(tmp) / "none.json"), *paths])
            out = io.StringIO()
            with mock.patch.object(importer, "import_files", wraps=importer.import_files) as together, \
                    contextlib.redirect_stdout(out):
                code = cli.cmd_parse(args)
            self.assertEqual(code, cli.EXIT_OK)
            together.assert_called_once()
            self.assertEqual(out.getvalue().count("Session:"), 2, "one summary per file")


if __name__ == "__main__":
    unittest.main()
