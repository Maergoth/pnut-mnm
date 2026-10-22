"""A shared settings gear responds to real host/child hover and safe teardown."""
from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from mnmparse.app.overlay_settings import OverlaySettingsGear


class OverlaySettingsGearTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.host.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.host.setGeometry(100, 100, 240, 120)
        self.cursor = QPoint(-10000, -10000)
        self.cursor_patch = patch("mnmparse.app.overlay_settings.QCursor.pos", side_effect=lambda: self.cursor)
        self.cursor_patch.start()
        self.enabled = True
        self.callback = Mock()
        self.gear = OverlaySettingsGear(self.host, self.callback, label="combat overlay",
                                        input_enabled=lambda: self.enabled)
        self.host.show()
        self.app.processEvents()

    def tearDown(self):
        if isValid(self.host):
            self.host.close()
            self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.cursor_patch.stop()

    def enter(self, widget):
        local = widget.rect().center()
        self.cursor = widget.mapToGlobal(local)
        self.app.sendEvent(widget, QEnterEvent(QPointF(local), QPointF(local), QPointF(self.cursor)))

    def leave_host(self):
        self.cursor = self.host.mapToGlobal(QPoint(self.host.width() + 10, 10))
        self.app.sendEvent(self.host, QEvent(QEvent.Type.Leave))

    def test_host_enter_shows_accessible_nonfocusable_gear_at_top_left(self):
        self.assertFalse(self.gear.isVisible())
        self.enter(self.host)
        self.assertTrue(self.gear.isVisible())
        self.assertTrue(self.gear.isEnabled())
        self.assertIs(self.gear.parentWidget(), self.host)
        self.assertEqual((self.gear.x(), self.gear.y(), self.gear.width(), self.gear.height()), (4, 4, 22, 22))
        self.assertEqual(self.gear.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertEqual(self.gear.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.assertEqual(self.gear.toolTip(), "Open combat overlay settings")
        self.assertEqual(self.gear.accessibleName(), self.gear.toolTip())
        self.callback.assert_not_called()

    def test_child_hover_including_later_children_keeps_gear_visible_until_host_leave(self):
        child = QWidget(self.host)
        nested = QWidget(child)
        child.setGeometry(40, 35, 100, 70)
        nested.setGeometry(10, 10, 50, 30)
        child.show()
        nested.show()
        self.enter(nested)
        self.assertTrue(self.gear.isVisible())
        self.enter(self.gear)
        self.app.sendEvent(nested, QEvent(QEvent.Type.Leave))
        self.assertTrue(self.gear.isVisible())
        self.leave_host()
        self.assertFalse(self.gear.isVisible())
        self.assertFalse(self.gear.isEnabled())

    def test_click_invokes_callback_only_while_visible_and_input_enabled(self):
        self.enter(self.host)
        self.enter(self.gear)
        QTest.mouseClick(self.gear, Qt.MouseButton.LeftButton)
        self.callback.assert_called_once_with()
        self.enabled = False  # A stale queued click must recheck click-through.
        self.gear.click()
        self.callback.assert_called_once_with()
        self.assertFalse(self.gear.isVisible())
        self.enter(self.host)
        self.assertFalse(self.gear.isVisible())
        self.enabled = True
        self.gear.sync()
        self.assertTrue(self.gear.isVisible())

    def test_hide_disable_and_stale_child_events_cannot_reveal_gear(self):
        child = QWidget(self.host)
        child.show()
        self.enter(child)
        self.host.hide()
        self.assertFalse(self.gear.isVisible())
        self.enter(child)
        self.assertFalse(self.gear.isVisible())
        self.host.show()
        self.gear.sync()
        self.assertTrue(self.gear.isVisible())
        self.host.setEnabled(False)
        self.assertFalse(self.gear.isVisible())
        self.gear.click()
        self.callback.assert_not_called()

    def test_resize_and_move_preserve_position_and_refresh_hover(self):
        self.enter(self.host)
        self.host.resize(400, 180)
        self.assertTrue(self.gear.isVisible())
        self.assertEqual(self.gear.pos(), QPoint(4, 4))
        self.host.move(2000, 2000)
        self.assertFalse(self.gear.isVisible())
        self.enter(self.host)
        self.assertTrue(self.gear.isVisible())

    def test_other_windows_do_not_reveal_this_hosts_gear(self):
        other = QWidget()
        other.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        other.setGeometry(1000, 1000, 100, 100)
        other.show()
        self.enter(other)
        self.assertFalse(self.gear.isVisible())
        other.close()
        other.deleteLater()

    def test_hidden_host_does_not_filter_events_from_other_windows_and_show_reattaches(self):
        other = QWidget()
        observed = []

        class RecordingGear(OverlaySettingsGear):
            def eventFilter(self, watched, event):
                if watched is other:
                    observed.append(event.type())
                return super().eventFilter(watched, event)

        gear = RecordingGear(self.host, self.callback, label="recording test")
        self.host.hide()
        self.app.sendEvent(other, QEvent(QEvent.Type.Enter))
        self.assertEqual(observed, [])
        self.host.show()
        self.app.sendEvent(other, QEvent(QEvent.Type.Enter))
        self.assertEqual(observed, [QEvent.Type.Enter])
        self.host.hide()
        self.app.sendEvent(other, QEvent(QEvent.Type.Enter))
        self.assertEqual(observed, [QEvent.Type.Enter])
        self.assertFalse(gear.isVisible())
        other.deleteLater()

    def test_host_deletion_destroys_gear_and_leaves_no_global_event_callback(self):
        self.enter(self.host)
        self.host.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(self.gear))
        other = QWidget()
        self.app.sendEvent(other, QEvent(QEvent.Type.Enter))
        other.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
