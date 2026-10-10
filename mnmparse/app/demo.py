"""Synthetic, deterministic data for a desktop preview without game capture or logs."""
from __future__ import annotations

from dataclasses import dataclass

from mnmparse.app.models import EncounterSnapshot, build_snapshot
from mnmparse.grammar import Event
from mnmparse.session import SessionSnapshot, SessionStats
from mnmparse.stats import Stats


@dataclass(frozen=True)
class DemoBundle:
    player_name: str
    events: tuple[Event, ...]
    encounters: tuple[EncounterSnapshot, ...]
    session: SessionSnapshot
    stats: Stats


def demo_bundle(player_name: str = "DemoHero") -> DemoBundle:
    """Build fresh data each time. Caller applies the same privacy projection as live.

    No Engine, Qt, capture, vocabulary files, user history or disk/network writes
    are involved. Every participant except the explicitly supplied self name is
    fictional. Use a persistent Demo banner when presenting this data in the app.
    """
    player = str(player_name or "DemoHero").strip() or "DemoHero"
    names = [player, "DemoAri", "DemoBram", "DemoCyra", "DemoDune", "DemoEris"]
    base = 1_700_000_000.0
    stats = Stats(player_name=player)
    for name in names[1:]:
        stats.roster.set_manual(name, True)
    session = SessionStats(player, started=base, roster=stats.roster)
    events: list[Event] = []

    def add(event: Event):
        events.append(event)
        stats.add(event)
        session.add(event)

    add(Event(base, "zone", "Entering Demo Training Grounds.", target="Demo Training Grounds"))
    for fight, target in enumerate(("a training skeleton", "a practice beetle")):
        start = base + 5 + fight * 60
        for turn in range(5):
            for index, name in enumerate(names):
                ts = start + turn * 3 + index * .12
                amount = 18 + (index * 7 + turn * 11 + fight * 5) % 47
                raw = "You" if name == player else name
                add(Event(ts, "melee_hit", f"{raw} hit {target} for {amount} points of damage.",
                          actor=name, raw_actor=raw, target=target, amount=amount, skill="hit", outcome="hit"))
            add(Event(start + turn * 3 + .8, "heal", f"DemoCyra heals {player} for 28 points.",
                      actor="DemoCyra", raw_actor="DemoCyra", target=player, amount=28, skill="Minor Mend"))
            add(Event(start + turn * 3 + 1, "melee_hit", f"{target} hits you for 9 points of damage.",
                      actor=target, raw_actor=target, target=player, amount=9, skill="hit", outcome="hit"))
        add(Event(start + 16, "ability_hit", f"Your Spark hits {target} for 62 points of damage.",
                  actor=player, raw_actor="You", target=target, amount=62, skill="Spark", outcome="hit"))
        add(Event(start + 17, "kill", f"You have slain {target}.", actor=player, raw_actor="You", target=target))
        add(Event(start + 18, "loot", f"You loot [Training Token] from {target}'s corpse.",
                  actor=player, raw_actor="You", target=target, item="Training Token"))
        add(Event(start + 19, "coin", f"You loot 12 copper coins from {target}'s corpse.",
                  actor=player, raw_actor="You", target=target, copper=12))
        stats.expire(start + 45)
    snapshots = tuple(build_snapshot(stats, enc, player, now=enc.end) for enc in stats.history)
    for snapshot in snapshots:
        session.note_encounter(snapshot.duration)
    return DemoBundle(player, tuple(events), snapshots, session.snapshot(base + 100), stats)


__all__ = ["DemoBundle", "demo_bundle"]
