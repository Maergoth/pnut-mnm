"""Independent stat samples peek native crops without changing chat capture."""
from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from mnmparse.capture import MssRegionSource, WgcWindowSource


CHAT = (0, 0, 4, 4)
STATS = (8, 0, 12, 4)


class NativeStatSampleTests(unittest.TestCase):
    def setUp(self):
        self.source = WgcWindowSource("fixture")
        self.source._thread = SimpleNamespace(is_alive=lambda: True)
        self.source._regions = (CHAT, STATS)
        self.feed(7, 101.25)

    def feed(self, value, captured_at):
        pixels = np.full((4, 12, 4), value, dtype=np.uint8)
        with patch("mnmparse.capture.time.monotonic", return_value=captured_at):
            self.source._on_frame(SimpleNamespace(frame_buffer=pixels), Mock())

    def test_copied_crop_count_and_time_refer_to_one_native_frame(self):
        image, count, captured_at = self.source.region_snapshot(STATS)
        self.assertEqual((count, captured_at), (1, 101.25))
        self.assertEqual(image.shape, (4, 4, 3))
        self.assertTrue(np.all(image == 7))
        self.assertFalse(np.shares_memory(image, self.source._regions_frames[1]))
        image[:] = 99
        self.assertTrue(np.all(self.source.region_snapshot(STATS)[0] == 7))
        self.feed(23, 102.5)
        image, count, captured_at = self.source.region_snapshot(STATS)
        self.assertEqual((count, captured_at), (2, 102.5))
        self.assertTrue(np.all(image == 23))

    def test_peeking_never_reconfigures_or_requests_full_frame(self):
        regions = self.source._regions
        full = self.source._frame
        with patch.object(self.source, "latest_regions", side_effect=AssertionError("reconfigure")), \
                patch.object(self.source, "latest", side_effect=AssertionError("full frame")), \
                patch.object(self.source, "_check_window", side_effect=AssertionError("window lookup")):
            self.assertIsNotNone(self.source.region_snapshot(STATS)[0])
            self.assertEqual(self.source.region_snapshot((1, 1, 3, 3)), (None, None, 0.0))
        self.assertIs(self.source._regions, regions)
        self.assertIs(self.source._frame, full)
        self.assertFalse(self.source._full_wanted.is_set())

    def test_new_region_configuration_waits_for_its_native_frame(self):
        new_crop = (4, 0, 8, 4)
        self.source._regions = (CHAT, new_crop)
        self.assertEqual(self.source.region_snapshot(new_crop), (None, None, 0.0))
        self.assertEqual(self.source.region_snapshot(STATS), (None, None, 0.0))
        self.feed(41, 103)
        self.assertTrue(np.all(self.source.region_snapshot(new_crop)[0] == 41))

    def test_missing_empty_or_incomplete_cache_returns_no_sample(self):
        cases = ({"_regions_cut": None}, {"_regions_frames": ()},
                 {"_regions_errors": ()}, {"_regions_frames": (None, None)},
                 {"_frame_count": 0}, {"_last_kept": 0.0})
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                with patch.multiple(self.source, **changes):
                    self.assertEqual(self.source.region_snapshot(STATS), (None, None, 0.0))

    def test_closed_stopping_dead_or_failed_session_never_replays_cached_pixels(self):
        cases = ({"_closed": True}, {"_stop_requested": True}, {"_thread": None},
                 {"_thread": SimpleNamespace(is_alive=lambda: False)},
                 {"_error": RuntimeError("native capture stopped")})
        for changes in cases:
            with self.subTest(changes=tuple(changes)):
                with patch.multiple(self.source, **changes):
                    self.assertEqual(self.source.region_snapshot(STATS), (None, None, 0.0))

    def test_only_requested_crop_error_is_reported(self):
        self.source._regions_errors = ("bad chat crop", None)
        self.assertIsNotNone(self.source.region_snapshot(STATS)[0])
        with self.assertRaisesRegex(ValueError, "bad chat crop"):
            self.source.region_snapshot(CHAT)
        with self.assertRaises(ValueError):
            self.source.region_snapshot((1, 2, 3))

    def test_copy_and_metadata_remain_atomic_while_next_frame_arrives(self):
        entered, release, attempted, updated = (threading.Event() for _ in range(4))
        source = self.source

        class SlowCopy(np.ndarray):
            def copy(self, *args, **kwargs):
                self.assert_locked = source._lock.locked()
                entered.set()
                if not release.wait(2):
                    raise AssertionError("test did not release crop copy")
                return super().copy(*args, **kwargs)

        slow = source._regions_frames[1].view(SlowCopy)
        source._regions_frames = (source._regions_frames[0], slow)
        result = []
        reader = threading.Thread(target=lambda: result.append(source.region_snapshot(STATS)))

        def update():
            attempted.set()
            self.feed(59, 105)
            updated.set()

        writer = threading.Thread(target=update)
        try:
            reader.start()
            self.assertTrue(entered.wait(2))
            writer.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(updated.is_set())
        finally:
            release.set()
            reader.join(2)
            if writer.ident is not None:
                writer.join(2)
        self.assertFalse(reader.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertTrue(slow.assert_locked)
        image, count, captured_at = result[0]
        self.assertEqual((count, captured_at), (1, 101.25))
        self.assertTrue(np.all(image == 7))
        image, count, captured_at = source.region_snapshot(STATS)
        self.assertEqual((count, captured_at), (2, 105))
        self.assertTrue(np.all(image == 59))


class DesktopStatSampleTests(unittest.TestCase):
    def test_desktop_sample_uses_fresh_grab_and_start_timestamp(self):
        source = MssRegionSource("fixture")
        pixels = np.full((4, 4, 3), 31, dtype=np.uint8)
        with patch("mnmparse.capture.time.monotonic", return_value=205.5), \
                patch.object(source, "latest_region", return_value=pixels) as grab:
            image, count, captured_at = source.region_snapshot(STATS)
        grab.assert_called_once_with(STATS)
        self.assertIs(image, pixels)
        self.assertIsNone(count)
        self.assertEqual(captured_at, 205.5)

    def test_desktop_missing_window_is_unavailable(self):
        source = MssRegionSource("fixture")
        with patch("mnmparse.capture.time.monotonic", return_value=208), \
                patch.object(source, "_current_rect", return_value=None), patch("mss.mss") as factory:
            self.assertEqual(source.region_snapshot(STATS), (None, None, 208))
        factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
