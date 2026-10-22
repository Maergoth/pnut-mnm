"""Celebrate fresh group kills using only the current zone's authoritative NPC list."""
from __future__ import annotations

from collections import OrderedDict
import re
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

from mnmparse.grammar import is_you
from mnmparse.maps import zone_title

PAIR_SECONDS = 2.0
PENDING_SECONDS = 30.0
MAX_PENDING = 16
MAX_SEEN = 256


def name_key(name: str) -> str:
    return " ".join(str(name or "").replace("_", " ").replace("’", "'").split()).casefold()


def eligible_group_event(event: Any, player: str, members: set[str], pets: dict[str, str],
                         known_players: set[str] = frozenset()) -> bool:
    """Resolve live ownership under the engine lock, before GUI delivery can be delayed."""
    actor, target = name_key(event.actor), name_key(event.target)
    people = {name_key(name) for name in members} | {name_key(player), "you"}
    people.discard("")
    owners = {name_key(pet): name_key(owner) for pet, owner in pets.items()}
    if (not actor or not target or target in people or target in owners
            or target in {name_key(name) for name in known_players}
            or re.search(r"\bPvP\b", event.text, re.I)
            or re.match(r"^Your (?:party member|pet) .+? (?:has|have) been slain\b", event.text, re.I)):
        return False
    return (is_you(event.raw_actor or event.actor) or actor in people
            or bool(event.is_pet) or owners.get(actor) in people)


class NotableKillController(QObject):
    celebrated = Signal(str)

    def __init__(self, details: Any, *, current_zone: Callable[[], str],
                 active: Callable[[], bool], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.details = details
        self._current_zone, self._active = current_zone, active
        self._zone = ""
        self._pending: list[tuple[float, Any]] = []
        self._seen: OrderedDict[int, Any] = OrderedDict()
        self._pairs: OrderedDict[tuple[str, str], tuple[float, str, str]] = OrderedDict()
        self._closed = False
        details.notables_updated.connect(self._notables_updated)

    def _live_zone(self) -> str:
        return zone_title(str(self._current_zone() or ""))

    def set_zone(self, zone: str) -> None:
        zone = zone_title(str(zone or ""))
        if zone != self._live_zone():
            return  # A queued transition already superseded by a newer captured zone.
        if zone != self._zone:
            self._zone = zone
            self._pending.clear()
            self._pairs.clear()
        if zone and self._active() and not self._closed:
            self.details.ensure_notables(zone)

    def on_state(self, state: str) -> None:
        if state != "running":
            self._pending.clear()
        else:
            self.set_zone(self._live_zone())

    def _eligible(self, zone: str) -> bool:
        return (not self._closed and self._active() and bool(zone)
                and zone == self._live_zone())

    @staticmethod
    def _canonical(target: str, record: Any, zone: str) -> str:
        if record is None or zone_title(str(getattr(record, "zone", ""))) != zone:
            return ""
        key = name_key(target)
        names = tuple(getattr(record, "names", ()))
        exact = [name for name in names if name_key(name) == key]
        if len(exact) == 1:
            return exact[0]
        bare = re.sub(r"^(?:a|an|the) ", "", key)
        candidates = [name for name in names if re.sub(r"^(?:a|an|the) ", "", name_key(name)) == bare]
        return candidates[0] if len(candidates) == 1 else ""

    def on_activity(self, event: Any, zone: str) -> None:
        zone = zone_title(zone)
        if not self._eligible(zone):
            return
        key = (zone, re.sub(r"^(?:a|an|the) ", "", name_key(event.target)))
        previous = self._pairs.get(key)
        if previous is not None and float(event.ts) > previous[0]:
            self._pairs.pop(key, None)

    def on_kill(self, event: Any, zone: str) -> None:
        zone = zone_title(zone)
        if not self._eligible(zone) or getattr(event, "kind", "") != "kill":
            return
        if zone != self._zone:
            self.set_zone(zone)
        identity = id(event)
        if identity in self._seen:
            return
        # Distinct rows in one captured frame can describe two identical mobs.
        # Retain object identities, not timestamp/text fingerprints, for redelivery.
        self._seen[identity] = event
        while len(self._seen) > MAX_SEEN:
            self._seen.popitem(last=False)
        record = self.details.repo.cached_notables(zone)
        if record is None:
            now = time.monotonic()
            self._pending = [(stamp, old) for stamp, old in self._pending if now - stamp <= PENDING_SECONDS]
            self._pending.append((now, event))
            self._pending = self._pending[-MAX_PENDING:]
            self.details.ensure_notables(zone)
            return
        self._deliver(event, record, zone)

    def _notables_updated(self, zone: str, record: Any) -> None:
        zone = zone_title(zone)
        if record is None or zone != self._zone or not self._eligible(zone):
            return
        pending, self._pending = self._pending, []
        now = time.monotonic()
        for stamp, event in pending:
            if now - stamp <= PENDING_SECONDS:
                self._deliver(event, record, zone)

    def _deliver(self, event: Any, record: Any, zone: str) -> None:
        name = self._canonical(event.target, record, zone)
        if not name:
            return
        key = (zone, re.sub(r"^(?:a|an|the) ", "", name_key(name)))
        form = "passive" if re.search(r"\bbeen slain by\b", event.text, re.I) else "active"
        actor, timestamp = name_key(event.actor), float(event.ts)
        previous = self._pairs.get(key)
        if (previous is not None and previous[1] == actor and previous[2] != form
                and 0 <= timestamp - previous[0] <= PAIR_SECONDS):
            self._pairs.pop(key, None)  # A complementary slay/death pair is one kill.
            return
        self._pairs[key] = (timestamp, actor, form)
        self._pairs.move_to_end(key)
        while len(self._pairs) > MAX_SEEN:
            self._pairs.popitem(last=False)
        if self.details.panel.enabled:
            self.details.show_npc(name)
        self.celebrated.emit(name)

    def shutdown(self) -> None:
        self._closed = True
        self._pending.clear()
        self._pairs.clear()
        self._seen.clear()
