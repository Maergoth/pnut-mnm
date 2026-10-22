"""Restart notice timing and user decisions without an updater or native install."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QCoreApplication, QEvent, QRect, Qt
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QWidget
from shiboken6 import isValid

from mnmparse.app import update_restart
from mnmparse.app.update_restart import UpdateRestartNotice


class UpdateRestartNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = 100.0
        self.clock = patch.object(update_restart.time, "monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.notice = UpdateRestartNotice()
        self.addCleanup(self.dispose)
        self.restart = QSignalSpy(self.notice.restart_requested)
        self.postponed = QSignalSpy(self.notice.postponed)

    def dispose(self):
        if isValid(self.notice):
            self.notice.stop()
            self.notice.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def tick(self, now):
        self.now = now
        self.notice._tick()

    def test_ten_second_countdown_uses_elapsed_monotonic_time_once(self):
        self.notice.begin("9.8.7")
        self.assertIn("10 seconds", self.notice.status_label.text())
        self.tick(103.4)
        self.assertIn("7 seconds", self.notice.status_label.text())
        self.tick(109.999)
        self.assertEqual(self.restart.count(), 0)
        self.tick(110.0)
        self.tick(200.0)
        self.notice.restart_button.click()
        self.assertEqual(self.restart.count(), 1)
        self.assertEqual(self.postponed.count(), 0)
        self.assertFalse(self.notice._timer.isActive())
        self.assertFalse(self.notice.isVisible())

    def test_late_timer_delivery_does_not_extend_countdown(self):
        self.notice.begin("9.8.7")
        self.tick(125.0)
        self.assertEqual(self.restart.count(), 1)

    def test_qt_timer_delivers_restart_after_elapsed_deadline(self):
        self.notice.begin("9.8.7")
        self.now = 110.0
        QTest.qWait(250)
        self.assertEqual(self.restart.count(), 1)
        self.assertFalse(self.notice._timer.isActive())

    def test_restart_now_and_pending_timeout_only_emit_once(self):
        self.notice.begin("9.8.7")
        self.notice.restart_button.click()
        self.notice.restart_button.click()
        self.tick(200.0)
        self.notice.close()
        self.assertEqual(self.restart.count(), 1)
        self.assertEqual(self.postponed.count(), 0)

    def test_repeated_ready_same_version_does_not_reset_deadline(self):
        self.notice.begin("9.8.7")
        self.tick(106.0)
        self.notice.begin("9.8.7")
        self.assertIn("4 seconds", self.notice.status_label.text())
        self.tick(110.0)
        self.notice.begin("9.8.7")
        self.assertEqual(self.restart.count(), 1)
        self.assertFalse(self.notice.isVisible())

    def test_later_close_and_escape_each_postpone_and_cannot_rearm(self):
        for version, action in (("9.8.7", lambda: self.notice.later_button.click()),
                                ("9.8.8", lambda: self.notice.close()),
                                ("9.8.9", lambda: QTest.keyClick(self.notice, Qt.Key.Key_Escape))):
            with self.subTest(version=version):
                before = self.postponed.count()
                self.notice.begin(version)
                action()
                self.assertEqual(self.postponed.count(), before + 1)
                self.assertFalse(self.notice.isVisible())
                self.assertFalse(self.notice._timer.isActive())
                self.tick(self.now + 20)
                self.notice.begin(version)
                self.assertFalse(self.notice.isVisible())
        self.notice.begin("9.8.7")
        self.assertFalse(self.notice.isVisible())
        self.assertEqual(self.restart.count(), 0)

    def test_stop_is_silent_and_cancels_queued_timeout(self):
        self.notice.begin("9.8.7")
        self.notice.stop()
        self.tick(200.0)
        self.notice.close()
        self.notice.begin("9.8.7")
        self.assertEqual((self.restart.count(), self.postponed.count()), (0, 0))
        self.assertFalse(self.notice.isVisible())

    def test_failure_is_plain_persistent_and_allows_one_manual_retry(self):
        self.notice.begin("<b>9.8.7</b>")
        self.notice.restart_button.click()
        self.notice.show_failure("<b>Retry failed & waiting</b>")
        self.assertTrue(self.notice.isVisible())
        self.assertFalse(self.notice._timer.isActive())
        self.assertEqual(self.notice.status_label.text(), "<b>Retry failed & waiting</b>")
        for label in (self.notice.version_label, self.notice.status_label):
            self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
            self.assertTrue(label.wordWrap())
        self.tick(999.0)
        self.notice.begin("<b>9.8.7</b>")
        self.assertEqual(self.restart.count(), 1)
        self.assertTrue(self.notice.isVisible())
        self.notice.restart_button.click()
        self.notice.restart_button.click()
        self.assertEqual(self.restart.count(), 2)
        self.assertFalse(self.notice.isVisible())

    def test_failure_can_be_deferred_without_an_automatic_retry(self):
        self.notice.begin("9.8.7")
        self.notice.show_failure("A fictional restart error")
        self.notice.later_button.click()
        self.tick(999.0)
        self.notice.begin("9.8.7")
        self.assertEqual((self.restart.count(), self.postponed.count()), (0, 1))
        self.assertFalse(self.notice.isVisible())

    def test_independent_nonmodal_topmost_notice_preserves_focus(self):
        main = QWidget()
        entry = QLineEdit(main)
        main.show()
        main.activateWindow()
        entry.setFocus()
        self.app.processEvents()
        focused = QApplication.focusWidget()
        self.assertIs(focused, entry)
        self.notice.begin("9.8.7")
        self.app.processEvents()
        self.assertIsNone(self.notice.parentWidget())
        self.assertTrue(self.notice.isWindow())
        self.assertFalse(self.notice.isModal())
        self.assertTrue(self.notice.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        self.assertTrue(self.notice.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        # The offscreen plugin activates every shown top-level window, even a
        # bare QWidget with WA_ShowWithoutActivating. Native Qt must preserve
        # focus; the platform-independent contract above checks the show flag.
        if QApplication.platformName() != "offscreen":
            self.assertIs(QApplication.focusWidget(), focused)
        main.showMinimized()
        self.app.processEvents()
        self.assertTrue(self.notice.isVisible())
        main.hide()
        self.app.processEvents()
        self.assertTrue(self.notice.isVisible())
        main.close()
        main.deleteLater()

    def test_long_failure_wraps_and_window_fits_available_screen(self):
        class Screen:
            def availableGeometry(self):
                return QRect(-600, 40, 420, 300)

        with patch.object(update_restart.QGuiApplication, "screenAt", return_value=Screen()):
            self.notice.begin("9.8.7")
            self.notice.show_failure("A long fictional failure description. " * 100)
        self.app.processEvents()
        self.assertTrue(Screen().availableGeometry().contains(self.notice.frameGeometry()))
        self.assertLess(self.notice.width(), 420)
        self.assertGreater(self.notice._scroll.verticalScrollBar().maximum(), 0)

    def test_deleting_notice_destroys_active_timer_without_signals(self):
        self.notice.begin("9.8.7")
        timer = self.notice._timer
        self.notice.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(timer))
        self.assertEqual((self.restart.count(), self.postponed.count()), (0, 0))


if __name__ == "__main__":
    unittest.main()
