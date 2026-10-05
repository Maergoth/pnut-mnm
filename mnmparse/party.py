"""The viewer's party, learned from explicit party messages and manual corrections.

Party joins, leader changes, member kills/deaths, and coin loot split with the viewer
identify members. An accepted invitation identifies the inviter. Ordinary loot, corpse-drag
permissions, heals, and fighting nearby do not prove membership. Leaving, being kicked,
disbanding, or joining a new party clears the relevant automatic membership.

The viewer is never on their own roster. Manual inclusion/exclusion and pet assignments
override automatic classification and survive party changes. Explicit members survive a
restart while recently seen. Older saved rosters used ambiguous evidence, so migration keeps
their manual choices but discards their automatic membership.
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

log = logging.getLogger(__name__)

__all__ = ["PartyRoster", "PARTY_FILE"]

PARTY_FILE = "party.json"
RELOAD_MAX_AGE_S = 6 * 3600.0  #: a saved member not seen for longer than this is another session's
INVITE_JOIN_S = 30.0  #: "You have joined the party." this soon after an invite joins the inviter's party

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
        self._invite: tuple[str, float] | None = None  #: the last "X has invited you" (name, ts)
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
            m = _JOINED_RX.match(text) or _LEADER_RX.match(text) or _PARTY_MEMBER_RX.match(text)
            if m:
                return self._add(m.group("name"), ts)
            viewer = is_you(getattr(ev, "raw_actor", None)) or self._is_self(actor)
            if kind == "coin" and getattr(ev, "split_copper", None) is not None and actor and not viewer:
                return self._add(actor, ts)
        return False

    def note_fight(self, names: Iterable[str], ts: float | None = None) -> bool:
        """Refresh explicit members seen fighting; nearby fighters never join the roster.

        The return value remains ``False`` because this cannot change membership.
        """
        ts = float(ts or 0.0) or time.time()
        with self._lock:
            for name in sorted({self._name(n) for n in names} - {""}):
                if name in self._seen:
                    self._seen[name] = max(ts, self._seen[name])
        return False

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
            if not gone:
                return False
            self.version += 1
            log.info("party: %s left", raw.strip())
            return True

    def _clear(self, reason: str) -> bool:
        """Forget the party (not the user's own choices): a new one starts."""
        with self._lock:
            changed = bool(self._seen)
            self._seen.clear()
            self._invite = None
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
        """Compatibility accessor: fighting together no longer infers party membership."""
        return set()

    def members(self) -> set[str]:
        """The group without the viewer: explicit members and manual additions, minus exclusions."""
        with self._lock:
            return (set(self._seen) | self.manual_in) - self.manual_out

    def known(self) -> bool:
        """Whether the roster has explicit members or manual additions.

        An empty roster means solo until evidence arrives; it never makes strangers members.
        """
        with self._lock:
            return bool(self._seen or self.manual_in)

    def manual_state(self, name: str) -> bool | None:
        with self._lock:
            return True if name in self.manual_in else False if name in self.manual_out else None

    # -- persistence -----------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"version": 4, "saved": time.time(), "seen": dict(self._seen),
                    "manual_in": sorted(self.manual_in), "manual_out": sorted(self.manual_out),
                    "pet_owners": dict(self._pet_owners)}

    def save(self, path: str | Path) -> None:
        try:
            Path(path).write_text(json.dumps(self.to_dict(), indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save the party roster: %s", exc)

    def load(self, path: str | Path, *, max_age_s: float = RELOAD_MAX_AGE_S) -> bool:
        """Restore manual choices and recent explicit members from version 4 rosters.

        Earlier versions mixed party evidence with heuristics, even in ``seen``. Their
        automatic membership cannot be trusted and must be relearned after upgrading.
        """
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
            if data.get("version") == 4:
                for name, when, _value in recent(data.get("seen")):
                    self._seen[name] = max(when, self._seen.get(name, 0.0))
            self.version += 1
        return True
