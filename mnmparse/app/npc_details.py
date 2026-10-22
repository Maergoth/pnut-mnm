"""Cache-first NPC lookups; bounded daemon workers never touch widgets."""
from __future__ import annotations

from collections import deque
from dataclasses import replace
import logging
import re
import threading
import time
from typing import Any

from PySide6.QtCore import QObject, Qt, Signal

from mnmparse.maps import zone_title
from mnmparse.npc_wiki import ItemDetails, WikiPageMissing, unique_drops_excluding_common
from mnmparse.parser import parse_line

log = logging.getLogger(__name__)
RECHECK_SECONDS = 300.0
RETRY_SECONDS = 60.0
MAX_QUEUED = 64


class NpcDetailsController(QObject):
    _ready = Signal(str, str, object, str)
    notables_updated = Signal(str, object)

    def __init__(self, repository: Any, panel: Any, map_overlay: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.repo, self.panel, self.map_overlay = repository, panel, map_overlay
        self._selected = ""
        self._zone = ""
        self._pending: set[tuple[str, str]] = set()
        self._next_check: dict[tuple[str, str], float] = {}
        self._last_errors: dict[tuple[str, str], str] = {}
        self._notable_records: dict[str, Any] = {}
        self._notable_status: dict[str, str] = {}
        self._map_views: list[Any] = []
        self._npc_metadata: dict[str, Any] = {}
        self._enrichment: set[tuple[str, str]] = set()
        self._enrichment_retry_at = 0.0
        self._queue: deque[tuple[str, str]] = deque()
        self._condition = threading.Condition()
        self._closed = False
        self._started = False
        self._ready.connect(self._finished, Qt.ConnectionType.QueuedConnection)
        panel.item_hovered.connect(self.show_item)
        self.attach_map(map_overlay)

    def attach_map(self, view: Any) -> None:
        """Share cached notable details with another map without changing hunting zone."""
        if self._closed or any(view is existing for existing in self._map_views):
            return
        self._map_views.append(view)
        view.npc_selected.connect(self.show_npc)
        view.npc_zone_requested.connect(self.show_notables)
        destroyed = getattr(view, "destroyed", None)
        if destroyed is not None:
            destroyed.connect(lambda *_args, attached=view: self.detach_map(attached))
        for zone, record in tuple(self._notable_records.items()):
            self._present_notables(zone, record, self._notable_status[zone], enrich=False)

    def detach_map(self, view: Any) -> None:
        self._map_views = [existing for existing in self._map_views if existing is not view]
        for name, callback in (("npc_selected", self.show_npc), ("npc_zone_requested", self.show_notables)):
            try:
                getattr(view, name).disconnect(callback)
            except (RuntimeError, TypeError):
                pass  # A QObject can already be in its native destructor.

    @staticmethod
    def _key(name: str) -> str:
        return " ".join(str(name or "").replace("_", " ").split())

    def on_message(self, _message: Any, event: Any) -> None:
        if getattr(event, "kind", "") == "consider" and getattr(event, "target", None):
            text = str(getattr(event, "text", "") or "")
            if getattr(event, "outcome", None) == "player" or re.search(r"\(PvP\s+Aggro\)", text, re.I):
                return
            # Old archived events lack the npc/player marker. Positive current
            # faction prose (or an article-led NPC corpse) is required even for
            # those events; an arbitrary target or a bare difficulty is not NPC
            # evidence. Reparse also rejects unknown text carrying a stale tag.
            identified = parse_line(text, float(getattr(event, "ts", 0.0) or 0.0))
            if (identified.kind != "consider" or identified.outcome != "npc"
                    or self._key(identified.target).casefold() != self._key(event.target).casefold()):
                return
            self.show_npc(event.target)

    def show_npc(self, name: str) -> None:
        name = self._key(name)
        if not name or self._closed:
            return
        self._selected = name
        self.panel.show_npc(name)
        self.refresh_selection()

    def refresh_selection(self) -> None:
        name = self._selected
        if not name or self._closed or not self.panel.enabled:
            return
        cached = self.repo.cached_npc(name)
        status = self._status("npc", name, cached)
        self.panel.set_details(cached, status)
        self._request("npc", name)
        if cached is not None:
            self._prefetch_items(cached)

    def show_notables(self, zone: str) -> None:
        zone = zone_title(zone)
        if not zone or self._closed:
            return
        self._zone = zone
        cached = self.repo.cached_notables(zone)
        self._present_notables(zone, cached, self._status("notables", zone, cached), enrich=False)
        self._request("notables", zone)
        for name in getattr(cached, "names", ()):
            self._request("npc", name, background=True)

    def ensure_notables(self, zone: str) -> None:
        """Warm the current hunting zone without changing the map's selected zone."""
        zone = zone_title(zone)
        if zone and not self._closed:
            self._request("notables", zone)

    @staticmethod
    def _positive_values(values: Any) -> tuple[str, ...]:
        """Unavailable placeholders never claim that loot or quests exist."""
        result = []
        if isinstance(values, str):
            values = (values,)
        for raw in values or ():
            if not isinstance(raw, str):
                continue
            value = " ".join(raw.replace("_", " ").split()).strip(" .:;!?–—-")
            key = value.casefold()
            if (not value or key in {"none", "none known", "unknown", "n/a", "n a", "na", "tbd", "not known", "not yet known"}
                    or key.startswith(("not available", "not recorded", "not listed", "unavailable", "no data", "no known ", "no quest", "no unique", "no drop"))):
                continue
            result.append(raw)
        return tuple(result)

    def _present_notables(self, zone: str, record: Any, status: str, *, enrich: bool = True) -> None:
        self._notable_records[zone] = record
        self._notable_status[zone] = status
        names = tuple(getattr(record, "names", ())) if record is not None else ()
        zone_levels = {self._key(name).casefold(): level for name, level in getattr(record, "levels", ())}
        levels, unique_loot, quests = {}, [], []
        for name in names:
            key = self._key(name).casefold()
            details = self._npc_metadata.get(key)
            if key not in self._npc_metadata:
                details = self.repo.cached_npc(name)
                self._npc_metadata[key] = details
            candidates = (zone_levels.get(key), getattr(details, "level", ""))
            level = next((value for value in candidates if isinstance(value, str)
                          and len(value) <= 120 and re.search(r"\d", value)), "")
            if level:
                levels[name] = level
            if details is not None:
                common = self._positive_values(getattr(details, "common_drops", ()))
                unique = self._positive_values(getattr(details, "unique_drops", ()))
                if unique_drops_excluding_common(common, unique):
                    unique_loot.append(name)
                if self._positive_values(getattr(details, "quests", ())):
                    quests.append(name)
        for view in tuple(self._map_views):
            try:
                view.set_notable_npcs(zone, names, status, levels=levels,
                                      unique_loot=unique_loot, quests=quests)
            except RuntimeError:
                self.detach_map(view)
        self.notables_updated.emit(zone, record)
        if enrich:
            for name in names:
                self._request("npc", name, background=True)

    def show_item(self, name: str) -> None:
        name = self._key(name)
        if not name or self._closed:
            return
        cached = self.repo.cached_item(name)
        if cached is not None:
            self._present_item(name, cached)
        else:
            self.panel.set_item_status(name, self._status("item", name, None))
        self._request("item", name)

    def _present_item(self, requested: str, item: Any) -> None:
        # Redirect targets can use a different title from the drop label. The
        # source remains canonical while the tooltip follows the hovered label.
        if isinstance(item, ItemDetails):
            item = replace(item, name=requested)
        self.panel.set_item_details(item)

    def _status(self, kind: str, name: str, cached: Any) -> str:
        error = self._last_errors.get((kind, name))
        if error == "page_missing":
            if cached is not None:
                return "No wiki page found · showing cached details"
            label = {"npc": "NPC", "item": "item", "notables": "zone"}[kind]
            return f"No wiki page found for this {label}"
        if error:
            return "Wiki unavailable · showing cached details" if cached is not None else "Wiki details unavailable · try again later"
        return "Cached wiki details" if cached is not None else "Loading wiki details…"

    def _prefetch_items(self, details: Any) -> None:
        if not self.panel.enabled:
            return
        options = self.panel.options
        drops = []
        for key in ("unique_drops", "common_drops"):
            if options.get(key):
                drops.extend(getattr(details, key, ()))
        # Warm tooltip records while the player reads the NPC card. Higher priority
        # NPC/zone requests can pass these background item requests in the queue.
        for name in dict.fromkeys(drops[:40]):
            cached = self.repo.cached_item(name)
            if cached is not None:
                self._present_item(name, cached)
            self._request("item", name, background=True)

    def _request(self, kind: str, name: str, *, background: bool = False) -> None:
        key = (kind, name)
        if self._closed or time.monotonic() < self._next_check.get(key, 0):
            return
        with self._condition:
            if background and kind == "npc" and time.monotonic() < self._enrichment_retry_at:
                return
            if key in self._pending:
                if not background:
                    self._enrichment.discard(key)
                if not background and key in self._queue:
                    self._queue.remove(key)
                    self._queue.appendleft(key)
                    self._condition.notify()
                return
            if len(self._queue) >= MAX_QUEUED:
                if background:
                    return
                discarded = self._queue.pop()
                self._pending.discard(discarded)
                self._enrichment.discard(discarded)
            self._pending.add(key)
            if background:
                self._queue.append(key)
                if kind == "npc":
                    self._enrichment.add(key)
            else:
                self._queue.appendleft(key)
            if not self._started:
                self._started = True
                for index in range(2):
                    threading.Thread(target=self._work, name=f"npc-wiki-{index}", daemon=True).start()
            self._condition.notify()

    def _work(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._queue)
                if self._closed:
                    return
                kind, name = self._queue.popleft()
            result, error = None, ""
            try:
                result = getattr(self.repo, f"refresh_{kind}")(name)
            except Exception as exc:
                error = "page_missing" if isinstance(exc, WikiPageMissing) else "unavailable"
                log.info("NPC wiki %s lookup for %s: %s", kind, name, exc)
                if error == "unavailable":
                    # A shared outage must not turn a zone list into dozens of
                    # failed requests. Already-active work stays bounded at two;
                    # foreground considers and item hovers remain available.
                    with self._condition:
                        self._enrichment_retry_at = time.monotonic() + RETRY_SECONDS
                        for queued in tuple(self._queue):
                            if queued in self._enrichment:
                                self._queue.remove(queued)
                                self._pending.discard(queued)
                                self._enrichment.discard(queued)
            if not self._closed:
                try:
                    self._ready.emit(kind, name, result, error)
                except RuntimeError:
                    return  # Qt teardown never needs to wait for the network.

    def _finished(self, kind: str, name: str, result: Any, error: str) -> None:
        if self._closed:
            return
        key = (kind, name)
        self._pending.discard(key)
        self._enrichment.discard(key)
        self._next_check[key] = time.monotonic() + (RETRY_SECONDS if error else RECHECK_SECONDS)
        if error:
            self._last_errors[key] = error
        else:
            self._last_errors.pop(key, None)
        cached = result or getattr(self.repo, f"cached_{kind}")(name)
        status = self._status(kind, name, cached) if error else ""
        if kind == "npc":
            if cached is not None:
                self._npc_metadata[self._key(name).casefold()] = cached
            if name == self._selected:
                self.panel.set_details(cached, status)
                if cached is not None:
                    self._prefetch_items(cached)
            for zone, record in tuple(self._notable_records.items()):
                if any(self._key(candidate).casefold() == self._key(name).casefold()
                       for candidate in getattr(record, "names", ())):
                    self._present_notables(zone, record, self._notable_status[zone], enrich=False)
        elif kind == "notables":
            self._present_notables(name, cached, status)
        elif kind == "item":
            if cached is not None:
                self._present_item(name, cached)
            else:
                self.panel.set_item_status(name, status)

    def shutdown(self) -> None:
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._pending.clear()
            self._enrichment.clear()
            self._condition.notify_all()
