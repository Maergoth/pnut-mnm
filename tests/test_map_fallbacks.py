"""Blocked wiki discovery still checks known maps against current CDN metadata."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from threading import Event, Lock, Thread, current_thread
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from mnmparse.map_catalog import MAP_CATALOG
from mnmparse.maps import (
    API_COOLDOWN, MAX_DOWNLOAD, MapImage, MapRepository, WIKI, ZONES,
    _cdn_sha1, _head_headers,
)


def base36_sha1(data: bytes) -> str:
    value = int(hashlib.sha1(data).hexdigest(), 16)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    encoded = ""
    while value:
        value, remainder = divmod(value, 36)
        encoded = digits[remainder] + encoded
    return encoded or "0"


class MapFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = MapRepository(Path(self.temp.name))
        self.entry = MapImage(*MAP_CATALOG["Ancient Crypt"][0])

    def test_catalog_covers_checked_zones_without_claiming_unknown_zones_are_mapless(self):
        self.assertEqual(set(MAP_CATALOG), set(ZONES))
        self.assertEqual(self.repo.known_maps("AncientCrypt"), [self.entry])
        self.assertEqual(self.repo.known_maps("Blacktide Bay"), [])
        self.assertIsNone(self.repo.known_maps("Undiscovered Zone"))
        with self.assertRaises(TypeError):
            MAP_CATALOG["Ancient Crypt"] = ()

    def test_valid_local_manifest_takes_precedence_and_invalid_manifest_is_ignored(self):
        local = MapImage("New map", self.entry.url.replace(".png", "_new.png"), self.entry.source)
        self.repo.save_maps("Ancient Crypt", [local])
        self.assertEqual(self.repo.known_maps("Ancient Crypt"), [local])
        path = self.repo._path("Ancient Crypt", ".json")
        for content in ('null', '{}', '[{"title":123,"url":null,"source":[]}]',
                        '[{"title":"bad","url":"https://example.com/map.png","source":"' + WIKI + '"}]'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(self.repo.known_maps("Ancient Crypt"), [self.entry])

    def test_normal_view_falls_back_without_saving_but_strict_refresh_reports_block(self):
        error = HTTPError(WIKI, 403, "Forbidden", {}, None)
        self.addCleanup(error.close)
        with patch("mnmparse.maps._download", side_effect=error) as download:
            self.assertEqual(self.repo.maps("Ancient Crypt"), [self.entry])
            self.assertFalse(self.repo._path("Ancient Crypt", ".json").exists())
            with self.assertRaises(URLError):
                self.repo.maps("Ancient Crypt", refresh=True, strict=True)
            # The second call observes the cooldown instead of repeating a 403.
            download.assert_called_once()

    def test_live_map_removal_stays_removed_offline_and_empty_lists_are_rechecked(self):
        self.repo.save_maps("Ancient Crypt", [self.entry])
        empty_page = b'{"parse":{"text":{"*":"<p>No map yet</p>"}}}'
        with patch("mnmparse.maps._download", return_value=empty_page) as download:
            self.assertEqual(self.repo.maps("Ancient Crypt", refresh=True, strict=True), [])
            self.assertEqual(self.repo.maps("Ancient Crypt"), [])
            self.assertEqual(download.call_count, 2)
        path = self.repo._path("Ancient Crypt", ".json")
        self.assertEqual(json.loads(path.read_text()), [])
        self.assertEqual(self.repo.known_maps("Ancient Crypt"), [])
        with patch("mnmparse.maps._download", side_effect=URLError("offline")):
            self.assertEqual(self.repo.maps("Ancient Crypt"), [])

    def test_unknown_zone_offline_and_malformed_api_payloads_do_not_report_cached_success(self):
        with patch("mnmparse.maps._download", side_effect=URLError("offline")):
            with self.assertRaises(URLError):
                self.repo.maps("Undiscovered Zone")
        for payload in (b"not json", b"{}", b'{"error":{"info":"missing page"}}'):
            repo = MapRepository(Path(self.temp.name))
            with self.subTest(payload=payload), patch("mnmparse.maps._download", return_value=payload):
                with self.assertRaises((ValueError, KeyError)):
                    repo.maps("Ancient Crypt")

    def test_blocked_api_reuses_legacy_bytes_via_cdn_hash_without_image_transfer(self):
        data = b"old map cached before CDN metadata support"
        path = self.repo._path(self.entry.url, ".image")
        path.write_bytes(data)
        with patch("mnmparse.maps._download", side_effect=URLError("wiki blocked")) as download, \
             patch("mnmparse.maps._head_headers", return_value={
                 "x-object-meta-sha1base36": base36_sha1(data),
             }) as headers:
            hashes = self.repo.image_hashes([self.entry])
            self.assertEqual(hashes, {self.entry.url: hashlib.sha1(data).hexdigest()})
            self.assertFalse(self.repo.update_image(self.entry, sha1=hashes[self.entry.url]))
        headers.assert_called_once_with(self.entry.url)
        download.assert_called_once()
        self.assertEqual(path.read_bytes(), data)

    def test_cdn_detects_replacement_at_same_url_and_keeps_hash_verification(self):
        path = self.repo._path(self.entry.url, ".image")
        path.write_bytes(b"old map")
        new = b"replacement map"
        with patch("mnmparse.maps._download", side_effect=URLError("wiki blocked")), \
             patch("mnmparse.maps._head_headers", return_value={
                 "x-object-meta-sha1base36": base36_sha1(new),
             }):
            hashes = self.repo.image_hashes([self.entry])
        with patch("mnmparse.maps._download", return_value=b"stale CDN content"):
            with self.assertRaisesRegex(ValueError, "current version"):
                self.repo.update_image(self.entry, sha1=hashes[self.entry.url])
        self.assertEqual(path.read_bytes(), b"old map")
        with patch("mnmparse.maps._download", return_value=new):
            self.assertTrue(self.repo.update_image(self.entry, sha1=hashes[self.entry.url]))
        self.assertEqual(path.read_bytes(), new)

    def test_one_bad_cdn_head_does_not_discard_other_current_file_hashes(self):
        second = MapImage("Second", self.entry.url.replace(".png", "_second.png"), self.entry.source)
        good_hash = hashlib.sha1(b"good map").hexdigest()
        for failure in (URLError("one unavailable map"), ValueError("Unexpected map redirect")):
            repo = MapRepository(Path(self.temp.name))

            def headers(url):
                if url == self.entry.url:
                    raise failure
                return {"x-object-meta-sha1base36": base36_sha1(b"good map")}

            with self.subTest(failure=failure), \
                 patch("mnmparse.maps._download", side_effect=URLError("wiki blocked")), \
                 patch("mnmparse.maps._head_headers", side_effect=headers), \
                 self.assertLogs("mnmparse.maps", level="WARNING"):
                self.assertEqual(repo.image_hashes([self.entry, second]), {second.url: good_hash})

    def test_cdn_checks_use_at_most_four_daemon_workers(self):
        entries = [MapImage(str(index), self.entry.url.replace(".png", f"_{index}.png"), self.entry.source)
                   for index in range(9)]
        four_started = Event()
        release = Event()
        guard = Lock()
        active = 0
        maximum = 0
        threads = set()
        results = []

        def headers(_url):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
                threads.add(current_thread())
                if active == 4:
                    four_started.set()
            release.wait(2)
            with guard:
                active -= 1
            return {"x-object-meta-sha1base36": base36_sha1(b"map")}

        caller = Thread(target=lambda: results.append(self.repo.image_hashes(entries)), daemon=True)
        with patch("mnmparse.maps._download", side_effect=URLError("wiki blocked")), \
             patch("mnmparse.maps._head_headers", side_effect=headers):
            try:
                caller.start()
                self.assertTrue(four_started.wait(1), "CDN checks did not run concurrently")
                with guard:
                    self.assertEqual(active, 4)
                    self.assertTrue(all(thread.daemon for thread in threads))
                release.set()
                caller.join(2)
                self.assertFalse(caller.is_alive())
            finally:
                release.set()
                caller.join(2)
        self.assertEqual(maximum, 4)
        self.assertEqual(results, [{entry.url: hashlib.sha1(b"map").hexdigest() for entry in entries}])

    def test_cancellation_returns_collected_hashes_without_waiting_for_active_heads(self):
        entries = [MapImage(str(index), self.entry.url.replace(".png", f"_{index}.png"), self.entry.source)
                   for index in range(6)]
        cancelled = Event()
        four_blocked = Event()
        release = Event()
        guard = Lock()
        threads = set()
        calls = []
        results = []

        def headers(url):
            with guard:
                calls.append(url)
                threads.add(current_thread())
                if len(calls) == 5:
                    four_blocked.set()
            if url != entries[0].url:
                release.wait(2)
            return {"x-object-meta-sha1base36": base36_sha1(b"map")}

        caller = Thread(target=lambda: results.append(self.repo.image_hashes(entries, cancelled=cancelled)),
                        daemon=True)
        with patch("mnmparse.maps._download", side_effect=URLError("wiki blocked")), \
             patch("mnmparse.maps._head_headers", side_effect=headers):
            try:
                caller.start()
                self.assertTrue(four_blocked.wait(1))
                cancelled.set()
                caller.join(0.5)
                self.assertFalse(caller.is_alive(), "Cancellation waited for active HEAD requests")
                self.assertEqual(results, [{entries[0].url: hashlib.sha1(b"map").hexdigest()}])
            finally:
                cancelled.set()
                release.set()
                caller.join(2)
                for worker in threads:
                    worker.join(2)
        # Queued files never started, and late completed HEADs do not change the
        # snapshot already returned to a cancelled download job.
        self.assertEqual(len(calls), 5)
        self.assertEqual(results, [{entries[0].url: hashlib.sha1(b"map").hexdigest()}])

    def test_pre_cancelled_hash_check_makes_no_requests(self):
        cancelled = Event()
        cancelled.set()
        with patch("mnmparse.maps._download") as download, \
             patch("mnmparse.maps._head_headers") as headers:
            self.assertEqual(self.repo.image_hashes([self.entry], cancelled=cancelled), {})
        download.assert_not_called()
        headers.assert_not_called()

    def test_invalid_api_data_does_not_trigger_cdn_success(self):
        for payload in (b"not json", b"{}", b'{"error":{"info":"bad request"}}'):
            with self.subTest(payload=payload), \
                 patch("mnmparse.maps._download", return_value=payload), \
                 patch("mnmparse.maps._head_headers") as headers:
                with self.assertRaises((ValueError, KeyError)):
                    self.repo.image_hashes([self.entry])
                headers.assert_not_called()

    def test_cdn_hash_is_bounded_and_missing_hash_never_validates_a_cache(self):
        for encoded in ("", "!bad", "z" * 31, "1" * 32, "-1", None):
            with self.subTest(encoded=encoded), \
                 patch("mnmparse.maps._head_headers", return_value={"x-object-meta-sha1base36": encoded}):
                self.assertIsNone(_cdn_sha1(self.entry.url))
        with patch("mnmparse.maps._head_headers", return_value={"x-object-meta-sha1base36": "1"}):
            self.assertEqual(_cdn_sha1(self.entry.url), "0" * 39 + "1")
        with patch("mnmparse.maps._head_headers") as headers:
            self.assertIsNone(_cdn_sha1(WIKI + "/wiki/Ancient_Crypt"))
        headers.assert_not_called()

    def test_api_cooldown_expires_and_does_not_hide_a_successful_retry(self):
        for status in (403, 429, 500, 503):
            repo = MapRepository(Path(self.temp.name))
            error = HTTPError(WIKI, status, "Unavailable", {}, None)
            self.addCleanup(error.close)
            with self.subTest(status=status), \
                 patch("mnmparse.maps.monotonic", return_value=100.0) as now, \
                 patch("mnmparse.maps._download", side_effect=[error, b"recovered"]) as download:
                with self.assertRaises(HTTPError):
                    repo._wiki_download(WIKI)
                with self.assertRaises(URLError):
                    repo._wiki_download(WIKI)
                self.assertEqual(download.call_count, 1)
                now.return_value = 100.0 + API_COOLDOWN
                self.assertEqual(repo._wiki_download(WIKI), b"recovered")
                self.assertEqual(download.call_count, 2)

    def test_missing_page_http_error_does_not_block_other_zone_requests(self):
        error = HTTPError(WIKI, 404, "Missing", {}, None)
        self.addCleanup(error.close)
        with patch("mnmparse.maps._download", side_effect=[error, b"next zone"]):
            with self.assertRaises(HTTPError):
                self.repo._wiki_download(WIKI)
            self.assertEqual(self.repo._wiki_download(WIKI), b"next zone")


class MapHeaderGuardTests(unittest.TestCase):
    def setUp(self):
        self.url = MAP_CATALOG["Ancient Crypt"][0][1]
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.response.geturl.return_value = self.url
        self.response.headers = {"Content-Length": "41735458", "X-Object-Meta-Sha1base36": "1"}

    def test_head_does_not_read_image_body(self):
        with patch("mnmparse.maps.urlopen", return_value=self.response) as open_url:
            self.assertEqual(_head_headers(self.url)["x-object-meta-sha1base36"], "1")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "HEAD")
        self.assertIn("https://github.com/Maergoth/pnut-mnm", request.get_header("User-agent"))
        self.response.read.assert_not_called()

    def test_external_redirect_and_oversized_file_are_rejected(self):
        with patch("mnmparse.maps.urlopen", return_value=self.response):
            self.response.geturl.return_value = "https://example.com/map.png"
            with self.assertRaisesRegex(ValueError, "redirect"):
                _head_headers(self.url)
            self.response.geturl.return_value = self.url
            self.response.headers = {"Content-Length": str(MAX_DOWNLOAD + 1)}
            with self.assertRaisesRegex(ValueError, "download limit"):
                _head_headers(self.url)
        self.response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
