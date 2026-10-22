"""Bounded public leaderboard reads and explicit, idempotent signed submissions."""
from __future__ import annotations

import json
import os
from queue import Empty, SimpleQueue
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PySide6.QtCore import QObject, QTimer, Signal

from mnmparse.app import APP_VERSION
from mnmparse.leaderboard import MAX_BYTES, READ_URL, dump_records, parse_records, validate_snapshot
from mnmparse.protected_stats import EncryptedFile


DEFAULT_SUBMIT_URL = "https://pnut-leaderboard-upload.maergoth.chatgpt.site/snapshots"


class LeaderboardError(ValueError):
    pass


def submission_url(value: str | None = None) -> str:
    value = os.environ.get("PNUT_LEADERBOARD_SUBMIT_URL", DEFAULT_SUBMIT_URL) if value is None else value
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (parsed.scheme not in ({"https", "http"} if local else {"https"}) or not parsed.hostname
                or parsed.username or parsed.password or parsed.fragment or parsed.query
                or parsed.path != "/snapshots" or parsed.port == 0):
            raise ValueError
    except (TypeError, ValueError):
        raise LeaderboardError("The leaderboard submission service address is invalid.") from None
    return value


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _fetch(url: str, data: bytes | None = None) -> bytes:
    request = Request(url, data=data, headers={"User-Agent": f"PNUT-MnM/{APP_VERSION}",
                      "Accept": "application/json", "Content-Type": "application/json"})
    limit = MAX_BYTES if data is None else 8192
    try:
        with build_opener(_NoRedirects()).open(request, timeout=40) as response:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise LeaderboardError("The leaderboard response is too large.")
            return raw
    except HTTPError as error:
        code = error.code
        error.close()
        if code == 400:
            raise LeaderboardError("The service rejected this snapshot. Read fresh mini stats before retrying.") from None
        raise LeaderboardError("The leaderboard service is unavailable. Cached entries remain available; try again later.") from None
    except (OSError, URLError, TimeoutError):
        raise LeaderboardError("The leaderboard could not be reached. Try again later.") from None


class _Worker:
    def __init__(self, file, operation, record, endpoint, fetch):
        self.file, self.operation, self.record, self.endpoint, self.fetch = file, operation, record, endpoint, fetch
        self.messages = SimpleQueue()
        self.cancelled = threading.Event()

    def post(self, kind, *args):
        if not self.cancelled.is_set():
            self.messages.put((kind, args))

    def run(self):
        try:
            try:
                state = self.file.load()
            except FileNotFoundError:
                state = {"records": [], "pending": None}
            if not isinstance(state, dict):
                raise LeaderboardError("Stats history is unavailable.")
            records = parse_records(dump_records(state.get("records", [])), strict=True)
            self.post("records", records, "Cached public entries")
            if self.cancelled.is_set():
                return
            if self.operation == "cache":
                self.post("done", "Cached entries shown. Refresh to check the public leaderboard.")
                return
            if self.operation == "refresh":
                records = parse_records(self.fetch(READ_URL).decode("utf-8"), strict=True)
                if self.cancelled.is_set():
                    return
                state["records"] = records
                self.file.save(state)
                self.post("records", records, "Public entries refreshed")
                self.post("done", "Leaderboard refreshed.")
                return
            record = validate_snapshot(self.record)
            # The anonymous identity/day ID is the idempotency key. Use fresh
            # reader evidence on retries so an ambiguous timeout cannot leave a
            # permanently expired five-minute payload; the server returns an
            # already accepted record for the same ID.
            state["pending"] = record
            self.file.save(state)
            if self.cancelled.is_set():
                return
            raw = self.fetch(self.endpoint, json.dumps(record, separators=(",", ":")).encode("ascii"))
            result = json.loads(raw)
            accepted = validate_snapshot(result["snapshot"])
            if (accepted["submission_id"] != record["submission_id"] or accepted["public_key"] != record["public_key"]):
                raise LeaderboardError("The service acknowledged a different snapshot; submission was not confirmed.")
            if self.cancelled.is_set():
                return
            records = [entry for entry in records if entry["submission_id"] != accepted["submission_id"]] + [accepted]
            state.update(records=records, pending=None)
            self.file.save(state)
            self.post("submitted", accepted)
            self.post("records", records, "Public submission confirmed")
            self.post("done", "Submitted. You can submit again on the next UTC day.")
        except Exception as error:
            message = str(error) if isinstance(error, LeaderboardError) else "Stats history or the leaderboard response is unavailable. Try again later."
            self.post("error", message)


class LeaderboardClient(QObject):
    records_updated = Signal(object, str)
    submitted = Signal(object)
    finished = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, cache: EncryptedFile, parent=None, *, endpoint=None, fetch=None):
        super().__init__(parent)
        self.cache = cache
        try:
            self.endpoint, self.configuration_error = submission_url(endpoint), ""
        except LeaderboardError as error:
            self.endpoint, self.configuration_error = "", str(error)
        self._fetch = fetch or _fetch
        self._worker = None
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(20)
        self._timer.timeout.connect(self._drain)

    @property
    def busy(self):
        return self._worker is not None

    @property
    def submission_unavailable(self):
        return self.configuration_error or ("Submission service is not configured yet." if not self.endpoint else "")

    def refresh(self):
        return self._start("refresh")

    def load_cached(self):
        return self._start("cache")

    def submit(self, record):
        if self.submission_unavailable:
            self.finished.emit(self.submission_unavailable)
            return False
        return self._start("submit", validate_snapshot(record))

    def _start(self, operation, record=None):
        if self._closed or self.busy:
            return False
        worker = _Worker(self.cache, operation, record, self.endpoint, self._fetch)
        self._worker = worker
        self._timer.start()
        self.busy_changed.emit(True)
        threading.Thread(target=worker.run, name="anonymous-leaderboard", daemon=True).start()
        return True

    def _drain(self):
        worker = self._worker
        if self._closed or worker is None:
            self._timer.stop()
            return
        while not self._closed and self._worker is worker:
            try:
                kind, args = worker.messages.get_nowait()
            except Empty:
                return
            if kind == "records":
                self.records_updated.emit(*args)
            elif kind == "submitted":
                self.submitted.emit(*args)
            elif kind in {"done", "error"}:
                self._timer.stop()
                self._worker = None
                self.busy_changed.emit(False)
                if not self._closed:
                    self.finished.emit(*args)
                return

    def shutdown(self):
        self._closed = True
        self._timer.stop()
        if self._worker:
            self._worker.cancelled.set()
            self._worker = None
