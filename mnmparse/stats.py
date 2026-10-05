"""Encounter segmentation and per-actor aggregation (SPEC section 6 / 8).

An :class:`Encounter` opens on the first combat event (a damaging hit or a
melee swing that missed), is extended by every later event, and closes only
when no damage/swing event has arrived for ``encounter_timeout_s`` seconds
(or at a zone change).  Kill lines are recorded (``killed_names``) but never
end a fight: whether a kill line is read cleanly, and whether it names every
enemy of the fight, varied from fight to fight, so fights ended at once or
dragged on unpredictably.  Non-combat events (casts, status lines, unknown
text) are appended to an open encounter for context but never open or
extend one, and so are hits between two NPCs ("a dunes madman hits a famished
zombie ..."): mobs fighting each other, or another group's charmed mob, are not
the viewer's fight.

When a fight closes, the players who fought on the viewer's side in it are
reported to the party roster (:meth:`mnmparse.party.PartyRoster.note_fight`),
which takes a player who shares several fights with the viewer as a member.

:class:`Stats` keeps the open encounter (``current()``) and the closed
ones (``history``) and renders the per-actor table shown in SPEC
section 8::

    Encounter vs a stumbling zombie - 00:42 - 57 events
    Actor         Damage     DPS  Swings  Hits  Miss   Hit%   Max    Avg  Heals
    Pidef             61     1.5       9     8     1  88.9%    24    7.6      0
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .grammar import Event, is_npc_name, is_you
from .party import PartyRoster
from .vocab import Vocabulary, clean, close_spellings, fold, observe_event

__all__ = ["Encounter", "Stats", "DAMAGE_KINDS", "ACTIVITY_KINDS", "OPENING_KINDS"]

log = logging.getLogger(__name__)

DAMAGE_KINDS: frozenset[str] = frozenset({"melee_hit", "ability_hit"})
"""Kinds that carry a damage amount."""

ACTIVITY_KINDS: frozenset[str] = DAMAGE_KINDS | {"melee_miss"}
"""Kinds that keep an encounter alive (reset the inactivity timeout)."""

OPENING_KINDS: frozenset[str] = ACTIVITY_KINDS
"""Kinds that open a new encounter when none is open.

Heals do not: an out-of-combat heal (regeneration, a top-up after the fight, an NPC
healing itself) made one-second "encounters" without any damage.  Heals inside an open
encounter still count.
"""

_NO_TARGET_LABEL = "unknown"
PREFIGHT_S = 20.0  #: how long before the first hit a cast / debuff / CC line still joins the fight
PREFIGHT_KINDS = frozenset({"cast", "debuff", "cc", "interrupt", "status", "aggro"})
#: "... tries to slash YOU, but misses!": aimed at the viewer (those misses are always shown).
_AT_YOU_RX = re.compile(r"\bYOU\b")


@dataclass
class Encounter:
    """One fight: a run of combat events against one or more targets.

    ``start``/``end`` are event timestamps (``end`` tracks the latest
    event while the encounter is open).  ``target_names`` are the NPCs
    involved (or, in a duel without NPCs, the attacked players);
    ``killed_names`` are those already reported slain.
    """

    start: float
    end: float
    events: list[Event] = field(default_factory=list)
    target_names: set[str] = field(default_factory=set)
    killed_names: set[str] = field(default_factory=set)
    last_activity: float = 0.0
    closed: bool = False
    damage_events: int = 0  #: melee and ability hits so far (an encounter without any is dropped)

    @property
    def has_damage(self) -> bool:
        return self.damage_events > 0

    @property
    def duration(self) -> float:
        """Seconds between the first and the last event (never negative)."""
        return max(0.0, self.end - self.start)

    def label(self) -> str:
        """``"a stumbling zombie, a rat"`` style target list for headers."""
        if not self.target_names:
            return _NO_TARGET_LABEL
        return ", ".join(sorted(self.target_names))


def _mmss(seconds: float) -> str:
    """Format a duration as ``mm:ss`` (minutes may exceed 59)."""
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


#: OCR glyph confusions seen in this game's font: p<->D, o<->0, l<->I/1, y<->v, and the
#: "ffi" ligature read as "m" ("a risen omcer").  Names are folded through these before
#: comparing so "WulDliso" == "Wululiso".
_FOLD = str.maketrans({"d": "p", "0": "o", "i": "l", "1": "l", "v": "y"})
_LIGATURES = (("ffi", "m"),)
NAME_SIMILARITY = 0.8


def _fold(name: str) -> str:
    name = name.lower()
    for ligature, read_as in _LIGATURES:
        name = name.replace(ligature, read_as)
    return name.translate(_FOLD)


def name_similar(a: str, b: str, *, vocab: Vocabulary | None = None) -> bool:
    """True when ``a`` and ``b`` are plausibly OCR readings of the same name.

    With ``vocab``, two names the vocabulary keeps apart as well-attested spellings of their
    own ("a jackal" and "a jackal pup", "a skeletal fighter" and "a skeletal cleric") are
    not similar however close they look (only the same glyphs misread still are).
    """
    fa, fb = _fold(a), _fold(b)
    if fa == fb:
        return True
    # A truncated OCR read ("a skeletal" for "a skeletal marksman") is a whole-word prefix
    # of the full name; NPC names always have at least an article plus one word.
    short, long_ = (fa, fb) if len(fa) < len(fb) else (fb, fa)
    if " " in short and long_.startswith(short + " "):
        return not _vocab_distinct(vocab, a, b)
    if fa[:1] != fb[:1] or abs(len(fa) - len(fb)) > 2:
        return False
    if difflib.SequenceMatcher(None, fa, fb, autojunk=False).ratio() < NAME_SIMILARITY:
        return False
    return not _vocab_distinct(vocab, a, b)


def _vocab_distinct(vocab: Vocabulary | None, a: str, b: str) -> bool:
    """True when ``vocab`` knows ``a`` and ``b`` as two different well-attested names."""
    distinct = getattr(vocab, "distinct", None) if vocab is not None else None
    if not callable(distinct):
        return False
    return bool(distinct("npc" if is_npc_name(a) or is_npc_name(b) else "player", a, b))


def _closeness(a: str, b: str) -> float:
    """How alike two names read after glyph folding (0..1)."""
    return difflib.SequenceMatcher(None, _fold(a), _fold(b), autojunk=False).ratio()


def canonical_names(counts: Counter[str], *, vocab: Vocabulary | None = None) -> dict[str, str]:
    """Map every name to the most frequent similar name.

    A rare reading is merged into a frequent one only when the frequent one has at
    least twice as many occurrences, so two genuinely different but similar names
    that both appear often stay separate (as do names ``vocab`` knows to be different,
    see :func:`name_similar`).  When several frequent names qualify, the closest spelling
    wins, so "a skeletal knighti" joins "a skeletal knight", not a commoner "a skeletal fighter".
    """
    canon: dict[str, str] = {}
    chosen: list[str] = []
    for name, count in counts.most_common():
        candidates = [c for c in chosen if counts[c] >= 2 * count and name_similar(name, c, vocab=vocab)]
        match = max(candidates, key=lambda c: _closeness(name, c)) if len(candidates) > 1 else next(iter(candidates), None)
        canon[name] = match or name
        if match is None:
            chosen.append(name)
    return canon


def _new_row(actor: str) -> dict[str, Any]:
    return {
        "actor": actor,
        "damage": 0,
        "dps": 0.0,
        "swings": 0,
        "hits": 0,
        "misses": 0,
        "hit_pct": 0.0,
        "max_hit": 0,
        "avg_hit": 0.0,
        "heals": 0,
        "_damaging_hits": 0,
    }


class Stats:
    """Aggregate parsed events into encounters and per-actor numbers."""

    def __init__(
        self, encounter_timeout_s: float = 12.0, *, vocab: Vocabulary | None = None, player_name: str = ""
    ) -> None:
        """``encounter_timeout_s``: seconds of no damage/swing events after
        which the open encounter is considered over.  ``vocab``: learned spellings used
        to merge OCR variants of zone and combatant names (and fed with every event).
        ``player_name``: the viewer's character name (the parser's stand-in for "You")."""
        self.encounter_timeout_s: float = float(encounter_timeout_s)
        self.history: list[Encounter] = []
        self._open: Encounter | None = None
        self.vocab = vocab
        self.player_name = str(player_name or "")
        #: ``(ts, zone name)`` for every zone line seen, in order (repeats of the same zone,
        #: OCR variants included, are dropped).
        self.zone_changes: list[tuple[float, str]] = []
        self.discarded = 0  #: encounters dropped at close because nothing was damaged
        #: Events seen while no fight is open, kept briefly: a pull often starts with a debuff or a
        #: cast before the first damage line ("Povebizu begins casting Distress.", "X's magical
        #: resistance frays.", then "Povebizu's Distress hits X ..."); they join the fight it opens.
        self._prefight: list[Event] = []
        #: Who is in the viewer's party (mnmparse.party): fights none of them (nor the viewer)
        #: took part in belong to other groups nearby, and only the group counts in the totals.
        canonical = (lambda name: vocab.canonical("player", name) or name) if vocab is not None else None
        self.roster = PartyRoster(self.player_name, canonical=canonical)
        #: Whether the chat shows other players' plain misses ("X tries to hit Y, but misses!").
        #: By default it does not (only the viewer's and the misses aimed at the viewer), so
        #: other players' hit rates are unknown until one such line is seen.
        self.others_misses_seen = False
        #: Dummy Fix: (zone visit or "*", group, actor, skill) -> [sum, count] of real amounts
        self._amounts: dict[tuple[Any, ...], list[float]] = {}

    # ------------------------------------------------------------------
    # Encounter lifecycle
    # ------------------------------------------------------------------

    def add(self, ev: Event) -> None:
        """Feed one event; opens, extends or closes encounters as needed."""
        observe_event(self.vocab, ev)
        if ev.amount is not None and not ev.estimated and (ev.kind in DAMAGE_KINDS or ev.kind == "heal"):
            self._learn_amount(ev)
        self.roster.observe(ev)
        if (
            not self.others_misses_seen and ev.kind == "melee_miss" and ev.outcome == "miss"
            and ev.actor and not is_you(ev.raw_actor or ev.actor) and not is_npc_name(ev.actor)
            and ev.target and not getattr(ev, "is_pet", False)
            and not _AT_YOU_RX.search(ev.text or "")  # a miss aimed at the viewer always shows
        ):
            self.others_misses_seen = True
        enc = self._open
        if enc is not None and ev.ts - enc.last_activity > self.encounter_timeout_s:
            self._close(enc, enc.last_activity, reason="timeout")
            enc = None

        if ev.kind == "zone":
            # "Entering X." / "Loading, please wait..." / "You have entered X.": the open
            # fight ends at the first of them, since nothing continues across a zone line.
            # The zone is remembered once (the two named lines, and OCR variants, are one change).
            if enc is not None:
                self._close(enc, enc.last_activity, reason="zone change")
            self._prefight.clear()
            if ev.target:
                zone = clean(ev.target)
                if zone and (not self.zone_changes or not self._same_zone(self.zone_changes[-1][1], zone)):
                    self.zone_changes.append((float(ev.ts), zone))
            return

        if ev.kind in OPENING_KINDS and self._npc_vs_npc(ev):
            # Mobs fighting each other: context for an open fight, never a fight of its own,
            # and no reason to keep one going.
            if enc is not None:
                enc.events.append(ev)
                enc.end = max(enc.end, ev.ts)
            return

        if ev.kind in OPENING_KINDS:
            if enc is None:
                enc = Encounter(start=ev.ts, end=ev.ts, last_activity=ev.ts)
                self._open = enc
                enc.events.extend(e for e in self._prefight if ev.ts - e.ts <= PREFIGHT_S)
                self._prefight.clear()
                log.info("encounter opened at %.1f by %s", ev.ts, ev.kind)
            enc.events.append(ev)
            enc.end = max(enc.end, ev.ts)
            if ev.kind in DAMAGE_KINDS:
                enc.damage_events += 1
            if ev.kind in ACTIVITY_KINDS:
                enc.last_activity = max(enc.last_activity, ev.ts)
                self._note_targets(enc, ev)
            return

        if enc is None:
            # nothing open; casts/status/unknown do not start a fight, but remember the ones that
            # may belong to the fight about to start
            if ev.kind in PREFIGHT_KINDS:
                self._prefight.append(ev)
                self._prefight = [e for e in self._prefight if ev.ts - e.ts <= PREFIGHT_S][-20:]
            return

        if ev.kind == "marker":
            # "(Block 6)" wrapped onto its own row: it qualifies the last hit line.
            last = next((e for e in reversed(enc.events) if e.kind in DAMAGE_KINDS), None)
            if last is not None:
                if ev.outcome == "block":
                    last.blocked = ev.amount
                elif ev.outcome == "absorb":
                    last.absorbed = ev.amount
                elif ev.outcome == "critical":
                    last.outcome = "critical"
            return

        enc.events.append(ev)
        enc.end = max(enc.end, ev.ts)
        if ev.kind == "kill" and ev.target:
            # Recorded, but the fight goes on until the timeout (see the module docstring).
            target = self._match_target(enc, ev.target)
            if target is not None:
                enc.killed_names.add(target)
            else:
                log.debug("kill of %r not part of the open encounter", ev.target)

    @property
    def party(self) -> set[str]:
        """The viewer's party members (seen in the chat or added by hand)."""
        return self.roster.members()

    def kill_count(self, enc: Encounter) -> int:
        """How many of the fight's enemies died (three "slain" lines for three skeletons = 3)."""
        return sum(
            1 for ev in enc.events
            if ev.kind == "kill" and ev.target and self._match_target(enc, ev.target) is not None
        )

    def _same_zone(self, a: str, b: str) -> bool:
        if self.vocab is not None and self.vocab.canonical("zone", a) == self.vocab.canonical("zone", b):
            return True
        return close_spellings(fold(a), fold(b))

    # -- Dummy Fix ------------------------------------------------------------------------
    @staticmethod
    def _amount_keys(ev: Event) -> list[tuple[str, ...]]:
        group = "heal" if ev.kind == "heal" else "damage"
        actor = (ev.actor or "").casefold()
        skill = (ev.skill or "").casefold()
        return [(group, actor, skill), (group, actor), (group, "*", skill), (group,)]

    def _zone_scope(self) -> float:
        return self.zone_changes[-1][0] if self.zone_changes else 0.0

    def _learn_amount(self, ev: Event) -> None:
        zone = self._zone_scope()
        for key in self._amount_keys(ev):
            for scope in (zone, "*"):
                acc = self._amounts.setdefault((scope, *key), [0.0, 0.0])
                acc[0] += float(ev.amount or 0)
                acc[1] += 1.0

    def estimate_amount(self, ev: Event) -> bool:
        """Dummy Fix: give ``ev`` (a hit or heal whose number was unreadable) an amount.

        The average of the same attacker's same attack in the current zone, else of that
        attacker, else of that attack by anyone, else of every hit (or heal) in the zone;
        the whole session's average when the zone has none yet.  Returns ``False`` when
        nothing comparable has been read yet (the line then stays unknown).
        """
        zone = self._zone_scope()
        for scope in (zone, "*"):
            for key in self._amount_keys(ev):
                acc = self._amounts.get((scope, *key))
                if acc and acc[1]:
                    ev.amount = max(1, int(round(acc[0] / acc[1])))
                    ev.estimated = True
                    return True
        return False

    def zone_name(self, raw: str) -> str:
        """``raw`` with OCR variants mapped to the learned spelling."""
        return (self.vocab.canonical("zone", raw) if self.vocab is not None else clean(raw)) or raw

    def zone_visits(self) -> list[tuple[float, str]]:
        """``(entered, zone)`` per visit: consecutive changes into the same zone merged."""
        visits: list[tuple[float, str]] = []
        for ts, raw in self.zone_changes:
            zone = self.zone_name(raw)
            if not visits or visits[-1][1] != zone:
                visits.append((ts, zone))
        return visits

    def zone_visit_at(self, ts: float) -> tuple[str, float]:
        """``(zone, entered)`` of the visit in effect at ``ts``; ``("", 0.0)`` before any."""
        current = ("", 0.0)
        for entered, zone in self.zone_visits():
            if entered <= ts:
                current = (zone, entered)
            else:
                break
        return current

    def zone_at(self, ts: float) -> str:
        """The zone in effect at ``ts`` (``""`` before the first zone line was seen)."""
        return self.zone_visit_at(ts)[0]

    def _match_target(self, enc: Encounter, name: str) -> str | None:
        """The fight target ``name`` refers to (exact, or an OCR variant of it)."""
        if name in enc.target_names:
            return name
        for target in enc.target_names:
            if name_similar(name, target, vocab=self.vocab) or close_spellings(fold(name), fold(target)):
                return target
        return None

    def expire(self, now: float) -> Encounter | None:
        """Close the open encounter if it has timed out by ``now``.

        Returns the encounter that was closed, or ``None``.  The CLI can
        call this between events so a fight that simply ended is reported.
        """
        enc = self._open
        if enc is not None and now - enc.last_activity > self.encounter_timeout_s:
            kept = self._close(enc, enc.last_activity, reason="timeout")
            return enc if kept else None
        return None

    def current(self) -> Encounter | None:
        """The open encounter, or ``None`` when no fight is in progress."""
        return self._open

    def _close(self, enc: Encounter, end: float, *, reason: str) -> bool:
        """Close ``enc``; returns ``False`` when it was dropped for having no damage."""
        enc.end = max(enc.start, end)
        enc.closed = True
        if self._open is enc:
            self._open = None
        if not enc.has_damage:
            # Only misses / fully absorbed swings: not a fight worth listing.
            self.discarded += 1
            log.debug("encounter without damage dropped (%s, %d events)", reason, len(enc.events))
            return False
        self.history.append(enc)
        log.info(
            "encounter vs %s closed (%s) after %s, %d events",
            enc.label(), reason, _mmss(enc.duration), len(enc.events),
        )
        allies = self._allies(enc)
        if allies:
            self.roster.note_fight(allies, enc.end)
        return True

    def _is_viewer(self, name: str | None, raw: str | None = None) -> bool:
        """True when ``name`` (``raw``: the token as written) is the viewer."""
        if is_you(raw) or is_you(name):
            return True
        return bool(self.player_name and name) and name.casefold() == self.player_name.casefold()

    def _npc_vs_npc(self, ev: Event) -> bool:
        """A hit or swing between two NPCs (never the viewer's pet, which is flagged)."""
        return (
            not ev.is_pet and bool(ev.actor) and bool(ev.target)
            and ev.actor not in self.roster.pet_owners() and ev.target not in self.roster.pet_owners()
            and is_npc_name(ev.actor) and is_npc_name(ev.target)
        )

    def _allies(self, enc: Encounter) -> set[str]:
        """The players (not the viewer, not the viewer's pet) who fought on the viewer's side in
        ``enc``: they damaged an NPC the viewer hit or was hit by, or they healed the viewer."""
        canon = self.canonical_map(enc)
        foes: set[str] = set()
        for ev in enc.events:
            if ev.kind not in ACTIVITY_KINDS or not ev.actor or not ev.target or ev.is_pet:
                continue
            if self._is_viewer(ev.actor, ev.raw_actor) and is_npc_name(ev.target):
                foes.add(canon.get(ev.target, ev.target))
            elif self._is_viewer(ev.target) and is_npc_name(ev.actor):
                foes.add(canon.get(ev.actor, ev.actor))
        allies: set[str] = set()
        for ev in enc.events:
            actor = ev.actor
            if not actor or not ev.target or ev.is_pet or actor in self.roster.pet_owners() or is_npc_name(actor) or self._is_viewer(actor, ev.raw_actor):
                continue
            if ev.kind in DAMAGE_KINDS and is_npc_name(ev.target) and canon.get(ev.target, ev.target) in foes:
                allies.add(canon.get(actor, actor))
            elif ev.kind == "heal" and self._is_viewer(ev.target):
                allies.add(canon.get(actor, actor))
        return allies

    def _note_targets(self, enc: Encounter, ev: Event) -> None:
        """Record who the fight is against.

        NPC names on either side of a damage/swing event count as targets; if neither side
        is an NPC (a duel) the attacked player counts, unless that is the viewer or a party
        member (then the attacker is the enemy, and the snapshot names it).  A line whose
        attacker and victim are the same name (an OCR misread) names nobody.
        """
        if ev.actor and ev.target and ev.actor.casefold() == ev.target.casefold():
            return
        npcs = [n for n in (ev.actor, ev.target) if n and is_npc_name(n)]
        if npcs:
            enc.target_names.update(npcs)
        elif ev.target and not self._is_viewer(ev.target) and ev.target not in self.roster.members():
            enc.target_names.add(ev.target)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def actor_table(self, enc: Encounter) -> list[dict[str, Any]]:
        """Per-actor numbers for ``enc``, sorted by damage (desc) then name.

        Keys: ``actor``, ``damage`` (melee + ability), ``dps``
        (damage / max(1, duration)), ``swings`` (melee hits + misses),
        ``hits``, ``misses``, ``hit_pct``, ``max_hit``, ``avg_hit``
        (per damaging hit, melee or ability) and ``heals`` (healing done).
        """
        canon = self.canonical_map(enc)
        rows: dict[str, dict[str, Any]] = {}
        for ev in enc.events:
            if not ev.actor:
                continue
            actor = canon.get(ev.actor, ev.actor)
            if ev.kind in DAMAGE_KINDS:
                row = rows.setdefault(actor, _new_row(actor))
                amount = ev.amount or 0
                row["damage"] += amount
                row["_damaging_hits"] += 1
                row["max_hit"] = max(row["max_hit"], amount)
                if ev.kind == "melee_hit":
                    row["hits"] += 1
            elif ev.kind == "melee_miss":
                row = rows.setdefault(actor, _new_row(actor))
                row["misses"] += 1
            elif ev.kind == "heal":
                row = rows.setdefault(actor, _new_row(actor))
                row["heals"] += ev.amount or 0

        duration = max(1.0, enc.end - enc.start)
        table: list[dict[str, Any]] = []
        for row in rows.values():
            swings = row["hits"] + row["misses"]
            row["swings"] = swings
            row["hit_pct"] = round(100.0 * row["hits"] / swings, 2) if swings else 0.0
            row["dps"] = round(row["damage"] / duration, 2)
            damaging = row.pop("_damaging_hits")
            row["avg_hit"] = round(row["damage"] / damaging, 2) if damaging else 0.0
            table.append(row)
        table.sort(key=lambda r: (-r["damage"], r["actor"]))
        return table

    def canonical_map(self, enc: Encounter) -> dict[str, str]:
        """OCR-noise name merge for ``enc``: every actor/target name -> canonical name.

        Variants are merged within the encounter first; with a vocabulary, the result is
        mapped to the spelling learned over the whole session ("a skeletal fizhter" read
        once in a short fight still shows as "a skeletal fighter").
        """
        counts: Counter[str] = Counter()
        for ev in enc.events:
            for name in (ev.actor, ev.target):
                if name:
                    counts[name] += 1
        canon = canonical_names(counts, vocab=self.vocab)
        vocab = self.vocab
        if vocab is not None:
            for name, target in canon.items():
                if is_you(target):
                    continue
                canon[name] = vocab.canonical("npc" if is_npc_name(target) else "player", target) or target
        return canon

    def label(self, enc: Encounter) -> str:
        """Target list for headers with OCR-noise variants merged."""
        canon = self.canonical_map(enc)
        names = sorted({canon.get(n, n) for n in enc.target_names})
        return ", ".join(names) if names else _NO_TARGET_LABEL

    def render(self, enc: Encounter) -> str:
        """Fixed-width console table for ``enc`` (SPEC section 8)."""
        rows = self.actor_table(enc)
        header = f"Encounter vs {self.label(enc)} - {_mmss(enc.duration)} - {len(enc.events)} events"
        name_w = max([10, len("Actor")] + [len(r["actor"]) for r in rows])
        columns = (
            f"{'Actor':<{name_w}}  {'Damage':>6}  {'DPS':>6}  {'Swings':>6}  "
            f"{'Hits':>4}  {'Miss':>4}  {'Hit%':>6}  {'Max':>4}  {'Avg':>5}  {'Heals':>5}"
        )
        lines = [header, columns]
        if not rows:
            lines.append("(no damage or healing events yet)")
        for r in rows:
            lines.append(
                f"{r['actor']:<{name_w}}  {r['damage']:>6d}  {r['dps']:>6.1f}  {r['swings']:>6d}  "
                f"{r['hits']:>4d}  {r['misses']:>4d}  {r['hit_pct']:>5.1f}%  {r['max_hit']:>4d}  "
                f"{r['avg_hit']:>5.1f}  {r['heals']:>5d}"
            )
        return "\n".join(lines)

    def to_json(self, enc: Encounter) -> dict[str, Any]:
        """JSON-serialisable summary of ``enc`` (header fields + actor table)."""
        return {
            "start": enc.start,
            "end": enc.end,
            "duration_s": round(enc.duration, 3),
            "targets": sorted(enc.target_names),
            "killed": sorted(enc.killed_names),
            "closed": enc.closed,
            "event_count": len(enc.events),
            "actors": self.actor_table(enc),
        }
