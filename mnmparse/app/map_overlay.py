"""Resizable, independently toggleable zone-map window."""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Mapping, Sequence
from io import BytesIO
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QBuffer, QEvent, QIODevice, QObject, QPoint, QPointF, QRect, QRectF, QSettings, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QActionGroup, QCursor, QDesktopServices, QGuiApplication, QIcon, QImage, QImageReader, QKeySequence, QPainter, QPainterPath, QPen, QPixmap, QRegion, QShortcut
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QMenu, QMessageBox,
    QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from mnmparse.app import theme
from mnmparse.app.atlas_view import AtlasView
from mnmparse.app.map_labels import MapLabelDialog, MapLabelItem, label_icon
from mnmparse.app.monster_icon import monster_icon as _monster_icon
from mnmparse.app.notable_npc_menu import NotableNpcMenu, npc_title_key
from mnmparse.app.window_identity import window_title
from mnmparse.config import project_path
from mnmparse.map_labels import MapLabel, MapLabelStore, map_key
from mnmparse.maps import MapImage, MapRepository, ZONES, wiki_url, zone_title, zone_from_who

log = logging.getLogger(__name__)

# Ancient Crypt has 78 million pixels: its decoded image exceeds Qt's default
# 256 MiB reader limit even though its compressed download is only 40 MiB.
MAX_MAP_PIXELS = 100_000_000
_MAP_DECODE_LOCK = threading.Lock()


def _decode_map_image(data: bytes) -> QImage:
    """Decode bounded originals off-thread without changing Qt's global limit."""
    # Serialize decoding so rapid zone changes cannot expand several large
    # originals at once. Downloads may still use both worker slots.
    with _MAP_DECODE_LOCK:
        buffer = QBuffer()
        buffer.setData(data)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        size = reader.size()
        pixels = size.width() * size.height()
        if size.isEmpty():
            raise ValueError("The map image could not be read: " + reader.errorString())
        if pixels > MAX_MAP_PIXELS:
            raise ValueError("The map exceeds the supported image pixel limit")
        limit = QImageReader.allocationLimit() * 1024 * 1024
        if not limit or pixels * 4 <= limit:
            image = reader.read()
            if not image.isNull():
                return image

        # PNG readers need the original allocation even when scaled decoding
        # is requested. Pillow can decode this explicitly bounded image while
        # retaining full-resolution labels and alpha for zooming.
        from PIL import Image
        from PIL.ImageQt import toqimage

        with Image.open(BytesIO(data)) as original:
            if original.width * original.height > MAX_MAP_PIXELS:
                raise ValueError("The map exceeds the supported image pixel limit")
            if original.mode == "RGBA":
                return toqimage(original)
            with original.convert("RGBA") as rgba:
                # ImageQt owns its backing bytes after the Pillow image closes.
                return toqimage(rgba)


def _wiki_icon() -> QIcon:
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(theme.qcolor(theme.MUTED), 3, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    frame = QPainterPath()
    frame.moveTo(17, 10)
    for x, y in ((8, 10), (8, 32), (30, 32), (30, 23)):
        frame.lineTo(x, y)
    painter.drawPath(frame)
    painter.drawLine(18, 22, 32, 8)
    painter.drawLine(22, 8, 32, 8)
    painter.drawLine(32, 8, 32, 18)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


def _map_source_icon() -> QIcon:
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(theme.qcolor(theme.MUTED), 2.4))
    painter.drawEllipse(QRectF(5, 5, 30, 30))
    painter.drawEllipse(QRectF(13, 5, 14, 30))
    painter.drawLine(5, 20, 35, 20)
    painter.end()
    pixmap.setDevicePixelRatio(2)
    return QIcon(pixmap)


class _MapHeader(QWidget):
    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor("#ffffff", 0.14), 1))
        painter.setBrush(theme.qcolor(theme.BG1, 0.97))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                                theme.RADIUS_CHIP, theme.RADIUS_CHIP)


class _DragHandle(QWidget):
    """A visible, generous mouse target; movement is owned by the map window."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAccessibleName("Move map")
        self.setToolTip("Drag to move the map")
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if self.underMouse():
            painter.setBrush(theme.qcolor("#ffffff", 0.08))
            painter.drawRoundedRect(QRectF(self.rect()), 6, 6)
        painter.setBrush(theme.qcolor(theme.ACCENT if self.underMouse() else theme.MUTED))
        for dx in (-3, 3):
            for dy in (-6, 0, 6):
                painter.drawEllipse(QRectF(self.width() / 2 + dx - 1.4,
                                           self.height() / 2 + dy - 1.4, 2.8, 2.8))


class _MapCombo(QComboBox):
    def showPopup(self) -> None:  # noqa: N802
        # Compact equal-width selectors should not truncate the choices in their
        # open menus, especially with the overlay's enlarged font setting.
        width = max((self.fontMetrics().horizontalAdvance(self.itemText(i))
                     for i in range(self.count())), default=0) + 44
        screen = self.screen()
        limit = screen.availableGeometry().width() - 16 if screen is not None else width
        self.view().setMinimumWidth(min(max(self.width(), width), max(1, limit)))
        super().showPopup()


class _Result(QObject):
    ready = Signal(int, object, str)


class _Load:
    def __init__(self, token: int, repo: MapRepository, zone: str, entry: MapImage | None,
                 refresh: bool, slots: threading.Semaphore, preferred_url: str = "", *,
                 cached_first: bool = False) -> None:
        self.signal = _Result()
        self.token, self.repo, self.zone, self.entry, self.refresh = token, repo, zone, entry, refresh
        self.cancelled = threading.Event()
        self.slots = slots
        self.preferred_url = preferred_url
        self.cached_first = cached_first

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
            maps = self.repo.known_maps(self.zone) if self.cached_first and not self.refresh and self.entry is None else None
            if maps is None:
                maps = self.repo.maps(self.zone, refresh=self.refresh) if self.entry is None else [self.entry]
            if self.cancelled.is_set():
                return
            selected = next((m for m in maps if m.url == self.preferred_url), maps[0] if maps else None)
            selected_url = selected.url if selected else ""
            data = self.repo.image(selected, refresh=self.refresh) if selected else b""
            # QImage is reentrant; decode expensive originals in this worker.
            # QPixmap creation and all scene changes remain in the GUI callback.
            data = _decode_map_image(data) if data else QImage()
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
    label_context_requested = Signal(QPointF, QPoint, object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._fit = True
        self._image_rect = QRectF()
        self._label_items: list[MapLabelItem] = []

    def set_image(self, image: QImage, *, preserve_view: bool = False) -> None:
        previous = self.sceneRect()
        centre = self.mapToScene(self.viewport().rect().center())
        keep_zoom = preserve_view and not self._fit and not previous.isEmpty()
        self._label_items.clear()
        self.scene().clear()
        self._image_rect = QRectF()
        if not image.isNull():
            item = self.scene().addPixmap(QPixmap.fromImage(image))
            self._image_rect = item.boundingRect()
            self.scene().setSceneRect(self._image_rect)
        else:
            self.scene().setSceneRect(0, 0, 1, 1)
        if keep_zoom:
            current = self.sceneRect()
            self.centerOn(centre.x() * current.width() / previous.width(),
                          centre.y() * current.height() / previous.height())
        else:
            self.fit()

    def set_labels(self, labels: Sequence[MapLabel]) -> None:
        for item in self._label_items:
            self.scene().removeItem(item)
            item.deleteLater()
        self._label_items.clear()
        if self._image_rect.isEmpty():
            return
        for label in labels:
            item = MapLabelItem(label)
            item.setPos(self._image_rect.left() + label.x * self._image_rect.width(),
                        self._image_rect.top() + label.y * self._image_rect.height())
            self.scene().addItem(item)
            self._label_items.append(item)

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        point = self.mapToScene(event.pos())
        if self._image_rect.isEmpty() or not self._image_rect.contains(point):
            event.ignore()
            return
        item = self.itemAt(event.pos())
        label = item.label if isinstance(item, MapLabelItem) else None
        self.label_context_requested.emit(point, event.globalPos(), label)
        event.accept()

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
    npc_selected = Signal(str)
    npc_zone_requested = Signal(str)

    def __init__(self, settings: QSettings, repository: MapRepository | None = None, *,
                 embedded: bool = False, parent: QWidget | None = None,
                 settings_group: str | None = None) -> None:
        flags = Qt.WindowType.Widget if embedded else (Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint |
                                                       Qt.WindowType.FramelessWindowHint)
        super().__init__(parent if embedded else None, flags)
        self.embedded = bool(embedded)
        self._settings_group = settings_group or ("zones" if embedded else "map")
        if not embedded:
            self.setWindowTitle(window_title("map"))
            self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.settings = settings
        source = str(settings.value(self._key("source"), "images"))
        self._source = source if source in ("images", "atlas") else "images"
        self._atlas: AtlasView | None = None
        self.label_store = MapLabelStore.for_settings(settings)
        self._label_menu: QMenu | None = None
        self._locked = settings.value("overlay/locked", False, type=bool)
        self._opacity = 0.85
        self._font_scale = 1.0
        self._drag_offset: QPoint | None = None
        self._drag_press: QPoint | None = None
        self._drag_widget: QWidget | None = None
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
        self._current_zone = str(settings.value(self._key("zone"), "") or "")
        self._notable_npcs: dict[str, tuple[tuple[str, ...], str, dict[str, str], frozenset[str], frozenset[str]]] = {}
        self._notable_menu_zone = ""
        self._saved_view = self._read_view_state(settings.value(self._key("view"), {}))
        self._restore_view = self._saved_view
        self.setMinimumSize(260 if embedded else 360, 220 if embedded else 240)
        if not embedded:
            self.resize(900, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        self._map_stack = QStackedWidget(self)
        self.view = MapView(self._map_stack)
        self.view.label_context_requested.connect(self._label_context_menu)
        self.label_store.changed.connect(self._refresh_labels)
        self._map_stack.addWidget(self.view)
        layout.addWidget(self._map_stack)
        # Floating controls never change the map viewport when they appear/disappear.
        self.header = _MapHeader(self)
        if embedded:
            layout.insertWidget(0, self.header)
        top = QHBoxLayout(self.header)
        top.setContentsMargins(6, 6, 6, 6)
        top.setSpacing(6)
        self.wiki_link = QToolButton()
        self.wiki_link.setIcon(_wiki_icon())
        self.wiki_link.setIconSize(QSize(20, 20))
        self.wiki_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.wiki_link.clicked.connect(self._open_zone_wiki)
        self._update_wiki_link()
        top.addWidget(self.wiki_link)
        self.notable_npcs = QToolButton()
        self.notable_npcs.setIcon(_monster_icon())
        self.notable_npcs.setIconSize(QSize(20, 20))
        self.notable_npcs.setCursor(Qt.CursorShape.PointingHandCursor)
        self.notable_npcs.setAccessibleName("Notable NPCs")
        self.notable_npcs.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.notable_npcs_menu = NotableNpcMenu(self.notable_npcs)
        self.notable_npcs_menu.setAccessibleName("Notable NPCs in the selected zone")
        self.notable_npcs_menu.aboutToShow.connect(self._open_notable_npcs)
        self.notable_npcs_menu.aboutToHide.connect(self._header_hide_after_menu)
        self.notable_npcs.setMenu(self.notable_npcs_menu)
        self._update_notable_npcs()
        top.addWidget(self.notable_npcs)
        self.source_button = QToolButton()
        self.source_button.setAccessibleName("Map source")
        self.source_button.setIcon(_map_source_icon())
        self.source_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.source_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._source_menu = QMenu(self.source_button)
        self._source_menu.setAccessibleName("Map source")
        source_group = QActionGroup(self._source_menu)
        source_group.setExclusive(True)
        self._source_actions = {}
        for key, title in (("images", "Saved maps"), ("atlas", "MnM Atlas (online)")):
            action = self._source_menu.addAction(title)
            action.setCheckable(True)
            source_group.addAction(action)
            action.triggered.connect(lambda _checked=False, source=key: self.set_source(source))
            self._source_actions[key] = action
        self._source_menu.aboutToShow.connect(self._show_header)
        self._source_menu.aboutToHide.connect(self._header_hide_after_menu)
        self.source_button.setMenu(self._source_menu)
        top.addWidget(self.source_button)
        self.drag_handle = _DragHandle(self.header)
        top.addWidget(self.drag_handle)
        self.zone = _MapCombo()
        self.zone.setEditable(True)
        self.zone.addItem("Select zone", "")
        self.zone.addItems(ZONES)
        self.zone.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.zone.setMinimumWidth(0)
        self.zone.setAccessibleName("Select zone")
        self.zone.setToolTip("Browse a zone without changing the live map" if embedded else
                             "Select a map manually, or let the Combat chat's zone-entry line choose it")
        if self._current_zone:
            self.zone.setCurrentText(self._current_zone)
        self.zone.textActivated.connect(self.set_zone)
        self.zone.lineEdit().returnPressed.connect(lambda: self.set_zone(self.zone.currentText()))
        self.zone.lineEdit().textEdited.connect(self._begin_zone_edit)
        self.zone.lineEdit().editingFinished.connect(self._finish_zone_edit)
        top.addWidget(self.zone, 1)
        self.variants = _MapCombo()
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
        self.status = QLabel("Select a zone to browse its maps and notable NPCs." if embedded else
                             "Select a zone, or enter one in game.", self)
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.status.setStyleSheet(f"background: transparent; color: {theme.MUTED}; padding: 12px;")
        self._header_hide = QTimer(self)
        self._header_hide.setSingleShot(True)
        self._header_hide.setInterval(300)
        self._header_hide.timeout.connect(self._hide_header)
        for widget in (self, self.view.viewport(), self.header, self.zone,
                       self.zone.lineEdit(), self.variants, self.fullscreen, self.drag_handle,
                       self.wiki_link, self.notable_npcs, self.source_button):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        self.header.setVisible(embedded)
        self.fullscreen.setVisible(not embedded)
        self.set_appearance(1.0 if embedded else settings.value("overlay/opacity", 0.85, type=float),
                            1.0 if embedded else settings.value("overlay/font_scale", 1.0, type=float))
        self.set_locked(self._locked)
        self._layout_source()
        if embedded:
            return
        QShortcut(QKeySequence("F11"), self).activated.connect(self.toggle_fullscreen)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.exit_fullscreen)
        rect = settings.value(self._key("geometry"))
        screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
        if (isinstance(rect, QRect) and rect.isValid()
                and any(area.contains(rect.center()) for area in screens)):
            self.setGeometry(rect)
        else:
            screen = QGuiApplication.primaryScreen()
            self.move(screen.availableGeometry().topLeft() if screen is not None else QPoint())
        if settings.value(self._key("fullscreen"), False, type=bool):
            self._normal_geometry = self.geometry()
            # Set the state without showing: a previously closed map stays closed.
            self.setWindowState(self.windowState() | Qt.WindowState.WindowFullScreen)
            self._update_fullscreen_icon()
            self.drag_handle.hide()
            self._position_chrome()

    def _key(self, name: str) -> str:
        return f"{self._settings_group}/{name}"

    @staticmethod
    def _read_view_state(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict) or not isinstance(value.get("zone"), str) or not isinstance(value.get("url"), str):
            return None
        if not value["zone"] or not value["url"] or not isinstance(value.get("fit"), bool):
            return None
        for key, low, high in (("scale", 0.015, 12), ("x", 0, 1), ("y", 0, 1)):
            number = value.get(key)
            # Bounds also reject NaN/infinity without converting huge integers to float.
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not low <= number <= high:
                return None
        return value.copy()

    @property
    def current_zone(self) -> str:
        return self._current_zone

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def source(self) -> str:
        return self._source

    def _layout_source(self) -> None:
        docked = self.embedded or self._source == "atlas"
        layout = self.layout()
        if docked:
            if layout.indexOf(self.header) < 0:
                layout.insertWidget(0, self.header)
            self.header.show()
        else:
            layout.removeWidget(self.header)
            self.header.hide()
        self.variants.setVisible(self._source == "images")
        self._source_actions[self._source].setChecked(True)
        self.source_button.setToolTip("Map source: " + ("MnM Atlas (online)" if self._source == "atlas" else "Saved maps"))
        self._position_chrome()

    def _ensure_atlas(self) -> AtlasView:
        if self._atlas is None:
            self._atlas = AtlasView(self._map_stack)
            self._atlas.zone_changed.connect(self._atlas_zone_changed)
            self._atlas.fallback_requested.connect(lambda: self.set_source("images"))
            self._map_stack.addWidget(self._atlas)
            self._watch_atlas_input(self._atlas)
        return self._atlas

    def _watch_atlas_input(self, widget: QWidget) -> None:
        """Keep the overlay's edge resize and Alt-drag on browser render children."""
        if self.embedded:
            return
        widget.setMouseTracking(True)
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    def _atlas_zone_changed(self, zone: str) -> None:
        if (not self._closing and self.isVisible() and self._source == "atlas"
                and zone and zone_title(zone) != self._current_zone):
            self.set_zone(zone)

    def set_source(self, source: str) -> None:
        if source not in ("images", "atlas"):
            raise ValueError("Unknown map source")
        if self._closing:
            return
        self.save_state()
        self._token += 1
        self._cancel_loads()
        self._source = source
        self.settings.setValue(self._key("source"), source)
        self._layout_source()
        if source == "images":
            if self._atlas is not None:
                self._atlas.deactivate()
            self._map_stack.setCurrentWidget(self.view)
            if self._loaded_zone == self._current_zone and not self.view._image_rect.isEmpty():
                self._set_status("")
            elif not self._current_zone or self._current_zone == "World":
                self._set_status("Select a zone to browse its saved maps.")
            elif self.isVisible():
                self.load()
        else:
            self._set_status("")
            if self.isVisible():
                atlas = self._ensure_atlas()
                self._map_stack.setCurrentWidget(atlas)
                atlas.activate(self._current_zone)

    def set_locked(self, locked: bool) -> None:
        self._locked = bool(locked)
        if self._locked:
            self._end_drag()
        self.drag_handle.setVisible(not self.embedded and not self._locked and not self.isFullScreen())
        self._position_chrome()

    def set_appearance(self, opacity: float, font_scale: float) -> None:
        """Share the overlay's glass and text styling; map pixels stay opaque."""
        self._opacity = 1.0 if self.embedded else min(1.0, max(0.2, float(opacity)))
        self._font_scale = min(2.0, max(0.6, float(font_scale)))
        px = max(9, round(theme.OVERLAY_FONT_PX * self._font_scale))
        self.setStyleSheet(theme.overlay_qss(self._opacity) + f"""
            QWidget {{ font-size: {px}px; }}
            QComboBox {{ background: {theme.BG2}; border: 1px solid {theme.LINE};
                border-radius: 6px; padding: 4px 6px; min-width: 0; }}
            QComboBox:hover {{ border-color: {theme.MUTED}; }}
            QComboBox:focus {{ border-color: {theme.ACCENT}; }}
            QComboBox QAbstractItemView {{ background: {theme.BG1}; color: {theme.TEXT};
                selection-background-color: {theme.BG2}; selection-color: {theme.ACCENT}; }}
            QComboBox QLineEdit {{ background: transparent; border: none; padding: 0; }}
            QToolButton {{ border: none; padding: 0; }}
            QToolButton::menu-indicator {{ image: none; }}
            QMenu {{ background: {theme.BG1}; color: {theme.TEXT};
                border: 1px solid {theme.LINE}; padding: 4px; }}
            QMenu::item {{ padding: 6px 12px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {theme.BG2}; color: {theme.ACCENT}; }}
            QMenu::item:disabled {{ color: {theme.MUTED}; }}
            QGraphicsView {{ border: none; }}
        """)
        size = max(30, round(px * 2.3))
        self.wiki_link.setFixedSize(size, size)
        self.notable_npcs.setFixedSize(size, size)
        self.source_button.setFixedSize(size, size)
        icon_size = max(20, round(px * 1.54))
        self.notable_npcs.setIconSize(QSize(icon_size, icon_size))
        self.source_button.setIconSize(QSize(icon_size, icon_size))
        self.notable_npcs_menu.set_badge_size(max(14, round(px * 1.35)))
        if self.embedded:
            self.zone.setMinimumHeight(self.zone.lineEdit().minimumSizeHint().height() + 12)
        self.drag_handle.setFixedSize(max(24, round(px * 1.85)), size)
        self.fullscreen.setFixedSize(size, size)
        self._position_chrome()
        self.update()

    def on_message(self, msg: Any, event: Any) -> None:
        if self.embedded:
            return
        if event is not None and getattr(event, "kind", "") == "zone" and getattr(event, "target", ""):
            self.set_zone(event.target)
            return
        # Capture can begin long after the entry line has scrolled away. A local
        # /who summary still names the current zone; it must replace a stale map
        # selection even though it is correctly classified as personal chat.
        zone = zone_from_who(getattr(event, "text", "") or getattr(msg, "text", ""))
        if zone:
            self.set_zone(zone)

    def set_zone(self, zone: str) -> None:
        title = zone_title(zone)
        if not title or title in ("Select zone", "Choose a zone…"):
            return
        if title == self._current_zone and (self._loaded_zone == title or self._token in self._loads):
            return
        self.save_state()
        if title != self._current_zone:
            self._restore_view = None
        self._current_zone = title
        self._update_wiki_link()
        self._update_notable_npcs()
        self.settings.setValue(self._key("zone"), title)
        self.zone.setCurrentText(title)
        if not self.embedded:
            self.setWindowTitle(window_title("map"))
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
            if title != "World":
                self.npc_zone_requested.emit(title)
            self.load()

    def set_notable_npcs(self, zone: str, names: Sequence[str], status: str = "", *,
                        levels: Mapping[str, str] | None = None,
                        unique_loot: Iterable[str] = (), quests: Iterable[str] = ()) -> None:
        """Accept cached or refreshed names without replacing another zone's menu.

        An empty status with no names means a successfully loaded empty list.
        Supply an explicit loading/unavailable status when the answer is unknown.
        Cached names remain selectable alongside a refresh or failure status.
        """
        title = zone_title(zone)
        if not title:
            return
        unique: dict[str, str] = {}
        for name in names:
            name = str(name).strip()
            if name:
                unique.setdefault(npc_title_key(name), name)
        rows = tuple(sorted(unique.values(), key=str.casefold))
        value = (rows, str(status).strip(),
                 {npc_title_key(name): str(level).strip() for name, level in (levels or {}).items()
                  if npc_title_key(name) and level is not None},
                 frozenset(npc_title_key(name) for name in unique_loot),
                 frozenset(npc_title_key(name) for name in quests))
        if self._notable_npcs.get(title) == value:
            return
        self._notable_npcs[title] = value
        if title == self._current_zone:
            self._update_notable_npcs()

    def _update_notable_npcs(self) -> None:
        zone = self._current_zone
        has_zone = bool(zone) and zone != "World"
        self.notable_npcs.setEnabled(has_zone)
        self.notable_npcs.setToolTip(f"Notable NPCs in {zone}" if has_zone else "Select a zone to browse notable NPCs")
        names, status, levels, unique_loot, quests = self._notable_npcs.get(
            zone, ((), "Loading notable NPCs…", {}, frozenset(), frozenset()))
        # Metadata changes keep native actions and their vertical hit targets.
        # Membership/status changes wait until close/open so no item underneath
        # the pointer can become a different NPC while the user selects it.
        if self.notable_npcs_menu.isVisible():
            if self._notable_menu_zone == zone:
                self.notable_npcs_menu.refresh_metadata(levels, unique_loot, quests)
            return
        self.notable_npcs_menu.clear()
        self._notable_menu_zone = zone
        for name in names:
            key = npc_title_key(name)
            action = self.notable_npcs_menu.addAction("")
            action.setData(name)
            self.notable_npcs_menu.set_action_label(action, name, levels.get(key, ""))
            self.notable_npcs_menu.set_badges(action, unique_loot=key in unique_loot, quests=key in quests)
            action.triggered.connect(lambda _checked=False, name=name, zone=zone: self._select_npc(zone, name))
        if status or not names:
            if names:
                self.notable_npcs_menu.addSeparator()
            action = self.notable_npcs_menu.addAction((status or "No notable NPCs listed").replace("&", "&&"))
            action.setEnabled(False)

    def _select_npc(self, zone: str, name: str) -> None:
        if zone == self._current_zone and not self._closing:
            self.npc_selected.emit(name)
            if self._source == "atlas" and self.isVisible():
                self._ensure_atlas().focus_npc(name)

    def _open_notable_npcs(self) -> None:
        self._show_header()
        self._update_notable_npcs()
        if self._current_zone and self._current_zone != "World" and not self._closing:
            self.npc_zone_requested.emit(self._current_zone)

    def _header_hide_after_menu(self) -> None:
        self._header_hide.start()
        QTimer.singleShot(0, self._update_notable_npcs)

    def load(self, *, refresh: bool = False, entry: MapImage | None = None,
             preserve_view: bool = False) -> None:
        if self._source == "atlas":
            if not self._closing and self.isVisible():
                atlas = self._ensure_atlas()
                self._map_stack.setCurrentWidget(atlas)
                self._set_status("")
                atlas.activate(self._current_zone)
            return
        if not self._current_zone or self._closing:
            return
        if self._current_zone == "World":
            self._set_status("Select a zone to browse its saved maps.")
            return
        self._token += 1
        self._cancel_loads()
        self._preserve_view_token = self._token if preserve_view else -1
        if not preserve_view or not self.view.scene().items():
            self._set_status(f"Loading {self._current_zone}…")
            self.view.set_image(QImage())
        preferred_url = self._selected_url if preserve_view else ""
        if not preferred_url and self._saved_view and self._saved_view["zone"] == self._current_zone:
            preferred_url = self._saved_view["url"]
        worker = _Load(self._token, self.repo, self._current_zone, entry, refresh, self._slots, preferred_url,
                       cached_first=self.embedded)
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
        # Accept bytes for callers predating worker-side decoding (and fixtures).
        image = data if isinstance(data, QImage) else QImage.fromData(data)
        if image.isNull():
            self._set_status("The map image could not be read. Download maps in Settings to retry.")
            # A decoder/resource failure does not prove the cached bytes are
            # corrupt. Explicit map updates can replace a damaged download.
            return
        self.view.set_image(image, preserve_view=(token == self._preserve_view_token
                                                  and selected_url == self._selected_url))
        self._selected_url = selected_url
        self._loaded_zone = self._current_zone
        self._refresh_labels()
        saved = self._restore_view
        self._restore_view = None
        if saved and saved["zone"] == self._current_zone and saved["url"] == selected_url and not saved["fit"]:
            self.view.resetTransform()
            self.view.scale(saved["scale"], saved["scale"])
            self.view._fit = False
            rect = self.view.sceneRect()
            self.view.centerOn(rect.left() + saved["x"] * rect.width(), rect.top() + saved["y"] * rect.height())
        self.save_state()
        self._set_status("")

    def _refresh_labels(self, changed_map: str = "") -> None:
        if self._closing:
            return
        if not self._selected_url or self._loaded_zone != self._current_zone:
            self.view.set_labels([])
        elif not changed_map or changed_map == map_key(self._selected_url):
            self.view.set_labels(self.label_store.labels(self._selected_url))

    def _label_context_menu(self, point: QPointF, global_pos: QPoint, label: MapLabel | None) -> None:
        if (self._closing or not self._selected_url or self._loaded_zone != self._current_zone
                or self.view._image_rect.isEmpty() or not self.view._image_rect.contains(point)):
            return
        map_url = self._selected_url
        rect = self.view._image_rect
        x, y = (point.x() - rect.left()) / rect.width(), (point.y() - rect.top()) / rect.height()
        if self._label_menu is not None:
            self._label_menu.close()
        menu = QMenu(self)
        self._label_menu = menu
        menu.setAccessibleName("Map labels")
        if label is not None and map_key(label.map_url) == map_key(map_url):
            edit = menu.addAction("Edit label…")
            edit.triggered.connect(lambda _checked=False: self._edit_label(map_url, label.x, label.y, label))
            remove = menu.addAction("Delete label")
            remove.triggered.connect(lambda _checked=False: self._remove_label(label))
            menu.addSeparator()
        add = menu.addAction(label_icon("pin"), "Add label…")
        add.triggered.connect(lambda _checked=False: self._edit_label(map_url, x, y))
        menu.aboutToHide.connect(lambda: self._label_menu_closed(menu))
        menu.popup(global_pos)

    def _label_menu_closed(self, menu: QMenu) -> None:
        if self._label_menu is menu:
            self._label_menu = None
        menu.deleteLater()

    def _edit_label(self, map_url: str, x: float, y: float, label: MapLabel | None = None) -> None:
        if self._closing or (label is not None and self.label_store.get(label.id) is None):
            return
        dialog = MapLabelDialog(self, label=label)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted or self._closing:
                return
            values = dialog.value()
            saved = MapLabel(label.id if label is not None else uuid4().hex,
                             map_url, x, y, values["name"], values["notes"], values["icon"])
            self.label_store.save(saved)
        except (OSError, ValueError):
            QMessageBox.warning(self, "Map labels", "The label could not be saved. Check that your settings folder is writable.")
        finally:
            dialog.deleteLater()

    def _remove_label(self, label: MapLabel) -> None:
        if self._closing:
            return
        try:
            self.label_store.remove(label.id)
        except OSError:
            QMessageBox.warning(self, "Map labels", "The label could not be deleted. Check that your settings folder is writable.")

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

    def _update_wiki_link(self) -> None:
        has_zone = bool(self._current_zone) and self._current_zone != "World"
        self.wiki_link.setEnabled(has_zone)
        name = (f"Open {self._current_zone} on the wiki" if has_zone
                else "Select a zone to open its wiki page")
        self.wiki_link.setAccessibleName(name)
        self.wiki_link.setToolTip(name)

    def _open_zone_wiki(self) -> None:
        if self._current_zone and self._current_zone != "World":
            QDesktopServices.openUrl(QUrl(wiki_url(self._current_zone)))

    def _update_fullscreen_icon(self) -> None:
        fullscreen = self.isFullScreen()
        # Four corner marks, reversed for the return-to-window action.
        pixmap = QPixmap(40, 40)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(theme.qcolor(theme.MUTED), 3, Qt.PenStyle.SolidLine,
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
        gear = getattr(self, "_settings_gear", None)
        if gear is not None:
            gear.raise_()

    def _hide_header(self) -> None:
        if self.embedded or self._source == "atlas":
            return
        # A popup extends beyond the header; keep its controls until it closes.
        if (self._drag_offset is not None or self._editing_zone
                or self.notable_npcs_menu.isVisible()
                or self._source_menu.isVisible()
                or any(combo.view().isVisible() for combo in (self.zone, self.variants))):
            self._header_hide.start()
        elif self.header.geometry().contains(self.mapFromGlobal(QCursor.pos())):
            return
        else:
            self.header.hide()

    def _resize_edges(self, point: QPoint) -> Qt.Edge:
        edges = Qt.Edge(0)
        if not self.embedded and not self._locked and not self.isFullScreen():
            if point.x() < 5:
                edges |= Qt.Edge.LeftEdge
            elif point.x() >= self.width() - 5:
                edges |= Qt.Edge.RightEdge
            if point.y() < 5:
                edges |= Qt.Edge.TopEdge
            elif point.y() >= self.height() - 5:
                edges |= Qt.Edge.BottomEdge
        return edges

    def _begin_drag(self, widget: QWidget, point: QPoint) -> None:
        if self.embedded or self._locked or self.isFullScreen():
            return
        self._drag_press = point
        self._drag_offset = point - self.frameGeometry().topLeft()
        self._drag_widget = widget
        widget.grabMouse()
        self._show_header()

    def _end_drag(self) -> None:
        if self._drag_offset is None:
            return
        if QWidget.mouseGrabber() is self._drag_widget:
            self._drag_widget.releaseMouse()
        self._drag_offset = self._drag_press = None
        self._drag_widget = None
        self.save_state()
        self._header_hide.start()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if self.embedded:
            return super().eventFilter(watched, event)
        kind = event.type()
        if kind == QEvent.Type.ChildPolished:
            child = event.child()
            if self._atlas is not None and isinstance(child, QWidget) and self._atlas.isAncestorOf(child):
                self._watch_atlas_input(child)
            return super().eventFilter(watched, event)
        if self._drag_offset is not None:
            if kind == QEvent.Type.MouseMove:
                if event.buttons() & Qt.MouseButton.LeftButton:
                    point = event.globalPosition().toPoint()
                    if (point - self._drag_press).manhattanLength() > 4:
                        self.move(point - self._drag_offset)
                    return True
                self._end_drag()
            elif kind == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                self._end_drag()
                return True
            elif kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide) and watched is self:
                self._end_drag()
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
                elif watched is self.drag_handle or (watched is self.header and not self._locked and not self.isFullScreen()):
                    watched.setCursor(Qt.CursorShape.SizeAllCursor)
                elif watched in (self.wiki_link, self.notable_npcs, self.source_button):
                    watched.setCursor(Qt.CursorShape.PointingHandCursor)
                else:
                    watched.unsetCursor()
            if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                handle = self.windowHandle()
                if handle is not None and edges:
                    return handle.startSystemResize(edges)
                if not self._locked and not self.isFullScreen() and (
                    watched in (self.header, self.drag_handle) or event.modifiers() & Qt.KeyboardModifier.AltModifier
                ):
                    self._begin_drag(watched, event.globalPosition().toPoint())
                    return True
        elif kind == QEvent.Type.Leave and not self._header_hide.isActive():
            self._header_hide.start()
        return super().eventFilter(watched, event)

    def resizeEvent(self, event: Any) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._position_chrome()

    def _position_chrome(self) -> None:
        self.layout().activate()
        if self.embedded or self._source == "atlas":
            self.status.setGeometry(self._map_stack.geometry().adjusted(16, 16, -16, -16))
        else:
            self.header.setGeometry(5, 5, self.width() - 10, self.header.sizeHint().height())
            self.status.setGeometry(16, self.header.height(), self.width() - 32,
                                    max(40, self.height() - self.header.height() * 2))
        # A child widget's rectangular paint is not clipped by the parent's rounded
        # background. Clip the map itself so zoomed images cannot square off the frame.
        if self.embedded or self.isFullScreen():
            self.view.clearMask()
            self._map_stack.clearMask()
        else:
            clip = QPainterPath()
            radius = theme.RADIUS_PANEL - 2
            clip.addRoundedRect(QRectF(self.view.rect()), radius, radius)
            self.view.setMask(QRegion(clip.toFillPolygon().toPolygon()))
            stack_clip = QPainterPath()
            stack_clip.addRoundedRect(QRectF(self._map_stack.rect()), radius, radius)
            self._map_stack.setMask(QRegion(stack_clip.toFillPolygon().toPolygon()))

    def _settings_gear_changed(self) -> None:
        """The hover gear floats above the unchanged map header."""
        self.update()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        if self.embedded:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = 0 if self.isFullScreen() else theme.RADIUS_PANEL
        painter.setPen(QPen(theme.qcolor("#ffffff", 0.14), 1))
        painter.setBrush(theme.qcolor(theme.BG0, self._opacity))
        painter.drawRoundedRect(rect, radius, radius)

    def _select_map(self, index: int) -> None:
        if 0 <= index < len(self._entries):
            self.save_state()
            self._restore_view = None
            self.load(entry=self._entries[index])

    def toggle_fullscreen(self) -> None:
        if self.embedded:
            return
        self._end_drag()
        if self.isFullScreen():
            self.exit_fullscreen()
        else:
            self._normal_geometry = self.geometry()
            self.showFullScreen()
            self._update_fullscreen_icon()
            self.drag_handle.hide()
            self._position_chrome()
            self.save_state()

    def exit_fullscreen(self) -> None:
        if self.embedded:
            return
        if self.isFullScreen():
            self.showNormal()
            if self._normal_geometry.isValid():
                self.setGeometry(self._normal_geometry)
            self._update_fullscreen_icon()
            self.drag_handle.setVisible(not self._locked)
            self._position_chrome()
            self.save_state()

    def showEvent(self, event: Any) -> None:  # noqa: N802
        super().showEvent(event)
        self.visibility_changed.emit(True)
        if self.isVisible() and self._current_zone and self._current_zone != "World" and not self._closing:
            self.npc_zone_requested.emit(self._current_zone)
        if self._source == "atlas" or (self._loaded_zone != self._current_zone and self._token not in self._loads):
            self.load()

    def hideEvent(self, event: Any) -> None:  # noqa: N802
        self._end_drag()
        self.notable_npcs_menu.hide()
        self._source_menu.hide()
        if self._atlas is not None:
            self._atlas.deactivate()
        if self._label_menu is not None:
            self._label_menu.close()
        if not self._closing:
            self.save_state()
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def save_state(self) -> None:
        rect = self._normal_geometry if self.isFullScreen() else self.geometry()
        if not self.embedded:
            if rect.isValid():
                self.settings.setValue(self._key("geometry"), rect)
            self.settings.setValue(self._key("fullscreen"), self.isFullScreen())
        if (self._source == "images" and self._loaded_zone == self._current_zone
                and self._selected_url and self.view.scene().items()):
            rect = self.view.sceneRect()
            centre = self.view.mapToScene(self.view.viewport().rect().center())
            state = {
                "zone": self._current_zone, "url": self._selected_url, "fit": self.view._fit,
                "scale": min(12.0, max(0.015, self.view.transform().m11())),
                "x": min(1.0, max(0.0, (centre.x() - rect.left()) / rect.width())),
                "y": min(1.0, max(0.0, (centre.y() - rect.top()) / rect.height())),
            }
            self._saved_view = state
            self.settings.setValue(self._key("view"), state)

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._atlas is not None:
            self._atlas.shutdown()
        self.notable_npcs_menu.hide()
        self._header_hide.stop()
        self._token += 1
        self._cancel_loads()
        self.save_state()
        self.hide()
