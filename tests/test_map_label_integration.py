"""Persistent map labels integrate with real scenes without wiki requests."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QImage
from PySide6.QtWidgets import QApplication, QDialog
from shiboken6 import isValid

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.map_labels import MapLabel, MapLabelStore
from mnmparse.maps import MapImage


MAP_A = "https://static.wikitide.net/monstersandmemorieswiki/a/ab/Test_map.png"
MAP_B = "https://static.wikitide.net/monstersandmemorieswiki/b/bc/Test_map_lower.png"


class MapLabelIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.temp.name) / "map.ini")
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        self.worker = patch("mnmparse.app.map_overlay._Load.start")
        self.worker.start()
        self.windows = []
        self.window = self.make_window()

    def tearDown(self):
        for window in reversed(self.windows):
            if isValid(window):
                window.shutdown()
                window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.worker.stop()
        self.settings.sync()
        self.temp.cleanup()

    def make_window(self, *, embedded=False, settings=None):
        window = MapOverlay(settings or self.settings, Mock(), embedded=embedded)
        window.resize(640, 480)
        self.windows.append(window)
        return window

    def display(self, window=None, *, url=MAP_A, zone="Sungreet Strand", width=1000, height=800):
        window = window or self.window
        entry = MapImage("Synthetic map", url, "https://monstersandmemories.miraheze.org/wiki/Test")
        window.set_zone(zone)
        window.load(entry=entry)
        image = QImage(width, height, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkGray)
        window._ready(window._token, ([entry], image, True, url), "")
        return entry

    def label(self, *, url=MAP_A, id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", x=.25, y=.5, name="Safe route"):
        return MapLabel(id, url, x, y, name, "Turn at the bridge.", "pin")

    @staticmethod
    def item_labels(window):
        return [item.label for item in window.view._label_items]

    def accepted_dialog(self, *, name="Meeting place", notes="Bring a torch.", icon="pin"):
        dialog = Mock()
        dialog.exec.return_value = QDialog.DialogCode.Accepted
        dialog.value.return_value = {"name": name, "notes": notes, "icon": icon}
        return dialog

    def context_event(self, window, viewport_point):
        return QContextMenuEvent(QContextMenuEvent.Reason.Mouse, viewport_point,
                                 window.view.viewport().mapToGlobal(viewport_point))

    def test_edit_persists_and_reopened_window_restores_label(self):
        self.display()
        dialog = self.accepted_dialog()
        with patch("mnmparse.app.map_overlay.MapLabelDialog", return_value=dialog):
            self.window._edit_label(MAP_A, .23, .71)
        saved, = self.window.label_store.labels(MAP_A)
        self.assertEqual((saved.name, saved.notes, saved.x, saved.y),
                         ("Meeting place", "Bring a torch.", .23, .71))
        self.assertEqual(self.item_labels(self.window), [saved])
        self.window.shutdown()
        new_settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        independent_store = MapLabelStore(new_settings)
        with patch("mnmparse.app.map_overlay.MapLabelStore.for_settings", return_value=independent_store):
            reopened = self.make_window(settings=new_settings)
        self.display(reopened)
        restored, = self.item_labels(reopened)
        self.assertEqual(restored, saved)
        self.assertAlmostEqual(reopened.view._label_items[0].pos().x(), 230)
        self.assertAlmostEqual(reopened.view._label_items[0].pos().y(), 568)

    def test_overlay_and_embedded_browser_share_same_image_across_zone_names(self):
        browser = self.make_window(embedded=True)
        self.display()
        self.display(browser, zone="Night Harbor", url=MAP_A + "?download=1#map")
        label = self.label()
        self.window.label_store.save(label)
        self.assertIs(self.window.label_store, browser.label_store)
        self.assertEqual(self.item_labels(self.window), [label])
        self.assertEqual(self.item_labels(browser), [label])
        edited = replace(label, name="New rendezvous", notes="Wait here.")
        browser.label_store.save(edited)
        self.assertEqual(self.item_labels(self.window), [edited])
        self.assertEqual(self.item_labels(browser), [edited])
        browser._remove_label(edited)
        self.assertEqual(self.item_labels(self.window), [])
        self.assertEqual(self.item_labels(browser), [])

    def test_alternative_map_labels_are_isolated_and_reload_restores_anchor(self):
        first = self.label()
        second = self.label(url=MAP_B, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", x=.8, y=.1, name="Lower entrance")
        self.window.label_store.save(first)
        self.window.label_store.save(second)
        self.display()
        self.assertEqual(self.item_labels(self.window), [first])
        original_item = self.window.view._label_items[0]
        self.window.label_store.save(replace(second, notes="Changed on another floor."))
        self.assertIs(self.window.view._label_items[0], original_item)
        self.display(url=MAP_B)
        self.assertEqual(self.item_labels(self.window), [replace(second, notes="Changed on another floor.")])
        self.display(width=2000, height=1600)
        self.assertEqual(self.item_labels(self.window), [first])
        self.assertEqual(self.window.view._label_items[0].pos(), QPointF(500, 800))
        self.assertEqual(self.window.view.sceneRect(), self.window.view._image_rect)

    def test_zoom_pan_context_uses_scene_anchor_and_keeps_marker_hit_test(self):
        self.display(width=1600, height=1200)
        label = self.label(x=.5, y=.5)
        self.window.label_store.save(label)
        self.window.show()
        self.app.processEvents()
        view = self.window.view
        view.zoom(3)
        view.centerOn(800, 600)
        self.app.processEvents()
        point = view.mapFromScene(QPointF(800, 600))
        requests = []
        view.label_context_requested.connect(lambda scene, global_, value: requests.append((scene, global_, value)))
        with patch("mnmparse.app.map_overlay.QMenu.popup"):
            event = self.context_event(self.window, point)
            view.contextMenuEvent(event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(requests[-1][2], label)
        self.assertLess((requests[-1][0] - QPointF(800, 600)).manhattanLength(), 3)
        self.assertEqual(requests[-1][1], view.viewport().mapToGlobal(point))
        actions = [a.text() for a in self.window._label_menu.actions() if not a.isSeparator()]
        self.assertEqual(actions, ["Edit label…", "Delete label", "Add label…"])
        background_scene = QPointF(900, 700)
        background = view.mapFromScene(background_scene)
        background_event = self.context_event(self.window, background)
        with patch("mnmparse.app.map_overlay.QMenu.popup"):
            view.contextMenuEvent(background_event)
        expected = view.mapToScene(background)
        with patch("mnmparse.app.map_overlay.MapLabelDialog", return_value=self.accepted_dialog()):
            self.window._label_menu.actions()[0].trigger()
        saved = next(label for label in self.window.label_store.labels(MAP_A) if label.id != self.label().id)
        self.assertAlmostEqual(saved.x, expected.x() / 1600)
        self.assertAlmostEqual(saved.y, expected.y() / 1200)

    def test_letterbox_and_empty_scene_do_not_offer_label_actions(self):
        self.display(width=1800, height=150)
        self.window.show()
        self.app.processEvents()
        view = self.window.view
        requests = []
        view.label_context_requested.connect(lambda *args: requests.append(args))
        point = QPoint(view.viewport().width() // 2, 1)
        self.assertFalse(view._image_rect.contains(view.mapToScene(point)))
        event = self.context_event(self.window, point)
        view.contextMenuEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertEqual(requests, [])
        view.set_image(QImage())
        empty = self.context_event(self.window, view.viewport().rect().center())
        view.contextMenuEvent(empty)
        self.assertFalse(empty.isAccepted())
        self.assertEqual(requests, [])

    def test_late_map_result_cannot_reinstall_old_labels_after_zone_change(self):
        self.window.label_store.save(self.label())
        old_entry = self.display()
        old_token = self.window._token
        self.window.set_zone("Night Harbor")
        new_label = self.label(url=MAP_B, id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", name="Harbor route")
        self.window.label_store.save(new_label)
        self.display(url=MAP_B, zone="Night Harbor")
        old_image = QImage(1000, 800, QImage.Format.Format_RGB32)
        self.window._ready(old_token, ([old_entry], old_image, True, MAP_A), "")
        self.assertEqual(self.window._selected_url, MAP_B)
        self.assertEqual(self.window._loaded_zone, "Night Harbor")
        self.assertEqual(self.item_labels(self.window), [new_label])

    def test_loading_or_invalid_new_map_never_accepts_old_map_context(self):
        self.display()
        self.window.set_zone("Night Harbor")
        with patch("mnmparse.app.map_overlay.QMenu.popup") as popup:
            self.window._label_context_menu(QPointF(200, 200), QPoint(10, 10), None)
        popup.assert_not_called()
        self.window._ready(self.window._token, None, "Synthetic offline error")
        with patch("mnmparse.app.map_overlay.QMenu.popup") as popup:
            self.window._label_context_menu(QPointF(200, 200), QPoint(10, 10), None)
        popup.assert_not_called()
        self.assertEqual(self.item_labels(self.window), [])

    def test_open_menu_keeps_original_map_and_position_when_selection_changes(self):
        self.display()
        with patch("mnmparse.app.map_overlay.QMenu.popup"):
            self.window._label_context_menu(QPointF(200, 600), QPoint(10, 10), None)
        menu = self.window._label_menu
        add = menu.actions()[0]
        self.display(url=MAP_B, zone="Night Harbor")
        with patch("mnmparse.app.map_overlay.MapLabelDialog", return_value=self.accepted_dialog()):
            add.trigger()
        saved, = self.window.label_store.labels(MAP_A)
        self.assertEqual((saved.x, saved.y), (.2, .75))
        self.assertEqual(self.window.label_store.labels(MAP_B), [])
        self.assertEqual(self.item_labels(self.window), [])

    def test_locked_map_still_offers_labels_and_zoom(self):
        self.display()
        self.window.show()
        self.window.set_locked(True)
        self.app.processEvents()
        view = self.window.view
        point = view.mapFromScene(QPointF(500, 400))
        event = self.context_event(self.window, point)
        with patch("mnmparse.app.map_overlay.QMenu.popup"):
            self.app.sendEvent(view.viewport(), event)
        self.assertTrue(event.isAccepted())
        self.assertEqual(self.window._label_menu.actions()[0].text(), "Add label…")
        before = view.transform().m11()
        view.zoom(1.2)
        self.assertAlmostEqual(view.transform().m11(), before * 1.2)
        self.assertTrue(self.window.locked)
        self.assertIsNone(self.window._drag_offset)

    def test_removed_scene_markers_are_deleted_and_late_changes_ignore_shutdown(self):
        self.display()
        label = self.label()
        self.window.label_store.save(label)
        old_item = self.window.view._label_items[0]
        self.window.label_store.save(replace(label, name="Updated"))
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.assertFalse(isValid(old_item))
        self.window.shutdown()
        with patch.object(self.window.view, "set_labels") as repaint:
            self.window.label_store.save(replace(label, name="Late refresh"))
        repaint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
