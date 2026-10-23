"""Oversized wiki originals retain their pixels without lifting Qt's global limit."""
from __future__ import annotations

import gc
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtGui import QImage, QImageReader
from PySide6.QtWidgets import QApplication

from mnmparse.app.map_overlay import MapOverlay, _decode_map_image
from mnmparse.maps import MapImage, MapRepository


def _png(size: tuple[int, int]) -> bytes:
    with Image.new("RGBA", size, (19, 83, 141, 117)) as source:
        source.putpixel((size[0] - 1, size[1] - 1), (233, 107, 41, 209))
        stream = io.BytesIO()
        source.save(stream, "PNG")
        return stream.getvalue()


class MapDecodeTests(unittest.TestCase):
    def test_small_map_uses_qt_and_preserves_original_pixels(self):
        data = _png((3, 2))
        with patch("PIL.Image.open", side_effect=AssertionError("Unexpected fallback")):
            image = _decode_map_image(data)
        self.assertFalse(image.isNull())
        self.assertEqual((image.width(), image.height()), (3, 2))
        self.assertEqual(image.pixelColor(0, 0).getRgb(), (19, 83, 141, 117))
        self.assertEqual(image.pixelColor(2, 1).getRgb(), (233, 107, 41, 209))

    def test_large_map_fallback_retains_resolution_alpha_and_owned_pixels(self):
        # Exercise the real allocation-limit failure with a small fixture instead
        # of allocating the Ancient Crypt original's roughly 300 MiB in a test.
        previous_limit = QImageReader.allocationLimit()
        QImageReader.setAllocationLimit(1)
        try:
            data = _png((700, 700))
            with patch("PIL.Image.open", wraps=Image.open) as fallback:
                image = _decode_map_image(data)
            fallback.assert_called_once()
            del data
            gc.collect()
            self.assertIsInstance(image, QImage)
            self.assertFalse(image.isNull())
            self.assertEqual((image.width(), image.height()), (700, 700))
            self.assertEqual(image.pixelColor(0, 0).getRgb(), (19, 83, 141, 117))
            self.assertEqual(image.pixelColor(699, 699).getRgb(), (233, 107, 41, 209))
            self.assertEqual(QImageReader.allocationLimit(), 1)
        finally:
            QImageReader.setAllocationLimit(previous_limit)

    def test_pixel_limit_rejects_image_before_pillow_decode(self):
        data = _png((20, 20))
        with patch("mnmparse.app.map_overlay.MAX_MAP_PIXELS", 399), \
             patch("PIL.Image.open") as fallback:
            with self.assertRaisesRegex(ValueError, "pixel limit"):
                _decode_map_image(data)
        fallback.assert_not_called()

    def test_corrupt_bytes_raise_a_decode_error(self):
        for data in (b"", b"not an image", b"\x89PNG\r\n\x1a\n"):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    _decode_map_image(data)


class MapDecodeCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_null_decoded_image_does_not_delete_the_cached_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = QSettings(str(root / "map.ini"), QSettings.Format.IniFormat)
            settings.setValue("map/default_source", "images")
            repo = MapRepository(root / "cache")
            entry = MapImage("Ancient Crypt", "https://static.wikitide.net/map.png", "")
            original = _png((3, 2))
            cache = repo._path(entry.url, ".image")
            cache.parent.mkdir()
            cache.write_bytes(original)
            window = MapOverlay(settings, repo)
            try:
                window.set_zone("Ancient Crypt")
                window._ready(window._token, ([entry], QImage(), True, entry.url), "")
                self.assertEqual(cache.read_bytes(), original)
                self.assertFalse(window.view.scene().items())
                self.assertTrue(window.status.text())
            finally:
                window.shutdown()
                window.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
