"""Map state survives restart without opening a map the user left closed."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, QSettings
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.maps import MapImage


class MapStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        image = QImage(2000, 2000, QImage.Format.Format_RGB32)
        image.fill(0x123456)
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        cls.image_data = bytes(data)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "map.ini"), QSettings.Format.IniFormat)
        self.entries = [MapImage("Ground", "https://example.invalid/ground.png", ""),
                        MapImage("Upper floor", "https://example.invalid/upper.png", "")]
        self.repo = Mock()
        self.repo.maps.side_effect = lambda *_args, **_kwargs: list(self.entries)
        self.repo.image.return_value = self.image_data
        self.windows = []
        self.worker = patch("mnmparse.app.map_overlay._Load.start", autospec=True,
                            side_effect=lambda load: load.run())
        self.worker.start()

    def tearDown(self):
        for window in self.windows:
            window.shutdown()
            window.deleteLater()
        self.app.processEvents()
        self.worker.stop()
        self.temp.cleanup()

    def window(self):
        window = MapOverlay(self.settings, self.repo)
        self.windows.append(window)
        return window

    def restart(self, window):
        window.shutdown()
        self.windows.remove(window)
        window.deleteLater()
        self.app.processEvents()
        self.settings.sync()
        self.settings = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        return self.window()

    def show_zone(self, window):
        window.setGeometry(QRect(30, 40, 720, 480))
        window.set_zone("Sungreet Strand")
        window.show()
        self.app.processEvents()

    def saved_view(self, **changes):
        state = {"zone": "Sungreet Strand", "url": self.entries[1].url, "fit": False,
                 "scale": 0.8, "x": 0.5, "y": 0.55}
        state.update(changes)
        return state

    def test_restart_restores_selected_floor_zoom_pan_and_window_geometry(self):
        first = self.window()
        self.show_zone(first)
        first._select_map(1)
        first.view.zoom(3)
        first.view.centerOn(1000, 1100)
        scale = first.view.transform().m11()
        centre = first.view.mapToScene(first.view.viewport().rect().center())
        geometry = first.geometry()
        second = self.restart(first)
        self.repo.image.reset_mock()
        self.assertFalse(second.isVisible())
        self.assertEqual(second.geometry(), geometry)
        self.assertEqual(second.current_zone, "Sungreet Strand")
        second.show()
        self.app.processEvents()
        self.repo.image.assert_called_once_with(self.entries[1], refresh=False)
        self.assertEqual(second.variants.currentIndex(), 1)
        self.assertAlmostEqual(second.view.transform().m11(), scale)
        self.assertFalse(second.view._fit)
        restored = second.view.mapToScene(second.view.viewport().rect().center())
        self.assertLess((restored - centre).manhattanLength(), 5)

    def test_fit_mode_stays_fit_when_restored_window_is_resized(self):
        first = self.window()
        self.show_zone(first)
        second = self.restart(first)
        second.resize(400, 600)
        second.show()
        self.app.processEvents()
        self.assertTrue(second.view._fit)
        mapped = second.view.transform().mapRect(second.view.sceneRect())
        viewport = second.view.viewport().size()
        self.assertLessEqual(mapped.width(), viewport.width())
        self.assertLessEqual(mapped.height(), viewport.height())

    def test_fullscreen_restores_without_showing_and_returns_to_normal_geometry(self):
        first = self.window()
        self.show_zone(first)
        geometry = first.geometry()
        first.toggle_fullscreen()
        self.app.processEvents()
        first.hide()
        second = self.restart(first)
        self.assertFalse(second.isVisible())
        self.assertTrue(second.isFullScreen())
        self.assertTrue(self.settings.value("map/fullscreen", type=bool))
        self.assertEqual(self.settings.value("map/geometry"), geometry)
        second.show()
        self.app.processEvents()
        second.exit_fullscreen()
        self.app.processEvents()
        self.assertEqual(second.geometry(), geometry)
        self.assertFalse(self.settings.value("map/fullscreen", type=bool))

    def test_unavailable_saved_floor_falls_back_to_fitted_first_map(self):
        self.settings.setValue("map/zone", "Sungreet Strand")
        self.settings.setValue("map/view", self.saved_view(url="https://example.invalid/removed.png"))
        window = self.window()
        window.show()
        self.app.processEvents()
        self.assertEqual(window.variants.currentIndex(), 0)
        self.assertTrue(window.view._fit)
        self.assertEqual(self.settings.value("map/view")["url"], self.entries[0].url)

    def test_failed_load_preserves_saved_view_for_next_successful_restart(self):
        state = self.saved_view()
        self.settings.setValue("map/zone", state["zone"])
        self.settings.setValue("map/view", state)
        self.repo.image.side_effect = OSError("offline")
        first = self.window()
        first.show()
        self.app.processEvents()
        second = self.restart(first)
        self.assertEqual(self.settings.value("map/view"), state)
        self.repo.image.side_effect = None
        second.show()
        self.app.processEvents()
        self.assertEqual(second.variants.currentIndex(), 1)
        self.assertAlmostEqual(second.view.transform().m11(), state["scale"])

    def test_new_zone_while_hidden_does_not_open_or_reuse_old_view(self):
        first = self.window()
        self.show_zone(first)
        first.view.zoom(3)
        first.hide()
        self.repo.image.reset_mock()
        first.on_message(None, SimpleNamespace(kind="zone", target="Night Harbor"))
        self.assertFalse(first.isVisible())
        self.repo.image.assert_not_called()
        second = self.restart(first)
        self.assertFalse(second.isVisible())
        self.assertEqual(second.current_zone, "Night Harbor")
        second.show()
        self.app.processEvents()
        self.assertTrue(second.view._fit)
        self.assertEqual(second.variants.currentIndex(), 0)

    def test_invalid_view_state_is_ignored_without_breaking_map_load(self):
        invalid = [None, "invalid", [], {}, self.saved_view(scale=float("nan")),
                   self.saved_view(scale=0), self.saved_view(x=4), self.saved_view(y=True),
                   self.saved_view(fit="False"), self.saved_view(url=42)]
        for state in invalid:
            with self.subTest(state=state):
                self.settings.setValue("map/zone", "Sungreet Strand")
                self.settings.setValue("map/view", state)
                window = self.window()
                window.show()
                self.app.processEvents()
                self.assertTrue(window.view._fit)
                self.assertEqual(window.variants.currentIndex(), 0)
                window.shutdown()

    def test_extreme_numeric_view_state_is_rejected_without_float_overflow(self):
        for key in ("scale", "x", "y"):
            for value in (10 ** 1000, -(10 ** 1000), float("inf"), -float("inf")):
                with self.subTest(key=key, value=value):
                    self.assertIsNone(MapOverlay._read_view_state(self.saved_view(**{key: value})))


if __name__ == "__main__":
    unittest.main()
