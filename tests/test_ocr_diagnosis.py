"""Local OCR diagnosis evidence and ZIP export, with no live capture or real OCR."""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    import cv2
    import numpy as np

    HAVE_IMAGES = True
except ImportError:  # pragma: no cover - optional desktop dependencies
    HAVE_IMAGES = False
try:
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:  # pragma: no cover - optional desktop dependencies
    HAVE_QT = False

from mnmparse import __version__  # noqa: E402
from mnmparse.config import Config  # noqa: E402
from mnmparse.diagnostics import write_ocr_diagnosis  # noqa: E402
from mnmparse.tracker import Message  # noqa: E402


class FakeOcr:
    def __init__(self) -> None:
        self.scale = 1.0
        self.language = "en-US"
        self.lines: list[Any] = []

    def read(self, _image: Any) -> list[Any]:
        return list(self.lines)


@unittest.skipUnless(HAVE_QT and HAVE_IMAGES, "Desktop dependencies unavailable")
class EngineOcrDiagnosisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def setUp(self) -> None:
        from mnmparse.app.engine import Engine

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch("mnmparse.app.engine._load_vocab_once")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cfg = Config(player_name="Mitch", log_dir=folder.name, crop=(2, 3, 12, 13), preprocess="none")
        self.engine = Engine(self.cfg)
        self.ocr = FakeOcr()
        self.engine._prepare_run(self.cfg, self.ocr)
        self.addCleanup(self._close_writer)
        self.now = time.time()

    def _close_writer(self) -> None:
        if self.engine._writer is not None:
            self.engine._writer.close()

    def _feed(self, text: str, *, fragment: bool = False) -> None:
        self.now += 0.1
        self.engine._handle_message(Message(text, self.now, 2, fragment=fragment), self.engine.config)

    def test_message_evidence_distinguishes_fragments_partial_unknown_and_estimates(self) -> None:
        self._feed("Mitch pierces a wolf for 8 points of damage", fragment=True)
        self._feed("zz %% qqq 7")
        self._feed("Mitch's Vampirism hits a wolf for $ points of Corruption Damage.")
        self.engine.update_config(dataclasses.replace(self.cfg, dummy_fix=True))
        self._feed("Mitch pierces a wolf for $ points of damage.")
        with mock.patch.object(self.engine, "grab_frame") as capture:
            snapshot = self.engine.ocr_diagnosis()
        capture.assert_not_called()
        json.dumps(snapshot)
        messages = snapshot["recent_messages"]
        self.assertEqual([message["kind"] for message in messages],
                         ["melee_hit", "unknown", "ability_partial", "melee_hit"])
        self.assertEqual([message["unreadable_reason"] for message in messages],
                         [None, "unrecognized_message", "incomplete_ability", "estimated_amount"])
        self.assertTrue(messages[0]["fragment"])
        self.assertEqual((messages[0]["amount"], messages[0]["unreadable"]), (8, False))
        self.assertEqual((messages[-1]["amount"], messages[-1]["estimated"]), (8, True))
        self.assertEqual(len(snapshot["missed_messages"]), 3)
        self.assertEqual(snapshot["runtime"]["unreadable_pct"], 75.0)
        self.assertTrue(any("never captured" in note for note in snapshot["limitations"]))

    def test_message_sample_is_bounded_and_returned_evidence_is_detached(self) -> None:
        from mnmparse.app.engine import DIAG_MESSAGE_LIMIT

        for i in range(DIAG_MESSAGE_LIMIT + 12):
            self._feed(f"zz %% qqq {i}")
        snapshot = self.engine.ocr_diagnosis()
        self.assertEqual(len(snapshot["recent_messages"]), DIAG_MESSAGE_LIMIT)
        self.assertEqual(snapshot["recent_messages"][0]["raw_text"], "zz %% qqq 12")
        snapshot["recent_messages"][0]["raw_text"] = "changed by consumer"
        snapshot["saved_settings"]["crop"][0] = 999
        again = self.engine.ocr_diagnosis()
        self.assertEqual(again["recent_messages"][0]["raw_text"], "zz %% qqq 12")
        self.assertEqual(again["saved_settings"]["crop"], list(self.cfg.crop))

    def test_recent_ocr_rows_have_geometry_active_settings_and_bounded_frames(self) -> None:
        from mnmparse.app.engine import DIAG_FRAME_LIMIT, DIAG_ROW_LIMIT

        frame = np.zeros((20, 20, 3), np.uint8)
        for i in range(DIAG_FRAME_LIMIT + 1):
            self.ocr.lines = [SimpleNamespace(x=1, y=2, h=3, text=f"row {i}")]
            self.engine._process_frame(frame, self.cfg)
        frames = self.engine.ocr_diagnosis()["recent_ocr_frames"]
        self.assertEqual(len(frames), DIAG_FRAME_LIMIT)
        self.assertEqual(frames[0]["rows"], [{"x": 1, "y": 2, "h": 3, "text": "row 1", "text_truncated": False}])
        self.assertEqual(frames[0]["crop"], list(self.cfg.crop))
        self.assertEqual(frames[0]["coordinate_space"], "unscaled_combat_crop_pixels")
        self.assertEqual(frames[0]["ocr_dimensions"], {"width": 10, "height": 10})
        self.ocr.lines = [SimpleNamespace(x=1, y=i * 25, h=21, text=f"row {i}.")
                          for i in range(DIAG_ROW_LIMIT + 1)]
        self.engine._process_frame(frame, self.cfg)
        latest = self.engine.ocr_diagnosis()["recent_ocr_frames"][-1]
        self.assertEqual(latest["row_count"], DIAG_ROW_LIMIT + 1)
        self.assertEqual(len(latest["rows"]), DIAG_ROW_LIMIT)
        self.assertTrue(latest["rows_truncated"])

    def test_saved_pending_and_actual_active_ocr_settings_are_separate(self) -> None:
        saved = dataclasses.replace(self.cfg, ocr_engine="rapid", ocr_scale=2.0)
        self.engine.update_config(saved)
        snapshot = self.engine.ocr_diagnosis()
        self.assertEqual(snapshot["saved_settings"]["ocr_engine"], "rapid")
        self.assertEqual(snapshot["saved_settings"]["ocr_scale"], 2.0)
        self.assertEqual(snapshot["effective_settings"]["ocr_engine"], "windows")
        self.assertEqual(snapshot["effective_settings"]["ocr_scale"], 1.0)
        self.assertEqual(snapshot["active_ocr"]["recognizer_language"], "en-US")
        self.assertTrue(snapshot["pending_ocr_settings"]["requires_capture_restart"])
        self.engine._ocr = SimpleNamespace(read=lambda _image: [])
        self.assertIsNone(self.engine.ocr_diagnosis()["active_ocr"]["recognizer_language"])

    def test_new_run_clears_diagnostic_samples(self) -> None:
        self._feed("zz %% qqq 7")
        self.engine._process_frame(np.zeros((20, 20, 3), np.uint8), self.cfg)
        self.assertTrue(self.engine.ocr_diagnosis()["recent_messages"])
        self.assertTrue(self.engine.ocr_diagnosis()["recent_ocr_frames"])
        with mock.patch.object(self.engine, "_save_vocab"):
            self.engine._finish()
        self.engine._prepare_run(self.cfg, self.ocr)
        snapshot = self.engine.ocr_diagnosis()
        self.assertEqual(snapshot["recent_messages"], [])
        self.assertEqual(snapshot["missed_messages"], [])
        self.assertEqual(snapshot["recent_ocr_frames"], [])
        self.assertIsNotNone(snapshot["runtime"]["run_started_at"])


def _snapshot(cfg: Config) -> dict[str, Any]:
    data = dataclasses.asdict(cfg)
    data["crop"] = list(cfg.crop)
    return {
        "saved_settings": data, "effective_settings": copy.deepcopy(data),
        "active_ocr": {"available": False, "engine": None, "scale": None, "recognizer_language": None},
        "pending_ocr_settings": {"requires_capture_restart": False},
        "runtime": {"messages": 0, "unreadable_pct": 0.0},
        "recent_messages": [], "missed_messages": [], "recent_ocr_frames": [], "limitations": [],
    }


class FakeEngine:
    def __init__(self, cfg: Config, frame: Any = None, error: Exception | None = None) -> None:
        self.snapshot = _snapshot(cfg)
        self.frame = frame
        self.error = error
        self.grabs = 0

    def ocr_diagnosis(self) -> dict[str, Any]:
        return copy.deepcopy(self.snapshot)

    def grab_frame(self) -> Any:
        self.grabs += 1
        if self.error is not None:
            raise self.error
        return self.frame


@unittest.skipUnless(HAVE_IMAGES, "Image dependencies unavailable")
class OcrDiagnosisExportTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "exports" / "diagnosis.zip"
        self.cfg = Config(crop=(2, 3, 9, 10), preprocess="none")
        self.frame = np.arange(12 * 20 * 3, dtype=np.uint8).reshape(12, 20, 3)

    def _read(self) -> tuple[dict[str, Any], list[str]]:
        with zipfile.ZipFile(self.path) as archive:
            return json.loads(archive.read("report.json")), archive.namelist()

    def test_export_attaches_only_the_effective_fresh_crop_and_reports_unsaved_changes(self) -> None:
        engine = FakeEngine(self.cfg, self.frame)
        edited = dataclasses.replace(self.cfg, crop=(1, 1, 5, 5), preprocess="maxchannel")
        result = write_ocr_diagnosis(self.path, engine, edited_config=edited,
                                     preview_metadata={"frame_dimensions": {"width": 99, "height": 88}})
        self.assertEqual(result, self.path.resolve())
        self.assertEqual(engine.grabs, 1)
        report, names = self._read()
        self.assertEqual(set(names), {"report.json", "combat-crop.png"})
        self.assertEqual(report["app_version"], __version__)
        self.assertTrue(report["created_at_utc"].endswith("+00:00"))
        self.assertTrue(report["environment"]["os"])
        self.assertEqual(report["capture"]["frame_dimensions"], {"width": 20, "height": 12})
        self.assertEqual(report["capture"]["image_dimensions"], {"width": 7, "height": 7})
        self.assertEqual(report["crop"]["effective"], list(self.cfg.crop))
        self.assertEqual(report["saved_vs_edited_differences"]["crop"],
                         {"saved": list(self.cfg.crop), "edited": list(edited.crop)})
        self.assertEqual(report["crop"]["manual_adjustment_history"], "not_tracked")
        self.assertTrue(report["crop"]["differs_from_default"])
        with zipfile.ZipFile(self.path) as archive:
            image = cv2.imdecode(np.frombuffer(archive.read("combat-crop.png"), dtype=np.uint8), cv2.IMREAD_COLOR)
        np.testing.assert_array_equal(image, self.frame[3:10, 2:9])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_crop_snapshot_is_used_even_if_settings_change_during_capture(self) -> None:
        engine = FakeEngine(self.cfg, self.frame)

        def grab() -> Any:
            engine.snapshot["saved_settings"]["crop"] = [0, 0, 20, 12]
            engine.snapshot["effective_settings"]["crop"] = [0, 0, 20, 12]
            return self.frame

        engine.grab_frame = grab
        write_ocr_diagnosis(self.path, engine)
        report, _names = self._read()
        self.assertEqual(report["crop"]["effective"], list(self.cfg.crop))
        self.assertEqual(report["capture"]["image_dimensions"], {"width": 7, "height": 7})

    def test_default_saved_crop_and_unsaved_custom_crop_are_distinguished(self) -> None:
        default = Config()
        engine = FakeEngine(default)
        write_ocr_diagnosis(self.path, engine, dataclasses.replace(default, crop=self.cfg.crop))
        report, _names = self._read()
        self.assertFalse(report["crop"]["differs_from_default"])
        self.assertTrue(report["crop"]["edited_differs_from_saved"])
        self.assertEqual(report["crop"]["saved"], list(default.crop))
        self.assertEqual(report["crop"]["edited"], list(self.cfg.crop))

    def test_no_game_frame_still_writes_json_and_does_not_use_the_old_preview(self) -> None:
        engine = FakeEngine(self.cfg)
        write_ocr_diagnosis(self.path, engine, preview_metadata={"frame_dimensions": {"width": 2560, "height": 1440}})
        report, names = self._read()
        self.assertEqual(names, ["report.json"])
        self.assertFalse(report["capture"]["available"])
        self.assertIsNone(report["capture"]["frame_dimensions"])
        self.assertIsNone(report["active_ocr"]["recognizer_language"])
        self.assertIn("No fresh game frame", report["capture"]["reason"])

    def test_capture_exception_has_a_clear_reason_without_traceback_or_paths(self) -> None:
        engine = FakeEngine(self.cfg, error=RuntimeError(r"failed in C:\Users\private-user\secret.py"))
        write_ocr_diagnosis(self.path, engine)
        report, names = self._read()
        self.assertEqual(names, ["report.json"])
        self.assertEqual(report["capture"]["failure_type"], "RuntimeError")
        self.assertIn("Fresh game capture failed", report["capture"]["reason"])
        text = json.dumps(report)
        self.assertNotIn("private-user", text)
        self.assertNotIn("Traceback", text)

    def test_invalid_or_full_window_crop_never_attaches_a_game_screenshot(self) -> None:
        for crop in ((18, 10, 30, 18), (22, 10, 30, 20), (0, 0, 20, 12)):
            with self.subTest(crop=crop):
                engine = FakeEngine(dataclasses.replace(self.cfg, crop=crop), self.frame)
                write_ocr_diagnosis(self.path, engine)
                report, names = self._read()
                self.assertEqual(names, ["report.json"])
                self.assertFalse(report["capture"]["available"])
                self.assertTrue(report["capture"]["reason"])
                self.assertEqual(report["capture"]["crop_valid_within_frame"], crop == (0, 0, 20, 12))


if __name__ == "__main__":
    unittest.main()
