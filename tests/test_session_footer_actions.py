"""The Session footer archives only the live session and stays usable at narrow sizes."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from mnmparse.app import theme
from mnmparse.app.pages import SessionPage
from mnmparse.app.session_view import SessionView
from mnmparse.config import Config
from mnmparse.session import SessionStats
from tests.test_layout import clipped_widgets


def collecting_session():
    session = SessionStats("Hero", started=0)
    session.note_player_status({"xp_percent": 94.8, "xp_gain_points": 0,
                                "xp_observed_seconds": 0, "xp_points_per_hour": None,
                                "xp_rate_kind": "rolling", "xp_rate_window_seconds": 3600,
                                "xp_rate_gain_points": 0, "xp_rate_observed_seconds": 0})
    return session.snapshot(now=600)


class SessionFooterActionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.snapshot = collecting_session()
        self.engine = SimpleNamespace(session_snapshot=Mock(return_value=self.snapshot),
                                      reset_session=Mock(return_value=True),
                                      archived_sessions=Mock(return_value=[]),
                                      archived_session=Mock(return_value=self.snapshot),
                                      history=Mock(return_value=[]), is_running=False)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def page(self):
        page = SessionPage(self.engine, Config(player_name="Hero"), self.settings)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.resize(1000, 640)
        page.show()
        self.widgets.append(page)
        self.app.processEvents()
        return page

    def test_footer_button_archives_through_existing_handler_and_refreshes_history(self):
        page = self.page()
        events = []

        def reset():
            events.append("archived")
            self.engine.archived_sessions.return_value = [{"id": "old-session", "started": 100}]
            return True

        self.engine.reset_session.side_effect = reset
        page.new_session_requested.connect(lambda: events.append("new session"))
        button = page._view._new_session_button
        self.assertTrue(button.isVisible())
        self.assertIn("XP collecting…", page._view._footer.text())
        self.assertEqual([widget for widget in page.findChildren(QPushButton)
                          if widget.text() == "New session"], [button])
        button.click()
        self.engine.reset_session.assert_called_once_with()
        self.assertEqual(events, ["archived", "new session"])
        self.assertGreater(page._source.findData("archive:old-session"), 0)
        self.assertIn("Previous session archived", page._status.text())

    def test_failed_archive_preserves_live_session_and_does_not_emit_reset(self):
        page = self.page()
        events = []
        page.new_session_requested.connect(lambda: events.append(True))
        self.engine.reset_session.return_value = False
        initial_queries = self.engine.archived_sessions.call_count
        page._view._new_session_button.click()
        self.assertEqual(events, [])
        self.assertEqual(self.engine.archived_sessions.call_count, initial_queries)
        self.assertIn("remains active", page._status.text())

    def test_imported_and_archived_sources_cannot_reset_the_live_session(self):
        self.engine.archived_sessions.return_value = [{"id": "older", "started": 100}]
        page = self.page()
        button = page._view._new_session_button
        page.add_imported_session("old.log", self.snapshot)
        imported = page._source.currentIndex()
        archived = page._source.findData("archive:older")
        for index in (imported, archived):
            with self.subTest(index=index):
                page._source.setCurrentIndex(index)
                self.assertFalse(button.isEnabled())
                button.click()
                page._view.new_session_requested.emit()
                self.engine.reset_session.assert_not_called()
        page._source.setCurrentIndex(0)
        self.assertTrue(button.isEnabled())
        button.click()
        self.engine.reset_session.assert_called_once_with()

    def test_demo_disables_reset_and_restores_action_only_for_live_source(self):
        page = self.page()
        button = page._view._new_session_button
        page.set_demo_active(True)
        page.set_session(self.snapshot)
        self.assertFalse(button.isEnabled())
        self.assertFalse(page._export.isEnabled())
        page._view.new_session_requested.emit()
        self.engine.reset_session.assert_not_called()
        page.set_demo_active(False)
        self.assertTrue(button.isEnabled())
        self.assertTrue(page._export.isEnabled())
        page.add_imported_session("old.log", self.snapshot)
        page.set_demo_active(True)
        page.set_demo_active(False)
        self.assertFalse(button.isEnabled())

    def test_compact_and_display_only_views_do_not_offer_a_reset_action(self):
        for compact, opted_in in ((True, True), (True, False), (False, False)):
            with self.subTest(compact=compact, opted_in=opted_in):
                view = SessionView(compact, show_new_session=opted_in)
                self.widgets.append(view)
                self.assertIsNone(view._new_session_button)

    def test_footer_wraps_beside_bottom_right_button_without_clipping(self):
        view = SessionView(False, show_new_session=True)
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.set_config(Config(player_name="Hero"))
        view.set_snapshot(self.snapshot)
        self.widgets.append(view)
        view.show()
        for width in (480, 900):
            with self.subTest(width=width):
                view.resize(width, 600)
                self.app.processEvents()
                footer, button = view._footer, view._new_session_button
                self.assertLess(footer.geometry().right(), button.geometry().left())
                self.assertEqual(button.geometry().right(), view.width() - 17)
                self.assertEqual(button.geometry().bottom(), view.height() - 13)
                self.assertEqual(clipped_widgets(view), [])


if __name__ == "__main__":
    unittest.main()
