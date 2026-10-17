"""Own-player health alerts and approximate XP from visible stat-panel readings.

XP compares stable bar positions about thirty seconds apart and estimates a
time-weighted recent rate over at most one hour. Brief OCR misses within
the capture cadence are tolerated; pauses and longer gaps reset live baselines.
It never invents raw XP or attributes gains to kills.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MAX_GAP = 2.0
MAX_SAMPLE_GAP = 10.0
XP_INTERVAL = 30.0
XP_RATE_WINDOW = 3600.0
# Allow an ordinary delayed comparison, but do not present an indefinitely old
# rate while the bar keeps failing stability checks.
XP_RATE_STALE_AFTER = 2 * XP_INTERVAL
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
        self._xp_candidate_ts: float | None = None
        self._xp_baseline: tuple[float, float] | None = None
        self._xp_stable: float | None = None
        self._xp_percent: float | None = None
        self._xp_drop: float | None = None
        self._xp_drop_first_ts: float | None = None
        self._levels: list[float] = []
        self._used_levels: set[float] = set()
        self._checkpoint: tuple[float, float, set[float]] | None = None
        self._xp_rate_intervals: list[tuple[float, float, float]] = []
        self._xp_pending_gain_start: float | None = None
        self._xp_pending_last_fraction: float | None = None

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
        first_low = self._xp_drop_first_ts if self._xp_drop_first_ts is not None else self._xp_drop
        matching = [level for level in self._levels if level not in self._used_levels
                    and abs(level - first_low) <= LEVEL_GRACE]
        if matching:
            self._used_levels.add(min(matching, key=lambda level: abs(level - first_low)))
            self._xp_drop = None
            self._xp_drop_first_ts = None
            return True
        if ts - self._xp_drop > LEVEL_GRACE:
            # A death or a bad reading must not become an automatic level wrap.
            self._start_window(ts, self._xp_stable)
        return False

    def _start_window(self, ts: float, fraction: float | None) -> None:
        self._xp_baseline = (ts, fraction) if fraction is not None else None
        self._xp_drop = None
        self._xp_drop_first_ts = None
        self._levels = []
        self._used_levels = set()
        self._checkpoint = None
        self._xp_rate_intervals.clear()
        self._xp_pending_gain_start = None
        self._xp_pending_last_fraction = None

    def _record_rate_interval(self, start: float, end: float, points: float) -> None:
        """Keep completed measurements, dropping leading idle before a new gain."""
        self._trim_rate_intervals(end)
        if points > 0 and not any(gain > 0 for _start, _end, gain in self._xp_rate_intervals):
            # A small change may need several checkpoints to exceed one pixel.
            # Retain that measured time rather than credit all gain to the last
            # thirty seconds; only idle before the first movement is excluded.
            pending = self._xp_pending_gain_start
            self._xp_rate_intervals = [interval for interval in self._xp_rate_intervals
                                       if pending is not None and interval[1] > pending]
        pending = self._xp_pending_gain_start
        if (points > 0 and pending is not None and pending < start
                and self._xp_pending_last_fraction is not None and self._xp_baseline is not None):
            # A carried movement was measured across several checkpoints. The
            # pixel deadband delayed confirmation, not the XP itself. Spread
            # only the earlier portion over its measurement span so it ages out
            # there. New gains and confirmed levels stay in the latest interval.
            # As with a boundary inside one checkpoint, assume uniform progress
            # where pixel resolution cannot locate the gain more precisely.
            carried = min(points, max(0.0, 100.0 * (self._xp_pending_last_fraction - self._xp_baseline[1])))
            rate = carried / (start - pending)
            self._xp_rate_intervals = [
                (begin, stop, gain + rate * max(0.0, stop - max(begin, pending)))
                for begin, stop, gain in self._xp_rate_intervals
            ]
            points -= carried
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

    def _update_xp(self, fraction: float | None, ts: float, tolerance: float,
                   width: float | None, max_gap: float) -> None:
        if fraction is None:
            if self._xp_previous_ts is None or ts - self._xp_previous_ts > max_gap:
                self._clear_xp()
            else:
                # One unclear frame must not discard completed bar
                # observations. Recovery still needs two fresh stable reads,
                # and is bounded from the last valid XP sample, not this miss.
                self._xp_percent = None
                self._xp_candidate = None
                self._xp_candidate_count = 0
                self._xp_candidate_ts = None
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
            self._xp_candidate_ts = ts
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
            # Match chat to the first low frame, not its later confirmation.
            # The confirmation still starts the ordinary wait for late chat.
            self._xp_drop = ts
            self._xp_drop_first_ts = self._xp_candidate_ts if self._xp_candidate_ts is not None else ts
        if not self._resolve_drop(ts):
            return
        start, baseline = self._xp_baseline
        duration = ts - start
        if duration < XP_INTERVAL:
            return
        levels = {level for level in self._levels if level <= ts}
        if any(level not in self._used_levels and ts - level <= LEVEL_GRACE for level in levels):
            # Chat can precede the visible bar wrap. Do not publish a whole
            # level against a still-old endpoint while that transition settles.
            # Older confirmations still cover full wraps missed between reads.
            return
        if (self._xp_pending_gain_start is not None
                and ts - self._xp_pending_gain_start > XP_RATE_WINDOW):
            # An uncredited change of at most one pixel cannot remain pending
            # forever and later masquerade as new XP. Age out that uncertain
            # movement against the last checkpoint's bar position.
            baseline = self._xp_pending_last_fraction if self._xp_pending_last_fraction is not None else baseline
            self._xp_pending_gain_start = None
            self._xp_pending_last_fraction = None
        delta = fraction - baseline + len(levels)
        if delta < -tolerance - 1e-12:
            self._start_window(ts, fraction)
            return
        points = 0.0 if abs(delta) <= tolerance + 1e-12 else max(0.0, delta * 100.0)
        if points == 0 and delta > 1e-12:
            if self._xp_pending_gain_start is None:
                self._xp_pending_gain_start = start
            self._xp_pending_last_fraction = fraction
        elif delta <= 1e-12:
            self._xp_pending_gain_start = None
            self._xp_pending_last_fraction = None
        self._xp_gain += points
        self._xp_seconds += duration
        self._xp_wraps += len(levels)
        self._record_rate_interval(start, ts, points)
        # Advance elapsed time once per checkpoint while carrying sub-pixel
        # gains against the last credited bar position. Repeated small changes
        # must neither disappear nor count the same XP more than once.
        self._xp_baseline = (ts, baseline if points == 0 and not levels else fraction)
        if points > 0:
            self._xp_pending_gain_start = None
            self._xp_pending_last_fraction = None
        self._levels = [level for level in self._levels if level > ts]
        self._used_levels.intersection_update(self._levels)
        self._checkpoint = (start, ts, levels)

    def note_level_up(self, ts: float) -> bool:
        """Confirm the viewer's level-up; chat may arrive just after a frame."""
        before = self.snapshot()
        now = _number(ts)
        if now is None or self._xp_baseline is None or self._clock is None:
            return False
        recent_checkpoint = False
        if self._checkpoint is not None:
            start, end, accepted = self._checkpoint
            if start < now <= end and self._clock - end <= LEVEL_GRACE:
                if now in accepted:
                    return False
                recent_checkpoint = True
        if ((not recent_checkpoint and not self._xp_baseline[0] < now <= self._clock + LEVEL_GRACE)
                or now in self._levels):
            return False
        # A late chat line belongs to the next verified comparison, even when
        # its timestamp precedes that comparison's baseline. Keeping it pending
        # lets a delayed visible wrap resolve without a phantom 100-point gain.
        self._levels.append(now)
        # More than this many pending levels is untrustworthy input.
        if len(self._levels) > PENDING_LIMIT:
            self._start_window(self._clock, self._xp_stable)
            return False
        self._resolve_drop(self._clock)
        return self.snapshot() != before

    def snapshot(self) -> dict[str, Any]:
        rate_gain = rate_seconds = 0.0
        cutoff = self._clock - XP_RATE_WINDOW if self._clock is not None else -math.inf
        for start, end, points in self._xp_rate_intervals:
            # Project accepted measurements onto the current rolling window.
            # Leave their original endpoints intact for future projections,
            # weighting only the retained portion.
            stop = min(end, self._clock) if self._clock is not None else end
            duration = max(0.0, stop - max(start, cutoff))
            measured_duration = end - start
            if measured_duration > 0:
                rate_gain += points * duration / measured_duration
                rate_seconds += duration
        if (self._xp_rate_intervals and self._clock is not None
                and self._clock - self._xp_rate_intervals[-1][1] > XP_RATE_STALE_AFTER):
            # Unstable samples are unknown progress, not proven idle time. Keep
            # accepted history for recovery, but show collecting until a fresh
            # comparison can replace this stale estimate.
            rate_gain = rate_seconds = 0.0
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
