"""Resizable, independently toggleable zone-map window."""
from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QIcon, QImage, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from mnmparse.app import APP_NAME
from mnmparse.config import project_path
from mnmparse.maps import MapImage, MapRepository, ZONES, zone_title

log = logging.getLogger(__name__)


class _Result(QObject):
    ready = Signal(int, object, str)


class _Load:
    def __init__(self, token: int, repo: MapRepository, zone: str, entry: MapImage | None,
                 refresh: bool, slots: threading.Semaphore, preferred_url: str = "") -> None:
        self.signal = _Result()
        self.token, self.repo, self.zone, self.entry, self.refresh = token, repo, zone, entry, refresh
        self.cancelled = threading.Event()
        self.slots = slots
        self.preferred_url = preferred_url

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
        selected_url = ""
        try:
            if self.cancelled.is_set():
                return
            maps = self.repo.maps(self.zone, refresh=self.refresh) if self.entry is None else [self.entry]
            if self.cancelled.is_set():
                return
            selected = next((m for m in maps if m.url == self.preferred_url), maps[0] if maps else None)
            selected_url = selected.url if selected else ""
            data = self.repo.image(selected, refresh=self.refresh) if selected else b""
        except Exception as exc:
            log.info("Map load for %s: %s", self.zone, exc)
            error = str(exc)
        finally:
            self.slots.release()
        if not self.cancelled.is_set():
            try:
                result = (maps, data, self.entry is None, selected_url) if maps is not None else None
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
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit = True

    def set_image(self, image: QImage, *, preserve_view: bool = False) -> None:
        previous = self.sceneRect()
        centre = self.mapToScene(self.viewport().rect().center())
        keep_zoom = preserve_view and not self._fit and not previous.isEmpty()
        self.scene().clear()
        if not image.isNull():
            item = self.scene().addPixmap(QPixmap.fromImage(image))
            self.scene().setSceneRect(item.boundingRect())
        else:
            self.scene().setSceneRect(0, 0, 1, 1)
        if keep_zoom:
            current = self.sceneRect()
            self.centerOn(centre.x() * current.width() / previous.width(),
                          centre.y() * current.height() / previous.height())
        else:
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
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                         Qt.WindowType.FramelessWindowHint)
        self.setWindowTitle(f"{APP_NAME} — Map")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.settings = settings
        self.repo = repository or MapRepository(project_path("map_cache"))
        self._slots = threading.Semaphore(2)
        self._token = 0
        self._loads: dict[int, _Load] = {}
        self._entries: list[MapImage] = []
        self._loaded_zone = ""
        self._selected_url = ""
        self._preserve_view_token = -1
        self._editing_zone = False
        self._normal_geometry = QRect()
        self._closing = False
        self._current_zone = str(settings.value("map/zone", "") or "")
        self.setMinimumSize(360, 240)
        self.resize(900, 650)
        self.setStyleSheet("QWidget { background: #161c25; color: #e9edf5; } "
                           "QComboBox { padding: 4px 6px; min-width: 0; } "
                           "QGraphicsView { border: none; } "
                           "QToolButton { border: none; border-radius: 4px; padding: 0; } "
                           "QToolButton:hover { background: #303a48; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.view = MapView(self)
        layout.addWidget(self.view)
        # Floating controls never change the map viewport when they appear/disappear.
        self.header = QWidget(self)
        top = QHBoxLayout(self.header)
        top.setContentsMargins(8, 8, 8, 8)
        top.setSpacing(6)
        self.zone = QComboBox()
        self.zone.setEditable(True)
        self.zone.addItem("Select zone", "")
        self.zone.addItems(ZONES)
        self.zone.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.zone.setMinimumWidth(0)
        self.zone.setAccessibleName("Select zone")
        self.zone.setToolTip("Select a map manually, or let the Combat chat's zone-entry line choose it")
        if self._current_zone:
            self.zone.setCurrentText(self._current_zone)
        self.zone.textActivated.connect(self.set_zone)
        self.zone.lineEdit().returnPressed.connect(lambda: self.set_zone(self.zone.currentText()))
        self.zone.lineEdit().textEdited.connect(self._begin_zone_edit)
        self.zone.lineEdit().editingFinished.connect(self._finish_zone_edit)
        top.addWidget(self.zone, 1)
        self.variants = QComboBox()
        self.variants.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.variants.setMinimumWidth(0)
        self.variants.setPlaceholderText("Select map")
        self.variants.setAccessibleName("Select map")
        self.variants.setToolTip("Choose a map or floor when this zone has several")
        self.variants.activated.connect(self._select_map)
        top.addWidget(self.variants, 1)
        self.fullscreen = QToolButton()
        self.fullscreen.setFixedSize(30, 30)
        self.fullscreen.setIconSize(QSize(20, 20))
        self.fullscreen.clicked.connect(self.toggle_fullscreen)
        self._update_fullscreen_icon()
        top.addWidget(self.fullscreen)
        self.status = QLabel("Select a zone, or enter one in game.", self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status.setStyleSheet("background: transparent; color: #c2cbd8; padding: 12px;")
        self._header_hide = QTimer(self)
        self._header_hide.setSingleShot(True)
        self._header_hide.setInterval(300)
        self._header_hide.timeout.connect(self._hide_header)
        for widget in (self, self.view.viewport(), self.header, self.zone,
                       self.zone.lineEdit(), self.variants, self.fullscreen):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        self.header.hide()
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
        if not title or title in ("Select zone", "Choose a zone…"):
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
        self._selected_url = ""
        self._entries.clear()
        self.variants.clear()
        self.view.set_image(QImage())
        self._set_status(f"{title} — open the map to load it.")
        if self.isVisible():
            self.load()

    def load(self, *, refresh: bool = False, entry: MapImage | None = None,
             preserve_view: bool = False) -> None:
        if not self._current_zone or self._closing:
            return
        self._token += 1
        self._cancel_loads()
        self._preserve_view_token = self._token if preserve_view else -1
        if not preserve_view or not self.view.scene().items():
            self._set_status(f"Loading {self._current_zone}…")
            self.view.set_image(QImage())
        worker = _Load(self._token, self.repo, self._current_zone, entry, refresh, self._slots,
                       self._selected_url if preserve_view else "")
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
        maps, data = [], b""
        selected_url = ""
        if result is not None:
            maps, data, populate = result[:3]
            selected_url = result[3] if len(result) > 3 else (maps[0].url if maps else "")
            if populate:
                self._entries = maps
                self.variants.clear()
                self.variants.addItems([m.title for m in maps])
                self.variants.setCurrentIndex(next((i for i, m in enumerate(maps) if m.url == selected_url), -1))
                self.variants.setEnabled(len(maps) > 1)
        if error:
            if token != self._preserve_view_token or not self.view.scene().items():
                self._set_status(f"Map unavailable for {self._current_zone}. Download maps in Settings to retry.")
            self.status.setToolTip(error)
            return
        self.status.setToolTip("")
        if not maps:
            self.view.set_image(QImage())
            self._set_status(f"No map is published for {self._current_zone} yet.")
            self._loaded_zone = self._current_zone
            return
        image = QImage.fromData(data)
        if image.isNull():
            self._set_status("The map image could not be read. Download maps in Settings to retry.")
            # An incomplete cached response should not poison later retries.
            try:
                self.repo._path(selected_url, ".image").unlink(missing_ok=True)
            except OSError:
                pass
            return
        self.view.set_image(image, preserve_view=(token == self._preserve_view_token
                                                  and selected_url == self._selected_url))
        self._selected_url = selected_url
        self._loaded_zone = self._current_zone
        self._set_status("")

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def reload_cached_map(self) -> None:
        """Display newly downloaded maps without opening a hidden overlay."""
        self._loaded_zone = ""
        if self.isVisible():
            self.load(preserve_view=True)

    def _begin_zone_edit(self, _text: str) -> None:
        self._editing_zone = True

    def _finish_zone_edit(self) -> None:
        self._editing_zone = False
        self._header_hide.start()

    def _update_fullscreen_icon(self) -> None:
        fullscreen = self.isFullScreen()
        # Four corner marks, reversed for the return-to-window action.
        pixmap = QPixmap(40, 40)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(Qt.GlobalColor.white, 3, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        for x, y, dx, dy in ((8, 8, 1, 1), (32, 8, -1, 1), (8, 32, 1, -1), (32, 32, -1, -1)):
            if fullscreen:
                x, y, dx, dy = x + dx * 8, y + dy * 8, -dx, -dy
            painter.drawLine(x, y, x + dx * 8, y)
            painter.drawLine(x, y, x, y + dy * 8)
        painter.end()
        pixmap.setDevicePixelRatio(2)
        self.fullscreen.setIcon(QIcon(pixmap))
        name = "Exit fullscreen" if fullscreen else "Fullscreen"
        self.fullscreen.setAccessibleName(name)
        self.fullscreen.setToolTip(name + " (F11; Esc to return)")

    def _show_header(self) -> None:
        self._header_hide.stop()
        self.header.show()
        self.header.raise_()

    def _hide_header(self) -> None:
        # A popup extends beyond the header; keep its controls until it closes.
        if self._editing_zone or any(combo.view().isVisible() for combo in (self.zone, self.variants)):
            self._header_hide.start()
        elif self.header.geometry().contains(self.mapFromGlobal(QCursor.pos())):
            return
        else:
            self.header.hide()

    def _resize_edges(self, point: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if not self.isFullScreen():
            if point.x() < 5:
                edges |= Qt.Edge.LeftEdge
            elif point.x() >= self.width() - 5:
                edges |= Qt.Edge.RightEdge
            if point.y() < 5:
                edges |= Qt.Edge.TopEdge
            elif point.y() >= self.height() - 5:
                edges |= Qt.Edge.BottomEdge
        return edges

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        kind = event.type()
        if kind in (QEvent.Type.Enter, QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress):
            point = self.mapFromGlobal(event.globalPosition().toPoint())
            if self.header.geometry().contains(point):
                self._show_header()
            elif not self._header_hide.isActive():
                self._header_hide.start()
            edges = self._resize_edges(point)
            if isinstance(watched, QWidget):
                if edges in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge):
                    watched.setCursor(Qt.CursorShape.SizeHorCursor)
                elif edges in (Qt.Edge.TopEdge, Qt.Edge.BottomEdge):
                    watched.setCursor(Qt.CursorShape.SizeVerCursor)
                elif edges in (Qt.Edge.TopEdge | Qt.Edge.LeftEdge, Qt.Edge.BottomEdge | Qt.Edge.RightEdge):
                    watched.setCursor(Qt.CursorShape.SizeFDiagCursor)
                elif edges:
                    watched.setCursor(Qt.CursorShape.SizeBDiagCursor)
                else:
                    watched.unsetCursor()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                handle = self.windowHandle()
                if handle is not None and edges:
                    return handle.startSystemResize(edges)
                if handle is not None and not self.isFullScreen() and (
                    watched is self.header or event.modifiers() & Qt.KeyboardModifier.AltModifier
                ):
                    return handle.startSystemMove()
        elif kind == QEvent.Type.Leave and not self._header_hide.isActive():
            self._header_hide.start()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.header.setGeometry(0, 0, self.width(), self.header.sizeHint().height())
        self.status.setGeometry(16, self.header.height(), self.width() - 32,
                                max(40, self.height() - self.header.height() * 2))

    def _select_map(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self.load(entry=self._entries[index])

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._normal_geometry = self.geometry()
            self.showFullScreen()
            self._update_fullscreen_icon()

    def exit_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry.isValid():
                self.setGeometry(self._normal_geometry)
            self._update_fullscreen_icon()

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
        self._header_hide.stop()
        self._token += 1
        self._cancel_loads()
        self.save_state()
        self.hide()
