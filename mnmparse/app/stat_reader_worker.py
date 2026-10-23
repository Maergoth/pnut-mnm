"""Independent mini-stat OCR with one replaceable pending frame and result.

The capture thread owns submission. This worker never touches Qt or chat OCR;
results go to a bounded polling slot and an optional guarded consumer callback.
Its recognizer is created, used and closed on its own thread; a slow recognizer
cannot build a queue of increasingly old captures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import math
import threading
import time
from typing import Callable

import numpy as np

from mnmparse.config import Config
from mnmparse.ocr import OcrEngine, make_engine
from mnmparse.player_status import PlayerStatusReading, read_status_crop

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StatReadRequest:
    """Owned pixels plus the capture context the consumer must revalidate.

    ``context`` should be an immutable engine epoch/source/settings token. The
    wall-clock ``captured_at`` is preserved even if OCR completes much later.
    """

    crop: np.ndarray = field(repr=False, compare=False)
    captured_at: float
    context: object
    ocr_engine: str = "windows"
    preprocess: str = "maxchannel"
    ocr_scale: float = 1.0

    def __post_init__(self) -> None:
        crop = np.array(self.crop, copy=True, order="C")
        crop.setflags(write=False)
        object.__setattr__(self, "crop", crop)


@dataclass(frozen=True, slots=True)
class StatReadResult:
    """Bounded output with original evidence time, never the completion time."""

    captured_at: float
    context: object
    shape: tuple[int, ...]
    reading: PlayerStatusReading | None
    ocr_ms: float
    error: str | None = None


class StatReaderWorker:
    """Lazy, single-owner recognizer with latest-only input and output slots.

    A request currently inside OCR cannot be interrupted safely. ``close``
    cancels both slots immediately and suppresses its late completion; the
    daemon closes its own recognizer when that call returns. Repeated close
    calls can wait again, but a closed worker can never be restarted. An
    optional request provider samples capture on this thread, independently
    of chat's OCR loop. It must return quickly and never perform GUI work.
    """

    def __init__(self, *, factory: Callable[[Config], OcrEngine] = make_engine,
                 read: Callable = read_status_crop,
                 on_result: Callable[[StatReadResult], None] | None = None,
                 request_provider: Callable[[], StatReadRequest | None] | None = None,
                 interval_provider: Callable[[], float] | None = None) -> None:
        self._factory = factory
        self._read = read
        self._on_result = on_result
        self._request_provider = request_provider
        self._interval_provider = interval_provider
        self._condition = threading.Condition()
        self._pending: StatReadRequest | None = None
        self._completed: StatReadResult | None = None
        self._closed = False
        self._thread: threading.Thread | None = None

    def _start_locked(self) -> None:
        if self._thread is None:
            thread = threading.Thread(target=self._run, name="mini-stat-ocr", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._pending = None
                raise

    def start(self) -> bool:
        """Start autonomous sampling without waiting for a chat submission."""
        with self._condition:
            if self._closed:
                return False
            self._start_locked()
            return True

    def submit(self, request: StatReadRequest) -> bool:
        """Replace the waiting frame without waiting for an active OCR call."""
        with self._condition:
            if self._closed:
                return False
            self._pending = request
            self._start_locked()
            self._condition.notify()
            return True

    def poll(self) -> StatReadResult | None:
        """Take the latest completion without waiting, or return ``None``."""
        with self._condition:
            result, self._completed = self._completed, None
            return result

    def clear_pending(self) -> None:
        """Drop waiting/output slots; in-flight context still needs validation."""
        with self._condition:
            self._pending = None
            self._completed = None

    def close(self, timeout: float = 0.0) -> bool:
        """Cancel immediately; optionally wait at most ``timeout`` seconds."""
        with self._condition:
            self._closed = True
            self._pending = None
            self._completed = None
            self._condition.notify_all()
        return self.wait_stopped(timeout)

    @property
    def is_running(self) -> bool:
        with self._condition:
            thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def wait_stopped(self, timeout: float = 0.0) -> bool:
        """Bounded lifecycle check, including a recognizer still in a read."""
        with self._condition:
            thread = self._thread
        if thread is None:
            return True
        if thread is not threading.current_thread():
            thread.join(max(0.0, timeout))
        return not thread.is_alive()

    @staticmethod
    def _close_reader(reader: OcrEngine | None) -> None:
        close = getattr(reader, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                # A broken optional recognizer must not prevent shutdown or a
                # fresh instance on the next request.
                pass

    def _run(self) -> None:
        reader: OcrEngine | None = None
        reader_key: tuple[str, float] | None = None
        next_poll = 0.0
        try:
            while True:
                with self._condition:
                    if self._closed:
                        return
                    if self._pending is not None:
                        request, self._pending = self._pending, None
                    else:
                        delay = next_poll - time.monotonic()
                        if self._request_provider is None or delay > 0:
                            self._condition.wait(None if self._request_provider is None else delay)
                            continue
                        request = None
                if request is None:
                    try:
                        request = self._request_provider()
                    except Exception:
                        log.exception("Could not sample the mini-stat capture")
                    try:
                        interval = .25 if self._interval_provider is None else float(self._interval_provider())
                    except Exception:
                        interval = .25
                    if not math.isfinite(interval):
                        interval = .25
                    next_poll = time.monotonic() + max(.01, interval)
                    with self._condition:
                        if self._closed:
                            return
                        # Explicit submissions supersede a provider snapshot
                        # taken while another thread published newer pixels.
                        if self._pending is not None:
                            request, self._pending = self._pending, None
                    if request is None:
                        continue
                started = time.perf_counter()
                reading, error = None, None
                try:
                    key = (request.ocr_engine, float(request.ocr_scale))
                    if reader is None or reader_key != key:
                        self._close_reader(reader)
                        reader, reader_key = None, None
                        reader = self._factory(Config(ocr_engine=key[0], ocr_scale=key[1]))
                        reader_key = key
                    reading, _lines = self._read(reader, request.crop, request.preprocess, request.ocr_scale)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"[:512]
                    self._close_reader(reader)
                    reader, reader_key = None, None
                result = StatReadResult(
                    captured_at=request.captured_at, context=request.context,
                    shape=tuple(request.crop.shape), reading=reading,
                    ocr_ms=(time.perf_counter() - started) * 1000.0, error=error,
                )
                # Release the pixels before sleeping; only the pending frame
                # and current read retain images, never completed results.
                del request
                with self._condition:
                    deliver = not self._closed
                    if deliver:
                        self._completed = result
                if deliver and self._on_result is not None:
                    try:
                        # The engine callback validates the original context
                        # and emits signals; there are no GUI calls here.
                        self._on_result(result)
                    except Exception:
                        # One consumer failure must not kill the OCR owner or
                        # strand a live worker that can no longer read frames.
                        pass
        finally:
            self._close_reader(reader)
