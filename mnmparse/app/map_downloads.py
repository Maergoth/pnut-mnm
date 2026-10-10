"""Background downloads for the Settings map update button and startup option."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from queue import Empty, Queue
from typing import TypeVar

from PySide6.QtCore import QObject, Signal, Slot

from mnmparse.maps import MapImage, MapRepository, ZONES

log = logging.getLogger(__name__)
MAX_MAP_WORKERS = 4
_Item = TypeVar("_Item")
_Result = TypeVar("_Result")


def _parallel_map(items: Sequence[_Item], cancelled: threading.Event,
                  operation: Callable[[_Item], _Result]) -> list[_Result | Exception | None]:
    """Bound requests without making stalled requests hold application exit open."""
    pending: Queue[tuple[int, _Item]] = Queue()
    for index, item in enumerate(items):
        pending.put((index, item))
    results: list[_Result | Exception | None] = [None] * len(items)

    def run() -> None:
        while not cancelled.is_set():
            try:
                index, item = pending.get_nowait()
            except Empty:
                return
            if cancelled.is_set():
                return
            try:
                results[index] = operation(item)
            except Exception as exc:
                results[index] = exc

    workers = [threading.Thread(target=run, name="map-request", daemon=True)
               for _ in range(min(MAX_MAP_WORKERS, len(items)))]
    for worker in workers:
        worker.start()
    for worker in workers:
        while worker.is_alive():
            worker.join(timeout=0.1)
            if cancelled.is_set():
                return results
    return results


def _download_maps(repository: MapRepository, zones: Iterable[str],
                   cancelled: threading.Event, progress: Callable[[str], None]) -> str:
    """Check every variant, downloading changed images and retaining offline maps."""
    zones = tuple(zones)
    downloaded = unchanged = page_failures = image_failures = 0

    def load_zone(item: tuple[int, str]) -> list[MapImage]:
        index, zone = item
        progress(f"Checking maps: {zone} ({index}/{len(zones)})…")
        return repository.maps(zone, refresh=True, strict=True, persist=False)

    page_results = _parallel_map(tuple(enumerate(zones, 1)), cancelled, load_zone)
    if cancelled.is_set():
        return "Map download cancelled."
    staged: list[tuple[str, list[MapImage]]] = []
    unique_images: dict[str, MapImage] = {}
    for zone, entries in zip(zones, page_results):
        if isinstance(entries, Exception):
            log.info("Map update for %s failed: %s", zone, entries)
            page_failures += 1
            continue
        if entries is None:
            continue
        staged.append((zone, entries))
        for entry in entries:
            unique_images.setdefault(entry.url, entry)
    if cancelled.is_set():
        return "Map download cancelled."
    images = tuple(unique_images.values())
    hashes: dict[str, str] = {}
    if images:
        try:
            hashes = repository.image_hashes(images)
        except Exception as exc:
            log.info("Could not check map image versions; refreshing images: %s", exc)
    if cancelled.is_set():
        return "Map download cancelled."

    def update_image(item: tuple[int, MapImage]) -> bool:
        index, entry = item
        progress(f"Updating maps: {entry.title} ({index}/{len(images)})…")
        return repository.update_image(entry, sha1=hashes.get(entry.url))

    updates = _parallel_map(tuple(enumerate(images, 1)), cancelled, update_image)
    if cancelled.is_set():
        return "Map download cancelled."
    image_results: dict[str, bool] = {}
    for entry, updated in zip(images, updates):
        if isinstance(updated, Exception):
            log.info("Map image update for %s failed: %s", entry.title, updated)
            image_failures += 1
            image_results[entry.url] = False
        else:
            image_results[entry.url] = True
            if updated:
                downloaded += 1
            else:
                unchanged += 1
    for zone, entries in staged:
        if cancelled.is_set():
            return "Map download cancelled."
        if all(image_results.get(entry.url, False) for entry in entries):
            try:
                repository.save_maps(zone, entries, strict=True)
            except Exception as exc:
                log.info("Map list update for %s failed: %s", zone, exc)
                page_failures += 1
    if cancelled.is_set():
        return "Map download cancelled."
    result = f"Downloaded {downloaded} {'map' if downloaded == 1 else 'maps'}."
    if unchanged:
        result += f" {unchanged} {'map' if unchanged == 1 else 'maps'} unchanged."
    failures = []
    if page_failures:
        failures.append(f"{page_failures} {'zone' if page_failures == 1 else 'zones'}")
    if image_failures:
        failures.append(f"{image_failures} {'image' if image_failures == 1 else 'images'}")
    if failures:
        result += f" Could not update {' and '.join(failures)}. Existing cached maps are still available."
    return result


class _Signals(QObject):
    progress = Signal(str)
    finished = Signal(str)


class _DownloadWorker:
    def __init__(self, repository: MapRepository) -> None:
        self.repository = repository
        self.cancelled = threading.Event()
        self.signals = _Signals()

    def _emit(self, signal: Signal, message: str) -> None:
        if not self.cancelled.is_set():
            try:
                signal.emit(message)
            except RuntimeError:
                pass  # The Qt application may have exited during a request.

    def run(self) -> None:
        try:
            result = _download_maps(self.repository, ZONES, self.cancelled,
                                    lambda message: self._emit(self.signals.progress, message))
        except Exception:
            log.exception("Unexpected map download failure")
            result = "Could not finish downloading maps. Existing cached maps are still available."
        self._emit(self.signals.finished, result)


class MapDownloadController(QObject):
    """One cancellable download at a time; all public signals run on the GUI thread."""

    started = Signal()
    progress = Signal(str)
    finished = Signal(str)

    def __init__(self, repository: MapRepository, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self._worker: _DownloadWorker | None = None
        self._closed = False

    @property
    def is_running(self) -> bool:
        return self._worker is not None

    @Slot()
    def start(self) -> bool:
        if self._closed or self._worker is not None:
            return False
        worker = _DownloadWorker(self.repository)
        self._worker = worker
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        self.started.emit()
        # A stalled wiki request is bounded by the HTTP timeout and cannot delay
        # application exit. Cancellation is checked between every page/image.
        threading.Thread(target=worker.run, name="map-downloads", daemon=True).start()
        return True

    @Slot(str)
    def _on_progress(self, message: str) -> None:
        if not self._closed:
            self.progress.emit(message)

    @Slot(str)
    def _on_finished(self, message: str) -> None:
        self._worker = None
        if not self._closed:
            self.finished.emit(message)

    def shutdown(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancelled.set()
            self._worker = None
