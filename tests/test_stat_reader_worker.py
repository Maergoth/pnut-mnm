"""Bounded concurrency and lifecycle checks without real OCR or game capture."""

from dataclasses import FrozenInstanceError
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np

from mnmparse.app.stat_reader_worker import StatReadRequest, StatReaderWorker
from mnmparse.player_status import PlayerStatusReading


def request(value=1, *, context=(1, "source", "settings"), **kwargs):
    return StatReadRequest(np.full((3, 4, 3), value, np.uint8), 123.5 + value, context, **kwargs)


def wait_result(worker):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        result = worker.poll()
        if result is not None:
            return result
        time.sleep(.001)
    raise AssertionError("stat worker did not complete")


class StatReaderWorkerTests(unittest.TestCase):
    def test_request_owns_immutable_pixels_and_preserves_context(self):
        pixels = np.zeros((3, 4, 3), np.uint8)
        token = (9, object(), ("windows", .8))
        req = StatReadRequest(pixels, 55, token)
        pixels[:] = 99
        assert req.crop.max() == 0
        assert not req.crop.flags.writeable
        assert req.context is token
        with self.assertRaises(ValueError):
            req.crop[:] = 1
        with self.assertRaises(FrozenInstanceError):
            req.captured_at = 60


    def test_slow_read_never_blocks_submission_and_keeps_only_latest_waiting_frame(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def read(reader, crop, mode, scale):
            value = int(crop[0, 0, 0])
            seen.append(value)
            if value == 1:
                entered.set()
                assert release.wait(3)
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: object(), read=read)
        try:
            assert worker.submit(request(1))
            assert entered.wait(3)
            start = time.monotonic()
            for value in range(2, 200):
                assert worker.submit(request(value))
            assert time.monotonic() - start < .5
            release.set()
            result = wait_result(worker)
            if result.captured_at != 322.5:
                result = wait_result(worker)
            assert result.captured_at == 322.5
            assert seen == [1, 199]
            assert worker.poll() is None
        finally:
            release.set()
            assert worker.close(3)


    def test_creation_read_and_close_share_dedicated_owner_thread(self):
        calls = []
        expected = PlayerStatusReading(10, 20, (0, 0, 4, 3), ())

        class Reader:
            def close(self):
                calls.append(("close", threading.get_ident()))

        def factory(cfg):
            calls.append(("create", threading.get_ident()))
            assert (cfg.ocr_engine, cfg.ocr_scale) == ("rapid", 2)
            return Reader()

        def read(reader, crop, mode, scale):
            calls.append(("read", threading.get_ident()))
            assert (mode, scale) == ("none", 2)
            return expected, []

        worker = StatReaderWorker(factory=factory, read=read)
        assert calls == []  # Lazy: no recognizer on the capture/GUI thread.
        req = request(ocr_engine="rapid", preprocess="none", ocr_scale=2)
        try:
            worker.submit(req)
            result = wait_result(worker)
            assert result.context is req.context
            assert result.captured_at == req.captured_at
            assert result.shape == (3, 4, 3)
            assert result.reading is expected
            assert result.error is None
            assert result.ocr_ms >= 0
        finally:
            assert worker.close(3)
        assert [name for name, _ in calls] == ["create", "read", "close"]
        assert len({owner for _, owner in calls}) == 1
        assert calls[0][1] != threading.get_ident()


    def test_completed_output_is_latest_only(self):
        completed = threading.Semaphore(0)

        def read(*args):
            completed.release()
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: object(), read=read)
        try:
            for value in range(1, 4):
                worker.submit(request(value))
                assert completed.acquire(timeout=3)
            # A barrier request and its completion ensure all earlier output slots
            # have been superseded, even if the semaphore ran before publication.
            worker.submit(request(4))
            assert completed.acquire(timeout=3)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                result = worker.poll()
                if result is not None and result.captured_at == 127.5:
                    break
                time.sleep(.001)
            else:
                self.fail("last completion missing")
            assert worker.poll() is None
        finally:
            assert worker.close(3)


    def test_close_cancels_pending_and_hung_result_without_waiting(self):
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        seen = []

        class Reader:
            def close(self):
                closed.set()

        def read(reader, crop, *args):
            seen.append(int(crop[0, 0, 0]))
            entered.set()
            assert release.wait(3)
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: Reader(), read=read)
        try:
            worker.submit(request(1))
            assert entered.wait(3)
            worker.submit(request(2))
            start = time.monotonic()
            assert not worker.close()
            assert time.monotonic() - start < .5
            assert not worker.submit(request(3))
            release.set()
            assert closed.wait(3)
            assert worker.close(3)
            assert seen == [1]
            assert worker.poll() is None
        finally:
            release.set()
            worker.close(3)


    def test_startup_failure_retries_on_next_capture(self):
        attempts = []

        def factory(cfg):
            attempts.append(cfg.ocr_engine)
            if len(attempts) == 1:
                raise RuntimeError("unavailable")
            return object()

        worker = StatReaderWorker(factory=factory, read=lambda *args: (None, []))
        try:
            worker.submit(request())
            assert wait_result(worker).error == "RuntimeError: unavailable"
            worker.submit(request())
            assert wait_result(worker).error is None
            assert attempts == ["windows", "windows"]
        finally:
            assert worker.close(3)


    def test_failed_read_and_failed_close_do_not_prevent_fresh_reader(self):
        created, closes = [], []

        class Reader:
            def close(self):
                closes.append(self)
                raise RuntimeError("close failed")

        def factory(cfg):
            reader = Reader()
            created.append(reader)
            return reader

        def read(reader, *args):
            if len(created) == 1:
                raise RuntimeError("recognizer failed")
            return None, []

        worker = StatReaderWorker(factory=factory, read=read)
        try:
            worker.submit(request())
            assert wait_result(worker).error == "RuntimeError: recognizer failed"
            worker.submit(request())
            assert wait_result(worker).error is None
            assert len(created) == 2
        finally:
            assert worker.close(3)
        assert closes == created


    def test_setting_change_replaces_recognizer_on_owner_thread(self):
        settings, closed = [], []

        class Reader:
            def close(self):
                closed.append(threading.get_ident())

        def factory(cfg):
            settings.append((cfg.ocr_engine, cfg.ocr_scale, threading.get_ident()))
            return Reader()

        worker = StatReaderWorker(factory=factory, read=lambda *args: (None, []))
        try:
            for kwargs in ({}, {"preprocess": "gray"}, {"ocr_scale": 2}):
                worker.submit(request(**kwargs))
                assert wait_result(worker).error is None
        finally:
            assert worker.close(3)
        assert [item[:2] for item in settings] == [("windows", 1), ("windows", 2)]
        assert closed == [settings[0][2], settings[0][2]]


    def test_clear_pending_discards_waiting_and_completed_slots(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def read(reader, crop, *args):
            calls.append(int(crop[0, 0, 0]))
            entered.set()
            assert release.wait(3)
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: object(), read=read)
        try:
            worker.submit(request(1))
            assert entered.wait(3)
            worker.submit(request(2))
            worker.clear_pending()
            release.set()
            result = wait_result(worker)
            # Clearing slots cannot cancel WinRT OCR already running. The original
            # context is retained so the engine can reject the old epoch safely.
            assert result.context == (1, "source", "settings")
            assert calls == [1]
            worker.clear_pending()
            assert worker.poll() is None
        finally:
            release.set()
            assert worker.close(3)


    def test_close_before_start_stays_closed_and_never_creates_reader(self):
        worker = StatReaderWorker(factory=lambda cfg: self.fail("created after close"))
        assert worker.close()
        assert worker.close()
        assert not worker.submit(request())
        assert worker.poll() is None

    def test_callback_runs_on_owner_thread_outside_lock_and_failure_is_contained(self):
        callbacks = threading.Semaphore(0)
        owners, polled = [], []

        def callback(result):
            owners.append(threading.get_ident())
            # Another thread must be able to poll while this callback remains
            # active. Joining here exposes an accidentally held worker lock.
            other = threading.Thread(target=lambda: polled.append(worker.poll()))
            other.start()
            other.join(1)
            assert not other.is_alive()
            callbacks.release()
            raise RuntimeError("consumer failed")

        worker = StatReaderWorker(factory=lambda cfg: object(),
                                  read=lambda *args: (None, []), on_result=callback)
        try:
            for value in (1, 2):
                worker.submit(request(value))
                assert callbacks.acquire(timeout=3)
            assert len(owners) == 2
            assert owners[0] == owners[1] != threading.get_ident()
            assert [value.captured_at for value in polled] == [124.5, 125.5]
            assert worker.is_running
            assert not worker.wait_stopped()
        finally:
            assert worker.close(3)
        assert not worker.is_running
        assert worker.wait_stopped()

    def test_thread_start_failure_can_retry_without_stranding_pending_frame(self):
        worker = StatReaderWorker(factory=lambda cfg: object(), read=lambda *args: (None, []))
        try:
            with patch("threading.Thread.start", side_effect=RuntimeError("no thread")):
                with self.assertRaisesRegex(RuntimeError, "no thread"):
                    worker.submit(request(1))
            assert not worker.is_running
            assert worker.poll() is None
            assert worker.submit(request(2))
            assert wait_result(worker).captured_at == 125.5
        finally:
            assert worker.close(3)

    def test_provider_samples_autonomously_without_chat_submissions(self):
        provided, read_on = [], []
        completed = threading.Semaphore(0)

        def provider():
            value = len(provided) + 1
            provided.append(threading.get_ident())
            return request(value)

        def read(reader, *args):
            read_on.append(threading.get_ident())
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: object(), read=read,
                                  request_provider=provider, interval_provider=lambda: .01,
                                  on_result=lambda result: completed.release())
        try:
            assert worker.start()
            assert worker.start()  # Idempotent, without a second owner.
            for _ in range(3):
                assert completed.acquire(timeout=3)
            assert len(read_on) >= 3
            assert len(set(read_on + provided)) == 1
            assert read_on[0] != threading.get_ident()
        finally:
            assert worker.close(3)
        assert worker.closed
        count = len(provided)
        assert not worker.start()
        time.sleep(.02)
        assert len(provided) == count

    def test_close_wakes_long_provider_interval_without_another_sample(self):
        calls = []
        completed = threading.Event()

        def provider():
            calls.append(1)
            return request()

        worker = StatReaderWorker(factory=lambda cfg: object(), read=lambda *args: (None, []),
                                  request_provider=provider, interval_provider=lambda: 60,
                                  on_result=lambda result: completed.set())
        try:
            worker.start()
            assert completed.wait(3)
            start = time.monotonic()
            assert worker.close(1)
            assert time.monotonic() - start < .5
            assert calls == [1]
            assert worker.closed
        finally:
            worker.close(3)

    def test_provider_runs_outside_lock_and_latest_submission_supersedes_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        seen = []

        def provider():
            entered.set()
            assert release.wait(3)
            return request(1)

        def read(reader, crop, *args):
            seen.append(int(crop[0, 0, 0]))
            return None, []

        worker = StatReaderWorker(factory=lambda cfg: object(), read=read,
                                  request_provider=provider, interval_provider=lambda: 60)
        try:
            worker.start()
            assert entered.wait(3)
            start = time.monotonic()
            for value in range(2, 200):
                assert worker.submit(request(value))
            assert time.monotonic() - start < .5
            release.set()
            assert wait_result(worker).captured_at == 322.5
            assert seen == [199]
        finally:
            release.set()
            assert worker.close(3)

    def test_close_during_provider_discards_unread_snapshot(self):
        entered, release = threading.Event(), threading.Event()
        worker = None

        def provider():
            entered.set()
            assert release.wait(3)
            return request()

        worker = StatReaderWorker(factory=lambda cfg: self.fail("created after cancellation"),
                                  request_provider=provider)
        try:
            worker.start()
            assert entered.wait(3)
            assert not worker.close()
            assert worker.closed and worker.is_running
            release.set()
            assert worker.wait_stopped(3)
            assert worker.poll() is None
        finally:
            release.set()
            worker.close(3)

    def test_provider_failure_retries_and_empty_frames_never_create_ocr(self):
        calls = []
        retry = threading.Event()

        def provider():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("capture temporarily unavailable")
            retry.set()
            return None

        worker = StatReaderWorker(factory=lambda cfg: self.fail("no pixels to read"),
                                  request_provider=provider, interval_provider=lambda: .01)
        try:
            with self.assertLogs("mnmparse.app.stat_reader_worker", level="ERROR") as messages:
                worker.start()
                assert retry.wait(3)
            assert "capture temporarily unavailable" in messages.output[0]
            assert worker.poll() is None
        finally:
            assert worker.close(3)
