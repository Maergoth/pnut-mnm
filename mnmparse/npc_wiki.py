"""Public wiki NPC/item records with a revision-aware, offline disk cache.

Cache reads never use the network. Callers can present those records immediately,
then run ``refresh_*`` on a worker. Refresh checks MediaWiki revision metadata
before requesting rendered content, and never replaces useful data with an error
page, missing page, or a page whose structure could not be understood.
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
from typing import Any, Iterator
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

WIKI = "https://monstersandmemories.miraheze.org"
API = WIKI + "/w/api.php"
USER_AGENT = "PNUT-MnM/1.0 (NPC reference; +https://github.com/Maergoth/pnut-mnm)"
REQUEST_TIMEOUT = 8
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
CACHE_SCHEMA = 1
PARSER_VERSION = 1
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NpcDetails:
    name: str
    level: str = ""
    common_drops: tuple[str, ...] = ()
    unique_drops: tuple[str, ...] = ()
    quests: tuple[str, ...] = ()
    reputation: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class ItemDetails:
    name: str
    lines: tuple[str, ...] = ()
    source: str = ""


@dataclass(frozen=True)
class NotableNpcs:
    zone: str
    names: tuple[str, ...] = ()
    source: str = ""


class WikiDataError(ValueError):
    """The wiki did not supply a recognizable, authoritative record."""


class WikiPageMissing(WikiDataError):
    """MediaWiki explicitly confirmed that the requested page is unpublished."""


def _title(value: str) -> str:
    if not isinstance(value, str):
        raise WikiDataError("A wiki title must be text")
    value = re.sub(r"\s+", " ", value.replace("_", " ")).strip()
    if not value or len(value) > 200 or any(c in value for c in "|\x00\r\n"):
        raise WikiDataError("Invalid wiki title")
    return value


def wiki_url(title: str) -> str:
    return WIKI + "/wiki/" + quote(_title(title).replace(" ", "_"), safe="")


def _api(parameters: dict[str, Any]) -> dict[str, Any]:
    url = API + "?" + urlencode({"format": "json", "formatversion": 2, **parameters})
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        final = urlsplit(response.geturl())
        if final.scheme != "https" or final.hostname != urlsplit(WIKI).hostname:
            raise WikiDataError("Unexpected wiki redirect")
        length = response.headers.get("Content-Length")
        if length is not None and (int(length) < 0 or int(length) > MAX_RESPONSE_BYTES):
            raise WikiDataError("Wiki response exceeds the size limit")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise WikiDataError("Wiki response exceeds the size limit")
    if length is not None and len(payload) != int(length):
        raise WikiDataError("Wiki response was incomplete")
    data = json.loads(payload)
    if not isinstance(data, dict) or "error" in data:
        raise WikiDataError("The wiki could not return this page")
    return data


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[Any]


_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_SKIP = {"script", "style", "nav", "noscript"}
_SKIP_CLASSES = {"mw-editsection", "toc", "navbox", "catlinks", "metadata", "mw-empty-elt"}
_BLOCK = {"p", "div", "li", "tr", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}


class _Tree(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, [])
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {k: v or "" for k, v in attrs}, [])
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _skip(node: _Node) -> bool:
    return node.tag in _SKIP or bool(set(node.attrs.get("class", "").split()) & _SKIP_CLASSES)


def _walk(node: _Node) -> Iterator[_Node]:
    if _skip(node):
        return
    yield node
    for child in node.children:
        if isinstance(child, _Node):
            yield from _walk(child)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _lines(node: _Node) -> list[str]:
    def parts(n: _Node) -> Iterator[str]:
        if _skip(n):
            return
        if n.tag == "br":
            yield "\n"
        elif n.tag in _BLOCK:
            yield "\n"
        for child in n.children:
            if isinstance(child, str):
                yield child
            else:
                yield from parts(child)
        if n.tag in _BLOCK:
            yield "\n"
        elif n.tag in {"td", "th"}:
            yield " "

    return [line for raw in "".join(parts(node)).splitlines() if (line := _clean(raw))]


def _text(node: _Node) -> str:
    return " ".join(_lines(node))


def _content(html: str) -> _Node:
    if not isinstance(html, str) or not html.strip() or len(html.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise WikiDataError("Wiki page content is empty or too large")
    root = _Tree(html).root
    return next((n for n in _walk(root) if "mw-parser-output" in n.attrs.get("class", "").split()), root)


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _field(label: str, parent: str = "") -> str:
    key = _key(label)
    if key in {"level", "levels", "level range", "lvl"}:
        return "level"
    if key in {"common", "unique"} and re.search(r"\b(?:loot|drops?)\b", _key(parent)):
        return key + "_drops"
    if re.search(r"\b(?:loot|drops?|items?)\b", key):
        if "common" in key:
            return "common_drops"
        if "unique" in key:
            return "unique_drops"
    if "quest" in key and not re.search(r"\b(?:dialogue|dialog|rewards?|loot|drops?|items?)\b", key):
        return "quests"
    if re.search(r"\b(?:reputations?|factions?)\b", key):
        return "reputation"
    return ""


_LOOT_SUBHEADINGS = {"weapons", "armor", "armour", "jewelry", "jewellery", "miscellaneous", "other", "items", "consumables", "crafting materials", "quest items"}


def _link_name(node: _Node) -> str:
    href = node.attrs.get("href", "")
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        if parts.scheme != "https" or parts.hostname != urlsplit(WIKI).hostname:
            return ""
    if not parts.path.startswith("/wiki/") or parts.query:
        return ""
    title = unquote(parts.path[6:]).replace("_", " ").strip()
    if not title or ":" in title or len(title) > 200:
        return ""
    return title


def _unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean(raw).strip("•* ")
        if not value or _key(value) in {"none", "none known", "unknown", "n a", "not known", "not yet known", "tbd"}:
            continue
        if len(value) > 600 or len(result) >= 512:
            raise WikiDataError("Wiki data exceeds the supported record size")
        if value.casefold() not in seen:
            result.append(value)
            seen.add(value.casefold())
    return tuple(result)


def _values(node: _Node, *, text: bool = False) -> list[str]:
    anchors = [child for child in _walk(node) if child.tag == "a"]
    links = [name for child in anchors if (name := _link_name(child))]
    if text:
        return _lines(node)
    return links if anchors else _lines(node)


def _blocks(node: _Node, *, boundary_class: str = "") -> Iterator[_Node]:
    """Yield meaningful blocks, optionally marking entry/exit of page variants."""
    if _skip(node):
        return
    boundary = bool(boundary_class and boundary_class in node.attrs.get("class", "").split())
    if boundary:
        yield node
    if re.fullmatch(r"h[1-6]", node.tag) or node.tag in {"p", "li", "tr", "dd", "dt"}:
        yield node
        return
    for child in node.children:
        if isinstance(child, _Node):
            yield from _blocks(child, boundary_class=boundary_class)
    if boundary:
        yield node


def _row(node: _Node) -> tuple[str, list[_Node]]:
    cells = [n for n in node.children if isinstance(n, _Node) and n.tag in {"td", "th"}]
    if len(cells) < 2:
        return "", []
    return _text(cells[0]).rstrip(":"), cells[1:]


def _row_tables(root: _Node) -> dict[int, _Node]:
    """Associate a row with its own table, including nested infobox tables."""
    result: dict[int, _Node] = {}

    def visit(node: _Node, table: _Node | None = None) -> None:
        if node.tag == "table":
            table = node
        elif node.tag == "tr" and table is not None:
            result[id(node)] = table
        for child in node.children:
            if isinstance(child, _Node):
                visit(child, table)

    visit(root)
    return result


def _infobox_field(label: str) -> str:
    """Field labels are finite; item/quest names can contain the same words."""
    key = _key(label)
    if key in {"quest", "quests", "required by quests", "required for quests", "related quests", "quest involvement", "quest requirements"}:
        return "quests"
    if key in {"reputation", "reputations", "reputation change", "reputation changes", "reputation gained", "reputation lost", "faction", "factions", "faction change", "faction changes", "faction hits", "faction gained", "faction lost"}:
        return "reputation"
    if key in {"common loot", "common drops", "common items", "loot common", "drops common"}:
        return "common_drops"
    if key in {"unique loot", "unique drops", "unique items", "loot unique", "drops unique"}:
        return "unique_drops"
    return "level" if key in {"level", "levels", "level range", "lvl"} else ""


def parse_npc(html: str, name: str, source: str = "") -> NpcDetails:
    root = _content(html)
    result: dict[str, list[str]] = {key: [] for key in ("level", "common_drops", "unique_drops", "quests", "reputation")}
    headings: list[tuple[int, str, str]] = []
    row_tables = _row_tables(root)
    name_columns: dict[int, int] = {}
    recognized = False
    for node in _blocks(root, boundary_class="wiki-mob-variant"):
        if "wiki-mob-variant" in node.attrs.get("class", "").split():
            # Each zone variant repeats headings. A final reputation heading
            # must not consume the next variant's stats or later page content.
            headings.clear()
            continue
        if re.fullmatch(r"h[1-6]", node.tag):
            depth, label = int(node.tag[1]), _text(node)
            headings = [h for h in headings if h[0] < depth]
            field = _field(label, " ".join(h[1] for h in headings))
            if not field and _key(label) in _LOOT_SUBHEADINGS and headings and headings[-1][2] in {"common_drops", "unique_drops"}:
                field = headings[-1][2]
            headings.append((depth, label, field))
            recognized |= bool(field)
            continue
        active_label, active_field = (headings[-1][1], headings[-1][2]) if headings else ("", "")
        row_cells = [n for n in node.children if isinstance(n, _Node) and n.tag in {"th", "td"}] if node.tag == "tr" else []
        table = row_tables.get(id(node))
        table_id = id(table) if table is not None else 0
        if row_cells and all(cell.tag == "th" for cell in row_cells):
            labels = {"quest", "quests", "quest name", "name"} if active_field == "quests" else {"item", "items", "item name", "name", "drop", "drops", "loot"}
            name_columns[table_id] = next((i for i, cell in enumerate(row_cells) if _key(_text(cell)) in labels), 0)
            continue
        label, cells = _row(node) if row_cells else ("", [])
        label_field = _infobox_field(label) if cells else ""
        # A section table lists records, even if an item is called "Quest
        # Signet" or "Faction Token". Only an actual infobox, or an unheaded
        # label/value row, may replace the section's meaning.
        table_classes = table.attrs.get("class", "").casefold().split() if table is not None else []
        infobox = any("infobox" in value for value in table_classes) or "mob-stats-box" in table_classes
        labelled_row = bool(label_field and not any(n.tag == "a" for n in _walk(row_cells[0])) and (infobox or (row_cells[0].tag == "th" and not active_field)))
        field = label_field if labelled_row else ""
        if field:
            recognized = True
            active_label = label
            values = [v for cell in cells for v in _values(cell, text=field in {"level", "reputation"})]
        else:
            field = active_field
            if not field:
                # Some pages use a simple paragraph rather than an infobox.
                match = re.match(r"^([^:]{1,60})\s*:\s*(.+)$", _text(node))
                inline_field = _field(match.group(1)) if match else ""
                if match and inline_field:
                    recognized = True
                    result[inline_field].extend(_values(node) if inline_field not in {"level", "reputation"} and any(child.tag == "a" for child in _walk(node)) else [match.group(2)])
                continue
            if row_cells and field in {"common_drops", "unique_drops", "quests"}:
                primary = name_columns.get(table_id)
                if primary is None:
                    primary = next((i for i, cell in enumerate(row_cells) if any(n.tag == "a" and _link_name(n) for n in _walk(cell))), 0)
                values = _values(row_cells[primary]) if primary < len(row_cells) else []
            else:
                values = _values(node, text=field in {"level", "reputation"})
        if field == "reputation":
            values = list(_unique(values))
            direction = "Gained" if re.search(r"gain|increase|positive", active_label, re.I) else "Lost" if re.search(r"lost|loss|decrease|negative", active_label, re.I) else ""
            if direction:
                values = [direction + ": " + value for value in values]
        result[field].extend(values)
    if not result["level"]:
        for line in _lines(root):
            match = re.match(r"^(?:Level(?: range)?|Levels|Lvl)\s*:\s*(.+)$", line, re.I)
            if match:
                result["level"].append(match.group(1))
                recognized = True
    if not recognized:
        raise WikiDataError("NPC page format is not recognized; keeping the cached record")
    levels = _unique(result["level"])
    return NpcDetails(_title(name), " / ".join(levels), **{
        key: _unique(result[key]) for key in ("common_drops", "unique_drops", "quests", "reputation")
    }, source=source or wiki_url(name))


_ITEM_PREFIX = re.compile(
    r"^(?:Slot|Slots|AC|HP|Mana|STA|STR|DEX|AGI|INT|WIS|CHA|END|"
    r"Weapon\s*DMG|Damage|DMG|ATK\s*Delay|Attack\s*Delay|Delay|Skill|"
    r"Effect|Focus|Weight|Size|Class|Classes|Race|Races|Required\s*Level|"
    r"Recommended\s*Level|Level\s*Required|Charges|Range|Resist\w*|"
    r"SV\s*\w+|Fire|Cold|Poison|Disease|Magic)\s*:", re.I,
)
_ITEM_FLAGS = re.compile(r"^(?:(?:MAGIC|LORE|NO\s*DROP|NODROP|NO\s*TRADE|NOTRADE|ATTUNABLE|ARTIFACT|TEMPORARY|QUEST|BIND\s*ON\s*(?:EQUIP|PICKUP))\s*)+$", re.I)


def parse_item(html: str, name: str, source: str = "") -> ItemDetails:
    root = _content(html)
    lines: list[str] = []
    for line in _lines(root):
        if _ITEM_PREFIX.match(line) or _ITEM_FLAGS.fullmatch(line):
            lines.append(line)
        elif lines and re.fullmatch(r"Effect\s*:", lines[-1], re.I):
            lines[-1] += " " + line
    # Infoboxes sometimes put the label and its value in separate cells.
    for row in _walk(root):
        if row.tag == "tr":
            label, cells = _row(row)
            candidate = label + ": " + " ".join(_text(cell) for cell in cells)
            if cells and _ITEM_PREFIX.match(candidate) and candidate not in lines:
                lines.append(candidate)
    values = _unique(lines)
    if not values:
        raise WikiDataError("Item page format is not recognized; keeping the cached record")
    return ItemDetails(_title(name), values, source or wiki_url(name))


def _notable_heading(label: str) -> bool:
    key = _key(label)
    return bool(re.search(r"\b(?:notable|named|rare)\b", key) and re.search(r"\b(?:npcs?|mobs?|monsters?|creatures?|enemies)\b", key))


def parse_notables(html: str, zone: str, source: str = "") -> NotableNpcs:
    root = _content(html)
    names: list[str] = []
    active_depth = 0
    recognized = False
    name_column = 0
    row_tables = _row_tables(root)
    notable_columns: dict[int, int] = {}
    # Zone pages also use a labelled row inside nested layout/infobox tables.
    # Visit every row, since _blocks deliberately stops at an enclosing row.
    for node in _walk(root):
        if node.tag != "tr":
            continue
        row_cells = [n for n in node.children if isinstance(n, _Node) and n.tag in {"td", "th"}]
        table_id = id(row_tables.get(id(node)))
        if row_cells and all(cell.tag == "th" for cell in row_cells):
            column = next((i for i, cell in enumerate(row_cells) if _notable_heading(_text(cell))), None)
            if column is not None:
                notable_columns[table_id] = column
                recognized = True
            else:
                notable_columns.pop(table_id, None)
            continue
        if table_id in notable_columns:
            column = notable_columns[table_id]
            if column < len(row_cells):
                names.extend(_values(row_cells[column]))
            continue
        label, cells = _row(node)
        first = row_cells[0] if row_cells else None
        if cells and first is not None and not any(n.tag == "table" for n in _walk(first)) and _notable_heading(label):
            recognized = True
            names.extend(value for cell in cells for value in _values(cell))
    for node in _blocks(root):
        if re.fullmatch(r"h[1-6]", node.tag):
            depth = int(node.tag[1])
            if active_depth and depth <= active_depth:
                active_depth = 0
            if _notable_heading(_text(node)):
                active_depth, recognized = depth, True
                name_column = 0
            continue
        if not active_depth:
            continue
        if node.tag == "tr":
            cells = [n for n in node.children if isinstance(n, _Node) and n.tag in {"td", "th"}]
            headers = [_key(_text(cell)) for cell in cells]
            if cells and all(cell.tag == "th" for cell in cells):
                if any(re.fullmatch(r"common (?:npcs?|mobs?|monsters?|creatures?|enemies)", value) for value in headers):
                    name_column = -1
                else:
                    name_column = next((i for i, value in enumerate(headers) if _notable_heading(value) or value in {"name", "npc", "npc name", "monster", "mob"}), 0)
                continue
            if cells and 0 <= name_column < len(cells):
                names.extend(_values(cells[name_column]))
        elif node.tag == "li":
            # A notable-NPC bullet can also mention its location or loot.
            names.extend(_values(node)[:1])
        else:
            names.extend(_values(node))
    if not recognized:
        raise WikiDataError("Zone notable-NPC section is not recognized; keeping the cached list")
    return NotableNpcs(_title(zone), _unique(names), source or wiki_url(zone))


_KINDS = {"npc": NpcDetails, "item": ItemDetails, "notables": NotableNpcs}
_PARSERS = {"npc": parse_npc, "item": parse_item, "notables": parse_notables}


class WikiRepository:
    """Cache-first access to public NPC, item and zone-reference pages."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def _path(self, kind: str, title: str) -> Path:
        digest = hashlib.sha256(_title(title).casefold().encode("utf-8")).hexdigest()
        return self.cache_dir / (kind + "-" + digest + ".json")

    def _cached(self, kind: str, title: str) -> tuple[Any, dict[str, Any]] | None:
        try:
            path = self._path(kind, title)
            if path.stat().st_size > MAX_RESPONSE_BYTES:
                return None
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(envelope, dict) or envelope.get("schema") != CACHE_SCHEMA or envelope.get("kind") != kind:
                return None
            data = envelope["data"]
            if not isinstance(data, dict):
                return None
            text_keys = {"name", "source", "level"} if kind == "npc" else {"name", "source"} if kind == "item" else {"zone", "source"}
            tuple_keys = {"common_drops", "unique_drops", "quests", "reputation"} if kind == "npc" else {"lines"} if kind == "item" else {"names"}
            if set(data) != text_keys | tuple_keys:
                return None
            if any(not isinstance(data[key], str) or len(data[key]) > 2000 for key in text_keys):
                return None
            for key in tuple_keys:
                if not isinstance(data[key], list) or len(data[key]) > 512 or any(not isinstance(v, str) or len(v) > 600 for v in data[key]):
                    return None
                data[key] = tuple(data[key])
            source = urlsplit(data["source"])
            if source.scheme != "https" or source.hostname != urlsplit(WIKI).hostname or not source.path.startswith("/wiki/"):
                return None
            if not isinstance(envelope.get("revision"), str) or not isinstance(envelope.get("title"), str):
                return None
            return _KINDS[kind](**data), envelope
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _save(self, kind: str, requested: str, title: str, revision: str, details: Any) -> None:
        data = json.dumps({"schema": CACHE_SCHEMA, "parser_version": PARSER_VERSION, "kind": kind, "title": title, "revision": revision, "data": asdict(details)}, ensure_ascii=False).encode("utf-8")
        for key in dict.fromkeys((requested, title)):
            temporary = None
            try:
                path = self._path(kind, key)
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data)
                os.replace(temporary, path)
            except OSError:
                log.warning("Could not save wiki reference cache", exc_info=True)
            finally:
                if temporary is not None:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        log.debug("Could not remove temporary wiki cache file", exc_info=True)

    @staticmethod
    def _revision(requested: str, kind: str) -> tuple[str, int, str]:
        title = _title(requested)
        candidates = [title]
        # /consider includes a/an even where the wiki omits it. Resolve exact
        # candidates through MediaWiki, never a fuzzy match to another monster.
        if kind == "npc" and re.match(r"^(?:a|an)\s+", title, re.I):
            candidates.append(re.sub(r"^(?:a|an)\s+", "", title, flags=re.I))
        data = _api({"action": "query", "prop": "info|revisions", "titles": "|".join(candidates), "redirects": 1, "rvprop": "ids", "rvlimit": 1})
        query = data.get("query", {})
        if not isinstance(query, dict):
            raise WikiDataError("Wiki revision metadata is malformed")
        pages = query.get("pages", [])
        if isinstance(pages, dict):
            pages = list(pages.values())
        if not isinstance(pages, list):
            raise WikiDataError("Wiki revision metadata is malformed")
        aliases: dict[str, str] = {}
        for key in ("normalized", "redirects"):
            for alias in query.get(key, []):
                if isinstance(alias, dict) and isinstance(alias.get("from"), str) and isinstance(alias.get("to"), str):
                    aliases[alias["from"].casefold()] = alias["to"]
        missing = 0
        for candidate in candidates:
            seen: set[str] = set()
            while candidate.casefold() in aliases and candidate.casefold() not in seen:
                seen.add(candidate.casefold())
                candidate = aliases[candidate.casefold()]
            page = next((p for p in pages if isinstance(p, dict) and isinstance(p.get("title"), str) and p["title"].casefold() == candidate.casefold()), None)
            if page is None:
                continue
            if "missing" in page:
                missing += 1
                continue
            if "invalid" in page:
                raise WikiDataError("The wiki could not resolve this page title")
            revisions = page.get("revisions", [])
            revision = revisions[0].get("revid") if isinstance(revisions, list) and revisions and isinstance(revisions[0], dict) else None
            if page.get("ns", 0) != 0 or isinstance(revision, bool) or not isinstance(revision, int) or revision <= 0:
                raise WikiDataError("Wiki revision metadata is malformed")
            # touched also changes when transcluded loot/stat templates change.
            touched = page.get("touched", "")
            if not isinstance(touched, str) or len(touched) > 64:
                raise WikiDataError("Wiki revision metadata is malformed")
            return _title(page["title"]), revision, str(revision) + ":" + touched
        if missing == len(candidates):
            raise WikiPageMissing("This NPC, item or zone is not published on the wiki")
        raise WikiDataError("Wiki revision metadata did not identify the requested page")

    def _refresh(self, kind: str, requested: str) -> Any:
        requested = _title(requested)
        title, revision, fingerprint = self._revision(requested, kind)
        requested_cache = self._cached(kind, requested)
        cached = requested_cache or self._cached(kind, title)
        if cached and cached[1]["title"] == title and cached[1]["revision"] == fingerprint and cached[1].get("parser_version", 0) == PARSER_VERSION:
            if requested_cache is None:
                self._save(kind, requested, title, fingerprint, cached[0])
            return cached[0]
        # The current-page route is available even when the wiki challenges
        # oldid requests. Ask for revid explicitly and reject any edit racing
        # the metadata lookup rather than caching it under the wrong revision.
        data = _api({"action": "parse", "page": title, "prop": "text|revid", "disableeditsection": 1})
        parsed = data.get("parse", {})
        if not isinstance(parsed, dict) or parsed.get("revid") != revision or parsed.get("title") != title:
            raise WikiDataError("Wiki page changed during loading; keeping the cached record")
        html = parsed.get("text")
        if isinstance(html, dict):
            html = html.get("*")
        details = _PARSERS[kind](html, title, wiki_url(title))
        self._save(kind, requested, title, fingerprint, details)
        return details

    def cached_npc(self, name: str) -> NpcDetails | None:
        cached = self._cached("npc", name)
        if cached is None and isinstance(name, str) and re.match(r"^(?:a|an)\s+", name, re.I):
            cached = self._cached("npc", re.sub(r"^(?:a|an)\s+", "", name, flags=re.I))
        return cached[0] if cached else None

    def refresh_npc(self, name: str) -> NpcDetails:
        return self._refresh("npc", name)

    def cached_item(self, name: str) -> ItemDetails | None:
        cached = self._cached("item", name)
        return cached[0] if cached else None

    def refresh_item(self, name: str) -> ItemDetails:
        return self._refresh("item", name)

    def cached_notables(self, zone: str) -> NotableNpcs | None:
        cached = self._cached("notables", zone)
        return cached[0] if cached else None

    def refresh_notables(self, zone: str) -> NotableNpcs:
        return self._refresh("notables", zone)


__all__ = ["NpcDetails", "ItemDetails", "NotableNpcs", "WikiRepository", "WikiDataError", "WikiPageMissing", "parse_npc", "parse_item", "parse_notables", "wiki_url"]
