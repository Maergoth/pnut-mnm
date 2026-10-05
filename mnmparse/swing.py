"""Auto-attack timing from the viewer's own melee lines.

The chat never says when the next swing comes, so the swing delay is learned from the
gaps between the viewer's swings ("You crush ...", "You try to crush ..., but miss!").
OCR timestamps are frame times (6 fps), and a line lost to OCR doubles a gap, so the
delay is a robust estimate: a low quantile of the gaps as the base, then every gap divided
by its nearest whole number of delays and the median of those taken.

Which weapon swung: the game writes the off hand and ranged shots out ("You slash X with
your offhand for ...", "X pierces Y with their bow ..."), so the main hand, the off hand
and the bow each get their own bar.  Only when two same-type weapons give no such hint
(no "offhand" in the lines) are the swings split into two hands by rhythm, a best guess.
Bars keep the order in which the hands were first seen, so they never swap rows.

The bar fills at the learned delay and waits at full until the swing line is read: it never
resets on a guess.  A line reaches the app about a quarter second after the swing (the OCR
must see it in two frames), so a bar that resets starts from the time already gone since the
swing.  A cast holds the swing until the spell lands, which shows as a full bar waiting.
The delay changes only when a swing is read (never while the bar fills): it follows a
rolling estimate, and two gaps in a row that agree with each other but differ from it by more
than ``REGIME_CHANGE`` (a haste buff starting or ending, a weapon swap) replace it at once.
"""

from __future__ import annotations

import bisect
import statistics
from collections import deque
from dataclasses import dataclass

__all__ = ["HandState", "SwingTracker", "estimate_delay"]

#: Melee-style verbs that are abilities, not the auto attack.
ABILITY_VERBS = frozenset({"kick", "kicks", "bash", "bashes", "slam", "backstab", "taunt", "strike", "jab", "jabs"})
MIN_GAP_S = 0.5  #: gaps shorter than this are two hands (or one OCR frame), not one delay
SAME_FRAME_S = 0.05  #: gaps this small are two lines read in the same frame: no evidence of anything
MAX_GAP_S = 8.0  #: longer gaps are a pause in the fight
HISTORY_S = 90.0  #: swings older than this no longer shape the estimate
IDLE_FACTOR = 2.5  #: no swing for this many delays: out of combat
MIN_GAPS = 2  #: gaps needed before a delay is shown
BASE_QUANTILE = 0.3  #: the base gap (robust against gaps doubled by lines the OCR lost)
PERIOD_SWINGS = 24  #: swings behind the rolling delay estimate
REGIME_CHANGE = 0.10  #: two agreeing gaps this far (share) from the delay replace it at once
REGIME_AGREE = 0.08  #: ...when they are this close (share) to each other
REGIME_MIN_S = 0.4  #: ...and at least this far (seconds): more than one OCR frame (1/6 s, 1/4 s at 4 fps)
REGIME_SETTLE_GAPS = 4  #: after a switch, gaps needed before the rolling estimate takes over again


@dataclass
class HandState:
    """What the bar draws for one hand."""

    label: str  #: "crush", "off hand", "bow", or "hand 1" / "hand 2" for a rhythm split
    last: float  #: time of the last swing
    delay: float | None  #: learned seconds between swings (None: not enough data yet)
    swings: int  #: swings counted in the history window

    def progress(self, now: float) -> float | None:
        """0..1 of the way to the next swing, holding at 1 until the swing line is read;
        ``None`` without a delay."""
        if not self.delay:
            return None
        return min(1.0, max(0.0, (now - self.last) / self.delay))

    def due(self, now: float) -> bool:
        """True while the bar waits at full for the swing line."""
        return bool(self.delay) and now - self.last >= self.delay

    def idle(self, now: float) -> bool:
        delay = self.delay or 3.0
        return now - self.last > IDLE_FACTOR * delay + 1.0


def estimate_delay(times: list[float]) -> float | None:
    """Robust swing delay from (sorted) swing times (see module docstring)."""
    gaps = sorted(b - a for a, b in zip(times, times[1:]) if MIN_GAP_S <= b - a <= MAX_GAP_S)
    if len(gaps) < MIN_GAPS:
        return None
    base = gaps[min(len(gaps) - 1, int(BASE_QUANTILE * len(gaps)))]
    refined = [g / max(1, round(g / base)) for g in gaps]
    return round(statistics.median(refined), 2)


def _clean_gaps(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:]) if MIN_GAP_S <= b - a <= MAX_GAP_S]


class _Rhythm:
    """The delay shown for one bar, updated only when a new swing is read (see the module
    docstring)."""

    def __init__(self) -> None:
        self.delay: float | None = None
        self.last = float("-inf")  #: last swing taken in
        self.since = float("-inf")  #: when the current delay regime began

    def update(self, times: list[float]) -> float | None:
        """Take the swings in ``times`` (sorted) newer than the last one seen."""
        if times and times[-1] > self.last:
            start = bisect.bisect_right(times, self.last)
            for i in range(max(start, 1), len(times)):
                self.delay = self._next(times[: i + 1])
            self.last = times[-1]
        return self.delay

    def _next(self, times: list[float]) -> float | None:
        current = self.delay
        if current and times[-1] - times[-2] > IDLE_FACTOR * current + 1.0:
            self.since = float("-inf")  # a new fight: the whole history counts again
        recent = _clean_gaps(times[-3:])
        if current and len(recent) == 2:
            mid = (recent[0] + recent[1]) / 2
            agree = abs(recent[0] - recent[1]) <= REGIME_AGREE * mid
            jump = max(REGIME_CHANGE * current, REGIME_MIN_S)  # one late OCR frame is no new rhythm
            if agree and abs(mid - current) > jump and mid < 1.6 * current:
                self.since = times[-3]
                return round(mid, 2)  # haste on or off, a weapon swap: switch now
        regime = [t for t in times[-PERIOD_SWINGS:] if t >= self.since]
        if current and len(_clean_gaps(regime)) < REGIME_SETTLE_GAPS:
            return current  # just switched: two or three gaps are too few to estimate from
        est = estimate_delay(regime)
        return est if est is not None else (current or estimate_delay(times[-PERIOD_SWINGS:]))


def _hand_key(verb: str, weapon: str | None) -> str:
    w = (weapon or "").lower()
    if w.startswith("off"):  # "offhand", OCR "offand" / "offfand"
        return "off hand"
    if w == "bow" or w.startswith("bow"):
        return "bow"
    return verb


class SwingTracker:
    """Collects the viewer's auto-attack swings and reports one :class:`HandState` per hand."""

    def __init__(self, player_name: str = "") -> None:
        self.player_name = player_name or "You"
        self._swings: deque[tuple[float, str]] = deque(maxlen=240)  #: (ts, hand key), sorted
        self._order: dict[str, int] = {}  #: hand key -> first-seen rank (stable bar order)
        self._split_anchor: float | None = None  #: a swing time of "hand 1" in a rhythm split
        self._rhythms: dict[str, _Rhythm] = {}  #: shown delay per bar label

    def observe(self, ev: object) -> bool:
        """Record ``ev`` when it is one of the viewer's auto-attack swings."""
        kind = getattr(ev, "kind", "")
        if kind not in ("melee_hit", "melee_miss") or getattr(ev, "is_pet", False):
            return False
        actor = getattr(ev, "actor", None)
        if actor not in (self.player_name, "You"):
            return False
        verb = str(getattr(ev, "skill", "") or "").lower()
        if not verb or verb in ABILITY_VERBS:
            return False
        ts = float(getattr(ev, "ts", 0.0) or 0.0)
        if self._swings and ts < self._swings[-1][0] - 1.0:
            return False  # far out of order (a re-read line): ignore
        key = _hand_key(verb, getattr(ev, "weapon", None))
        self._order.setdefault(key, len(self._order))
        # slightly out of order (lines of one frame): keep the list sorted
        items = list(self._swings)
        pos = bisect.bisect_right([t for t, _k in items], ts)
        if pos == len(items):
            self._swings.append((ts, key))
        else:
            items.insert(pos, (ts, key))
            self._swings = deque(items, maxlen=self._swings.maxlen)
        return True

    def clear(self) -> None:
        self._swings.clear()
        self._order.clear()
        self._split_anchor = None
        self._rhythms.clear()

    def hands(self, now: float) -> list[HandState]:
        recent = [(ts, key) for ts, key in self._swings if now - ts <= HISTORY_S]
        if not recent:
            return []
        by_key: dict[str, list[float]] = {}
        for ts, key in recent:
            by_key.setdefault(key, []).append(ts)
        explicit = any(k in ("off hand", "bow") for k in by_key)
        busy = [k for k, t in by_key.items() if len(t) >= 3 or (explicit and k in ("off hand", "bow"))]
        busy.sort(key=lambda k: self._order.get(k, 99))
        if len(busy) >= 2:
            # main hand + off hand / bow, or two weapon types (different verbs)
            return [self._hand(k, by_key[k]) for k in busy[:3]]
        key = busy[0] if busy else min(by_key, key=lambda k: (-len(by_key[k]), self._order.get(k, 99)))
        times = by_key.get(key, [t for t, _k in recent])
        split = None if explicit else self._split_same_verb(times)
        if split is None:
            self._split_anchor = None
            return [self._hand(key, times)]
        first, second = self._stable_split(*split)
        return [self._hand("hand 1", first), self._hand("hand 2", second)]

    def _hand(self, label: str, times: list[float]) -> HandState:
        delay = self._rhythms.setdefault(label, _Rhythm()).update(times)
        return HandState(label, times[-1], delay, len(times))

    def _stable_split(self, a: list[float], b: list[float]) -> tuple[list[float], list[float]]:
        """Keep "hand 1" the same hand from call to call (by phase against a remembered swing)."""
        delay = estimate_delay(a) or estimate_delay(b)
        if self._split_anchor is None or not delay:
            self._split_anchor = a[-1]
            return a, b

        def phase(hand: list[float]) -> float:
            off = (hand[-1] - self._split_anchor) % delay
            return min(off, delay - off)

        return (a, b) if phase(a) <= phase(b) else (b, a)

    @staticmethod
    def _split_same_verb(times: list[float]) -> tuple[list[float], list[float]] | None:
        """Two same-type weapons without "offhand" in the lines: split by phase, or ``None``.

        Used only when many gaps are short but real (two hands swinging close together;
        same-frame duplicates are not evidence).
        """
        if len(times) < 8:
            return None
        gaps = [b - a for a, b in zip(times, times[1:]) if b - a <= MAX_GAP_S]
        if len(gaps) < 6:
            return None
        short = sum(1 for g in gaps if SAME_FRAME_S < g < MIN_GAP_S)
        if short < 0.25 * len(gaps):
            return None
        hands: list[list[float]] = [[times[0]], []]
        for ts in times[1:]:
            if not hands[1]:
                hands[1].append(ts)
                continue
            predictions = []
            for hand in hands:
                delay = estimate_delay(hand) if len(hand) >= 3 else None
                predictions.append(hand[-1] + delay if delay else None)
            if all(p is not None for p in predictions):
                pick = min(range(2), key=lambda i: abs(ts - predictions[i]))  # type: ignore[operator]
            else:
                pick = 0 if len(hands[0]) <= len(hands[1]) else 1  # alternate until both have a rhythm
            hands[pick].append(ts)
        if min(len(h) for h in hands) < 3:
            return None
        return hands[0], hands[1]
