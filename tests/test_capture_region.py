"""WgcWindowSource: Windows is asked for no more frames than are kept, and once a crop is
asked for, frames copy only that rectangle (whole frames on demand).  No window needed: the
library callback is driven with fake frames."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace

import numpy as np

from mnmparse.capture import WgcWindowSource


def _frame(h: int = 2160, w: int = 3840, value: int = 7) -> SimpleNamespace:
    buf = np.full((h, w, 4), value, dtype=np.uint8)
    buf[100:110, 200:230, :3] = (1, 2, 3)  # a marker inside the crop
    return SimpleNamespace(frame_buffer=buf)


class _Control:
    def stop(self) -> None:  # pragma: no cover - not reached in these tests
        pass


class CaptureRegionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.src = WgcWindowSource("x", max_fps=12.0)
        self.src._thread = SimpleNamespace(is_alive=lambda: True)  # "running" without a session

    def feed(self, frame: SimpleNamespace | None = None) -> None:
        self.src._last_kept = 0.0  # let the throttle pass
        self.src._on_frame(frame or _frame(), _Control())

    def test_windows_is_asked_for_no_more_frames_than_kept(self) -> None:
        from unittest import mock

        with mock.patch("mnmparse.capture._supports_min_update_interval", return_value=True):
            self.assertEqual(self.src.capture_kwargs()["minimum_update_interval"], 83)
            self.assertNotIn("minimum_update_interval", WgcWindowSource("x").capture_kwargs())
        with mock.patch("mnmparse.capture._supports_min_update_interval", return_value=False):
            self.assertNotIn("minimum_update_interval", self.src.capture_kwargs())  # Windows 11 23H2 refuses it

    def test_whole_frames_until_a_crop_is_asked_for(self) -> None:
        self.feed()
        self.assertEqual(self.src._frame.shape, (2160, 3840, 3))
        self.assertIsNone(self.src._region_frame)

    def test_region_mode_copies_only_the_crop(self) -> None:
        self.feed()
        first = self.src.latest_region((0, 0, 730, 873))  # cut from the last whole frame
        self.assertEqual(first.shape, (873, 730, 3))
        before = self.src._frame
        self.feed(_frame(value=9))
        part = self.src.latest_region((0, 0, 730, 873))
        self.assertEqual(part.shape, (873, 730, 3))
        self.assertEqual(tuple(part[105, 210]), (1, 2, 3))
        self.assertEqual(tuple(part[0, 0]), (9, 9, 9), "the crop comes from the new frame")
        self.assertIs(self.src._frame, before, "no whole-frame copy in region mode")
        part[0, 0] = (0, 0, 0)
        self.assertEqual(tuple(self.src.latest_region((0, 0, 730, 873))[0, 0]), (9, 9, 9), "callers get a copy")

    def test_whole_frame_on_demand_in_region_mode(self) -> None:
        self.feed()
        self.src.latest_region((0, 0, 730, 873))
        self.feed(_frame(value=9))
        got: list[np.ndarray | None] = []
        t = threading.Thread(target=lambda: got.append(self.src.latest()))
        t.start()
        deadline = time.monotonic() + 2
        while not self.src._full_wanted.is_set() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.feed(_frame(value=11))  # the next frame is copied whole for the waiting caller
        t.join(2)
        self.assertEqual(got[0].shape, (2160, 3840, 3))
        self.assertEqual(int(got[0][0, 0, 0]), 11)
        self.assertFalse(self.src._full_wanted.is_set())
        self.assertEqual(tuple(self.src.latest_region((0, 0, 730, 873))[0, 0]), (11, 11, 11))

    def test_a_crop_outside_the_frame_raises(self) -> None:
        self.feed(_frame(h=400, w=400))
        with self.assertRaises(ValueError):  # switching to it cuts from the stored frame
            self.src.latest_region((500, 500, 600, 600))
        self.feed(_frame(h=400, w=400))
        with self.assertRaises(ValueError):  # and from then on from each new frame
            self.src.latest_region((500, 500, 600, 600))

    def test_throttle_skips_frames_between_kept_ones(self) -> None:
        self.feed()
        self.src.latest_region((0, 0, 730, 873))
        self.feed()
        kept = self.src.frame_count
        self.src._on_frame(_frame(), _Control())  # immediately after: dropped without a copy
        self.assertEqual(self.src.frame_count, kept)


if __name__ == "__main__":
    unittest.main()
