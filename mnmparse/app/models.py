"""View models shared by the overlay, the pages and the engine (APP_SPEC section 5).

This module is pure Python: it imports nothing from Qt so it can be unit-tested
headless and reused by any front end.  :func:`build_snapshot` turns a
:class:`mnmparse.stats.Encounter` into an immutable-by-convention
:class:`EncounterSnapshot` with one :class:`ActorRow` per participant; attributed
pets contribute to their owner's row. The UI filters the rows per tab with
:func:`snapshot_rows_for_tab`.

Numbers follow :meth:`mnmparse.stats.Stats.actor_table` where the two overlap
(``hit_pct`` is a percentage 0..100, ``dps`` is damage / max(1, duration)), and
add what a meter needs on top: damage taken, healing received, the share of the
encounter's damage (0..1) and a per-skill breakdown.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import zlib
from dataclasses import dataclass, field
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, Callable

from mnmparse.grammar import DAMAGE_EFFECT_OUTCOMES, VERB_LEMMAS, is_npc_name, lemmatize
from mnmparse.stats import name_similar
from mnmparse.vocab import close_spellings, edit_distance, fold
from mnmparse.interrupts import CC_CATEGORIES, CREDIT_WINDOW_S, canonical_skill, cc_categories, is_debuff_spell, load_cc_table
from mnmparse.stats import ACTIVITY_KINDS, DAMAGE_KINDS

if TYPE_CHECKING:
    from mnmparse.grammar import Event
    from mnmparse.stats import Encounter, Stats

__all__ = [
    "ActorRow",
    "EncounterSnapshot",
    "SkillRow",
    "build_snapshot",
    "snapshot_rows_for_tab",
    "actor_color",
]

log = logging.getLogger(__name__)

MELEE_SKILL = "melee"
"""Skill key for damage events without a skill name."""

MISS_KINDS: frozenset[str] = frozenset({"melee_miss", "ability_miss", "resist", "fizzle"})
"""Kinds that count as a failed attempt in the per-skill breakdown."""

TABS: tuple[str, ...] = ("overview", "damage", "healing", "taken")
"""Meter tabs understood by :func:`snapshot_rows_for_tab`."""

# Fallback palette (APP_SPEC section 4 tokens) used only when ``theme.actor_color``
# cannot be imported, e.g. in headless tests before the theme module exists.
_YOU_COLOR = "#ffd166"
_NPC_COLOR = "#d9655b"
_PET_COLOR = "#c084fc"
_PARTY_PALETTE: tuple[str, ...] = (
    "#5bc0eb", "#7ee787", "#ff9f68", "#c792ea", "#f78fb3", "#4dd0e1", "#ffd54f", "#a5d6a7",
)

ColorFn = Callable[..., str]

#: Debuff type -> substrings of the ability most likely to have caused it (lower-case).
#: The game usually prints an effect BEFORE the action that caused it: "a dunes madman's
#: casting is interrupted." then "Famafenezo kicks a dunes madman."; "X is stunned by an
#: electric arc." then "Y's Electric Arc hits X"; "X's arcane defenses weaken." then
#: "Y's Arcane Infusion hits X".  (In the 2026-10-02 logs 122 interrupts had their kick or
#: slam only after the result line and 16 only before.)  So a result is matched against
#: attempts up to this many seconds after it as well as CREDIT_WINDOW_S before it...
RESULT_LOOKAHEAD_S = 2.0
#: ...and within this many events after it (a result is decided once either limit passes).
RESULT_LOOKAHEAD_EVENTS = 12
#: The very next lines also count when capture timestamps jumped ("a skeletal monk is
#: stunned." 04:39:58, next line "Gozif uppercuts a skeletal monk." 04:40:01).
RESULT_NEXT_LINES = 3
RESULT_NEXT_LINES_S = 5.0
#: In sparse capture stretches the next one or two lines may be many seconds "later".
RESULT_ADJACENT_LINES = 2
RESULT_ADJACENT_S = 10.0
#: An instant attempt (kick, bash, slam, an ability hit) printed BEFORE its effect is at most
#: this far back (the second blind audit: older kicks that did not interrupt were the main
#: over-credit) ...
INSTANT_BACK_S = 1.5
INSTANT_BACK_LINES = 4
#: ... while a spell lands a cast time after "begins casting" (4-6 s seen for Interdiction,
#: Root, Stun and Distress); a cast can never explain an effect printed before it.
CAST_BACK_S = 6.5
#: Abilities whose effect line names them ("stunned by scintillating lights"): they never
#: explain a plain "X is stunned." and only match their own phrase.
ABILITY_PHRASES = {
    "scintillating shock": "scintillating", "scintillating stupor": "scintillating", "scintillation": "scintillating",
    "electric arc": "electric arc", "electric infusion": "electric shock",
}
#: Spells that interrupt only through their debuff: Interdiction interrupts when "X is
#: condemned." shows it landed on that caster.
INTERRUPT_VIA_DEBUFF = {"interdiction": "condemned"}
#: Area effects the grammar reads as untargeted "casts" but that land at once (no cast time).
INSTANT_AREA_SKILLS = frozenset({"ground smash"})

#: Debuff type -> ability name fragments that cause it, MOST LIKELY FIRST. A fragment
#: earlier in the list beats a closer action matching a later one. Damage-only effects
#: such as bleeding are excluded from Utility, including older saved debuff events.
_DEBUFF_HINTS: dict[str, tuple[str, ...]] = {
    "condemned": ("interdiction", "condemn"),
    "weakened": ("omen of enfeeblement", "enfeeblement", "exposing shot", "enfeeble", "weakness", "malady"),
    "tormented": ("torment",),
    "resist down": ("distress", "vocalization", "torment", "shock", "volley", "flash", "surge"),
    "arcane weakened": ("infusion", "arcane"),
    "slowed": ("telekinetic", "infusion"),
    "faltering pulse": ("faltering",),
    "vigor drained": ("vigor",),
    "chilled": ("chill", "frost"),
}
#: Kinds whose actions can cause a debuff when no named ability is found (plain melee swings
#: never do: they were the commonest wrong credit in the audit).
_DEBUFF_FALLBACK_KINDS = frozenset({"ability_hit", "ability_partial", "ability_miss", "cast"})
_DAMAGE_ONLY_EFFECTS = frozenset(DAMAGE_EFFECT_OUTCOMES.values())
_DAMAGE_ACTIONS = frozenset(VERB_LEMMAS.values()) | {
    "attack", "shoot", "fire", "throw", "uppercut", "backstab", "rend", "gash", "lacerate",
    "bleed", "barbed arrow", "shield bash", "shield slam", "shield toss", "low blow",
    "burn", "shock", "blast", "smite", "scorch", "freeze", "chill", "wound", "zap",
    "drain", "sear", "blight", "lash",
}
_NONCOMBAT_ACTIONS = frozenset({
    "feel", "weave", "lose", "begin", "cast", "look", "break", "heal", "learn", "loot",
    "receive", "sell", "buy", "train", "say", "hail", "consider", "follow", "invite",
})
#: An interrupt is caused after the victim began casting: attempts before its last "begins
#: casting" line (within this many seconds) are not candidates.
CAST_MEMORY_S = 12.0


@dataclass
class SkillRow:
    """Damage breakdown for one skill of one actor.

    ``count`` is the number of attempts (``hits`` + ``misses``), ``total`` the
    damage dealt, ``avg`` the damage per damaging hit.
    """

    skill: str
    count: int
    total: int
    max_hit: int
    avg: float
    hits: int
    misses: int


@dataclass
class ActorRow:
    """One participant of an encounter with every number the meters show.

    ``hit_pct`` is a percentage (0..100) over melee swings; ``share`` is this
    actor's fraction (0..1) of the encounter's total damage; ``color`` is the
    hex colour the UI paints the name and bar with.
    """

    name: str
    damage: int
    dps: float
    taken: int
    dtps: float
    heals: int
    hps: float
    healed: int
    swings: int
    hits: int
    misses: int
    hit_pct: float
    max_hit: int
    avg_hit: float
    share: float
    color: str
    is_you: bool
    is_npc: bool
    is_pet: bool
    skills: list[SkillRow] = field(default_factory=list)
    max_heal: int = 0  #: largest single heal landed
    cc: int = 0  #: CC score: crowd-control effects that landed (interrupts, stuns, mezzes, roots, silences ...)
    cc_attempts: int = 0  #: uses of any CC ability or spell (see mnmparse.interrupts / cc.json)
    cc_types: dict[str, int] = field(default_factory=dict)  #: landed effects by category
    heal_skills: list[SkillRow] = field(default_factory=list)  #: healing breakdown by spell
    taken_from: list[SkillRow] = field(default_factory=list)  #: damage taken by "attacker: skill"
    cc_skills: dict[str, int] = field(default_factory=dict)  #: landed effects by ability name
    utility: int = 0  #: Utility score = CC landed + debuffs landed + aggro gained
    debuffs: dict[str, int] = field(default_factory=dict)  #: non-damage effects and other utility actions by type
    debuff_skills: dict[str, int] = field(default_factory=dict)  #: debuffs landed by ability name
    aggro: int = 0  #: times a mob turned on this actor ("X looks angry at Y")
    prevented: int = 0  #: damage prevented on this actor by blocks and absorbs
    estimated: int = 0  #: hits and heals by this actor whose amount is a Dummy Fix estimate
    taunts: int = 0  #: taunts used (each is also one aggro)
    enemy_heals: int = 0  #: healing this actor landed on enemies (a PvP foe, a mob): not in heals or hps
    #: On the viewer's side and in their group (you, your pet, your party): counted in the
    #: totals.  False for enemies and for players outside the group (other groups nearby).
    in_group: bool = True
    #: Fights the group: an NPC ("a/an/the ..."), or anyone who hurt the group or was hurt by
    #: it (named mobs, PvP), worked out from who hit whom (see _classify_sides).
    is_enemy: bool = False
    #: Whether this actor's misses can be seen in the chat: the viewer's, their pet's and the
    #: enemies' (their misses on the group show) are; other players' are not unless the chat
    #: shows them (Stats.others_misses_seen), so their hit rate would read 100 %.
    misses_shown: bool = True
    deaths: int = 0  #: times this actor died in the fight ("X has been slain by Y", "You have been slain")
    killed_by: dict[str, int] = field(default_factory=dict)  #: who killed them, and how often
    pet_owner: str = ""  #: named owner on a raw pet row before it is folded into its owner
    attributed_pets: list[str] = field(default_factory=list)  #: pets included in an owner summary

    @property
    def display_name(self) -> str:
        """The visible label; ``name`` remains the owner's stable character identity."""
        if not self.attributed_pets:
            return self.name
        pets = "Pet" if len(self.attributed_pets) == 1 else "Pets"
        return f"{self.name} + {self.name}'s {pets}"


@dataclass
class EncounterSnapshot:
    """A frozen view of one encounter for display.

    ``key`` is stable for the life of the encounter (``f"{start:.3f}"``) so a
    view can tell "same fight, newer numbers" from "a new fight".
    ``duration`` is the time the rates are per: the group's own fighting, from its first
    to its last swing or hit (ticking on to now while the fight is open); ``start`` stays
    the encounter's.  It is at least one second so rates never divide by zero.
    """

    key: str
    label: str
    start: float
    end: float
    duration: float
    closed: bool
    event_count: int
    total_damage: int
    raid_dps: float
    killed: list[str]
    rows: list[ActorRow] = field(default_factory=list)
    zone: str = ""  #: zone in effect when the encounter started ("" when no zone line was seen yet)
    zone_since: float = 0.0  #: when that zone was entered (identifies the visit; 0.0 = unknown)
    encounters: int = 1  #: how many encounters this snapshot covers (a zone summary covers several)
    ours: bool = True  #: the viewer or a party member took part (else another group's fight nearby)
    kills: int = 0  #: enemies that died (each "slain" line, not distinct names)
    #: The group's first to last swing or hit, never ticking to now (at least 1 s): the same
    #: while the fight is open and once it closed, so a copy taken mid-fight matches the one
    #: taken at the end (mnmparse.export).  0.0 when unknown (a snapshot built elsewhere).
    active_duration: float = 0.0
    group_members: list[str] = field(default_factory=list)  #: owner choices, including absent members


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------


def _fallback_actor_color(name: str, *, is_you: bool, is_npc: bool, is_pet: bool) -> str:
    """Stable colour per name using the APP_SPEC section 4 tokens (no Qt needed)."""
    if is_you:
        return _YOU_COLOR
    if is_pet:
        return _PET_COLOR
    if is_npc:
        return _NPC_COLOR
    index = zlib.crc32(name.strip().lower().encode("utf-8")) % len(_PARTY_PALETTE)
    return _PARTY_PALETTE[index]


_color_fn: ColorFn | None = None


def actor_color(name: str, *, is_you: bool, is_npc: bool, is_pet: bool) -> str:
    """Return the hex colour for an actor, preferring :func:`mnmparse.app.theme.actor_color`.

    The theme module is imported lazily (and only once) so this module stays
    Qt-free; when the theme is unavailable the built-in palette with the same
    tokens is used.
    """
    global _color_fn
    if _color_fn is None:
        try:
            from mnmparse.app.theme import actor_color as theme_color  # type: ignore[import-not-found]

            _color_fn = theme_color
        except Exception:  # noqa: BLE001 - missing module or no Qt platform
            log.debug("theme.actor_color unavailable; using the built-in palette", exc_info=True)
            _color_fn = _fallback_actor_color
    return _color_fn(name, is_you=is_you, is_npc=is_npc, is_pet=is_pet)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass
class _Acc:
    """Mutable accumulator for one actor while scanning the events."""

    name: str
    damage: int = 0
    taken: int = 0
    heals: int = 0
    healed: int = 0
    hits: int = 0  #: melee hits (swings that landed)
    misses: int = 0  #: melee misses, including absorbed swings
    damaging_hits: int = 0  #: melee + ability hits with an amount
    max_hit: int = 0
    max_heal: int = 0
    estimated: int = 0  #: hits and heals whose amount is a Dummy Fix estimate
    taunts: int = 0  #: taunts used (each also counts as aggro)
    cc_landed: int = 0
    cc_attempts: int = 0
    cc_types: dict[str, int] = field(default_factory=dict)
    cc_skills: dict[str, int] = field(default_factory=dict)
    debuffs: dict[str, int] = field(default_factory=dict)
    debuff_skills: dict[str, int] = field(default_factory=dict)
    aggro: int = 0
    prevented: int = 0
    is_pet: bool = False
    enemy_heals: int = 0  #: healing landed on enemies (moved out of ``heals``, see _drop_enemy_heals)
    skills: dict[str, _SkillAcc] = field(default_factory=dict)
    heal_skills: dict[str, _SkillAcc] = field(default_factory=dict)
    taken_from: dict[str, _SkillAcc] = field(default_factory=dict)
    #: every heal landed: (target, spell, amount, estimated), so heals on enemies can be moved
    #: out once the sides are known
    heal_log: list[tuple[str | None, str, int, bool]] = field(default_factory=list)

    def skill(self, key: str) -> _SkillAcc:
        acc = self.skills.get(key)
        if acc is None:
            acc = self.skills[key] = _SkillAcc(key)
        return acc

    def heal_skill(self, key: str) -> _SkillAcc:
        acc = self.heal_skills.get(key)
        if acc is None:
            acc = self.heal_skills[key] = _SkillAcc(key)
        return acc

    def taken_source(self, key: str) -> _SkillAcc:
        acc = self.taken_from.get(key)
        if acc is None:
            acc = self.taken_from[key] = _SkillAcc(key)
        return acc


@dataclass
class _Attempt:
    """One use of a crowd-control ability, awaiting result lines to credit."""

    ts: float
    actor: _Acc
    target: str | None
    cats: frozenset[str]
    skill: str
    multi: bool  #: a cast may land on several victims (area stuns)
    title: str = ""  #: the ability name as printed, for the per-ability breakdown
    credited: int = 0
    spent: set[str] = field(default_factory=set)  #: categories already credited (a single-target
    #: attempt credits each category once: an uppercut can stun AND interrupt the same cast)
    idx: int = 0  #: position in the event list (ties between equally close attempts)
    cast: bool = False  #: a spell with a cast time (lands after "begins casting")


@dataclass
class _SkillAcc:
    skill: str
    total: int = 0
    max_hit: int = 0
    hits: int = 0
    misses: int = 0

    def row(self) -> SkillRow:
        return SkillRow(
            skill=self.skill,
            count=self.hits + self.misses,
            total=self.total,
            max_hit=self.max_hit,
            avg=round(self.total / self.hits, 2) if self.hits else 0.0,
            hits=self.hits,
            misses=self.misses,
        )


def _skill_key(ev: Event) -> str:
    skill = (ev.skill or "").strip()
    return skill if skill else MELEE_SKILL


def _accumulate(
    events: list[Event], canon: dict[str, str], trace: list[dict[str, Any]] | None = None, *, vocab: Any = None,
    utility_credited: set[int] | None = None,
) -> dict[str, _Acc]:
    """Scan ``events`` once and return one accumulator per canonical name (insertion order).

    Crowd-control bookkeeping (the "CC" score): every use of a CC ability or spell
    (``mnmparse.interrupts`` / ``cc.json``) is an *attempt* by its actor on its target
    (casts have no target); a later result line (``"<caster>'s casting is interrupted."``,
    ``"X is stunned."``, ``"X is mesmerized."`` ...) is credited to the most recent attempt
    within ``CREDIT_WINDOW_S`` whose categories include the result's category, preferring
    one aimed at that victim, or whose spell matches the result's source phrase
    (``"stunned by scintillating lights"``).  Single-target attempts are credited once;
    a cast may credit several victims (area stuns).  ``vocab`` (the learned spellings) keeps
    two different mobs ("a jackal", "a jackal pup") from passing for the same victim.
    """
    events = list(events)
    accs: dict[str, _Acc] = {}
    table = load_cc_table()
    attempts: list[_Attempt] = []
    actions: list[tuple[float, _Acc, str | None, str, int, str]] = []  # (ts, actor, target, skill, idx, kind) of player actions
    began: dict[str, tuple[float, int]] = {}  #: caster -> (ts, idx) of their last "begins casting"
    hostile: set[tuple[str, str]] = set()  #: (attacker, victim) pairs that traded damage (PvP)
    debuff_lines: list[tuple[float, str | None, str]] = []  #: (ts, victim, type) of every debuff line
    taunts: list[tuple[float, _Acc, str | None]] = []  #: (ts, taunter, mob)
    credited_actions = utility_credited if utility_credited is not None else set()
    #: results waiting for the attempts printed after them:
    #: (ts, idx, kind, category, victim, source, victim's cast start at that moment)
    pending: list[tuple[float, int, str, str, str | None, str | None, tuple[float, int] | None]] = []

    def get(raw: str | None) -> _Acc | None:
        if not raw:
            return None
        name = canon.get(raw, raw)
        acc = accs.get(name)
        if acc is None:
            acc = accs[name] = _Acc(name)
        return acc

    def note_attempt(ev: Event, idx: int) -> None:
        cats = cc_categories(ev.skill, table)
        if not cats:
            return
        actor = get(ev.actor)
        if actor is None:
            return
        actor.cc_attempts += 1
        target = canon.get(ev.target, ev.target) if ev.target else None
        multi = ev.kind == "cast" or target is None
        attempts.append(
            _Attempt(float(ev.ts), actor, target, cats, (ev.skill or "").lower(), multi,
                     title=_skill_key(ev), idx=idx,
                     cast=ev.kind == "cast" and (ev.skill or "").lower() not in INSTANT_AREA_SKILLS)
        )
        del attempts[:-60]

    def same_name(a: str | None, b: str | None) -> bool:
        if not a or not b:
            return False
        if a == b or name_similar(a, b, vocab=vocab) or close_spellings(fold(a), fold(b)):
            return True
        wa, wb = fold(a).split(), fold(b).split()
        if not wa or not wb:
            return False
        la, lb = wa[-1], wb[-1]
        # "skelet gayalier" ~ "a skeletal cavalier": the mob's noun within two edits, the rest a prefix
        rest_a, rest_b = " ".join(w for w in wa[:-1] if w not in ("a", "an", "the")), " ".join(w for w in wb[:-1] if w not in ("a", "an", "the"))
        prefix_ok = (not rest_a or not rest_b) or rest_b.startswith(rest_a[:5]) or rest_a.startswith(rest_b[:5])
        return min(len(la), len(lb)) >= 6 and prefix_ok and edit_distance(la, lb, 2) <= 2

    def in_window(result_ts: float, ts: float, result_idx: int = -1, idx: int = -1, cast: bool = False) -> bool:
        back = result_ts - ts
        if cast:
            return idx < result_idx and 0 <= back <= CAST_BACK_S
        if idx <= result_idx or result_idx < 0:
            if result_idx < 0:
                return -RESULT_LOOKAHEAD_S <= back <= CREDIT_WINDOW_S
            return back <= INSTANT_BACK_S and result_idx - idx <= INSTANT_BACK_LINES
        ahead, lines = -back, idx - result_idx
        return (
            ahead <= RESULT_LOOKAHEAD_S
            or (lines <= RESULT_NEXT_LINES and ahead <= RESULT_NEXT_LINES_S)
            or (lines <= RESULT_ADJACENT_LINES and ahead <= RESULT_ADJACENT_S)
        )

    def phrase_ok(a: _Attempt, source: str | None) -> bool:
        phrase = next((p for name, p in ABILITY_PHRASES.items() if name in a.skill), None)
        if phrase is None:
            return True
        return bool(source) and phrase.split()[0] in source

    def plausible_side(a: _Attempt, victim: str | None) -> bool:
        """An untargeted cast by a player lands on a player only when the two are fighting (PvP:
        the caster damaged the victim); a groupmate's Interdiction never interrupts another groupmate.
        Attempts aimed at the victim by name, and NPC casts, always qualify."""
        if victim is None or (a.target is not None and same_name(a.target, victim)):
            return True
        if is_npc_name(a.actor.name) or is_npc_name(victim):
            return True
        return (a.actor.name, victim) in hostile

    def debuff_landed(victim: str | None, kind: str, ts: float) -> bool:
        return any(v == victim and k == kind and abs(t - ts) <= 1.5 for t, v, k in debuff_lines)

    def closest(cands: list[Any], ts: float, idx: int, ts_of: Any, idx_of: Any) -> Any:
        return min(cands, key=lambda a: (abs(ts_of(a) - ts), abs(idx_of(a) - idx)))

    def credit(ts: float, idx: int, category: str, victim: str | None, source: str | None,
               cast_start: tuple[float, int] | None = None) -> None:
        # An interrupt can also come from a stun landing on a caster (uppercut, bash, slam).
        usable = {category} | ({"stun"} if category == "interrupt" else set())
        recent = [
            a for a in attempts
            if in_window(ts, a.ts, idx, a.idx, a.cast) and (a.cats & usable) and category not in a.spent
            # aimed at the victim, or untargeted (a cast, an area effect); never at someone else
            and (a.target is None or a.multi or same_name(a.target, victim))
            and plausible_side(a, victim)
            and phrase_ok(a, source)
        ]
        if category == "interrupt":
            recent = [
                a for a in recent
                if not any(spell in a.skill for spell in INTERRUPT_VIA_DEBUFF)
                or any(debuff_landed(victim, kind, ts) for spell, kind in INTERRUPT_VIA_DEBUFF.items() if spell in a.skill)
            ]
        if category == "interrupt" and victim:
            start = cast_start  # the cast in progress when the interrupt line was printed
            if start is not None and ts - start[0] <= CAST_MEMORY_S:
                # an instant attempt before the cast began is not its interrupt (a spell started
                # earlier can still land after it)
                recent = [a for a in recent if a.cast or a.idx > start[1]]
        if not recent:
            return
        tiers: list[list[_Attempt]] = []
        if source:
            words = [w for w in source.split() if w not in ("a", "an", "the", "by")]
            if words:
                tiers.append([a for a in recent if words[0] in a.skill])
        tiers.append([a for a in recent if category in a.cats and victim and same_name(a.target, victim)])
        tiers.append([a for a in recent if category in a.cats])
        tiers.append(recent)
        group = next(t for t in tiers if t)
        match: _Attempt = closest(group, ts, idx, lambda a: a.ts, lambda a: a.idx)
        if trace is not None:
            trace.append({"result_idx": idx, "category": category, "actor": match.actor.name,
                          "skill": match.title, "attempt_idx": match.idx})
        match.credited += 1
        credited_actions.add(match.idx)
        if not match.multi:
            match.spent.add(category)
        match.actor.cc_landed += 1
        match.actor.cc_types[category] = match.actor.cc_types.get(category, 0) + 1
        match.actor.cc_skills[match.title] = match.actor.cc_skills.get(match.title, 0) + 1

    def note_action(ev: Event, idx: int) -> None:
        actor = get(ev.actor)
        if actor is None or is_npc_name(actor.name):
            return
        target = canon.get(ev.target, ev.target) if ev.target else None
        name = _skill_key(ev)
        if ev.weapon:
            name = f"{name} {ev.weapon}"  # "pierce bow": a bow shot (the Barbed Arrow stand-in)
        actions.append((float(ev.ts), actor, target, name, idx, ev.kind))
        del actions[:-80]

    def credit_debuff(ts: float, idx: int, kind: str, victim: str | None) -> None:
        if kind in _DAMAGE_ONLY_EFFECTS:
            return
        recent = [a for a in actions if in_window(ts, a[0], idx, a[4], a[5] == "cast")]
        hints = _DEBUFF_HINTS.get(kind, ())
        # 1. an ability named like the debuff, the likeliest name first (Distress before
        #    Screaming Vocalization for frayed resistance); aimed at
        #    the victim first, then at anyone (OCR garbles targets: "Gozif's Slice hits a for 3");
        # 2. an untargeted known debuff spell; 3. the closest ABILITY (not a plain melee swing)
        #    aimed at the victim.  Within a tier the closest in time wins (before or after).
        tiers: list[list[Any]] = []
        for hint in hints:
            named = [a for a in recent if hint in a[3].lower()]
            tiers.append([a for a in named if a[2] is None or same_name(a[2], victim)])
            tiers.append(named)
        tiers.append([a for a in recent if a[2] is None and is_debuff_spell(a[3], table)])
        tiers.append([a for a in recent if victim and same_name(a[2], victim) and a[5] in _DEBUFF_FALLBACK_KINDS])
        group = next((t for t in tiers if t), None)
        if group is None:
            return
        _ts, actor, _target, skill, _idx, _kind = closest(group, ts, idx, lambda a: a[0], lambda a: a[4])
        credited_actions.add(_idx)
        if trace is not None:
            trace.append({"result_idx": idx, "category": kind, "actor": actor.name, "skill": skill, "attempt_idx": _idx})
        actor.debuffs[kind] = actor.debuffs.get(kind, 0) + 1
        actor.debuff_skills[skill] = actor.debuff_skills.get(skill, 0) + 1

    def resolve(item: tuple[float, int, str, str, str | None, str | None, tuple[float, int] | None]) -> None:
        ts, idx, kind, category, victim, source, cast_start = item
        if kind == "debuff":
            credit_debuff(ts, idx, category, victim)
        else:
            credit(ts, idx, category, victim, source, cast_start)

    def resolve_due(ts: float, idx: int) -> None:
        while pending and (
            ts - pending[0][0] > max(RESULT_LOOKAHEAD_S, RESULT_NEXT_LINES_S, RESULT_ADJACENT_S)
            or idx - pending[0][1] > RESULT_LOOKAHEAD_EVENTS
        ):
            resolve(pending.pop(0))

    for ev in events:  # who damaged whom, and which debuffs landed (used while crediting)
        if ev.kind in DAMAGE_KINDS and ev.actor and ev.target:
            hostile.add((canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)))
        if ev.kind == "debuff" and ev.outcome:
            debuff_lines.append((float(ev.ts), canon.get(ev.target, ev.target) if ev.target else None, ev.outcome))
    for idx, ev in enumerate(events):
        resolve_due(float(ev.ts), idx)
        if ev.kind == "cast" and ev.actor:
            began[canon.get(ev.actor, ev.actor)] = (float(ev.ts), idx)
        if ev.kind in ("status", "ability_hit", "ability_partial", "ability_miss", "melee_miss", "melee_hit", "cast") and ev.skill:
            note_attempt(ev, idx)
            note_action(ev, idx)
        if ev.kind == "debuff":
            victim = canon.get(ev.target, ev.target) if ev.target else None
            get(ev.target)
            if ev.outcome and ev.outcome not in _DAMAGE_ONLY_EFFECTS | {"lockout"}:
                pending.append((float(ev.ts), idx, "debuff", ev.outcome, victim, None, None))
            continue
        if ev.kind == "aggro":
            holder = get(ev.target)
            get(ev.actor)
            if holder is not None and not is_npc_name(holder.name):
                if not any(t[1] is holder and t[2] == canon.get(ev.actor, ev.actor) and float(ev.ts) - t[0] <= CREDIT_WINDOW_S
                           for t in taunts):
                    holder.aggro += 1  # a taunt just before already counted this
            continue
        if ev.kind == "status" and (ev.skill or "").lower() in ("taunt", "taunts") and ev.actor and ev.target:
            # "You taunt a dunes madman.": the game prints no "looks angry at you" for the viewer, so
            # the taunt itself is the aggro action (one per taunt; an "angry at" line right after it
            # is the same aggro).
            taunter = get(ev.actor)
            get(ev.target)
            if taunter is not None and not is_npc_name(taunter.name):
                taunter.aggro += 1
                taunter.taunts += 1
                taunts.append((float(ev.ts), taunter, canon.get(ev.target, ev.target)))
            continue
        if ev.kind == "interrupt":
            caster = canon.get(ev.actor, ev.actor) if ev.actor else None
            get(ev.actor)
            pending.append((float(ev.ts), idx, "cc", "interrupt", caster, None, began.get(caster) if caster else None))
            continue
        if ev.kind == "cc":
            victim = canon.get(ev.target, ev.target) if ev.target else None
            get(ev.target)
            if ev.outcome:
                pending.append((float(ev.ts), idx, "cc", ev.outcome, victim, (ev.skill or "").lower() or None, None))
            continue
        if ev.kind in DAMAGE_KINDS:
            amount = int(ev.amount or 0)
            actor = get(ev.actor)
            real = not ev.estimated  # an estimate is an average: never a "max hit"
            if actor is not None:
                actor.damage += amount
                actor.damaging_hits += 1
                if real:
                    actor.max_hit = max(actor.max_hit, amount)
                else:
                    actor.estimated += 1
                if ev.kind == "melee_hit":
                    actor.hits += 1
                if ev.is_pet:
                    actor.is_pet = True
                sk = actor.skill(_skill_key(ev))
                sk.total += amount
                sk.hits += 1
                if real:
                    sk.max_hit = max(sk.max_hit, amount)
            target = get(ev.target)
            if target is not None:
                target.taken += amount
                target.prevented += int(ev.blocked or 0) + int(ev.absorbed or 0)
                source = target.taken_source(f"{canon.get(ev.actor, ev.actor) if ev.actor else '?'}: {_skill_key(ev)}")
                source.total += amount
                source.hits += 1
                if real:
                    source.max_hit = max(source.max_hit, amount)
        elif ev.kind == "melee_miss":
            actor = get(ev.actor)
            if actor is not None:
                actor.misses += 1  # "absorb", "dodge", "parry" ... all count as a miss
                if ev.is_pet:
                    actor.is_pet = True
                actor.skill(_skill_key(ev)).misses += 1
            get(ev.target)  # the defender still belongs to the fight
        elif ev.kind in MISS_KINDS:
            actor = get(ev.actor)
            if actor is not None:
                if ev.is_pet:
                    actor.is_pet = True
                actor.skill(_skill_key(ev)).misses += 1
        elif ev.kind == "env_damage":
            # falling and the like: damage taken with no attacker (never a damage dealer row)
            amount = int(ev.amount or 0)
            target = get(ev.target)
            if target is not None:
                target.taken += amount
                source = target.taken_source(f"environment: {ev.skill or 'damage'}")
                source.total += amount
                source.hits += 1
                source.max_hit = max(source.max_hit, amount)
        elif ev.kind == "heal":
            amount = int(ev.amount or 0)
            actor = get(ev.actor)
            if actor is not None:
                actor.heals += amount
                spell = actor.heal_skill(_skill_key(ev))
                spell.total += amount
                spell.hits += 1
                if ev.estimated:
                    actor.estimated += 1
                else:
                    actor.max_heal = max(actor.max_heal, amount)
                    spell.max_hit = max(spell.max_hit, amount)
                if ev.is_pet:
                    actor.is_pet = True
                healed = canon.get(ev.target, ev.target) if ev.target else None
                actor.heal_log.append((healed, _skill_key(ev), amount, bool(ev.estimated)))
            target = get(ev.target)
            if target is not None:
                target.healed += amount
        elif ev.is_pet and ev.actor:
            acc = accs.get(canon.get(ev.actor, ev.actor))
            if acc is not None:
                acc.is_pet = True
    for item in pending:
        resolve(item)
    return accs


def _credit_default_utility(
    events: list[Event], canon: dict[str, str], accs: dict[str, _Acc],
    enemies: set[str], group: set[str], credited_actions: set[int],
) -> None:
    # A named action on an actual enemy is useful even when a new ability has no
    # dedicated effect rule yet. Damage, failures, and already credited result lines
    # never earn a second Utility point. Target validation rejects status prose that
    # merely resembles an NPC name ("You feel the touch of earth", for example).
    damaging_skills: set[str] = set()
    combatants: set[str] = set()
    for ev in events:
        if ev.skill and ev.kind in DAMAGE_KINDS | {"ability_partial"}:
            damaging_skills.add(canonical_skill(ev.skill))
        if ev.kind in ACTIVITY_KINDS and ev.actor and ev.target:
            actor, target = canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)
            if actor != target:
                combatants.update((actor, target))
    for idx, ev in enumerate(events):
        if ev.kind != "status" or not ev.actor or not ev.target or not ev.skill or idx in credited_actions:
            continue
        actor, target = canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)
        key = canonical_skill(ev.skill)
        if (actor in enemies or (is_npc_name(actor) and actor not in group)
                or target not in enemies or target not in combatants or ev.amount is not None
                or lemmatize(key) in _DAMAGE_ACTIONS | _NONCOMBAT_ACTIONS
                or key in damaging_skills or key in {"taunt", "taunts"}):
            continue
        if ev.outcome and any(word in ev.outcome.lower().split() for word in
                              ("immune", "failed", "fails", "failure", "resisted", "miss", "missed", "cannot", "longer")):
            continue
        failed = False
        for other_idx in range(max(0, idx - RESULT_NEXT_LINES), min(len(events), idx + RESULT_NEXT_LINES + 1)):
            other = events[other_idx]
            if abs(float(other.ts) - float(ev.ts)) > RESULT_ADJACENT_S:
                continue
            if (other.kind in MISS_KINDS and canon.get(other.actor, other.actor) == actor
                    and canon.get(other.target, other.target) == target
                    and canonical_skill(other.skill or "") == key):
                failed = True
            if (other.kind == "status" and (other.outcome or "").lower() == "immune"
                    and canon.get(other.actor, other.actor) == target
                    and canon.get(other.target, other.target) == actor):
                source = re.search(r"\bimmune\s+to\s+.+?(?:['’]s|s)\s+(.+?)[.!?]*$", other.text, re.IGNORECASE)
                if source is None or canonical_skill(source.group(1).strip()) == key:
                    failed = True
        if failed:
            continue
        acc = accs.get(actor)
        if acc is not None:
            acc.debuffs["other utility"] = acc.debuffs.get("other utility", 0) + 1
            skill = _skill_key(ev)
            acc.debuff_skills[skill] = acc.debuff_skills.get(skill, 0) + 1


def _drop_enemy_heals(acc: _Acc, enemies: set[str]) -> None:
    """Move ``acc``'s heals on ``enemies`` out of its healing into ``enemy_heals``.

    A heal that lands on the other side (a group heal catching a PvP foe, a heal on a mob)
    is not the group's healing: it stays out of the heals, the per-spell breakdown, the
    largest heal and the HPS.  An enemy's own heals (a mob healing itself) are untouched.
    """
    if acc.name in enemies or not any(target in enemies for target, _s, _n, _e in acc.heal_log):
        return
    acc.heals = acc.max_heal = 0
    acc.heal_skills = {}
    for target, skill, amount, estimated in acc.heal_log:
        if target in enemies:
            acc.enemy_heals += amount
            continue
        acc.heals += amount
        spell = acc.heal_skill(skill)
        spell.total += amount
        spell.hits += 1
        if not estimated:
            acc.max_heal = max(acc.max_heal, amount)
            spell.max_hit = max(spell.max_hit, amount)


def _row(acc: _Acc, duration: float, total_damage: int, you_name: str) -> ActorRow:
    swings = acc.hits + acc.misses
    is_you = acc.name == you_name
    is_npc = is_npc_name(acc.name) and not acc.is_pet
    skills = sorted((s.row() for s in acc.skills.values()), key=lambda r: (-r.total, r.skill))
    return ActorRow(
        name=acc.name,
        damage=acc.damage,
        dps=round(acc.damage / duration, 2),
        taken=acc.taken,
        dtps=round(acc.taken / duration, 2),
        heals=acc.heals,
        hps=round(acc.heals / duration, 2),
        healed=acc.healed,
        swings=swings,
        hits=acc.hits,
        misses=acc.misses,
        hit_pct=round(100.0 * acc.hits / swings, 2) if swings else 0.0,
        max_hit=acc.max_hit,
        avg_hit=round(acc.damage / acc.damaging_hits, 2) if acc.damaging_hits else 0.0,
        share=(acc.damage / total_damage) if total_damage > 0 else 0.0,
        color=actor_color(acc.name, is_you=is_you, is_npc=is_npc, is_pet=acc.is_pet),
        is_you=is_you,
        is_npc=is_npc,
        is_pet=acc.is_pet,
        skills=skills,
        max_heal=acc.max_heal,
        cc=acc.cc_landed,
        cc_attempts=acc.cc_attempts,
        cc_types={cat: acc.cc_types[cat] for cat in CC_CATEGORIES if acc.cc_types.get(cat)},
        heal_skills=sorted((s.row() for s in acc.heal_skills.values()), key=lambda r: (-r.total, r.skill)),
        taken_from=sorted((s.row() for s in acc.taken_from.values()), key=lambda r: (-r.total, r.skill)),
        cc_skills=dict(sorted(acc.cc_skills.items(), key=lambda kv: (-kv[1], kv[0]))),
        utility=acc.cc_landed + sum(acc.debuffs.values()) + acc.aggro,
        debuffs=dict(sorted(acc.debuffs.items(), key=lambda kv: (-kv[1], kv[0]))),
        debuff_skills=dict(sorted(acc.debuff_skills.items(), key=lambda kv: (-kv[1], kv[0]))),
        aggro=acc.aggro,
        prevented=acc.prevented,
        estimated=acc.estimated,
        taunts=acc.taunts,
        enemy_heals=acc.enemy_heals,
    )


def self_rows(snap: EncounterSnapshot, tab: str, player_name: str) -> list[ActorRow]:
    """The viewer's own sources for ``tab``, shaped like actor rows so the meter can show them.

    ``damage``/``overview``: one row per ability (plus plain melee) with the damage done and,
    for the overview, the healing and CC landed by that ability; ``healing``: one row per
    healing spell; ``taken``: one row per "attacker: skill".  ``share`` is the fraction of
    the viewer's own total, so the bars compare the viewer's sources with each other.
    """
    me = owner_row(snap, player_name or "You")
    if me is None:
        return []
    duration = max(1.0, snap.duration)
    color = me.color

    def make(name: str, **values: Any) -> ActorRow:
        base: dict[str, Any] = dict(
            name=name, damage=0, dps=0.0, taken=0, dtps=0.0, heals=0, hps=0.0, healed=0,
            swings=0, hits=0, misses=0, hit_pct=0.0, max_hit=0, avg_hit=0.0, share=0.0,
            color=color, is_you=False, is_npc=False, is_pet=False,
        )
        base.update(values)
        return ActorRow(**base)

    rows: list[ActorRow] = []
    if tab in ("damage", "overview"):
        names: list[str] = [s.skill for s in me.skills]
        if tab == "overview":
            names += [s.skill for s in me.heal_skills if s.skill not in names]
            names += [n for n in me.cc_skills if n not in names]
            names += [n for n in me.debuff_skills if n not in names]
        dmg = {s.skill: s for s in me.skills}
        heal = {s.skill: s for s in me.heal_skills}
        for name in names:
            d, h = dmg.get(name), heal.get(name)
            total = d.total if d else 0
            swings = (d.hits + d.misses) if d else 0
            cc = me.cc_skills.get(name, 0)
            debuff = me.debuff_skills.get(name, 0)
            rows.append(make(
                name,
                damage=total, dps=round(total / duration, 2),
                heals=h.total if h else 0, hps=round((h.total if h else 0) / duration, 2),
                swings=swings, hits=d.hits if d else 0, misses=d.misses if d else 0,
                hit_pct=round(100.0 * d.hits / swings, 2) if d and swings else 0.0,
                max_hit=d.max_hit if d else 0, avg_hit=d.avg if d else 0.0,
                share=(total / me.damage) if me.damage else 0.0,
                cc=cc, utility=cc + debuff, max_heal=h.max_hit if h else 0,
                debuffs={name: debuff} if debuff else {},
            ))
        if tab == "overview" and me.aggro:
            rows.append(make("Aggro gained", utility=me.aggro, aggro=me.aggro))
        key = (lambda r: (-r.dps, -r.hps, -r.utility, r.name)) if tab == "overview" else (lambda r: (-r.damage, r.name))
        rows.sort(key=key)
    elif tab == "healing":
        for s in me.heal_skills:
            rows.append(make(s.skill, heals=s.total, hps=round(s.total / duration, 2), max_heal=s.max_hit,
                             share=(s.total / me.heals) if me.heals else 0.0))
    elif tab == "taken":
        for s in me.taken_from:
            rows.append(make(s.skill, taken=s.total, dtps=round(s.total / duration, 2), max_hit=s.max_hit,
                             share=(s.total / me.taken) if me.taken else 0.0))
    return rows


_ATTACK_KINDS = frozenset({"melee_hit", "melee_miss", "ability_hit"})
_PARTICIPATION_KINDS = _ATTACK_KINDS | {"ability_miss", "resist", "kill"}
DEATH_REPEAT_S = 30.0  #: the same player "slain" again this soon is the same death read twice


def _deaths(events: list[Event], canon: dict[str, str]) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
    """``(deaths per name, {name: {killer: n}})`` from the fight's kill lines.  Every NPC death
    counts (three skeletons of one name die three times); a player's repeat within
    DEATH_REPEAT_S is one death (a chat line read twice)."""
    deaths: dict[str, int] = {}
    killers: dict[str, dict[str, int]] = {}
    last: dict[str, float] = {}
    for ev in events:
        if ev.kind != "kill" or not ev.target:
            continue
        victim = canon.get(ev.target, ev.target)
        if not is_npc_name(victim):
            if ev.ts - last.get(victim, -1e9) < DEATH_REPEAT_S:
                continue
            last[victim] = ev.ts
        deaths[victim] = deaths.get(victim, 0) + 1
        killer = canon.get(ev.actor, ev.actor) if ev.actor else "?"
        by = killers.setdefault(victim, {})
        by[killer] = by.get(killer, 0) + 1
    return deaths, killers


def _classify_sides(
    events: list[Event], canon: dict[str, str], accs: dict[str, _Acc], you_name: str, members: set[str],
    pet_owners: dict[str, str] | None = None, excluded: set[str] | None = None,
) -> tuple[set[str], set[str]]:
    """``(enemies, group)``: the actor names fighting the group, and the ones counted as the
    group (in the totals).

    The group is the viewer, their pet and the party (``members``).  Enemies are the NPCs
    plus anyone who hit the group or was hit by it (a named mob without "a/an/the", a player
    in PvP).  Whoever hits an enemy, or is hit by one, is on the group's side; whoever fights
    them is an enemy too.  Players on the group's side who are not in the party are outsiders
    (other groups nearby): neither enemy nor group. An empty roster means only the
    viewer and their pets count; fighting nearby never establishes membership.
    """
    names = set(accs)
    pet_owners = pet_owners or {}
    excluded = excluded or set()
    # Keep identities even when only a kill/death line mentions them; such lines
    # prove participation but do not necessarily create a numeric accumulator.
    group = ({you_name} | members) - excluded
    # A manually assigned pet follows its owner, even if its name once looked like a mob.
    group -= set(pet_owners)
    group |= {pet for pet, owner in pet_owners.items() if owner in group}
    group -= excluded - set(pet_owners)
    enemies = {name for name in names if is_npc_name(name)} - group - set(pet_owners)
    edges: set[tuple[str, str]] = set()
    for ev in events:
        if ev.kind in _ATTACK_KINDS and ev.actor and ev.target:
            a, t = canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)
            if a != t and a in names and t in names:
                edges.add((a, t))
    friends: set[str] = set()
    for _ in range(8):
        before = (len(enemies), len(friends))
        for a, t in edges:  # fighting the group makes an enemy
            if a in group and t not in group:
                enemies.add(t)
            elif t in group and a not in group:
                enemies.add(a)
        for a, t in edges:  # fighting an enemy puts you on the group's side
            if t in enemies and a not in enemies:
                friends.add(a)
            elif a in enemies and t not in enemies:
                friends.add(t)
        for a, t in edges:  # fighting the group's side (outsiders) makes an enemy
            if t in friends and a not in friends and a not in group:
                enemies.add(a)
            elif a in friends and t not in friends and t not in group:
                enemies.add(t)
        enemies -= group | set(pet_owners)
        friends -= enemies
        if (len(enemies), len(friends)) == before:
            break
    return enemies, group - enemies


def _group_participated(
    events: list[Event], canon: dict[str, str], accs: dict[str, _Acc],
    group: set[str], enemies: set[str],
) -> bool:
    """Require combat involvement, rather than merely a group name in the log.

    Self-buffs, regeneration and looting during someone else's fight are not
    participation. Healing a friendly combatant or landing credited utility is.
    Helping a stranger does not add that stranger to the party.
    """
    combatants: set[str] = set()
    for ev in events:
        if ev.kind not in _PARTICIPATION_KINDS or not ev.actor or not ev.target:
            continue
        actor, target = canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)
        if actor == target:
            continue
        combatants.update((actor, target))
        if (actor in group) != (target in group):
            return True
    for ev in events:
        if ev.kind != "heal" or not ev.actor or not ev.target or not ev.amount or ev.amount <= 0:
            continue
        actor, target = canon.get(ev.actor, ev.actor), canon.get(ev.target, ev.target)
        if actor in group and actor != target and target in combatants and target not in enemies:
            return True
    return any(
        acc.name in group and (acc.cc_landed or acc.debuffs or acc.aggro)
        for acc in accs.values()
    )


def _group_span(events: list[Event], canon: dict[str, str], group: set[str]) -> tuple[float, float] | None:
    """``(first, last)`` swing or hit by the group in ``events``, or ``None`` when it has none.

    The fight's rates are per second of this span: NPCs fighting each other and other groups'
    swings open, stretch or prolong an encounter, but they are not the group's fighting time.
    """
    times = [
        float(ev.ts) for ev in events
        if ev.kind in ACTIVITY_KINDS and ev.actor and canon.get(ev.actor, ev.actor) in group
    ]
    return (min(times), max(times)) if times else None


def _fight_label(enc: Encounter, canon: dict[str, str], enemies: set[str], group: set[str]) -> str:
    """Who the group fought: the NPCs hit in the fight plus any other enemy (a named mob, a
    PvP attacker), never the group or bystanders (a gank used to be named after its victim)."""
    targets = {canon.get(n, n) for n in enc.target_names}
    foes = {n for n in targets if is_npc_name(n)} | {n for n in enemies if not is_npc_name(n)}
    foes -= group
    if not foes:
        # Nobody the group fought (two other players dueling nearby): name who was hit.
        foes = (targets - group) or targets
    return ", ".join(sorted(foes)) if foes else "unknown"


def build_snapshot(
    stats: Stats, enc: Encounter, player_name: str, *, now: float | None = None
) -> EncounterSnapshot:
    """Build the display snapshot of ``enc``.

    Args:
        stats: The aggregator that owns ``enc`` (used for name canonicalisation
            and the label).
        enc: The encounter (open or closed).
        player_name: The configured character name; ``"You"`` rows carry this
            name when it is set (the parser already maps the pronouns).
        now: Optional wall-clock time (``time.time()``); while the encounter is
            still open it extends ``end`` and ``duration`` so the meter ticks between
            events (``active_duration`` never does).

    The rates (DPS, HPS, damage taken per second) are per second of the group's own
    fighting, from its first to its last swing or hit: NPCs fighting each other and other
    groups' swings before or after it do not stretch the fight.  A fight the group took
    no swing in keeps the encounter's own span.

    Returns:
        An :class:`EncounterSnapshot` with rows sorted by damage (desc) then
        name.  An encounter without combat events yields an empty ``rows`` list.
    """
    canon = stats.canonical_map(enc)
    you_name = player_name or "You"
    roster = getattr(stats, "roster", None)
    assignments = roster.pet_owners() if roster is not None else {}
    # Explicit ownership declares two distinct identities, even when OCR would otherwise
    # merge their similar spellings. Apply this before accumulating either one's numbers.
    identities = set(assignments) | {
        you_name if owner.casefold() in {"you", you_name.casefold()} else owner
        for owner in assignments.values()
    }
    by_canonical: dict[str, set[str]] = {}
    for name in identities:
        by_canonical.setdefault(canon.get(name, name), set()).add(name)
    for names in by_canonical.values():
        if len(names) > 1:
            for name in names:
                canon[name] = name
    vocab = getattr(stats, "vocab", None)
    utility_credited: set[int] = set()
    accs = _accumulate(enc.events, canon, vocab=vocab, utility_credited=utility_credited)

    party = set(getattr(stats, "party", ()) or ())
    if vocab is not None:
        party |= {vocab.canonical("player", n) or n for n in party}
    party |= {canon.get(n, n) for n in party}
    pet_owners = {acc.name: you_name for acc in accs.values() if acc.is_pet}
    if assignments:
        for pet, owner in assignments.items():
            owner = you_name if owner.casefold() in {"you", you_name.casefold()} else canon.get(owner, owner)
            pet_owners[canon.get(pet, pet)] = owner
    for pet in pet_owners:
        if pet in accs:
            accs[pet].is_pet = True
    excluded = {canon.get(n, n) for n in getattr(roster, "manual_out", ())}
    enemies, group = _classify_sides(enc.events, canon, accs, you_name, party, pet_owners, excluded)
    _credit_default_utility(enc.events, canon, accs, enemies, group, utility_credited)
    for acc in accs.values():
        _drop_enemy_heals(acc, enemies)

    span = _group_span(enc.events, canon, group)
    if span is None:  # the group never swung (another group's fight): the encounter's own span
        first = enc.start
        last = enc.end if enc.closed or enc.last_activity < enc.start else enc.last_activity
    else:
        first, last = span
    active_duration = max(1.0, last - first)
    end = enc.end
    duration = active_duration
    if now is not None and not enc.closed:
        end = max(end, float(now))
        duration = max(1.0, max(last, float(now)) - first)

    total_damage = sum(a.damage for a in accs.values())
    rows = [_row(acc, duration, total_damage, you_name) for acc in accs.values()]
    if vocab is not None:
        for row in rows:
            _canonical_skills(row, vocab)
    rows.sort(key=lambda r: (-r.damage, r.name))

    killed = sorted({canon.get(n, n) for n in enc.killed_names})
    ours = _group_participated(enc.events, canon, accs, group, enemies)
    deaths, killers = _deaths(enc.events, canon)
    for r in rows:
        r.deaths = deaths.get(r.name, 0)
        r.killed_by = killers.get(r.name, {})
    group_damage = sum(r.damage for r in rows if r.name in group)
    others_misses = bool(getattr(stats, "others_misses_seen", False))
    for r in rows:
        r.pet_owner = pet_owners.get(r.name, "")
        r.is_enemy = r.name in enemies
        r.in_group = r.name in group
        r.misses_shown = r.is_you or r.pet_owner == you_name or r.is_npc or r.is_enemy or others_misses
        if r.is_enemy and not r.is_npc:  # a named enemy: drawn like the other enemies
            r.color = actor_color(r.name, is_you=False, is_npc=True, is_pet=False)
        # The group's shares add up to 100 %; anyone else's is of everything dealt.
        denom = group_damage if r.in_group else total_damage
        r.share = (r.damage / denom) if denom > 0 else 0.0
    count_kills = getattr(stats, "kill_count", None)
    kills = int(count_kills(enc)) if callable(count_kills) else len(killed)
    visit_at = getattr(stats, "zone_visit_at", None)
    zone, zone_since = visit_at(enc.start) if callable(visit_at) else ("", 0.0)
    snap = EncounterSnapshot(
        key=f"{enc.start:.3f}",
        label=_fight_label(enc, canon, enemies, group),
        start=enc.start,
        end=end,
        duration=duration,
        closed=enc.closed,
        event_count=len(enc.events),
        total_damage=group_damage,  # the group's own (no enemies, no outsiders)
        raid_dps=round(group_damage / duration, 2),
        killed=killed,
        rows=rows,
        zone=str(zone),
        zone_since=float(zone_since),
        ours=ours,
        kills=kills,
        active_duration=active_duration,
        group_members=sorted(({you_name} | party) - set(pet_owners), key=str.casefold),
    )
    owners = {row.pet_owner for row in rows if row.pet_owner}
    if owners:
        combined = [owner_row(snap, owner, you_name=you_name) for owner in sorted(owners)]
        snap.rows = [row for row in rows if not row.pet_owner and row.name not in owners]
        snap.rows.extend(row for row in combined if row is not None)
        snap.rows.sort(key=lambda row: (-row.damage, row.name))
    return snap


# ---------------------------------------------------------------------------
# Zone summaries (several encounters merged, like ACT's "All" entry)
# ---------------------------------------------------------------------------


def _canonical_skills(row: ActorRow, vocab: Any) -> None:
    """Merge OCR variants of ability names in ``row``'s breakdowns ("Righteou's Smite")."""

    def rename(rows: list[SkillRow]) -> list[SkillRow]:
        mapping = vocab.canonical_map("skill", [r.skill for r in rows])
        if all(mapping.get(r.skill, r.skill) == r.skill for r in rows):
            return rows
        renamed = [dataclasses.replace(r, skill=mapping.get(r.skill, r.skill)) for r in rows]
        return _merge_skill_rows([renamed])

    def rename_counts(counts: dict[str, int]) -> dict[str, int]:
        mapping = vocab.canonical_map("skill", counts.keys())
        out: dict[str, int] = {}
        for name, n in counts.items():
            key = mapping.get(name, name)
            out[key] = out.get(key, 0) + n
        return _sorted_counts(out)

    row.skills = rename(row.skills)
    row.heal_skills = rename(row.heal_skills)
    row.cc_skills = rename_counts(row.cc_skills)
    row.debuff_skills = rename_counts(row.debuff_skills)


def _merge_skill_rows(groups: Iterable[list[SkillRow]]) -> list[SkillRow]:
    totals: dict[str, list[int]] = {}  # skill -> [count, total, max_hit, hits, misses]
    for rows in groups:
        for r in rows:
            t = totals.setdefault(r.skill, [0, 0, 0, 0, 0])
            t[0] += r.count
            t[1] += r.total
            t[2] = max(t[2], r.max_hit)
            t[3] += r.hits
            t[4] += r.misses
    merged = [
        SkillRow(skill, count, total, max_hit, round(total / hits, 2) if hits else 0.0, hits, misses)
        for skill, (count, total, max_hit, hits, misses) in totals.items()
    ]
    merged.sort(key=lambda r: (-r.total, r.skill))
    return merged


def _add_counts(target: dict[str, int], source: dict[str, int]) -> None:
    for key, n in source.items():
        target[key] = target.get(key, 0) + n


def _sorted_counts(counts: dict[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _sum_counts(dicts: Iterable[dict[str, int]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0) + int(v)
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def merge_snapshots(
    snaps: Sequence[EncounterSnapshot],
    *,
    key: str,
    label: str,
    zone: str = "",
    zone_since: float = 0.0,
) -> EncounterSnapshot:
    """One snapshot summing ``snaps`` (a zone visit's summary).

    Like ACT's "All" entry: the duration is the time spent in those fights (their
    durations added up, and their ``active_duration`` likewise), so DPS and HPS are per
    second of combat; totals and counts add up, maxima take the largest, and per-skill
    breakdowns merge by skill name.
    """
    snaps = [s for s in snaps if s is not None]
    duration = max(1.0, sum(s.duration for s in snaps))
    by_name: dict[str, list[ActorRow]] = {}
    for snap in snaps:
        for row in snap.rows:
            by_name.setdefault(row.name, []).append(row)
    total_damage = sum(s.total_damage for s in snaps)  # the group's (see build_snapshot)
    all_damage = sum(r.damage for parts in by_name.values() for r in parts)
    rows: list[ActorRow] = []
    for name, parts in by_name.items():
        first = parts[0]
        damage = sum(r.damage for r in parts)
        taken = sum(r.taken for r in parts)
        heals = sum(r.heals for r in parts)
        hits = sum(r.hits for r in parts)
        misses = sum(r.misses for r in parts)
        swings = sum(r.swings for r in parts)
        damaging = sum((r.damage / r.avg_hit) for r in parts if r.avg_hit)
        cc_types: dict[str, int] = {}
        cc_skills: dict[str, int] = {}
        debuffs: dict[str, int] = {}
        debuff_skills: dict[str, int] = {}
        for r in parts:
            _add_counts(cc_types, r.cc_types)
            _add_counts(cc_skills, r.cc_skills)
            _add_counts(debuffs, r.debuffs)
            _add_counts(debuff_skills, r.debuff_skills)
        rows.append(
            ActorRow(
                name=name,
                damage=damage,
                dps=round(damage / duration, 2),
                taken=taken,
                dtps=round(taken / duration, 2),
                heals=heals,
                hps=round(heals / duration, 2),
                healed=sum(r.healed for r in parts),
                swings=swings,
                hits=hits,
                misses=misses,
                hit_pct=round(100.0 * hits / swings, 2) if swings else 0.0,
                max_hit=max(r.max_hit for r in parts),
                avg_hit=round(damage / damaging, 2) if damaging else 0.0,
                share=(damage / (total_damage if any(r.in_group for r in parts) else all_damage))
                if (total_damage if any(r.in_group for r in parts) else all_damage) > 0 else 0.0,
                color=first.color,
                is_you=first.is_you,
                is_npc=first.is_npc,
                is_pet=first.is_pet,
                skills=_merge_skill_rows(r.skills for r in parts),
                max_heal=max(r.max_heal for r in parts),
                cc=sum(r.cc for r in parts),
                cc_attempts=sum(r.cc_attempts for r in parts),
                cc_types={cat: cc_types[cat] for cat in CC_CATEGORIES if cc_types.get(cat)},
                heal_skills=_merge_skill_rows(r.heal_skills for r in parts),
                taken_from=_merge_skill_rows(r.taken_from for r in parts),
                cc_skills=_sorted_counts(cc_skills),
                utility=sum(r.utility for r in parts),
                debuffs=_sorted_counts(debuffs),
                debuff_skills=_sorted_counts(debuff_skills),
                aggro=sum(r.aggro for r in parts),
                prevented=sum(r.prevented for r in parts),
                estimated=sum(r.estimated for r in parts),
                taunts=sum(r.taunts for r in parts),
                enemy_heals=sum(int(getattr(r, "enemy_heals", 0) or 0) for r in parts),
                in_group=any(r.in_group for r in parts),
                is_enemy=any(r.is_enemy for r in parts),
                misses_shown=all(r.misses_shown for r in parts),
                deaths=sum(int(getattr(r, "deaths", 0) or 0) for r in parts),
                killed_by=_sum_counts(getattr(r, "killed_by", {}) or {} for r in parts),
                pet_owner=next((r.pet_owner for r in reversed(parts) if r.pet_owner), ""),
                attributed_pets=sorted({pet for r in parts for pet in r.attributed_pets}, key=str.casefold),
            )
        )
    rows.sort(key=lambda r: (-r.damage, r.name))
    killed = sorted({k for s in snaps for k in s.killed})
    start = min((s.start for s in snaps), default=0.0)
    end = max((s.end for s in snaps), default=start)
    return EncounterSnapshot(
        key=key,
        label=label,
        start=start,
        end=end,
        duration=duration,
        closed=all(s.closed for s in snaps),
        event_count=sum(s.event_count for s in snaps),
        total_damage=total_damage,
        raid_dps=round(total_damage / duration, 2),
        killed=killed,
        rows=rows,
        zone=zone,
        zone_since=zone_since,
        encounters=sum(max(1, s.encounters) for s in snaps),
        ours=any(s.ours for s in snaps),
        kills=sum(int(getattr(s, "kills", 0) or 0) for s in snaps),
        active_duration=max(1.0, sum(float(getattr(s, "active_duration", 0.0) or s.duration) for s in snaps)),
        group_members=sorted({name for s in snaps for name in s.group_members}, key=str.casefold),
    )


def owner_row(snap: EncounterSnapshot, owner: str, *, you_name: str | None = None) -> ActorRow | None:
    """The owner's combined parse, also accepting older snapshots with separate pet rows.

    Pet skills include their source name, so the personal breakdown explains exactly what
    contributed. This also works when only the pet acted in the encounter.
    """
    direct = next((r for r in snap.rows if r.name == owner or (r.is_you and owner == "You")), None)
    name = direct.name if direct is not None else owner
    pets = [r for r in snap.rows if r is not direct and r.name != name and r.pet_owner == name]
    if not pets:
        return direct
    is_you = direct.is_you if direct is not None else name == (you_name or "You")
    is_enemy = direct.is_enemy if direct is not None else False
    color = direct.color if direct is not None else actor_color(name, is_you=is_you, is_npc=is_enemy, is_pet=False)
    parts = [direct] if direct is not None else []
    for pet in pets:
        parts.append(dataclasses.replace(
            pet, name=name, is_pet=False, pet_owner="", is_you=is_you,
            is_npc=bool(direct and direct.is_npc), is_enemy=is_enemy, color=color,
            skills=[dataclasses.replace(s, skill=f"{pet.name}: {s.skill}") for s in pet.skills],
            heal_skills=[dataclasses.replace(s, skill=f"{pet.name}: {s.skill}") for s in pet.heal_skills],
            taken_from=[dataclasses.replace(s, skill=f"{pet.name}: {s.skill}") for s in pet.taken_from],
            cc_skills={f"{pet.name}: {k}": v for k, v in pet.cc_skills.items()},
            debuff_skills={f"{pet.name}: {k}": v for k, v in pet.debuff_skills.items()},
        ))
    merged = merge_snapshots([dataclasses.replace(snap, rows=parts)], key=snap.key, label=snap.label)
    return dataclasses.replace(
        merged.rows[0],
        share=sum(row.share for row in parts),
        attributed_pets=sorted(set(direct.attributed_pets if direct is not None else []) | {r.name for r in pets}, key=str.casefold),
    )


def snapshot_rows_for_tab(snap: EncounterSnapshot, tab: str) -> list[ActorRow]:
    """Rows relevant to a meter tab, in the order a meter shows them.

    ``"damage"``: actors that dealt damage or swung (sorted by damage desc);
    ``"healing"``: actors with heals (sorted by heals desc);
    ``"taken"``: actors that took damage (sorted by taken desc).
    Unknown tabs return every row.
    """
    if tab == "overview":
        active = [
            r for r in snap.rows
            if r.damage > 0 or r.swings > 0 or r.heals > 0 or r.taken > 0 or r.utility > 0 or r.cc_attempts > 0
        ]
        return sorted(active, key=lambda r: (-r.dps, -r.hps, -r.utility, r.name))
    if tab == "damage":
        return [r for r in snap.rows if r.damage > 0 or r.swings > 0]
    if tab == "healing":
        return sorted((r for r in snap.rows if r.heals > 0), key=lambda r: (-r.heals, r.name))
    if tab == "taken":
        return sorted((r for r in snap.rows if r.taken > 0), key=lambda r: (-r.taken, r.name))
    log.debug("snapshot_rows_for_tab: unknown tab %r; returning all rows", tab)
    return list(snap.rows)
