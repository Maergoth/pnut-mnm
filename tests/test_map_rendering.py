"""Saved map pixels remain visible through the stacked provider viewport."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.maps import MapImage


class _Atlas(QWidget):
    zone_changed = Signal(str)
    fallback_requested = Signal()

    def activate(self, _zone):
        pass

    def deactivate(self):
        pass

    def shutdown(self):
        pass


class MapRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "map.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("map/default_source", "images")
        self.loader = patch("mnmparse.app.map_overlay._Load.start")
        self.loader.start()
        self.atlas = patch("mnmparse.app.map_overlay.AtlasView", _Atlas)
        self.atlas.start()
        self.window = MapOverlay(self.settings, Mock())
        self.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.window.resize(700, 500)
        self.window.set_zone("Night Harbor")
        self.window.show()
        self.app.processEvents()
        entry = MapImage("Synthetic map", "https://static.wikitide.net/map.png", "")
        image = QImage(1000, 800, QImage.Format.Format_RGB32)
        self.color = QColor("#c74020")
        image.fill(self.color)
        self.window._ready(self.window._token, ([entry], image, True, entry.url), "")

    def tearDown(self):
        self.window.shutdown()
        self.window.deleteLater()
        self.app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.atlas.stop()
        self.loader.stop()
        self.settings.sync()
        self.temp.cleanup()

    def assert_map_visible(self):
        self.app.processEvents()
        view = self.window.view
        rendered = self.window.grab().toImage()
        rect = view._image_rect
        # Sample the image itself rather than the letterbox around a fitted map.
        # Checking its whole area also catches a stale clip after provider resize.
        for x in (.15, .5, .85):
            for y in (.15, .5, .85):
                point = view.viewport().mapTo(
                    self.window, view.mapFromScene(QPointF(rect.width() * x, rect.height() * y)))
                self.assertEqual(rendered.pixelColor(point), self.color, (x, y))

    def test_initial_overlay_paints_map_beyond_the_construction_size(self):
        self.assert_map_visible()

    def test_map_stays_visible_after_overlay_resize(self):
        for width, height in ((1100, 760), (380, 260), (800, 600)):
            with self.subTest(size=(width, height)):
                self.window.resize(width, height)
                self.assert_map_visible()

    def test_atlas_switch_and_resize_restores_visible_saved_map(self):
        self.window.set_source("atlas")
        self.app.processEvents()
        self.window.resize(1000, 750)
        self.app.processEvents()
        self.window.set_source("images")
        self.assert_map_visible()


if __name__ == "__main__":
    unittest.main()
