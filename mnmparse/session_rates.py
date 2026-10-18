"""Pure rolling activity-rate aggregation, independent of visible XP readings."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import math
from typing import Any

from .player_telemetry import XP_RATE_WINDOW

ACTIVITY_RATE_WINDOW = XP_RATE_WINDOW
ActivityRateEvent = tuple[float, str | None, int, int, int, int]


def _number(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _counts(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    result = []
    for count in value:
        number = _number(count)
        if number is None or number < 0 or not number.is_integer():
            return None
        result.append(int(count))
    return result


def _copy_summary(summary: Any) -> dict[str, Any] | None:
    if not isinstance(summary, Mapping) or summary.get("kind") != "rolling":
        return None
    window = _number(summary.get("window_seconds"))
    observed = _number(summary.get("observed_seconds"))
    as_of = _number(summary.get("as_of"))
    basis = summary.get("coin_basis")
    actors = summary.get("by_actor")
    if (window != ACTIVITY_RATE_WINDOW or observed is None or not 0 <= observed <= window
            or as_of is None or basis not in ("gross", "received") or not isinstance(actors, Mapping)):
        return None
    copied = {}
    for actor, values in actors.items():
        counts = _counts(values)
        if isinstance(actor, str) and counts is not None:
            copied[actor] = counts
    return {"kind": "rolling", "window_seconds": ACTIVITY_RATE_WINDOW,
            "as_of": as_of, "observed_seconds": observed, "coin_basis": basis, "by_actor": copied}


def build_activity_rates(events: Iterable[ActivityRateEvent], *, now: float,
                         started: float) -> dict[str, Any]:
    """Aggregate every event in ``(now - one hour, now]``, including complete details.

    Columns are kills, items, gross copper, and personally received copper. The
    caller supplies the appropriate actor for each contribution; receipt of a
    party split can therefore be separate from its looter's gross coin event.
    """
    clock = _number(now)
    valid_clock = clock is not None
    clock = clock if clock is not None else 0.0
    beginning = _number(started)
    observed = (min(ACTIVITY_RATE_WINDOW, max(0.0, clock - beginning))
                if valid_clock and beginning is not None else 0.0)
    actors: dict[str, list[int]] = {}
    if valid_clock:
        cutoff = clock - ACTIVITY_RATE_WINDOW
        for event in events:
            if not isinstance(event, (list, tuple)) or len(event) != 6:
                continue
            timestamp, actor = _number(event[0]), event[1]
            counts = _counts(event[2:])
            if (timestamp is None or not cutoff < timestamp <= clock or counts is None
                    or actor is not None and not isinstance(actor, str)):
                continue
            totals = actors.setdefault(actor or "", [0, 0, 0, 0])
            for index, count in enumerate(counts):
                totals[index] += count
    return {"kind": "rolling", "window_seconds": ACTIVITY_RATE_WINDOW, "as_of": clock,
            "observed_seconds": observed, "coin_basis": "gross", "by_actor": actors}


def project_activity_rates(summary: Any, included: Callable[[str], bool], *,
                           personal_coin: bool = False) -> dict[str, Any]:
    """Copy a summary while retaining only the requested participants."""
    copied = _copy_summary(summary)
    if copied is None:
        return {}
    copied["by_actor"] = {actor: counts for actor, counts in copied["by_actor"].items() if included(actor)}
    if personal_coin:
        copied["coin_basis"] = "received"
    return copied


def activity_rate(summary: Any, key: str, *, fallback_count: float, elapsed: float) -> float:
    """Return a rolling rate, or the original session rate for a legacy snapshot."""
    if key not in ("kills", "items", "coin"):
        return 0.0
    copied = _copy_summary(summary)
    if copied is None:
        count = _number(fallback_count)
        total = max(0.0, count) if count is not None else 0.0
        duration = max(60.0, _number(elapsed) or 0.0)
    else:
        column = {"kills": 0, "items": 1, "coin": 3 if copied["coin_basis"] == "received" else 2}[key]
        total = sum(counts[column] for counts in copied["by_actor"].values())
        duration = max(60.0, min(ACTIVITY_RATE_WINDOW, copied["observed_seconds"]))
    try:
        result = total * 3600.0 / duration
    except OverflowError:
        return 0.0
    return result if math.isfinite(result) else 0.0
