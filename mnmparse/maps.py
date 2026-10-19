"""Read zone maps from the community wiki, with an offline disk cache.

Only public wiki pages and their linked map images are requested. Combat lines and
player names never leave the app. Network work is run off the GUI thread.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from collections.abc import Iterable
import hashlib
from http.client import HTTPException
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
from queue import Empty, Queue
import re
import tempfile
from threading import Event, Lock, Thread
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from .map_catalog import MAP_CATALOG

log = logging.getLogger(__name__)
WIKI = "https://monstersandmemories.miraheze.org"
USER_AGENT = "PNUT-MnM/1.0 (community map viewer; +https://github.com/Maergoth/pnut-mnm)"
API_COOLDOWN = 30.0
_TRANSPORT_ERRORS = (OSError, HTTPException)
# The Ancient Crypt original is about 40 MB. Keep full-resolution maps bounded
# without rejecting this legitimate map on every startup.
MAX_DOWNLOAD = 64 * 1024 * 1024
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
    # OCR and in-game identifiers can collapse a title's spaces (FallenPass).
    # Match only exact known titles after spacing normalization, never a fuzzy
    # guess that could choose a different zone.
    key = re.sub(r"\s+", "", title).casefold()
    return ALIASES.get(title.casefold(), next((z for z in ZONES if re.sub(r"\s+", "", z).casefold() == key), title))


_WHO_ZONE_RX = re.compile(
    r"^\s*There\s+(?:are|is)\s+\d+\s+players?\s+in\s+(.+?)\s+who\s+match\s+your\s+search[.!?]*\s*$",
    re.IGNORECASE,
)


def zone_from_who(text: str) -> str:
    """Read the zone in a complete local /who summary without exposing players.

    This is navigation evidence, not a zone transition: the parser keeps the
    line's personal classification and an ongoing fight continues unchanged.
    Ignore global/unknown search results and quoted chat containing the phrase.
    """
    match = _WHO_ZONE_RX.fullmatch(str(text or ""))
    if match is None:
        return ""
    title = zone_title(match.group(1))
    return title if title in ZONES else ""


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
        if unquote(urlsplit(url).path.rsplit("/", 1)[-1]).casefold() == "phformaps.png":
            return  # The wiki's shared "map coming soon" placeholder is not a zone map.
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
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > MAX_DOWNLOAD:
            raise ValueError("Map exceeds the 64 MB download limit")
        data = response.read(MAX_DOWNLOAD + 1)
        if length is not None and len(data) != int(length):
            raise ValueError("Map download was incomplete")
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Map exceeds the 64 MB download limit")
    return data


def _head_headers(url: str) -> dict[str, str]:
    """Read current CDN metadata without transferring the map image."""
    if not _allowed_url(url):
        raise ValueError("Map URL is not on the community wiki")
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urlopen(request, timeout=15) as response:
        if not _allowed_url(response.geturl()):
            raise ValueError("Unexpected map redirect")
        headers = {key.lower(): value for key, value in response.headers.items()}
        length = headers.get("content-length")
        if length is not None and (int(length) < 0 or int(length) > MAX_DOWNLOAD):
            raise ValueError("Map exceeds the 64 MB download limit")
        return headers


def _cdn_sha1(url: str) -> str | None:
    """WikiTide exposes the uploaded file's SHA-1 in base 36 on its CDN."""
    if urlsplit(url).hostname not in {"static.wikitide.net", "static.miraheze.org"}:
        return None
    encoded = _head_headers(url).get("x-object-meta-sha1base36", "")
    # A SHA-1 fits in 31 base-36 digits. Bound both parsing and the resulting
    # integer; a malformed header must never validate local image content.
    if not isinstance(encoded, str) or not re.fullmatch(r"[0-9a-zA-Z]{1,31}", encoded):
        return None
    value = int(encoded, 36)
    return f"{value:040x}" if value < 1 << 160 else None


def _cdn_hashes(urls: Iterable[str], cancelled: Event) -> dict[str, str]:
    """Check at most four media files concurrently, without delaying shutdown."""
    pending = Queue()
    for url in urls:
        pending.put(url)
    hashes: dict[str, str] = {}
    results_lock = Lock()

    def check_files() -> None:
        while not cancelled.is_set():
            try:
                image_url = pending.get_nowait()
            except Empty:
                return
            if cancelled.is_set():
                return
            try:
                digest = _cdn_sha1(image_url)
            except (OSError, HTTPException, ValueError):
                # One unavailable file must not discard other verified hashes.
                # The caller can download this image normally.
                log.warning("Could not check current map metadata at %s", image_url, exc_info=True)
                continue
            if digest is not None:
                with results_lock:
                    hashes[image_url] = digest

    workers = [Thread(target=check_files, name="map-cdn-metadata", daemon=True)
               for _ in range(min(4, pending.qsize()))]
    for worker in workers:
        worker.start()
    for worker in workers:
        while worker.is_alive() and not cancelled.is_set():
            worker.join(0.1)
    # On cancellation, in-flight HEADs finish on daemon threads. Return a copy
    # so their late results cannot mutate the caller's completed snapshot.
    with results_lock:
        return dict(hashes)


class MapRepository:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self._api_lock = Lock()
        self._api_retry_at = 0.0

    def _wiki_download(self, url: str) -> bytes:
        """Serialize discovery and pause briefly after a blocked/unavailable API."""
        with self._api_lock:
            if monotonic() < self._api_retry_at:
                raise URLError("Community wiki temporarily unavailable; map discovery will retry shortly")
            try:
                return _download(url)
            except HTTPError as exc:
                if exc.code in {403, 429} or 500 <= exc.code < 600:
                    self._api_retry_at = monotonic() + API_COOLDOWN
                raise
            except _TRANSPORT_ERRORS:
                self._api_retry_at = monotonic() + API_COOLDOWN
                raise

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
        # Remember an authoritative empty result too, so an older bundled map
        # cannot reappear while offline. Normal viewing still rechecks empty lists.
        self._save(path, json.dumps([asdict(m) for m in entries]).encode(), strict=strict)

    def _cached_maps(self, title: str) -> list[MapImage] | None:
        try:
            rows = json.loads(self._path(title, ".json").read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                return None
            entries = [MapImage(**row) for row in rows]
            if any(not isinstance(value, str) for entry in entries
                   for value in (entry.title, entry.url, entry.source)):
                return None
            if any(not _allowed_url(entry.url) or not _allowed_url(entry.source) for entry in entries):
                return None
            return entries
        except (OSError, ValueError, TypeError):
            return None

    def known_maps(self, zone: str) -> list[MapImage] | None:
        """Return saved discovery or bundled wiki links, without network access.

        None means the zone has never been checked. An empty list is a known
        mapless zone; neither result claims that live discovery succeeded.
        """
        title = zone_title(zone)
        cached = self._cached_maps(title)
        if cached is not None:
            return cached
        rows = MAP_CATALOG.get(title)
        return [MapImage(*row) for row in rows] if rows is not None else None

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
        cached = self._cached_maps(title)
        if cached and not refresh:
            return cached
        url = WIKI + "/w/api.php?" + urlencode({"action": "parse", "page": title.replace(" ", "_"), "prop": "text", "format": "json", "redirects": 1})
        try:
            data = json.loads(self._wiki_download(url))
            if "error" in data:
                raise ValueError(data["error"].get("info", "Zone page not found"))
            text = data["parse"]["text"]
            maps = parse_maps(text["*"] if isinstance(text, dict) else text, title)
            if persist:
                self.save_maps(title, maps, strict=strict)
            return maps
        except _TRANSPORT_ERRORS:
            known = self.known_maps(title) if not strict else None
            if known is not None:
                return known
            raise

    def image_hashes(self, entries: Iterable[MapImage], *, cancelled: Event | None = None) -> dict[str, str]:
        """Check current file hashes in small API batches, without image transfers.

        Compare actual cached content, so caches from older versions need no
        migration. A page revision cannot detect a replacement at the same image
        URL; imageinfo reports the uploaded file's own hash instead. Cancellation
        returns the hashes collected so far without waiting for CDN HEAD timeouts.
        """
        cancelled = cancelled if cancelled is not None else Event()
        if cancelled.is_set():
            return {}
        titles = {}
        for entry in entries:
            if _allowed_url(entry.url):
                filename = unquote(urlsplit(entry.url).path.rsplit("/", 1)[-1])
                if filename:
                    titles["File:" + filename.replace("_", " ")] = entry.url
        hashes = {}
        cdn_urls = []
        names = list(titles)
        for start in range(0, len(names), 50):
            if cancelled.is_set():
                break
            url = WIKI + "/w/api.php?" + urlencode({
                "action": "query", "prop": "imageinfo", "iiprop": "sha1|url",
                "titles": "|".join(names[start:start + 50]), "format": "json",
            })
            try:
                raw = self._wiki_download(url)
            except _TRANSPORT_ERRORS:
                # The public media CDN remains available during many wiki API
                # challenges. A HEAD request checks actual current image bytes,
                # including same-URL replacements and caches from older builds.
                cdn_urls.extend(titles[name] for name in names[start:start + 50])
                continue
            payload = json.loads(raw)
            if "error" in payload:
                raise ValueError(payload["error"].get("info", "Could not check map versions"))
            for page in payload["query"]["pages"].values():
                info = page.get("imageinfo", [])
                if not info:
                    continue
                remote = info[0]
                digest = remote.get("sha1", "")
                image_url = remote.get("url", "")
                # Require a matching original URL: a similarly named file from
                # another repository must never validate this cached image.
                if (isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{40}", digest)
                        and isinstance(image_url, str) and _allowed_url(image_url)):
                    for name in names[start:start + 50]:
                        local_url = titles[name]
                        if unquote(image_url) == unquote(local_url):
                            hashes[local_url] = digest.lower()
        if cdn_urls and not cancelled.is_set():
            hashes.update(_cdn_hashes(cdn_urls, cancelled))
        return hashes

    def _cached_image(self, entry: MapImage) -> bytes | None:
        path = self._path(entry.url, ".image")
        try:
            if path.stat().st_size <= MAX_DOWNLOAD:
                return path.read_bytes() or None
        except OSError:
            pass
        return None

    def update_image(self, entry: MapImage, *, sha1: str | None = None) -> bool:
        """Reuse a matching image or download and atomically cache its replacement.

        Return whether content changed. If version metadata is unavailable, a
        full request still detects updates; failures preserve the offline cache.
        """
        cached = self._cached_image(entry)
        if sha1 is not None and cached is not None and hashlib.sha1(cached).hexdigest() == sha1:
            return False
        data = _download(entry.url)
        if not data:
            raise ValueError("Map image download was empty")
        if sha1 is not None and hashlib.sha1(data).hexdigest() != sha1:
            raise ValueError("Map image does not match the wiki's current version. Try again later.")
        if data == cached:
            return False
        self._save(self._path(entry.url, ".image"), data, strict=True)
        return True

    def image(self, entry: MapImage, *, refresh: bool = False, strict: bool = False) -> bytes:
        path = self._path(entry.url, ".image")
        cached = self._cached_image(entry)
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
