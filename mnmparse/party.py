"""Who is in the viewer's group (party).

The Combat chat names party members in a few kinds of line, and the roster learns from all of
them: "Your party member X has slain ..." (or "... has been slain by ..."), loot and coin-split
lines (only party loot is announced), "X has joined the party.", "X is now the leader of the
party.", "You give X permission to drag all your existing corpses." (printed for every member
when the viewer zones in after a death), "X has invited you to their party." followed by "You
have joined the party.", and the viewer's group heals (one spell healing two or more players at
once: "Your Restorative Smite heals X for 11 Health." for each member).  "X has left the
party." and "X has been kicked from the party." remove X; joining a party, "Your party has been
disbanded.", "You have left the party." and the viewer being kicked empty it.

Those lines can be rare (a healer or an enchanter seldom loots or lands a killing blow), and
when the app starts in the middle of a group none of them may come for a long time.  So the
roster also infers members: a player who fought on the viewer's side in SHARED_FIGHTS fights
since the last join or disband counts as one (:meth:`PartyRoster.note_fight`; strangers nearby
seldom share more than two).  An inferred member is weaker than the chat: a join, a disband or
a kick forgets it, and the user's own choice wins.

The viewer is never on their own roster (it is "the group without the viewer"), and only
one-word player names are taken ("L Pidef", "a rat" are not players).

The game never says whose pet a pet is (only the viewer's own: "Your pet ..."), so a party
member's pet looks like a stranger.  The user can fix that, or any other mistake, by hand:
:meth:`PartyRoster.set_manual` counts a name in or out of the group whatever the chat says;
:meth:`PartyRoster.set_pet_owner` records who owns a summoned or charmed combatant.

The roster is saved next to the logs (``party.json``, private) and reloaded on start; each
member is kept while they were seen recently, so a restart in the middle of a session does not
forget the group.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .grammar import is_you
from .vocab import close_spellings, fold

log = logging.getLogger(__name__)

__all__ = ["PartyRoster", "PARTY_FILE", "SHARED_FIGHTS"]

PARTY_FILE = "party.json"
RELOAD_MAX_AGE_S = 6 * 3600.0  #: a saved member not seen for longer than this is another session's
INVITE_JOIN_S = 30.0  #: "You have joined the party." this soon after an invite joins the inviter's party
GROUP_HEAL_S = 1.5  #: the lines of one group heal arrive within this of each other
SHARED_FIGHTS = 3  #: fights on the viewer's side that make a player an inferred member

_NAME = r"(?P<name>[A-Z][A-Za-z'`]{1,30})"
_JOINED_RX = re.compile(rf"^\W*{_NAME}\s+(?:has|have)\s+joined\s+the\s+party", re.IGNORECASE)
_LEADER_RX = re.compile(rf"^\W*{_NAME}\s+is\s+now\s+the\s+leader\s+of\s+the\s+party", re.IGNORECASE)
_LEFT_RX = re.compile(rf"^\W*{_NAME}\s+(?:has|have)\s+left\s+the\s+party", re.IGNORECASE)
_DISBANDED_RX = re.compile(r"^\W*(?:[YV]?our\s+party\s+has\s+been\s+disbanded|You\s+have\s+left\s+the\s+party)", re.IGNORECASE)
#: "You have joined the party." (the viewer joined someone's group: a new party).
_YOU_JOINED_RX = re.compile(r"^\W*[YV]?ou\s+have\s+joined\s+the\s+party", re.IGNORECASE)
#: "X has been kicked from the party." (X may be the viewer: "<player> has been ...", "You have been ...").
_KICKED_RX = re.compile(rf"^\W*{_NAME}\s+(?:has|have)\s+been\s+kicked\s+from\s+the\s+party", re.IGNORECASE)
_INVITED_RX = re.compile(rf"^\W*{_NAME}\s+has\s+invited\s+[YV]?ou\s+to\s+(?:their|the|a)\s+party", re.IGNORECASE)
#: "You give X permission to drag all your existing corpses." (often cut off after "drag").
_DRAG_RX = re.compile(rf"^\W*[YV]?ou\s+give\s+{_NAME}\s+permission\b", re.IGNORECASE)
#: "Your party member X has slain Y!" / "... has been slain by Y!" (also with a dropped first letter).
_PARTY_MEMBER_RX = re.compile(rf"^\W*[YV]?our\s+party\s+member\s+{_NAME}\s+has\s+(?:been\s+)?slain\b", re.IGNORECASE)
#: A player name: one capitalised word (no spaces, hyphens or digits).
_PLAYER_NAME_RX = re.compile(r"^[A-Z][A-Za-z'`]{1,30}$")
_YOU = {"you", "your", "yourself", "ou", "vou"}


class PartyRoster:
    """The party as the chat reveals it, plus the user's own corrections (thread-safe).

    ``player_name`` is the viewer (never a member); ``canonical`` maps a name to its learned
    spelling (``Vocabulary.canonical("player", ...)``) so OCR variants are one member.
    """

    def __init__(self, player_name: str = "", canonical: Callable[[str], str] | None = None) -> None:
        self._lock = threading.RLock()
        self.player_name = str(player_name or "")
        self._canonical = canonical
        self._seen: dict[str, float] = {}  #: name -> when last seen as a party member
        #: name -> when last seen, for players inferred from shared fights (not named by the chat)
        self._inferred: dict[str, float] = {}
        #: name -> (fights on the viewer's side since the last join or disband, last one's time)
        self._shared: dict[str, tuple[int, float]] = {}
        self._invite: tuple[str, float] | None = None  #: the last "X has invited you" (name, ts)
        self._heals: list[tuple[float, str, str]] = []  #: the viewer's recent heals: (ts, spell, target)
        self.manual_in: set[str] = set()  #: counted as group whatever the chat says
        self.manual_out: set[str] = set()  #: never counted as group
        self._pet_owners: dict[str, str] = {}  #: manual ownership, persisted across sessions
        self.version = 0  #: bumped on every change (for saving, and so recent fights are re-counted)

    # -- learning --------------------------------------------------------------------------
    def observe(self, ev: Any) -> bool:
        """Learn from one parsed event; ``True`` when the roster changed."""
        text = str(getattr(ev, "text", "") or "")
        kind = str(getattr(ev, "kind", "") or "")
        actor = str(getattr(ev, "actor", "") or "")
        ts = float(getattr(ev, "ts", 0.0) or 0.0) or time.time()
        with self._lock:
            if _DISBANDED_RX.match(text):
                return self._clear("party disbanded")
            if _YOU_JOINED_RX.match(text):
                invite, self._invite = self._invite, None
                changed = self._clear("joined a party")
                if invite is not None and 0.0 <= ts - invite[1] <= INVITE_JOIN_S:
                    changed = self._add(invite[0], ts) or changed
                return changed
            m = _KICKED_RX.match(text)
            if m:
                if self._is_self(m.group("name")):
                    return self._clear("kicked from the party")
                return self._remove(m.group("name"))
            m = _LEFT_RX.match(text)
            if m:
                return self._remove(m.group("name"))
            m = _INVITED_RX.match(text)
            if m:
                self._invite = (m.group("name"), ts)
                return False
            m = (_JOINED_RX.match(text) or _LEADER_RX.match(text) or _DRAG_RX.match(text)
                 or _PARTY_MEMBER_RX.match(text))
            if m:
                return self._add(m.group("name"), ts)
            viewer = is_you(getattr(ev, "raw_actor", None)) or self._is_self(actor)
            if kind in ("loot", "coin") and actor and not viewer:
                return self._add(actor, ts)
            if kind == "heal" and viewer:
                return self._group_heal(str(getattr(ev, "skill", "") or ""), str(getattr(ev, "target", "") or ""), ts)
        return False

    def note_fight(self, names: Iterable[str], ts: float | None = None) -> bool:
        """Count one closed fight in which ``names`` fought on the viewer's side; ``True`` when
        that made someone an inferred member (their SHARED_FIGHTS-th such fight since the last
        join or disband).  Members already on the roster are only marked as still around."""
        ts = float(ts or 0.0) or time.time()
        changed = False
        with self._lock:
            for name in sorted({self._name(n) for n in names} - {""}):
                if name in self._seen:
                    self._seen[name] = max(ts, self._seen[name])
                    continue
                if name in self._inferred:
                    self._inferred[name] = max(ts, self._inferred[name])
                    continue
                count = self._shared.get(name, (0, 0.0))[0] + 1
                self._shared[name] = (count, ts)
                if count >= SHARED_FIGHTS:
                    self._inferred[name] = ts
                    self.version += 1
                    changed = True
                    log.info("party: %s (fought alongside you in %d fights)", name, count)
        return changed

    def _group_heal(self, spell: str, target: str, ts: float) -> bool:
        """The viewer healed ``target``: when the same spell healed another player at the same
        moment it was a group heal, and everyone it healed is in the party."""
        name = self._name(target)
        if not name or not spell:
            return False
        spell = fold(spell)
        self._heals = [h for h in self._heals if abs(ts - h[0]) <= GROUP_HEAL_S]
        self._heals.append((ts, spell, name))
        healed: list[str] = []
        for _ts, other_spell, other in self._heals:
            # Two close spellings of a target are one heal read twice, not two players.
            if close_spellings(other_spell, spell) and not any(close_spellings(fold(other), fold(h)) for h in healed):
                healed.append(other)
        if len(healed) < 2:
            return False
        changed = False
        for other in healed:
            changed = self._add(other, ts) or changed
        return changed

    def _is_self(self, name: str | None) -> bool:
        """True when ``name`` is the viewer (a pronoun or the configured character name)."""
        key = (name or "").strip().casefold()
        return key in _YOU or (bool(self.player_name) and key == self.player_name.strip().casefold())

    def _name(self, raw: str | None) -> str:
        """``raw`` as a member name (its learned spelling), or ``""`` when it is not a player
        name (two words, a hyphen, a digit) or is the viewer."""
        name = (raw or "").strip()
        if not _PLAYER_NAME_RX.match(name) or self._is_self(name):
            return ""
        if self._canonical is not None:
            name = (self._canonical(name) or name).strip()
            if not _PLAYER_NAME_RX.match(name) or self._is_self(name):
                return ""
        return name

    def _add(self, raw: str, ts: float) -> bool:
        name = self._name(raw)
        if not name:
            return False
        with self._lock:
            new = name not in self._seen
            self._seen[name] = max(ts, self._seen.get(name, 0.0))
            self._inferred.pop(name, None)  # the chat says so now
            if new:
                self.version += 1
                log.info("party: %s", name)
            return new

    def _remove(self, raw: str) -> bool:
        names = {raw.strip(), self._name(raw)} - {""}
        with self._lock:
            gone = False
            for name in names:
                gone = self._seen.pop(name, None) is not None or gone
                gone = self._inferred.pop(name, None) is not None or gone
                self._shared.pop(name, None)
            if not gone:
                return False
            self.version += 1
            log.info("party: %s left", raw.strip())
            return True

    def _clear(self, reason: str) -> bool:
        """Forget the party (not the user's own choices): a new one starts."""
        with self._lock:
            changed = bool(self._seen or self._inferred)
            self._seen.clear()
            self._inferred.clear()
            self._shared.clear()
            self._heals.clear()
            if not changed:
                return False
            self.version += 1
            log.info("party: %s", reason)
            return True

    def set_manual(self, name: str, in_group: bool | None) -> None:
        """Count ``name`` in (``True``) or out (``False``) of the group, or follow the chat
        again (``None``)."""
        name = name.strip()
        if not name:
            return
        with self._lock:
            self.manual_in.discard(name)
            self.manual_out.discard(name)
            if in_group is True:
                self.manual_in.add(name)
            elif in_group is False:
                self.manual_out.add(name)
            self.version += 1

    def set_pet_owner(self, pet: str, owner: str | None) -> None:
        """Assign a combatant to a player, or clear its manual assignment with ``None``.

        Pet names may be NPC names (charmed creatures). Ownership follows the owner's
        current group membership; it never permanently adds a former member to the group.
        """
        pet = str(pet or "").strip()
        owner = str(owner or "").strip()
        if not pet or self._is_self(pet):
            return
        if owner:
            owner = (self.player_name or "You") if self._is_self(owner) else self._name(owner)
            if not owner or pet.casefold() == owner.casefold():
                return
        with self._lock:
            if owner and (owner in self._pet_owners or any(pet.casefold() == p.casefold() for p in self._pet_owners.values())):
                return  # a pet cannot own another pet, and an owner cannot become its pet
            if self._pet_owners.get(pet, "") == owner:
                return
            if owner:
                self._pet_owners[pet] = owner
            else:
                self._pet_owners.pop(pet, None)
            self.version += 1

    def pet_owners(self) -> dict[str, str]:
        """A copy of the manual pet-to-player assignments."""
        with self._lock:
            return dict(self._pet_owners)

    # -- answers ---------------------------------------------------------------------------
    def seen(self) -> set[str]:
        """Names the chat showed as party members (no manual changes, nothing inferred)."""
        with self._lock:
            return set(self._seen)

    def inferred(self) -> set[str]:
        """Players taken as members because they shared SHARED_FIGHTS fights with the viewer."""
        with self._lock:
            return set(self._inferred)

    def members(self) -> set[str]:
        """The group without the viewer: seen, inferred or added by hand, minus the names
        taken out."""
        with self._lock:
            return (set(self._seen) | set(self._inferred) | self.manual_in) - self.manual_out

    def known(self) -> bool:
        """True once anything says who the party is (else everyone counts as group)."""
        with self._lock:
            return bool(self._seen or self._inferred or self.manual_in)

    def manual_state(self, name: str) -> bool | None:
        with self._lock:
            return True if name in self.manual_in else False if name in self.manual_out else None

    # -- persistence -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"version": 3, "saved": time.time(), "seen": dict(self._seen),
                    "inferred": dict(self._inferred),
                    "shared": {name: [count, ts] for name, (count, ts) in self._shared.items()},
                    "manual_in": sorted(self.manual_in), "manual_out": sorted(self.manual_out),
                    "pet_owners": dict(self._pet_owners)}

    def save(self, path: str | Path) -> None:
        try:
            Path(path).write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save the party roster: %s", exc)

    def load(self, path: str | Path, *, max_age_s: float = RELOAD_MAX_AGE_S) -> bool:
        """Take a saved roster: the manual choices always, each member only when they were seen
        within ``max_age_s`` (an older one is another session's group)."""
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        oldest = time.time() - max_age_s

        def recent(table: Any) -> Iterable[tuple[str, float, Any]]:
            """``(name, last seen, saved value)`` of the entries seen since ``oldest``."""
            for raw, value in (table.items() if isinstance(table, dict) else ()):
                try:
                    when = float((value[-1] if isinstance(value, list) and value else value) or 0.0)
                except (TypeError, ValueError):
                    continue
                name = self._name(raw) if isinstance(raw, str) else ""
                if name and when >= oldest:
                    yield name, when, value

        with self._lock:
            self.manual_in |= {str(n) for n in data.get("manual_in") or [] if n}
            self.manual_out |= {str(n) for n in data.get("manual_out") or [] if n}
            pets = data.get("pet_owners", {})
            if isinstance(pets, dict):
                for pet, owner in pets.items():
                    if isinstance(pet, str) and isinstance(owner, str):
                        self.set_pet_owner(pet, owner)
            for name, when, _value in recent(data.get("seen")):
                self._seen[name] = max(when, self._seen.get(name, 0.0))
                self._inferred.pop(name, None)
            for name, when, _value in recent(data.get("inferred")):
                if name not in self._seen:
                    self._inferred[name] = max(when, self._inferred.get(name, 0.0))
            for name, when, value in recent(data.get("shared")):  # name: [fights, last one's time]
                try:
                    count = int(value[0])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue
                if name not in self._seen and name not in self._inferred and count > self._shared.get(name, (0, 0.0))[0]:
                    self._shared[name] = (count, when)
            self.version += 1
        return True
