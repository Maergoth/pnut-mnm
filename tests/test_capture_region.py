"""WgcWindowSource: Windows is asked for no more frames than are kept, and once a crop is
asked for, frames copy only that rectangle (whole frames on demand).  No window needed: the
library callback is driven with fake frames."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from mnmparse.capture import MssRegionSource, WgcWindowSource


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

    def test_multi_region_mode_copies_disjoint_crops_from_one_frame(self) -> None:
        copies = []

        class TrackedBuffer(np.ndarray):
            def copy(self, *args, **kwargs):
                copies.append(self.shape)
                return super().copy(*args, **kwargs)

        self.feed()
        regions = ((0, 0, 730, 873), (3200, 100, 3700, 300))
        self.src.latest_regions(regions)
        before = self.src._frame
        frame = _frame(value=19)
        frame.frame_buffer = frame.frame_buffer.view(TrackedBuffer)
        frame.frame_buffer[100:300, 3200:3700, :3] = (4, 5, 6)
        copies.clear()
        self.feed(frame)
        self.assertEqual(copies, [], "Native BGRA crops need no whole-frame or union BGR copy")
        chat, status = self.src.latest_regions(regions)
        self.assertEqual(chat.shape, (873, 730, 3))
        self.assertEqual(status.shape, (200, 500, 3))
        self.assertEqual(tuple(chat[0, 0]), (19, 19, 19))
        self.assertEqual(tuple(status[0, 0]), (4, 5, 6))
        self.assertIs(self.src._frame, before)
        self.assertEqual(self.src.full_frame_dimensions, (3840, 2160))
        self.assertFalse(np.shares_memory(chat, frame.frame_buffer))
        self.assertFalse(np.shares_memory(status, frame.frame_buffer))
        chat[0, 0] = status[0, 0] = 0
        again = self.src.latest_regions(regions)
        self.assertEqual(tuple(again[0][0, 0]), (19, 19, 19))
        self.assertEqual(tuple(again[1][0, 0]), (4, 5, 6))

    def test_changed_regions_wait_for_new_frame_instead_of_replaying_stale_whole_frame(self) -> None:
        self.feed()
        self.src.latest_region((0, 0, 730, 873))
        self.src._frame_at = time.monotonic() - 10
        regions = ((0, 0, 730, 873), (3200, 100, 3700, 300))
        self.assertEqual(self.src.latest_regions(regions), (None, None))
        self.feed(_frame(value=29))
        self.assertTrue(all(int(part[0, 0, 0]) == 29 for part in self.src.latest_regions(regions)))
        self.assertIsNone(self.src.latest_region((0, 0, 730, 873)))
        self.feed(_frame(value=31))
        self.assertEqual(int(self.src.latest_region((0, 0, 730, 873))[0, 0, 0]), 31)

    def test_whole_frame_on_demand_in_region_mode(self) -> None:
        for regions in (((0, 0, 730, 873),), ((0, 0, 730, 873), (3200, 100, 3700, 300))):
            with self.subTest(region_count=len(regions)):
                self.feed()
                self.src.latest_regions(regions)
                self.feed(_frame(value=9))
                got: list[np.ndarray | None] = []
                t = threading.Thread(target=lambda: got.append(self.src.latest()))
                t.start()
                deadline = time.monotonic() + 2
                while not self.src._full_wanted.is_set() and time.monotonic() < deadline:
                    time.sleep(0.005)
                self.feed(_frame(value=11))  # next frame serves whole-frame caller and every crop
                t.join(2)
                self.assertFalse(t.is_alive())
                self.assertEqual(got[0].shape, (2160, 3840, 3))
                self.assertEqual(int(got[0][0, 0, 0]), 11)
                self.assertFalse(self.src._full_wanted.is_set())
                self.assertTrue(all(tuple(part[0, 0]) == (11, 11, 11) for part in self.src.latest_regions(regions)))

    def test_a_crop_outside_the_frame_raises(self) -> None:
        self.feed(_frame(h=400, w=400))
        with self.assertRaises(ValueError):  # switching to it cuts from the stored frame
            self.src.latest_region((500, 500, 600, 600))
        self.feed(_frame(h=400, w=400))
        with self.assertRaises(ValueError):  # and from then on from each new frame
            self.src.latest_region((500, 500, 600, 600))

    def test_invalid_second_crop_and_closed_multi_source_preserve_validation(self) -> None:
        self.feed(_frame(h=400, w=400))
        regions = ((0, 0, 100, 100), (500, 500, 600, 600))
        with self.assertRaises(ValueError):
            self.src.latest_regions(regions)
        self.feed(_frame(h=400, w=400))
        with self.assertRaises(ValueError):
            self.src.latest_regions(regions)
        self.src._closed = True
        self.assertEqual(self.src.latest_regions(regions), (None, None))
        self.assertIsNone(self.src.latest_region(regions[0]))

    def test_throttle_skips_frames_between_kept_ones(self) -> None:
        self.feed()
        self.src.latest_region((0, 0, 730, 873))
        self.feed()
        kept = self.src.frame_count
        self.src._on_frame(_frame(), _Control())  # immediately after: dropped without a copy
        self.assertEqual(self.src.frame_count, kept)


class MssCaptureRegionTests(unittest.TestCase):
    def test_two_crops_use_one_desktop_grab_and_private_bgr_copies(self) -> None:
        source = MssRegionSource("x")
        buf = np.full((400, 400, 4), 17, dtype=np.uint8)
        buf[300:350, 300:350, :3] = (1, 2, 3)
        shot = SimpleNamespace(bgra=buf.tobytes(), width=400, height=400)
        grabber = Mock()
        grabber.grab.return_value = shot
        with patch.object(source, "_current_rect", return_value=(100, 200, 500, 600)), patch("mss.mss") as factory:
            factory.return_value.__enter__.return_value = grabber
            chat, status = source.latest_regions(((0, 0, 50, 60), (300, 300, 350, 350)))
            grabber.grab.assert_called_once_with({"left": 100, "top": 200, "width": 400, "height": 400})
            self.assertEqual(chat.shape, (60, 50, 3))
            self.assertEqual(status.shape, (50, 50, 3))
            self.assertEqual(tuple(chat[0, 0]), (17, 17, 17))
            self.assertEqual(tuple(status[0, 0]), (1, 2, 3))
            self.assertTrue(chat.flags.c_contiguous and status.flags.c_contiguous)
            self.assertFalse(np.shares_memory(chat, status))
            status[0, 0] = 0
            self.assertEqual(tuple(source.latest_region((300, 300, 350, 350))[0, 0]), (1, 2, 3))

    def test_missing_window_and_empty_crop_do_not_return_unrelated_pixels(self) -> None:
        source = MssRegionSource("x")
        with patch.object(source, "_current_rect", return_value=None), patch("mss.mss") as factory:
            self.assertEqual(source.latest_regions(((0, 0, 50, 60), (300, 300, 350, 350))), (None, None))
            factory.assert_not_called()
        buf = np.zeros((10, 10, 4), dtype=np.uint8)
        shot = SimpleNamespace(bgra=buf.tobytes(), width=10, height=10)
        with patch.object(source, "_current_rect", return_value=(0, 0, 10, 10)), patch("mss.mss") as factory:
            factory.return_value.__enter__.return_value.grab.return_value = shot
            with self.assertRaises(ValueError):
                source.latest_regions(((0, 0, 5, 5), (20, 20, 30, 30)))


if __name__ == "__main__":
    unittest.main()
