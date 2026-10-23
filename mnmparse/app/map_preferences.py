"""The startup map provider shared by the overlay and Zones page."""
from __future__ import annotations

from PySide6.QtCore import QSettings


MAP_SOURCE_KEY = "map/default_source"
MAP_SOURCES = (("atlas", "MnM Atlas (online)"), ("images", "Saved maps (offline)"))


def default_map_source(settings: QSettings) -> str:
    source = str(settings.value(MAP_SOURCE_KEY, "atlas"))
    return source if source in {key for key, _title in MAP_SOURCES} else "atlas"
