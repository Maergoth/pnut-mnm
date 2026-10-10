"""Read, merge and share timer definitions without changing local audio settings.

The public bundle contains only triggers.  Older ``triggers.json`` stores, a list
of triggers, and an individual trigger are accepted too.  External sound files
remain references; importing a bundle never opens or copies those files.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable
import uuid

from .triggers import ACTIONS, BUILTIN_SOUNDS, MODES, TIMER_MODES, Trigger
from .privacy import safe_trigger_definition


FORMAT = "pnut-timers"
VERSION = 1
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TRIGGERS = 5000
# The editor uses Qt integer spin boxes for some time values.
MAX_SECONDS = 2 ** 31 - 1
_LEGACY_MODES = {"restart": "replace", "ignore": "retain"}
_BOOLEAN_FIELDS = {"enabled", "fuzzy", "timer"}
_NUMBER_FIELDS = {"volume", "cooldown_s", "timer_seconds", "timer_warn_s", "timer_low_s"}
_COLOR_FIELDS = {"timer_color", "timer_warn_color", "timer_low_color"}
_FIELDS = {field.name for field in fields(Trigger)}
_ENUM_FIELDS = {
    "mode": MODES,
    "action": ACTIONS,
    "timer_warn_action": ACTIONS,
    "timer_end_action": ACTIONS,
    "timer_mode": (*TIMER_MODES, *_LEGACY_MODES),
    "sound": BUILTIN_SOUNDS,
    "timer_warn_sound": BUILTIN_SOUNDS,
    "timer_end_sound": BUILTIN_SOUNDS,
}


class TriggerExchangeError(ValueError):
    """An invalid bundle or a file that could not safely be read or written."""


@dataclass(frozen=True)
class MergeResult:
    triggers: list[Trigger]
    added: int
    skipped: int
    conflicts: int


def _validate_trigger(data: object, index: int) -> Trigger:
    prefix = f"Timer {index}"
    if not isinstance(data, dict):
        raise TriggerExchangeError(f"{prefix} must be a JSON object.")
    if isinstance(data.get("name"), str) and data["name"].strip():
        prefix += f" ({data['name']})"
    unknown = set(data) - _FIELDS
    if unknown:
        raise TriggerExchangeError(f"{prefix} has unsupported fields: {', '.join(sorted(unknown))}.")
    if "pattern" not in data:
        raise TriggerExchangeError(f"{prefix} is missing its pattern.")
    for name, value in data.items():
        if name in _BOOLEAN_FIELDS:
            if type(value) is not bool:
                raise TriggerExchangeError(f"{prefix}: {name} must be true or false.")
        elif name in _NUMBER_FIELDS:
            if type(value) not in (int, float):
                raise TriggerExchangeError(f"{prefix}: {name} must be a finite number.")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise TriggerExchangeError(f"{prefix}: {name} must be a finite number.")
            minimum = 1 if name == "timer_seconds" else 0
            if value < minimum:
                raise TriggerExchangeError(f"{prefix}: {name} must be at least {minimum}.")
            if name == "volume" and (value > 100 or value != int(value)):
                raise TriggerExchangeError(f"{prefix}: volume must be a whole number from 0 to 100.")
            if name != "volume" and value > MAX_SECONDS:
                raise TriggerExchangeError(f"{prefix}: {name} must not exceed {MAX_SECONDS} seconds.")
        elif not isinstance(value, str):
            raise TriggerExchangeError(f"{prefix}: {name} must be text.")
        if name in _ENUM_FIELDS and value not in _ENUM_FIELDS[name]:
            raise TriggerExchangeError(f"{prefix}: unsupported {name} {value!r}.")
        if name in _COLOR_FIELDS and value and not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise TriggerExchangeError(f"{prefix}: {name} must be empty or a color such as #33aa77.")
    trigger = Trigger.from_dict(data)
    if not trigger.name.strip():
        raise TriggerExchangeError(f"{prefix}: name must not be empty.")
    if not trigger.id.strip():
        raise TriggerExchangeError(f"{prefix}: id must not be empty; omit it to create a new ID.")
    problems = trigger.problems()
    if problems:
        raise TriggerExchangeError(f"{prefix}: {' '.join(problems)}")
    return trigger


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    data: dict[str, object] = {}
    for key, value in pairs:
        if key in data:
            raise TriggerExchangeError(f"The JSON contains the duplicate field {key!r}.")
        data[key] = value
    return data


def validate_trigger(data: object) -> Trigger:
    """Validate and normalize one definition without any file or store access."""
    return _validate_trigger(data, 1)


def _reject_constant(value: str) -> None:
    raise TriggerExchangeError(f"The JSON contains {value}; numbers must be finite.")


def read_trigger_file(path: str | Path) -> list[Trigger]:
    """Validate a complete JSON file and return converted, independent triggers."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise TriggerExchangeError("The timer file is too large (maximum 8 MB).")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_reject_constant)
    except TriggerExchangeError:
        raise
    except (OSError, ValueError, RecursionError) as exc:
        raise TriggerExchangeError(f"Could not read {path.name}: {exc}") from exc

    if isinstance(data, dict) and "triggers" in data:
        if "format" in data and data["format"] != FORMAT:
            raise TriggerExchangeError("This is not a PNUT timer bundle.")
        if "version" in data and (type(data["version"]) is not int or data["version"] != VERSION):
            raise TriggerExchangeError("This timer file uses an unsupported version. Update PNUT and try again.")
        if data.get("format") == FORMAT and "version" not in data:
            raise TriggerExchangeError("The timer bundle is missing its version.")
        items = data["triggers"]
    elif isinstance(data, dict) and "pattern" in data:
        items = [data]
    else:
        items = data
    if not isinstance(items, list):
        raise TriggerExchangeError("Choose a timer bundle, a triggers.json store, or a JSON list of timers.")
    if len(items) > MAX_TRIGGERS:
        raise TriggerExchangeError(f"The file contains too many timers (maximum {MAX_TRIGGERS}).")
    return [_validate_trigger(item, index) for index, item in enumerate(items, 1)]


def _validated_copies(triggers: Iterable[Trigger]) -> list[Trigger]:
    items = list(triggers)
    if len(items) > MAX_TRIGGERS:
        raise TriggerExchangeError(f"Too many timers (maximum {MAX_TRIGGERS}).")
    for index, trigger in enumerate(items, 1):
        if not isinstance(trigger, Trigger):
            raise TriggerExchangeError(f"Timer {index} is not a trigger definition.")
    return [_validate_trigger(trigger.to_dict(), index) for index, trigger in enumerate(items, 1)]


def _content_key(trigger: Trigger) -> tuple[object, ...]:
    # Numeric int/float equality is intentional: legacy stores used both.
    data = trigger.to_dict()
    data.pop("id")
    data["name"] = data["name"].strip().casefold()
    data["timer_mode"] = _LEGACY_MODES.get(data["timer_mode"], data["timer_mode"])
    for name in _COLOR_FIELDS:
        data[name] = data[name].lower()
    return tuple(data.values())


def merge_triggers(existing: Iterable[Trigger], incoming: Iterable[Trigger]) -> MergeResult:
    """Keep local versions, skip exact duplicates, and retain conflicting variants.

    Identity is not content: a shared timer may acquire a new ID on one computer.
    Comparing all settings (with case-insensitive names) makes repeat imports
    idempotent, including imports of legacy definitions with default fields absent.
    Neither the input lists nor any of their Trigger instances are modified.
    """
    # Locally edited rows may be unfinished (for example, a new empty pattern
    # or a regex still being typed).  Preserve them exactly rather than applying
    # the stricter validation required for definitions coming from a file.
    merged: list[Trigger] = []
    for index, trigger in enumerate(existing, 1):
        if not isinstance(trigger, Trigger):
            raise TriggerExchangeError(f"Local timer {index} is not a trigger definition.")
        merged.append(replace(trigger))
    additions = _validated_copies(incoming)
    contents = {_content_key(trigger) for trigger in merged}
    ids = {trigger.id for trigger in merged}
    names = {trigger.name.strip().casefold() for trigger in merged}
    added = skipped = conflicts = 0
    for trigger in additions:
        content = _content_key(trigger)
        if content in contents:
            skipped += 1
            continue
        name = trigger.name.strip().casefold()
        if trigger.id in ids or name in names:
            conflicts += 1
        if trigger.id in ids:
            while trigger.id in ids:
                trigger.id = uuid.uuid4().hex[:10]
        merged.append(trigger)
        contents.add(content)
        ids.add(trigger.id)
        names.add(name)
        added += 1
    if len(merged) > MAX_TRIGGERS:
        raise TriggerExchangeError(f"Import would exceed {MAX_TRIGGERS} timers.")
    return MergeResult(merged, added, skipped, conflicts)


def external_sound_files(triggers: Iterable[Trigger]) -> list[str]:
    """Sound references to mention to recipients; their contents stay private."""
    return sorted({trigger.file for trigger in triggers if trigger.file and "file" in
                   (trigger.action, trigger.timer_warn_action, trigger.timer_end_action)})


def export_trigger_file(path: str | Path, triggers: Iterable[Trigger]) -> None:
    """Serialize complete definitions for storage/protocol tools.

    Presentation callers must use export_visible_trigger_file so that an
    omitted or unconfirmed presentation policy cannot publish source text.
    """
    path = Path(path)
    copies = _validated_copies(triggers)
    try:
        payload = (json.dumps({"format": FORMAT, "version": VERSION,
                               "triggers": [trigger.to_dict() for trigger in copies]},
                              indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise TriggerExchangeError(f"Could not export {path.name}: {exc}") from exc
    if len(payload) > MAX_FILE_BYTES:
        raise TriggerExchangeError("The timer bundle is too large (maximum 8 MB). Export fewer timers.")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb",
                                         dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise TriggerExchangeError(f"Could not export {path.name}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def export_visible_trigger_file(path: str | Path, triggers: Iterable[Trigger], *, cfg: object = None) -> None:
    """Publish definitions only under an explicitly confirmed full-mode policy."""
    items = list(triggers)
    if any(safe_trigger_definition(trigger, cfg) is None for trigger in items):
        raise TriggerExchangeError("Carebear Mode hides timer definitions; exporting is unavailable.")
    export_trigger_file(path, items)
