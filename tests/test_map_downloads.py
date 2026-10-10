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


def repository_mock():
    repository = Mock()
    repository.image_hashes.return_value = {}
    repository.update_image.return_value = True
    return repository


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
        repo = repository_mock()
        pages = {"First": [MAP, SECOND_MAP], "Second": [MAP], "Mapless": []}
        repo.maps.side_effect = lambda zone, **_: pages[zone]
        progress = []
        result = _download_maps(repo, ["First", "Second", "Mapless"], threading.Event(), progress.append)
        self.assertCountEqual([call.args[0] for call in repo.maps.call_args_list], pages)
        for call in repo.maps.call_args_list:
            self.assertEqual(call.kwargs, {"refresh": True, "strict": True, "persist": False})
        repo.image_hashes.assert_called_once_with((MAP, SECOND_MAP))
        for call in repo.update_image.call_args_list:
            self.assertEqual(call.kwargs, {"sha1": None})
        self.assertCountEqual([call.args[0] for call in repo.update_image.call_args_list], [MAP, SECOND_MAP])
        self.assertEqual([call.args for call in repo.save_maps.call_args_list],
                         [("First", [MAP, SECOND_MAP]), ("Second", [MAP]), ("Mapless", [])])
        self.assertEqual(result, "Downloaded 2 maps.")
        self.assertTrue(any("Mapless (3/3)" in message for message in progress))
        self.assertTrue(any(message.startswith("Updating maps:") for message in progress))

    def test_matching_images_are_reported_unchanged_and_receive_remote_hashes(self):
        repo = repository_mock()
        repo.maps.return_value = [MAP, SECOND_MAP]
        repo.image_hashes.return_value = {MAP.url: "first hash", SECOND_MAP.url: "second hash"}
        repo.update_image.side_effect = lambda entry, **_: entry == SECOND_MAP
        result = _download_maps(repo, ["First"], threading.Event(), lambda _: None)
        self.assertEqual(result, "Downloaded 1 map. 1 map unchanged.")
        self.assertCountEqual([(call.args[0], call.kwargs["sha1"])
                               for call in repo.update_image.call_args_list],
                              [(MAP, "first hash"), (SECOND_MAP, "second hash")])
        repo.save_maps.assert_called_once_with("First", [MAP, SECOND_MAP], strict=True)

    def test_version_lookup_failure_falls_back_to_refreshing_images(self):
        repo = repository_mock()
        repo.maps.return_value = [MAP]
        repo.image_hashes.side_effect = OSError("metadata unavailable")
        result = _download_maps(repo, ["First"], threading.Event(), lambda _: None)
        self.assertEqual(result, "Downloaded 1 map.")
        repo.update_image.assert_called_once_with(MAP, sha1=None)
        repo.save_maps.assert_called_once_with("First", [MAP], strict=True)

    def test_page_and_image_failures_do_not_stop_remaining_downloads(self):
        repo = repository_mock()
        def maps(zone, **_):
            if zone == "First":
                raise OSError("offline page")
            return [MAP, SECOND_MAP]
        def update_image(entry, **_):
            if entry == MAP:
                raise OSError("offline image")
            return True
        repo.maps.side_effect = maps
        repo.update_image.side_effect = update_image
        result = _download_maps(repo, ["First", "Second"], threading.Event(), lambda _: None)
        self.assertIn("Downloaded 1 map.", result)
        self.assertIn("Could not update 1 zone and 1 image", result)
        self.assertIn("Existing cached maps are still available", result)
        self.assertEqual(repo.update_image.call_count, 2)
        repo.save_maps.assert_not_called()

    def test_changed_image_url_failure_retains_previous_offline_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = MapRepository(Path(temporary))
            repo.save_maps("Sungreet Strand", [MAP])
            repo._save(repo._path(MAP.url, ".image"), b"old image")
            html = f'<figure class="mw-image-border"><img src="{SECOND_MAP.url}"></figure>'
            payload = json.dumps({"parse": {"text": {"*": html}}}).encode()
            with patch("mnmparse.maps._download", side_effect=[payload, OSError("new image unavailable")]), \
                    patch.object(repo, "image_hashes", return_value={}):
                result = _download_maps(repo, ["Sungreet Strand"], threading.Event(), lambda _: None)
            self.assertIn("Could not update 1 image", result)
            with patch("mnmparse.maps._download", side_effect=OSError("offline")) as download:
                entries = repo.maps("Sungreet Strand")
                self.assertEqual(entries, [MAP])
                self.assertEqual(repo.image(entries[0]), b"old image")
                download.assert_not_called()

    def test_shared_failed_image_prevents_later_zone_manifest_commit(self):
        repo = repository_mock()
        repo.maps.side_effect = lambda zone, **_: [MAP] if zone == "First" else [MAP, SECOND_MAP]
        def update_image(entry, **_):
            if entry == MAP:
                raise OSError("shared image unavailable")
            return True
        repo.update_image.side_effect = update_image
        result = _download_maps(repo, ["First", "Second"], threading.Event(), lambda _: None)
        self.assertIn("Downloaded 1 map.", result)
        self.assertIn("Could not update 1 image", result)
        self.assertCountEqual([call.args[0] for call in repo.update_image.call_args_list], [MAP, SECOND_MAP])
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
        repo = repository_mock()
        def maps(*_args, **_kwargs):
            cancelled.set()
            return [MAP]
        repo.maps.side_effect = maps
        self.assertEqual(_download_maps(repo, ["First", "Second"], cancelled, lambda _: None),
                         "Map download cancelled.")
        repo.maps.assert_called_once()
        repo.image_hashes.assert_not_called()
        repo.update_image.assert_not_called()
        repo.save_maps.assert_not_called()

    def test_page_and_image_requests_run_concurrently_with_four_daemon_workers(self):
        repo = repository_mock()
        zones = tuple(f"Zone {index}" for index in range(9))
        images = {zone: MapImage(zone, f"https://static.wikitide.net/example/{index}.png", MAP.source)
                  for index, zone in enumerate(zones)}
        entered = {phase: threading.Event() for phase in ("pages", "images")}
        release = {phase: threading.Event() for phase in entered}
        active = dict.fromkeys(entered, 0)
        peak = dict.fromkeys(entered, 0)
        daemon_workers = []
        lock = threading.Lock()

        def block(phase):
            with lock:
                active[phase] += 1
                peak[phase] = max(peak[phase], active[phase])
                daemon_workers.append(threading.current_thread().daemon)
                if active[phase] == 4:
                    entered[phase].set()
            try:
                if not release[phase].wait(3):
                    raise TimeoutError("test did not release requests")
            finally:
                with lock:
                    active[phase] -= 1

        def maps(zone, **_):
            block("pages")
            return [images[zone]]

        def update_image(_entry, **_):
            block("images")
            return True

        repo.maps.side_effect = maps
        repo.update_image.side_effect = update_image
        results = []
        worker = threading.Thread(target=lambda: results.append(
            _download_maps(repo, zones, threading.Event(), lambda _: None)), daemon=True)
        worker.start()
        try:
            self.assertTrue(entered["pages"].wait(1), "Zone checks did not overlap")
            self.assertEqual(repo.maps.call_count, 4)
            repo.image_hashes.assert_not_called()
            release["pages"].set()
            self.assertTrue(entered["images"].wait(1), "Image updates did not overlap")
            self.assertEqual(repo.maps.call_count, len(zones))
            self.assertEqual(repo.update_image.call_count, 4)
            repo.save_maps.assert_not_called()
            release["images"].set()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(results, ["Downloaded 9 maps."])
            self.assertEqual(peak, {"pages": 4, "images": 4})
            self.assertTrue(all(daemon_workers))
            self.assertEqual(repo.save_maps.call_count, len(zones))
        finally:
            for event in release.values():
                event.set()
            worker.join(timeout=3)

    def test_cancellation_skips_queued_page_and_image_requests_and_manifest_commits(self):
        for phase in ("pages", "images"):
            with self.subTest(phase=phase):
                repo = repository_mock()
                zones = tuple(f"Zone {index}" for index in range(9))
                images = {zone: MapImage(zone, f"https://static.wikitide.net/example/{index}.png", MAP.source)
                          for index, zone in enumerate(zones)}
                entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
                lock = threading.Lock()
                active = 0

                def block():
                    nonlocal active
                    with lock:
                        active += 1
                        if active == 4:
                            entered.set()
                    if not release.wait(3):
                        raise TimeoutError("test did not release requests")

                def maps(zone, **_):
                    if phase == "pages":
                        block()
                    return [images[zone]]

                def update_image(_entry, **_):
                    block()
                    return True

                repo.maps.side_effect = maps
                repo.update_image.side_effect = update_image
                results = []
                worker = threading.Thread(target=lambda: results.append(
                    _download_maps(repo, zones, cancelled, lambda _: None)), daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    cancelled.set()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive(), "Cancellation waited for stalled requests")
                    self.assertEqual(results, ["Map download cancelled."])
                    release.set()
                    self.assertEqual(repo.maps.call_count, 4 if phase == "pages" else len(zones))
                    self.assertEqual(repo.update_image.call_count, 0 if phase == "pages" else 4)
                    repo.save_maps.assert_not_called()
                finally:
                    cancelled.set()
                    release.set()
                    worker.join(timeout=3)


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
        repo = repository_mock()
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
        repo = repository_mock()
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
                repo.update_image.assert_not_called()
                self.assertEqual(finished, [])
        finally:
            release.set()
            controller.shutdown()


if __name__ == "__main__":
    unittest.main()
