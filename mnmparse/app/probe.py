"""Opt-in diagnostics for UI hiccups (``--probe-stalls``).

When enabled, ``logs/app.log`` gets:

- every event the GUI thread took longer than ``SLOW_EVENT_MS`` to handle (which object,
  which event type), from :class:`StallProbeMixin` on the application's ``notify``;
- every Python garbage collection that took longer than ``SLOW_GC_MS``;
- every ``REPORT_S`` seconds, where the other threads were whenever a watchdog thread that
  sleeps 2 ms woke more than ``GIL_LATE_MS`` late (something held Python's interpreter lock,
  which every GUI callback needs too);
- every ``REPORT_S`` seconds, the real intervals of the auto-attack bar's animation frames
  (asked for every 16 ms), so a hiccup the eye sees can be told apart: late frames with a
  slow event or collection next to them are the app; regular frames mean the stutter
  happens later, when Windows composes the overlay over the game.

Off by default, and then nothing here runs.
"""

from __future__ import annotations

import collections
import gc
import logging
import sys
import threading
import time
from typing import Any

from PySide6.QtCore import QCoreApplication, QThread

log = logging.getLogger(__name__)

SLOW_EVENT_MS = 20.0
SLOW_GC_MS = 10.0
GIL_LATE_MS = 15.0
REPORT_S = 30.0

enabled = False
_frames: dict[str, list[float]] = collections.defaultdict(list)
_last: dict[str, float] = {}
_gc_started = 0.0
_slow_events = 0
_depth = 0
_gil_waits: collections.Counter[str] = collections.Counter()
_gil_worst: dict[str, float] = {}


def enable() -> None:
    """Turn the probe on (once, at startup)."""
    global enabled
    if enabled:
        return
    enabled = True
    gc.callbacks.append(_on_gc)
    threading.Thread(target=_gil_watch, name="stall-probe-gil", daemon=True).start()
    log.info("stall probe on: events > %.0f ms, collections > %.0f ms, frame report every %.0f s",
             SLOW_EVENT_MS, SLOW_GC_MS, REPORT_S)


def instrument() -> None:
    """Time the slots the engine's signals reach (queued calls show up in :meth:`notify` only
    as an anonymous "MetaCall"); a slow one is logged by name."""
    if not enabled:
        return
    from mnmparse.app import main, overlay, pages

    targets = {
        main.MainWindow: ("on_snapshot", "on_session", "on_message", "on_status", "on_state", "on_encounter_closed"),
        main.App: ("_on_message_for_overlay", "_on_message_for_triggers", "_on_engine_status", "_on_engine_state"),
        overlay.OverlayWindow: ("set_snapshot", "set_session", "set_status", "observe_event"),
        pages.LivePage: ("set_snapshot", "add_encounter"),
        pages.SessionPage: ("set_session",),
        pages.SettingsPage: ("set_status",),
    }
    for cls, names in targets.items():
        for name in names:
            fn = cls.__dict__.get(name)
            if callable(fn) and not getattr(fn, "_probed", False):
                setattr(cls, name, _timed(f"{cls.__name__}.{name}", fn))


def _timed(label: str, fn: Any) -> Any:
    import functools

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            ms = (time.perf_counter() - started) * 1000.0
            if ms > SLOW_EVENT_MS:
                log.info("stall probe: slot %s took %.0f ms", label, ms)

    wrapper._probed = True  # type: ignore[attr-defined]
    return wrapper


def frame(name: str) -> None:
    """An animation frame of ``name`` was drawn (call from its timer)."""
    if not enabled:
        return
    now = time.perf_counter()
    prev = _last.get(name)
    _last[name] = now
    if prev is not None:
        _frames[name].append((now - prev) * 1000.0)


def pause(name: str) -> None:
    """``name`` stopped animating (or slowed down on purpose): the next frame starts afresh."""
    _last.pop(name, None)


def report() -> None:
    """Log the frame intervals collected since the last report."""
    global _slow_events
    if not enabled:
        return
    for name, xs in list(_frames.items()):
        if not xs:
            continue
        xs.sort()
        n = len(xs)
        log.info(
            "stall probe: %s %d frames, interval p50 %.1f p90 %.1f p99 %.1f max %.1f ms; "
            "late > 25 ms: %d, > 50 ms: %d, > 100 ms: %d; slow GUI events since last report: %d",
            name, n, xs[n // 2], xs[int(n * 0.9)], xs[min(n - 1, int(n * 0.99))], xs[-1],
            sum(x > 25 for x in xs), sum(x > 50 for x in xs), sum(x > 100 for x in xs), _slow_events,
        )
    _frames.clear()
    _slow_events = 0
    if _gil_waits:
        top = "; ".join(f"{n}x (worst {_gil_worst[k]:.0f} ms) {k}" for k, n in _gil_waits.most_common(6))
        log.info("stall probe: interpreter lock held > %.0f ms %d times; threads were at: %s",
                 GIL_LATE_MS, sum(_gil_waits.values()), top)
        _gil_waits.clear()
        _gil_worst.clear()


def _where(frame: Any) -> str:
    """``file:line function`` of a frame and two callers, short."""
    parts = []
    while frame is not None and len(parts) < 3:
        code = frame.f_code
        parts.append(f"{code.co_filename.rsplit(chr(92), 1)[-1].rsplit('/', 1)[-1]}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
    return " < ".join(parts)


def _gil_watch() -> None:
    me = threading.get_ident()
    while True:
        start = time.perf_counter()
        time.sleep(0.002)
        late = (time.perf_counter() - start) * 1000.0 - 2.0
        if late <= GIL_LATE_MS:
            continue
        names = {t.ident: t.name for t in threading.enumerate()}
        for ident, frame in sys._current_frames().items():
            if ident == me or frame is None:
                continue
            key = f"[{names.get(ident, ident)}] {_where(frame)}"
            _gil_waits[key] += 1
            _gil_worst[key] = max(_gil_worst.get(key, 0.0), late)


def _on_gc(phase: str, info: dict[str, Any]) -> None:
    global _gc_started
    if phase == "start":
        _gc_started = time.perf_counter()
        return
    ms = (time.perf_counter() - _gc_started) * 1000.0
    if ms > SLOW_GC_MS:
        log.info("stall probe: garbage collection (generation %s) took %.0f ms, %s collected",
                 info.get("generation"), ms, info.get("collected"))


class StallProbeMixin:
    """Mixed into the QApplication subclass: times every event the GUI thread handles."""

    def notify(self, receiver: Any, event: Any) -> bool:  # noqa: D102 - Qt override
        global _depth, _slow_events
        started = time.perf_counter()
        _depth += 1
        try:
            return super().notify(receiver, event)  # type: ignore[misc]
        finally:
            _depth -= 1
            ms = (time.perf_counter() - started) * 1000.0
            if ms > SLOW_EVENT_MS:
                try:
                    what = f"{type(receiver).__name__}({receiver.objectName() or '-'})"
                    kind = event.type().name if hasattr(event.type(), "name") else int(event.type())
                except Exception:  # noqa: BLE001 - a deleted receiver
                    what, kind = "?", "?"
                gui = QThread.currentThread() is QCoreApplication.instance().thread()  # type: ignore[union-attr]
                if gui:
                    _slow_events += 1
                log.info("stall probe: [%s] %s %s took %.0f ms (nesting %d)",
                         "gui" if gui else "worker", what, kind, ms, _depth)


__all__ = ["StallProbeMixin", "enable", "frame", "instrument", "pause", "report"]
