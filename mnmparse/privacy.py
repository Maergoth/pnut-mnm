"""One allowlisted presentation policy for Carebear Mode.

Capture and correction keep their original data. Only this restricted projection may
reach Carebear views, clipboard, files, speech or support exports. Unknown source text
is withheld rather than attempting to recognize and replace every possible name.
"""
from __future__ import annotations

import dataclasses
import math
import re
from typing import Any

CASUAL_LABEL = "🌼 Carebear Mode 🌼"
ELITIST_LABEL = "Elitist Scumbag Mode"
PROMISE = ("will not use this to shit on your teammates. Video games are not "
           "hard enough for you to be an asshole.")
MIN_AVERAGE_GROUP = 3
AVERAGE_UNAVAILABLE_TEXT = "At least three active group members are needed for this average."


def casual_enabled(cfg: Any = None) -> bool:
    return bool(getattr(cfg, "casual_mode", True)) or not bool(
        getattr(cfg, "casual_mode_confirmed", False))


def _own(name: str | None, player: str) -> bool:
    return bool(player) and str(name or "").casefold() in {player.casefold(), "you", "your", "yourself"}


def rounded_average(total: float, count: int) -> float:
    """Round to two significant digits; never expose exact small-cohort totals."""
    value = max(0.0, float(total)) / max(1, count)
    return round(value, 1 - int(math.floor(math.log10(value)))) if value else 0.0


def metric_available(row: Any, key: str) -> bool:
    """A protected role average can be absent without changing numeric model fields."""
    metric = {"dps": "damage", "hps": "heals", "heal": "heals"}.get(key, key)
    counts = getattr(row, "average_counts", {}) or {}
    return metric not in counts or counts[metric] >= MIN_AVERAGE_GROUP


def _safe_skill(text: str, peers: set[str]) -> bool:
    folded = str(text).casefold()
    return not any(name and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", folded) for name in peers)


def project_encounter(snap: Any, cfg: Any = None) -> Any:
    if snap is None or not casual_enabled(cfg) or getattr(snap, "privacy_mode", "") == "casual":
        return snap
    from mnmparse.app.models import ActorRow, owner_row

    player = str(getattr(cfg, "player_name", "") or "").strip()
    peers = {str(r.name).casefold() for r in snap.rows if not _own(r.name, player)}
    peers.update(str(n).casefold() for n in snap.group_members if not _own(n, player))
    # Roster spelling can differ in case from the captured actor's stable name.
    names = {str(r.name).casefold(): str(r.name) for r in snap.rows}
    for row in snap.rows:
        if row.pet_owner:
            names.setdefault(row.pet_owner.casefold(), row.pet_owner)
    own = owner_row(snap, names.get(player.casefold(), player), you_name=player) if player else None
    if own is not None:
        peers.difference_update(name.casefold() for name in own.attributed_pets)
    own_share = min(1.0, max(0.0, float(own.share))) if own is not None else 0.0
    rows = []
    if own is not None and not own.is_enemy:
        rows.append(dataclasses.replace(
            own, name=player, is_you=True, is_npc=False, is_enemy=False, in_group=True,
            share=own_share, taken_from=[], killed_by={}, pet_owner="",
            skills=[s for s in own.skills if _safe_skill(s.skill, peers)],
            heal_skills=[s for s in own.heal_skills if _safe_skill(s.skill, peers)],
            cc_skills={k: v for k, v in own.cc_skills.items() if _safe_skill(k, peers)},
            debuff_skills={k: v for k, v in own.debuff_skills.items() if _safe_skill(k, peers)},
        ))
    members = {str(n).casefold(): str(n) for n in snap.group_members if n}
    count = len(members) if player and player.casefold() in members else 0
    if count >= MIN_AVERAGE_GROUP:
        people = [owner_row(snap, names.get(n.casefold(), n)) for n in members.values()]
        people = [r for r in people if r is not None and not r.is_enemy and not r.is_npc and not r.is_pet]
        damage_people = [r for r in people if r.damage > 0 and r.damage >= r.heals]
        healing_people = [r for r in people if r.heals > 0 and r.heals >= r.damage]
        damage_count = len(damage_people)
        healing_count = len(healing_people)
        # Each role needs its own privacy cohort; idle roster slots cannot make an
        # individual healer or damage dealer's result safe to disclose.
        damage = (rounded_average(sum(r.damage for r in damage_people), damage_count)
                  if damage_count >= MIN_AVERAGE_GROUP else 0.0)
        heals = (rounded_average(sum(r.heals for r in healing_people), healing_count)
                 if healing_count >= MIN_AVERAGE_GROUP else 0.0)
        avg = lambda key: rounded_average(sum(float(getattr(r, key, 0)) for r in people), count)
        duration = max(1.0, float(snap.duration))
        rows.append(ActorRow(
            name=f"Group average ({count})", damage=damage, dps=damage / duration,
            taken=avg("taken"), dtps=avg("taken") / duration, heals=heals,
            hps=heals / duration, healed=avg("healed"), swings=0, hits=0, misses=0,
            hit_pct=0.0, max_hit=0, avg_hit=0.0,
            share=1.0 - own_share if snap.total_damage > 0 else 0.0, color="#86b998",
            is_you=False, is_npc=False, is_pet=False, misses_shown=False,
            utility=avg("utility"), cc=avg("cc"), deaths=avg("deaths"),
            average_counts={"damage": damage_count if damage_count >= MIN_AVERAGE_GROUP else 0,
                            "heals": healing_count if healing_count >= MIN_AVERAGE_GROUP else 0},
        ))
    return dataclasses.replace(
        snap, label="Your encounter", rows=rows, group_members=[player] if player else [],
        total_damage=rows[0].damage if rows and rows[0].is_you else 0,
        raid_dps=rows[0].dps if rows and rows[0].is_you else 0.0,
        event_count=0, killed=[], kills=0, privacy_mode="casual",
        group_average_count=count if count >= MIN_AVERAGE_GROUP else 0,
    )


def project_session(snap: Any, cfg: Any = None) -> Any:
    if snap is None or not casual_enabled(cfg) or getattr(snap, "privacy_mode", "") == "casual":
        return snap
    from mnmparse.session import SessionEntry, filter_session

    player = str(getattr(cfg, "player_name", "") or "").strip()
    # Blank identity fails closed, including imported sessions whose 'You' may be somebody else.
    own = filter_session(snap, player or "\0")
    loot = [dataclasses.replace(e, looter=player, source=None) for e in own.loot]
    rewards = [dataclasses.replace(e, looter=player, source=None) for e in own.rewards]
    crafts = [(ts, player, item, n) for ts, _actor, item, n in own.craft_entries]
    kills = [(ts, player, "Enemy") for ts, _actor, _target in own.kill_entries]
    recent = [SessionEntry(e.ts, "loot", player, f"You looted {e.item}") for e in loot[-200:]]
    members = {n.casefold() for n in snap.party if n}
    if player:
        members.add(player.casefold())
    count = len(members) if player and snap.party else 0
    averages = {key: rounded_average(getattr(snap, key), count)
                for key in ("items", "coin_total", "crafts", "deaths")} if count >= MIN_AVERAGE_GROUP else {}
    return dataclasses.replace(
        own, loot=loot, rewards=rewards, craft_entries=crafts, kill_entries=kills,
        recent=recent, party=[player] if player else [], outsider_deaths=[], outsider_kills=0,
        outsider_crafts=[], mez_breaks=[], cc_total=0, cc_by_type=[], cc_on_npcs=0, cc_on_players=0,
        kills_by_target=[("Enemy", own.kills)] if own.kills else [],
        coin_split=own.coin_split if player else 0, coin_received=own.coin_received if player else 0,
        coin_total=own.coin_total if player else 0,
        skill_ups=own.skill_ups if player else [], faction=own.faction if player else [],
        xp_ticks=own.xp_ticks if player else 0,
        privacy_mode="casual", group_averages=averages,
        group_average_count=count if averages else 0,
    )


def safe_event_text(event: Any, cfg: Any = None) -> str | None:
    """Render only own structured actions. Raw/unknown/chat/peer lines are withheld."""
    if not casual_enabled(cfg):
        return str(getattr(event, "text", "") or "")
    player = str(getattr(cfg, "player_name", "") or "").strip()
    kind = str(getattr(event, "kind", ""))
    if not _own(getattr(event, "actor", None), player):
        return None
    amount = getattr(event, "amount", None)
    amount_text = f" ({amount})" if amount is not None else ""
    safe_kinds = {"melee_hit": "You dealt damage", "ability_hit": "You dealt damage",
                  "ability_partial": "You dealt damage", "melee_miss": "You missed",
                  "ability_miss": "Your ability missed", "heal": "You healed",
                  "cast": "You cast an ability", "resist": "Your ability was resisted",
                  "fizzle": "Your spell fizzled", "kill": "You defeated an enemy",
                  "loot": "You looted an item", "craft": "You crafted an item",
                  "coin": "You looted coins", "coin_split": "You received coins",
                  "reward": "You received a reward", "experience": "You gained experience",
                  "personal": "Your personal information changed"}
    return safe_kinds[kind] + amount_text if kind in safe_kinds else None


def personal_trigger_title(trigger: Any, line: str, cfg: Any = None) -> tuple[str, str]:
    """Approve only an authored static title for a personal event or public preset."""
    from mnmparse.grammar import is_you
    from mnmparse.parser import parse_line
    from mnmparse.trigger_presets import PRESETS
    from types import SimpleNamespace

    for preset in PRESETS:
        if trigger.id == preset["id"] and trigger.name == preset["name"]:
            return trigger.name, ""
    player = str(getattr(cfg, "player_name", "") or "").strip()
    event = parse_line(line, 0.0, player)
    raw_actor = str(event.raw_actor or "").strip()
    raw_folded = raw_actor.casefold()
    personal_actor = (is_you(raw_actor) or raw_folded.startswith("your ")
                      or bool(player and raw_folded.startswith(player.casefold() + "'s ")))
    own_cfg = SimpleNamespace(player_name=player, casual_mode=True)
    personal = (safe_event_text(event, own_cfg) is not None
                or event.kind == "status" and (_own(event.actor, player) or personal_actor)
                or event.kind != "unknown" and personal_actor)
    if not personal:
        return "Timer", ""
    peers = {str(name).casefold() for name in (event.actor, event.target)
             if name and not _own(name, player) and not is_you(str(name))
             and not (name == event.actor and personal_actor)}
    for candidate in (trigger.timer_label, trigger.name):
        candidate = str(candidate or "").strip()
        if candidate and "{" not in candidate and "}" not in candidate and _safe_skill(candidate, peers):
            return candidate, player
    return "Timer", ""


def safe_static_trigger_title(trigger: Any, cfg: Any = None) -> str:
    """A definition can show its personal title without exposing its pattern."""
    if not casual_enabled(cfg):
        return trigger.name
    pattern = str(trigger.pattern or "")
    if trigger.mode == "regex":
        # Only anchored literal personal prefixes can establish self provenance.
        # Dynamic expressions stay generic until an actual own event is matched.
        player = str(getattr(cfg, "player_name", "") or "").strip()
        own_prefix = r"(?:You|Your" + ("|" + re.escape(player) if player else "") + r")\b"
        if not pattern.startswith("^") or not re.match(own_prefix, pattern[1:]) or "|" in pattern:
            return personal_trigger_title(trigger, "", cfg)[0]
        literal = re.split(r"[\[\]{}()*+?|$]", pattern[1:], maxsplit=1)[0]
        pattern = re.sub(r"\\([.!?])", r"\1", literal)
    return personal_trigger_title(trigger, pattern, cfg)[0]


def safe_trigger_label(label: str, cfg: Any = None) -> str:
    return "Timer" if casual_enabled(cfg) else label


def safe_trigger_definition(trigger: Any, cfg: Any = None) -> dict[str, Any] | None:
    if casual_enabled(cfg):
        return None
    return dataclasses.asdict(trigger) if dataclasses.is_dataclass(trigger) else dict(trigger)


def safe_config_data(data: dict[str, Any]) -> dict[str, Any]:
    """Technical report settings never include arbitrary user text or peer data."""
    keys = {"config_schema", "capture_backend", "crop", "fps", "ocr_engine", "ocr_scale",
            "preprocess", "encounter_timeout_s", "stats_interval_s", "feed_max_lines",
            "overlay_opacity", "overlay_font_scale", "casual_mode", "reduced_motion",
            "history_recent_fights", "history_retention_days", "revenge_enabled",
            "revenge_days", "revenge_entries"}
    return {key: data[key] for key in keys if key in data}
