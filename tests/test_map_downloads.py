"""Bulk map refreshes stay off the GUI thread and report stale-cache failures."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app.map_downloads import MapDownloadController, _download_maps
from mnmparse.maps import MapImage, MapRepository

MAP = MapImage("First floor", "https://static.wikitide.net/example/first.png",
               "https://monstersandmemories.miraheze.org/wiki/File:First.png")
SECOND_MAP = MapImage("Second floor", "https://static.wikitide.net/example/second.png",
                      "https://monstersandmemories.miraheze.org/wiki/File:Second.png")


class MapRefreshTests(unittest.TestCase):
    def test_strict_refresh_reports_failure_and_preserves_offline_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = MapRepository(Path(temporary))
            repo._save(repo._path("Sungreet Strand", ".json"), json.dumps([asdict(MAP)]).encode())
            repo._save(repo._path(MAP.url, ".image"), b"cached image")
            with patch("mnmparse.maps._download", side_effect=OSError("offline")):
                with self.assertRaisesRegex(OSError, "offline"):
                    repo.maps("Sungreet Strand", refresh=True, strict=True)
                with self.assertRaisesRegex(OSError, "offline"):
                    repo.image(MAP, refresh=True, strict=True)
                self.assertEqual(repo.maps("Sungreet Strand"), [MAP])
                self.assertEqual(repo.image(MAP), b"cached image")

    def test_strict_refresh_reports_disk_failure_and_keeps_previous_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = MapRepository(Path(temporary))
            repo._save(repo._path(MAP.url, ".image"), b"old")
            with patch("mnmparse.maps._download", return_value=b"new"), \
                    patch("mnmparse.maps.os.replace", side_effect=PermissionError("read only")):
                with self.assertRaisesRegex(PermissionError, "read only"), self.assertLogs("mnmparse.maps"):
                    repo.image(MAP, refresh=True, strict=True)
            self.assertEqual(repo.image(MAP), b"old")
            self.assertEqual(len(list(Path(temporary).iterdir())), 1)

    def test_all_zones_and_variants_refresh_but_shared_images_download_once(self):
        repo = Mock()
        repo.maps.side_effect = [[MAP, SECOND_MAP], [MAP], []]
        progress = []
        result = _download_maps(repo, ["First", "Second", "Mapless"], threading.Event(), progress.append)
        self.assertEqual([call.args[0] for call in repo.maps.call_args_list], ["First", "Second", "Mapless"])
        for call in repo.maps.call_args_list:
            self.assertEqual(call.kwargs, {"refresh": True, "strict": True, "persist": False})
        for call in repo.image.call_args_list:
            self.assertEqual(call.kwargs, {"refresh": True, "strict": True})
        self.assertEqual([call.args[0] for call in repo.image.call_args_list], [MAP, SECOND_MAP])
        self.assertEqual([call.args for call in repo.save_maps.call_args_list],
                         [("First", [MAP, SECOND_MAP]), ("Second", [MAP]), ("Mapless", [])])
        self.assertEqual(result, "Downloaded 2 maps.")
        self.assertIn("Mapless (3/3)", progress[-1])

    def test_page_and_image_failures_do_not_stop_remaining_downloads(self):
        repo = Mock()
        repo.maps.side_effect = [OSError("offline page"), [MAP, SECOND_MAP]]
        repo.image.side_effect = [OSError("offline image"), b"new image"]
        result = _download_maps(repo, ["First", "Second"], threading.Event(), lambda _: None)
        self.assertIn("Downloaded 1 map.", result)
        self.assertIn("Could not update 1 zone and 1 image", result)
        self.assertIn("Existing cached maps are still available", result)
        self.assertEqual(repo.image.call_count, 2)
        repo.save_maps.assert_not_called()

    def test_changed_image_url_failure_retains_previous_offline_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = MapRepository(Path(temporary))
            repo.save_maps("Sungreet Strand", [MAP])
            repo._save(repo._path(MAP.url, ".image"), b"old image")
            html = f'<figure class="mw-image-border"><img src="{SECOND_MAP.url}"></figure>'
            payload = json.dumps({"parse": {"text": {"*": html}}}).encode()
            with patch("mnmparse.maps._download", side_effect=[payload, OSError("new image unavailable")]):
                result = _download_maps(repo, ["Sungreet Strand"], threading.Event(), lambda _: None)
            self.assertIn("Could not update 1 image", result)
            with patch("mnmparse.maps._download", side_effect=OSError("offline")) as download:
                entries = repo.maps("Sungreet Strand")
                self.assertEqual(entries, [MAP])
                self.assertEqual(repo.image(entries[0]), b"old image")
                download.assert_not_called()

    def test_shared_failed_image_prevents_later_zone_manifest_commit(self):
        repo = Mock()
        repo.maps.side_effect = [[MAP], [MAP, SECOND_MAP]]
        repo.image.side_effect = [OSError("shared image unavailable"), b"new image"]
        result = _download_maps(repo, ["First", "Second"], threading.Event(), lambda _: None)
        self.assertIn("Downloaded 1 map.", result)
        self.assertIn("Could not update 1 image", result)
        self.assertEqual([call.args[0] for call in repo.image.call_args_list], [MAP, SECOND_MAP])
        repo.save_maps.assert_not_called()

    def test_successful_empty_download_discards_previous_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = MapRepository(Path(temporary))
            repo.save_maps("Sungreet Strand", [MAP])
            payload = json.dumps({"parse": {"text": {"*": "No map yet"}}}).encode()
            with patch("mnmparse.maps._download", return_value=payload):
                result = _download_maps(repo, ["Sungreet Strand"], threading.Event(), lambda _: None)
            self.assertEqual(result, "Downloaded 0 maps.")
            self.assertFalse(repo._path("Sungreet Strand", ".json").exists())

    def test_cancellation_between_page_and_image_stops_remaining_requests(self):
        cancelled = threading.Event()
        repo = Mock()
        def maps(*_args, **_kwargs):
            cancelled.set()
            return [MAP]
        repo.maps.side_effect = maps
        self.assertEqual(_download_maps(repo, ["First", "Second"], cancelled, lambda _: None),
                         "Map download cancelled.")
        repo.maps.assert_called_once()
        repo.image.assert_not_called()
        repo.save_maps.assert_not_called()


class MapDownloadControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.assertTrue(predicate(), "Background map download did not complete")

    def test_start_is_nonblocking_idempotent_and_delivers_completion_on_gui_thread(self):
        entered, release = threading.Event(), threading.Event()
        worker_threads = []
        repo = Mock()
        def maps(*_args, **_kwargs):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release worker")
            return [MAP]
        repo.maps.side_effect = maps
        controller = MapDownloadController(repo)
        started, progress, finished = [], [], []
        controller.started.connect(lambda: started.append(threading.get_ident()))
        controller.progress.connect(lambda text: progress.append((text, threading.get_ident())))
        controller.finished.connect(lambda text: finished.append((text, threading.get_ident())))
        main_thread = threading.get_ident()
        try:
            with patch("mnmparse.app.map_downloads.ZONES", ("Sungreet Strand",)):
                self.assertTrue(controller.start())
                self.assertTrue(entered.wait(1))
                self.assertTrue(controller.is_running)
                self.assertFalse(controller.start())
                release.set()
                self.wait_until(lambda: bool(finished))
                self.assertFalse(controller.is_running)
                self.assertEqual(started, [main_thread])
                self.assertEqual(progress[0][1], main_thread)
                self.assertEqual(finished, [("Downloaded 1 map.", main_thread)])
                self.assertNotEqual(worker_threads, [main_thread])
                self.assertTrue(controller.start())
                self.wait_until(lambda: len(finished) == 2)
        finally:
            release.set()
            controller.shutdown()

    def test_shutdown_cancels_pending_images_and_suppresses_late_signals(self):
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        repo = Mock()
        def maps(*_args, **_kwargs):
            entered.set()
            release.wait(2)
            returned.set()
            return [MAP]
        repo.maps.side_effect = maps
        controller = MapDownloadController(repo)
        finished = []
        controller.finished.connect(finished.append)
        try:
            with patch("mnmparse.app.map_downloads.ZONES", ("Sungreet Strand",)):
                self.assertTrue(controller.start())
                self.assertTrue(entered.wait(1))
                worker = controller._worker
                controller.shutdown()
                self.assertFalse(controller.is_running)
                self.assertFalse(controller.start())
                self.assertTrue(worker.cancelled.is_set())
                release.set()
                self.assertTrue(returned.wait(1))
                self.app.processEvents()
                repo.image.assert_not_called()
                self.assertEqual(finished, [])
        finally:
            release.set()
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
