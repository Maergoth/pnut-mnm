"""One lossless timer definition in one compact, checksummed game-chat message.

Version 1 freezes the field order and defaults below. Its JSON is
[changed_field_bitmask, [changed_values_in_field_order]]. Defaults belong to
the protocol, not to the current Trigger class, so later app defaults cannot
change an imported setting. The payload is UTF-8, raw DEFLATE and lowercase hex.

The single frame is PNUT1 <payload> <CRC32> END (at most 255 characters).
Payload hex digits are grouped in sixes and the eight CRC digits in fours to
help OCR read the code. CRC32 covers the complete compressed payload. O/0, I/1,
L/1 aliases, letter case and OCR whitespace are tolerated; other damage fails
closed. Checksums detect corruption, not sender identity. A complete frame may
visually wrap across several OCR messages without becoming multiple chat sends.

The assembler never imports, stores, or plays timers. It emits one validated
ReceivedShare for the app to offer to the user, with bounded buffers and expiry.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, fields
import hashlib
import json
import re
import time
from typing import Callable
import zlib

from .trigger_exchange import TriggerExchangeError, validate_trigger
from .triggers import Trigger
from .privacy import safe_trigger_definition


MAX_CHAT_LINE = 255
_FRAME_OVERHEAD = 16  # PNUT1 + eight CRC hex digits + END, excluding OCR whitespace.
MAX_TOKEN_CHARS = MAX_CHAT_LINE - _FRAME_OVERHEAD
MAX_COMPRESSED_BYTES = MAX_TOKEN_CHARS // 2
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_PENDING = 12
MAX_SEEN = 256
FRAGMENT_TTL = 30.0
SEEN_TTL = 6 * 60 * 60.0
MAX_INPUT_CHARS = 16 * 1024
MAX_FRAGMENT_CHARS = 2048

# Frozen protocol schema: change the protocol version if changing either tuple.
FIELDS_V1 = (
    "name", "pattern", "mode", "fuzzy", "enabled", "action", "sound", "file",
    "speech", "volume", "cooldown_s", "timer", "timer_seconds", "timer_label",
    "timer_mode", "timer_color", "timer_warn_color", "timer_low_color", "timer_low_s",
    "timer_warn_s", "timer_warn_action", "timer_warn_sound", "timer_warn_speech",
    "timer_end_action", "timer_end_sound", "timer_end_speech", "category", "id",
)
DEFAULTS_V1 = (
    "New trigger", "", "contains", True, True, "sound", "Chime", "",
    "{match}", 80, 0.0, False, 30.0, "", "replace", "", "", "", 5.0,
    0.0, "sound", "Tick", "{label} soon", "sound", "Bell", "{label}", "", "",
)
_ALIASES = str.maketrans("OIL", "011")
_START = re.compile(r"(?<![a-z0-9])PNUT(?P<version>[0-9]{1,3}|[IL])(?=\s|$)", re.IGNORECASE)
# N is not a hex digit, so the end marker cannot be mistaken for payload or CRC.
_END = re.compile(r"E\s*N\s*D\b", re.IGNORECASE)
_TOKEN = re.compile(r"[0-9A-F]+\Z")


@dataclass(frozen=True)
class ReceivedShare:
    trigger: Trigger
    sender: str
    share_id: str


@dataclass
class _Fragment:
    text: str
    sender: str
    created: float


def _compress(raw: bytes) -> bytes:
    compressor = zlib.compressobj(9, wbits=-15)
    return compressor.compress(raw) + compressor.flush()


def _normal_token(value: str) -> str:
    result = value.upper().translate(_ALIASES)
    if not _TOKEN.fullmatch(result):
        raise TriggerExchangeError("Invalid timer chat encoding.")
    return result


def _share_id(compressed: bytes) -> str:
    return hashlib.sha256(compressed).hexdigest()


def _checksum(compressed: bytes) -> str:
    return f"{zlib.crc32(compressed):08x}"


def _group(text: str, size: int) -> str:
    return " ".join(text[pos:pos + size] for pos in range(0, len(text), size))


def _frame(compressed: bytes) -> str:
    return f"PNUT1 {_group(compressed.hex(), 6)} {_group(_checksum(compressed), 4)} END"


def _too_large() -> TriggerExchangeError:
    return TriggerExchangeError(
        f"This timer will not fit in one {MAX_CHAT_LINE}-character game chat message. "
        "Use JSON export instead; no settings have been removed.")


def encode_trigger(trigger: Trigger) -> str:
    """Serialize one definition; presentation callers use encode_visible_trigger."""
    if not isinstance(trigger, Trigger):
        raise TriggerExchangeError("Select one timer to share in game chat.")
    if set(FIELDS_V1) != {item.name for item in fields(Trigger)}:
        raise TriggerExchangeError("These timer fields require a newer chat format. Use JSON export.")
    data = validate_trigger(trigger.to_dict()).to_dict()
    changed = [(index, data[key]) for index, key in enumerate(FIELDS_V1)
               if data[key] != DEFAULTS_V1[index]]
    mask = sum(1 << index for index, _value in changed)
    try:
        raw = json.dumps([mask, [value for _index, value in changed]], separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise TriggerExchangeError(f"Could not share this timer: {exc}") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise _too_large()
    compressed = _compress(raw)
    message = _frame(compressed)
    if len(message) > MAX_CHAT_LINE:
        raise _too_large()
    return message


def encode_visible_trigger(trigger: Trigger, *, cfg: object = None) -> str:
    """Fail closed before encoding raw definitions for any presentation caller."""
    if safe_trigger_definition(trigger, cfg) is None:
        raise TriggerExchangeError("Carebear Mode hides timer definitions; sharing is unavailable.")
    return encode_trigger(trigger)


def _decode(compressed: bytes) -> Trigger:
    inflater = zlib.decompressobj(wbits=-15)
    raw = inflater.decompress(compressed, MAX_PAYLOAD_BYTES + 1)
    if (len(raw) > MAX_PAYLOAD_BYTES or inflater.unconsumed_tail or
            not inflater.eof or inflater.unused_data):
        raise TriggerExchangeError("Invalid or oversized timer chat payload.")

    def reject_constant(value: str) -> None:
        raise TriggerExchangeError(f"Invalid timer chat number: {value}.")

    payload = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
    if (not isinstance(payload, list) or len(payload) != 2 or
            type(payload[0]) is not int or not isinstance(payload[1], list)):
        raise TriggerExchangeError("Unsupported timer chat schema.")
    mask, changed = payload
    if not 0 <= mask < (1 << len(FIELDS_V1)) or mask.bit_count() != len(changed):
        raise TriggerExchangeError("Unsupported timer chat fields.")
    values = iter(changed)
    data = {key: next(values) if mask & (1 << index) else DEFAULTS_V1[index]
            for index, key in enumerate(FIELDS_V1)}
    if not mask & (1 << FIELDS_V1.index("id")):
        # The encoder includes the ID. A valid sparse sender may omit it.
        del data["id"]
    return validate_trigger(data)


def _parse_frame(frame: str) -> tuple[str, Trigger]:
    compact = "".join(frame.split()).upper()
    if len(compact) > MAX_CHAT_LINE:
        raise TriggerExchangeError("The timer chat message is too large.")
    if not compact.startswith(("PNUT1", "PNUTI", "PNUTL")) or not compact.endswith("END"):
        raise TriggerExchangeError("Invalid timer chat frame.")
    body = _normal_token(compact[5:-3])
    token, crc = body[:-8], body[-8:]
    if not token or len(token) > MAX_TOKEN_CHARS or len(token) % 2 or len(crc) != 8:
        raise TriggerExchangeError("Invalid timer chat payload length.")
    compressed = bytes.fromhex(token)
    if len(compressed) > MAX_COMPRESSED_BYTES or _checksum(compressed).upper() != crc:
        raise TriggerExchangeError("The timer chat checksum did not match.")
    return _share_id(compressed), _decode(compressed)


class ChatShareAssembler:
    """Recover one chat message from OCR rows and deduplicate received definitions.

    Senderless continuations attach only to a single unambiguous pending frame.
    Incomplete frames expire after 30 seconds. The recent-share cache holds at
    most 256 shares for six hours; forget permits retry after a full UI queue.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._fragments: OrderedDict[str, _Fragment] = OrderedDict()
        self._seen: OrderedDict[str, float] = OrderedDict()

    def forget(self, share_id: str) -> None:
        """Allow a verified share to be offered again after delivery was declined."""
        self._seen.pop(share_id, None)

    def _expire(self, now: float) -> None:
        for sender, fragment in list(self._fragments.items()):
            if now - fragment.created >= FRAGMENT_TTL:
                del self._fragments[sender]
        for share_id, seen in list(self._seen.items()):
            if now - seen >= SEEN_TTL:
                del self._seen[share_id]

    def feed(self, text: str, sender: str = "") -> list[ReceivedShare]:
        """Inspect captured text; unrelated, incomplete and corrupt chat is ignored."""
        now = self._clock()
        self._expire(now)
        if not isinstance(text, str) or len(text) > MAX_INPUT_CHARS:
            return []
        sender = str(sender or "").strip()[:128]
        sender_key = sender.casefold()
        fragment_created = now
        starts = list(_START.finditer(text))
        if not starts:
            key = sender_key if sender_key in self._fragments else None
            if key is None and not sender and len(self._fragments) == 1:
                key = next(iter(self._fragments))
            if key is None:
                return []
            fragment = self._fragments.pop(key)
            fragment_created = fragment.created
            text = fragment.text + "\n" + text
            sender = fragment.sender or sender
            sender_key = sender.casefold()
            starts = list(_START.finditer(text))
            if len(text) > MAX_FRAGMENT_CHARS:
                return []
        else:
            self._fragments.pop(sender_key, None)
        received: list[ReceivedShare] = []
        for index, start in enumerate(starts):
            limit = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            if start.group("version").upper() not in ("1", "I", "L"):
                continue
            end = _END.search(text, start.end(), limit)
            if end is None:
                tail = text[start.start():limit]
                if index + 1 == len(starts) and len(tail) <= MAX_FRAGMENT_CHARS:
                    if len(self._fragments) >= MAX_PENDING:
                        self._fragments.popitem(last=False)
                    self._fragments[sender_key] = _Fragment(tail, sender, fragment_created)
                continue
            frame = text[start.start():end.end()]
            if len(frame) > MAX_FRAGMENT_CHARS:
                continue
            try:
                share_id, trigger = _parse_frame(frame)
            except (ValueError, TypeError, OverflowError, RecursionError, zlib.error):
                continue
            if share_id in self._seen:
                continue
            if len(self._seen) >= MAX_SEEN:
                self._seen.popitem(last=False)
            self._seen[share_id] = now
            received.append(ReceivedShare(trigger, sender, share_id))
        return received
