"""Resizable, independently toggleable zone-map window."""
from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, QRect, QSettings, Qt, Signal
from PySide6.QtGui import QGuiApplication, QImage, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

from mnmparse.app import APP_NAME
from mnmparse.config import project_path
from mnmparse.maps import MapImage, MapRepository, ZONES, wiki_url, zone_title

log = logging.getLogger(__name__)


class _Result(QObject):
    ready = Signal(int, object, str)


class _Load:
    def __init__(self, token: int, repo: MapRepository, zone: str, entry: MapImage | None,
                 refresh: bool, slots: threading.Semaphore) -> None:
        self.signal = _Result()
        self.token, self.repo, self.zone, self.entry, self.refresh = token, repo, zone, entry, refresh
        self.cancelled = threading.Event()
        self.slots = slots

    def start(self) -> None:
        # A stalled public wiki must never hold the application open on exit.
        # Workers own no widgets, and cancellation prevents any later result delivery.
        threading.Thread(target=self.run, name="zone-map", daemon=True).start()

    def run(self) -> None:
        while not self.cancelled.is_set():
            if self.slots.acquire(timeout=0.1):
                break
        else:
            return
        maps = None
        data = b""
        error = ""
        try:
            if self.cancelled.is_set():
                return
            maps = self.repo.maps(self.zone, refresh=self.refresh) if self.entry is None else [self.entry]
            if self.cancelled.is_set():
                return
            data = self.repo.image(maps[0], refresh=self.refresh) if maps else b""
        except Exception as exc:
            log.info("Map load for %s: %s", self.zone, exc)
            error = str(exc)
        finally:
            self.slots.release()
        if not self.cancelled.is_set():
            try:
                result = (maps, data, self.entry is None) if maps is not None else None
                self.signal.ready.emit(self.token, result, error)
            except RuntimeError:
                pass  # Qt was torn down while a request was finishing.


class MapView(QGraphicsView):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(Qt.GlobalColor.black)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self._fit = True

    def set_image(self, image: QImage) -> None:
        self.scene().clear()
        if not image.isNull():
            item = self.scene().addPixmap(QPixmap.fromImage(image))
            self.scene().setSceneRect(item.boundingRect())
        else:
            self.scene().setSceneRect(0, 0, 1, 1)
        self.fit()

    def fit(self) -> None:
        self._fit = True
        if self.scene().items():
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom(self, factor: float) -> None:
        if not self.scene().items():
            return
        scale = self.transform().m11() * factor
        if 0.015 <= scale <= 12:
            self._fit = False
            self.scale(factor, factor)

    def wheelEvent(self, event: Any) -> None:  # noqa: N802
        self.zoom(1.2 if event.angleDelta().y() > 0 else 1 / 1.2)
        event.accept()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._fit:
            self.fit()


class MapOverlay(QWidget):
    visibility_changed = Signal(bool)

    def __init__(self, settings: QSettings, repository: MapRepository | None = None) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setWindowTitle(f"{APP_NAME} — Map")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.settings = settings
        self.repo = repository or MapRepository(project_path("map_cache"))
        self._slots = threading.Semaphore(2)
        self._token = 0
        self._loads: dict[int, _Load] = {}
        self._entries: list[MapImage] = []
        self._loaded_zone = ""
        self._normal_geometry = QRect()
        self._closing = False
        self._current_zone = str(settings.value("map/zone", "") or "")
        self.setMinimumSize(460, 320)
        self.resize(900, 650)
        self.setStyleSheet("QWidget { background: #161c25; color: #e9edf5; } "
                           "QPushButton, QComboBox { padding: 5px 8px; } QGraphicsView { border: none; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.addItem("Choose a zone…", "")
        self.zone.addItems(ZONES)
        self.zone.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.zone.setMinimumWidth(150)
        self.zone.setToolTip("Select a map manually, or let the Combat chat's zone-entry line choose it")
        if self._current_zone:
            self.zone.setCurrentText(self._current_zone)
        self.zone.textActivated.connect(self.set_zone)
        self.zone.lineEdit().returnPressed.connect(lambda: self.set_zone(self.zone.currentText()))
        top.addWidget(self.zone, 1)
        refresh = QPushButton("Refresh")
        refresh.setToolTip("Check the wiki for updated maps of this zone")
        refresh.clicked.connect(lambda: self.load(refresh=True))
        top.addWidget(refresh)
        self.fullscreen = QPushButton("Fullscreen")
        self.fullscreen.setToolTip("Toggle fullscreen (F11); Escape returns to the window")
        self.fullscreen.clicked.connect(self.toggle_fullscreen)
        top.addWidget(self.fullscreen)
        layout.addLayout(top)
        toolbar = QHBoxLayout()
        self.variants = QComboBox()
        self.variants.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.variants.setMinimumWidth(100)
        self.variants.setToolTip("Choose a map or floor when this zone has several")
        self.variants.activated.connect(self._select_map)
        toolbar.addWidget(self.variants, 1)
        self.view = MapView()
        for text, handler in (("−", lambda: self.view.zoom(1 / 1.2)), ("+", lambda: self.view.zoom(1.2)), ("Fit", self.view.fit)):
            button = QPushButton(text)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)
        self.status = QLabel("Waiting for a zone-entry message, or choose a zone above.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(self.view, 1)
        self.credit = QLabel()
        self.credit.setWordWrap(True)
        self.credit.setOpenExternalLinks(True)
        self.credit.setText('<a href="' + wiki_url("Category:Zones") + '">Maps: Monsters &amp; Memories Wiki contributors</a> · Scroll to zoom; drag to pan.')
        layout.addWidget(self.credit)
        QShortcut(QKeySequence("F11"), self).activated.connect(self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.exit_fullscreen)
        rect = settings.value("map/geometry")
        if isinstance(rect, QRect) and rect.isValid():
            screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
            if any(area.contains(rect.center()) for area in screens):
                self.setGeometry(rect)

    @property
    def current_zone(self) -> str:
        return self._current_zone

    def on_message(self, _msg: Any, event: Any) -> None:
        if event is not None and getattr(event, "kind", "") == "zone" and getattr(event, "target", ""):
            self.set_zone(event.target)

    def set_zone(self, zone: str) -> None:
        title = zone_title(zone)
        if not title or title == "Choose a zone…":
            return
        if title == self._current_zone and (self._loaded_zone == title or self._token in self._loads):
            return
        self._current_zone = title
        self.settings.setValue("map/zone", title)
        self.zone.setCurrentText(title)
        self.setWindowTitle(f"{APP_NAME} — {title}")
        # Clear immediately so a late response cannot show the previous zone's map.
        self._token += 1
        self._cancel_loads()
        self._loaded_zone = ""
        self._entries.clear()
        self.variants.clear()
        self.view.set_image(QImage())
        self.status.setText(f"{title} — open the map to load it.")
        if self.isVisible():
            self.load()

    def load(self, *, refresh: bool = False, entry: MapImage | None = None) -> None:
        if not self._current_zone or self._closing:
            return
        self._token += 1
        self._cancel_loads()
        self.status.setText(f"Loading {self._current_zone}…")
        self.view.set_image(QImage())
        worker = _Load(self._token, self.repo, self._current_zone, entry, refresh, self._slots)
        worker.signal.ready.connect(self._ready)
        self._loads[self._token] = worker
        worker.start()

    def _cancel_loads(self) -> None:
        for worker in self._loads.values():
            worker.cancelled.set()
        self._loads.clear()

    def _ready(self, token: int, result: Any, error: str) -> None:
        self._loads.pop(token, None)
        if token != self._token or self._closing:
            return
        if result is not None:
            maps, data, populate = result
            if populate:
                self._entries = maps
                self.variants.clear()
                self.variants.addItems([m.title for m in maps])
                self.variants.setEnabled(len(maps) > 1)
        if error:
            self.status.setText(f"Map unavailable for {self._current_zone}. Check your connection and click Refresh.")
            self.status.setToolTip(error)
            return
        self.status.setToolTip("")
        if not maps:
            self.status.setText(f"No map is published for {self._current_zone} yet. Try the wiki link below.")
            self.credit.setText(f'<a href="{wiki_url(self._current_zone)}">Open this zone on the Monsters &amp; Memories Wiki</a>')
            self._loaded_zone = self._current_zone
            return
        image = QImage.fromData(data)
        if image.isNull():
            self.status.setText("The map image could not be read. Click Refresh to retry.")
            # An incomplete cached response should not poison later retries.
            try:
                self.repo._path(maps[0].url, ".image").unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.view.set_image(image)
        self._loaded_zone = self._current_zone
        self.status.setText(self._current_zone + " · Scroll to zoom; drag to pan.")
        self.credit.setText(f'<a href="{maps[0].source}">Map source &amp; credits: Monsters &amp; Memories Wiki</a>')

    def _select_map(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self.load(entry=self._entries[index])

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._normal_geometry = self.geometry()
            self.fullscreen.setText("Windowed")
            self.showFullScreen()

    def exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry.isValid():
                self.setGeometry(self._normal_geometry)
            self.fullscreen.setText("Fullscreen")

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self.visibility_changed.emit(True)
        if self._loaded_zone != self._current_zone and self._token not in self._loads:
            self.load()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self.save_state()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def save_state(self) -> None:
        rect = self._normal_geometry if self.isFullScreen() else self.geometry()
        if rect.isValid():
            self.settings.setValue("map/geometry", rect)

    def shutdown(self) -> None:
        self._closing = True
        self._token += 1
        self._cancel_loads()
        self.save_state()
        self.hide()
