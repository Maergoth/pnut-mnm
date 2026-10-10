"""Map version checks reuse identical local bytes and preserve cache on failures."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, urlsplit

from mnmparse.maps import MAX_DOWNLOAD, MapImage, MapRepository, WIKI, _download


def map_entry(filename: str) -> MapImage:
    encoded = quote(filename.replace(" ", "_"), safe="")
    return MapImage(filename, f"https://static.wikitide.net/monstersandmemorieswiki/a/ab/{encoded}",
                    f"{WIKI}/wiki/File:{encoded}")


def digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class MapVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = MapRepository(Path(self.temp.name))
        self.entry = map_entry("Night harbor V5.jpg")
        self.image_path = self.repo._path(self.entry.url, ".image")

    def cache(self, data: bytes) -> None:
        self.image_path.write_bytes(data)

    def test_hashes_batch_at_most_fifty_and_match_urls_instead_of_response_order(self):
        entries = [map_entry(f"Floor {index:02}.png") for index in range(51)]
        entries.append(map_entry("Ail'Vorith map.jpg"))
        by_title = {"File:" + entry.title: entry for entry in entries}
        batches = []

        def metadata(url):
            params = parse_qs(urlsplit(url).query)
            titles = params["titles"][0].split("|")
            batches.append(titles)
            self.assertLessEqual(len(titles), 50)
            self.assertEqual(params["action"], ["query"])
            self.assertEqual(params["prop"], ["imageinfo"])
            self.assertEqual(set(params["iiprop"][0].split("|")), {"sha1", "url"})
            pages = {
                str(index): {"title": title, "imageinfo": [{
                    "url": by_title[title].url,
                    "sha1": digest(title.encode()),
                }]}
                for index, title in enumerate(reversed(titles))
            }
            return json.dumps({"query": {"pages": pages}}).encode()

        with patch("mnmparse.maps._download", side_effect=metadata) as download:
            hashes = self.repo.image_hashes(iter(entries))
        self.assertEqual(download.call_count, 2)
        self.assertEqual([len(batch) for batch in batches], [50, 2])
        self.assertEqual(hashes, {entry.url: digest(("File:" + entry.title).encode()) for entry in entries})

    def test_normalized_titles_and_equivalent_encoded_urls_are_accepted(self):
        entry = map_entry("Ail'Vorith map.jpg")
        payload = {"query": {
            "normalized": [{"from": "File:Ail'Vorith_map.jpg", "to": "File:Ail'Vorith map.jpg"}],
            "pages": {"9312": {"title": "File:Ail'Vorith map.jpg", "imageinfo": [{
                "url": entry.url.replace("%27", "'"), "sha1": digest(b"map").upper(),
            }]}},
        }}
        with patch("mnmparse.maps._download", return_value=json.dumps(payload).encode()) as download:
            self.assertEqual(self.repo.image_hashes([entry]), {entry.url: digest(b"map")})
        params = parse_qs(urlsplit(download.call_args.args[0]).query)
        self.assertEqual(params["titles"], ["File:Ail'Vorith map.jpg"])

    def test_missing_invalid_or_unrelated_metadata_cannot_validate_cache(self):
        entries = [map_entry(f"Map {index}.png") for index in range(8)]
        pages = {
            "0": {"missing": ""},
            "1": {"imageinfo": []},
            "2": {"imageinfo": [{"url": entries[2].url}]},
            "3": {"imageinfo": [{"url": entries[3].url, "sha1": 123}]},
            "4": {"imageinfo": [{"url": entries[4].url, "sha1": "f" * 39}]},
            "5": {"imageinfo": [{"url": entries[5].url, "sha1": "z" * 40}]},
            "6": {"imageinfo": [{"url": "https://example.com/Map_6.png", "sha1": digest(b"map")}]},
            "7": {"imageinfo": [{"url": entries[7].url.replace("/a/ab/", "/b/bc/"),
                                   "sha1": digest(b"map")}]},
        }
        with patch("mnmparse.maps._download", return_value=json.dumps({"query": {"pages": pages}}).encode()):
            self.assertEqual(self.repo.image_hashes(entries), {})

    def test_empty_or_untrusted_entries_do_not_request_metadata(self):
        external = MapImage("map", "https://example.com/map.png", WIKI)
        with patch("mnmparse.maps._download") as download:
            self.assertEqual(self.repo.image_hashes([]), {})
            self.assertEqual(self.repo.image_hashes([external]), {})
        download.assert_not_called()

    def test_api_errors_propagate_for_caller_to_choose_fallback(self):
        with patch("mnmparse.maps._download", return_value=b'{"error":{"info":"wiki unavailable"}}'):
            with self.assertRaisesRegex(ValueError, "wiki unavailable"):
                self.repo.image_hashes([self.entry])
        with patch("mnmparse.maps._download", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                self.repo.image_hashes([self.entry])

    def test_legacy_identical_cache_skips_image_download_and_write(self):
        self.cache(b"old map from an earlier app version")
        with patch("mnmparse.maps._download") as download, patch.object(self.repo, "_save") as save:
            changed = self.repo.update_image(self.entry, sha1=digest(self.image_path.read_bytes()))
        self.assertFalse(changed)
        download.assert_not_called()
        save.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.image_path])

    def test_replacement_at_same_url_downloads_and_updates_cache(self):
        self.cache(b"old map")
        with patch("mnmparse.maps._download", return_value=b"new map") as download:
            self.assertTrue(self.repo.update_image(self.entry, sha1=digest(b"new map")))
        download.assert_called_once_with(self.entry.url)
        self.assertEqual(self.image_path.read_bytes(), b"new map")

    def test_missing_empty_and_oversized_cache_each_require_download(self):
        for state in ("missing", "empty", "oversized"):
            with self.subTest(state=state):
                self.image_path.unlink(missing_ok=True)
                if state == "empty":
                    self.cache(b"")
                elif state == "oversized":
                    self.cache(b"oversized")
                with patch("mnmparse.maps.MAX_DOWNLOAD", 5), \
                     patch("mnmparse.maps._download", return_value=b"new") as download:
                    self.assertTrue(self.repo.update_image(self.entry, sha1=digest(b"new")))
                download.assert_called_once_with(self.entry.url)
                self.assertEqual(self.image_path.read_bytes(), b"new")

    def test_wrong_hash_or_empty_download_preserves_old_image(self):
        for downloaded in (b"stale CDN map", b""):
            with self.subTest(downloaded=downloaded):
                self.cache(b"old map")
                with patch("mnmparse.maps._download", return_value=downloaded), \
                     patch.object(self.repo, "_save") as save:
                    with self.assertRaises(ValueError):
                        self.repo.update_image(self.entry, sha1=digest(b"new map"))
                save.assert_not_called()
                self.assertEqual(self.image_path.read_bytes(), b"old map")

    def test_download_failure_preserves_old_image(self):
        self.cache(b"old map")
        with patch("mnmparse.maps._download", side_effect=OSError("offline")):
            with self.assertRaisesRegex(OSError, "offline"):
                self.repo.update_image(self.entry, sha1=digest(b"new map"))
        self.assertEqual(self.image_path.read_bytes(), b"old map")

    def test_disk_commit_failure_preserves_old_image_and_removes_temporary_file(self):
        self.cache(b"old map")
        with patch("mnmparse.maps._download", return_value=b"new map"), \
             patch("mnmparse.maps.os.replace", side_effect=OSError("disk full")), \
             self.assertLogs("mnmparse.maps", level="WARNING"):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.repo.update_image(self.entry, sha1=digest(b"new map"))
        self.assertEqual(self.image_path.read_bytes(), b"old map")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.image_path])

    def test_metadata_unavailable_fallback_downloads_but_does_not_rewrite_identical_image(self):
        self.cache(b"old map")
        with patch("mnmparse.maps._download", return_value=b"old map") as download, \
             patch.object(self.repo, "_save") as save:
            self.assertFalse(self.repo.update_image(self.entry))
        download.assert_called_once_with(self.entry.url)
        save.assert_not_called()
        self.assertEqual(self.image_path.read_bytes(), b"old map")

    def test_metadata_unavailable_fallback_still_refreshes_changed_image(self):
        self.cache(b"old map")
        with patch("mnmparse.maps._download", return_value=b"new map"):
            self.assertTrue(self.repo.update_image(self.entry))
        self.assertEqual(self.image_path.read_bytes(), b"new map")


class DownloadGuardTests(unittest.TestCase):
    def response(self, data, length=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = map_entry("Map.png").url
        response.headers = {} if length is None else {"Content-Length": str(length)}
        response.read.return_value = data
        return response

    def test_declared_oversized_image_is_rejected_before_body_is_read(self):
        response = self.response(b"", MAX_DOWNLOAD + 1)
        with patch("mnmparse.maps.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                _download(map_entry("Map.png").url)
        response.read.assert_not_called()

    def test_truncated_image_is_rejected(self):
        response = self.response(b"partial", len(b"partial") + 10)
        with patch("mnmparse.maps.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "incomplete"):
                _download(map_entry("Map.png").url)

    def test_download_without_content_length_still_enforces_size_limit(self):
        response = self.response(b"12345")
        with patch("mnmparse.maps.MAX_DOWNLOAD", 4), \
             patch("mnmparse.maps.urlopen", return_value=response):
            with self.assertRaises(ValueError):
                _download(map_entry("Map.png").url)
        response.read.assert_called_once_with(5)

    def test_ancient_crypt_sized_image_is_permitted_without_large_test_allocation(self):
        # The real map is 41,735,458 bytes; mock its length without allocating it.
        content = MagicMock()
        content.__len__.return_value = 41_735_458
        response = self.response(content, 41_735_458)
        with patch("mnmparse.maps.urlopen", return_value=response):
            self.assertIs(_download(map_entry("Map.png").url), content)
        response.read.assert_called_once_with(MAX_DOWNLOAD + 1)


if __name__ == "__main__":
    unittest.main()
