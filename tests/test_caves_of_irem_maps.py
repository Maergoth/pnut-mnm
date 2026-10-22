"""Zone-named primary map panels and existing alternate-only cache upgrades."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mnmparse.maps import MapRepository, parse_maps


# Minimal structure from the public Caves of Irem API response, 2026-10-22.
# Its primary map is a regular full-file figure in a bordered, zone-titled table.
PRIMARY = '''<table style="border: 1px solid gray; max-width: 1000px;">
<tbody><tr><td><div class="mw-heading mw-heading3"><h3 id="Caves_of_Irem">Caves of Irem</h3>
<span class="mw-editsection">[<a>edit</a>]</span></div>
<figure class="mw-halign-right" typeof="mw:File"><a href="/wiki/File:Irem.png">
<img src="//static.wikitide.net/monstersandmemorieswiki/thumb/2/27/Irem.png/800px-Irem.png" />
</a><figcaption></figcaption></figure></td></tr></tbody></table>'''
ALTERNATE = '''<table style="border: 1px solid gray;"><tr><td>
<h4>Caves of Irem Alternate Map</h4><figure class="mw-halign-right mw-image-border" typeof="mw:File">
<a href="/wiki/File:Caves_of_Irem1stDraft.jpg"><img
src="//static.wikitide.net/monstersandmemorieswiki/thumb/f/fc/Caves_of_Irem1stDraft.jpg/500px-Caves_of_Irem1stDraft.jpg" />
</a><figcaption></figcaption></figure></td></tr></table>'''
INTRO = '''<figure class="mw-halign-right" typeof="mw:File/Thumb">
<a href="/wiki/File:Deep_Dunes_screenshot.png"><img
src="//static.wikitide.net/monstersandmemorieswiki/thumb/e/ea/Deep_Dunes_screenshot.png/400px-Deep_Dunes_screenshot.png" />
</a><figcaption>Screenshot taken in the Deep Dunes</figcaption></figure>'''


class CavesOfIremMapTests(unittest.TestCase):
    def test_primary_precedes_alternate_and_keeps_original_resolution(self):
        maps = parse_maps(INTRO + PRIMARY + ALTERNATE, "Caves of Irem")
        self.assertEqual([entry.title for entry in maps], ["Caves of Irem", "Caves of Irem1stDraft"])
        self.assertEqual(maps[0].url, "https://static.wikitide.net/monstersandmemorieswiki/2/27/Irem.png")
        self.assertTrue(maps[0].source.endswith("/wiki/File:Irem.png"))
        self.assertTrue(maps[1].url.endswith("/f/fc/Caves_of_Irem1stDraft.jpg"))

    def test_zone_titled_panel_does_not_accept_thumbnail_photographs(self):
        self.assertEqual(parse_maps(PRIMARY.replace('typeof="mw:File"', 'typeof="mw:File/Thumb"'),
                                    "Caves of Irem"), [])

    def test_zone_title_requires_a_bordered_section_below_the_page_heading(self):
        for html in (PRIMARY.replace('style="border: 1px solid gray; max-width: 1000px;"', ""),
                     PRIMARY.replace("<h3", "<h1").replace("</h3>", "</h1>"),
                     PRIMARY.replace("Caves of Irem</h3>", "Other Zone</h3>")):
            with self.subTest(html=html):
                self.assertEqual(parse_maps(html, "Caves of Irem"), [])

    def test_zone_panel_evidence_ends_at_table_boundary(self):
        unrelated = PRIMARY[PRIMARY.index("<figure"):PRIMARY.index("</figure>") + len("</figure>")]
        unrelated = unrelated.replace("Irem.png", "Beetle.png")
        self.assertEqual(len(parse_maps(PRIMARY + unrelated, "Caves of Irem")), 1)

    def test_existing_alternate_only_cache_refresh_adds_primary_without_deleting_images(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = MapRepository(Path(folder))
            old = parse_maps(ALTERNATE, "Caves of Irem")
            repo.save_maps("Caves of Irem", old)
            old_image = repo._path(old[0].url, ".image")
            old_image.write_bytes(b"cached alternate fixture")
            self.assertEqual(repo.known_maps("Caves of Irem"), old)
            payload = json.dumps({"parse": {"text": {"*": INTRO + PRIMARY + ALTERNATE}}}).encode()
            with patch("mnmparse.maps._download", return_value=payload):
                updated = repo.maps("Caves of Irem", refresh=True, strict=True)
            self.assertEqual(len(updated), 2)
            self.assertEqual(MapRepository(Path(folder)).known_maps("Caves of Irem"), updated)
            self.assertEqual(old_image.read_bytes(), b"cached alternate fixture")
            with patch("mnmparse.maps._download", side_effect=OSError("offline")):
                self.assertEqual(repo.maps("Caves of Irem", refresh=True), updated)

    def test_verified_fallback_catalog_includes_both_maps_during_wiki_outage(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = MapRepository(Path(folder))
            expected = parse_maps(PRIMARY + ALTERNATE, "Caves of Irem")
            self.assertEqual(repo.known_maps("Caves of Irem"), expected)
            with patch("mnmparse.maps._download", side_effect=OSError("offline")):
                self.assertEqual(repo.maps("Caves of Irem"), expected)


if __name__ == "__main__":
    unittest.main()
