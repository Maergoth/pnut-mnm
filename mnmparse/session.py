"""Session-wide, non-combat bookkeeping: loot, coin, crafting, kills, deaths, crowd control.

Everything here is *shared* information - lines the whole group produces in the Combat
window.  Lines that only the viewer gets about themselves (skill-ups, faction standing, XP
ticks, PvP opt-in, eating) are classified by the parser as ``personal``/``experience`` and
are ignored unless ``include_personal`` is set.

:class:`SessionStats` consumes every :class:`~mnmparse.grammar.Event` of a run (combat and
non-combat alike) and :meth:`SessionStats.snapshot` returns a plain :class:`SessionSnapshot`
for the Session tab.  Pure Python, no Qt.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from .grammar import Event, is_npc_name, is_you
from .interrupts import CC_CATEGORIES
from .party import PartyRoster
from .stats import canonical_names
from .vocab import Vocabulary

__all__ = [
    "LOOTED_BY_YOU", "LootEntry", "SessionEntry", "SessionSnapshot", "SessionStats", "filter_session", "format_coin",
]

RECENT_LIMIT = 200
#: A wrapped "N copper coins from X's corpse as your split." line belongs to a coin loot at most
#: this many seconds before it (the two halves were always read in the same second).
COIN_SPLIT_JOIN_S = 5.0


def format_coin(copper: int) -> str:
    """``10234`` copper -> ``"1g 2s 34c"`` (100 copper = 1 silver, 100 silver = 1 gold,
    100 gold = 1 platinum; zero parts omitted)."""
    copper = int(copper or 0)
    if copper == 0:
        return "0c"
    sign = "-" if copper < 0 else ""
    copper = abs(copper)
    pp, rest = divmod(copper, 1_000_000)
    gp, rest = divmod(rest, 10_000)
    sp, cp = divmod(rest, 100)
    parts = [f"{n}{unit}" for n, unit in ((pp, "p"), (gp, "g"), (sp, "s"), (cp, "c")) if n]
    return sign + " ".join(parts)


@dataclass
class LootEntry:
    """One item leaving a corpse."""

    ts: float
    looter: str
    item: str
    source: str | None  #: the corpse it came from


@dataclass
class SessionEntry:
    """One line of the Session feed (loot, coin, craft, kill, death, cc)."""

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
    mez_breaks: list[tuple[float, str]]  #: (ts, mob) for every "X awakens." (a mesmerize broke)
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

    @property
    def kills_per_hour(self) -> float:
        hours = max(self.elapsed, 60.0) / 3600.0
        return self.kills / hours

    @property
    def items_per_hour(self) -> float:
        hours = max(self.elapsed, 60.0) / 3600.0
        return self.items / hours

    @property
    def coin_per_hour(self) -> float:
        hours = max(self.elapsed, 60.0) / 3600.0
        return self.coin_total / hours


#: The Me view's detail row under the coin it received (``coin_by_looter`` there holds this one row).
LOOTED_BY_YOU = "Looted by you (gross)"


def filter_session(snap: SessionSnapshot, player: str) -> SessionSnapshot:
    """The viewer's own share of ``snap``: loot, coin, kills, deaths and crafts by ``player``.

    The coin is what the viewer received (``coin_received``: their splits, plus their own
    loots without a split), which is also what coin per hour is worked out from; the gross
    coin they looted themselves is one detail row, ``(LOOTED_BY_YOU, copper)``, in
    ``coin_by_looter``.  Zones and mez breaks are events, not attributions, so they stay as
    they are.
    """
    player = player or "You"
    loot = [e for e in snap.loot if e.looter == player]
    items: Counter[str] = Counter(e.item for e in loot)
    kills = [k for k in snap.kill_entries if k[1] == player]
    by_target: Counter[str] = Counter(v for _ts, _k, v in kills)
    crafts = [c for c in snap.craft_entries if c[1] == player]
    by_item: Counter[str] = Counter()
    for _ts, _c, item, n in crafts:
        by_item[item] += n
    gross = sum(c for name, c in snap.coin_by_looter if name == player)
    deaths = [(name, n) for name, n in snap.deaths_by_player if name == player]
    personal = getattr(snap, "_personal_totals", {})
    complete = personal if personal.get("player") == player else {}
    item_counts = complete.get("items_by_name", sorted(items.items(), key=lambda kv: (-kv[1], kv[0])))
    item_total = complete.get("items", len(loot))
    craft_counts = complete.get("crafts_by_item", sorted(by_item.items(), key=lambda kv: (-kv[1], kv[0])))
    craft_total = complete.get("crafts", sum(by_item.values()))
    kill_total = complete.get("kills", len(kills))
    return dataclasses.replace(
        snap,
        items=item_total,
        items_by_name=item_counts,
        items_by_looter=[(player, item_total)] if item_total else [],
        item_looters={item: [(player, n)] for item, n in item_counts},
        loot=loot,
        coin_total=snap.coin_received,
        coin_by_looter=[(LOOTED_BY_YOU, gross)] if gross else [],
        rewards=[e for e in snap.rewards if e.looter == player],
        crafts=craft_total,
        crafts_by_crafter=[(player, craft_total)] if craft_total else [],
        crafts_by_item=craft_counts,
        kills=kill_total,
        kills_by_killer=[(player, kill_total)] if kill_total else [],
        kills_by_target=complete.get("kills_by_target", sorted(by_target.items(), key=lambda kv: (-kv[1], kv[0]))),
        deaths=sum(n for _n, n in deaths),
        deaths_by_player=deaths,
        kill_entries=kills,
        craft_entries=crafts,
    )


#: "Your party member X has slain Y!" (also with a dropped first letter).
_PARTY_LINE_RX = re.compile(r"^\W*[YV]?our\s+party\s+member\s", re.IGNORECASE)
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
        self._mez_breaks: list[tuple[float, str]] = []
        self._kill_entries: list[tuple[float, str | None, str]] = []
        #: every craft line, the party's and other players' (told apart in the snapshot)
        self._craft_entries: list[tuple[float, str, str, int]] = []
        self._coin_by_looter: Counter[str] = Counter()
        self._coins: list[_Coin] = []
        self._rewards: list[LootEntry] = []
        #: every slain line: (ts, killer, victim, said "Your party member" / "You have slain")
        self._slain: list[tuple[float, str | None, str, bool]] = []
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
        kind = ev.kind
        if kind == "loot" and ev.item:
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
        elif kind == "awaken" and ev.target:
            self._mez_breaks.append((float(ev.ts), self._name(ev.target) or ev.target))
            self._push(ev.ts, "awaken", None, f"Mez broke on {ev.target}")
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

    def _classify_slain(self, who: Any, detail_limit: int | None = None) -> tuple[Any, Any, Any, int, dict[str, Any]]:
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
        # Anyone seen killing an "a/an/the" mob is a player (outside the party, too).
        players = party | {who(k) for _ts, k, v, _o in self._slain if k and is_npc_name(v)}
        kills = deque(maxlen=detail_limit)
        deaths = deque(maxlen=detail_limit)
        outsiders = deque(maxlen=detail_limit)
        totals = {name: Counter() for name in ("killers", "targets", "deaths", "outsiders", "own_targets")}
        def killed(ts: float, killer: str | None, victim: str) -> None:
            kills.append((ts, killer, victim))
            if killer:
                totals["killers"][killer] += 1
            totals["targets"][victim] += 1
            if killer == you:
                totals["own_targets"][victim] += 1
        outsider_kills = 0
        last_death: dict[str, float] = {}
        for ts, killer_raw, victim_raw, ours in self._slain:
            killer = who(killer_raw) if killer_raw else None
            victim = who(victim_raw)
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
                is_death = killer not in party
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
        detail_limit = None if detail_limit is None else max(1, int(detail_limit))
        roster = self.roster if self.roster is not None else self._local_roster
        dependency = (self.version, self.player_name, self.include_personal, id(roster), getattr(roster, "version", 0),
                      id(self.vocab), getattr(self.vocab, "_version", 0), self.encounters, self.combat_seconds, detail_limit)
        content = self._snapshot_cache.get(dependency)
        if content is None:
            content = self._build_snapshot(self.started, detail_limit=detail_limit)
            self._snapshot_revision += 1
            content.revision = self._snapshot_revision
            self._snapshot_cache = {dependency: content}
        result = dataclasses.replace(content, elapsed=max(0.0, (time.time() if now is None else now) - self.started))
        result._personal_totals = content._personal_totals
        return result

    def _build_snapshot(self, now: float | None = None, *, detail_limit: int | None = None) -> SessionSnapshot:
        """Build the display snapshot (names merged through the OCR-noise canonicaliser
        and, with a vocabulary, mapped to the spellings learned over the session)."""
        now = time.time() if now is None else now
        # the vocabulary keeps different mobs apart ("a jackal" is not "a jackal pup")
        canon = canonical_names(self._names, vocab=self.vocab) if self._names else {}
        vocab = self.vocab

        def who(name: str) -> str:
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
        kill_list, death_list, outsider_list, outsider_kills, slain = self._classify_slain(who, detail_limit)
        kills_by_killer = slain["killers"]
        kills_by_target = slain["targets"]
        deaths_by_player = slain["deaths"]
        outsider_deaths = slain["outsiders"]
        item_looters: dict[str, Counter[str]] = {}
        for item, counter in self._item_looters.items():
            merged = item_looters.setdefault(item_name(item), Counter())
            for looter, n in counter.items():
                merged[who(looter)] += n
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
            if crafter == you:
                own_crafts[item] += n
        # What the viewer got: every split (theirs by definition), and their own loots without
        # one (solo, no "as your split").
        coin_received = sum(
            c.split if c.split is not None else (c.copper if who(c.looter) == you or is_you(c.looter) else 0)
            for c in self._coins
        )
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
            mez_breaks=[(ts, who(mob)) for ts, mob in (self._mez_breaks if detail_limit is None else self._mez_breaks[-detail_limit:])],
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
        )
        own_items = {item: counts.get(you, 0) for item, counts in item_looters.items() if counts.get(you, 0)}
        own_kills = slain["own_targets"]
        # Internal aggregation aid, excluded from dataclass/JSON export. Self filtering
        # keeps full totals when the live view carries only recent detail rows.
        snap._personal_totals = dict(player=you, items=sum(own_items.values()), items_by_name=fold(Counter(own_items), lambda n: n),
                                    crafts=sum(own_crafts.values()), crafts_by_item=fold(own_crafts, lambda n: n),
                                    kills=sum(own_kills.values()), kills_by_target=fold(own_kills, lambda n: n))
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
