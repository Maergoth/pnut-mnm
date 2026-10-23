"""Atlas navigation uses verified map IDs and keeps searches as query data."""
import unittest
from urllib.parse import parse_qs, urlsplit

from mnmparse.atlas import ATLAS_ORIGIN, atlas_url, atlas_zone, is_atlas_url


class AtlasUrlTests(unittest.TestCase):
    def test_compact_world_and_unknown_zones(self):
        expected = ATLAS_ORIGIN + "/?embed=1"
        for zone in ("", "World", "world", "Night Harbor Sewers", "The Hidden Grotto", "New Uncharted Zone"):
            with self.subTest(zone=zone):
                self.assertEqual(atlas_url(zone), expected)
                self.assertIsNone(atlas_zone(atlas_url(zone)))

    def test_known_exceptional_ids_and_existing_zone_aliases(self):
        examples = {
            "Ail'Vorith": "ail-vorith",
            "Ail\u2019Vorith": "ail-vorith",
            "Keeper\u2019s Bight": "keepers-bight",
            "Keeper's Bight": "keepers-bight",
            "Tomb of the Last Wyrmsbane": "wyrmsbane-tomb",
            "Wyrmsbane Tomb": "wyrmsbane-tomb",
            "wyrmsbane": "wyrmsbane-tomb",
            "Night_Harbor": "night-harbor",
            "Night Harbor (West)": "night-harbor",
            "FallenPass": "fallen-pass",
        }
        for zone, map_id in examples.items():
            with self.subTest(zone=zone):
                self.assertEqual(atlas_url(zone), f"{ATLAS_ORIGIN}/{map_id}/?embed=1")

    def test_all_verified_zone_ids_round_trip(self):
        zones = (
            "Night Harbor", "Underdocks", "Ail'Vorith", "Blind Midden", "Faelindral",
            "Evershade Weald", "Sungreet Strand", "Shaded Dunes", "Fallen Pass",
            "Tomb of the Last Wyrmsbane", "Glass Flats", "Ancient Crypt", "Infested Crypt",
            "Blacktide Bay", "Scarwood", "Vale of Zintar", "Grain Cellar", "Great Cavern Sea",
            "Caves of Irem", "Tel Ekir", "Keeper's Bight", "Fallen Watch", "Shallow Shoals",
            "Glinting Hollow", "Sunken Resort", "Rothold", "Grimtide Sanctum",
        )
        self.assertEqual(len(zones), 27)
        for zone in zones:
            with self.subTest(zone=zone):
                url = atlas_url(zone, "Fictional NPC")
                self.assertTrue(is_atlas_url(url))
                self.assertEqual(atlas_zone(url), zone)

    def test_named_search_retains_unicode_punctuation_and_restricts_known_map(self):
        name = "Keeper\u2019s Acolyte, \u00c9lan & 'Friends' / ?map=world #1"
        url = atlas_url("Keeper\u2019s Bight", name)
        self.assertEqual(urlsplit(url).path, "/keepers-bight/")
        self.assertEqual(parse_qs(urlsplit(url).query), {
            "embed": ["1"], "find": [name], "map": ["keepers-bight"],
        })
        self.assertNotIn("\u2019", url)
        self.assertIn("%27", url)
        self.assertTrue(is_atlas_url(url))
        unknown = atlas_url("Uncharted Place", name)
        self.assertEqual(parse_qs(urlsplit(unknown).query), {"embed": ["1"], "find": [name]})

    def test_legacy_map_queries_and_path_precedence(self):
        self.assertEqual(atlas_zone(ATLAS_ORIGIN + "/?map=wyrmsbane-tomb&embed=1"), "Tomb of the Last Wyrmsbane")
        self.assertEqual(atlas_zone(ATLAS_ORIGIN + "/keepers-bight/?map=night-harbor"), "Keeper's Bight")
        for suffix in ("/?map=world", "/world/", "/?map=unknown", "/?map=rothold&map=night-harbor",
                       "/assets/map.js?map=rothold", "/unknown/"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(atlas_zone(ATLAS_ORIGIN + suffix))

    def test_origin_accepts_https_static_routes_and_explicit_standard_port(self):
        for url in (ATLAS_ORIGIN, ATLAS_ORIGIN + "/embed/", ATLAS_ORIGIN + "/assets/map.js?v=1",
                    "https://www.mnmatlas.com:443/night-harbor/", "HTTPS://WWW.MNMATLAS.COM/"):
            with self.subTest(url=url):
                self.assertTrue(is_atlas_url(url))

    def test_rejects_hostile_or_malformed_urls(self):
        urls = (
            "", None, 123, "http://www.mnmatlas.com/", "//www.mnmatlas.com/", "javascript:alert(1)",
            "https://mnmatlas.com/", "https://www.mnmatlas.com.evil.test/", "https://evil.test/www.mnmatlas.com",
            "https://user:pass@www.mnmatlas.com/", "https://@www.mnmatlas.com/",
            "https://www.mnmatlas.com@evil.test/", "https://www.mnmatlas.com:444/",
            "https://www.mnmatlas.com:/", "https://www.mnmatlas.com:bad/", "https://[www.mnmatlas.com]/",
            "https://www.mnmatlas.com./", "https://www.mnmatlas.com\\@evil.test/",
            "https://www.mnmatlas.com/\\evil", "https://www.mnmatlas.com/\nnight-harbor/",
            "\thttps://www.mnmatlas.com/", "https://www.mnmatlas.com/%0aevil",
            "https://www.mnmatlas.com/%00", "https://www.mnmatlas.com/a b", "https://www.mnmatlas.com/%GG",
        )
        for url in urls:
            with self.subTest(url=url):
                self.assertFalse(is_atlas_url(url))
                self.assertIsNone(atlas_zone(url))

    def test_untrusted_names_are_bounded_and_reject_controls(self):
        for value in (None, 1, "x" * 257, "name\nnew", "name\x00", "bad\ud800"):
            for field in ("zone", "npc"):
                with self.subTest(field=field, value=repr(value)):
                    with self.assertRaises(ValueError):
                        atlas_url(**{field: value})
        self.assertTrue(is_atlas_url(atlas_url("Night Harbor", "x" * 256)))


if __name__ == "__main__":
    unittest.main()
