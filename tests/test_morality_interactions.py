"""The red lever requires a deliberate gesture before requesting full mode."""
from __future__ import annotations

import os
import unittest
from unittest.mock import Mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mnmparse.app.morality import MoralityPanel, RedModeSwitch
from mnmparse.config import Config


class MoralityInteractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widget = MoralityPanel(Config(reduced_motion=True))
        self.widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.widget.resize(520, 330)
        self.widget.show()
        self.widget._confirm = Mock()
        self.requests, self.sirens = [], []
        self.widget.mode_requested.connect(self.requests.append)
        self.widget.siren_requested.connect(lambda: self.sirens.append(True))
        self.app.processEvents()

    def tearDown(self):
        self.widget.set_config(Config(reduced_motion=True))
        self.widget.close()
        self.widget.deleteLater()
        self.app.processEvents()

    def drag(self, fraction):
        switch = self.widget.switch
        start = switch._knob_rect().center().toPoint()
        end = QPoint(round(start.x() + (switch.width() - 106) * fraction), start.y())
        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(switch, end)
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=end)

    def test_plain_handle_track_and_programmatic_clicks_never_request_full_mode(self):
        switch = self.widget.switch
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=switch._knob_rect().center().toPoint())
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=QPoint(switch.width() - 45, 50))
        switch.click()
        self.app.processEvents()
        self.assertTrue(switch.isChecked())
        self.assertFalse(self.widget._pending)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.sirens, [])
        self.widget._confirm.assert_not_called()

    def test_short_drag_returns_immediately_with_reduced_motion_and_no_siren(self):
        self.drag(.4)
        self.app.processEvents()
        self.assertEqual(self.widget.switch.position, 0)
        self.assertTrue(self.widget.switch.isChecked())
        self.assertFalse(self.widget._pending)
        self.assertEqual(self.sirens, [])
        self.widget._confirm.assert_not_called()

    def test_long_drag_requests_one_confirmation_while_casual_stays_active(self):
        self.drag(.9)
        self.assertTrue(self.widget._pending)
        self.assertTrue(self.widget.switch.isChecked())
        self.assertFalse(self.widget.switch.isEnabled())
        self.assertEqual(self.widget.switch.position, 1)
        self.assertEqual(self.sirens, [True])
        self.assertEqual(self.requests, [], "Dragging does not itself reveal individual information")
        self.app.processEvents()
        self.widget._confirm.assert_called_once()

    def test_keyboard_requires_documented_shift_right_not_enter_space_or_right(self):
        switch = self.widget.switch
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space, Qt.Key.Key_Right):
            QTest.keyClick(switch, key)
        self.assertFalse(self.widget._pending)
        self.assertEqual(self.sirens, [])
        self.assertNotIn("Shift+Right", self.widget.interaction_hint.text())
        self.assertIn("Shift+Right", switch.accessibleDescription())
        QTest.keyClick(switch, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
        self.app.processEvents()
        self.assertEqual(self.sirens, [True])
        self.widget._confirm.assert_called_once()
        self.assertEqual(self.requests, [])

    def test_short_drag_uses_return_animation_when_motion_enabled(self):
        self.widget.set_config(Config(reduced_motion=False))
        # Explicitly test the animation path even if Windows disables animation.
        self.widget.switch.motion_enabled = True
        self.drag(.4)
        self.assertEqual(self.widget.switch.animation.endValue(), 0)
        self.assertEqual(self.sirens, [])
        self.widget.switch.animation.setCurrentTime(self.widget.switch.animation.duration())
        self.assertEqual(self.widget.switch.position, 0)

    def test_policy_replacement_cancels_delayed_confirmation(self):
        self.drag(.9)
        self.widget.set_config(Config(reduced_motion=True))
        self.app.processEvents()
        self.assertFalse(self.widget._pending)
        self.assertTrue(self.widget.switch.isEnabled())
        self.assertEqual(self.widget.switch.position, 0)
        self.widget._confirm.assert_not_called()

    def test_full_mode_returns_to_casual_with_one_click_without_siren(self):
        self.widget.set_config(Config(casual_mode=False, casual_mode_confirmed=True, reduced_motion=True))
        switch = self.widget.switch
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton, pos=switch._knob_rect().center().toPoint())
        self.assertEqual(self.requests, [True])
        self.assertEqual(self.sirens, [])
        self.widget._confirm.assert_not_called()

    def test_full_mode_returns_to_casual_with_space(self):
        self.widget.set_config(Config(casual_mode=False, casual_mode_confirmed=True, reduced_motion=True))
        QTest.keyClick(self.widget.switch, Qt.Key.Key_Space)
        self.assertEqual(self.requests, [True])
        self.assertEqual(self.sirens, [])

    def test_escape_aborts_a_drag_and_accessibility_tracks_current_mode(self):
        switch = self.widget.switch
        start = switch._knob_rect().center().toPoint()
        end = QPoint(round(start.x() + (switch.width() - 106) * .8), start.y())
        QTest.mousePress(switch, Qt.MouseButton.LeftButton, pos=start)
        QTest.mouseMove(switch, end)
        QTest.keyClick(switch, Qt.Key.Key_Escape)
        QTest.mouseRelease(switch, Qt.MouseButton.LeftButton, pos=end)
        self.assertEqual(switch.position, 0)
        self.assertEqual(self.sirens, [])
        self.assertFalse(self.widget._pending)
        self.assertEqual(switch.accessibleName(), "Casual Mode")
        self.widget.set_config(Config(casual_mode=False, casual_mode_confirmed=True, reduced_motion=True))
        self.assertEqual(switch.accessibleName(), "Elitist Scumbag Mode")


if __name__ == "__main__":
    unittest.main()
