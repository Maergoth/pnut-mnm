"""Own-player health alerts and approximate XP from visible stat-panel readings.

XP compares stable bar positions every five continuous minutes. It never invents
raw XP, attributes gains to kills, or bridges a missing panel or a restart.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MAX_GAP = 2.0
XP_INTERVAL = 300.0
LEVEL_GRACE = 8.0
PENDING_LIMIT = 64


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative(value: Any, default: float | None = None) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else default


class PlayerTelemetry:
    """``update`` reports summary changes; ``snapshot`` returns JSON-ready data.

    Readings contain hp_current/hp_max and/or xp_fraction (0..1), optionally
    xp_resolution_points (one bar pixel) and xp_pixels (bar width). HP episodes
    are internal alert signals. Own level-up chat can confirm XP bar wraps.
    """

    def __init__(self, initial: dict[str, Any] | None = None):
        initial = initial if isinstance(initial, dict) else {}
        self._hp_seconds = _nonnegative(initial.get("hp_observed_seconds"))
        self._hp_low_seconds = _nonnegative(initial.get("hp_below10_seconds"))
        self._hp_episodes = _nonnegative(initial.get("hp_below10_episodes"))
        lowest = _number(initial.get("hp_lowest_percent"))
        self._hp_lowest = lowest if lowest is not None and 0 <= lowest <= 100 else None
        if self._hp_seconds is not None:
            self._hp_low_seconds = self._hp_low_seconds or 0.0
            self._hp_episodes = self._hp_episodes or 0
        self._xp_seconds = _nonnegative(initial.get("xp_observed_seconds"))
        self._xp_gain = _nonnegative(initial.get("xp_gain_points"))
        if self._xp_seconds is not None or self._xp_gain is not None:
            self._xp_seconds = self._xp_seconds or 0.0
            self._xp_gain = self._xp_gain or 0.0
        self._xp_wraps = int(_nonnegative(initial.get("xp_level_ups"), 0))
        self._clear_live()

    def _clear_xp(self) -> None:
        self._xp_previous_ts: float | None = None
        self._xp_width: float | None = None
        self._xp_candidate: float | None = None
        self._xp_candidate_count = 0
        self._xp_baseline: tuple[float, float] | None = None
        self._xp_stable: float | None = None
        self._xp_percent: float | None = None
        self._xp_drop: float | None = None
        self._levels: list[float] = []
        self._used_levels: set[float] = set()
        self._checkpoint: tuple[float, float, set[float]] | None = None

    def _clear_live(self) -> None:
        self._hp_previous: tuple[float, float] | None = None
        self._hp_percent: float | None = None
        self._hp_low_count = 0
        self._hp_low = False
        self._last_seen: float | None = None
        self._clock: float | None = None
        self._clear_xp()

    def pause(self) -> None:
        """Clear live baselines, preserving completed measurements and totals."""
        self._clear_live()

    def update(self, reading: Mapping[str, Any] | None, ts: float) -> bool:
        before = self.snapshot()
        now = _number(ts)
        if now is None or not isinstance(reading, Mapping):
            self.pause()
            return self.snapshot() != before
        if self._last_seen is not None and now <= self._last_seen:
            return False
        self._clock = now
        current, maximum = _number(reading.get("hp_current")), _number(reading.get("hp_max"))
        hp = (100.0 * current / maximum if current is not None and maximum is not None
              and maximum > 0 and 0 <= current <= maximum else None)
        self._update_hp(hp, now)
        fraction = _number(reading.get("xp_fraction"))
        if fraction is not None and not 0 <= fraction <= 1:
            fraction = None
        pixels = _number(reading.get("xp_pixels"))
        pixels = pixels if pixels is not None and pixels > 0 else None
        resolution = _number(reading.get("xp_resolution_points"))
        tolerance = (resolution / 100.0 if resolution is not None and 0 < resolution <= 100
                     else 1.0 / max(2.0, pixels or 200.0))
        self._update_xp(fraction, now, tolerance, pixels)
        if hp is not None or fraction is not None:
            self._last_seen = now
        return self.snapshot() != before

    def _update_hp(self, percent: float | None, ts: float) -> None:
        if percent is None:
            self._hp_previous = None
            self._hp_percent = None
            self._hp_low_count = 0
            self._hp_low = False
            return
        previous = self._hp_previous
        contiguous = previous is not None and ts - previous[0] <= MAX_GAP
        if self._hp_seconds is None:
            self._hp_seconds = self._hp_low_seconds = 0.0
            self._hp_episodes = 0
        self._hp_percent = percent
        self._hp_lowest = percent if self._hp_lowest is None else min(self._hp_lowest, percent)
        if contiguous:
            self._hp_seconds += ts - previous[0]
        else:
            self._hp_low_count = 0
            self._hp_low = False
        if percent < 10:
            self._hp_low_count += 1
            if contiguous and previous[1] < 10:
                self._hp_low_seconds += ts - previous[0]
            if self._hp_low_count >= 2 and not self._hp_low:
                self._hp_episodes += 1
                self._hp_low = True
        else:
            self._hp_low_count = 0
            self._hp_low = False
        self._hp_previous = (ts, percent)

    def _resolve_drop(self, ts: float) -> bool:
        if self._xp_drop is None:
            return True
        matching = [level for level in self._levels if level not in self._used_levels
                    and abs(level - self._xp_drop) <= LEVEL_GRACE]
        if matching:
            self._used_levels.add(min(matching, key=lambda level: abs(level - self._xp_drop)))
            self._xp_drop = None
            return True
        if ts - self._xp_drop > LEVEL_GRACE:
            # A death or a bad reading must not become an automatic level wrap.
            self._start_window(ts, self._xp_stable)
        return False

    def _start_window(self, ts: float, fraction: float | None) -> None:
        self._xp_baseline = (ts, fraction) if fraction is not None else None
        self._xp_drop = None
        self._levels = []
        self._used_levels = set()
        self._checkpoint = None

    def _update_xp(self, fraction: float | None, ts: float, tolerance: float,
                   width: float | None) -> None:
        if fraction is None:
            self._clear_xp()
            return
        contiguous = (self._xp_previous_ts is not None and ts - self._xp_previous_ts <= MAX_GAP
                      and width == self._xp_width)
        if not contiguous:
            self._clear_xp()
        self._xp_previous_ts = ts
        self._xp_width = width
        if self._checkpoint is not None and ts - self._checkpoint[1] > LEVEL_GRACE:
            self._checkpoint = None
        if self._xp_gain is None:
            self._xp_gain = self._xp_seconds = 0.0
        if self._xp_candidate is None or abs(fraction - self._xp_candidate) > tolerance + 1e-12:
            self._xp_candidate, self._xp_candidate_count = fraction, 1
            self._resolve_drop(ts)
            return
        self._xp_candidate_count += 1
        self._xp_candidate = fraction
        previous = self._xp_stable
        self._xp_stable = fraction
        self._xp_percent = fraction * 100.0
        if self._xp_baseline is None:
            self._start_window(ts, fraction)
            return
        if previous is not None and fraction < previous - tolerance - 1e-12:
            if self._xp_drop is not None:
                # Two unconfirmed drops cannot establish a trustworthy interval.
                self._start_window(ts, fraction)
                return
            self._xp_drop = ts
        if not self._resolve_drop(ts):
            return
        start, baseline = self._xp_baseline
        duration = ts - start
        if duration < XP_INTERVAL:
            return
        levels = {level for level in self._levels if start < level <= ts}
        delta = fraction - baseline + len(levels)
        if delta < -tolerance - 1e-12:
            self._start_window(ts, fraction)
            return
        points = 0.0 if abs(delta) <= tolerance + 1e-12 else max(0.0, delta * 100.0)
        self._xp_gain += points
        self._xp_seconds += duration
        self._xp_wraps += len(levels)
        self._xp_baseline = (ts, fraction)
        self._levels = [level for level in self._levels if level > ts]
        self._used_levels.intersection_update(self._levels)
        self._checkpoint = (start, ts, levels)

    def note_level_up(self, ts: float) -> bool:
        """Confirm the viewer's level-up; chat may arrive just after a frame."""
        before = self.snapshot()
        now = _number(ts)
        if now is None or self._xp_baseline is None or self._clock is None:
            return False
        if self._checkpoint is not None:
            start, end, accepted = self._checkpoint
            if start < now <= end and self._clock - end <= LEVEL_GRACE:
                if now not in accepted:
                    accepted.add(now)
                    self._xp_gain += 100.0
                    self._xp_wraps += 1
                return self.snapshot() != before
        if not self._xp_baseline[0] < now <= self._clock + LEVEL_GRACE or now in self._levels:
            return False
        self._levels.append(now)
        # More than this many levels in five minutes is untrustworthy input.
        if len(self._levels) > PENDING_LIMIT:
            self._start_window(self._clock, self._xp_stable)
            return False
        self._resolve_drop(self._clock)
        return self.snapshot() != before

    def snapshot(self) -> dict[str, Any]:
        rate = (self._xp_gain * 3600.0 / self._xp_seconds
                if self._xp_gain is not None and self._xp_seconds else None)

        def rounded(value: float | None) -> float | None:
            return round(value, 6) if value is not None and math.isfinite(value) else None

        return {
            "hp_percent": rounded(self._hp_percent), "hp_low": self._hp_low,
            "hp_observed_seconds": rounded(self._hp_seconds),
            "hp_below10_seconds": rounded(self._hp_low_seconds),
            "hp_below10_episodes": int(self._hp_episodes) if self._hp_episodes is not None else None,
            "hp_lowest_percent": rounded(self._hp_lowest),
            "xp_percent": rounded(self._xp_percent), "xp_gain_points": rounded(self._xp_gain),
            "xp_observed_seconds": rounded(self._xp_seconds), "xp_points_per_hour": rounded(rate),
            "xp_level_ups": self._xp_wraps, "last_seen": self._last_seen,
        }
