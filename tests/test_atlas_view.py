"""Atlas lifecycle/navigation tests never load a native DLL or use the network."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QUrl, Qt
from PySide6.QtWidgets import QApplication
import shiboken6

from mnmparse.app import atlas_native as native, atlas_view
from mnmparse.atlas import atlas_url


class FakeHost:
    instances = []

    def __init__(self, hwnd, storage, callback):
        self.hwnd, self.storage, self.callback = hwnd, Path(storage), callback
        self.runtime_version = "150.0.0.0"
        self.calls = []
        self.url = ""
        self.closed = False
        self.instances.append(self)

    def event(self, event, value=0, data=""):
        return self.callback(event, value, data) if self.callback is not None else 0

    def ready(self):
        return self.event(native.READY)

    def finish(self, url=None, ok=True):
        self.url = self.url if url is None else url
        self.event(native.URL_CHANGED, 0, self.url)
        self.event(native.LOAD_DONE, int(ok), "" if ok else "Synthetic failure")

    def navigate(self, url):
        self.url = url
        self.calls.append(("navigate", url))
        self.event(native.LOAD_START, 0, url)

    def load_html(self, html):
        self.calls.append(("load_html", html))

    def evaluate(self, script, request_id):
        self.calls.append(("evaluate", script, request_id))

    def resize(self):
        self.calls.append(("resize",))

    def set_visible(self, visible):
        self.calls.append(("set_visible", visible))

    def suspend(self):
        self.calls.append(("suspend",))

    def stop(self):
        self.calls.append(("stop",))

    def resume(self):
        self.calls.append(("resume",))

    def close(self, *_args):
        if not self.closed:
            self.closed = True
            self.calls.append(("close",))
            self.callback = None

    @property
    def navigations(self):
        return [call[1] for call in self.calls if call[0] == "navigate"]


class AtlasViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "atlas"
        self.views = []
        FakeHost.instances.clear()
        self.host_patch = patch.object(native, "NativeHost", FakeHost)
        self.host_patch.start()

    def tearDown(self):
        for view in reversed(self.views):
            if shiboken6.isValid(view):
                view.shutdown()
                shiboken6.delete(view)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        self.host_patch.stop()
        FakeHost.instances.clear()
        self.temp.cleanup()

    def view(self, **kwargs):
        view = atlas_view.AtlasView(storage_path=self.path, **kwargs)
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.views.append(view)
        return view

    def loaded_view(self, zone="Night Harbor", npc=""):
        view = self.view()
        view.activate(zone, npc)
        view._native.ready()
        view._native.finish()
        return view

    def test_import_and_construction_are_offline_and_do_not_load_native_library(self):
        code = """
import ctypes,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
app=QApplication([])
with patch.object(ctypes,'WinDLL',side_effect=AssertionError('native DLL loaded'),create=True):
    from mnmparse.app.atlas_view import AtlasView
    with tempfile.TemporaryDirectory() as directory:
        path=Path(directory)/'absent'
        view=AtlasView(storage_path=path)
        view.set_zone('Night Harbor')
        view.focus_npc('Fictional guardian')
        assert not path.exists()
        assert view._native is None
        assert not any(name.startswith('PySide6.QtWebEngine') for name in sys.modules)
        view.shutdown()
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_activation_waits_for_ready_and_uses_documented_url_and_credit(self):
        view = self.view()
        view.activate("Night Harbor", "Fictional guardian")
        host = view._native
        self.assertIsInstance(host.hwnd, int)
        self.assertEqual(host.storage, self.path / "webview2")
        self.assertEqual(host.navigations, [])
        self.assertTrue(view._timeout.isActive())
        self.assertIs(view._stack.currentWidget(), view._notice)
        host.ready()
        self.assertEqual(host.navigations, [atlas_url("Night Harbor", "Fictional guardian")])
        host.finish()
        self.assertIs(view._stack.currentWidget(), view._browser)
        self.assertFalse(view._timeout.isActive())
        self.assertTrue(any(label.text() == "Maps by MnM Atlas" for label in view.findChildren(atlas_view.QLabel)))

    def test_zone_changed_before_ready_navigates_only_latest_requested_zone(self):
        view = self.view()
        view.activate("Night Harbor")
        view.set_zone("Shaded Dunes")
        host = view._native
        self.assertEqual(len(FakeHost.instances), 1)
        self.assertEqual(host.navigations, [])
        host.ready()
        self.assertEqual(host.navigations, [atlas_url("Shaded Dunes")])

    def test_hidden_readiness_suspends_until_reactivated(self):
        view = self.view()
        view.activate("Night Harbor")
        host = view._native
        view.deactivate()
        view.set_zone("Shaded Dunes")
        host.ready()
        self.assertEqual(host.navigations, [])
        self.assertIn(("set_visible", False), host.calls)
        self.assertIn(("suspend",), host.calls)
        self.assertFalse(view._timeout.isActive())
        view.activate("Shaded Dunes")
        self.assertEqual(host.navigations, [atlas_url("Shaded Dunes")])

    def test_same_map_and_spa_zone_echo_preserve_page_view(self):
        view = self.loaded_view()
        host = view._native
        view.set_zone("Night Harbor")
        self.assertEqual(len(host.navigations), 1)
        changes = []
        view.zone_changed.connect(changes.append)
        url = atlas_url("Shaded Dunes") + "&level=lower&x=120&y=240&z=2"
        host.event(native.URL_CHANGED, 0, url)
        view.set_zone("Shaded Dunes")
        self.assertEqual(changes, ["Shaded Dunes"])
        self.assertEqual(len(host.navigations), 1)
        self.assertEqual(view._document_url.toString(), url)

    def test_world_navigation_survives_hide_and_reactivation(self):
        view = self.loaded_view()
        host = view._native
        changes = []
        view.zone_changed.connect(changes.append)
        host.event(native.URL_CHANGED, 0, atlas_url())
        self.assertEqual(changes, ["World"])
        self.assertEqual(view._zone, "World")
        view.deactivate()
        view.activate("World")
        self.assertEqual(len(host.navigations), 1)
        self.assertIn(("suspend",), host.calls)
        self.assertIn(("resume",), host.calls)

    def test_programmatic_unsupported_zone_world_fallback_retains_context(self):
        view = self.loaded_view()
        changes = []
        view.zone_changed.connect(changes.append)
        view.set_zone("Fictional Uncharted Zone")
        view._native.finish()
        self.assertEqual(view._zone, "Fictional Uncharted Zone")
        self.assertEqual(changes, [])
        self.assertEqual(view._document_url.toString(), atlas_url())

    def test_focus_npc_is_literal_and_hidden_updates_wait(self):
        view = self.view()
        view.set_zone("Night Harbor")
        self.assertIsNone(view._native)
        view.activate("Night Harbor")
        host = view._native
        host.ready()
        host.finish()
        view.focus_npc("Fictional & Guardian")
        self.assertEqual(host.navigations[-1], atlas_url("Night Harbor", "Fictional & Guardian"))
        view.deactivate()
        count = len(host.navigations)
        view.set_zone("Shaded Dunes")
        host.event(native.LOAD_DONE, 0, "Synthetic late failure")
        self.assertEqual(len(host.navigations), count)
        self.assertFalse(view._timeout.isActive())

    def test_hidden_spa_navigation_does_not_override_pending_zone_or_notify(self):
        view = self.loaded_view()
        changes = []
        view.zone_changed.connect(changes.append)
        view.deactivate()
        view.set_zone("Shaded Dunes")
        view._native.event(native.URL_CHANGED, 0, atlas_url("Fallen Pass"))
        self.assertEqual(changes, [])
        self.assertEqual(view._zone, "Shaded Dunes")
        view.activate("Shaded Dunes")
        self.assertEqual(view._native.navigations[-1], atlas_url("Shaded Dunes"))

    def test_navigation_allows_only_atlas_and_initial_about_blank(self):
        view = self.view()
        view.activate("Night Harbor")
        host = view._native
        self.assertEqual(host.event(native.NAV_REQUEST, 0, "about:blank"), 1)
        self.assertEqual(host.event(native.NAV_REQUEST, 0, atlas_url("Shaded Dunes")), 1)
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            for url in ("https://www.mnmatlas.com.evil.test/", "http://www.mnmatlas.com/",
                        "file:///private.txt", "https://user:password@www.mnmatlas.com/", "javascript:alert(1)"):
                self.assertEqual(host.event(native.NAV_REQUEST, 0, url), 0)
            self.assertEqual(host.event(native.NAV_REQUEST, 2, "https://example.test/redirect"), 0)
            opened.assert_not_called()
            self.assertEqual(host.event(native.NAV_REQUEST, 1, "https://example.test/"), 0)
            opened.assert_called_once_with(QUrl("https://example.test/"))
        host.ready()
        host.finish()
        self.assertEqual(host.event(native.NAV_REQUEST, 0, "about:blank"), 0)
        view.deactivate()
        self.assertEqual(host.event(native.NAV_REQUEST, 0, atlas_url("Shaded Dunes")), 0)

    def test_external_user_link_keeps_current_map_without_allowed_load_start(self):
        view = self.loaded_view()
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            self.assertEqual(view._native.event(native.NAV_REQUEST, 1, "https://example.test/"), 0)
            opened.assert_called_once()
        self.assertIs(view._stack.currentWidget(), view._browser)
        self.assertFalse(view._failed)
        self.assertFalse(view._loading)

    def test_new_window_requires_user_gesture_and_queues_atlas_navigation(self):
        view = self.loaded_view()
        host = view._native
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            host.event(native.NEW_WINDOW, 0, "https://example.test/")
            opened.assert_not_called()
            host.event(native.NEW_WINDOW, 1, "https://example.test/")
            opened.assert_called_once_with(QUrl("https://example.test/"))
            count = len(host.navigations)
            target = atlas_url("Shaded Dunes")
            host.event(native.NEW_WINDOW, 1, target)
            self.assertEqual(len(host.navigations), count)
            self.app.processEvents()
            self.assertEqual(host.navigations[-1], target)

    def test_queued_atlas_new_window_does_nothing_after_hide(self):
        view = self.loaded_view()
        host = view._native
        count = len(host.navigations)
        host.event(native.NEW_WINDOW, 1, atlas_url("Shaded Dunes"))
        view.deactivate()
        self.app.processEvents()
        self.assertEqual(len(host.navigations), count)

    def test_open_full_retains_spa_location_and_removes_only_embed(self):
        view = self.loaded_view()
        current = atlas_url("Shaded Dunes", "Fictional guardian") + "&level=lower&x=120&z=2"
        view._native.event(native.URL_CHANGED, 0, current)
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            view.open_button.click()
        full = opened.call_args.args[0].toString()
        self.assertNotIn("embed=", full)
        self.assertIn("find=Fictional+guardian", full)
        self.assertIn("level=lower&x=120&z=2", full)
        self.assertIn("/shaded-dunes/", full)

    def test_failure_timeout_and_process_failure_offer_saved_map_recovery(self):
        view = self.loaded_view()
        host = view._native
        fallbacks = []
        view.fallback_requested.connect(lambda: fallbacks.append(True))
        view.focus_npc("Fictional guardian")
        host.finish(ok=False)
        self.assertFalse(view.retry_button.isHidden())
        self.assertIs(view._stack.currentWidget(), view._notice)
        view.retry_button.click()
        self.assertEqual(len(host.navigations), 3)
        view._timed_out()
        self.assertIn(("suspend",), host.calls)
        self.assertIn("too long", view._message.text())
        host.event(native.PROCESS_FAILED, 1, "")
        self.assertIn("unexpectedly", view._message.text())
        view.saved_maps_button.click()
        self.assertEqual(fallbacks, [True])

    def test_missing_runtime_offers_only_explicit_microsoft_link_and_saved_maps(self):
        view = self.view()
        with patch.object(native, "NativeHost", side_effect=native.RuntimeUnavailable("Synthetic missing runtime")):
            view.activate("Night Harbor")
        self.assertIsNone(view._native)
        self.assertFalse(view.runtime_button.isHidden())
        self.assertIn("Microsoft WebView2", view._message.text())
        self.assertFalse(view._timeout.isActive())
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            view.runtime_button.click()
        opened.assert_called_once_with(QUrl(atlas_view.WEBVIEW2_DOWNLOAD))

    def test_failed_async_environment_retry_replaces_closed_host(self):
        view = self.view()
        view.activate("Night Harbor")
        old = view._native
        old.event(native.ERROR, -1, "Synthetic environment failure")
        self.assertFalse(view._native_ready)
        view.retry_button.click()
        replacement = view._native
        self.assertIsNot(old, replacement)
        self.assertTrue(old.closed)
        replacement.ready()
        self.assertEqual(replacement.navigations, [atlas_url("Night Harbor")])

    def test_ready_host_process_or_native_error_retry_creates_fresh_controller(self):
        for event in (native.PROCESS_FAILED, native.ERROR):
            with self.subTest(event=event):
                view = self.loaded_view()
                old = view._native
                old.event(event, 0, "Synthetic browser failure")
                self.assertFalse(view._native_ready)
                view.retry_button.click()
                replacement = view._native
                self.assertIsNot(replacement, old)
                self.assertTrue(old.closed)
                replacement.ready()
                self.assertEqual(replacement.navigations, [atlas_url("Night Harbor")])
                replacement.finish()
                self.assertIs(view._stack.currentWidget(), view._browser)

    def test_parent_deletion_closes_host_without_explicit_shutdown(self):
        view = self.loaded_view()
        host = view._native
        shiboken6.delete(view)
        self.assertTrue(host.closed)
        self.assertEqual(host.calls.count(("close",)), 1)
        self.assertIsNone(host.callback)

    def test_disconnected_runtime_during_hide_still_closes_and_can_reopen(self):
        view = self.loaded_view()
        host = view._native
        with patch.object(host, "stop", side_effect=RuntimeError("Runtime already exited")):
            view.deactivate()
        self.assertTrue(host.closed)
        self.assertIsNone(view._native)
        view.activate("Night Harbor")
        self.assertIsNot(view._native, host)
        view._native.ready()
        self.assertEqual(view._native.navigations, [atlas_url("Night Harbor")])

    def test_shutdown_is_idempotent_and_late_native_events_are_inert(self):
        view = self.loaded_view()
        host = view._native
        view.shutdown()
        view.shutdown()
        calls = list(host.calls)
        view.activate("Shaded Dunes")
        view.set_zone("Shaded Dunes")
        view.focus_npc("Fictional guardian")
        for event in (native.READY, native.LOAD_DONE, native.URL_CHANGED, native.PROCESS_FAILED, native.ERROR):
            self.assertEqual(view._event(event, 1, atlas_url("Shaded Dunes")), 0)
        self.assertEqual(host.calls, calls)
        self.assertEqual(host.calls.count(("close",)), 1)
        self.assertFalse(view._native_ready)
        self.assertFalse(view._timeout.isActive())


if __name__ == "__main__":
    unittest.main()
