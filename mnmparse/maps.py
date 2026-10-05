"""Read zone maps from the community wiki, with an offline disk cache.

Only public wiki pages and their linked map images are requested. Combat lines and
player names never leave the app. Network work is run off the GUI thread.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)
WIKI = "https://monstersandmemories.miraheze.org"
USER_AGENT = "MnMZoneMaps/1.0 (community wiki map viewer)"
MAX_DOWNLOAD = 24 * 1024 * 1024
# Zone titles from Category:Zones. Editable selection also accepts newly added zones.
ZONES = (
    "Ail'Vorith", "Ancient Crypt", "Blacktide Bay", "Blind Midden", "Caves of Irem",
    "Evershade Weald", "Faelindral", "Fallen Pass", "Fallen Watch", "Glass Flats",
    "Glinting Hollow", "Grain Cellar", "Great Cavern Sea", "Grimtide Sanctum",
    "Infested Crypt", "Keeper's Bight", "Night Harbor", "Night Harbor Sewers",
    "Rothold", "Scarwood", "Shaded Dunes", "Shallow Shoals", "Sungreet Strand",
    "Sunken Resort", "Tel Ekir", "The Hidden Grotto", "Tomb of the Last Wyrmsbane",
    "Underdocks", "Vale of Zintar",
)
ALIASES = {"wyrmsbane tomb": "Tomb of the Last Wyrmsbane", "wyrmsbane": "Tomb of the Last Wyrmsbane"}


def zone_title(zone: str) -> str:
    """Log zone variants such as Night Harbor (West) share their wiki map."""
    title = re.sub(r"\s+\((?:north|south|east|west)\)$", "", zone.strip(), flags=re.I)
    title = title.replace("_", " ").rstrip(".").strip()
    return ALIASES.get(title.casefold(), next((z for z in ZONES if z.casefold() == title.casefold()), title))


def wiki_url(zone: str) -> str:
    return WIKI + "/wiki/" + quote(zone_title(zone).replace(" ", "_"), safe="")


@dataclass(frozen=True)
class MapImage:
    title: str
    url: str
    source: str


def _allowed_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in {
        "monstersandmemories.miraheze.org", "static.wikitide.net", "static.miraheze.org"
    }


def _original_image(url: str) -> str:
    """MediaWiki thumbnails link to originals; keep all labels legible when zooming."""
    parts = urlsplit(url)
    path = parts.path
    if "/thumb/" in path:
        path = path.replace("/thumb/", "/", 1).rsplit("/", 1)[0]
    return parts._replace(path=path, query="", fragment="").geturl()


class _MapParser(HTMLParser):
    """Wiki zone maps use bordered figures, map captions or Map sections.

    Deliberately exclude overview photographs and item icons, even on mapless pages.
    """
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.maps: list[MapImage] = []
        self.figure: dict | None = None
        self.heading: list[str] | None = None
        self.in_map_section = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = []
        if tag == "figure":
            self.figure = {"border": "mw-image-border" in (a.get("class") or ""), "text": [], "url": "", "file": ""}
        if self.figure is not None:
            if tag == "a" and "/wiki/File:" in (a.get("href") or ""):
                self.figure["file"] = unquote((a["href"] or "").split("File:", 1)[1]).replace("_", " ")
            if tag == "img":
                self.figure["url"] = urljoin(WIKI, a.get("src") or "")
                self.figure["text"].append(a.get("alt") or "")

    def handle_data(self, data: str) -> None:
        if self.heading is not None:
            self.heading.append(data)
        if self.figure is not None:
            self.figure["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            self.in_map_section = bool(re.search(r"\bmaps?\b", "".join(self.heading), re.I))
            self.heading = None
        if tag != "figure" or self.figure is None:
            return
        f, self.figure = self.figure, None
        caption = " ".join(f["text"]).strip()
        is_map = f["border"] or self.in_map_section or re.search(r"\bmap\b|isometric", caption + " " + f["file"], re.I)
        url = _original_image(f["url"])
        if is_map and _allowed_url(url) and urlsplit(url).path.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
            name = caption or re.sub(r"\.(?:png|jpe?g|webp)$", "", f["file"], flags=re.I)
            if url not in {m.url for m in self.maps}:
                source = WIKI + "/wiki/File:" + quote(f["file"].replace(" ", "_"), safe="") if f["file"] else self.source
                self.maps.append(MapImage(name or "Map", url, source))


def parse_maps(html: str, zone: str) -> list[MapImage]:
    parser = _MapParser(wiki_url(zone))
    parser.feed(html)
    return parser.maps


def _download(url: str) -> bytes:
    if not _allowed_url(url):
        raise ValueError("Map URL is not on the community wiki")
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=15) as response:
        if not _allowed_url(response.geturl()):
            raise ValueError("Unexpected map redirect")
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Map exceeds the 24 MB download limit")
    return data


class MapRepository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)

    def _path(self, key: str, suffix: str) -> Path:
        return self.cache_dir / (hashlib.sha256(key.encode()).hexdigest() + suffix)

    def _save(self, path: Path, data: bytes, *, strict: bool = False) -> None:
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
            os.replace(temporary, path)
        except OSError:
            log.warning("Could not cache map at %s", path, exc_info=True)
            if strict:
                raise
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove temporary map download at %s", temporary)

    def save_maps(self, zone: str, entries: list[MapImage], *, strict: bool = False) -> None:
        """Commit a map list after its images are available in the cache."""
        title = zone_title(zone)
        if not title:
            return
        path = self._path(title, ".json")
        # Do not persist a no-map result forever: maps are still being added.
        if entries:
            self._save(path, json.dumps([asdict(m) for m in entries]).encode(), strict=strict)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove obsolete map manifest at %s", path)
                if strict:
                    raise

    def maps(self, zone: str, *, refresh: bool = False, strict: bool = False,
             persist: bool = True) -> list[MapImage]:
        """Load a zone's map list; strict refreshes surface network/cache failures.

        Normal viewing can fall back to an older cache while offline. An explicit
        download needs to distinguish a fresh copy from that fallback. Bulk
        downloads stage lists with persist=False until all their images succeed.
        """
        title = zone_title(zone)
        if not title:
            return []
        path = self._path(title, ".json")
        cached = None
        try:
            cached = [MapImage(**m) for m in json.loads(path.read_text(encoding="utf-8"))]
            if any(not _allowed_url(m.url) or not _allowed_url(m.source) for m in cached):
                cached = None
        except (OSError, ValueError, TypeError):
            pass
        if cached is not None and not refresh:
            return cached
        url = WIKI + "/w/api.php?" + urlencode({"action": "parse", "page": title.replace(" ", "_"), "prop": "text", "format": "json", "redirects": 1})
        try:
            data = json.loads(_download(url))
            if "error" in data:
                raise ValueError(data["error"].get("info", "Zone page not found"))
            text = data["parse"]["text"]
            maps = parse_maps(text["*"] if isinstance(text, dict) else text, title)
            if persist:
                self.save_maps(title, maps, strict=strict)
            return maps
        except Exception:
            if cached is not None and not strict:
                return cached
            raise

    def image(self, entry: MapImage, *, refresh: bool = False, strict: bool = False) -> bytes:
        path = self._path(entry.url, ".image")
        cached = None
        try:
            if path.stat().st_size <= MAX_DOWNLOAD:
                cached = path.read_bytes() or None
        except OSError:
            pass
        if cached is not None and not refresh:
            return cached
        try:
            data = _download(entry.url)
        except Exception:
            if cached is not None and not strict:
                return cached
            raise
        self._save(path, data, strict=strict)
        return data
