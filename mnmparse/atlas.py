"""Small, verified MnM Atlas navigation metadata; no map or marker data.

Embedding and named-search parameters: https://www.mnmatlas.com/embed/
Map IDs checked against the public Atlas manifest on 2026-10-23.
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from .maps import zone_title


ATLAS_ORIGIN = "https://www.mnmatlas.com"
_HOST = "www.mnmatlas.com"
_MAX_NAME = 256
_MAX_URL = 8192

# Keep canonical PNUT zone titles, including the ASCII apostrophe in Keeper's
# Bight. These are navigation identifiers, not copies of Atlas map content.
_ZONE_MAP_IDS = {
    "Night Harbor": "night-harbor",
    "Underdocks": "underdocks",
    "Ail'Vorith": "ail-vorith",
    "Blind Midden": "blind-midden",
    "Faelindral": "faelindral",
    "Evershade Weald": "evershade-weald",
    "Sungreet Strand": "sungreet-strand",
    "Shaded Dunes": "shaded-dunes",
    "Fallen Pass": "fallen-pass",
    "Tomb of the Last Wyrmsbane": "wyrmsbane-tomb",
    "Glass Flats": "glass-flats",
    "Ancient Crypt": "ancient-crypt",
    "Infested Crypt": "infested-crypt",
    "Blacktide Bay": "blacktide-bay",
    "Scarwood": "scarwood",
    "Vale of Zintar": "vale-of-zintar",
    "Grain Cellar": "grain-cellar",
    "Great Cavern Sea": "great-cavern-sea",
    "Caves of Irem": "caves-of-irem",
    "Tel Ekir": "tel-ekir",
    "Keeper's Bight": "keepers-bight",
    "Fallen Watch": "fallen-watch",
    "Shallow Shoals": "shallow-shoals",
    "Glinting Hollow": "glinting-hollow",
    "Sunken Resort": "sunken-resort",
    "Rothold": "rothold",
    "Grimtide Sanctum": "grimtide-sanctum",
}
_MAP_ZONES = {map_id: title for title, map_id in _ZONE_MAP_IDS.items()}


def _has_controls(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)


def _name(value: str, field: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_NAME or _has_controls(value):
        raise ValueError(f"Invalid Atlas {field}")
    return value


def atlas_url(zone: str = "", npc: str = "") -> str:
    """Open the compact Atlas, optionally searching within a known zone.

    Unknown and uncharted zones open the world view. NPC punctuation is retained
    and encoded as query data rather than interpreted as a URL or map selector.
    """
    zone = _name(zone, "zone").replace("\u2018", "'").replace("\u2019", "'")
    npc = _name(npc, "NPC name")
    map_id = _ZONE_MAP_IDS.get(zone_title(zone))
    path = f"/{map_id}/" if map_id else "/"
    params = {"embed": "1"}
    if npc:
        params["find"] = npc
        if map_id:
            params["map"] = map_id
    return ATLAS_ORIGIN + path + "?" + urlencode(params)


def is_atlas_url(url: str) -> bool:
    """Whether a URL has the exact safe HTTPS Atlas origin.

    Internal pages and static resources are allowed, so this can guard browser
    navigation without treating only known map paths as valid Atlas content.
    """
    if (not isinstance(url, str) or not url or len(url) > _MAX_URL
            or "\\" in url or any(char.isspace() for char in url)
            or _has_controls(url) or re.search(r"%(?![0-9a-fA-F]{2})", url)):
        return False
    # urlsplit silently discards some literal controls; check before parsing and
    # reject encoded controls as well rather than letting them reach the browser.
    if _has_controls(unquote(url)):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == _HOST
                and parsed.username is None and parsed.password is None
                and parsed.port in {None, 443}
                and parsed.netloc.casefold() in {_HOST, _HOST + ":443"})
    except (ValueError, UnicodeError):
        return False


def atlas_zone(url: str) -> str | None:
    """Return a canonical zone for a known map path or legacy root map query."""
    if not is_atlas_url(url):
        return None
    parsed = urlsplit(url)
    for map_id, title in _MAP_ZONES.items():
        if parsed.path in {f"/{map_id}", f"/{map_id}/"}:
            return title
    if parsed.path not in {"", "/"}:
        return None
    # Legacy links use /?map=id. Ambiguous repeated parameters are not evidence
    # of a selected zone; the world map also deliberately has no zone title.
    selected = parse_qs(parsed.query).get("map", [])
    return _MAP_ZONES.get(selected[0]) if len(selected) == 1 else None
