"""OCR Diagnosis exports edited settings in the background through a real button."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mnmparse.app.main import _MissingEngine  # noqa: E402
from mnmparse.app.pages import SettingsPage  # noqa: E402
from mnmparse.config import Config  # noqa: E402


class OcrDiagnosisUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cfg = Config(player_name="Mitch", log_dir=str(self.root / "logs"))
        self.engine = _MissingEngine(self.cfg)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.page = SettingsPage(self.engine, self.cfg, self.settings)
        # Replace only the export boundary; no capture, ZIP write, or networking occurs.
        self.helper = mock.Mock(return_value=self.root / "report.zip")
        module = ModuleType("mnmparse.diagnostics")
        module.write_ocr_diagnosis = self.helper
        patcher = mock.patch.dict(sys.modules, {"mnmparse.diagnostics": module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        if self.page is not None:
            self._wait_finished()
            self.page.close()
            self.page.deleteLater()
        self.app.processEvents()
        self.temporary.cleanup()

    def _wait_finished(self) -> None:
        deadline = time.monotonic() + 3.0
        while self.page._ocr_diagnosis_busy and time.monotonic() < deadline:
            self.app.processEvents()
            QTest.qWait(5)
        self.assertFalse(self.page._ocr_diagnosis_busy, "Background export did not finish")
        self.app.processEvents()

    def _dialog(self, path: Path | str):
        return mock.patch(
            "mnmparse.app.pages.QFileDialog.getSaveFileName", return_value=(str(path), "Diagnosis ZIP (*.zip)"),
        )

    def test_actual_button_exports_current_form_config_and_shows_saved_path(self) -> None:
        self.page.player_name.setText("EditedPlayer")
        self.page.ocr_scale.setValue(2.0)
        self.page.preprocess.setCurrentText("none")
        crop = (20, 90, 650, 500)
        self.page.crop_picker.set_crop(crop)
        path = self.root / "chosen-report.zip"
        self.helper.return_value = path
        gui_thread = threading.get_ident()
        worker_threads: list[int] = []

        def export(*_args, **_kwargs):
            worker_threads.append(threading.get_ident())
            return path

        self.helper.side_effect = export
        with self._dialog(path) as dialog:
            self.page.ocr_diagnosis_button.click()
            self.assertFalse(self.page.ocr_diagnosis_button.isEnabled())
            self._wait_finished()

        dialog.assert_called_once()
        default = Path(dialog.call_args.args[2])
        self.assertEqual(default.parent, self.root / "logs" / "exports")
        self.assertRegex(default.name, r"^ocr-diagnosis-\d{8}-\d{6}\.zip$")
        self.helper.assert_called_once()
        self.assertEqual(self.helper.call_args.args, (path, self.engine))
        edited = self.helper.call_args.kwargs["edited_config"]
        self.assertEqual((edited.player_name, edited.ocr_scale, edited.preprocess, edited.crop),
                         ("EditedPlayer", 2.0, "none", crop))
        self.assertEqual(self.cfg.ocr_scale, 1.0, "Exporting must not save or apply edits")
        metadata = self.helper.call_args.kwargs["preview_metadata"]
        self.assertEqual(metadata["preview_ocr_line_count"], 0)
        if "screen" in metadata:
            self.assertGreater(metadata["screen"]["geometry"]["width"], 0)
            self.assertGreater(metadata["screen"]["device_pixel_ratio"], 0)
            self.assertGreater(metadata["screen"]["logical_dpi"], 0)
        self.assertNotEqual(worker_threads, [gui_thread], "ZIP export must not run on the GUI thread")
        self.assertEqual(len(worker_threads), 1)
        self.assertTrue(self.page.ocr_diagnosis_button.isEnabled())
        self.assertIn(str(path), self.page.ocr_diagnosis_status.text())
        self.assertEqual(self.page.ocr_diagnosis_status.textFormat(), Qt.TextFormat.PlainText)

    def test_cancel_does_not_export_or_create_default_directory(self) -> None:
        with self._dialog("") as dialog:
            self.page.ocr_diagnosis_button.click()
        dialog.assert_called_once()
        self.helper.assert_not_called()
        self.assertTrue(self.page.ocr_diagnosis_button.isEnabled())
        self.assertIsNone(self.page._ocr_diagnosis_job)
        self.assertFalse((self.root / "logs" / "exports").exists())

    def test_export_failure_is_shown_and_button_is_reenabled(self) -> None:
        self.helper.side_effect = OSError("The selected folder is read-only")
        with self._dialog(self.root / "failed.zip"), self.assertLogs("mnmparse.app.pages", level="ERROR"):
            self.page.ocr_diagnosis_button.click()
            self._wait_finished()
        self.assertIn("Could not export OCR Diagnosis", self.page.ocr_diagnosis_status.text())
        self.assertIn("read-only", self.page.ocr_diagnosis_status.text())
        self.assertTrue(self.page.ocr_diagnosis_button.isEnabled())

    def test_a_running_export_prevents_overlapping_dialogs_and_exports(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def export(*_args, **_kwargs):
            entered.set()
            if not release.wait(2.0):
                raise TimeoutError("Test export was not released")
            return self.root / "report.zip"

        self.helper.side_effect = export
        with self._dialog(self.root / "report.zip") as dialog:
            try:
                self.page.ocr_diagnosis_button.click()
                self.assertTrue(entered.wait(1.0))
                self.page._request_ocr_diagnosis()
                self.page.ocr_diagnosis_button.click()
                dialog.assert_called_once()
                self.helper.assert_called_once()
                self.assertFalse(self.page.ocr_diagnosis_button.isEnabled())
            finally:
                release.set()
                self._wait_finished()

    def test_file_dialog_failure_is_shown_without_starting_a_worker(self) -> None:
        with mock.patch("mnmparse.app.pages.QFileDialog.getSaveFileName", side_effect=RuntimeError("Dialog unavailable")):
            self.page.ocr_diagnosis_button.click()
        self.helper.assert_not_called()
        self.assertTrue(self.page.ocr_diagnosis_button.isEnabled())
        self.assertIn("Dialog unavailable", self.page.ocr_diagnosis_status.text())

    def test_page_can_be_destroyed_while_a_background_export_finishes(self) -> None:
        entered, release = threading.Event(), threading.Event()

        def export(*_args, **_kwargs):
            entered.set()
            if not release.wait(2.0):
                raise TimeoutError("Test export was not released")
            return self.root / "report.zip"

        self.helper.side_effect = export
        with self._dialog(self.root / "report.zip"), mock.patch("threading.excepthook") as thread_errors:
            try:
                self.page.ocr_diagnosis_button.click()
                self.assertTrue(entered.wait(1.0))
                job = self.page._ocr_diagnosis_job
                self.assertIsNone(job.parent(), "The page must not own the active worker QObject")
                page, self.page = self.page, None
                page.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            finally:
                release.set()
            job._thread.join(3.0)
            self.assertFalse(job._thread.is_alive(), "Export should finish after its page is destroyed")
            thread_errors.assert_not_called()
        self.helper.assert_called_once()


if __name__ == "__main__":
    unittest.main()
