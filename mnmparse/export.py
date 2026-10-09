"""One-line encounter summaries for the clipboard (like ACT's "copy to clipboard").

A summary is built from three templates: the line (``{title} [{duration}] ... {actors}``),
one entry per person (``{name} {dps}``) and the separator between entries.  Placeholders are
``{field}`` or ``{field:spec}`` with a Python format spec (``{dps:.0f}``, ``{damage:,}``); an
unknown field is left as typed, so a typo shows up in the preview instead of failing.

People are the viewer's group in the fight (``ActorRow.in_group``: no enemies, no players
outside the group), sorted by the preset's measure and cut to ``max_actors``.  Someone with
nothing in that measure is left out (no damage in a DPS summary, no healing in a healing
one), unless the person template also shows healing, utility or damage taken: then anyone
with any of those is listed (a healer in "Everything").  The line's ``{damage}``/``{dps}`` are
the group's own.  The result is always one line.

The duration and the per-second rates come from the snapshot's ``active_duration`` (the
group's first to last swing or hit), not from the live meter's clock, which runs on to now
while a fight is open: a copy taken mid-fight reads the same as the one taken when it ends.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ACTOR_FIELDS",
    "DEFAULT_PRESET",
    "LINE_FIELDS",
    "PRESETS",
    "SORTS",
    "ExportFormat",
    "actor_export_name",
    "format_from_config",
    "format_snapshot",
    "has_people",
    "preset_for",
    "render",
]

#: What the people are sorted by (and who counts: a positive value).
SORTS = ("damage", "healing", "taken", "utility")


@dataclass(frozen=True)
class ExportFormat:
    line: str
    actor: str
    separator: str = ", "
    sort: str = "damage"
    max_actors: int = 10


#: Ready-made formats (Settings > Export starts from one; editing makes it "Custom").
PRESETS: dict[str, ExportFormat] = {
    "DPS": ExportFormat("{title} [{duration}] {dps} DPS - {actors}", "{name} {dps}"),
    "DPS and share": ExportFormat("{title} [{duration}] {dps} DPS - {actors}", "{name} {dps} ({share}%)", " | "),
    "Damage": ExportFormat("{title} [{duration}] {damage} dmg - {actors}", "{name} {damage} ({share}%)", " | "),
    "Healing": ExportFormat("{title} [{duration}] healing - {actors}", "{name} {hps} HPS", ", ", "healing"),
    "Everything": ExportFormat(
        "{title} [{duration}] {dps} DPS - {actors}",
        "{name} {dps} dps / {hps} hps / {utility} util / {taken} taken",
        " | ",
    ),
}
DEFAULT_PRESET = "DPS"

#: Line placeholders and what they mean (Settings > Export lists them).
LINE_FIELDS = {
    "title": "the fight's name (the main enemy, or the zone for a zone summary)",
    "zone": "the zone",
    "duration": "how long the group fought (m:ss)",
    "start": "when it started (hh:mm)",
    "damage": "total damage dealt",
    "dps": "the whole group's damage per second",
    "kills": "how many enemies died",
    "killed": "the names of the enemies that died",
    "encounters": "how many fights (a zone summary covers several)",
    "actors": "the people, one entry each (below)",
}
#: Per-person placeholders.
ACTOR_FIELDS = {
    "rank": "place in the list (1, 2, ...)",
    "name": "character name (+Pet when pet output is included)",
    "dps": "damage per second",
    "damage": "damage dealt",
    "share": "share of the group's damage (%)",
    "max": "biggest hit",
    "hit": "hit rate of swings (%)",
    "hps": "healing per second",
    "heal": "healing done",
    "taken": "damage taken",
    "utility": "utility score (crowd control, debuffs, aggro)",
    "deaths": "times they died in the fight",
}

_FIELD_RX = re.compile(r"\{(\w+)(?::([^{}]*))?\}")
_SPACE_RX = re.compile(r"\s+")


class _Pct(float):
    """A percentage: printed whole by default, a spec sees the exact value ({share:.1f})."""


def render(template: str, values: dict[str, Any]) -> str:
    """Fill ``{field}`` / ``{field:spec}`` from ``values``; unknown fields stay as typed.

    Values without a spec: ints get thousands separators, floats one decimal; a spec that
    does not suit the value falls back to the default formatting.
    """

    def one(m: re.Match[str]) -> str:
        key, spec = m.group(1), m.group(2)
        if key not in values:
            return m.group(0)
        value = values[key]
        if spec:
            try:
                return format(value, spec)
            except (TypeError, ValueError):
                pass
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, _Pct):
            return f"{value:.0f}"
        if isinstance(value, int):
            return f"{value:,}"
        if isinstance(value, float):
            return f"{value:,.1f}"
        return str(value)

    return _FIELD_RX.sub(one, template or "")


def _mmss(seconds: float) -> str:
    total = int(round(max(0.0, seconds)))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _measure(row: Any, sort: str) -> float:
    if sort == "healing":
        return float(getattr(row, "heals", 0) or 0)
    if sort == "taken":
        return float(getattr(row, "taken", 0) or 0)
    if sort == "utility":
        return float(getattr(row, "utility", 0) or 0)
    return float(getattr(row, "damage", 0) or 0)


_OTHER_FIELDS = {"hps": "healing", "heal": "healing", "utility": "utility", "taken": "taken"}


def _people(snap: Any, fmt: ExportFormat) -> list[Any]:
    sort = fmt.sort if fmt.sort in SORTS else "damage"
    rows = [
        r for r in getattr(snap, "rows", []) or []
        if not getattr(r, "is_npc", False) and not getattr(r, "is_enemy", False) and getattr(r, "in_group", True)
    ]
    shown = {sort} | {m for key, m in _OTHER_FIELDS.items() if re.search(rf"\{{{key}[}}:]", fmt.actor or "")}
    rows = [r for r in rows if any(_measure(r, m) > 0 for m in shown)]
    rows.sort(key=lambda r: (-_measure(r, sort), str(getattr(r, "name", ""))))
    return rows[: max(1, int(fmt.max_actors or 1))]


def has_people(snap: Any, fmt: ExportFormat) -> bool:
    """Whether the summary of ``snap`` would list anyone (the automatic copy skips empty ones)."""
    return snap is not None and bool(_people(snap, fmt))


def _rate(amount: Any, active: float, fallback: Any) -> float:
    """``amount`` per second of ``active`` (rounded like the meter's), else ``fallback``."""
    if active > 0:
        return round(int(amount or 0) / active, 2)
    return float(fallback or 0.0)


def actor_export_name(row: Any) -> str:
    """Compact owner label for exports, including any number of attributed pets."""
    name = str(getattr(row, "name", "") or "?")
    return f"{name}+Pet" if getattr(row, "attributed_pets", ()) else name


def format_snapshot(snap: Any, fmt: ExportFormat) -> str:
    """The one-line summary of ``snap`` (an ``EncounterSnapshot``) in ``fmt``.

    With an ``active_duration`` (see the module docstring) the duration, DPS and HPS are
    worked out from it; a snapshot without one (0 or missing) is printed as it is.
    """
    if snap is None:
        return ""
    active = float(getattr(snap, "active_duration", 0.0) or 0.0)
    people = _people(snap, fmt)
    entries = []
    for rank, row in enumerate(people, 1):
        entries.append(render(fmt.actor, {
            "rank": rank,
            "name": actor_export_name(row),
            "dps": _rate(getattr(row, "damage", 0), active, getattr(row, "dps", 0.0)),
            "damage": int(getattr(row, "damage", 0) or 0),
            "share": _Pct(100.0 * float(getattr(row, "share", 0.0) or 0.0)),
            "max": int(getattr(row, "max_hit", 0) or 0),
            "hit": _Pct(float(getattr(row, "hit_pct", 0.0) or 0.0)) if getattr(row, "misses_shown", True) else "?",
            "hps": _rate(getattr(row, "heals", 0), active, getattr(row, "hps", 0.0)),
            "heal": int(getattr(row, "heals", 0) or 0),
            "taken": int(getattr(row, "taken", 0) or 0),
            "utility": int(getattr(row, "utility", 0) or 0),
            "deaths": int(getattr(row, "deaths", 0) or 0),
        }))
    killed = [str(k) for k in getattr(snap, "killed", []) or []]
    start = float(getattr(snap, "start", 0.0) or 0.0)
    line = render(fmt.line, {
        "title": str(getattr(snap, "label", "") or "fight"),
        "zone": str(getattr(snap, "zone", "") or ""),
        "duration": _mmss(active or float(getattr(snap, "duration", 0.0) or 0.0)),
        "start": time.strftime("%H:%M", time.localtime(start)) if start > 0 else "",
        "damage": int(getattr(snap, "total_damage", 0) or 0),
        "dps": _rate(getattr(snap, "total_damage", 0), active, getattr(snap, "raid_dps", 0.0)),
        "kills": int(getattr(snap, "kills", 0) or 0) or len(killed),
        "killed": ", ".join(dict.fromkeys(killed)),
        "encounters": int(getattr(snap, "encounters", 1) or 1),
        "actors": fmt.separator.join(entries),
    })
    return _SPACE_RX.sub(" ", line).strip()  # always one line


def preset_for(fmt: ExportFormat) -> str:
    """The preset ``fmt`` is (its name), or ``"Custom"``."""
    return next((name for name, preset in PRESETS.items() if preset == fmt), "Custom")


def format_from_config(cfg: Any) -> ExportFormat:
    """The export format a ``Config`` describes (defaults for missing fields)."""
    base = PRESETS[DEFAULT_PRESET]
    try:
        max_actors = int(getattr(cfg, "export_max_actors", base.max_actors))
    except (TypeError, ValueError):
        max_actors = base.max_actors
    return ExportFormat(
        line=str(getattr(cfg, "export_line", base.line) or base.line),
        actor=str(getattr(cfg, "export_actor", base.actor) or base.actor),
        separator=str(getattr(cfg, "export_separator", base.separator)),
        sort=str(getattr(cfg, "export_sort", base.sort) or base.sort),
        max_actors=max(1, min(40, max_actors)),
    )
