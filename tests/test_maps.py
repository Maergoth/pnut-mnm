"""Wiki extraction/cache and zone-map lifecycle, without network or game capture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.maps import MapImage, MapRepository, parse_maps, wiki_url, zone_title

HTML = '''
<figure class="mw-halign-right"><a href="/wiki/File:Beach.jpg"><img
src="//static.wikitide.net/monstersandmemorieswiki/thumb/a/ab/Beach.jpg/400px-Beach.jpg"></a>
<figcaption>Overview of the beach</figcaption></figure>
<figure class="mw-halign-right mw-image-border"><a href="/wiki/File:Strand_v4.jpg"><img
src="//static.wikitide.net/monstersandmemorieswiki/thumb/0/0c/Strand_v4.jpg/600px-Strand_v4.jpg"></a></figure>
<figure><a href="/wiki/File:Floor2.png"><img
src="//static.wikitide.net/monstersandmemorieswiki/f/fa/Floor2.png"></a><figcaption>Second floor map</figcaption></figure>
'''


class MapTests(unittest.TestCase):
    def test_zone_aliases_and_url_encoding(self):
        self.assertEqual(zone_title("Night Harbor (West)"), "Night Harbor")
        self.assertEqual(zone_title("Wyrmsbane Tomb"), "Tomb of the Last Wyrmsbane")
        self.assertEqual(zone_title("Sungreet_Strand."), "Sungreet Strand")
        self.assertTrue(wiki_url("Ail'Vorith").endswith("Ail%27Vorith"))

    def test_maps_not_overview_photos_and_original_resolution(self):
        maps = parse_maps(HTML, "Sungreet Strand")
        self.assertEqual(len(maps), 2)
        self.assertEqual(maps[0].url, "https://static.wikitide.net/monstersandmemorieswiki/0/0c/Strand_v4.jpg")
        self.assertEqual(maps[1].title, "Second floor map")
        self.assertIn("/wiki/File:Strand_v4.jpg", maps[0].source)

    def test_mapless_and_external_images(self):
        self.assertEqual(parse_maps("<p>No map yet</p>", "Void"), [])
        html = '<figure class="mw-image-border"><img src="https://example.com/map.png"></figure>'
        self.assertEqual(parse_maps(html, "Void"), [])

    def test_cache_survives_offline_refresh(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = MapRepository(Path(temp))
            payload = json.dumps({"parse": {"text": {"*": HTML}}}).encode()
            with patch("mnmparse.maps._download", return_value=payload) as download:
                entries = repo.maps("Sungreet Strand")
                self.assertEqual(repo.maps("Sungreet Strand"), entries)
                self.assertEqual(download.call_count, 1)
            with patch("mnmparse.maps._download", return_value=b"image"):
                self.assertEqual(repo.image(entries[0]), b"image")
            with patch("mnmparse.maps._download", side_effect=OSError("offline")):
                self.assertEqual(repo.maps("Sungreet Strand", refresh=True), entries)
                self.assertEqual(repo.image(entries[0], refresh=True), b"image")
                with self.assertRaises(OSError):
                    repo.maps("Night Harbor")

    def test_refresh_updates_same_image_url(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = MapRepository(Path(temp))
            entry = parse_maps(HTML, "Sungreet Strand")[0]
            with patch("mnmparse.maps._download", side_effect=[b"old", b"new"]):
                self.assertEqual(repo.image(entry), b"old")
                self.assertEqual(repo.image(entry, refresh=True), b"new")
                self.assertEqual(repo.image(entry), b"new")

    def test_successful_empty_refresh_discards_old_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = MapRepository(Path(temp))
            payloads = [json.dumps({"parse": {"text": {"*": html}}}).encode() for html in (HTML, "", "")]
            with patch("mnmparse.maps._download", side_effect=payloads) as download:
                self.assertEqual(len(repo.maps("Sungreet Strand")), 2)
                self.assertEqual(repo.maps("Sungreet Strand", refresh=True), [])
                self.assertEqual(repo.maps("Sungreet Strand"), [])
                self.assertEqual(download.call_count, 3)


class MapWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from PySide6.QtCore import QSettings
        from mnmparse.app.map_overlay import MapOverlay
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.window = MapOverlay(self.settings, MapRepository(Path(self.temp.name) / "cache"))

    def tearDown(self):
        self.window.shutdown()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def test_zone_events_and_stale_response(self):
        self.window.on_message(None, SimpleNamespace(kind="zone", target="Sungreet Strand"))
        old_token = self.window._token
        self.window.on_message(None, SimpleNamespace(kind="zone", target="Night Harbor (East)"))
        self.assertEqual(self.window.current_zone, "Night Harbor")
        self.window._ready(old_token, None, "old error")
        self.assertIn("Night Harbor", self.window.status.text())
        self.assertNotIn("unavailable", self.window.status.text())
        self.window.on_message(None, SimpleNamespace(kind="zone", target=None))
        self.assertEqual(self.window.current_zone, "Night Harbor")

    def test_no_map_and_failed_fetch_never_keep_previous_image(self):
        from PySide6.QtGui import QImage
        self.window.view.set_image(QImage(20, 20, QImage.Format.Format_RGB32))
        self.window.set_zone("Void")
        self.assertEqual(len(self.window.view.scene().items()), 0)
        self.window._ready(self.window._token, ([], b"", True), "")
        self.assertIn("No map", self.window.status.text())
        self.window._ready(self.window._token, None, "network unavailable")
        self.assertIn("Refresh", self.window.status.text())

    def test_window_resize_fullscreen_return_and_geometry_saved(self):
        self.window.resize(720, 480)
        self.window.show()
        self.app.processEvents()
        original = self.window.geometry()
        self.window.toggle_fullscreen()
        self.app.processEvents()
        self.assertTrue(self.window.isFullScreen())
        self.window.exit_fullscreen()
        self.app.processEvents()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.size(), original.size())
        self.window.hide()
        self.assertEqual(self.settings.value("map/geometry").size(), original.size())

    def test_real_zone_parse_routes_to_overlay(self):
        from mnmparse.parser import parse_line
        event = parse_line("You have entered Sungreet Strand.", 1.0)
        self.window.on_message(None, event)
        self.assertEqual(self.window.current_zone, "Sungreet Strand")

    def test_failed_first_image_still_offers_other_maps(self):
        entries = parse_maps(HTML, "Sungreet Strand")
        self.window.set_zone("Sungreet Strand")
        self.window._ready(self.window._token, (entries, b"", True), "image not found")
        self.assertEqual(self.window.variants.count(), 2)
        self.assertTrue(self.window.variants.isEnabled())
        with patch.object(self.window, "load") as load:
            self.window._select_map(1)
        load.assert_called_once_with(entry=entries[1])

    def test_cancelled_worker_does_not_begin_image_download(self):
        import threading
        from mnmparse.app.map_overlay import _Load
        from unittest.mock import Mock
        repo = Mock()
        worker = _Load(1, repo, "Sungreet Strand", None, False, threading.Semaphore(1))
        def page(*_args, **_kwargs):
            worker.cancelled.set()
            return parse_maps(HTML, "Sungreet Strand")
        repo.maps.side_effect = page
        worker.run()
        repo.image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
