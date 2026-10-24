"""Small HTTP adapter for a fixed GitHub JSONL file; no desktop dependencies."""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import sys
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mnmparse.leaderboard import MAX_BYTES, dump_records, parse_records, parse_stamp, validate_snapshot


REPOSITORY = "Maergoth/pnut-mnm"
BRANCH = "leaderboard-data"
FILE_PATH = "leaderboard.jsonl"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}"
CONTENTS_URL = f"{API_ROOT}/contents/{FILE_PATH}"
MAX_REQUEST_BYTES = 4096
MAX_API_BYTES = ((MAX_BYTES + 2) // 3) * 4 + 65_536
MAX_CLOCK_SKEW_SECONDS = 300
CUMULATIVE_FIELDS = ("personal_kills", "personal_deaths", "high_hit", "coin_looted")


class StorageError(Exception):
    """The public file could not be safely read or updated."""


class _GitHubError(StorageError):
    def __init__(self, status: int = 0):
        self.status = status
        super().__init__("GitHub storage is unavailable")


class _NoRedirects(HTTPRedirectHandler):
    # A token must never follow a redirect to another endpoint or host.
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    bind: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        token = env.get("GITHUB_TOKEN", "").strip()
        if not token:
            raise ValueError("GITHUB_TOKEN must be supplied on the server")
        try:
            port = int(env.get("PNUT_LEADERBOARD_PORT", "8765"))
        except ValueError:
            raise ValueError("PNUT_LEADERBOARD_PORT must be an integer") from None
        if not 1 <= port <= 65_535:
            raise ValueError("PNUT_LEADERBOARD_PORT is out of range")
        bind = env.get("PNUT_LEADERBOARD_BIND", "127.0.0.1").strip()
        if not bind:
            raise ValueError("PNUT_LEADERBOARD_BIND cannot be empty")
        return cls(token=token, bind=bind, port=port)


class GitHubStore:
    """Optimistically append to one server-selected file, retrying concurrent writes."""

    def __init__(self, token: str, *, opener=None, timeout: float = 10.0, attempts: int = 3):
        if not token or "\r" in token or "\n" in token:
            raise ValueError("A server-side GitHub token is required")
        if not 1 <= attempts <= 5 or not 0 < timeout <= 30:
            raise ValueError("Invalid GitHub request bounds")
        self._token = token
        self._opener = opener or build_opener(_NoRedirects()).open
        self._timeout = timeout
        self._attempts = attempts

    def _request(self, method: str, url: str, payload: dict | None = None, *,
                 accept: str = "application/vnd.github+json") -> dict:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(url, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token,
            "Accept": accept,
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "PNUT-Leaderboard-Service",
        })
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(MAX_API_BYTES + 1)
                if len(raw) > MAX_API_BYTES:
                    raise StorageError("GitHub response is too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise StorageError("Invalid GitHub response")
                return result
        except HTTPError as error:
            status = error.code
            error.close()
            raise _GitHubError(status) from None
        except (URLError, TimeoutError, OSError, HTTPException, UnicodeError, ValueError, RecursionError):
            raise StorageError("GitHub storage is unavailable") from None

    @staticmethod
    def _decode_blob(result: dict, sha: str) -> bytes:
        if result.get("encoding") != "base64" or not isinstance(result.get("content"), str):
            raise StorageError("Invalid GitHub file encoding")
        try:
            data = base64.b64decode("".join(result["content"].split()), validate=True)
        except (ValueError, binascii.Error):
            raise StorageError("Invalid GitHub file content") from None
        if len(data) > MAX_BYTES:
            raise StorageError("Leaderboard file is too large")
        digest = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
        if digest != sha:
            raise StorageError("GitHub file checksum does not match")
        return data

    def _read(self) -> tuple[list[dict[str, Any]], str | None]:
        try:
            result = self._request("GET", CONTENTS_URL + "?" + urlencode({"ref": BRANCH}),
                                   accept="application/vnd.github.object+json")
        except _GitHubError as error:
            if error.status != 404:
                raise
            # A missing file is allowed, a missing branch/repository is not. The
            # service never creates branches and every PUT names the fixed branch.
            self._request("GET", API_ROOT + "/branches/" + BRANCH)
            return [], None
        sha, size = result.get("sha"), result.get("size")
        if (result.get("type") != "file" or not isinstance(sha, str)
                or not re.fullmatch(r"[0-9a-f]{40}", sha)
                or type(size) is not int or not 0 <= size <= MAX_BYTES):
            raise StorageError("Invalid GitHub file metadata")
        if result.get("encoding") == "none":
            # Contents omits data above 1 MiB. Read the immutable blob by its SHA,
            # rather than re-reading a branch that could change between requests.
            result = self._request("GET", API_ROOT + "/git/blobs/" + sha)
        raw = self._decode_blob(result, sha)
        if len(raw) != size:
            raise StorageError("GitHub file size does not match")
        try:
            return parse_records(raw.decode("utf-8"), strict=True), sha
        except (UnicodeError, ValueError):
            raise StorageError("Existing leaderboard data is invalid") from None

    def append(self, record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        record = validate_snapshot(record)
        for attempt in range(self._attempts):
            records, sha = self._read()
            for existing in records:
                if existing["submission_id"] == record["submission_id"]:
                    return existing, False
            prior = [existing for existing in records if existing["public_key"] == record["public_key"]]
            if any(record["sequence"] <= existing["sequence"] for existing in prior):
                raise ValueError("New daily snapshots require an increasing sequence")
            if any(record[field] < existing[field] for existing in prior for field in CUMULATIVE_FIELDS):
                raise ValueError("Cumulative snapshot values cannot decrease")
            try:
                # Only this identity's rollback floors were checked above.
                # Preserve unrelated history until its owner next updates it.
                content = dump_records(
                    [existing for existing in records if existing["public_key"] != record["public_key"]] + [record]
                ).encode("utf-8")
            except ValueError:
                raise StorageError("Leaderboard capacity has been reached") from None
            payload = {
                "branch": BRANCH,
                "message": "Update anonymous leaderboard snapshot",
                "content": base64.b64encode(content).decode("ascii"),
            }
            if sha is not None:
                payload["sha"] = sha
            try:
                result = self._request("PUT", CONTENTS_URL, payload)
                content_sha = hashlib.sha1(b"blob " + str(len(content)).encode("ascii") + b"\0" + content).hexdigest()
                if not isinstance(result.get("content"), dict) or result["content"].get("sha") != content_sha:
                    raise StorageError("GitHub did not confirm the saved file")
                return record, True
            except _GitHubError as error:
                # 422 also represents a create-file race (another writer created
                # the initially absent file). Other validation errors are final.
                conflict = error.status == 409 or (error.status == 422 and sha is None)
                if not conflict or attempt + 1 == self._attempts:
                    raise
        raise StorageError("GitHub storage is unavailable")


class SnapshotService:
    def __init__(self, store: GitHubStore, *, clock: Callable[[], datetime] | None = None):
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(self, value: Any) -> tuple[dict[str, Any], bool]:
        record = validate_snapshot(value)
        stamp = parse_stamp(record["captured_at"])
        now = self._clock().astimezone(timezone.utc)
        if stamp.date() != now.date() or abs((stamp - now).total_seconds()) > MAX_CLOCK_SKEW_SECONDS:
            raise ValueError("Snapshot time must be within five minutes of the current UTC day")
        return self._store.append(record)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


class _Handler(BaseHTTPRequestHandler):
    server_version = "PNUT-Leaderboard"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10.0)

    def log_message(self, format, *args):
        # Neither payloads, IP addresses nor the write token belong in access logs.
        pass

    def _send(self, status: int, value: dict):
        body = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        if self.path != "/snapshots":
            self._send(404, {"error": "Not found"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self._send(400, {"error": "A single Content-Length is required"})
            return
        try:
            if not re.fullmatch(r"[0-9]+", lengths[0]):
                raise ValueError
            length = int(lengths[0])
        except ValueError:
            self._send(400, {"error": "Invalid Content-Length"})
            return
        if not 0 < length <= MAX_REQUEST_BYTES:
            self._send(413, {"error": "Snapshot body is too large or empty"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Content-Type must be application/json"})
            return
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request")
            record = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
            result, created = self.server.service.submit(record)
        except (ValueError, TypeError, KeyError, RecursionError):
            self._send(400, {"error": "Invalid snapshot"})
            return
        except (StorageError, TimeoutError, OSError):
            self._send(503, {"error": "Leaderboard storage is temporarily unavailable"})
            return
        self._send(201 if created else 200, {"snapshot": result, "created": created})


class LeaderboardHTTPServer(HTTPServer):
    """Serial requests keep memory and write concurrency bounded per process."""
    allow_reuse_address = True

    def __init__(self, address, service: SnapshotService):
        self.service = service
        super().__init__(address, _Handler)

    def handle_error(self, request, client_address):
        # HTTPServer's default traceback includes the client address. Retain a
        # process-level failure indicator without storing request/private data.
        print("PNUT leaderboard request failed", file=sys.stderr, flush=True)


def main():
    try:
        settings = Settings.from_env()
        store = GitHubStore(settings.token)
        with LeaderboardHTTPServer((settings.bind, settings.port), SnapshotService(store)) as server:
            print(f"PNUT leaderboard draft service listening on {settings.bind}:{settings.port}", flush=True)
            try:
                server.serve_forever(poll_interval=0.5)
            except KeyboardInterrupt:
                pass
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from None
