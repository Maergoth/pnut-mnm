"""Session-wide bookkeeping: loot, coin, crafting, harvests, kills, deaths, crowd control.

Most counters use *shared* information - lines the whole group produces in the Combat
window. Lines that only the viewer gets about themselves (skill-ups, faction standing, XP
ticks, PvP opt-in, eating) are classified by the parser as ``personal``/``experience`` and
are ignored unless ``include_personal`` is set.

Own XP-bar measurements are separate from these chat counts and remain available
regardless of personal-line filtering. Health readings are alert signals only.
Successful harvests are viewer-only counters, independent of personal-line filtering.

:class:`SessionStats` consumes every :class:`~mnmparse.grammar.Event` of a run (combat and
non-combat alike) and :meth:`SessionStats.snapshot` returns a plain :class:`SessionSnapshot`
for the Session tab.  Pure Python, no Qt.
"""

from __future__ import annotations

import dataclasses
import math
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from .currency import format_coin
from .grammar import Event, is_npc_name, is_you
from .interrupts import CC_CATEGORIES
from .party import PartyRoster
from .parser import normalize_ocr, parse_line
from .player_telemetry import XP_RATE_WINDOW
from .session_rates import ACTIVITY_RATE_WINDOW, activity_rate, build_activity_rates, project_activity_rates
from .stats import canonical_names
from .vocab import Vocabulary

__all__ = [
    "LOOTED_BY_YOU", "LootEntry", "SessionEntry", "SessionSnapshot", "SessionStats", "filter_group_session", "filter_session", "format_coin",
]

RECENT_LIMIT = 200

PLAYER_STATUS_FIELDS = {
    "xp_percent", "xp_gain_points", "xp_observed_seconds", "xp_points_per_hour",
    "xp_level_ups",
}


def _copy_player_status(summary: Any) -> dict[str, Any]:
    """Copy own net-XP measurements; HP alerts have no session metrics."""
    if not isinstance(summary, dict):
        return {}

    def valid_number(value: Any) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    copied = {}
    for key in PLAYER_STATUS_FIELDS:
        if key not in summary:
            continue
        value = summary[key]
        if value is None:
            copied[key] = None
        elif valid_number(value) and (key in {"xp_gain_points", "xp_points_per_hour"} or value >= 0):
            if key != "xp_percent" or value <= 100:
                copied[key] = value
    if not copied:
        return copied
    if summary.get("xp_rate_kind") != "rolling":
        # Older archives measured the entire observed session. Preserve that
        # historical rate, but never label it as a current rolling estimate.
        copied["xp_rate_kind"] = "session"
        return copied
    copied["xp_rate_kind"] = "rolling"
    copied["xp_rate_window_seconds"] = XP_RATE_WINDOW
    copied["xp_rate_gain_points"] = 0.0
    copied["xp_rate_observed_seconds"] = 0.0
    copied["xp_points_per_hour"] = None
    window = summary.get("xp_rate_window_seconds")
    gain = summary.get("xp_rate_gain_points")
    seconds = summary.get("xp_rate_observed_seconds")
    if (valid_number(window) and window == XP_RATE_WINDOW and valid_number(gain)
            and valid_number(seconds) and 0 <= seconds <= window and (seconds > 0 or gain == 0)):
        rate = gain * 3600.0 / seconds if seconds else None
        if rate is None or math.isfinite(rate):
            copied["xp_rate_gain_points"] = gain
            copied["xp_rate_observed_seconds"] = seconds
            copied["xp_points_per_hour"] = round(rate, 6) if rate is not None else None
    return copied
#: A wrapped "N copper coins from X's corpse as your split." line belongs to a coin loot at most
#: this many seconds before it (the two halves were always read in the same second).
COIN_SPLIT_JOIN_S = 5.0


@dataclass
class LootEntry:
    """One item leaving a corpse."""

    ts: float
    looter: str
    item: str
    source: str | None  #: the corpse it came from


@dataclass
class SessionEntry:
    """One line of the Session feed (loot, coin, craft, harvest, kill, death, cc)."""

    ts: float
    kind: str
    actor: str | None
    text: str  #: short human text, e.g. "Abepulifif looted Bone Chips (a skeletal marksman)"


@dataclass
class _Coin:
    """One coin loot as announced, with the viewer's split once it is known."""

    ts: float
    looter: str
    copper: int
    split: int | None  #: the viewer's share; None when no split was read (yet)
    entry: SessionEntry  #: its line in the feed (completed when a wrapped split line arrives)


@dataclass
class SessionSnapshot:
    """Frozen view of the session for display."""

    started: float
    elapsed: float
    encounters: int
    combat_seconds: float
    items: int  #: items looted (all looters)
    items_by_name: list[tuple[str, int]]  #: (item, count) desc
    items_by_looter: list[tuple[str, int]]  #: (looter, count) desc
    item_looters: dict[str, list[tuple[str, int]]]  #: item -> [(looter, count)] desc
    zones: list[str]  #: zones entered, in order, without repeats
    kill_entries: list[tuple[float, str | None, str]]  #: (ts, killer, victim) for NPC kills
    craft_entries: list[tuple[float, str, str, int]]  #: (ts, crafter, item, quantity), the group's crafts
    loot: list[LootEntry]  #: every item, oldest first
    coin_total: int  #: copper looted by everyone (as announced)
    coin_by_looter: list[tuple[str, int]]  #: (looter, copper) desc
    coin_split: int  #: copper the viewer received as splits
    crafts: int
    crafts_by_crafter: list[tuple[str, int]]
    crafts_by_item: list[tuple[str, int]]
    kills: int
    kills_by_killer: list[tuple[str, int]]
    kills_by_target: list[tuple[str, int]]
    deaths: int  #: deaths of the viewer and their party members
    deaths_by_player: list[tuple[str, int]]
    cc_total: int  #: crowd-control effects landed on anyone
    cc_by_type: list[tuple[str, int]]
    cc_on_npcs: int
    cc_on_players: int
    personal_included: bool
    skill_ups: list[tuple[str, int]]  #: (skill, latest value) when personal lines are included
    faction: list[tuple[str, int]]  #: (faction, net change) when personal lines are included
    xp_ticks: int
    recent: list[SessionEntry]  #: newest last
    #: players outside the party who died nearby (the Combat chat shows other groups too);
    #: not counted in ``deaths``
    outsider_deaths: list[tuple[str, int]] = field(default_factory=list)
    outsider_kills: int = 0  #: kills by players outside the party (not counted in ``kills``)
    party: list[str] = field(default_factory=list)  #: party members recognised so far
    #: copper the viewer actually got: every split, plus their own loots that had none (solo)
    coin_received: int = 0
    #: (crafter, quantity) for players outside the party crafting nearby; not counted in ``crafts``
    outsider_crafts: list[tuple[str, int]] = field(default_factory=list)
    #: quest hand-in rewards ("You receive X from <NPC>"): not corpse loot, not in ``items``
    rewards: list[LootEntry] = field(default_factory=list)
    revision: int = 0
    capture_quality: dict[str, Any] = field(default_factory=dict)
    privacy_mode: str = ""
    group_averages: dict[str, float] = field(default_factory=dict)
    group_average_count: int = 0
    emitted_at: float = 0.0
    player_status: dict[str, Any] = field(default_factory=dict)
    activity_rates: dict[str, Any] = field(default_factory=dict)
    harvests: int = 0  #: successful harvest lines from the viewer, not corpse loot
    harvests_by_item: list[tuple[str, int]] = field(default_factory=list)

    @property
    def kills_per_hour(self) -> float:
        return activity_rate(self.activity_rates, "kills", fallback_count=self.kills, elapsed=self.elapsed)

    @property
    def items_per_hour(self) -> float:
        return activity_rate(self.activity_rates, "items", fallback_count=self.items, elapsed=self.elapsed)

    @property
    def coin_per_hour(self) -> float:
        return activity_rate(self.activity_rates, "coin", fallback_count=self.coin_total, elapsed=self.elapsed)


#: The Me view's detail row under the coin it received (``coin_by_looter`` there holds this one row).
LOOTED_BY_YOU = "Looted by you (gross)"


def _harvest_projection(snap: SessionSnapshot, included: Any) -> tuple[int, list[tuple[str, int]], dict[str, Counter[str]]]:
    """Keep complete item/actor allocations, including earlier viewer identities."""
    allocations = getattr(snap, "_harvest_item_actors", None)
    if allocations is None:
        personal = getattr(snap, "_personal_totals", {})
        owner = personal.get("player", "")
        allocations = {item: {owner: count} for item, count in personal.get("harvests_by_item", [])}
    filtered = {item: Counter({actor: count for actor, count in actors.items() if included(actor)})
                for item, actors in allocations.items()}
    filtered = {item: actors for item, actors in filtered.items() if actors}
    counts = sorted(((item, sum(actors.values())) for item, actors in filtered.items()),
                    key=lambda pair: (-pair[1], pair[0]))
    return sum(count for _item, count in counts), counts, filtered


def filter_group_session(snap: SessionSnapshot, player: str) -> SessionSnapshot:
    """Show current-group totals without changing the collected session.

    Item counters are complete even when only recent loot details were loaded.
    Historical party kills can name members who have since left. Complete
    participant counters keep current-group totals accurate with bounded details.
    """
    members = {name.casefold() for name in snap.party if name}
    members.add((player or "You").casefold())
    included = lambda name: bool(name) and name.casefold() in members
    harvest_total, harvest_items, harvest_actors = _harvest_projection(snap, included)
    item_looters = {item: [(name, count) for name, count in looters if included(name)]
                    for item, looters in snap.item_looters.items()}
    item_looters = {item: looters for item, looters in item_looters.items() if looters}
    item_counts = [(item, sum(count for _name, count in looters))
                   for item, looters in item_looters.items()]
    looter_counts = [(name, count) for name, count in snap.items_by_looter if included(name)]
    if not snap.items_by_looter:
        by_looter: Counter[str] = Counter()
        for looters in item_looters.values():
            for name, count in looters:
                by_looter[name] += count
        looter_counts = sorted(by_looter.items(), key=lambda pair: (-pair[1], pair[0]))
    coin = [(name, count) for name, count in snap.coin_by_looter if included(name)]
    kills = [entry for entry in snap.kill_entries if included(entry[1])]
    killers = [(name, count) for name, count in snap.kills_by_killer if included(name)]
    if not snap.kills_by_killer and len(snap.kill_entries) == snap.kills:
        killers = sorted(Counter(name for _ts, name, _target in kills).items(),
                         key=lambda pair: (-pair[1], pair[0]))
    kill_total = sum(count for _name, count in killers)
    crafts = [entry for entry in snap.craft_entries if included(entry[1])]
    crafters = [(name, count) for name, count in snap.crafts_by_crafter if included(name)]
    if not snap.crafts_by_crafter and sum(entry[3] for entry in snap.craft_entries) == snap.crafts:
        by_crafter: Counter[str] = Counter()
        for _ts, name, _item, count in crafts:
            by_crafter[name] += count
        crafters = sorted(by_crafter.items(), key=lambda pair: (-pair[1], pair[0]))
    craft_total = sum(count for _name, count in crafters)
    deaths = [(name, count) for name, count in snap.deaths_by_player if included(name)]
    allocations = getattr(snap, "_participant_totals", {})
    filtered_allocations = {
        kind: {key: count for key, count in counts.items() if included(key[0])}
        for kind, counts in allocations.items()
    }

    def detail_counts(kind: str, total: int, aggregate: list[tuple[str, int]],
                      original_total: int, details: Counter[str], complete: bool) -> list[tuple[str, int]]:
        if kind in filtered_allocations:
            counts: Counter[str] = Counter()
            for (_actor, target), count in filtered_allocations[kind].items():
                counts[target] += count
            return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])) if sum(counts.values()) == total else []
        if total == original_total:
            return aggregate
        # Older snapshots may only carry recent rows. Keep their complete
        # participant totals without presenting partial details as full totals.
        return sorted(details.items(), key=lambda pair: (-pair[1], pair[0])) if complete else []

    kill_targets = detail_counts(
        "kill_targets", kill_total, snap.kills_by_target, snap.kills,
        Counter(target for _ts, _actor, target in kills), len(snap.kill_entries) == snap.kills,
    )
    craft_items: Counter[str] = Counter()
    for _ts, _actor, item, count in crafts:
        craft_items[item] += count
    craft_targets = detail_counts(
        "craft_items", craft_total, snap.crafts_by_item, snap.crafts, craft_items,
        sum(entry[3] for entry in snap.craft_entries) == snap.crafts,
    )

    def recent_included(entry: SessionEntry) -> bool:
        if entry.kind == "kill":
            # SessionStats formats death rows with the victim first; the actor
            # is the killer, which can be an NPC killing an unrelated player.
            victim = entry.text.partition(" slain")[0]
            return included(entry.actor) or included(victim)
        return not entry.actor or is_npc_name(entry.actor) or included(entry.actor)

    projected = dataclasses.replace(
        snap,
        items=sum(count for _name, count in looter_counts),
        items_by_name=sorted(item_counts, key=lambda pair: (-pair[1], pair[0])),
        items_by_looter=looter_counts,
        item_looters=item_looters,
        loot=[entry for entry in snap.loot if included(entry.looter)],
        coin_total=sum(count for _name, count in coin),
        coin_by_looter=coin,
        rewards=[entry for entry in snap.rewards if included(entry.looter)],
        kills=kill_total, kills_by_killer=killers, kills_by_target=kill_targets, kill_entries=kills,
        crafts=craft_total, crafts_by_crafter=crafters, crafts_by_item=craft_targets, craft_entries=crafts,
        harvests=harvest_total, harvests_by_item=harvest_items,
        deaths=sum(count for _name, count in deaths), deaths_by_player=deaths,
        recent=[entry for entry in snap.recent if recent_included(entry)],
        outsider_deaths=[], outsider_kills=0, outsider_crafts=[],
        activity_rates=project_activity_rates(snap.activity_rates, included),
    )
    if hasattr(snap, "_personal_totals"):
        projected._personal_totals = snap._personal_totals
    projected._harvest_item_actors = harvest_actors
    if allocations:
        projected._participant_totals = filtered_allocations
    return projected


def filter_session(snap: SessionSnapshot, player: str) -> SessionSnapshot:
    """The viewer's own share of ``snap``: loot, coin, kills, deaths and crafts by ``player``.

    The coin is what the viewer received (``coin_received``: their splits, plus their own
    loots without a split), which is also what coin per hour is worked out from; the gross
    coin they looted themselves is one detail row, ``(LOOTED_BY_YOU, copper)``, in
    ``coin_by_looter``. Zones stay shared.
    """
    player = player or "You"
    own = lambda name: bool(name) and name.casefold() == player.casefold()
    harvest_total, harvest_items, harvest_actors = _harvest_projection(snap, own)
    loot = [e for e in snap.loot if own(e.looter)]
    items: Counter[str] = (Counter({item: sum(count for name, count in looters if own(name))
                                   for item, looters in snap.item_looters.items()})
                           if snap.item_looters else Counter(e.item for e in loot))
    items = Counter({item: count for item, count in items.items() if count})
    kills = [k for k in snap.kill_entries if own(k[1])]
    by_target: Counter[str] = Counter(v for _ts, _k, v in kills)
    crafts = [c for c in snap.craft_entries if own(c[1])]
    by_item: Counter[str] = Counter()
    for _ts, _c, item, n in crafts:
        by_item[item] += n
    gross = sum(c for name, c in snap.coin_by_looter if own(name))
    deaths = [(name, n) for name, n in snap.deaths_by_player if own(name)]
    personal = getattr(snap, "_personal_totals", {})
    complete = personal if own(personal.get("player")) else {}
    item_counts = complete.get("items_by_name", sorted(items.items(), key=lambda kv: (-kv[1], kv[0])))
    item_total = complete.get("items", sum(count for name, count in snap.items_by_looter if own(name))
                              if snap.items_by_looter else sum(items.values()))
    craft_counts = complete.get("crafts_by_item", sorted(by_item.items(), key=lambda kv: (-kv[1], kv[0])))
    craft_total = complete.get("crafts", sum(by_item.values()))
    kill_total = complete.get("kills", len(kills))
    projected = dataclasses.replace(
        snap,
        items=item_total,
        items_by_name=item_counts,
        items_by_looter=[(player, item_total)] if item_total else [],
        item_looters={item: [(player, n)] for item, n in item_counts},
        loot=loot,
        coin_total=snap.coin_received,
        coin_by_looter=[(LOOTED_BY_YOU, gross)] if gross else [],
        rewards=[e for e in snap.rewards if own(e.looter)],
        crafts=craft_total,
        crafts_by_crafter=[(player, craft_total)] if craft_total else [],
        crafts_by_item=craft_counts,
        harvests=harvest_total,
        harvests_by_item=harvest_items,
        kills=kill_total,
        kills_by_killer=[(player, kill_total)] if kill_total else [],
        kills_by_target=complete.get("kills_by_target", sorted(by_target.items(), key=lambda kv: (-kv[1], kv[0]))),
        deaths=sum(n for _n, n in deaths),
        deaths_by_player=deaths,
        kill_entries=kills,
        craft_entries=crafts,
        activity_rates=project_activity_rates(snap.activity_rates, own, personal_coin=True),
    )
    projected._personal_totals = complete
    projected._harvest_item_actors = harvest_actors
    return projected


#: "Your party member X has slain Y!" (also with a dropped first letter).
_PARTY_LINE_RX = re.compile(r"^\W*[YV]?our\s+party\s+member\s", re.IGNORECASE)
_OWN_PET_SUBJECT_RX = re.compile(r"^Your\s+pet\s+([A-Z][A-Za-z]*)\b")
#: The same player cannot die twice this quickly: a second report is a re-read of the first.
DEATH_DEDUP_S = 60.0


class SessionStats:
    """Aggregate non-combat (and some combat) events over a whole run.

    Kills, deaths and crafts count the viewer's group only.  The Combat chat also shows
    other groups nearby, so those lines are attributed when the snapshot is built, with the
    party known by then: the viewer plus the members of ``roster`` (the meter's
    :class:`~mnmparse.party.PartyRoster`, with its join lines and the user's own choices)
    or a local roster using the same explicit evidence. An empty roster is solo.
    "X has died." is not a
    death: it is what a Feign Death prints.
    """

    def __init__(
        self,
        player_name: str = "",
        *,
        include_personal: bool = False,
        started: float | None = None,
        vocab: Vocabulary | None = None,
        roster: Any = None,
    ) -> None:
        self.player_name = player_name
        self.include_personal = include_personal
        self.vocab = vocab
        #: The meter's roster, or a local roster for standalone session imports.
        self.roster = roster
        self._local_roster = PartyRoster(player_name)
        self.started = started if started is not None else time.time()
        self.last_ts = self.started
        self.encounters = 0
        self.combat_seconds = 0.0
        self._loot: list[LootEntry] = []
        self._items_by_name: Counter[str] = Counter()
        self._items_by_looter: Counter[str] = Counter()
        self._item_looters: dict[str, Counter[str]] = {}
        self._zones: list[str] = []
        self._kill_entries: list[tuple[float, str | None, str]] = []
        #: every craft line, the party's and other players' (told apart in the snapshot)
        self._craft_entries: list[tuple[float, str, str, int]] = []
        self._harvests_by_item: dict[str, Counter[str]] = {}
        self._coin_by_looter: Counter[str] = Counter()
        self._coins: list[_Coin] = []
        self._rewards: list[LootEntry] = []
        #: every slain line: (ts, killer, victim, said "Your party member" / "You have slain")
        self._slain: list[tuple[float, str | None, str, bool]] = []
        #: Explicit "Your pet" evidence, also learned from hits before a generic slain line.
        #: A dictionary survives archive recovery without changing legacy slain rows.
        self._observed_pet_owners: dict[str, str] = {}
        self._cc_by_type: Counter[str] = Counter()
        self._cc_on_npcs = 0
        self._cc_on_players = 0
        self._skill_ups: dict[str, int] = {}
        self._faction: Counter[str] = Counter()
        self._xp_ticks = 0
        self._names: Counter[str] = Counter()
        self._recent: deque[SessionEntry] = deque(maxlen=RECENT_LIMIT)
        self._version = 0
        self.capture_quality: dict[str, Any] = {}
        self.player_status: dict[str, Any] = {}
        self._snapshot_cache: dict[Any, SessionSnapshot] = {}
        self._snapshot_revision = 0

    # ------------------------------------------------------------------
    @property
    def version(self) -> int:
        """Increments whenever a snapshot would change (cheap change detection)."""
        return self._version

    def note_encounter(self, duration_s: float) -> None:
        """Record a closed encounter (for time-in-combat and rates)."""
        self.encounters += 1
        self.combat_seconds += max(0.0, float(duration_s))
        self._version += 1

    def note_player_status(self, summary: dict[str, Any]) -> bool:
        """Update own XP measurements independently of personal chat filtering."""
        copied = _copy_player_status(summary)
        if copied == self.player_status:
            return False
        self.player_status = copied
        self._version += 1
        return True

    def note_zone(self, zone: str) -> bool:
        """Update location metadata without adding a replayed line to the feed."""
        if not zone or (self._zones and self._zones[-1] == zone):
            return False
        self._zones.append(zone)
        self._version += 1
        return True

    def set_roster(self, roster: Any) -> None:
        """Take the party from ``roster`` (the meter's PartyRoster) from now on."""
        self.roster = roster
        self._version += 1

    def revise_encounter(self, previous_s: float | None, replacement_s: float | None) -> None:
        """Reconcile an already closed fight after a group or pet correction.

        ``None`` means that version of the fight does not count for this session.
        """
        self.encounters = max(0, self.encounters + int(replacement_s is not None) - int(previous_s is not None))
        self.combat_seconds = max(0.0, self.combat_seconds
                                  + max(0.0, replacement_s or 0.0) - max(0.0, previous_s or 0.0))
        self._version += 1

    def add(self, ev: Event) -> None:
        """Feed one parsed event; most kinds are ignored here."""
        self.last_ts = max(self.last_ts, float(ev.ts))
        for name, observed in (("estimated_amounts", ev.estimated), ("delayed_timestamps", ev.estimated_ts),
                               ("unrecognized_messages", ev.kind in {"unknown", "ability_partial"})):
            if observed:
                self.capture_quality[name] = self.capture_quality.get(name, 0) + 1
                self._version += 1
        if self.roster is None:
            self._local_roster.observe(ev)
        if ev.is_pet:
            # Passive slain messages swap actor/target: "Your pet Sprout has
            # been slain by a wolf" identifies Sprout, not the killing wolf.
            subject = _OWN_PET_SUBJECT_RX.match(normalize_ocr(ev.text or "")) if ev.kind == "kill" else None
            pet = subject.group(1) if subject is not None else ev.actor
            if pet and self._observed_pet_owners.get(pet) != self._you():
                self._observed_pet_owners[pet] = self._you()
                self._version += 1
        kind = ev.kind
        # Older imported events classified successful harvests as personal.
        # Reparse the complete line; personal items also include eating and
        # retrieving one's corpse, which must never count as gathering.
        if kind == "personal" and ev.item:
            reparsed = parse_line(ev.text, ev.ts, self.player_name)
            if reparsed.kind == "harvest":
                ev, kind = reparsed, reparsed.kind
        if kind == "harvest" and ev.item:
            own_actor = is_you(ev.raw_actor or ev.actor or "") or (
                bool(ev.actor) and ev.actor.casefold() == self._you().casefold())
            if not own_actor:
                return
            # The configured viewer can change during a session. Keep the
            # event's original mapped identity rather than assigning history
            # to whichever player name is configured when snapshotting later.
            actor = ev.actor if ev.actor and not is_you(ev.actor) else self._you()
            self._harvests_by_item.setdefault(ev.item, Counter())[actor] += 1
            self._push(ev.ts, "harvest", actor, f"{actor} harvested {ev.item}")
        elif kind == "loot" and ev.item:
            looter = self._name(ev.actor) or "?"
            self._loot.append(LootEntry(ev.ts, looter, ev.item, ev.target))
            self._items_by_name[ev.item] += 1
            self._items_by_looter[looter] += 1
            self._item_looters.setdefault(ev.item, Counter())[looter] += 1
            where = f" ({ev.target})" if ev.target else ""
            self._push(ev.ts, "loot", looter, f"{looter} looted {ev.item}{where}")
        elif kind == "coin":
            looter = self._name(ev.actor) or "?"
            amount = int(ev.copper or 0)
            self._coin_by_looter[looter] += amount
            where = f" ({ev.target})" if ev.target else ""
            entry = self._push(ev.ts, "coin", looter, f"{looter} looted {format_coin(amount)}{where}")
            coin = _Coin(float(ev.ts), looter, amount, None, entry)
            self._coins.append(coin)
            if ev.split_copper is not None:
                self._set_split(coin, int(ev.split_copper))
                self._coins[-1] = coin
        elif kind == "coin_split":
            # "22 copper coins from X's corpse as your split.": the second row of a wrapped coin line
            if not self._join_split(ev):
                return
        elif kind == "reward" and ev.item:
            # "You receive X from <NPC>": a quest hand-in, not corpse loot
            receiver = self._name(ev.actor) or self._you()
            self._rewards.append(LootEntry(ev.ts, receiver, ev.item, ev.target))
            where = f" from {ev.target}" if ev.target else ""
            self._push(ev.ts, "reward", receiver, f"{receiver} received {ev.item}{where}")
        elif kind == "craft" and ev.item:
            crafter = self._name(ev.actor) or "?"
            n = int(ev.amount or 1)
            self._craft_entries.append((float(ev.ts), crafter, ev.item, n))
            qty = f" x{n}" if n > 1 else ""
            self._push(ev.ts, "craft", crafter, f"{crafter} crafted {ev.item}{qty}")
        elif kind == "kill":
            killer = self._name(ev.actor)
            victim = self._name(ev.target)
            if not victim:
                return
            ours = bool(_PARTY_LINE_RX.match(ev.text or "")) or (killer is not None and killer == self._you())
            self._slain.append((float(ev.ts), killer, victim, ours))
            by = f" by {killer}" if killer else ""
            self._push(ev.ts, "kill", killer, f"{victim} slain{by}")
        elif kind in ("cc", "interrupt"):
            category = "interrupt" if kind == "interrupt" else (ev.outcome or "cc")
            victim = self._name(ev.actor if kind == "interrupt" else ev.target)
            self._cc_by_type[category] += 1
            if victim and is_npc_name(victim):
                self._cc_on_npcs += 1
            elif victim:
                self._cc_on_players += 1
            label = {"interrupt": "interrupted", "stun": "stunned", "mez": "mesmerized", "root": "rooted",
                     "silence": "silenced", "snare": "snared", "fear": "feared", "charm": "charmed",
                     "blind": "blinded"}.get(category, category)
            self._push(ev.ts, "cc", victim, f"{victim or '?'} {label}")
        elif kind == "zone" and ev.target:
            if not self._zones or self._zones[-1] != ev.target:
                self._zones.append(ev.target)
            self._push(ev.ts, "zone", None, f"Entered {ev.target}")
        elif kind == "experience":
            self._xp_ticks += 1
            if self.include_personal:
                self._version += 1
        elif kind == "personal" and self.include_personal:
            if ev.skill and ev.amount is not None:
                self._skill_ups[ev.skill] = max(self._skill_ups.get(ev.skill, 0), int(ev.amount))
                self._push(ev.ts, "personal", None, f"{ev.skill} skill up ({ev.amount})")
            elif ev.target and ev.outcome in ("better", "worse"):
                self._faction[ev.target] += 1 if ev.outcome == "better" else -1
                self._push(ev.ts, "personal", None, f"Faction {ev.target}: {ev.outcome}")
        else:
            return
        self._version += 1

    # ------------------------------------------------------------------
    def _you(self) -> str:
        return self.player_name or "You"

    @staticmethod
    def _set_split(coin: _Coin, split: int) -> None:
        coin.split = split
        if split:
            coin.entry.text += f", your split {format_coin(split)}"

    def _join_split(self, ev: Event) -> bool:
        """Give a wrapped split line's copper to its coin loot: the latest one without a split
        within ``COIN_SPLIT_JOIN_S``, when the split is no larger than that loot."""
        if ev.split_copper is None:
            return False
        split = int(ev.split_copper)
        for index in range(len(self._coins) - 1, -1, -1):
            coin = self._coins[index]
            if abs(float(ev.ts) - coin.ts) > COIN_SPLIT_JOIN_S:
                break
            if coin.split is None:
                if split > coin.copper:
                    break
                self._set_split(coin, split)
                self._coins[index] = coin
                for entry in self._recent:
                    if entry.ts == coin.entry.ts and entry.kind == "coin":
                        entry.text = coin.entry.text
                return True
        return False

    def _party(self, who: Any) -> tuple[set[str], bool]:
        """The viewer and explicitly identified members, including a solo empty roster.

        An empty shared roster must not fall back to stale loot or former members.
        Standalone sessions use the same evidence rules as the combat meter.
        """
        you = self._you()
        roster = self.roster if self.roster is not None else self._local_roster
        return {who(n) for n in roster.members()} | {you}, True

    def _classify_slain(self, who: Any, detail_limit: int | None = None, *,
                        rate_cutoff: float = -math.inf) -> tuple[Any, Any, Any, int, dict[str, Any]]:
        """Split the slain lines into party kills, party deaths, outsider deaths and outsider kills.

        ``who(name)`` maps a raw name to its displayed spelling.  Rules, in order: a line
        the game addressed to the party ("You have slain", "Your party member X has
        slain") is a party kill; the viewer as victim is a death; an article-led victim
        is a kill (counted when the killer is in the party); a player killed by an
        article-led mob died (counted when in the party); between two capitalised names
        the party decides (a named mob such as "Grandmaster Obadiah" killed by the party
        is a kill).  A second death of the same player within ``DEATH_DEDUP_S`` is the
        same death read again.
        """
        you = self._you()
        party, _known = self._party(who)
        roster = self.roster if self.roster is not None else self._local_roster
        pet_owners = {who(pet).casefold(): who(owner)
                      for pet, owner in self._observed_pet_owners.items()}
        # Manual corrections take precedence over automatic own-pet evidence,
        # matching the combat meter. A pet follows its owner's group membership.
        pet_owners.update({who(pet).casefold(): who(owner)
                           for pet, owner in roster.pet_owners().items()})
        party = {name for name in party if name.casefold() not in pet_owners}
        party_by_name = {name.casefold(): name for name in party}

        def credited_killer(raw: str) -> str:
            name = who(raw)
            owner = pet_owners.get(name.casefold(), name)
            return party_by_name.get(owner.casefold(), owner)

        # Anyone seen killing an "a/an/the" mob is a player (outside the party, too).
        players = party | {credited_killer(k) for _ts, k, v, _o in self._slain if k and is_npc_name(v)}
        kills = deque(maxlen=detail_limit)
        deaths = deque(maxlen=detail_limit)
        outsiders = deque(maxlen=detail_limit)
        totals = {name: Counter() for name in ("killers", "targets", "deaths", "outsiders", "own_targets")}
        totals["kill_targets"] = Counter()
        totals["rate_events"] = []
        def killed(ts: float, killer: str | None, victim: str) -> None:
            kills.append((ts, killer, victim))
            if killer:
                totals["killers"][killer] += 1
            totals["targets"][victim] += 1
            totals["kill_targets"][(killer, victim)] += 1
            if ts > rate_cutoff:
                totals["rate_events"].append((ts, killer, 1, 0, 0, 0))
            if killer == you:
                totals["own_targets"][victim] += 1
        outsider_kills = 0
        last_death: dict[str, float] = {}
        for ts, killer_raw, victim_raw, ours in self._slain:
            killer = credited_killer(killer_raw) if killer_raw else None
            victim = who(victim_raw)
            if victim.casefold() in pet_owners:
                # A summoned or charmed pet dying is neither a monster kill nor
                # a player death, even when its name starts with "a/an/the".
                continue
            pet_killer = bool(killer_raw and who(killer_raw).casefold() in pet_owners)
            if pet_killer:
                # A party-addressed line about an assigned outsider's pet must
                # follow the corrected owner rather than stale party evidence.
                ours = ours and killer in party and victim not in party
            if ours:
                killed(ts, killer, victim)
                continue
            if victim == you:
                is_death = True
            elif is_npc_name(victim):
                is_death = False
            elif killer is not None and is_npc_name(killer):
                is_death = True
            elif victim in party:
                is_death = killer not in party or pet_killer
            elif killer in party:
                is_death = False
            else:
                is_death = victim in players and killer not in players
            if not is_death:
                if killer in party:
                    killed(ts, killer, victim)
                else:
                    outsider_kills += 1
                continue
            previous = last_death.get(victim)
            if previous is not None and abs(ts - previous) < DEATH_DEDUP_S:
                continue
            last_death[victim] = ts
            (deaths if victim in party else outsiders).append((ts, victim))
            totals["deaths" if victim in party else "outsiders"][victim] += 1
        return list(kills), list(deaths), list(outsiders), outsider_kills, totals

    def snapshot(self, now: float | None = None, *, detail_limit: int | None = None) -> SessionSnapshot:
        """Reuse stable session content; only elapsed changes while idle."""
        now = time.time() if now is None else now
        detail_limit = None if detail_limit is None else max(1, int(detail_limit))
        roster = self.roster if self.roster is not None else self._local_roster
        dependency = (self.version, self.player_name, self.include_personal, id(roster), getattr(roster, "version", 0),
                      id(self.vocab), getattr(self.vocab, "_version", 0), self.encounters, self.combat_seconds, detail_limit)
        content = self._snapshot_cache.get(dependency)
        if content is None or now < content._rate_as_of:
            content = self._build_snapshot(now, detail_limit=detail_limit)
            self._snapshot_revision += 1
            content.revision = self._snapshot_revision
            self._snapshot_cache = {dependency: content}
        result = dataclasses.replace(content, elapsed=max(0.0, now - self.started),
                                   player_status=_copy_player_status(content.player_status),
                                   activity_rates=build_activity_rates(content._rate_events, now=now, started=self.started))
        result._personal_totals = content._personal_totals
        result._participant_totals = content._participant_totals
        result._harvest_item_actors = content._harvest_item_actors
        return result

    def _build_snapshot(self, now: float | None = None, *, detail_limit: int | None = None) -> SessionSnapshot:
        """Build the display snapshot (names merged through the OCR-noise canonicaliser
        and, with a vocabulary, mapped to the spellings learned over the session)."""
        now = time.time() if now is None else now
        # the vocabulary keeps different mobs apart ("a jackal" is not "a jackal pup")
        canon = canonical_names(self._names, vocab=self.vocab) if self._names else {}
        vocab = self.vocab
        roster = self.roster if self.roster is not None else self._local_roster
        assignments = dict(self._observed_pet_owners)
        assignments.update(roster.pet_owners())
        # Explicit ownership declares separate identities. Otherwise a similar
        # pet spelling can merge into its owner and hide real player deaths.
        identities = set(assignments) | set(assignments.values())
        protected = {name.casefold(): name for name in sorted(identities)}
        protected.update({name.casefold(): name for name in sorted(roster.members() | {self._you()})
                          if name.casefold() in protected})

        def who(name: str) -> str:
            if name.casefold() in protected:
                return protected[name.casefold()]
            name = canon.get(name, name)
            if vocab is None or is_you(name):
                return name
            return vocab.canonical("npc" if is_npc_name(name) else "player", name) or name

        def item_name(name: str) -> str:
            return (vocab.canonical("item", name) if vocab is not None else name) or name

        def fold(counter: Counter[str], key: Any = who) -> list[tuple[str, int]]:
            merged: Counter[str] = Counter()
            for name, n in counter.items():
                merged[key(name)] += n
            return sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))

        items_by_looter = fold(self._items_by_looter)
        coin_by_looter = fold(self._coin_by_looter)
        cutoff = now - ACTIVITY_RATE_WINDOW
        kill_list, death_list, outsider_list, outsider_kills, slain = self._classify_slain(
            who, detail_limit, rate_cutoff=cutoff)
        rate_events = slain["rate_events"]
        rate_events.extend((entry.ts, who(entry.looter), 0, 1, 0, 0)
                           for entry in self._loot if entry.ts > cutoff)
        kills_by_killer = slain["killers"]
        kills_by_target = slain["targets"]
        deaths_by_player = slain["deaths"]
        outsider_deaths = slain["outsiders"]
        item_looters: dict[str, Counter[str]] = {}
        for item, counter in self._item_looters.items():
            merged = item_looters.setdefault(item_name(item), Counter())
            for looter, n in counter.items():
                merged[who(looter)] += n
        harvest_actors: dict[str, Counter[str]] = {}
        for item, actors in self._harvests_by_item.items():
            harvest_actors.setdefault(item_name(item), Counter()).update(actors)
        harvest_items = Counter({item: sum(actors.values()) for item, actors in harvest_actors.items()})
        own_harvests = Counter({item: sum(count for actor, count in actors.items()
                                        if actor.casefold() == self._you().casefold())
                               for item, actors in harvest_actors.items()})
        own_harvests = Counter({item: count for item, count in own_harvests.items() if count})
        zones: list[str] = []
        for zone in self._zones:
            shown = (vocab.canonical("zone", zone) if vocab is not None else zone) or zone
            if not zones or zones[-1] != shown:
                zones.append(shown)
        # Crafts count only for the viewer and identified party members.
        you = self._you()
        party, known = self._party(who)
        crafts = deque(maxlen=detail_limit)
        crafts_by_crafter: Counter[str] = Counter()
        crafts_by_item: Counter[str] = Counter()
        craft_items_by_participant: Counter[tuple[str, str]] = Counter()
        own_crafts: Counter[str] = Counter()
        outsider_crafts: Counter[str] = Counter()
        for ts, raw, item, n in self._craft_entries:
            crafter, item = who(raw), item_name(item)
            if known and crafter not in party:
                outsider_crafts[crafter] += n
                continue
            crafts.append((ts, crafter, item, n))
            crafts_by_crafter[crafter] += n
            crafts_by_item[item] += n
            craft_items_by_participant[(crafter, item)] += n
            if crafter == you:
                own_crafts[item] += n
        # What the viewer got: every split (theirs by definition), and their own loots without
        # one (solo, no "as your split").
        coin_received = 0
        for coin in self._coins:
            looter = who(coin.looter)
            received = coin.split if coin.split is not None else (coin.copper if looter == you or is_you(coin.looter) else 0)
            coin_received += received
            if coin.ts > cutoff:
                rate_events.append((coin.ts, looter, 0, 0, coin.copper, 0))
                if received:
                    rate_events.append((coin.ts, you, 0, 0, 0, received))
        snap = SessionSnapshot(
            started=self.started,
            elapsed=max(0.0, now - self.started),
            encounters=self.encounters,
            combat_seconds=self.combat_seconds,
            items=len(self._loot),
            items_by_name=fold(self._items_by_name, item_name),
            items_by_looter=items_by_looter,
            item_looters={
                item: sorted(counter.items(), key=lambda kv: (-kv[1], kv[0])) for item, counter in item_looters.items()
            },
            zones=zones,
            kill_entries=kill_list,
            craft_entries=list(crafts),
            loot=[dataclasses.replace(e, looter=who(e.looter), item=item_name(e.item))
                  for e in (self._loot if detail_limit is None else self._loot[-detail_limit:])],
            coin_total=sum(self._coin_by_looter.values()),
            coin_by_looter=coin_by_looter,
            coin_split=sum(c.split for c in self._coins if c.split is not None),
            crafts=sum(crafts_by_crafter.values()),
            crafts_by_crafter=sorted(crafts_by_crafter.items(), key=lambda kv: (-kv[1], kv[0])),
            crafts_by_item=sorted(crafts_by_item.items(), key=lambda kv: (-kv[1], kv[0])),
            kills=sum(kills_by_target.values()),
            kills_by_killer=sorted(kills_by_killer.items(), key=lambda kv: (-kv[1], kv[0])),
            kills_by_target=sorted(kills_by_target.items(), key=lambda kv: (-kv[1], kv[0])),
            deaths=sum(deaths_by_player.values()),
            deaths_by_player=sorted(deaths_by_player.items(), key=lambda kv: (-kv[1], kv[0])),
            cc_total=sum(self._cc_by_type.values()),
            cc_by_type=[(cat, self._cc_by_type[cat]) for cat in CC_CATEGORIES if self._cc_by_type.get(cat)],
            cc_on_npcs=self._cc_on_npcs,
            cc_on_players=self._cc_on_players,
            personal_included=self.include_personal,
            skill_ups=sorted(self._skill_ups.items()),
            faction=sorted(self._faction.items()),
            xp_ticks=self._xp_ticks,
            recent=[dataclasses.replace(e) for e in self._recent],  # a later split line edits the live ones
            outsider_deaths=sorted(outsider_deaths.items(), key=lambda kv: (-kv[1], kv[0])),
            outsider_kills=outsider_kills,
            party=sorted(party - {you}),
            coin_received=coin_received,
            outsider_crafts=sorted(outsider_crafts.items(), key=lambda kv: (-kv[1], kv[0])),
            rewards=[dataclasses.replace(e, looter=who(e.looter), item=item_name(e.item))
                     for e in (self._rewards if detail_limit is None else self._rewards[-detail_limit:])],
            capture_quality=dict(self.capture_quality),
            player_status=_copy_player_status(self.player_status),
            harvests=sum(harvest_items.values()),
            harvests_by_item=fold(harvest_items, lambda n: n),
        )
        own_items = {item: counts.get(you, 0) for item, counts in item_looters.items() if counts.get(you, 0)}
        own_kills = slain["own_targets"]
        # Internal aggregation aid, excluded from dataclass/JSON export. Self filtering
        # keeps full totals when the live view carries only recent detail rows.
        snap._personal_totals = dict(player=you, items=sum(own_items.values()), items_by_name=fold(Counter(own_items), lambda n: n),
                                    crafts=sum(own_crafts.values()), crafts_by_item=fold(own_crafts, lambda n: n),
                                    harvests=sum(own_harvests.values()), harvests_by_item=fold(own_harvests, lambda n: n),
                                    kills=sum(own_kills.values()), kills_by_target=fold(own_kills, lambda n: n))
        # Complete participant allocations are internal like _personal_totals;
        # bounded views can filter former members without scanning raw history.
        snap._participant_totals = {"kill_targets": slain["kill_targets"],
                                    "craft_items": craft_items_by_participant}
        snap._harvest_item_actors = harvest_actors
        # Complete recent activity is separate from bounded display details.
        # Cached timestamps are reprojected as the clock moves, including idle.
        snap._rate_events = rate_events
        snap._rate_as_of = now
        return snap

    # ------------------------------------------------------------------
    def _name(self, raw: str | None) -> str | None:
        if not raw:
            return None
        self._names[raw] += 1
        return raw

    def _push(self, ts: float, kind: str, actor: str | None, text: str) -> SessionEntry:
        entry = SessionEntry(float(ts), kind, actor, text)
        self._recent.append(entry)
        return entry
