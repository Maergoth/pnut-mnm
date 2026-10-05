"""Crowd-control registry: which abilities interrupt, stun, mesmerize, root, silence, snare,
fear, charm or blind (Overview tab "CC" score).

Monsters & Memories prints a CC *attempt* as the ability's own line (``"Dogabetarolem kicks a
skeletal warrior."``, ``"Palidu begins casting Mesmerize."``, ``"X's Shield Slam hits Y for 3
points of damage."``) and the *result* as a line about the victim (``"<caster>'s casting is
interrupted."``, ``"a skeletal defender is mesmerized."``, ``"Fozo is rooted to the ground."``)
that never names who caused it.  Every class has its own CC abilities, so the set is data: a
built-in table plus an optional ``cc.json`` in the project root::

    {"interrupt": ["Kick", ...], "stun": [...], "mez": [...], "root": [...],
     "silence": [...], "snare": [...], "fear": [...], "charm": [...], "blind": [...]}

(A legacy ``interrupts.json`` with keys ``interrupts`` / ``stuns`` / ``silences`` is read too.)
Names match the parsed ``Event.skill`` case-insensitively, ignoring a trailing rank numeral
(``Shield Bash II``) and, failing that, a tier prefix (``Lesser Gust of Wind``).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

CC_CATEGORIES: tuple[str, ...] = ("interrupt", "stun", "mez", "root", "silence", "snare", "fear", "charm", "blind")
"""Every crowd-control category, in display order."""

CC_LABELS: dict[str, str] = {
    "interrupt": "Interrupts",
    "stun": "Stuns",
    "mez": "Mezzes",
    "root": "Roots",
    "silence": "Silences",
    "snare": "Snares",
    "fear": "Fears",
    "charm": "Charms",
    "blind": "Blinds",
}

#: Built-in table (wiki survey 2026-10-02, all 18 classes); cc.json extends it.
DEFAULT_CC: dict[str, frozenset[str]] = {
    "interrupt": frozenset(
        {
            "kick", "jab", "shield bash", "reflecting bash", "shield slam", "shield toss",
            "grip of shadow", "interrupting shot", "net shot", "pinning shot", "scatter shot",
            "lurching impairment", "gust of wind", "windblast", "flash of lightning",
            "lightning surge", "burst of lightning", "lightning nova",
            # seen interrupting casts in the 2026-10-02 logs: "X bashes Y.", "Y is condemned." +
            # "Y's casting is interrupted." in the same second with nothing else on Y
            "bash", "interdiction",
        }
    ),
    "stun": frozenset(
        {
            "uppercut", "low blow", "clobber", "charge", "slam stun", "sweeping kick", "hundred fists", "pinning throw",
            "stun", "scintillating shock", "scintillating stupor", "scintillation", "psychic choke",
            "telekinetic knockback", "staggering winds", "ogre smash", "electric arc", "scatter shot",
            # seen stunning in the 2026-10-02 logs: NPC "X's Bash hits" / "X's Slam hits", "stunned by
            # an electric shock" (Electric Infusion), "X smashes the ground around them" (area stun)
            "bash", "slam", "electric infusion", "ground smash",
        }
    ),
    "mez": frozenset({"mesmerize", "entrance", "enthrall", "somniferous serenade", "lull", "hypnotize", "sleep"}),
    "root": frozenset(
        {"root", "entangle", "ensnare", "grasping roots", "twisting roots", "lurching impairment", "net shot",
         "pinning shot", "pinning throw"}
    ),
    "silence": frozenset({"silence", "mandate of silence"}),
    "snare": frozenset({"snare", "lesser snare", "hamstring", "snaring shot"}),
    "fear": frozenset({"fear", "terrify", "scare", "intimidate", "intimidation"}),
    "charm": frozenset({"charm", "lesser charm"}),
    "blind": frozenset({"blind"}),
}

#: Spells whose cast lines name no target but whose result is a debuff line on the victim
#: ("Povebizu begins casting Interdiction." ... "a skeletal fighter is condemned.").  Not CC;
#: used only to credit a debuff when no targeted action precedes it.
DEFAULT_DEBUFF_SPELLS: frozenset[str] = frozenset(
    {"interdiction", "torment", "distress", "malaise", "enfeeble", "weakness", "curse", "blight", "condemn", "hex"}
)

#: The plain Kick skill.
KICK_SKILLS: frozenset[str] = frozenset({"kick"})

#: A result line is credited to an attempt no older than this (seconds).  Casts take one
#: to two seconds to land, melee interrupts land at once.
CREDIT_WINDOW_S = 3.0

_RANK_RX = re.compile(r"\s+(?:I|II|III|IV|V|VI|VII|VIII|IX|X)$")
_TIER_PREFIX_RX = re.compile(r"^(?:lesser|greater|exceptional|minor|major)\s+")
_CACHE: dict[str, tuple[tuple[float, float], dict[str, frozenset[str]]]] = {}


def canonical_skill(skill: str) -> str:
    """Lower-case ``skill`` without a trailing rank numeral (``Shield Bash II`` -> ``shield bash``)."""
    return _RANK_RX.sub("", skill.strip()).lower()


def _project_root(project_root: Path | None) -> Path:
    if project_root is not None:
        return Path(project_root)
    try:
        from .config import PROJECT_ROOT

        return PROJECT_ROOT
    except Exception:  # pragma: no cover
        return Path(__file__).resolve().parent.parent


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime if path.exists() else -1.0
    except OSError:
        return -1.0


def load_cc_table(project_root: Path | None = None) -> dict[str, frozenset[str]]:
    """The CC table (category -> lower-cased ability names), merged with ``cc.json`` and the
    legacy ``interrupts.json`` when present.  Cached per file modification time."""
    root = _project_root(project_root)
    cc_path, legacy_path = root / "cc.json", root / "interrupts.json"
    stamp = (_mtime(cc_path), _mtime(legacy_path))
    cached = _CACHE.get(str(root))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    table: dict[str, set[str]] = {cat: set(names) for cat, names in DEFAULT_CC.items()}
    table["debuff"] = set(DEFAULT_DEBUFF_SPELLS)
    if cc_path.exists():
        _merge_file(table, cc_path, {cat: cat for cat in (*CC_CATEGORIES, "debuff")})
    if legacy_path.exists():
        _merge_file(table, legacy_path, {"interrupts": "interrupt", "stuns": "stun", "silences": "silence"})
    result = {cat: frozenset(names) for cat, names in table.items()}
    _CACHE[str(root)] = (stamp, result)
    return result


def _merge_file(table: dict[str, set[str]], path: Path, key_map: dict[str, str]) -> None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed file: keep what we have
        log.warning("could not read %s: %s", path, exc)
        return
    added = 0
    for key, cat in key_map.items():
        for name in data.get(key, []) or []:
            if isinstance(name, str) and name.strip():
                table.setdefault(cat, set()).add(canonical_skill(name))
                added += 1
    log.info("loaded %d crowd-control abilities from %s", added, path)


def cc_categories(skill: str | None, table: dict[str, frozenset[str]] | None = None) -> frozenset[str]:
    """Categories ``skill`` belongs to (empty when it is not a CC ability)."""
    if not skill:
        return frozenset()
    table = table if table is not None else DEFAULT_CC
    key = canonical_skill(skill)
    hits = {cat for cat, names in table.items() if cat != "debuff" and key in names}
    if not hits:
        bare = _TIER_PREFIX_RX.sub("", key)
        if bare != key:
            hits = {cat for cat, names in table.items() if cat != "debuff" and bare in names}
    return frozenset(hits)


def is_debuff_spell(skill: str | None, table: dict[str, frozenset[str]] | None = None) -> bool:
    """True when ``skill`` is a known debuff spell (``cc.json`` category ``debuff``)."""
    if not skill:
        return False
    names = (table or {}).get("debuff", DEFAULT_DEBUFF_SPELLS)
    key = canonical_skill(skill)
    return key in names or _TIER_PREFIX_RX.sub("", key) in names


def is_cc_skill(skill: str | None, table: dict[str, frozenset[str]] | None = None) -> bool:
    """True when ``skill`` is any kind of crowd control."""
    return bool(cc_categories(skill, table))


def is_kick(skill: str | None) -> bool:
    """True for the plain Kick skill."""
    return bool(skill) and canonical_skill(skill) in KICK_SKILLS


# ---- backwards-compatible helpers (interrupt-only view of the table) -------------------


def load_interrupt_skills(project_root: Path | None = None) -> frozenset[str]:
    """Every CC ability name in one set (interrupts, stuns, silences, ...)."""
    table = load_cc_table(project_root)
    return frozenset().union(*table.values())


def is_interrupt_skill(skill: str | None, skills: frozenset[str] | None = None) -> bool:
    """True when ``skill`` is an interrupt-type or stun-type ability (legacy name)."""
    if not skill:
        return False
    if skills is not None:
        key = canonical_skill(skill)
        return key in skills or _TIER_PREFIX_RX.sub("", key) in skills
    return bool(cc_categories(skill) & {"interrupt", "stun", "silence"})
