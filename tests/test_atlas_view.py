"""Atlas lifecycle/navigation tests use Qt widgets with an entirely offline fake engine."""
from __future__ import annotations

from enum import Enum
import ast
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QApplication, QWidget
import shiboken6

from mnmparse.app import atlas_view
from mnmparse.atlas import atlas_url


class FakeProfile(QObject):
    downloadRequested = Signal(object)
    HttpCacheType = types.SimpleNamespace(DiskHttpCache=1)
    PersistentCookiesPolicy = types.SimpleNamespace(AllowPersistentCookies=1)
    PersistentPermissionsPolicy = types.SimpleNamespace(StoreInMemory=1)

    def __init__(self, name, parent):
        super().__init__(parent)
        self.values = {"name": name}

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda value: self.values.__setitem__(name[3:], value)
        raise AttributeError(name)


class FakeSettings:
    WebAttribute = types.SimpleNamespace(**{name: name for name in (
        "JavascriptCanAccessClipboard", "JavascriptCanPaste", "LocalContentCanAccessFileUrls",
        "LocalContentCanAccessRemoteUrls", "AllowWindowActivationFromJavaScript", "NavigateOnDropEnabled",
        "PlaybackRequiresUserGesture")})

    def __init__(self):
        self.attributes = {}

    def setAttribute(self, attr, value):
        self.attributes[attr] = value


class FakePage(QObject):
    permissionRequested = Signal(object)
    newWindowRequested = Signal(object)
    loadStarted = Signal()
    loadFinished = Signal(bool)
    urlChanged = Signal(QUrl)
    renderProcessTerminated = Signal(object, int)
    class LifecycleState(Enum):
        Active = 1
        Frozen = 2
    NavigationType = types.SimpleNamespace(NavigationTypeLinkClicked=1, NavigationTypeOther=2)

    def __init__(self, profile, parent):
        super().__init__(parent)
        self.profile = profile
        self.calls = []
        self._url = QUrl("about:blank")
        self._settings = FakeSettings()

    def settings(self):
        return self._settings

    def setLifecycleState(self, state):
        self.state = state

    def setVisible(self, visible):
        self.visible = visible

    def setUrl(self, url):
        self.calls.append(QUrl(url))
        self._url = QUrl(url)
        self.loadStarted.emit()

    def url(self):
        return self._url

    def finish(self, ok=True):
        self.urlChanged.emit(self._url)
        self.loadFinished.emit(ok)


class FakeBrowser(QWidget):
    def setPage(self, page):
        self.page = page
        self.stopped = False

    def stop(self):
        self.stopped = True


class AtlasViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "atlas"
        self.views = []
        self.modules = patch.dict(sys.modules, {
            "PySide6.QtWebEngineCore": types.SimpleNamespace(QWebEnginePage=FakePage,
                QWebEngineProfile=FakeProfile, QWebEngineSettings=FakeSettings),
            "PySide6.QtWebEngineWidgets": types.SimpleNamespace(QWebEngineView=FakeBrowser),
        })
        self.modules.start()

    def tearDown(self):
        for view in self.views:
            if shiboken6.isValid(view):
                view.shutdown()
                shiboken6.delete(view)
        if atlas_view._profiles is not None:
            atlas_view._profiles.shutdown()
            shiboken6.delete(atlas_view._profiles)
            atlas_view._profiles = None
        self.modules.stop()
        self.temp.cleanup()

    def view(self, **kwargs):
        view = atlas_view.AtlasView(storage_path=self.path, **kwargs)
        self.views.append(view)
        return view

    def test_import_and_construction_do_not_import_webengine_or_create_storage(self):
        code = """
import sys,tempfile
from pathlib import Path
from PySide6.QtWidgets import QApplication
from mnmparse.app.atlas_view import AtlasView
app=QApplication([])
with tempfile.TemporaryDirectory() as directory:
    path=Path(directory)/'absent'
    view=AtlasView(storage_path=path)
    view.set_zone('Night Harbor')
    view.focus_npc('Fictional guardian')
    assert not path.exists()
    assert not any(name.startswith('PySide6.QtWebEngine') for name in sys.modules)
    view.shutdown()
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_activation_loads_documented_url_and_native_credit(self):
        view = self.view()
        view.activate("Night Harbor", "Fictional guardian")
        self.assertEqual(view._page.calls[0].toString(), atlas_url("Night Harbor", "Fictional guardian"))
        self.assertEqual(view._stack.currentWidget(), view._notice)
        view._page.finish()
        self.assertEqual(view._stack.currentWidget(), view._browser)
        self.assertTrue(view.open_button.isEnabled())
        self.assertTrue(any(label.text() == "Maps by MnM Atlas" for label in view.findChildren(atlas_view.QLabel)))

    def test_same_map_and_internal_zone_echo_preserve_page_view(self):
        view = self.view()
        view.activate("Night Harbor")
        view._page.finish()
        view.set_zone("Night Harbor")
        self.assertEqual(len(view._page.calls), 1)
        changes = []
        view.zone_changed.connect(changes.append)
        view._page._url = QUrl("https://www.mnmatlas.com/shaded-dunes/?embed=1&floor=2&place=camp")
        view._page.urlChanged.emit(view._page.url())
        view.set_zone("Shaded Dunes")
        self.assertEqual(changes, ["Shaded Dunes"])
        self.assertEqual(len(view._page.calls), 1)
        self.assertIn("floor=2", view._page.url().toString())

    def test_world_view_does_not_reload_on_reactivation(self):
        view = self.view()
        view.activate("")
        view._page.finish()
        view.deactivate()
        self.assertEqual(view._page.state, FakePage.LifecycleState.Frozen)
        self.assertFalse(view._page.visible)
        view.activate("")
        self.assertEqual(len(view._page.calls), 1)
        self.assertEqual(view._page.state, FakePage.LifecycleState.Active)

    def test_explicit_world_navigation_survives_host_echo_and_reactivation(self):
        view = self.view()
        view.activate("Night Harbor")
        view._page.finish()
        changed = []
        view.zone_changed.connect(changed.append)
        view._page._url = QUrl(atlas_url("World"))
        view._page.urlChanged.emit(view._page.url())
        self.assertEqual(changed, ["World"])
        view.set_zone("World")
        view.deactivate()
        view.activate("World")
        self.assertEqual(len(view._page.calls), 1)

    def test_unknown_game_zone_initial_world_fallback_keeps_host_context(self):
        view = self.view()
        changed = []
        view.zone_changed.connect(changed.append)
        view.activate("Uncharted fictional zone")
        view._page.finish()
        self.assertEqual(changed, [])
        self.assertEqual(view._zone, "Uncharted fictional zone")

    def test_programmatic_unknown_zone_after_known_map_keeps_host_context(self):
        view = self.view()
        view.activate("Night Harbor")
        view._page.finish()
        changed = []
        view.zone_changed.connect(changed.append)
        view.set_zone("Uncharted fictional zone")
        view._page.finish()
        self.assertEqual(changed, [])
        self.assertEqual(view._zone, "Uncharted fictional zone")
        view.deactivate()
        view.activate("Uncharted fictional zone")
        self.assertEqual(len(view._page.calls), 2)

    def test_shutdown_remains_safe_after_application_quit_disposes_page(self):
        view = self.view()
        view.activate("Night Harbor")
        atlas_view._profiles.shutdown()
        view.shutdown()
        self.assertIsNone(view._page)

    def test_pending_changes_offline_then_focus_npc_only_after_activation(self):
        view = self.view()
        view.set_zone("Night Harbor")
        self.assertIsNone(view._page)
        view.activate("Night Harbor")
        view._page.finish()
        view.focus_npc("Fictional guardian")
        self.assertIn("find=Fictional+guardian", view._page.calls[-1].toString())
        view.deactivate()
        count = len(view._page.calls)
        view.set_zone("Shaded Dunes")
        view._page.loadFinished.emit(False)
        self.assertEqual(len(view._page.calls), count)
        self.assertFalse(view._timeout.isActive())

    def test_queued_hidden_navigation_does_not_override_pending_zone_or_notify_host(self):
        view = self.view()
        view.activate("Night Harbor")
        view._page.finish()
        changed = []
        view.zone_changed.connect(changed.append)
        view.deactivate()
        view.set_zone("Shaded Dunes")
        view._page._url = QUrl(atlas_url("Fallen Pass"))
        view._page.urlChanged.emit(view._page.url())
        self.assertEqual(changed, [])
        self.assertEqual(view._zone, "Shaded Dunes")
        view.activate("Shaded Dunes")
        self.assertEqual(view._page.calls[-1].toString(), atlas_url("Shaded Dunes"))

    def test_shared_profile_path_bounded_cache_and_pages_before_profile(self):
        first, second = self.view(), self.view()
        first.activate("Night Harbor")
        second.activate("Shaded Dunes")
        profile = first._page.profile
        self.assertIs(profile, second._page.profile)
        self.assertEqual(profile.values["HttpCacheMaximumSize"], 64 * 1024 * 1024)
        self.assertEqual(profile.values["PersistentStoragePath"], str(self.path / "storage"))
        first.shutdown()
        self.app.processEvents()
        self.assertTrue(shiboken6.isValid(profile))
        second.shutdown()
        self.app.processEvents()
        self.assertFalse(shiboken6.isValid(profile))

    def test_profile_survives_immediate_view_replacement_and_parent_deletion(self):
        first = self.view()
        first.activate("Night Harbor")
        profile = first._page.profile
        first.shutdown()
        second = self.view()
        second.activate("Night Harbor")
        self.assertIs(profile, second._page.profile)
        self.app.processEvents()
        self.assertTrue(shiboken6.isValid(profile))
        shiboken6.delete(second)
        self.app.processEvents()
        self.assertFalse(shiboken6.isValid(profile))

    def test_navigation_rejects_foreign_automatic_and_unsafe_links(self):
        view = self.view()
        view.activate("Night Harbor")
        page = view._page
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            for url in ("https://www.mnmatlas.com.evil.test/", "http://www.mnmatlas.com/", "file:///private.txt",
                        "https://user:password@www.mnmatlas.com/", "javascript:alert(1)"):
                self.assertFalse(page.acceptNavigationRequest(QUrl(url), 2, True))
            self.assertFalse(page.acceptNavigationRequest(QUrl("https://example.test/"), 2, True))
            opened.assert_not_called()
            self.assertFalse(page.acceptNavigationRequest(QUrl("https://example.test/"), 1, True))
            opened.assert_called_once_with(QUrl("https://example.test/"))
            self.assertTrue(page.acceptNavigationRequest(QUrl(atlas_url("Shaded Dunes")), 2, True))
            self.assertEqual(page.chooseFiles(None, [], []), [])

    def test_target_blank_requires_user_action_and_open_full_removes_embed_only(self):
        view = self.view()
        view.activate("Night Harbor", "Fictional guardian")
        view._page.finish()
        with patch.object(atlas_view.QDesktopServices, "openUrl") as opened:
            request = types.SimpleNamespace(isUserInitiated=lambda: False,
                requestedUrl=lambda: QUrl("https://example.test/"))
            view._new_window(request)
            opened.assert_not_called()
            request.isUserInitiated = lambda: True
            view._new_window(request)
            self.assertEqual(opened.call_count, 1)
            view.open_button.click()
            full = opened.call_args.args[0].toString()
            self.assertNotIn("embed=", full)
            self.assertIn("find=Fictional+guardian", full)

    def test_external_clicked_link_keeps_existing_map_after_rejected_load(self):
        view = self.view()
        view.activate("Night Harbor")
        view._page.finish()
        view._page.loadStarted.emit()
        with patch.object(atlas_view.QDesktopServices, "openUrl"):
            view._page.acceptNavigationRequest(QUrl("https://example.test/"), 1, True)
        view._page.loadFinished.emit(False)
        self.assertEqual(view._stack.currentWidget(), view._browser)
        self.assertFalse(view._failed)

    def test_fail_timeout_renderer_and_import_failure_offer_native_recovery(self):
        view = self.view()
        fallbacks = []
        view.fallback_requested.connect(lambda: fallbacks.append(True))
        view.activate("Night Harbor")
        view._page.finish(False)
        self.assertFalse(view.retry_button.isHidden())
        self.assertEqual(view._stack.currentWidget(), view._notice)
        view.retry_button.click()
        self.assertEqual(len(view._page.calls), 2)
        view._timed_out()
        self.assertTrue(view._browser.stopped)
        self.assertIn("too long", view._message.text())
        view._page.renderProcessTerminated.emit(None, -1)
        self.assertIn("unexpectedly", view._message.text())
        view.saved_maps_button.click()
        self.assertEqual(fallbacks, [True])
        broken = self.view()
        with patch.object(broken, "_create_engine", side_effect=ImportError("missing DLL")):
            broken.activate("Night Harbor")
        self.assertEqual(broken._stack.currentWidget(), broken._notice)
        self.assertFalse(broken.retry_button.isHidden())

    def test_shutdown_is_idempotent_and_late_events_are_inert(self):
        view = self.view()
        view.activate("Night Harbor")
        view.shutdown()
        view.shutdown()
        view.activate("Shaded Dunes")
        view.set_zone("Shaded Dunes")
        view.focus_npc("Fictional guardian")
        view._load_finished(False)
        view._renderer_terminated(None, 1)
        self.assertIsNone(view._browser)
        self.assertFalse(view._timeout.isActive())


class AtlasNoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "scripts" / "generate_legal_notices.py"
        spec = importlib.util.spec_from_file_location("atlas_notice_generator", path)
        cls.notices = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.notices)

    def test_full_license_blocks_preserve_text_without_documentation_markup(self):
        parser = self.notices._LicenseBlocks()
        parser.feed('<html>Unrelated prose<pre class="cpp">Copyright Fictional\n'
                    'Permission &amp;quot;example&amp;quot; &lt;holder&gt;\n</pre>'
                    '<pre>Second license\n</pre>Footer</html>')
        self.assertEqual(parser.blocks, ['Copyright Fictional\nPermission "example" <holder>\n',
                                         'Second license\n'])

    def test_committed_notice_manifest_and_chromium_component_coverage(self):
        self.notices.verify_upstream()
        data = (self.notices.DEST / "Qt-6.11.2-Chromium.txt").read_text(encoding="utf-8")
        self.assertGreater(data.count("Source SHA-256:"), 120)
        for component in ("boringssl", "webkit", "icu", "sqlite", "ffmpeg", "zlib"):
            self.assertIn(component, data.lower())
        self.assertIn("5170777d28bee1ce92cc693a0dbf2ad01492e5cf", data)

    def test_refresh_rejects_changed_documentation_version_without_more_requests(self):
        with patch.object(self.notices, "download", return_value=b"Qt 6.11.3") as fetched:
            with self.assertRaisesRegex(RuntimeError, "version changed"):
                self.notices.chromium_notices()
        fetched.assert_called_once()


class AtlasPackagingTests(unittest.TestCase):
    def test_qml_payload_is_removed_but_required_native_quick_dependencies_survive(self):
        path = Path(__file__).resolve().parents[1] / "mnmparser.spec"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wanted = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name in {"_keep", "_prune_qt_dlls"}]
        context = {"Path": Path, "_DROP": ()}
        exec(compile(ast.Module(body=wanted, type_ignores=[]), str(path), "exec"), context)
        keep = context["_keep"]
        for name in ("PySide6/qml/QtQuick3D/qquick3dplugin.dll", "PySide6/qml/QtQuick/Controls/qmldir",
                     "PySide6/plugins/qmltooling/qmldbg_debugger.dll"):
            self.assertFalse(keep((name, "fixture", "BINARY")))
        entries = [(f"PySide6/{name}", name, "BINARY") for name in (
            "QtWebEngineWidgets.pyd", "plugins/platforms/qwindows.dll", "Qt6Widgets.dll", "Qt6QuickWidgets.dll",
            "Qt6Quick.dll", "Qt6Qml.dll", "Qt6Core.dll", "Qt6Gui.dll", "Qt6Quick3D.dll", "Qt6Graphs.dll")]
        dependencies = {"QtWebEngineWidgets.pyd": ("Qt6Widgets.dll", "Qt6QuickWidgets.dll"),
                        "Qt6QuickWidgets.dll": ("Qt6Quick.dll",), "Qt6Quick.dll": ("Qt6Qml.dll", "Qt6Core.dll"),
                        "plugins/platforms/qwindows.dll": ("Qt6Gui.dll",), "Qt6Gui.dll": ("Qt6Core.dll",)}
        context["get_imports"] = lambda path, **_kwargs: [(name, None) for name in dependencies.get(path, ())]
        actual = [entry[1] for entry in context["_prune_qt_dlls"](entries)]
        self.assertIn("Qt6QuickWidgets.dll", actual)
        self.assertIn("Qt6Qml.dll", actual)
        self.assertIn("Qt6Gui.dll", actual)
        self.assertNotIn("Qt6Quick3D.dll", actual)
        self.assertNotIn("Qt6Graphs.dll", actual)


if __name__ == "__main__":
    unittest.main()
