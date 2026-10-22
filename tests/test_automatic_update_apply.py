"""Downloaded updates reach verified restart without a Settings-page visit."""
from __future__ import annotations

import inspect
import os
from pathlib import Path
import tempfile
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.main import App
from mnmparse.app import update_restart


class Host:
    def __init__(self, config_path):
        self._selftest = self._shut_down = self._quit_pending = False
        self._update_notice = None
        self._update_ready_version = self._update_last_status = ""
        self.config_path = config_path
        self.settings_page = SimpleNamespace(has_unsaved_changes=Mock(return_value=False),
                                             set_app_update_status=Mock())
        self.trigger_page = SimpleNamespace(flush_pending_changes=Mock(return_value=True))
        self.window = SimpleNamespace(
            page=lambda name: {"settings": self.settings_page, "triggers": self.trigger_page}.get(name),
            show_message=Mock())
        self.tray = SimpleNamespace(notify=Mock(), set_update_ready=Mock())
        self.revenge = SimpleNamespace(flush_pending_changes=Mock(return_value=True))
        self.app_updates = SimpleNamespace(ready_package=Path("verified-package"), ready_version="9.8.7",
                                           automatic_restart_allowed=Mock(return_value=True),
                                           prepare_restart=Mock(return_value=True))
        self.request_quit = Mock(side_effect=self.mark_quitting)

    def mark_quitting(self):
        self._quit_pending = True

    def __getattr__(self, name):
        method = getattr(App, name)
        if isinstance(inspect.getattr_static(App, name), staticmethod):
            return method
        return MethodType(method, self)


class AutomaticUpdateApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = Host(Path(self.temporary.name) / "character profile.json")
        self.now = 100.0
        clock = patch.object(update_restart.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.addCleanup(self.cleanup_notice)

    def cleanup_notice(self):
        if self.host._update_notice is not None:
            self.host._update_notice.stop()
            self.host._update_notice.deleteLater()
        self.app.processEvents()

    def ready(self):
        self.host._on_app_update_ready("9.8.7")
        return self.host._update_notice

    def expire(self):
        self.now += 11
        self.host._update_notice._tick()

    def test_ready_notice_automatically_prepares_and_quits_after_countdown(self):
        notice = self.ready()
        self.assertTrue(notice.isVisible())
        self.assertIn("Restarting in 10 seconds", notice.status_label.text())
        self.host.tray.set_update_ready.assert_called_with("9.8.7")
        self.host.tray.notify.assert_called_once()
        self.host.app_updates.prepare_restart.assert_not_called()
        events = []
        self.host.trigger_page.flush_pending_changes.side_effect = lambda: events.append("triggers") or True
        self.host.revenge.flush_pending_changes.side_effect = lambda: events.append("revenge") or True
        self.host.app_updates.prepare_restart.side_effect = lambda **kwargs: events.append("verify") or True
        self.expire()
        self.assertEqual(events, ["triggers", "revenge", "verify"])
        self.host.app_updates.prepare_restart.assert_called_once_with(
            arguments=["--config", str(self.host.config_path)])
        self.host.request_quit.assert_called_once()
        self.host._on_app_update_ready("9.8.7")
        self.assertFalse(notice.isVisible())
        self.assertEqual(self.host.app_updates.prepare_restart.call_count, 1)

    def test_later_preserves_readiness_and_duplicate_ready_does_not_rearm(self):
        notice = self.ready()
        notice.later_button.click()
        self.host._on_app_update_ready("9.8.7")
        self.expire()
        self.assertIs(self.host._update_notice, notice)
        self.assertFalse(notice.isVisible())
        self.assertIn("downloaded", self.host._update_last_status)
        self.host.app_updates.prepare_restart.assert_not_called()
        self.host.request_quit.assert_not_called()
        # The same handler is used by Settings and the persistent tray action.
        self.assertTrue(self.host.restart_for_update())
        self.host.request_quit.assert_called_once()

    def test_ready_is_ignored_during_selftest_shutdown_or_quit(self):
        for flag in ("_selftest", "_shut_down", "_quit_pending"):
            with self.subTest(flag=flag):
                setattr(self.host, flag, True)
                self.assertIsNone(self.ready())
                setattr(self.host, flag, False)
        self.host.app_updates.prepare_restart.assert_not_called()

    def test_verified_restart_failure_stays_visible_and_never_quits(self):
        notice = self.ready()
        error = "Could not install the update: checksum mismatch."

        def failure(**_kwargs):
            self.host._on_app_update_status(error)
            return False

        self.host.app_updates.prepare_restart.side_effect = failure
        self.expire()
        self.assertTrue(notice.isVisible())
        self.assertEqual(notice.status_label.text(), error)
        self.assertFalse(notice._timer.isActive())
        self.host.request_quit.assert_not_called()
        self.now += 100
        notice._tick()
        self.assertEqual(self.host.app_updates.prepare_restart.call_count, 1)
        self.host.app_updates.prepare_restart.side_effect = None
        notice.restart_button.click()
        self.host.request_quit.assert_called_once()

    def test_failed_release_requires_explicit_retry_instead_of_loop(self):
        self.host.app_updates.automatic_restart_allowed.return_value = False
        notice = self.ready()
        self.assertTrue(notice.isVisible())
        self.assertIn("Automatic retry is paused", notice.status_label.text())
        self.assertNotIn("failed", notice.status_label.text())
        self.expire()
        self.host.app_updates.prepare_restart.assert_not_called()
        notice.restart_button.click()
        self.host.app_updates.prepare_restart.assert_called_once()
        self.host.request_quit.assert_called_once()

    def test_postponing_failed_release_does_not_promise_an_automatic_retry(self):
        self.host.app_updates.automatic_restart_allowed.return_value = False
        notice = self.ready()
        notice.later_button.click()
        self.assertIn("Automatic retry is paused", self.host._update_last_status)
        self.assertNotIn("next launch", self.host._update_last_status)

    def test_unsaved_settings_or_failed_flush_block_helper_before_shutdown(self):
        for source in ("settings", "triggers", "revenge", "setup"):
            with self.subTest(source=source):
                self.host.settings_page.has_unsaved_changes.return_value = source == "settings"
                self.host.trigger_page.flush_pending_changes.return_value = source != "triggers"
                self.host.revenge.flush_pending_changes.return_value = source != "revenge"
                self.host._setup_wizard = SimpleNamespace(isVisible=lambda: source == "setup")
                self.assertFalse(self.host.restart_for_update())
                self.assertTrue(self.host._update_notice.isVisible())
                self.assertIn("paused", self.host._update_last_status)
                self.host.app_updates.prepare_restart.assert_not_called()
                self.host.request_quit.assert_not_called()

    def test_progress_and_up_to_date_status_are_visible_outside_settings(self):
        self.host.app_updates.ready_package = None
        self.host._on_app_update_started()
        self.host.window.show_message.assert_called_with("Checking GitHub for updates…")
        self.host.settings_page.set_app_update_status.assert_called_with("Checking GitHub for updates…", True)
        self.host._on_app_update_progress("Downloading 40%")
        self.host.window.show_message.assert_called_with("Downloading 40%")
        self.host._on_app_update_status("PNUT is up to date.")
        self.host.tray.set_update_ready.assert_called_with("")
        self.assertIsNone(self.host._update_notice)
        self.host.request_quit.assert_not_called()

    def test_unexpected_save_error_is_visible_without_launching_helper(self):
        self.host.trigger_page.flush_pending_changes.side_effect = OSError("write failed")
        self.assertFalse(self.host.restart_for_update())
        self.assertIn("Pending changes could not be saved", self.host._update_last_status)
        self.assertTrue(self.host._update_notice.isVisible())
        self.host.app_updates.prepare_restart.assert_not_called()
        self.host.request_quit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
