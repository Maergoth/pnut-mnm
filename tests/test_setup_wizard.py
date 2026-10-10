"""Setup stages settings and requires crop-specific OCR evidence before Finish."""
from __future__ import annotations

import dataclasses
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtCore import Qt, QRect, QPoint, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QWizard

from mnmparse.app.demo import demo_bundle
from mnmparse.app.crop_picker import CropPicker
from mnmparse.app.setup_wizard import SetupWizard, _CalibrationEngine
from mnmparse.config import Config
from mnmparse.privacy import PROMISE, project_session, project_encounter


class SetupWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.cfg = Config(player_name="", crop=(0, 60, 700, 600))
        self.engine = SimpleNamespace(grab_frame=Mock(), test_ocr=Mock(return_value=[]), update_config=Mock())
        self.wizard = SetupWizard(self.engine, self.cfg)
        self.wizard.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.finished = []
        self.wizard.finished_config.connect(self.finished.append)
        self.wizard.show()
        self.app.processEvents()

    def tearDown(self):
        self.wizard.reject()
        self.wizard.deleteLater()
        self.app.processEvents()

    def reach_crop(self):
        self.wizard.character_page.name.setText("DemoHero")
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 1)

    def calibrate(self):
        self.reach_crop()
        self.wizard.crop_picker.set_frame(np.ones((720, 1280, 3), dtype=np.uint8) * 99)
        self.wizard.crop_picker.set_crop((10, 20, 500, 300))
        self.wizard._begin_ocr()
        self.wizard._ocr_completed(3)
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 2)

    def choose_full_mode(self, *, agree):
        panel = self.wizard.character_page.morality
        self.wizard._draft.reduced_motion = True
        panel.set_config(self.wizard._draft)
        observations = []
        switch = panel.switch
        start = switch._knob_rect().center().toPoint()
        end = QPoint(round(start.x() + (switch.width() - 106) * .9), start.y())
        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(switch, end)
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=end)

        def resolve():
            dialog = panel._dialog
            observations.append((dialog.findChild(QPushButton, "moralityAgree").isEnabled(),
                                 self.wizard._draft.casual_mode,
                                 [label.text() for label in dialog.findChildren(QLabel)]))
            if agree:
                dialog.findChild(QCheckBox, "moralityPledge").setChecked(True)
                dialog.findChild(QPushButton, "moralityAgree").click()
            else:
                dialog.reject()

        QTimer.singleShot(0, resolve)
        self.app.processEvents()
        self.assertFalse(observations[0][0], "The pledge must be explicitly checked")
        self.assertTrue(observations[0][1], "Full mode stays staged off until agreement")
        self.assertIn("Confirm you " + PROMISE, observations[0][2])

    def test_first_setup_reuses_physical_mode_slider_and_stages_confirmed_choice_until_finish(self):
        panel = self.wizard.character_page.morality
        self.assertTrue(panel.switch.isChecked())
        panel.switch.click()
        self.assertTrue(self.wizard._draft.casual_mode)
        sirens = []
        self.wizard.siren_requested.connect(lambda: sirens.append(True))
        self.choose_full_mode(agree=True)
        self.assertEqual(sirens, [True])
        self.assertFalse(self.wizard._draft.casual_mode)
        self.assertTrue(self.wizard._draft.casual_mode_confirmed)
        self.assertFalse(panel.switch.isChecked())
        self.assertTrue(self.cfg.casual_mode)
        self.assertFalse(self.cfg.casual_mode_confirmed)
        self.engine.update_config.assert_not_called()
        self.calibrate()
        self.wizard.next()
        self.wizard.accept()
        self.assertFalse(self.finished[0].casual_mode)
        self.assertTrue(self.finished[0].casual_mode_confirmed)
        self.assertTrue(self.cfg.casual_mode)

    def test_rejected_pledge_stays_casual_and_return_from_full_resets_confirmation(self):
        self.choose_full_mode(agree=False)
        self.assertTrue(self.wizard._draft.casual_mode)
        self.assertFalse(self.wizard._draft.casual_mode_confirmed)
        self.choose_full_mode(agree=True)
        panel = self.wizard.character_page.morality
        panel.switch.click()
        self.assertTrue(self.wizard._draft.casual_mode)
        self.assertFalse(self.wizard._draft.casual_mode_confirmed)
        self.assertTrue(self.cfg.casual_mode)

    def test_cancelled_setup_cancels_delayed_pledge_and_never_commits_staged_mode(self):
        panel = self.wizard.character_page.morality
        with patch.object(panel, "_confirm") as confirm:
            panel.request_toggle()
            self.assertTrue(panel._pending)
            self.wizard.reject()
            self.app.processEvents()
            self.assertFalse(panel._pending)
            confirm.assert_not_called()
        self.assertTrue(self.cfg.casual_mode)
        self.assertEqual(self.finished, [])

    def test_construct_and_navigation_never_capture_or_mutate_live_config(self):
        before = dataclasses.asdict(self.cfg)
        self.assertFalse(self.wizard.character_page.isComplete())
        self.reach_crop()
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.engine.grab_frame.assert_not_called()
        self.engine.test_ocr.assert_not_called()
        self.engine.update_config.assert_not_called()
        self.assertEqual(dataclasses.asdict(self.cfg), before)
        self.wizard.reject()
        self.assertEqual(self.finished, [])

    def test_crop_step_requires_matching_positive_ocr_before_common_options(self):
        self.reach_crop()
        self.wizard.crop_picker.set_frame(np.ones((720, 1280, 3), dtype=np.uint8) * 99)
        self.wizard.crop_picker.set_crop((10, 20, 500, 300))
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 1, "A frame alone is not OCR validation")
        self.wizard._begin_ocr()
        self.wizard._ocr_completed(0)
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 1)
        self.wizard._begin_ocr()
        self.wizard._ocr_completed(3)
        self.wizard.crop_picker.set_crop((20, 20, 500, 300))
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 1, "Changed crop requires a new OCR result")
        self.wizard._begin_ocr()
        self.wizard._ocr_completed(2)
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 2)
        self.assertIs(self.wizard.currentPage(), self.wizard.options_page)
        self.assertIn("Common options", self.wizard.currentPage().title())

    def test_common_options_preserve_config_defaults_and_passed_map_preference(self):
        page = self.wizard.options_page
        self.assertFalse(page.revenge_enabled.isChecked())
        self.assertTrue(page.display_map.isChecked())
        self.assertTrue(page.attack_bar.isChecked())
        self.assertFalse(page.export_auto.isChecked())
        customized = dataclasses.replace(self.cfg, revenge_enabled=True, attack_bar=False, export_auto=True)
        extra = SetupWizard(self.engine, customized, display_map=False)
        try:
            self.assertTrue(extra.options_page.revenge_enabled.isChecked())
            self.assertFalse(extra.display_map)
            self.assertFalse(extra.options_page.attack_bar.isChecked())
            self.assertTrue(extra.options_page.export_auto.isChecked())
            self.assertFalse(hasattr(extra._draft, "display_map"), "Map remains a UI preference, outside Config")
        finally:
            extra.reject()
            extra.deleteLater()

    def test_common_options_stage_and_survive_back_navigation_without_committing_on_cancel(self):
        before = dataclasses.asdict(self.cfg)
        self.calibrate()
        page = self.wizard.options_page
        for switch in (page.revenge_enabled, page.display_map, page.attack_bar, page.export_auto):
            switch.toggle()
        self.assertTrue(self.wizard._draft.revenge_enabled)
        self.assertFalse(self.wizard._draft.attack_bar)
        self.assertTrue(self.wizard._draft.export_auto)
        self.assertFalse(self.wizard.display_map)
        self.assertEqual(dataclasses.asdict(self.cfg), before)
        self.wizard.back()
        self.wizard.next()
        self.assertTrue(page.revenge_enabled.isChecked())
        self.assertFalse(page.display_map.isChecked())
        self.wizard.reject()
        self.assertEqual(dataclasses.asdict(self.cfg), before)
        self.assertEqual(self.finished, [])
        self.engine.update_config.assert_not_called()

    def test_saved_common_options_are_emitted_together_with_chosen_map_state(self):
        self.calibrate()
        page = self.wizard.options_page
        page.revenge_enabled.setChecked(True)
        page.display_map.setChecked(False)
        page.attack_bar.setChecked(False)
        page.export_auto.setChecked(True)
        delivered_map = []
        self.wizard.finished_config.connect(lambda cfg: delivered_map.append(self.wizard.display_map))
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 3)
        self.wizard.accept()
        configured = self.finished[0]
        self.assertTrue(configured.revenge_enabled)
        self.assertFalse(configured.attack_bar)
        self.assertTrue(configured.export_auto)
        self.assertTrue(configured.setup_complete)
        self.assertEqual(delivered_map, [False])
        self.assertFalse(self.cfg.revenge_enabled)
        self.assertTrue(self.cfg.attack_bar)
        self.assertFalse(self.cfg.export_auto)

    def test_finish_requires_matching_crop_positive_ocr_without_confirmation_checkbox(self):
        self.calibrate()
        self.assertTrue(self.wizard.crop_page.isComplete())
        self.assertFalse(hasattr(self.wizard.crop_page, "checked"))
        self.wizard.accept()
        self.assertEqual(self.finished, [])
        self.wizard.next()
        self.assertEqual(self.wizard.currentId(), 3)
        self.wizard.accept()
        self.assertEqual(len(self.finished), 1)
        configured = self.finished[0]
        self.assertTrue(configured.setup_complete)
        self.assertEqual(configured.player_name, "DemoHero")
        self.assertEqual(configured.crop, (10, 20, 500, 300))
        self.assertFalse(self.cfg.setup_complete)
        self.assertEqual(self.cfg.player_name, "")
        self.assertTrue(configured.casual_mode)
        self.assertIsNone(self.wizard.crop_picker._frame)
        self.assertFalse(self.wizard.crop_picker.canvas.has_frame())

    def test_changed_crop_invalidates_previous_or_inflight_ocr(self):
        self.calibrate()
        self.assertTrue(self.wizard.crop_page.isComplete())
        self.wizard.crop_picker.set_crop((20, 20, 500, 300))
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.wizard._ocr_completed(3)  # result from the old crop
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.wizard._begin_ocr()
        self.wizard._ocr_completed(0)
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.assertIn("No readable lines", self.wizard.crop_page.ocr_status.text())

    def test_changed_source_invalidates_frame_and_blocks_next(self):
        self.calibrate()
        self.wizard.crop_page.title.setText("Another game title")
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.assertFalse(self.wizard.crop_page.isComplete())
        self.assertIsNone(self.wizard.crop_picker.frame())

    def test_only_local_setup_canvas_shows_explicit_calibration_frame_without_changing_mode(self):
        self.reach_crop()
        frame = np.ones((720, 1280, 3), dtype=np.uint8) * 99
        self.wizard.crop_picker.set_frame(frame)
        self.assertEqual(self.wizard.crop_picker.canvas._pixmap.toImage().pixelColor(10, 10), QColor(99, 99, 99))
        self.wizard.crop_picker.set_config(self.wizard._draft)
        self.assertEqual(self.wizard.crop_picker.canvas._pixmap.toImage().pixelColor(10, 10), QColor(99, 99, 99))
        # Accessors used outside calibration still obey the presentation policy.
        self.assertTrue(np.all(self.wizard.crop_picker.frame() == 0))
        self.assertTrue(self.wizard._draft.casual_mode)
        self.assertTrue(self.cfg.casual_mode)
        self.assertEqual(self.wizard._frame_size, (1280, 720))
        normal = CropPicker(self.engine, self.cfg)
        try:
            normal.set_frame(frame)
            self.assertEqual(normal.canvas._pixmap.toImage().pixelColor(10, 10), QColor(0, 0, 0))
            self.assertTrue(np.all(normal.frame() == 0))
        finally:
            normal.deleteLater()

    def test_calibration_does_not_reveal_ocr_text_or_visible_keyboard_callouts(self):
        self.reach_crop()
        self.wizard.crop_picker.set_frame(np.ones((720, 1280, 3), dtype=np.uint8) * 99)
        self.wizard.crop_picker._on_ocr_done(([SimpleNamespace(text="Teammate hits Goblin for 12 points of damage.",
                                                            x=0, y=0, h=12)], 5), None)
        for index in range(self.wizard.crop_picker._lines.count()):
            self.assertNotIn("Teammate", self.wizard.crop_picker._lines.item(index).text())
        for box in self.wizard.crop_picker.canvas._boxes:
            self.assertNotIn("Teammate", box.text)
        for label in self.wizard.crop_picker.findChildren(QLabel):
            self.assertNotIn("Ctrl+arrows", label.text())
            self.assertNotIn("Shift 10", label.text())
        self.assertIn("Ctrl", self.wizard.crop_picker.canvas.accessibleDescription())
        self.assertIn("cleared when setup closes", self.wizard.crop_page.privacy_hint.text())

    def test_cancel_clears_frame_and_late_capture_cannot_restore_hidden_pixels(self):
        self.reach_crop()
        frame = np.ones((720, 1280, 3), dtype=np.uint8) * 99
        self.wizard.crop_picker.set_frame(frame)
        # A running worker keeps the owner alive; its eventual result must be
        # cleared even though it arrives after the setup window has hidden.
        self.wizard.crop_picker._job = object()
        self.wizard.reject()
        self.assertTrue(self.wizard.isHidden())
        self.assertIsNone(self.wizard.crop_picker._frame)
        self.assertFalse(self.wizard.crop_picker.canvas.has_frame())
        self.wizard.crop_picker._job = None
        self.wizard.crop_picker._on_frame_done(frame, None)
        self.assertIsNone(self.wizard.crop_picker._frame)
        self.assertFalse(self.wizard.crop_picker.canvas.has_frame())
        self.wizard._finish_cancel()
        self.assertEqual(self.finished, [])

    def test_cancelled_full_mode_calibration_also_discards_late_ocr_results(self):
        self.reach_crop()
        full = dataclasses.replace(self.wizard._draft, casual_mode=False, casual_mode_confirmed=True)
        self.wizard.crop_picker.set_config(full)
        self.wizard.crop_picker.set_frame(np.ones((720, 1280, 3), dtype=np.uint8) * 99)
        self.wizard.crop_picker._job = object()
        self.wizard.reject()
        self.wizard.crop_picker._job = None
        self.wizard.crop_picker._on_ocr_done(([SimpleNamespace(text="Unprotected teammate name", x=0, y=0, h=12)], 5), None)
        self.assertEqual(self.wizard.crop_picker._lines.count(), 0)
        self.assertEqual(self.wizard.crop_picker.canvas._boxes, [])
        self.assertIsNone(self.wizard.crop_picker._frame)
        self.wizard._finish_cancel()

    def test_temporary_source_always_stops_without_updating_live_config(self):
        source = SimpleNamespace(start=Mock(), latest=Mock(return_value=np.zeros((30, 50, 3), dtype=np.uint8)), stop=Mock())
        adapter = _CalibrationEngine(self.engine, Config(window_title="Test title"))
        with patch("mnmparse.capture.make_source", return_value=source) as make:
            self.assertEqual(adapter.grab_frame().shape, (30, 50, 3))
        self.assertEqual(make.call_args.args[0].window_title, "Test title")
        source.start.assert_called_once()
        source.stop.assert_called_once()
        self.engine.update_config.assert_not_called()

    def test_modern_wizard_renders_dark_surfaces_with_readable_text_even_under_light_native_palette(self):
        from mnmparse.app import theme

        def luminance(color):
            rgb = [channel / 255 for channel in (color.red(), color.green(), color.blue())]
            linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in rgb]
            return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))

        def contrast(left, right):
            low, high = sorted((luminance(left), luminance(right)))
            return (high + .05) / (low + .05)

        palette, stylesheet = self.app.palette(), self.app.styleSheet()
        extra = None
        try:
            native = QPalette()
            for role in (QPalette.ColorRole.Window, QPalette.ColorRole.Base, QPalette.ColorRole.Button):
                native.setColor(role, QColor("#ffffff"))
            native.setColor(QPalette.ColorRole.WindowText, QColor(theme.TEXT))
            self.app.setStyleSheet("")
            self.app.setPalette(native)
            extra = SetupWizard(self.engine, self.cfg)
            extra.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            extra.show()
            self.app.processEvents()
            self.assertEqual(extra.wizardStyle(), QWizard.WizardStyle.ModernStyle, "Windows Aero painting must never choose the setup surfaces")
            self.assertIn("PNUT setup", extra.currentPage().title())
            image = extra.grab().toImage()
            viewport = extra.character_page.body_scroll.viewport().grab().toImage()
            backgrounds = [image.pixelColor(image.width() - 15, 15),
                           image.pixelColor(image.width() // 2, image.height() - 4),
                           viewport.pixelColor(viewport.width() // 2, viewport.height() - 20)]
            for color in backgrounds:
                self.assertLess(max(color.red(), color.green(), color.blue()), 70, "Header, footer and page body must render dark, not native white")
                self.assertGreater(contrast(QColor(theme.TEXT), color), 4.5)
            header = next(label for label in extra.findChildren(QLabel)
                          if label.text().startswith("PNUT setup") and label.isVisible())
            self.assertGreater(contrast(header.palette().color(QPalette.ColorRole.WindowText), backgrounds[0]), 4.5)
            extra.character_page.name.setText("DemoHero")
            self.app.processEvents()
            button = extra.button(QWizard.WizardButton.NextButton)
            self.assertGreater(contrast(button.palette().color(QPalette.ColorRole.ButtonText),
                                        button.palette().color(QPalette.ColorRole.Button)), 4.5)
        finally:
            if extra is not None:
                extra.reject()
                extra.deleteLater()
            self.app.setPalette(palette)
            self.app.setStyleSheet(stylesheet)

    def test_small_logical_screen_keeps_navigation_visible_and_crop_body_scrollable(self):
        screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 760, 520))
        with patch("mnmparse.app.setup_wizard.QApplication.primaryScreen", return_value=screen):
            small = SetupWizard(self.engine, self.cfg)
        try:
            small.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            small.show()
            small.character_page.name.setText("DemoHero")
            small.next()
            self.app.processEvents()
            self.app.processEvents()
            self.assertLessEqual(small.width(), 760)
            self.assertLessEqual(small.height(), 520)
            self.assertGreater(small.crop_page.body_scroll.verticalScrollBar().maximum(), 0)
            for role in (QWizard.WizardButton.NextButton, QWizard.WizardButton.CancelButton):
                button = small.button(role)
                self.assertTrue(button.isVisible())
                bottom = button.mapTo(small, button.rect().bottomRight())
                self.assertTrue(small.rect().contains(bottom), "Navigation must stay inside the DPI-scaled screen")
        finally:
            small.reject()
            small.deleteLater()


class DemoFixtureTests(unittest.TestCase):
    def test_demo_is_fresh_repeatable_and_has_own_and_group_data(self):
        first, second = demo_bundle("DemoHero"), demo_bundle("DemoHero")
        self.assertEqual([dataclasses.asdict(e) for e in first.events], [dataclasses.asdict(e) for e in second.events])
        self.assertIsNot(first.stats, second.stats)
        self.assertEqual(len(first.encounters), 2)
        self.assertEqual(first.session.items, 2)
        cfg = Config(player_name="DemoHero")
        private = project_encounter(first.encounters[0], cfg)
        self.assertTrue(any(row.is_you for row in private.rows))
        self.assertTrue(any("Group average" in row.name for row in private.rows))
        encoded = str(dataclasses.asdict(private)) + str(dataclasses.asdict(project_session(first.session, cfg)))
        for peer in ("DemoAri", "DemoBram", "DemoCyra", "DemoDune", "DemoEris"):
            self.assertNotIn(peer, encoded)


if __name__ == "__main__":
    unittest.main()
