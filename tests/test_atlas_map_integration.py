"""Atlas provider routing uses a fake widget, never Chromium or network access."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.map_labels import MapLabel
from mnmparse.maps import MapImage


MAP_URL = "https://static.wikitide.net/monstersandmemorieswiki/a/ab/Fictional_map.png"


class FakeAtlasView(QWidget):
    zone_changed = Signal(str)
    fallback_requested = Signal()
    instances = []

    def __init__(self, parent=None):
        super().__init__(parent)
        self.calls = []
        self.active = False
        self.instances.append(self)

    def activate(self, zone, npc=""):
        self.active = True
        self.calls.append(("activate", zone, npc))

    def set_zone(self, zone):
        self.calls.append(("set_zone", zone))

    def focus_npc(self, name):
        self.calls.append(("focus_npc", name))

    def deactivate(self):
        self.active = False
        self.calls.append(("deactivate",))

    def shutdown(self):
        self.active = False
        self.calls.append(("shutdown",))


class AcceptingRenderChild(QWidget):
    """Model Chromium's render child accepting presses before they can bubble."""

    def __init__(self, parent):
        super().__init__(parent)
        self.presses = []

    def mousePressEvent(self, event):
        self.presses.append((event.position(), event.modifiers()))
        event.accept()


class AtlasMapIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "map.ini"), QSettings.Format.IniFormat)
        FakeAtlasView.instances.clear()
        self.fake = patch("mnmparse.app.map_overlay.AtlasView", FakeAtlasView)
        self.fake.start()
        self.load = patch("mnmparse.app.map_overlay._Load.start")
        self.load_start = self.load.start()
        self.windows = []

    def tearDown(self):
        for window in reversed(self.windows):
            if isValid(window):
                window.shutdown()
                window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.fake.stop()
        self.load.stop()
        FakeAtlasView.instances.clear()
        self.settings.sync()
        self.temp.cleanup()

    def window(self, *, embedded=False):
        window = MapOverlay(self.settings, Mock(), embedded=embedded)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        window.resize(700, 500)
        self.windows.append(window)
        return window

    def show(self, window):
        window.show()
        self.app.processEvents()

    def display_image(self, window, zone="Sungreet Strand"):
        window.set_zone(zone)
        window.load(entry=MapImage("Fictional map", MAP_URL, "https://example.test/wiki/Fictional"))
        image = QImage(1000, 800, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkGray)
        entry = MapImage("Fictional map", MAP_URL, "https://example.test/wiki/Fictional")
        window._ready(window._token, ([entry], image, True, MAP_URL), "")
        return entry

    def render_child(self, window):
        """Add descendants after Atlas creation to exercise ChildPolished wiring."""
        container = QWidget(window._atlas)
        container.setGeometry(window._atlas.rect())
        container.show()
        self.app.processEvents()
        render = AcceptingRenderChild(container)
        render.setGeometry(container.rect())
        render.show()
        self.app.processEvents()
        return render

    def mouse(self, child, kind, global_point, *, button=Qt.MouseButton.LeftButton,
              buttons=Qt.MouseButton.LeftButton, modifiers=Qt.KeyboardModifier.NoModifier):
        event = QMouseEvent(kind, QPointF(child.mapFromGlobal(global_point)), QPointF(global_point),
                            button, buttons, modifiers)
        self.app.sendEvent(child, event)
        return event

    def test_default_saved_maps_never_constructs_atlas_even_when_visible(self):
        window = self.window()
        self.assertEqual(window.source, "images")
        self.assertIsNone(window._atlas)
        self.display_image(window)
        self.show(window)
        self.assertEqual(FakeAtlasView.instances, [])
        self.assertIs(window._map_stack.currentWidget(), window.view)
        self.assertIsNotNone(window.source_button.menu())

    def test_hidden_persisted_atlas_waits_until_visible_without_image_load(self):
        self.settings.setValue("map/source", "atlas")
        self.settings.setValue("map/zone", "Night Harbor")
        window = self.window()
        self.assertEqual(window.source, "atlas")
        self.assertIsNone(window._atlas)
        window.set_zone("Sungreet Strand")
        self.assertEqual(FakeAtlasView.instances, [])
        self.load_start.assert_not_called()
        self.show(window)
        self.assertEqual(len(FakeAtlasView.instances), 1)
        self.assertTrue(window._atlas.active)
        self.assertIn(("activate", "Sungreet Strand", ""), window._atlas.calls)
        self.load_start.assert_not_called()

    def test_hidden_source_selection_is_lazy_and_preferences_are_independent(self):
        overlay = self.window()
        browser = self.window(embedded=True)
        overlay.set_source("atlas")
        self.assertEqual(overlay.source, "atlas")
        self.assertEqual(browser.source, "images")
        self.assertEqual(self.settings.value("map/source"), "atlas")
        self.assertEqual(self.settings.value("zones/source", "images"), "images")
        self.assertEqual(FakeAtlasView.instances, [])
        browser.set_source("atlas")
        overlay.set_source("images")
        self.assertEqual(self.settings.value("map/source"), "images")
        self.assertEqual(self.settings.value("zones/source"), "atlas")
        self.assertEqual(FakeAtlasView.instances, [])

    def test_provider_switch_preserves_saved_image_zoom_and_native_labels(self):
        window = self.window()
        self.display_image(window)
        label = MapLabel("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", MAP_URL, .25, .5,
                         "Fictional meeting place", "Bring a torch.", "camp")
        window.label_store.save(label)
        self.show(window)
        window.view.zoom(2)
        scale = window.view.transform().m11()
        marker = window.view._label_items[0]
        self.load_start.reset_mock()
        window.set_source("atlas")
        self.app.processEvents()
        self.assertIs(window._map_stack.currentWidget(), window._atlas)
        self.assertFalse(window.view.isVisible())
        self.assertTrue(isValid(marker))
        self.assertEqual(marker.label, label)
        self.assertEqual(window._selected_url, MAP_URL)
        window.set_source("images")
        self.app.processEvents()
        self.assertIs(window._map_stack.currentWidget(), window.view)
        self.assertEqual(window.view._label_items[0].label, label)
        self.assertAlmostEqual(window.view.transform().m11(), scale)
        self.assertFalse(window._atlas.active)
        self.load_start.assert_not_called()

    def test_live_zone_updates_atlas_and_npc_context_without_image_downloads(self):
        window = self.window()
        window.set_source("atlas")
        window.set_zone("Sungreet Strand")
        self.show(window)
        requested = []
        window.npc_zone_requested.connect(requested.append)
        self.load_start.reset_mock()
        window.on_message(None, SimpleNamespace(kind="zone", target="Night Harbor (West)"))
        self.assertEqual(window.current_zone, "Night Harbor")
        self.assertTrue(any(call[0] in {"set_zone", "activate"} and call[1] == "Night Harbor"
                            for call in window._atlas.calls))
        self.assertIn("Night Harbor", requested)
        self.load_start.assert_not_called()

    def test_internal_atlas_navigation_updates_only_its_own_browser_selection(self):
        overlay = self.window()
        browser = self.window(embedded=True)
        overlay.set_zone("Sungreet Strand")
        browser.set_zone("Night Harbor")
        browser.set_source("atlas")
        self.show(browser)
        requested = []
        browser.npc_zone_requested.connect(requested.append)
        self.load_start.reset_mock()
        browser._atlas.zone_changed.emit("Shaded Dunes")
        self.assertEqual(browser.current_zone, "Shaded Dunes")
        self.assertEqual(browser.zone.currentText(), "Shaded Dunes")
        self.assertEqual(self.settings.value("zones/zone"), "Shaded Dunes")
        self.assertEqual(overlay.current_zone, "Sungreet Strand")
        self.assertIn("Shaded Dunes", requested)
        browser.on_message(None, SimpleNamespace(kind="zone", target="Fallen Pass"))
        self.assertEqual(browser.current_zone, "Shaded Dunes")
        self.load_start.assert_not_called()

    def test_world_transition_clears_zone_controls_and_reopens_without_lookup(self):
        window = self.window()
        self.display_image(window, zone="Night Harbor")
        self.show(window)
        window.set_source("atlas")
        atlas = window._atlas
        requested = []
        window.npc_zone_requested.connect(requested.append)
        self.load_start.reset_mock()
        atlas.zone_changed.emit("World")
        self.assertEqual(window.current_zone, "World")
        self.assertEqual(window.zone.currentText(), "World")
        self.assertEqual(self.settings.value("map/zone"), "World")
        self.assertFalse(window.notable_npcs.isEnabled())
        self.assertFalse(window.wiki_link.isEnabled())
        self.assertEqual(window._selected_url, "")
        self.assertTrue(window.view._image_rect.isEmpty())
        self.assertEqual(requested, [])
        window.hide()
        self.show(window)
        self.assertEqual(window.current_zone, "World")
        self.assertEqual(atlas.calls[-1], ("activate", "World", ""))
        self.assertEqual(requested, [])
        window.set_source("images")
        self.assertIs(window._map_stack.currentWidget(), window.view)
        self.assertIn("Select a zone", window.status.text())
        self.assertFalse(window.notable_npcs.isEnabled())
        self.assertFalse(window.wiki_link.isEnabled())
        window.set_zone("World")
        window.load(refresh=True)
        window._open_notable_npcs()
        self.assertEqual(requested, [])
        self.load_start.assert_not_called()

    def test_explicit_npc_selection_focuses_literal_name_only_in_current_zone(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        selected = []
        window.npc_selected.connect(selected.append)
        window._select_npc("Night Harbor", "Bill & Ben")
        self.assertEqual(selected, ["Bill & Ben"])
        self.assertIn(("focus_npc", "Bill & Ben"), window._atlas.calls)
        before = list(window._atlas.calls)
        window._select_npc("Sungreet Strand", "Fictional guard")
        self.assertEqual(selected, ["Bill & Ben"])
        self.assertEqual(window._atlas.calls, before)

    def test_saved_map_fallback_restores_cached_scene_without_fetch(self):
        window = self.window()
        self.display_image(window)
        self.show(window)
        window.set_source("atlas")
        self.load_start.reset_mock()
        window._atlas.fallback_requested.emit()
        self.assertEqual(window.source, "images")
        self.assertEqual(self.settings.value("map/source"), "images")
        self.assertIs(window._map_stack.currentWidget(), window.view)
        self.assertEqual(window._selected_url, MAP_URL)
        self.assertFalse(window._atlas.active)
        self.load_start.assert_not_called()

    def test_unsupported_zone_does_not_silently_request_a_native_map(self):
        window = self.window()
        window.set_source("atlas")
        window.set_zone("Fictional unmapped zone")
        self.show(window)
        self.assertEqual(window.current_zone, "Fictional unmapped zone")
        self.assertIn(("activate", "Fictional unmapped zone", ""), window._atlas.calls)
        self.assertEqual(window.source, "atlas")
        self.load_start.assert_not_called()
        window._atlas.fallback_requested.emit()
        self.assertEqual(window.source, "images")
        self.assertEqual(window.current_zone, "Fictional unmapped zone")
        self.load_start.assert_called_once()

    def test_late_image_completion_cannot_replace_active_atlas(self):
        window = self.window()
        window.set_zone("Night Harbor")
        self.show(window)
        old_token = window._token
        window.set_source("atlas")
        entry = MapImage("Late map", MAP_URL, "https://example.test/wiki/Fictional")
        image = QImage(1000, 800, QImage.Format.Format_RGB32)
        window._ready(old_token, ([entry], image, True, MAP_URL), "")
        self.assertEqual(window.source, "atlas")
        self.assertIs(window._map_stack.currentWidget(), window._atlas)
        self.assertTrue(window._atlas.active)
        self.assertTrue(window.view._image_rect.isEmpty())

    def test_hidden_live_zone_change_is_applied_on_reopen_without_background_activation(self):
        window = self.window()
        window.set_source("atlas")
        window.set_zone("Night Harbor")
        self.show(window)
        atlas = window._atlas
        window.hide()
        self.app.processEvents()
        before = list(atlas.calls)
        self.load_start.reset_mock()
        window.on_message(None, SimpleNamespace(kind="zone", target="Sungreet Strand"))
        self.assertEqual(window.current_zone, "Sungreet Strand")
        self.assertFalse(atlas.active)
        self.assertEqual(atlas.calls, before)
        self.load_start.assert_not_called()
        self.show(window)
        self.assertIs(window._atlas, atlas)
        self.assertTrue(atlas.active)
        self.assertEqual(atlas.calls[-1], ("activate", "Sungreet Strand", ""))
        self.load_start.assert_not_called()

    def test_inactive_atlas_navigation_cannot_retarget_saved_map_source(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        atlas = window._atlas
        window.set_source("images")
        window.set_zone("Sungreet Strand")
        token = window._token
        requested = []
        window.npc_zone_requested.connect(requested.append)
        atlas.zone_changed.emit("Fallen Pass")
        self.assertEqual(window.source, "images")
        self.assertEqual(window.current_zone, "Sungreet Strand")
        self.assertEqual(self.settings.value("map/zone"), "Sungreet Strand")
        self.assertEqual(window._token, token)
        self.assertEqual(requested, [])

    def test_atlas_header_is_permanent_and_saved_map_variants_are_hidden(self):
        window = self.window()
        self.display_image(window)
        self.show(window)
        window.set_source("atlas")
        self.app.processEvents()
        with patch("mnmparse.app.map_overlay.QCursor.pos", return_value=QPoint(-2000, -2000)):
            window._hide_header()
        self.assertTrue(window.header.isVisible())
        self.assertFalse(window.variants.isVisible())
        self.assertIs(window.header.parentWidget(), window)
        self.assertGreaterEqual(window._map_stack.geometry().top(), window.header.geometry().bottom())

    def test_late_accepting_render_child_still_routes_inner_edge_resize(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        render = self.render_child(window)
        point = window.mapToGlobal(QPoint(3, 100))
        self.assertTrue(render.rect().contains(render.mapFromGlobal(point)))
        handle = Mock()
        handle.startSystemResize.return_value = True
        with patch.object(window, "windowHandle", return_value=handle):
            self.mouse(render, QEvent.Type.MouseButtonPress, point)
        handle.startSystemResize.assert_called_once_with(Qt.Edge.LeftEdge)
        self.assertEqual(render.presses, [])

    def test_alt_drag_crosses_render_child_while_locked_click_remains_ordinary(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        render = self.render_child(window)
        origin = window.pos()
        start = window.mapToGlobal(QPoint(120, 150))
        with patch.object(render, "grabMouse"):
            self.mouse(render, QEvent.Type.MouseButtonPress, start,
                       modifiers=Qt.KeyboardModifier.AltModifier)
            self.assertIs(window._drag_widget, render)
            self.assertEqual(render.presses, [])
            self.mouse(render, QEvent.Type.MouseMove, start + QPoint(60, 35),
                       button=Qt.MouseButton.NoButton, modifiers=Qt.KeyboardModifier.AltModifier)
            self.assertEqual(window.pos(), origin + QPoint(60, 35))
            self.mouse(render, QEvent.Type.MouseButtonRelease, start + QPoint(60, 35),
                       buttons=Qt.MouseButton.NoButton, modifiers=Qt.KeyboardModifier.AltModifier)
        self.assertIsNone(window._drag_offset)
        window.set_locked(True)
        handle = Mock()
        with patch.object(window, "windowHandle", return_value=handle):
            self.mouse(render, QEvent.Type.MouseButtonPress, window.mapToGlobal(QPoint(3, 100)),
                       modifiers=Qt.KeyboardModifier.AltModifier)
        handle.startSystemResize.assert_not_called()
        self.assertEqual(len(render.presses), 1)
        self.assertIsNone(window._drag_offset)

    def test_embedded_atlas_render_child_keeps_edge_and_alt_clicks(self):
        window = self.window(embedded=True)
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        render = self.render_child(window)
        point = window.mapToGlobal(QPoint(3, 100))
        self.assertTrue(render.rect().contains(render.mapFromGlobal(point)))
        handle = Mock()
        with patch.object(window, "windowHandle", return_value=handle):
            self.mouse(render, QEvent.Type.MouseButtonPress, point,
                       modifiers=Qt.KeyboardModifier.AltModifier)
        handle.startSystemResize.assert_not_called()
        self.assertEqual(len(render.presses), 1)
        self.assertIsNone(window._drag_offset)

    def test_native_selector_popup_children_are_not_filtered_as_atlas_renderers(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        # A selector's native popup can extend outside the overlay. Its clicks
        # must not become edge-resize gestures from the Atlas descendant hook.
        popup = QWidget(window.zone, Qt.WindowType.Popup)
        popup.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        popup.resize(60, 80)
        popup.move(window.mapToGlobal(QPoint(window.width() + 10, 100)))
        render = AcceptingRenderChild(popup)
        render.setGeometry(popup.rect())
        try:
            with patch.object(window, "_watch_atlas_input", wraps=window._watch_atlas_input) as watch:
                popup.show()
                render.show()
                self.app.processEvents()
            watch.assert_not_called()
            point = render.mapToGlobal(render.rect().center())
            self.assertGreaterEqual(window.mapFromGlobal(point).x(), window.width())
            handle = Mock()
            with patch.object(window, "windowHandle", return_value=handle):
                self.mouse(render, QEvent.Type.MouseButtonPress, point)
            handle.startSystemResize.assert_not_called()
            self.assertEqual(len(render.presses), 1)
        finally:
            popup.hide()
            popup.deleteLater()

    def test_hide_deactivates_and_shutdown_is_idempotent(self):
        window = self.window()
        window.set_zone("Night Harbor")
        window.set_source("atlas")
        self.show(window)
        atlas = window._atlas
        window.hide()
        self.app.processEvents()
        self.assertFalse(atlas.active)
        self.assertIn(("deactivate",), atlas.calls)
        self.show(window)
        self.assertTrue(atlas.active)
        window.shutdown()
        window.shutdown()
        self.assertFalse(atlas.active)
        self.assertEqual(atlas.calls.count(("shutdown",)), 1)
        count = len(atlas.calls)
        atlas.zone_changed.emit("Fallen Pass")
        atlas.fallback_requested.emit()
        self.assertEqual(len(atlas.calls), count)
        self.assertEqual(window.current_zone, "Night Harbor")


if __name__ == "__main__":
    unittest.main()
