"""Own-player health alerts and approximate XP from visible stat-panel readings.

XP compares stable bar positions about five minutes apart and estimates a
time-weighted recent rate over at most fifteen minutes. Brief OCR misses within
the capture cadence are tolerated; pauses and longer gaps reset live baselines.
It never invents raw XP or attributes gains to kills.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MAX_GAP = 2.0
MAX_SAMPLE_GAP = 10.0
XP_INTERVAL = 300.0
XP_RATE_WINDOW = 900.0
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

    def __init__(self, initial: dict[str, Any] | None = None, *, low_health_threshold: int = 10):
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
        self.set_health_threshold(low_health_threshold)

    def set_health_threshold(self, threshold: int) -> None:
        """Require fresh HP confirmation after changes, preserving the XP window."""
        if type(threshold) is not int or not 1 <= threshold <= 100:
            raise ValueError("Health threshold must be an integer between 1 and 100.")
        self.low_health_threshold = threshold
        self._hp_previous = None
        self._hp_percent = None
        self._hp_low_count = 0
        self._hp_low = False

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
        self._xp_rate_intervals: list[tuple[float, float, float]] = []

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

    def update(self, reading: Mapping[str, Any] | None, ts: float, *, max_gap: float = MAX_GAP) -> bool:
        """Observe a fresh sample within the caller's bounded capture cadence."""
        gap = _number(max_gap)
        if gap is None or not 0 < gap <= MAX_SAMPLE_GAP:
            raise ValueError("Sample gap must be a finite number greater than zero and at most 10 seconds.")
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
        self._update_hp(hp, now, gap)
        fraction = _number(reading.get("xp_fraction"))
        if fraction is not None and not 0 <= fraction <= 1:
            fraction = None
        pixels = _number(reading.get("xp_pixels"))
        pixels = pixels if pixels is not None and pixels > 0 else None
        resolution = _number(reading.get("xp_resolution_points"))
        tolerance = (resolution / 100.0 if resolution is not None and 0 < resolution <= 100
                     else 1.0 / max(2.0, pixels or 200.0))
        self._update_xp(fraction, now, tolerance, pixels, gap)
        if hp is not None or fraction is not None:
            self._last_seen = now
        return self.snapshot() != before

    def _update_hp(self, percent: float | None, ts: float, max_gap: float) -> None:
        if percent is None:
            self._hp_previous = None
            self._hp_percent = None
            self._hp_low_count = 0
            self._hp_low = False
            return
        previous = self._hp_previous
        contiguous = previous is not None and ts - previous[0] <= max_gap
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
        if percent < self.low_health_threshold:
            self._hp_low_count += 1
            if contiguous and previous[1] < self.low_health_threshold:
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
        self._xp_rate_intervals.clear()

    def _record_rate_interval(self, start: float, end: float, points: float) -> None:
        """Keep completed measurements, dropping leading idle before a new gain."""
        self._trim_rate_intervals(end)
        if points > 0 and not any(gain > 0 for _start, _end, gain in self._xp_rate_intervals):
            self._xp_rate_intervals.clear()
        self._xp_rate_intervals.append((start, end, points))
        self._trim_rate_intervals(end)

    def _trim_rate_intervals(self, end: float) -> None:
        cutoff = end - XP_RATE_WINDOW
        recent = []
        for start, stop, points in self._xp_rate_intervals:
            if stop <= cutoff:
                continue
            if start < cutoff:
                # Bar checkpoints only locate gain within this interval. Weight
                # the retained portion instead of averaging interval rates.
                points *= (stop - cutoff) / (stop - start)
                start = cutoff
            recent.append((start, stop, points))
        self._xp_rate_intervals = recent

    def _correct_rate_checkpoint(self, start: float, end: float) -> None:
        for index, (begin, stop, points) in enumerate(self._xp_rate_intervals):
            if stop == end:
                correction = 100.0 * (stop - begin) / (end - start)
                if not any(gain > 0 for _begin, _stop, gain in self._xp_rate_intervals):
                    # A late level line turns an idle-looking checkpoint into
                    # the first gain; earlier idle must still be excluded.
                    self._xp_rate_intervals = [(begin, stop, points + correction)]
                else:
                    self._xp_rate_intervals[index] = (begin, stop, points + correction)
                return
        self._record_rate_interval(start, end, 100.0)

    def _update_xp(self, fraction: float | None, ts: float, tolerance: float,
                   width: float | None, max_gap: float) -> None:
        if fraction is None:
            if self._xp_previous_ts is None or ts - self._xp_previous_ts > max_gap:
                self._clear_xp()
            else:
                # One unclear frame must not discard five minutes of good
                # observations. Recovery still needs two fresh stable reads,
                # and is bounded from the last valid XP sample, not this miss.
                self._xp_percent = None
                self._xp_candidate = None
                self._xp_candidate_count = 0
            return
        contiguous = (self._xp_previous_ts is not None and ts - self._xp_previous_ts <= max_gap
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
        self._record_rate_interval(start, ts, points)
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
                    self._correct_rate_checkpoint(start, end)
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
        rate_gain = sum(points for _start, _end, points in self._xp_rate_intervals)
        rate_seconds = sum(end - start for start, end, _points in self._xp_rate_intervals)
        rate = rate_gain * 3600.0 / rate_seconds if rate_seconds else None

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
            "xp_rate_kind": "rolling", "xp_rate_window_seconds": XP_RATE_WINDOW,
            "xp_rate_gain_points": rounded(rate_gain), "xp_rate_observed_seconds": rounded(rate_seconds),
            "xp_level_ups": self._xp_wraps, "last_seen": self._last_seen,
        }
