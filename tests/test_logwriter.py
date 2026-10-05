"""Log segment rotation: time-stamped names, idle-gap and size splits, paired files."""

from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path

from mnmparse.logwriter import LogWriter
from mnmparse.parser import parse_line


@dataclass
class Msg:
    text: str
    first_seen: float
    frames_seen: int = 2
    fragment: bool = False


T0 = time.mktime((2026, 10, 2, 1, 57, 1, 0, 0, -1))


def _write(writer: LogWriter, ts: float, text: str = "You crush a crocodile hatchling for 22 points of damage.") -> None:
    writer.write_raw(Msg(text, ts))
    writer.write_event(parse_line(text, ts, "Maergoth"))


class LogWriterTests(unittest.TestCase):
    def test_names_carry_the_first_message_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp) as w:
                _write(w, T0)
                self.assertEqual(w.raw_path.name, "combat_2026-10-02_015701.log")
                self.assertEqual(w.events_path.name, "events_2026-10-02_015701.jsonl")
                _write(w, T0 + 5)
            self.assertEqual(len(w.segments), 1)
            # the format header, then the two messages
            self.assertEqual(len(w.raw_path.read_text(encoding="utf-8").splitlines()), 3)
            self.assertEqual(len(w.events_path.read_text(encoding="utf-8").splitlines()), 3)

    def test_idle_gap_starts_a_new_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp, break_s=3600.0) as w:
                _write(w, T0)
                _write(w, T0 + 3599)  # still the same segment
                self.assertEqual(len(w.segments), 1)
                _write(w, T0 + 3599 + 3601)  # more than an hour later
                self.assertEqual(len(w.segments), 2)
                self.assertEqual(w.raw_path.name, "combat_2026-10-02_035701.log")
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(
                names,
                ["combat_2026-10-02_015701.log", "combat_2026-10-02_035701.log",
                 "events_2026-10-02_015701.jsonl", "events_2026-10-02_035701.jsonl"],
            )

    def test_size_cap_starts_a_new_segment_and_keeps_pairs_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp, max_bytes=300) as w:
                for i in range(12):
                    _write(w, T0 + i)
                self.assertGreater(len(w.segments), 1)
                for raw, events in w.segments:
                    self.assertEqual(
                        len(raw.read_text(encoding="utf-8").splitlines()),
                        len(events.read_text(encoding="utf-8").splitlines()),
                        f"{raw.name} and {events.name} must hold the same messages",
                    )

    def test_out_of_order_timestamps_do_not_rotate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with LogWriter(tmp) as w:
                _write(w, T0 + 100)
                _write(w, T0 + 90)  # a held line flushed with an earlier first_seen
                self.assertEqual(len(w.segments), 1)


if __name__ == "__main__":
    unittest.main()
