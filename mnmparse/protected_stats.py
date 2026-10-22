"""Windows-user protected leaderboard ledger, separate from editable session history.

Only the capture engine feeds this ledger. Numeric values and identity key material
are serialized only inside a DPAPI envelope; upload/copy decrypt in memory.
"""
from __future__ import annotations

from collections import OrderedDict
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from typing import Any

from mnmparse.grammar import is_you
from mnmparse.leaderboard import (COUNTERS, MAX_NUMBER, MAX_STAT, RECORD_FIELDS, alias_for_key, make_snapshot,
                                 parse_stamp, phrase_key, public_key, reading_stats)

MAGIC = b"PNUT-DPAPI-v1\x00"
MAX_LOCAL_BYTES = 10 * 1024 * 1024


class ProtectionError(ValueError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsProtector:
    """DPAPI user scope includes authenticated encryption managed by Windows."""
    def _crypt(self, data: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise ProtectionError("Leaderboard history is available on Windows")
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        buf = ctypes.create_string_buffer(data)
        source = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
        entropy_data = b"PNUT anonymous leaderboard v1"
        entropy_buf = ctypes.create_string_buffer(entropy_data)
        entropy = _Blob(len(entropy_data), ctypes.cast(entropy_buf, ctypes.POINTER(ctypes.c_ubyte)))
        output = _Blob()
        if decrypt:
            operation = crypt.CryptUnprotectData
            operation.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob),
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
            ok = operation(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output))
        else:
            operation = crypt.CryptProtectData
            operation.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob),
                                  ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
            ok = operation(ctypes.byref(source), "PNUT leaderboard", ctypes.byref(entropy),
                           None, None, 1, ctypes.byref(output))
        operation.restype = wintypes.BOOL
        if not ok:
            raise ProtectionError("Windows could not open leaderboard history")
        try:
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            kernel.LocalFree(output.pbData)

    def encrypt(self, data: bytes) -> bytes:
        return MAGIC + self._crypt(data, decrypt=False)

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(MAGIC):
            raise ProtectionError("Leaderboard history is unavailable")
        return self._crypt(data[len(MAGIC):], decrypt=True)


class EncryptedFile:
    """Atomic writes never create a plaintext temporary file or fallback."""
    def __init__(self, path: Path, protector: Any = None) -> None:
        self.path = Path(path)
        self.protector = protector or WindowsProtector()

    def load(self) -> Any:
        if self.path.stat().st_size > MAX_LOCAL_BYTES:
            raise ProtectionError("Leaderboard history is too large")
        try:
            return json.loads(self.protector.decrypt(self.path.read_bytes()).decode("utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ProtectionError("Leaderboard history could not be opened for this Windows user") from exc

    def save(self, value: Any) -> None:
        encrypted = self.protector.encrypt(json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
        if len(encrypted) > MAX_LOCAL_BYTES:
            raise ProtectionError("Leaderboard history is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encrypted)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


class ProtectedLedger:
    def __init__(self, path: Path, *, protector: Any = None) -> None:
        self.file = EncryptedFile(path, protector)
        self._lock = threading.RLock()
        self.error = ""
        self._seen: OrderedDict[int, Any] = OrderedDict()
        self._kill_pairs: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        self._death_at = float("-inf")
        self._stats_live_at = float("-inf")
        self._backend_eligible = False
        try:
            if not self.file.path.exists():
                self.file.save({"version": 1, "key": secrets.token_bytes(32).hex(),
                                "counters": dict.fromkeys(COUNTERS, 0), "sequence": 0,
                                "stats": None, "submitted_day": "", "history_restored": False,
                                "history_floor": 0, "prepared_signatures": [], "records": {}})
            self._load()
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def _load(self) -> dict[str, Any]:
        value = self.file.load()
        try:
            if (not isinstance(value, dict) or type(value["version"]) is not int or value["version"] != 1
                    or not isinstance(value["key"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["key"])
                    or not isinstance(value["counters"], dict) or set(value["counters"]) != set(COUNTERS)
                    or not all(type(v) is int and 0 <= v <= MAX_NUMBER for v in value["counters"].values())
                    or type(value["sequence"]) is not int or not 0 <= value["sequence"] <= MAX_NUMBER
                    or not isinstance(value["submitted_day"], str)):
                raise ValueError
            # Older local ledgers acquire these fields on their next encrypted write.
            value.setdefault("history_restored", False)
            value.setdefault("history_floor", 0)
            value.setdefault("prepared_signatures", [])
            value.setdefault("records", {})
            if (type(value["history_restored"]) is not bool
                    or type(value["history_floor"]) is not int or not 0 <= value["history_floor"] <= MAX_NUMBER
                    or not isinstance(value["prepared_signatures"], list) or len(value["prepared_signatures"]) > 64
                    or not all(isinstance(sig, str) and re.fullmatch(r"[0-9a-f]{128}", sig)
                               for sig in value["prepared_signatures"])):
                raise ValueError
            if not isinstance(value["records"], dict) or not set(value["records"]) <= set(RECORD_FIELDS):
                raise ValueError
            for field, record in value["records"].items():
                limit = MAX_STAT * (7 if field == "stat_total" else 8 if field == "resist_total" else 1)
                minimum = 1 if field in {"max_health", "max_mana"} else -limit
                if (not isinstance(record, dict) or set(record) != {"value", "captured_at"}
                        or type(record["value"]) is not int or not minimum <= record["value"] <= limit):
                    raise ValueError
                parse_stamp(record["captured_at"])
        except (KeyError, TypeError, ValueError):
            raise ProtectionError("Leaderboard history is unavailable") from None
        return value

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self.error:
                return {"error": self.error, "eligible": False}
            try:
                value = self._load()
                return {"alias": alias_for_key(public_key(bytes.fromhex(value["key"]))),
                        **value["counters"], "submitted_day": value["submitted_day"],
                        "records": {field: dict(record) for field, record in value["records"].items()},
                        "eligible": self._backend_eligible and time.monotonic() - self._stats_live_at <= 10,
                        "error": ""}
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                return {"error": self.error, "eligible": False}

    def set_capture_eligible(self, eligible: bool) -> None:
        with self._lock:
            self._backend_eligible = bool(eligible)
            if not eligible:
                self._stats_live_at = float("-inf")

    def set_phrase(self, phrase: str) -> None:
        key = phrase_key(phrase)
        with self._lock:
            self._check()
            value = self._load()
            if value["key"] == key.hex():
                return
            value.update(key=key.hex(), counters=dict.fromkeys(COUNTERS, 0), sequence=0,
                         submitted_day="", stats=None, history_restored=False, history_floor=0,
                         prepared_signatures=[], records={})
            self.file.save(value)
            self._stats_live_at = float("-inf")
            self._seen.clear()
            self._kill_pairs.clear()
            self._death_at = float("-inf")

    def _check(self) -> None:
        if self.error:
            raise ProtectionError(self.error)

    def record_stats(self, stats: dict[str, Any]) -> None:
        with self._lock:
            if not self._backend_eligible or stats.get("capture_backend") != "window" or self.error:
                return
            maximum, mana = stats.get("max_health"), stats.get("max_mana")
            if type(maximum) is not int or not 0 < maximum <= 1_000_000_000:
                return
            if mana is not None and (type(mana) is not int or not 0 < mana <= 1_000_000_000):
                return
            parse_stamp(stats["captured_at"])
            value = self._load()
            value["stats"] = {k: stats[k] for k in ("max_health", "max_mana", "captured_at", "capture_backend")}
            value["stats"].update(reading_stats(stats))
            for field in RECORD_FIELDS:
                number = value["stats"][field]
                previous = value["records"].get(field)
                if number is not None and (previous is None or number > previous["value"]):
                    value["records"][field] = {"value": number, "captured_at": stats["captured_at"]}
            value["sequence"] = min(MAX_NUMBER, value["sequence"] + 1)
            self.file.save(value)
            self._stats_live_at = time.monotonic()

    def record_event(self, event: Any, player_name: str = "") -> None:
        """Fresh, exact Window Capture events only; never session totals or imports."""
        with self._lock:
            if (not self._backend_eligible or self.error or event.estimated or event.estimated_ts
                    or id(event) in self._seen):
                return
            self._seen[id(event)] = event
            while len(self._seen) > 512:
                self._seen.popitem(last=False)
            changes = {}
            own = bool(is_you(event.raw_actor or "") or player_name
                       and (event.actor or "").casefold() == player_name.casefold()) and not event.is_pet
            target_you = bool(re.match(r"^You (?:have|has) been slain by\b", event.text, re.I)
                              or player_name and (event.target or "").casefold() == player_name.casefold())
            pet_death = bool(event.is_pet or re.match(r"^Your\s+pet\b", event.text, re.I))
            if own and event.kind in ("melee_hit", "ability_hit") and type(event.amount) is int:
                changes["high_hit"] = event.amount
            elif event.kind == "kill":
                if not pet_death and (target_you or re.search(r"\bslain (?:you|YOU)[.!]?\s*$", event.text)):
                    if event.ts - self._death_at > 5:
                        changes["personal_deaths"] = 1
                        self._death_at = event.ts
                elif own and not pet_death:
                    key = ((event.actor or "").casefold(), (event.target or "").casefold())
                    form = "passive" if "been slain by" in event.text.lower() else "active"
                    previous = self._kill_pairs.get(key)
                    if previous and previous[1] != form and 0 <= event.ts - previous[0] <= 2:
                        self._kill_pairs.pop(key)
                    else:
                        changes["personal_kills"] = 1
                        self._kill_pairs[key] = (event.ts, form)
                        while len(self._kill_pairs) > 512:
                            self._kill_pairs.popitem(last=False)
            elif own and event.kind == "coin" and type(event.copper) is int:
                # Gross coins personally looted from corpses, before party splitting.
                changes["coin_looted"] = event.copper
            if not changes:
                return
            value = self._load()
            for field, amount in changes.items():
                old = value["counters"][field]
                value["counters"][field] = min(MAX_NUMBER, max(old, amount) if field == "high_hit" else old + max(0, amount))
            value["sequence"] = min(MAX_NUMBER, value["sequence"] + 1)
            self.file.save(value)

    def restore_history(self, records: Any) -> bool:
        """Restore one signed public baseline, retaining fresh local capture totals.

        Editable session history is never accepted here. Exact snapshots prepared
        by this ledger are already included locally, including timeout/restart
        retries, so discovering those publicly must not add their totals again.
        """
        from mnmparse.leaderboard import validate_snapshot
        with self._lock:
            self._check()
            value = self._load()
            if value["history_restored"]:
                return False
            own_key = public_key(bytes.fromhex(value["key"])).hex()
            matching = []
            for record in records:
                # Most public records belong to other users. Only the current
                # key's few records require signature verification on this path.
                if not isinstance(record, dict) or record.get("public_key") != own_key:
                    continue
                try:
                    matching.append(validate_snapshot(record))
                except (ValueError, TypeError, KeyError):
                    continue
            if not matching:
                return False
            baseline = {field: max(record[field] for record in matching) for field in COUNTERS}
            floor = max(record["sequence"] for record in matching)
            prepared = set(value["prepared_signatures"])
            already_accounted = any(record["signature"] in prepared for record in matching)
            if not already_accounted:
                for field in COUNTERS:
                    local = value["counters"][field]
                    value["counters"][field] = (max(local, baseline[field]) if field == "high_hit"
                                                 else min(MAX_NUMBER, local + baseline[field]))
                value["sequence"] = min(MAX_NUMBER, floor + value["sequence"] + 1)
            else:
                value["sequence"] = max(value["sequence"], floor)
            value["history_restored"] = True
            value["history_floor"] = floor
            value["submitted_day"] = max(value["submitted_day"],
                                         max(parse_stamp(record["captured_at"]).date().isoformat()
                                             for record in matching))
            self.file.save(value)
            return True

    def prepare_submission(self, message: str = "") -> dict[str, Any]:
        with self._lock:
            self._check()
            if not self._backend_eligible or time.monotonic() - self._stats_live_at > 10:
                raise ProtectionError("Read current mini stats using Window Capture before submitting")
            value = self._load()  # Decrypt and validate the protected source immediately before signing.
            if value["stats"] is None:
                raise ProtectionError("Read current mini stats using Window Capture before submitting")
            record = make_snapshot(value["stats"], value["counters"], bytes.fromhex(value["key"]), value["sequence"],
                                   message=message)
            signatures = value["prepared_signatures"]
            if record["signature"] not in signatures:
                value["prepared_signatures"] = (signatures + [record["signature"]])[-64:]
                self.file.save(value)
            return record

    def mark_submitted(self, record: dict[str, Any]) -> None:
        from mnmparse.leaderboard import validate_snapshot
        record = validate_snapshot(record)
        with self._lock:
            self._check()
            value = self._load()
            if record["public_key"] != public_key(bytes.fromhex(value["key"])).hex():
                return
            if not value["history_restored"]:
                self.restore_history([record])
                value = self._load()
            value["submitted_day"] = max(value["submitted_day"], parse_stamp(record["captured_at"]).date().isoformat())
            value["history_restored"] = True
            value["history_floor"] = max(value["history_floor"], record["sequence"])
            self.file.save(value)
