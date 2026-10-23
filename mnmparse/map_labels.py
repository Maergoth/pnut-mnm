"""Persistent user labels attached to individual original map image URLs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import math
from numbers import Real
import os
import re
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID
from weakref import WeakValueDictionary

from PySide6.QtCore import QObject, QSettings, Signal
from shiboken6 import isValid

ICONS = (("pin", "Pin"), ("star", "Star"), ("camp", "Camp"),
         ("treasure", "Treasure"), ("danger", "Danger"), ("merchant", "Merchant"))
MAX_NAME = 80
MAX_NOTES = 500
MAX_RECORD_BYTES = 8192
MAX_MAP_URL = 4096
_PREFIX = "map_labels/"
_FIELDS = frozenset(("id", "map_url", "x", "y", "name", "notes", "icon"))
_ICON_IDS = frozenset(key for key, _title in ICONS)
_STORES: WeakValueDictionary[tuple[str, int], MapLabelStore] = WeakValueDictionary()


@dataclass(frozen=True, slots=True)
class MapLabel:
    id: str
    map_url: str
    x: float
    y: float
    name: str
    notes: str
    icon: str


def map_key(url: str) -> str:
    """Canonical original file identity; never equate different maps of a zone."""
    if not isinstance(url, str) or not url.strip() or len(url) > MAX_MAP_URL:
        raise ValueError("A map image URL is required")
    url = url.strip()
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in url):
        raise ValueError("Invalid map image URL")
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
                or parts.password is not None or not parts.path or parts.path == "/"):
            raise ValueError
        port = parts.port
    except ValueError:
        raise ValueError("Invalid map image URL") from None
    path = parts.path
    if re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ValueError("Invalid map image URL")

    def escaped(match):
        character = chr(int(match[0][1:], 16))
        return character if character.isascii() and (character.isalnum() or character in "-._~") else match[0].upper()

    path = re.sub(r"%[0-9a-fA-F]{2}", escaped, path)
    if "/thumb/" in path:
        prefix, remainder = path.split("/thumb/", 1)
        original, separator, thumbnail = remainder.rpartition("/")
        if separator and original and re.match(r"(?:(?:lossy|page\d+)-)*\d+px-", thumbnail):
            path = prefix + "/" + original
    path = quote(path, safe="/%:@!$&'()*+,;=-._~")
    host = parts.hostname.lower()
    if ":" in host:
        host = "[" + host + "]"
    if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        host += ":" + str(port)
    return urlunsplit((parts.scheme, host, path, "", ""))


def _identifier(identifier: str) -> str:
    if not isinstance(identifier, str):
        raise ValueError("A label UUID is required")
    try:
        return str(UUID(identifier))
    except (ValueError, AttributeError):
        raise ValueError("A label UUID is required") from None


def _text(value: str, *, notes: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Label text must be text")
    if notes:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any(unicodedata.category(char) in {"Cc", "Cs"} and (not notes or char not in "\n\t") for char in value):
        raise ValueError("Label text contains unsupported control characters")
    value = value.strip()
    limit = MAX_NOTES if notes else MAX_NAME
    if len(value) > limit or not notes and not value:
        raise ValueError("Label text is empty or too long")
    return value


def _validated(label: MapLabel) -> MapLabel:
    if not isinstance(label, MapLabel):
        raise ValueError("A MapLabel is required")
    coordinates = []
    for value in (label.x, label.y):
        if isinstance(value, bool) or not isinstance(value, Real) or not 0 <= value <= 1 or not math.isfinite(value):
            raise ValueError("Label coordinates must be between zero and one")
        coordinates.append(float(value))
    if not isinstance(label.icon, str) or label.icon not in _ICON_IDS:
        raise ValueError("Unknown label icon")
    return MapLabel(_identifier(label.id), map_key(label.map_url), *coordinates,
                    _text(label.name), _text(label.notes, notes=True), label.icon)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Repeated label field")
        result[key] = value
    return result


class MapLabelStore(QObject):
    changed = Signal(str)

    def __init__(self, settings: QSettings, parent: QObject | None = None):
        super().__init__(parent)
        # Own a root-namespace handle. Caller groups and QObject teardown must
        # not redirect the shared store or destroy its backing QSettings object.
        self._settings = QSettings(settings.fileName(), settings.format(), self)
        self._settings.setFallbacksEnabled(False)

    @classmethod
    def for_settings(cls, settings: QSettings) -> MapLabelStore:
        return for_settings(settings)

    def _sync(self, *, writing: bool = False) -> None:
        self._settings.sync()
        if writing and self._settings.status() != QSettings.Status.NoError:
            raise IOError("Map labels could not be saved")

    def _read(self, identifier: str) -> MapLabel | None:
        raw = self._settings.value(_PREFIX + identifier)
        if not isinstance(raw, str) or len(raw) > MAX_RECORD_BYTES:
            return None
        try:
            if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
                return None
            value = json.loads(raw, object_pairs_hook=_object)
            if not isinstance(value, dict) or set(value) != _FIELDS:
                return None
            label = _validated(MapLabel(**value))
            return label if label.id == identifier else None
        except (ValueError, TypeError, UnicodeError, OverflowError):
            return None

    def labels(self, map_url: str) -> list[MapLabel]:
        key = map_key(map_url)
        self._sync()
        result = []
        for stored in self._settings.allKeys():
            if not stored.startswith(_PREFIX):
                continue
            identifier = stored[len(_PREFIX):]
            try:
                if _identifier(identifier) != identifier:
                    continue
            except ValueError:
                continue
            label = self._read(identifier)
            if label is not None and label.map_url == key:
                result.append(label)
        return sorted(result, key=lambda label: (label.name.casefold(), label.id))

    def get(self, identifier: str) -> MapLabel | None:
        try:
            identifier = _identifier(identifier)
        except ValueError:
            return None
        self._sync()
        return self._read(identifier)

    def _write(self, identifier: str, value: str | None) -> None:
        self._sync(writing=True)
        key = _PREFIX + identifier
        existed, previous = self._settings.contains(key), self._settings.value(key)
        if value is None:
            self._settings.remove(key)
        else:
            self._settings.setValue(key, value)
        try:
            self._sync(writing=True)
        except IOError:
            # Keep a failed edit out of the in-memory view, too. Only this UUID
            # is restored; other views' pending keys are never rewritten.
            if existed:
                self._settings.setValue(key, previous)
            else:
                self._settings.remove(key)
            raise

    def save(self, label: MapLabel) -> None:
        label = _validated(label)
        encoded = json.dumps(asdict(label), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
            raise ValueError("Map label is too large")
        previous = self.get(label.id)
        if previous == label:
            return
        self._write(label.id, encoded)
        if previous is not None and previous.map_url != label.map_url:
            self.changed.emit(previous.map_url)
        self.changed.emit(label.map_url)

    def remove(self, identifier: str) -> None:
        try:
            identifier = _identifier(identifier)
        except ValueError:
            return
        previous = self.get(identifier)
        if not self._settings.contains(_PREFIX + identifier):
            return
        self._write(identifier, None)
        if previous is not None:
            self.changed.emit(previous.map_url)


def for_settings(settings: QSettings) -> MapLabelStore:
    key = (os.path.normcase(settings.fileName()), settings.format().value)
    store = _STORES.get(key)
    if store is None or not isValid(store):
        store = MapLabelStore(settings)
        _STORES[key] = store
    return store


__all__ = ["MapLabel", "MapLabelStore", "ICONS", "MAX_NAME", "MAX_NOTES", "map_key", "for_settings"]
