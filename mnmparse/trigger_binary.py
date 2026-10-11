"""Frozen, lossless binary definitions for compact timer sharing.

The envelope supplies the protocol version, compression and checksum. Each
definition starts with an unsigned changed-field mask. Changed fields follow in
the frozen order below: UTF-8 strings have a byte-length varint, enums have a
frozen index, and changed booleans invert their frozen default without a byte.
Numbers carry a tag: 0 is an integer varint, 1 an IEEE754 little-endian double,
and 2 an integral float varint. Types and negative zero are preserved.

Varints are canonical unsigned LEB128. Changing the schema or its defaults
requires a new envelope version; current application defaults are never read.
"""

from __future__ import annotations

from dataclasses import fields
import math
import struct

from .trigger_exchange import TriggerExchangeError, validate_trigger
from .triggers import Trigger


MAX_BINARY_BYTES = 8 * 1024 * 1024

# These are protocol constants, including the order of every enum option.
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
_BOOLEANS = frozenset(("fuzzy", "enabled", "timer"))
_NUMBERS = frozenset(("volume", "cooldown_s", "timer_seconds", "timer_low_s", "timer_warn_s"))
_ENUMS = {
    "mode": ("contains", "starts", "exact", "regex"),
    "action": ("none", "sound", "file", "speak"),
    "timer_mode": ("replace", "retain", "stack"),
    "timer_warn_action": ("none", "sound", "file", "speak"),
    "timer_end_action": ("none", "sound", "file", "speak"),
    "sound": ("Chime", "Dink", "Alert", "Bell", "Beep", "Low tone", "Tick", "Rising", "Falling", "Siren"),
    "timer_warn_sound": ("Chime", "Dink", "Alert", "Bell", "Beep", "Low tone", "Tick", "Rising", "Falling", "Siren"),
    "timer_end_sound": ("Chime", "Dink", "Alert", "Bell", "Beep", "Low tone", "Tick", "Rising", "Falling", "Siren"),
}
_MAX_INTEGER = 2 ** 31 - 1


def _invalid() -> TriggerExchangeError:
    return TriggerExchangeError("Invalid or unsupported compact timer payload.")


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


class _Reader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.position = 0

    def take(self, length: int) -> bytes:
        end = self.position + length
        if end > len(self.raw):
            raise _invalid()
        result = self.raw[self.position:end]
        self.position = end
        return result

    def varint(self, maximum: int) -> int:
        value = 0
        for index in range(max(1, (maximum.bit_length() + 6) // 7)):
            byte = self.take(1)[0]
            value |= (byte & 127) << (index * 7)
            if not byte & 128:
                if value > maximum or (index and byte == 0):
                    raise _invalid()
                return value
        raise _invalid()


def _changed(value: object, default: object) -> bool:
    if value != default or type(value) is not type(default):
        return True
    return (type(value) is float and value == 0.0
            and math.copysign(1.0, value) != math.copysign(1.0, default))


def encode_binary(trigger: Trigger) -> bytes:
    """Validate and pack one definition without dropping fields or shortening text."""
    if not isinstance(trigger, Trigger):
        raise TriggerExchangeError("Select a timer definition to export.")
    if set(FIELDS_V1) != {item.name for item in fields(Trigger)}:
        raise TriggerExchangeError("These timer fields require a newer compact format.")
    original = trigger.to_dict()
    data = validate_trigger(original).to_dict()
    # Legacy model loading normalizes several numbers for the editor. Validation
    # establishes their range, while the sharing format retains the given types.
    for key in _NUMBERS:
        data[key] = original[key]
    changed = [(index, data[key]) for index, key in enumerate(FIELDS_V1)
               if _changed(data[key], DEFAULTS_V1[index])]
    encoded = bytearray(_varint(sum(1 << index for index, _value in changed)))

    def append(value: bytes) -> None:
        if len(encoded) + len(value) > MAX_BINARY_BYTES:
            raise TriggerExchangeError("The compact timer payload is too large (maximum 8 MB).")
        encoded.extend(value)

    for index, value in changed:
        key = FIELDS_V1[index]
        if key in _BOOLEANS:
            continue
        if key in _NUMBERS:
            if type(value) is int:
                append(b"\x00" + _varint(value))
            elif value.is_integer() and math.copysign(1.0, value) > 0:
                append(b"\x02" + _varint(int(value)))
            else:
                append(b"\x01" + struct.pack("<d", value))
        elif key in _ENUMS:
            try:
                append(_varint(_ENUMS[key].index(value)))
            except ValueError as exc:
                raise TriggerExchangeError("These timer options require a newer compact format.") from exc
        else:
            try:
                if len(value) > MAX_BINARY_BYTES:
                    raise TriggerExchangeError("The compact timer payload is too large (maximum 8 MB).")
                text = value.encode("utf-8")
            except UnicodeError as exc:
                raise TriggerExchangeError("The timer contains invalid Unicode.") from exc
            append(_varint(len(text)))
            append(text)
    return bytes(encoded)


def decode_binary(raw: bytes) -> Trigger:
    """Read one bounded definition, rejecting malformed encodings and trailing data."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BINARY_BYTES:
        raise _invalid()
    reader = _Reader(raw)
    mask = reader.varint((1 << len(FIELDS_V1)) - 1)
    data = dict(zip(FIELDS_V1, DEFAULTS_V1))
    for index, key in enumerate(FIELDS_V1):
        if not mask & (1 << index):
            continue
        if key in _BOOLEANS:
            value = not DEFAULTS_V1[index]
        elif key in _NUMBERS:
            tag = reader.take(1)[0]
            if tag in (0, 2):
                number = reader.varint(_MAX_INTEGER)
                value = number if tag == 0 else float(number)
            elif tag == 1:
                value = struct.unpack("<d", reader.take(8))[0]
            else:
                raise _invalid()
        elif key in _ENUMS:
            options = _ENUMS[key]
            value = options[reader.varint(len(options) - 1)]
        else:
            length = reader.varint(MAX_BINARY_BYTES)
            try:
                value = reader.take(length).decode("utf-8")
            except UnicodeError as exc:
                raise _invalid() from exc
        data[key] = value
    if reader.position != len(raw):
        raise _invalid()
    if not mask & (1 << FIELDS_V1.index("id")):
        # As with legacy imports, a sender can omit an ID to request a fresh one.
        del data["id"]
    trigger = validate_trigger(data)
    for key in _NUMBERS:
        setattr(trigger, key, data[key])
    return trigger


__all__ = ["encode_binary", "decode_binary", "FIELDS_V1", "DEFAULTS_V1", "MAX_BINARY_BYTES"]
