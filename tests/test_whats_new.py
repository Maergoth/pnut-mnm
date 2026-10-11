"""Release announcements acknowledge the version only after the user sees them."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget, QWizard

from mnmparse.app import APP_VERSION, theme
from mnmparse.app.main import App, MainWindow, _MissingEngine
from mnmparse.app.whats_new import RELEASE_NOTES, SEEN_VERSION_KEY, WhatsNewController, WhatsNewDialog
from mnmparse.app.window_identity import window_title
from mnmparse.config import Config
from tests.test_layout import clipped_widgets


class WhatsNewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.temp.name) / "ui.ini")
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        self.window = QWidget()
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.window.show()
        self.app.processEvents()
        self.active = patch.object(self.window, "isActiveWindow", return_value=True)
        self.active.start()
        self.eligible = Mock(return_value=True)
        self.navigation = Mock()
        self.controllers = []
        self.extra_windows = []

    def tearDown(self):
        for controller in self.controllers:
            controller.stop()
        for window in self.extra_windows:
            window.close()
            window.deleteLater()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.active.stop()
        self.temp.cleanup()

    def controller(self, **kwargs):
        controller = WhatsNewController(self.settings, self.window, eligible=self.eligible,
                                        open_settings=self.navigation, **kwargs)
        self.controllers.append(controller)
        return controller

    def test_current_release_has_concise_notes_and_a_settings_destination(self):
        self.assertIn(APP_VERSION, RELEASE_NOTES,
                      "Every version bump must add its What's New announcement")
        notes = RELEASE_NOTES[APP_VERSION]
        self.assertTrue(notes.changes)
        self.assertTrue(notes.settings_label)
        self.assertLess(sum(len(change.split()) for change in notes.changes), 100)
        current = " ".join(RELEASE_NOTES["1.0.5"].changes)
        for feature in ("Players", "Guilds", "Add", "clipboard", "CSV", "hover", "six-dot"):
            self.assertIn(feature, current)

    def test_once_per_version_is_persisted_after_actual_dismissal(self):
        controller = self.controller()
        self.assertTrue(controller.show_if_needed())
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY, ""), "")
        self.assertFalse(controller.show_if_needed(), "A second announcement must not stack")
        controller.dialog.got_it_button.click()
        self.assertEqual(QSettings(self.settings_path, QSettings.Format.IniFormat).value(SEEN_VERSION_KEY), APP_VERSION)
        self.assertFalse(self.controller().show_if_needed())
        self.settings.setValue(SEEN_VERSION_KEY, "1.0.4")
        self.assertTrue(self.controller().show_if_needed(), "An earlier acknowledged release must not suppress this one")

    def test_window_close_is_an_actual_dismissal(self):
        controller = self.controller()
        self.assertTrue(controller.show_if_needed())
        controller.dialog.close()
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY), APP_VERSION)

    def test_shutdown_preserves_unseen_announcement(self):
        self.settings.setValue(SEEN_VERSION_KEY, "1.0.4")
        controller = self.controller()
        self.assertTrue(controller.show_if_needed())
        controller.stop()
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY), "1.0.4")
        self.assertFalse(controller.show_if_needed())
        self.assertTrue(self.controller().show_if_needed())

    def test_first_time_setup_defers_until_finished_or_dismissed(self):
        host = SimpleNamespace(_selftest=False, _shut_down=False, _quit_pending=False,
                               _whats_new_waiting_setup=True, _setup_wizard=None)
        self.eligible.side_effect = lambda: App._whats_new_eligible(host)
        controller = self.controller()
        host._whats_new = controller
        self.assertFalse(controller.show_if_needed())
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY, ""), "")
        from mnmparse.app.setup_wizard import SetupWizard

        cfg = Config(reduced_motion=True)
        wizard = SetupWizard(_MissingEngine(cfg), cfg, self.window)
        self.extra_windows.append(wizard)
        host._setup_wizard = wizard
        host._whats_new_waiting_setup = False
        wizard.open()
        self.assertFalse(controller.show_if_needed())
        wizard.finished.connect(lambda result: App._on_setup_closed(host, result))
        with patch.object(self.window, "isActiveWindow", return_value=False):
            wizard.button(QWizard.WizardButton.CancelButton).click()
            self.app.processEvents()
            self.assertIsNone(controller.dialog)
        QApplication.sendEvent(self.window, QEvent(QEvent.Type.WindowActivate))
        self.app.processEvents()
        self.assertIsNotNone(controller.dialog)
        self.assertTrue(controller.dialog.isVisible())

    def test_selftest_quit_and_shutdown_never_present_or_acknowledge(self):
        for state in ("_selftest", "_quit_pending", "_shut_down"):
            with self.subTest(state=state):
                host = SimpleNamespace(_selftest=False, _shut_down=False, _quit_pending=False)
                setattr(host, state, True)
                self.eligible.side_effect = lambda: App._whats_new_eligible(host)
                controller = self.controller()
                self.assertFalse(controller.show_if_needed())
                self.assertEqual(self.settings.value(SEEN_VERSION_KEY, ""), "")

    def test_hidden_minimized_passive_and_modal_windows_defer(self):
        controller = self.controller()
        self.window.hide()
        self.assertFalse(controller.show_if_needed())
        self.window.show()
        with patch.object(self.window, "isMinimized", return_value=True):
            self.assertFalse(controller.show_if_needed())
        with patch.object(self.window, "isActiveWindow", return_value=False):
            self.assertFalse(controller.show_if_needed())
        modal = QDialog(self.window)
        self.extra_windows.append(modal)
        modal.open()
        self.assertFalse(controller.show_if_needed())
        modal.reject()
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY, ""), "")
        controller.request()
        self.app.processEvents()
        self.assertTrue(controller.dialog.isVisible())

    def test_settings_navigation_acknowledges_and_scrolls_to_revenge_options(self):
        controller = self.controller()
        cfg = Config(player_name="PreviewHero", setup_complete=True, start_capture_on_launch=False)
        window = MainWindow(_MissingEngine(cfg), None, cfg, self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.resize(1100, 750)
        window.show()
        self.extra_windows.append(window)
        controller._open_settings = window.open_revenge_settings
        self.app.processEvents()
        self.assertTrue(controller.show_if_needed())
        controller.dialog.settings_button.click()
        self.app.processEvents()
        page = window.page("settings")
        self.assertIs(window._stack.currentWidget(), page)
        self.assertTrue(window._nav_buttons["settings"].isChecked())
        self.assertGreater(page._scroll.verticalScrollBar().value(), 0)
        y = page.revenge_enabled.mapTo(page._scroll.viewport(), QPoint(0, 0)).y()
        self.assertGreaterEqual(y, 0)
        self.assertLess(y, page._scroll.viewport().height())
        self.assertEqual(self.settings.value(SEEN_VERSION_KEY), APP_VERSION)

    def test_dialog_renders_dark_readable_content_and_random_native_caption(self):
        palette, stylesheet = self.app.palette(), self.app.styleSheet()
        try:
            native = QPalette()
            native.setColor(QPalette.ColorRole.Window, QColor("#ffffff"))
            native.setColor(QPalette.ColorRole.WindowText, QColor("#000000"))
            self.app.setPalette(native)
            self.app.setStyleSheet("")
            dialog = WhatsNewDialog(APP_VERSION, RELEASE_NOTES[APP_VERSION])
            dialog.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            self.extra_windows.append(dialog)
            dialog.show()
            self.app.processEvents()
            self.assertEqual(dialog.windowTitle(), window_title("whats-new"))
            self.assertEqual(dialog.findChild(QLabel, "Heading").text(), "What's New!")
            image = dialog.grab().toImage()
            self.assertLess(image.pixelColor(5, 5).lightness(), 50)
            self.assertEqual(dialog.change_labels[0].palette().color(QPalette.ColorRole.WindowText), QColor(theme.TEXT))
            self.assertEqual(clipped_widgets(dialog), [])
        finally:
            self.app.setPalette(palette)
            self.app.setStyleSheet(stylesheet)


if __name__ == "__main__":
    unittest.main()
