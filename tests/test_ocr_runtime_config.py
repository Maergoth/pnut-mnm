"""Saved OCR scale must not change the running engine's coordinate system.

OCR engine and scale changes apply on the next capture start. Crop dimensions and
tracker pitch are already in original game-window pixels, regardless of OCR scale.
The tests use the real preprocessing path and isolated stand-ins, with no capture
or personal settings/logs.
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    import numpy as np
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - optional desktop dependencies
    HAVE_QT = False

from mnmparse.config import Config  # noqa: E402
from mnmparse.tracker import Tracker  # noqa: E402


class _RecordingOcr:
    """An active OCR engine with its original coordinate-unscale factor."""

    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.shapes: list[tuple[int, ...]] = []

    def read(self, image: np.ndarray) -> list:
        self.shapes.append(image.shape)
        return []


@unittest.skipUnless(HAVE_QT, "Desktop dependencies unavailable")
class OcrRuntimeConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def engine(self, cfg: Config):
        from mnmparse.app.engine import Engine

        # Engine initialization normally learns vocabulary from personal logs.
        with mock.patch("mnmparse.app.engine._load_vocab_once"):
            return Engine(cfg)

    def prepare_pipeline(self, engine, cfg: Config, ocr: _RecordingOcr) -> None:
        # Keep the real run initialization, including active OCR configuration,
        # while replacing only personal state and log-file dependencies.
        with (
            mock.patch.object(engine, "_start_tracker", return_value=(Tracker(), True)),
            mock.patch.object(engine, "_load_session_state", return_value=""),
            mock.patch.object(engine, "_install_stats"),
            mock.patch("mnmparse.logwriter.LogWriter"),
        ):
            engine._prepare_run(cfg, ocr)

    def test_saved_scale_change_keeps_preprocessing_in_active_coordinate_scale(self) -> None:
        for active_scale, saved_scale in ((1.0, 2.0), (2.0, 1.0), (1.0, 0.5)):
            for cropped in (False, True):
                with self.subTest(active_scale=active_scale, saved_scale=saved_scale, cropped=cropped):
                    cfg = Config(crop=(0, 0, 80, 60), ocr_scale=active_scale, preprocess="none")
                    engine = self.engine(cfg)
                    active_ocr = _RecordingOcr(active_scale)
                    self.prepare_pipeline(engine, cfg, active_ocr)
                    engine.update_config(replace(cfg, ocr_scale=saved_scale))

                    engine._process_frame(np.zeros((60, 80, 3), dtype=np.uint8), engine.config, cropped=cropped)

                    self.assertEqual(engine.config.ocr_scale, saved_scale, "The new preference must be retained")
                    self.assertIs(engine._ocr, active_ocr, "Running OCR is replaced on the next capture start")
                    self.assertEqual(
                        active_ocr.shapes,
                        [(round(60 * active_scale), round(80 * active_scale), 3)],
                        "The image scale must agree with the active OCR engine's coordinate-unscale factor",
                    )

    def test_tracker_state_uses_active_ocr_settings_until_the_next_start(self) -> None:
        from mnmparse.app.engine import TRACKER_STATE_FILE

        with tempfile.TemporaryDirectory() as folder:
            cfg = Config(crop=(0, 0, 80, 60), ocr_scale=1.0, ocr_engine="windows", log_dir=folder)
            engine = self.engine(cfg)
            self.prepare_pipeline(engine, cfg, _RecordingOcr(1.0))
            saved = replace(cfg, ocr_scale=2.0, ocr_engine="rapid")
            engine.update_config(saved)

            engine._save_state(engine.config, engine._tracker, "")
            state = json.loads((Path(folder) / TRACKER_STATE_FILE).read_text(encoding="utf-8"))
            expected_active_key = [list(cfg.crop), 1.0, "windows"]
            self.assertEqual(
                state["capture"], expected_active_key,
                "Geometry measured by Windows OCR at 1x must not be saved as pending RapidOCR at 2x",
            )

            with mock.patch.object(engine, "_save_vocab"):
                engine._finish()
            self.assertEqual(engine._kept_tracker[0], expected_active_key)

            next_ocr = _RecordingOcr(saved.ocr_scale)
            self.prepare_pipeline(engine, engine.config, next_ocr)
            engine._process_frame(np.zeros((60, 80, 3), dtype=np.uint8), engine.config)
            self.assertEqual(next_ocr.shapes, [(120, 160, 3)], "The next start adopts the saved scale")
            engine._save_state(engine.config, engine._tracker, "")
            next_state = json.loads((Path(folder) / TRACKER_STATE_FILE).read_text(encoding="utf-8"))
            self.assertEqual(next_state["capture"], [list(cfg.crop), 2.0, "rapid"])

    def test_short_crop_warning_counts_unscaled_rows_at_every_ocr_scale(self) -> None:
        from mnmparse.app.engine import CROP_ROWS_FRAMES

        for scale in (0.5, 1.0, 2.0):
            with self.subTest(scale=scale):
                cfg = Config(crop=(0, 60, 700, 460), ocr_scale=scale)
                engine = self.engine(cfg)
                notices: list[str] = []
                engine.notice.connect(notices.append)
                tracker = SimpleNamespace(prev=["known row"] * 6, pitch=40.0)

                for _ in range(CROP_ROWS_FRAMES * 2):
                    engine._check_crop_rows(cfg, tracker)

                self.assertEqual(len(notices), 1, "A 400px crop with 40px pitch always holds only ten rows")
                self.assertIn("10 chat lines", notices[0])

    def test_tall_crop_does_not_warn_when_ocr_downscales_pixels(self) -> None:
        from mnmparse.app.engine import CROP_ROWS_FRAMES

        cfg = Config(crop=(0, 60, 700, 660), ocr_scale=0.5)
        engine = self.engine(cfg)
        notices: list[str] = []
        engine.notice.connect(notices.append)
        tracker = SimpleNamespace(prev=["known row"] * 6, pitch=40.0)

        for _ in range(CROP_ROWS_FRAMES):
            engine._check_crop_rows(cfg, tracker)

        self.assertEqual(notices, [], "Fifteen physical chat rows do not become 7.5 rows when OCR scales down")


if __name__ == "__main__":
    unittest.main()
