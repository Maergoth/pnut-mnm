"""An independent map browser and cache-backed NPC reference inside the main app."""
from __future__ import annotations

from html import escape
from typing import Any
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QPoint, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtWidgets import QLabel, QSplitter, QTextBrowser, QVBoxLayout, QWidget

from mnmparse.app import theme
from mnmparse.app.map_overlay import MapOverlay
from mnmparse.app.npc_panel import _ItemTooltip, _available_text
from mnmparse.maps import MapRepository
from mnmparse.npc_wiki import WIKI, unique_drops_excluding_common, wiki_url


def _key(name: str) -> str:
    return " ".join(str(name).replace("_", " ").split()).casefold()


def _wiki_target(name: str, record: Any) -> str:
    source = getattr(record, "source", "")
    try:
        parts = urlsplit(source)
        if (parts.scheme == "https" and parts.netloc.casefold() == urlsplit(WIKI).netloc
                and parts.path.startswith("/wiki/") and len(parts.path) > 6
                and not parts.query and "\\" not in parts.path):
            return source
    except (TypeError, ValueError):
        pass
    return wiki_url(name)


class BrowserOnlyNpcPanel(QObject):
    """Keep reference lookups available when the optional combat UI is unavailable."""
    item_hovered = Signal(str)
    enabled = False
    options: dict[str, bool] = {}

    def show_npc(self, *_args):
        pass

    def set_details(self, *_args):
        pass

    def set_item_details(self, *_args):
        pass

    def set_item_status(self, *_args):
        pass


class NpcBrowserView(QWidget):
    item_requested = Signal(str)

    def __init__(self, cfg: Any, settings: QSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self._cfg, self._settings = cfg, settings
        self._px = 14.0
        self.selected_name = ""
        self._details = None
        self._status = ""
        self._items: dict[str, Any] = {}
        self._item_status: dict[str, str] = {}
        self._links: dict[str, str] = {}
        self._hovered_item = ""
        self.setMinimumWidth(280)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        heading = QLabel("NPC details", self)
        heading.setObjectName("Heading")
        layout.addWidget(heading)
        self.browser = QTextBrowser(self)
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setAccessibleName("Selected notable NPC details")
        self.browser.setStyleSheet(f"QTextBrowser {{ background: {theme.BG1}; color: {theme.TEXT};"
                                  f"border: 1px solid {theme.LINE}; border-radius: 8px; padding: 12px; }}")
        self.browser.document().setDefaultStyleSheet(
            f"a {{ color: {theme.ACCENT}; text-decoration: none; }} h3 {{ margin-top: 0; }}"
            "h4 { margin-bottom: 4px; } ul { margin-top: 2px; margin-left: 16px; }")
        self.browser.anchorClicked.connect(self._open_link)
        self.browser.highlighted.connect(self._hover_link)
        layout.addWidget(self.browser, 1)
        self._tooltip = _ItemTooltip(self)
        self._render()

    def select_npc(self, name: str) -> None:
        self.selected_name = str(name).strip()
        self._details = None
        self._status = "Loading wiki details…"
        self._hovered_item = ""
        self._tooltip.hide()
        self._render()

    def npc_updated(self, name: str, details: Any, status: str) -> None:
        if _key(name) == _key(self.selected_name):
            self._details, self._status = details, status
            self._render()

    def item_updated(self, name: str, item: Any, status: str) -> None:
        key = _key(name)
        if item is not None:
            self._items[key] = item
        self._item_status[key] = status
        if key == _key(self._hovered_item):
            self._present_item()

    def set_config(self, cfg: Any) -> None:
        self._cfg = cfg
        self._render()

    def _render(self) -> None:
        rows, links = [], {}
        if not self.selected_name:
            rows.append(f'<p style="color:{theme.MUTED}">Select a notable NPC from the monster menu to see its details.</p>')
        else:
            rows.append(f'<h3><a href="npc:current">{escape(self.selected_name)}</a></h3>')
        details = self._details
        if details is not None:
            enabled = lambda field: self._settings.value("npc/" + field, bool(getattr(self._cfg, "npc_" + field)), type=bool)
            level = _available_text(getattr(details, "level", ""))
            if enabled("level") and level:
                rows.append(f"<p><b>Level:</b> {escape(level)}</p>")
            common = tuple(getattr(details, "common_drops", ()))
            unique = unique_drops_excluding_common(common, tuple(getattr(details, "unique_drops", ())))
            for field, title, values in (("unique_drops", "Unique drops", unique),
                                         ("common_drops", "Common drops", common),
                                         ("quests", "Quests", getattr(details, "quests", ())),
                                         ("reputation", "Reputation", getattr(details, "reputation", ()))):
                values = [text for value in values if (text := _available_text(value))]
                if enabled(field) and values:
                    rows.append(f"<h4>{title}</h4><ul>")
                    for value in values:
                        text = escape(value)
                        if field.endswith("drops"):
                            anchor = f"item:{len(links)}"
                            links[anchor] = value
                            text = f'<a href="{anchor}">{text}</a>'
                        rows.append(f"<li>{text}</li>")
                    rows.append("</ul>")
        if self._status:
            rows.append(f'<p style="color:{theme.MUTED}">{escape(self._status)}</p>')
        self._links = links
        if self._hovered_item and not any(_key(value) == _key(self._hovered_item) for value in links.values()):
            self._hovered_item = ""
            self._tooltip.hide()
        position = self.browser.verticalScrollBar().value()
        blocked = self.browser.blockSignals(True)
        try:
            self.browser.setHtml("".join(rows))
            self.browser.verticalScrollBar().setValue(position)
        finally:
            self.browser.blockSignals(blocked)

    def _open_link(self, url: QUrl) -> None:
        if url.toString() == "npc:current" and self.selected_name:
            name, record = self.selected_name, self._details
        elif url.toString() in self._links:
            name = self._links[url.toString()]
            record = self._items.get(_key(name))
        else:
            return
        try:
            QDesktopServices.openUrl(QUrl(_wiki_target(name, record)))
        except ValueError:
            return

    def _hover_link(self, url: QUrl) -> None:
        name = self._links.get(url.toString(), "")
        if name == self._hovered_item:
            return
        self._hovered_item = name
        if name:
            self._present_item()
            self.item_requested.emit(name)
        else:
            self._tooltip.hide()

    def _present_item(self) -> None:
        if not self.isVisible() or not self._hovered_item:
            return
        name = self._hovered_item
        item = self._items.get(_key(name))
        status = self._item_status.get(_key(name), "" if item is not None else "Loading item details…")
        self._tooltip.present(name, tuple(getattr(item, "lines", ())), status, QCursor.pos() + QPoint(16, 18))

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._render()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._hovered_item = ""
        self._tooltip.hide()
        super().hideEvent(event)


class ZonesPage(QWidget):
    def __init__(self, engine: Any, cfg: Any, settings: QSettings, parent: QWidget | None = None, *,
                 repository: MapRepository | None = None):
        super().__init__(parent)
        self._settings = settings
        self._controller = None
        self._browse_chosen = bool(str(settings.value("zones/zone", "") or "").strip())
        self._seeded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        title = QLabel("Zones", self)
        title.setObjectName("Heading")
        layout.addWidget(title)
        hint = QLabel("Browse maps and notable NPCs. Your choice here stays separate from the live map.", self)
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.map = MapOverlay(settings, repository, embedded=True, parent=self.splitter)
        self.details = NpcBrowserView(cfg, settings, self.splitter)
        self.splitter.addWidget(self.map)
        self.splitter.addWidget(self.details)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setSizes([650, 360])
        saved = settings.value("zones/splitter")
        if saved is not None:
            self.splitter.restoreState(saved)
        self.splitter.splitterMoved.connect(lambda *_: settings.setValue("zones/splitter", self.splitter.saveState()))
        layout.addWidget(self.splitter, 1)
        self.map.zone.lineEdit().textEdited.connect(self._mark_browsing)
        self.map.zone.textActivated.connect(self._mark_browsing)
        self.map.npc_selected.connect(self._select_npc)
        changed = getattr(engine, "zone_changed", None)
        if hasattr(changed, "connect"):
            changed.connect(self.seed_initial_zone)
        self.seed_initial_zone(getattr(engine, "current_zone", ""))

    def _mark_browsing(self, *_args) -> None:
        self._browse_chosen = True

    def seed_initial_zone(self, zone: str) -> None:
        if isinstance(zone, str) and zone and not (self._seeded or self._browse_chosen or self.map.current_zone):
            self._seeded = True
            self.map.set_zone(zone)

    def bind(self, controller: Any) -> None:
        if self._controller is not None:
            return
        self._controller = controller
        controller.attach_map(self.map)
        controller.npc_updated.connect(self.details.npc_updated)
        controller.item_updated.connect(self.details.item_updated)
        self.details.item_requested.connect(controller.request_item)
        if self.map.isVisible() and self.map.current_zone:
            controller.show_notables(self.map.current_zone)

    def _select_npc(self, name: str) -> None:
        self.details.select_npc(name)
        if self._controller is not None:
            self._controller.request_npc(name)

    def set_config(self, cfg: Any) -> None:
        self.details.set_config(cfg)

    def shutdown(self) -> None:
        self._settings.setValue("zones/splitter", self.splitter.saveState())
        self.map.shutdown()
        self.details._tooltip.hide()


__all__ = ["ZonesPage"]
