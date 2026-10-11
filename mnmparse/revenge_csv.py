"""Names-only CSV exchange with legacy typed-list import support."""
from __future__ import annotations

import csv
from dataclasses import dataclass
import io
from itertools import chain
from typing import Iterable

MAX_ENTRIES = 1000
MAX_NAME_CHARS = 32767
MAX_CSV_BYTES = 8 * 1024 * 1024
KINDS = ("Player", "Guild")


class RevengeCsvError(ValueError):
    """CSV cannot be imported completely without changing its meaning."""


@dataclass(frozen=True)
class CsvEntry:
    name: str
    kind: str = "Player"


def _check_size(text: str) -> None:
    if not isinstance(text, str):
        raise RevengeCsvError("Paste CSV text to import.")
    try:
        if len(text) > MAX_CSV_BYTES or len(text.encode("utf-8")) > MAX_CSV_BYTES:
            raise RevengeCsvError("CSV exceeds the 8 MB limit.")
    except UnicodeError as exc:
        raise RevengeCsvError("CSV contains invalid text.") from exc


def validate_entries(entries: Iterable[CsvEntry]) -> tuple[CsvEntry, ...]:
    """Validate the complete batch before a caller changes its saved list."""
    checked = []
    total_bytes = 0
    for entry in entries:
        if len(checked) >= MAX_ENTRIES:
            raise RevengeCsvError("Import at most 1,000 entries at a time.")
        if not isinstance(entry, CsvEntry) or entry.kind not in KINDS:
            raise RevengeCsvError("Each entry needs a Player or Guild type.")
        if not isinstance(entry.name, str) or not entry.name.strip() or len(entry.name) > MAX_NAME_CHARS:
            raise RevengeCsvError("Each entry needs a nonempty name within the text field limit.")
        try:
            total_bytes += len(entry.name.encode("utf-8"))
        except UnicodeError as exc:
            raise RevengeCsvError("An entry contains invalid text.") from exc
        if total_bytes > MAX_CSV_BYTES:
            raise RevengeCsvError("CSV exceeds the 8 MB limit.")
        checked.append(entry)
    return tuple(checked)


def parse_csv(text: str, *, default_kind: str = "Player") -> tuple[CsvEntry, ...]:
    """Read comma-separated names for one list, or legacy header-based CSV.

    Names are literal text. Quoted commas, quotes, Unicode and embedded newlines
    survive a round trip; no player-name detection rules apply to imports.
    """
    _check_size(text)
    if default_kind not in KINDS:
        raise RevengeCsvError("Choose a Player or Guild list.")
    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        rows = (row for row in reader if row)
        first = next(rows, None)
        if first is None:
            raise RevengeCsvError("CSV is empty. Paste comma-separated names.")
        fields = tuple(value.strip().casefold() for value in first)
        if fields in (("type", "name"), ("name", "type"), ("name",)):
            following = next(rows, None)
            if following is not None:
                rows = chain((following,), rows)
            else:
                # A lone row can literally contain players called Type or Name.
                fields = ()
                rows = iter((first,))
        else:
            fields = ()
            rows = chain((first,), rows)
        entries = []
        for row in rows:
            if len(entries) + (1 if fields else len(row)) > MAX_ENTRIES:
                raise RevengeCsvError("Import at most 1,000 entries at a time.")
            if fields:
                if len(row) != len(fields):
                    raise RevengeCsvError(f"CSV row {reader.line_num} does not match its header.")
                kind = row[fields.index("type")].strip().title() if "type" in fields else default_kind
                if kind not in KINDS:
                    raise RevengeCsvError(f"CSV row {reader.line_num} needs a Player or Guild type.")
                entries.append(CsvEntry(row[fields.index("name")], kind))
            else:
                entries.extend(CsvEntry(name.strip(), default_kind) for name in row)
        return validate_entries(entries)
    except csv.Error as exc:
        raise RevengeCsvError(f"CSV row {reader.line_num} could not be read: {exc}.") from exc


def format_csv(entries: Iterable[CsvEntry]) -> str:
    """Return one comma-separated row of names for the caller's chosen list."""
    checked = validate_entries(entries)
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(entry.name for entry in checked)
    text = stream.getvalue()
    _check_size(text)
    return text


def parse_names(text: str, *, kind: str = "Player") -> tuple[CsvEntry, ...]:
    """Read an ordinary clipboard list with one literal name on each line."""
    _check_size(text)
    if kind not in KINDS:
        raise RevengeCsvError("Choose a Player or Guild list.")
    return validate_entries(CsvEntry(name.strip(), kind) for name in text.splitlines() if name.strip())


__all__ = ["CsvEntry", "RevengeCsvError", "parse_csv", "parse_names", "format_csv", "validate_entries"]
