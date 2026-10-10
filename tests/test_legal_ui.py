"""The persistent legal footer reads selectable bundled documents and offline licenses."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QEvent, QSettings, QUrl, Qt
    from PySide6.QtGui import QFont, QFontMetricsF, QPalette, QTextCursor, QTextDocument
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.config import Config

if HAVE_QT:
    from mnmparse.app import legal_dialog, main
    from mnmparse.app.legal_dialog import LegalDialog
    from mnmparse.app.theme import apply_theme


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LegalUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.addCleanup(self.app.setStyleSheet, self.app.styleSheet())
        self.addCleanup(self.app.setFont, QFont(self.app.font()))
        self.addCleanup(self.app.setPalette, QPalette(self.app.palette()))
        apply_theme(self.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.legal = self.root / "legal"
        (self.legal / "licenses").mkdir(parents=True)
        privacy = "# Privacy Policy\n\n" + "The application processes the capture locally.\n\n" * 80
        privacy += "![Remote resource](https://example.com/tracker.png)\n"
        (self.legal / "privacy.md").write_text(privacy, encoding="utf-8")
        (self.legal / "terms.md").write_text("# Terms & Disclaimer\n\nTerms fixture text.", encoding="utf-8")
        (self.legal / "open-source.md").write_text(
            "# Open Source Notices\n\nMIT and LGPL notices.\n\n"
            "[Full license](licenses/PNUT-MIT.txt)\n\n[Project](https://example.com/project)", encoding="utf-8")
        self.license_text = "MIT License\n\nCopyright fixture\n\n" + "Permission is hereby granted.\n" * 80
        (self.legal / "licenses" / "PNUT-MIT.txt").write_text(self.license_text, encoding="utf-8")
        (self.root / "outside.txt").write_text("PRIVATE OUTSIDE FILE", encoding="utf-8")
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.windows = []
        self.path_patch = patch("mnmparse.app.legal_dialog.legal_directory", return_value=self.legal)
        self.path_patch.start()

    def tearDown(self):
        for window in reversed(self.windows):
            if hasattr(window, "prepare_quit"):
                window.prepare_quit()
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.path_patch.stop()
        self.tmp.cleanup()

    def dialog(self, section="privacy"):
        dialog = LegalDialog(section)
        self.windows.append(dialog)
        dialog.show()
        self.app.processEvents()
        return dialog

    def main_window(self):
        cfg = Config(minimize_to_tray=False, start_capture_on_launch=False)
        window = main.MainWindow(main._MissingEngine(cfg), None, cfg, self.settings)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_footer_opens_correct_local_tab_without_network_or_browser(self):
        window = self.main_window()
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as browser, \
                patch("urllib.request.urlopen") as request, patch("socket.create_connection") as connect:
            dialog = None
            for key in ("privacy", "terms", "open-source"):
                QTest.mouseClick(window._legal_buttons[key], Qt.MouseButton.LeftButton)
                self.app.processEvents()
                if dialog is not None:
                    self.assertIs(window._legal_dialog, dialog, "Footer links reuse the local reader")
                dialog = window._legal_dialog
                self.assertTrue(dialog.isVisible())
                self.assertIs(dialog.tabs.currentWidget(), dialog.browsers[key])
            browser.assert_not_called()
            request.assert_not_called()
            connect.assert_not_called()
        self.assertIn("Privacy Policy", dialog.browsers["privacy"].toPlainText())
        self.assertIn("Terms fixture text", dialog.browsers["terms"].toPlainText())
        self.assertIn("MIT and LGPL", dialog.browsers["open-source"].toPlainText())
        self.assertRegex(dialog.windowTitle(), r"^[0-9a-f]{24}$")
        self.assertEqual(window._legal_buttons["terms"].text(), "Terms && Disclaimer")
        self.assertEqual(window._legal_buttons["terms"].accessibleName(), "Terms & Disclaimer")
        self.assertEqual(dialog.tabs.tabText(dialog.tabs.indexOf(dialog.browsers["terms"])), "Terms && Disclaimer")

    def test_documents_are_read_only_selectable_scrollable_and_resources_stay_offline(self):
        dialog = self.dialog()
        privacy = dialog.browsers["privacy"]
        self.assertTrue(privacy.isReadOnly())
        self.assertFalse(privacy.openLinks())
        self.assertFalse(privacy.openExternalLinks())
        self.assertTrue(privacy.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.assertTrue(privacy.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertGreater(privacy.verticalScrollBar().maximum(), 0)
        cursor = privacy.textCursor()
        cursor.select(QTextCursor.SelectionType.Document)
        privacy.setTextCursor(cursor)
        self.assertIn("Privacy Policy", privacy.textCursor().selectedText())
        self.assertIsNone(privacy.document().resource(QTextDocument.ResourceType.ImageResource,
                                                    QUrl("https://example.com/tracker.png")))
        for key, browser in dialog.browsers.items():
            dialog.select_section(key)
            self.assertIs(dialog.tabs.currentWidget(), browser)
            self.assertTrue(browser.accessibleName())

    def test_bundled_license_link_opens_complete_text_in_local_tab(self):
        dialog = self.dialog("open-source")
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as external:
            dialog.browsers["open-source"].anchorClicked.emit(QUrl("licenses/PNUT-MIT.txt"))
            self.app.processEvents()
            external.assert_not_called()
        license_browser = dialog.tabs.currentWidget()
        self.assertEqual(license_browser.toPlainText(), self.license_text)
        self.assertTrue(license_browser.isReadOnly())
        self.assertGreater(license_browser.verticalScrollBar().maximum(), 0)
        self.assertEqual(dialog.tabs.count(), 4)
        dialog.select_section("privacy")
        self.assertIs(dialog.tabs.currentWidget(), dialog.browsers["privacy"])

    def test_absolute_local_license_inside_bundle_is_allowed(self):
        dialog = self.dialog("open-source")
        dialog.browsers["open-source"].anchorClicked.emit(QUrl.fromLocalFile(str(self.legal / "licenses" / "PNUT-MIT.txt")))
        self.assertEqual(dialog.tabs.currentWidget().toPlainText(), self.license_text)

    def test_source_bundle_documents_and_mit_license_are_available_offline(self):
        source_root = Path(legal_dialog.__file__).resolve().parents[2] / "legal"
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as external:
            dialog = LegalDialog("open-source", legal_dir=source_root)
            self.windows.append(dialog)
            self.assertIn("Privacy Policy", dialog.browsers["privacy"].toPlainText())
            self.assertIn("Terms and Disclaimer", dialog.browsers["terms"].toPlainText())
            self.assertIn("LGPL", dialog.browsers["open-source"].toPlainText())
            dialog.browsers["open-source"].anchorClicked.emit(QUrl("licenses/PNUT-MIT.txt"))
            expected = (source_root / "licenses" / "PNUT-MIT.txt").read_text(encoding="utf-8")
            self.assertEqual(dialog.tabs.currentWidget().toPlainText(), expected)
            self.assertIn("MIT License", expected)
            external.assert_not_called()

    def test_cross_document_links_reuse_the_existing_markdown_tabs(self):
        dialog = self.dialog()
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as external:
            for source, target, filename in (("privacy", "terms", "terms.md"),
                                              ("terms", "open-source", "open-source.md"),
                                              ("open-source", "privacy", "privacy.md")):
                with self.subTest(source=source, target=target):
                    dialog.browsers[source].anchorClicked.emit(QUrl(filename))
                    self.assertIs(dialog.tabs.currentWidget(), dialog.browsers[target])
                    self.assertEqual(dialog.tabs.count(), 3)
            external.assert_not_called()

    def test_dialog_tabs_remain_accessible_at_480_with_a_long_license_name(self):
        dialog = self.dialog("open-source")
        name = "licensed-component-1.2.3-with-a-very-long-distribution-name-APACHE-2.0.txt"
        (self.legal / "licenses" / name).write_text(self.license_text, encoding="utf-8")
        dialog.resize(480, 360)
        dialog.browsers["open-source"].anchorClicked.emit(QUrl(f"licenses/{name}"))
        self.app.processEvents()
        self.assertEqual(dialog.width(), 480)
        self.assertEqual(dialog.tabs.currentWidget().toPlainText(), self.license_text)
        self.assertEqual(dialog.tabs.tabToolTip(dialog.tabs.currentIndex()), name)
        self.assertTrue(dialog.tabs.usesScrollButtons())
        self.assertEqual(dialog.tabs.elideMode(), Qt.TextElideMode.ElideRight)
        for index in range(dialog.tabs.count()):
            dialog.tabs.setCurrentIndex(index)
            self.app.processEvents()
            tab = dialog.tabs.tabBar().tabRect(index)
            self.assertGreater(tab.width(), 0)
            self.assertGreaterEqual(tab.left(), 0)
            self.assertLessEqual(tab.right(), dialog.tabs.tabBar().width())
            self.assertGreater(dialog.tabs.currentWidget().viewport().width(), 0)

    def test_traversal_other_drives_remote_files_and_unsupported_schemes_are_blocked(self):
        dialog = self.dialog("open-source")
        targets = ("../outside.txt", "licenses/../../outside.txt", "licenses/%2e%2e/%2e%2e/outside.txt",
                   "licenses\\..\\..\\outside.txt", "file://remote-server/share/license.txt",
                   "//remote-server/license.txt", "http://example.com", "ftp://example.com/license.txt",
                   "licenses/missing.txt", "licenses/%00.txt")
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as external:
            for target in (*map(QUrl, targets), QUrl.fromLocalFile(str(self.root / "outside.txt"))):
                with self.subTest(target=target.toString()):
                    dialog.browsers["open-source"].anchorClicked.emit(target)
                    self.assertEqual(dialog.tabs.count(), 3)
                    self.assertNotIn("PRIVATE OUTSIDE FILE", dialog.tabs.currentWidget().toPlainText())
            external.assert_not_called()

    def test_only_an_explicit_https_anchor_click_opens_external_browser(self):
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl", return_value=True) as external:
            dialog = self.dialog("open-source")
            for key in dialog.browsers:
                dialog.select_section(key)
            external.assert_not_called()
            url = QUrl("https://example.com/project")
            dialog.browsers["open-source"].anchorClicked.emit(url)
            external.assert_called_once_with(url)
            self.assertEqual(dialog.tabs.count(), 3)

    def test_frozen_and_source_legal_roots_resolve_to_bundled_documents(self):
        self.path_patch.stop()
        try:
            with patch.object(legal_dialog.sys, "_MEIPASS", str(self.root), create=True):
                self.assertEqual(legal_dialog.legal_directory(), self.legal)
                dialog = LegalDialog("privacy")
                self.windows.append(dialog)
                self.assertIn("Privacy Policy", dialog.browsers["privacy"].toPlainText())
            self.assertEqual(legal_dialog.legal_directory(), Path(legal_dialog.__file__).resolve().parents[2] / "legal")
        finally:
            self.path_patch.start()

    def test_footer_fits_minimum_main_window_and_remains_visible_on_every_page(self):
        window = self.main_window()
        window.on_state("waiting")
        window._status_bar.showMessage("A temporary status message")
        for page in window._pages:
            with self.subTest(page=page):
                window.show_page(page)
                self.app.processEvents()
                window.resize(window.minimumSize())
                self.app.processEvents()
                for button in window._legal_buttons.values():
                    self.assertTrue(button.isVisibleTo(window))
                    self.assertGreaterEqual(button.width(), button.sizeHint().width())
                    self.assertGreaterEqual(button.height(), button.sizeHint().height())
                    self.assertGreaterEqual(button.width(), QFontMetricsF(button.font()).horizontalAdvance(button.text().replace("&&", "&")))
                    self.assertEqual(button.font().pixelSize(), 11)
                    self.assertTrue(button.parentWidget().rect().contains(button.geometry()))
                footer = window._legal_buttons["privacy"].parentWidget()
                self.assertTrue(window._status_bar.rect().contains(footer.geometry()))
                self.assertLess(footer.geometry().right(), window._state_label.geometry().left())

    def test_missing_document_is_readable_without_throwing_or_network(self):
        (self.legal / "terms.md").unlink()
        with patch("mnmparse.app.legal_dialog.QDesktopServices.openUrl") as external:
            dialog = self.dialog("terms")
            self.assertIn("not available", dialog.browsers["terms"].toPlainText())
            external.assert_not_called()


if __name__ == "__main__":
    unittest.main()
