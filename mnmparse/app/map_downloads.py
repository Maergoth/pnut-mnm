"""Background downloads for the Settings map update button and startup option."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable

from PySide6.QtCore import QObject, Signal, Slot

from mnmparse.maps import MapRepository, ZONES

log = logging.getLogger(__name__)


def _download_maps(repository: MapRepository, zones: Iterable[str],
                   cancelled: threading.Event, progress: Callable[[str], None]) -> str:
    """Refresh every map variant, preserving old cache files when requests fail."""
    zones = tuple(zones)
    downloaded = page_failures = image_failures = 0
    image_results: dict[str, bool] = {}
    for index, zone in enumerate(zones, 1):
        if cancelled.is_set():
            return "Map download cancelled."
        progress(f"Downloading maps: {zone} ({index}/{len(zones)})…")
        try:
            entries = repository.maps(zone, refresh=True, strict=True, persist=False)
        except Exception as exc:
            log.info("Map update for %s failed: %s", zone, exc)
            page_failures += 1
            continue
        zone_complete = True
        for entry in entries:
            if cancelled.is_set():
                return "Map download cancelled."
            if entry.url in image_results:
                zone_complete = zone_complete and image_results[entry.url]
                continue
            try:
                repository.image(entry, refresh=True, strict=True)
            except Exception as exc:
                log.info("Map image update for %s failed: %s", entry.title, exc)
                image_failures += 1
                image_results[entry.url] = False
                zone_complete = False
            else:
                downloaded += 1
                image_results[entry.url] = True
        if cancelled.is_set():
            return "Map download cancelled."
        if zone_complete:
            try:
                repository.save_maps(zone, entries, strict=True)
            except Exception as exc:
                log.info("Map list update for %s failed: %s", zone, exc)
                page_failures += 1
    if cancelled.is_set():
        return "Map download cancelled."
    result = f"Downloaded {downloaded} {'map' if downloaded == 1 else 'maps'}."
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
