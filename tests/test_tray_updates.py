"""Downloaded application updates remain accessible from the tray menu."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from mnmparse.app.tray import TrayIcon


class TrayUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tray = TrayIcon(QIcon())
        self.requests = []
        self.tray.restart_update_requested.connect(lambda: self.requests.append("restart"))

    def tearDown(self):
        self.tray.hide()
        self.tray._menu.close()
        self.tray.deleteLater()
        self.app.processEvents()

    def test_update_action_is_initially_hidden_and_cleared(self):
        action = self.tray._update_action
        self.assertFalse(action.isVisible())
        self.assertFalse(action.isEnabled())
        self.assertEqual(action.text(), "")
        action.trigger()
        self.assertEqual(self.requests, [])

    def test_ready_version_shows_one_action_before_quit(self):
        self.tray.set_update_ready("1.2.3")
        action = self.tray._update_action
        self.assertTrue(action.isVisible())
        self.assertTrue(action.isEnabled())
        self.assertEqual(action.text(), "Restart to update to PNUT 1.2.3")
        self.tray.set_update_ready("1.2.4")
        actions = self.tray.contextMenu().actions()
        self.assertEqual(action.text(), "Restart to update to PNUT 1.2.4")
        self.assertEqual(actions.count(action), 1)
        self.assertLess(actions.index(action), actions.index(self.tray._quit_action))
        self.assertEqual(self.requests, [])

    def test_ready_action_emits_only_update_restart_request(self):
        other_requests = []
        self.tray.quit_requested.connect(lambda: other_requests.append("quit"))
        self.tray.toggle_capture_requested.connect(lambda: other_requests.append("capture"))
        self.tray.set_update_ready("1.2.3")
        self.tray._update_action.trigger()
        self.assertEqual(self.requests, ["restart"])
        self.assertEqual(other_requests, [])

    def test_clearing_ready_version_hides_and_removes_stale_label(self):
        self.tray.set_update_ready("1.2.3")
        self.tray.set_update_ready("  ")
        action = self.tray._update_action
        self.assertFalse(action.isVisible())
        self.assertFalse(action.isEnabled())
        self.assertEqual(action.text(), "")
        action.trigger()
        self.assertEqual(self.requests, [])

    def test_existing_state_labels_and_update_readiness_are_independent(self):
        self.tray.set_update_ready("1.2.3")
        self.tray.set_window_visible(False)
        self.tray.set_capture_running(True)
        self.tray.set_overlay_visible(True)
        self.tray.set_overlay_locked(True)
        self.tray.set_overlay_click_through(True)
        self.tray.set_muted(True)
        self.assertEqual([self.tray._window_action.text(), self.tray._capture_action.text(),
                          self.tray._overlay_action.text(), self.tray._lock_action.text(),
                          self.tray._quit_action.text()],
                         ["Show window", "Stop capture", "Hide overlay", "Unlock overlay", "Quit"])
        self.assertTrue(self.tray._click_through_action.isChecked())
        self.assertTrue(self.tray._mute_action.isChecked())
        self.assertEqual(self.tray._update_action.text(), "Restart to update to PNUT 1.2.3")
        self.tray.set_update_ready("")
        self.assertEqual(self.tray._capture_action.text(), "Stop capture")
        self.assertTrue(self.tray._click_through_action.isChecked())
        self.assertTrue(self.tray._mute_action.isChecked())


if __name__ == "__main__":
    unittest.main()
