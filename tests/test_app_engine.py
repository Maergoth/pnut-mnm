"""Regression tests for the engine's stop path (``Engine._finish``).

Stop capture must (a) close the open encounter and announce it through
``encounter_closed`` plus a closed ``snapshot`` so it reaches History and the views
show "ended", and (b) flush the tracker and close the log writer BEFORE the frame
source is stopped (``WgcWindowSource.stop`` may take seconds).

No capture or OCR runs here: the pipeline objects are installed directly and the
frame source is a fake.  Qt is only needed for the ``QObject`` signals (a
``QApplication`` is created because unittest discovery shares the process with the
GUI tests, which need one).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # real Windows fonts on the offscreen platform (realistic text widths)
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtWidgets import QApplication  # noqa: E402

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False

from mnmparse.config import Config  # noqa: E402
from mnmparse.logwriter import LogWriter  # noqa: E402
from mnmparse.parser import parse_line  # noqa: E402
from mnmparse.stats import Stats  # noqa: E402
from mnmparse.tracker import Tracker  # noqa: E402

PLAYER = "Pidef"
FIGHT = (
    (100.0, "You crush a stumbling zombie for 10 points of damage."),
    (101.0, "a stumbling zombie bites YOU for 5 points of damage."),
    (102.0, "Tovozen crushes a stumbling zombie for 2 points of damage."),
)


class _FakeSource:
    """Frame source stand-in that records whether the writer was already closed."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine
        self.stopped = False
        self.writer_closed_first: bool | None = None

    def latest(self) -> None:
        return None

    def stop(self) -> None:
        self.stopped = True
        self.writer_closed_first = self._engine._writer is None


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class EngineFinishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # A full QApplication: test discovery shares one process with the Qt GUI tests.
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def _engine(self, tmp: str) -> Any:
        from mnmparse.app.engine import Engine

        cfg = Config(player_name=PLAYER, log_dir=tmp, encounter_timeout_s=12.0)
        engine = Engine(cfg)
        engine._tracker = Tracker()
        engine._stats = Stats(cfg.encounter_timeout_s)
        engine._writer = LogWriter(tmp)
        for ts, line in FIGHT:
            engine._stats.add(parse_line(line, ts, PLAYER))
        return engine

    def test_stop_closes_open_encounter_and_announces_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            self.assertIsNotNone(engine._stats.current())
            closed: list[Any] = []
            snaps: list[Any] = []
            states: list[str] = []
            engine.encounter_closed.connect(closed.append)
            engine.snapshot.connect(snaps.append)
            engine.state_changed.connect(states.append)

            engine._finish()

            self.assertEqual(len(closed), 1, "the open fight must be closed on stop")
            snap = closed[0]
            self.assertTrue(snap.closed)
            self.assertEqual(snap.total_damage, 12, "the group's damage (the mob's 5 does not count)")
            self.assertEqual([s.key for s in snaps], [snap.key], "a closed snapshot is sent to the views")
            self.assertTrue(snaps[0].closed)
            self.assertEqual([s.key for s in engine.history()], [snap.key], "it is in history for export")
            self.assertEqual(engine.state, "stopped")
            self.assertNotIn("running", states)
            self.assertIsNone(engine._stats)
            self.assertIsNone(engine._writer)

    def test_stop_without_open_encounter_emits_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            engine._stats.add(parse_line("You have slain a stumbling zombie!", 103.0, PLAYER))
            engine._stats.expire(1000.0)  # the timeout ends the fight (a kill alone does not)
            self.assertIsNone(engine._stats.current())
            already = len(engine._stats.history)
            closed: list[Any] = []
            engine.encounter_closed.connect(closed.append)
            engine._finish()
            self.assertEqual(closed, [])
            self.assertEqual(already, 1)
            self.assertEqual(engine.history(), [], "kills closed before stop were announced when they happened")

    def test_flush_and_writer_close_before_source_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            source = _FakeSource(engine)
            engine._source = source
            engine._finish()
            self.assertTrue(source.stopped)
            self.assertTrue(
                source.writer_closed_first,
                "the log writer must be closed (lines flushed) before the slow source stop",
            )
            self.assertIsNone(engine._source)

    def test_reset_encounter_uses_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = self._engine(tmp)
            closed: list[Any] = []
            engine.encounter_closed.connect(closed.append)
            engine.reset_encounter()
            self.assertEqual(len(closed), 1)
            self.assertIsNone(engine._stats.current())
            engine.reset_encounter()  # nothing open: no second emission
            self.assertEqual(len(closed), 1)
            engine._writer.close()

    def test_join_timeout_covers_source_stop(self) -> None:
        from mnmparse.app import engine as engine_mod

        # WgcWindowSource.stop joins its thread for up to 2 s; one frame at the default
        # fps plus the flush must fit as well.
        self.assertGreaterEqual(engine_mod.STOP_JOIN_TIMEOUT_S, 2.0 + 1.0 / Config().fps + 1.0)


if __name__ == "__main__":
    unittest.main()
