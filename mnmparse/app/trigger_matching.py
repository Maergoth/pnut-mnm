"""FIFO chat matching off the GUI thread; Qt owns delivery, timers and audio."""
from __future__ import annotations

from dataclasses import dataclass, replace
import logging
from queue import Empty, Full, Queue
import threading
import time

from PySide6.QtCore import QObject, Signal

from mnmparse.triggers import Match, Trigger, TriggerStore

log = logging.getLogger(__name__)
MAX_PENDING_LINES = 2048


@dataclass(frozen=True)
class MatchRequest:
    line: str
    observed_at: float
    queued_at: float
    epoch: int
    triggers: tuple[Trigger, ...]


@dataclass(frozen=True)
class MatchResult:
    request: MatchRequest
    matches: list[Match]
    warnings: dict[str, str]
    disabled_ids: frozenset[str]
    delivered: threading.Event


class TriggerMatcher(QObject):
    completed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._requests: Queue[MatchRequest | None] = Queue(MAX_PENDING_LINES)
        self._stop = threading.Event()
        self._epoch = 0
        self._thread: threading.Thread | None = None

    def submit(self, line: str, observed_at: float, epoch: int, triggers: tuple[Trigger, ...]) -> bool:
        if self._stop.is_set():
            return False
        request = MatchRequest(line, observed_at, time.monotonic(), epoch, triggers)
        try:
            self._requests.put_nowait(request)
        except Full:
            return False
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="PNUT-trigger-matching", daemon=True)
            self._thread.start()
        return True

    def invalidate(self, epoch: int) -> None:
        self._epoch = epoch
        self._drain()

    def close(self) -> None:
        self._stop.set()
        self._drain()
        self._requests.put_nowait(None)

    def _drain(self) -> None:
        while True:
            try:
                self._requests.get_nowait()
            except Empty:
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            request = self._requests.get()
            if request is None:
                return
            cancelled = lambda: self._stop.is_set() or request.epoch != self._epoch
            if cancelled():
                continue
            store = TriggerStore()
            # Matching may disable an unsafe expression. Never mutate the GUI's
            # definitions or the original snapshot used to reject stale results.
            store.triggers = [replace(trigger) for trigger in request.triggers]
            started = time.monotonic()
            try:
                matches = store.matches(request.line, line_budget_s=None,
                                        retry_busy=True, cancelled=cancelled)
            except Exception:
                log.exception("Background trigger matching failed")
                matches = []
                store.match_warnings = {"": "Trigger matching could not complete. Check the diagnostic log."}
            if cancelled():
                continue
            elapsed = time.monotonic() - started
            queued = max(0.0, started - request.queued_at)
            if elapsed > .150 or queued > .150:
                log.warning("Trigger matching delivery: queue_ms=%.1f match_ms=%.1f triggers=%d",
                            queued * 1000, elapsed * 1000, len(request.triggers))
            result = MatchResult(request, matches, dict(store.match_warnings),
                                 frozenset(t.id for t in store.triggers if not t.enabled), threading.Event())
            try:
                self.completed.emit(result)
            except RuntimeError:
                # The QObject owner can be destroyed while a native match ends.
                return
            # Keep at most one result awaiting GUI delivery. Otherwise a stalled
            # GUI could accumulate an unbounded second queue of Qt signal payloads.
            while not cancelled() and not result.delivered.wait(.05):
                pass
