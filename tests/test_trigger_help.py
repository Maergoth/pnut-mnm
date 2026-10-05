"""The in-app guide navigates internally and returns to the unchanged trigger editor."""

from __future__ import annotations

from html.parser import HTMLParser
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QObject, QSettings, Qt, QUrl, Signal
    from PySide6.QtGui import QTextCursor
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialogButtonBox

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.config import Config
from mnmparse.triggers import Trigger, TriggerStore


class _GuideMarkup(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.targets: set[str] = set()
        self.links: list[str] = []
        self.headings: list[str] = []
        self._in_heading = False
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        for attr in ("name", "id"):
            if attributes.get(attr):
                self.targets.add(attributes[attr])
        if tag == "a" and attributes.get("href", "").startswith("#"):
            self.links.append(attributes["href"][1:])
        if tag in ("h1", "h2", "h3"):
            self._in_heading = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("h1", "h2", "h3"):
            self._in_heading = False

    def handle_data(self, text: str) -> None:
        if self._in_heading and text.strip():
            self.headings.append(text.strip())


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TriggerHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from mnmparse.app import theme

        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage

        class Runner(QObject):
            fired = Signal(object)

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trigger = Trigger(name="Smite", pattern="hits", timer=False, timer_label="My label")
        self.runner = Runner()
        self.runner.store = TriggerStore(self.root / "triggers.json")
        self.runner.store.triggers = [self.trigger]
        self.runner.save = Mock()
        self.runner.audio = SimpleNamespace(voices=lambda: [], devices=lambda: [])
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.page = TriggersPage(_MissingEngine(Config()), Config(), settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.set_runner(self.runner)
        self.page.test_line.setText("Your Righteous Smite II hits a rat for 154 points of Holy Damage.")
        self.page.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        if self.page._help_dialog is not None:
            self.page._help_dialog.close()
        self.page._save_timer.stop()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _open(self):
        self.page.timer_help.click()
        self.app.processEvents()
        dialog = self.page._help_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.WindowModal)
        return dialog

    def test_close_escape_and_titlebar_close_restore_unchanged_editor(self) -> None:
        original = self.trigger.to_dict()
        test_line = self.page.test_line.text()
        for action in ("button", "escape", "window"):
            with self.subTest(action=action):
                dialog = self._open()
                if action == "button":
                    dialog.buttons.button(QDialogButtonBox.StandardButton.Close).click()
                elif action == "escape":
                    QTest.keyClick(dialog, Qt.Key.Key_Escape)
                else:
                    dialog.close()
                self.assertIsNone(self.page._help_dialog)
                self.app.processEvents()
                self.assertTrue(self.page.isVisible())
                self.assertIs(self.page._current, self.trigger)
                self.assertEqual(self.trigger.to_dict(), original)
                self.assertEqual(self.page.timer_label.text(), "My label")
                self.assertEqual(self.page.test_line.text(), test_line)
                self.runner.save.assert_not_called()

    def test_repeated_help_uses_one_dialog_and_reopen_starts_at_top(self) -> None:
        dialog = self._open()
        self.page._open_help()
        self.assertIs(self.page._help_dialog, dialog)
        bar = dialog.browser.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        bar.setValue(bar.maximum())
        dialog.close()
        reopened = self._open()
        self.assertIsNot(reopened, dialog)
        self.assertEqual(reopened.browser.verticalScrollBar().value(), 0)

    def test_table_of_contents_links_and_headings_render_inside_app(self) -> None:
        from mnmparse.app.trigger_help_content import TRIGGER_HELP_HTML

        markup = _GuideMarkup(TRIGGER_HELP_HTML)
        self.assertGreater(len(markup.headings), 5)
        self.assertGreater(len(markup.links), 5)
        self.assertTrue(set(markup.links) <= markup.targets)
        dialog = self._open()
        browser = dialog.browser
        for heading in markup.headings:
            self.assertIn(heading, browser.toPlainText())
        self.assertTrue(browser.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByMouse)
        self.assertFalse(browser.openExternalLinks())
        self.assertFalse(browser.openLinks())

        # Click a rendered contents link, exercising QTextBrowser's anchor signal.
        block = browser.document().begin()
        fragment = None
        while block.isValid() and fragment is None:
            iterator = block.begin()
            while not iterator.atEnd():
                candidate = iterator.fragment()
                href = candidate.charFormat().anchorHref()
                if href.startswith("#") and href[1:] != "top":
                    fragment = candidate
                    break
                iterator += 1
            block = block.next()
        self.assertIsNotNone(fragment)
        cursor = QTextCursor(browser.document())
        cursor.setPosition(fragment.position() + 1)
        browser.setTextCursor(cursor)
        browser.ensureCursorVisible()
        point = browser.cursorRect(cursor).center()
        self.assertEqual(browser.anchorAt(point), fragment.charFormat().anchorHref())
        QTest.mouseClick(browser.viewport(), Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertGreater(browser.verticalScrollBar().value(), 0)
        before = browser.toPlainText()
        position = browser.verticalScrollBar().value()
        browser.anchorClicked.emit(QUrl("https://example.com/"))
        browser.anchorClicked.emit(QUrl("file:///C:/not-a-guide.html"))
        self.assertEqual(browser.toPlainText(), before)
        self.assertEqual(browser.verticalScrollBar().value(), position)

    def test_guide_wraps_at_narrow_width_and_initial_size_fits_screen(self) -> None:
        dialog = self._open()
        self.assertTrue(dialog.screen().availableGeometry().contains(dialog.geometry()))
        dialog.resize(dialog.minimumWidth(), 420)
        self.app.processEvents()
        # QTextDocument lays out long guides lazily; finish the full reflow before
        # checking the scroll range, which initially includes estimated widths.
        document_size = dialog.browser.document().documentLayout().documentSize()
        self.app.processEvents()
        self.assertLessEqual(document_size.width(), dialog.browser.viewport().width())
        self.assertEqual(dialog.browser.horizontalScrollBar().maximum(), 0)
        self.assertFalse(dialog.browser.horizontalScrollBar().isVisible())


if __name__ == "__main__":
    unittest.main()
