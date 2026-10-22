"""The Session footer archives only the live session and stays usable at narrow sizes."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton

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
        question = patch("mnmparse.app.pages.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes)
        self.question = question.start()
        self.addCleanup(question.stop)

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
        self.question.assert_called_once()
        self.assertEqual(self.question.call_args.args[2], "Start new session?")
        self.assertEqual(self.question.call_args.args[4], QMessageBox.StandardButton.Cancel)
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

    def test_footer_cancel_preserves_snapshot_archive_and_reset_side_effects(self):
        page = self.page()
        self.question.return_value = QMessageBox.StandardButton.Cancel
        reset = []
        page.new_session_requested.connect(lambda: reset.append(True))
        initial_queries = self.engine.archived_sessions.call_count
        QTest.mouseClick(page._view._footer, Qt.MouseButton.LeftButton)
        self.question.assert_called_once()
        self.engine.reset_session.assert_not_called()
        self.assertEqual(self.engine.archived_sessions.call_count, initial_queries)
        self.assertIs(page._live, self.snapshot)
        self.assertEqual(reset, [])

    def test_footer_confirm_uses_existing_reset_and_archives_exactly_once(self):
        page = self.page()
        reset = []
        page.new_session_requested.connect(lambda: reset.append(True))
        QTest.mouseClick(page._view._footer, Qt.MouseButton.LeftButton)
        self.question.assert_called_once()
        self.engine.reset_session.assert_called_once_with()
        self.assertEqual(reset, [True])

    def test_prompt_reentry_and_demo_or_source_changes_cannot_reset(self):
        page = self.page()

        def nested(*args):
            page.request_new_session()
            page.set_demo_active(True)
            return QMessageBox.StandardButton.Yes

        self.question.side_effect = nested
        QTest.mouseClick(page._view._footer, Qt.MouseButton.LeftButton)
        self.question.assert_called_once()
        self.engine.reset_session.assert_not_called()
        page.set_demo_active(False)
        self.question.reset_mock()
        self.question.side_effect = lambda *args: (page.add_imported_session("old.log", self.snapshot)
                                                   or QMessageBox.StandardButton.Yes)
        page.request_new_session()
        self.question.assert_called_once()
        self.engine.reset_session.assert_not_called()

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
                self.assertFalse(page._view._footer._action_enabled)
                QTest.mouseClick(page._view._footer, Qt.MouseButton.LeftButton)
                button.click()
                page._view.new_session_requested.emit()
                self.engine.reset_session.assert_not_called()
                self.question.assert_not_called()
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

    def test_display_only_footer_right_clicks_and_drags_are_not_reset_requests(self):
        page = self.page()
        footer = page._view._footer
        QTest.mouseClick(footer, Qt.MouseButton.RightButton)
        start = footer.rect().center()
        QTest.mousePress(footer, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(footer, start + type(start)(30, 0))
        QTest.mouseMove(footer, start)
        QTest.mouseRelease(footer, Qt.MouseButton.LeftButton, pos=start)
        self.question.assert_not_called()
        view = SessionView(False)
        self.widgets.append(view)
        view.set_snapshot(self.snapshot)
        view.set_new_session_enabled(True)
        self.assertFalse(view._footer._action_enabled)

    def test_compact_app_footer_targets_live_session_while_main_browses_archive(self):
        # Reuse the existing real App bootstrap fixture, including its normal
        # post-success connection to the Revenge session lifecycle.
        from tests.test_npc_settings import NpcSettingsIntegrationTests

        fixture = NpcSettingsIntegrationTests()
        fixture.app = self.app
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        host = fixture.bootstrap(Config(player_name="Hero"))
        page, overlay = host.window.page("session"), host.overlay
        host.engine.reset_session = Mock(return_value=True)
        host.engine.archived_sessions = Mock(return_value=[])
        page.set_session(self.snapshot)
        page._imported["archive:older"] = self.snapshot
        page._source.addItem("Previous session", "archive:older")
        page._source.setCurrentIndex(page._source.findData("archive:older"))
        overlay.set_session(self.snapshot)
        overlay.set_tab("session")
        overlay.set_click_through(False)
        host.window.hide()
        self.app.processEvents()
        marker, events = "fictional-attacker", []
        host.revenge._seen.add(marker)

        def archive():
            events.append(("archive", marker in host.revenge._seen))
            return True

        host.engine.reset_session.side_effect = archive
        page.new_session_requested.connect(lambda: events.append(("new session", marker in host.revenge._seen)))
        footer = overlay._session._footer
        self.assertTrue(footer._action_enabled)
        self.assertFalse(page._view._footer._action_enabled)
        self.question.return_value = QMessageBox.StandardButton.Cancel
        QTest.mouseClick(footer, Qt.MouseButton.LeftButton)
        self.question.assert_called_once()
        self.assertIs(self.question.call_args.args[0], overlay)
        self.assertEqual(page._source.currentData(), "archive:older")
        self.assertIn(marker, host.revenge._seen)
        host.engine.reset_session.assert_not_called()
        self.question.reset_mock()
        self.question.return_value = QMessageBox.StandardButton.Yes
        QTest.mouseClick(footer, Qt.MouseButton.LeftButton)
        self.question.assert_called_once()
        host.engine.reset_session.assert_called_once_with()
        self.assertEqual(events, [("archive", True), ("new session", False)])
        self.assertIsNone(page._source.currentData())
        self.question.reset_mock()
        overlay.set_click_through(True)
        overlay.new_session_requested.emit()  # A stale queued request rechecks input.
        self.question.assert_not_called()

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
