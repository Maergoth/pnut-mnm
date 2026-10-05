"""OCR, preprocessing and config round-trip tests.

The OCR tests run Windows.Media.Ocr over a synthetic crop of the game's Combat
window (``tests/fixtures/ocr_sample.png``, 680x530, drawn by
``tests/fixtures/make_ocr_sample.py``: real captures show other players' names, so
they are not published) and are skipped where the Windows OCR runtime is unavailable.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mnmparse import ocr  # noqa: E402
from mnmparse.config import Config, load_config, save_config  # noqa: E402

FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "ocr_sample.png"

EXPECTED_SUBSTRINGS = (  # make_ocr_sample.EXPECTED
    "Stopped attacking.",
    "Tamsin begins casting Lesser Heal.",
    "Your party member Tamsin has slain a stumbling zombie!",
)


def _windows_ocr_available() -> bool:
    try:
        ocr.WindowsOcr()
    except ocr.OcrUnavailableError:
        return False
    return True


def _load_fixture() -> np.ndarray:
    img = cv2.imread(str(FIXTURE), cv2.IMREAD_COLOR)
    if img is None:
        raise FileNotFoundError(FIXTURE)
    return img


class PreprocessTests(unittest.TestCase):
    """``preprocess`` keeps/doubles the shape and returns 3-channel BGR."""

    def setUp(self) -> None:
        rng = np.random.default_rng(1234)
        self.img = rng.integers(0, 256, size=(53, 71, 3), dtype=np.uint8)

    def test_scale_one_preserves_shape_for_all_modes(self) -> None:
        for mode in ("none", "gray", "maxchannel"):
            with self.subTest(mode=mode):
                out = ocr.preprocess(self.img, mode, 1.0)
                self.assertEqual(out.shape, self.img.shape)
                self.assertEqual(out.dtype, np.uint8)

    def test_scale_two_doubles_shape_for_all_modes(self) -> None:
        h, w = self.img.shape[:2]
        for mode in ("none", "gray", "maxchannel"):
            with self.subTest(mode=mode):
                out = ocr.preprocess(self.img, mode, 2.0)
                self.assertEqual(out.shape, (2 * h, 2 * w, 3))

    def test_fixture_shapes(self) -> None:
        img = _load_fixture()
        self.assertEqual(img.shape, (530, 680, 3))
        self.assertEqual(ocr.preprocess(img, "maxchannel", 1.0).shape, (530, 680, 3))
        self.assertEqual(ocr.preprocess(img, "maxchannel", 2.0).shape, (1060, 1360, 3))

    def test_maxchannel_takes_brightest_channel(self) -> None:
        px = np.zeros((2, 2, 3), dtype=np.uint8)
        px[0, 0] = (10, 200, 30)
        px[1, 1] = (90, 20, 250)
        out = ocr.preprocess(px, "maxchannel", 1.0)
        self.assertEqual(tuple(out[0, 0]), (200, 200, 200))
        self.assertEqual(tuple(out[1, 1]), (250, 250, 250))
        self.assertEqual(tuple(out[0, 1]), (0, 0, 0))

    def test_gray_is_three_identical_channels(self) -> None:
        out = ocr.preprocess(self.img, "gray", 1.0)
        self.assertTrue(np.array_equal(out[:, :, 0], out[:, :, 1]))
        self.assertTrue(np.array_equal(out[:, :, 1], out[:, :, 2]))

    def test_none_leaves_pixels_unchanged(self) -> None:
        out = ocr.preprocess(self.img, "none", 1.0)
        self.assertTrue(np.array_equal(out, self.img))

    def test_accepts_gray_and_bgra_inputs(self) -> None:
        gray = self.img[:, :, 0]
        self.assertEqual(ocr.preprocess(gray, "none", 1.0).shape, (53, 71, 3))
        bgra = cv2.cvtColor(self.img, cv2.COLOR_BGR2BGRA)
        self.assertEqual(ocr.preprocess(bgra, "maxchannel", 1.0).shape, (53, 71, 3))

    def test_bad_arguments_raise(self) -> None:
        with self.assertRaises(ValueError):
            ocr.preprocess(self.img, "sepia", 1.0)
        with self.assertRaises(ValueError):
            ocr.preprocess(self.img, "none", 0.0)


@unittest.skipUnless(_windows_ocr_available(), "Windows.Media.Ocr is not available on this machine")
class WindowsOcrFixtureTests(unittest.TestCase):
    """Windows OCR over the real combat-window crop."""

    lines: list[ocr.OcrLine]
    img: np.ndarray

    @classmethod
    def setUpClass(cls) -> None:
        cls.img = _load_fixture()
        cfg = Config(ocr_engine="windows", ocr_scale=1.0, preprocess="maxchannel")
        engine = ocr.make_engine(cfg)
        cls.lines = engine.read(ocr.preprocess(cls.img, cfg.preprocess, cfg.ocr_scale))

    def _texts(self) -> list[str]:
        return [ln.text for ln in self.lines]

    def test_expected_lines_present(self) -> None:
        texts = self._texts()
        for expected in EXPECTED_SUBSTRINGS:
            with self.subTest(expected=expected):
                self.assertTrue(
                    any(expected in t for t in texts),
                    f"{expected!r} not found in OCR output:\n" + "\n".join(texts),
                )

    def test_at_least_ten_lines(self) -> None:
        self.assertGreaterEqual(len(self.lines), 10, self._texts())

    def test_lines_sorted_by_y_with_sane_boxes(self) -> None:
        h, w = self.img.shape[:2]
        ys = [ln.y for ln in self.lines]
        self.assertEqual(ys, sorted(ys))
        for ln in self.lines:
            self.assertIsInstance(ln, ocr.OcrLine)
            self.assertTrue(0 <= ln.x < w, ln)
            self.assertTrue(0 <= ln.y < h, ln)
            self.assertTrue(0 < ln.h <= h, ln)
            self.assertTrue(ln.text.strip(), ln)

    def test_repeated_reads_do_not_break_the_event_loop(self) -> None:
        engine = ocr.make_engine(Config(ocr_engine="windows"))
        prepped = ocr.preprocess(self.img, "maxchannel", 1.0)
        for _ in range(3):
            lines = engine.read(prepped)
            self.assertGreaterEqual(len(lines), 10)
        engine.close()
        self.assertGreaterEqual(len(engine.read(prepped)), 10)  # loop is recreated after close()

    def test_scale_two_coordinates_are_undone(self) -> None:
        engine = ocr.make_engine(Config(ocr_engine="windows", ocr_scale=2.0))
        lines2 = engine.read(ocr.preprocess(self.img, "maxchannel", 2.0))
        h, w = self.img.shape[:2]
        self.assertGreaterEqual(len(lines2), 10)
        for ln in lines2:
            self.assertTrue(0 <= ln.y < h, ln)
            self.assertTrue(0 <= ln.x < w, ln)
        target = "Stopped attacking."
        y1 = next(ln.y for ln in self.lines if target in ln.text)
        y2 = next(ln.y for ln in lines2 if target in ln.text)
        self.assertLessEqual(abs(y1 - y2), 4, (y1, y2))

    def test_empty_image_gives_no_lines(self) -> None:
        engine = ocr.make_engine(Config(ocr_engine="windows"))
        self.assertEqual(engine.read(np.zeros((0, 0, 3), dtype=np.uint8)), [])


class ConfigRoundTripTests(unittest.TestCase):
    """``save_config`` followed by ``load_config`` reproduces the dataclass."""

    def test_round_trip(self) -> None:
        cfg = Config(
            window_title="Monsters and Memories",
            capture_backend="mss",
            crop=(5, 70, 690, 590),
            fps=2.5,
            ocr_engine="rapid",
            ocr_scale=1.5,
            preprocess="gray",
            log_dir="somewhere/logs",
            player_name="Wululiso",
            encounter_timeout_s=20.0,
            stats_interval_s=3.0,
            overlay_enabled=True,
            overlay_opacity=0.6,
            overlay_locked=False,
            overlay_font_scale=1.25,
            overlay_tab="healing",
            start_capture_on_launch=True,
            minimize_to_tray=False,
            feed_max_lines=250,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "config.json")
            save_config(cfg, path)
            self.assertTrue(os.path.isfile(path))
            self.assertFalse(os.path.exists(path + ".tmp"))
            loaded = load_config(path)
        self.assertEqual(loaded, cfg)
        self.assertIsInstance(loaded.crop, tuple)
        self.assertEqual(loaded.crop, (5, 70, 690, 590))

    def test_defaults_match_spec(self) -> None:
        cfg = Config()
        self.assertEqual(cfg.crop, (0, 60, 700, 600))
        self.assertEqual(cfg.capture_backend, "wgc")
        self.assertEqual(cfg.ocr_engine, "windows")
        self.assertEqual(cfg.preprocess, "maxchannel")
        self.assertEqual(cfg.ocr_scale, 1.0)
        self.assertEqual(cfg.problems(), [])
        # APP_SPEC section 9: desktop-app defaults (overlay off and locked).
        self.assertFalse(cfg.overlay_enabled)
        self.assertTrue(cfg.overlay_locked)
        self.assertEqual(cfg.overlay_opacity, 0.85)
        self.assertEqual(cfg.overlay_font_scale, 1.0)
        self.assertEqual(cfg.overlay_tab, "damage")
        self.assertTrue(cfg.start_capture_on_launch)  # capture starts with the app
        self.assertTrue(cfg.minimize_to_tray)
        self.assertEqual(cfg.feed_max_lines, 500)

    def test_example_config_matches_fields(self) -> None:
        """config.example.json lists every Config field and loads without warnings."""
        import json
        from dataclasses import fields

        example = PROJECT_ROOT / "config.example.json"
        with open(example, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(set(data), {f.name for f in fields(Config)})
        loaded = load_config(str(example))
        self.assertEqual(loaded, Config())
        self.assertEqual(loaded.problems(), [])

    def test_bool_fields_accept_strings_and_ints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"overlay_enabled": "true", "minimize_to_tray": 0, "overlay_locked": "nope"}')
            with self.assertLogs("mnmparse.config", level="WARNING"):
                loaded = load_config(path)
        self.assertIs(loaded.overlay_enabled, True)
        self.assertIs(loaded.minimize_to_tray, False)
        self.assertIs(loaded.overlay_locked, True)  # bad value -> default kept

    def test_missing_file_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_config(os.path.join(tmp, "nope.json")), Config())

    def test_partial_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"fps": 8, "crop": [1, 2, 300, 400], "bogus": 1, "ocr_scale": "oops"}')
            with self.assertLogs("mnmparse.config", level="WARNING"):
                loaded = load_config(path)
        self.assertEqual(loaded.fps, 8.0)
        self.assertIsInstance(loaded.fps, float)
        self.assertEqual(loaded.crop, (1, 2, 300, 400))
        self.assertEqual(loaded.ocr_scale, 1.0)  # bad value -> default kept
        self.assertEqual(loaded.window_title, Config().window_title)


if __name__ == "__main__":
    unittest.main()
