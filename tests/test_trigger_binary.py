"""Compact definitions preserve authored settings and reject malformed payloads."""

from dataclasses import fields, replace
import math
import struct
import unittest

from mnmparse.trigger_binary import (
    DEFAULTS_V1, FIELDS_V1, MAX_BINARY_BYTES, decode_binary, encode_binary,
)
from mnmparse.trigger_exchange import TriggerExchangeError
from mnmparse.triggers import Trigger


def varint(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


class TriggerBinaryTests(unittest.TestCase):
    def test_frozen_wire_fixture_and_all_current_fields_round_trip(self) -> None:
        self.assertEqual(FIELDS_V1, tuple(field.name for field in fields(Trigger)))
        self.assertEqual(len(FIELDS_V1), len(DEFAULTS_V1))
        basic = Trigger(name="Root", pattern="Root", id="abc")
        frozen = b"\x83\x80\x80\x40\x04Root\x04Root\x03abc"
        self.assertEqual(encode_binary(basic), frozen)
        self.assertEqual(decode_binary(frozen).to_dict(), basic.to_dict())

        custom = Trigger(
            name="Paci 雪", pattern=r"(?P<mob>雪\w+) fades", mode="regex", fuzzy=False,
            enabled=False, action="speak", sound="Dink", file="C:/音/cue.wav",
            speech="{mob} fades 🌻", volume=37, cooldown_s=3.75, timer=True,
            timer_seconds=421.25, timer_label="Paci {mob}", timer_mode="stack",
            timer_color="#ABCDEF", timer_warn_color="#2468ac", timer_low_color="#Aa0011",
            timer_low_s=7.5, timer_warn_s=12.5, timer_warn_action="speak",
            timer_warn_sound="Rising", timer_warn_speech="Wake soon", timer_end_action="none",
            timer_end_sound="Falling", timer_end_speech="Wake now", category="控制", id="ID-雪🌻",
        )
        self.assertEqual(decode_binary(encode_binary(custom)).to_dict(), custom.to_dict())

    def test_integer_float_precision_and_negative_zero_are_preserved(self) -> None:
        trigger = Trigger(pattern="target", id="numeric", volume=80.0, cooldown_s=-0.0,
                          timer_seconds=30, timer_low_s=0, timer_warn_s=math.nextafter(0.5, 1.0))
        restored = decode_binary(encode_binary(trigger))
        for name in ("volume", "cooldown_s", "timer_seconds", "timer_low_s", "timer_warn_s"):
            before, after = getattr(trigger, name), getattr(restored, name)
            self.assertIs(type(after), type(before))
            if type(before) is float:
                self.assertEqual(struct.pack("<d", after), struct.pack("<d", before))
            else:
                self.assertEqual(after, before)
        maximum = replace(trigger, timer_seconds=2 ** 31 - 1, cooldown_s=float(2 ** 31 - 1))
        self.assertEqual(decode_binary(encode_binary(maximum)).to_dict(), maximum.to_dict())

    def test_long_unicode_text_is_retained_and_drafts_are_rejected(self) -> None:
        trigger = Trigger(pattern="target", speech="雪🌻" * 30000, id="long")
        before = trigger.to_dict()
        self.assertEqual(decode_binary(encode_binary(trigger)).to_dict(), before)
        self.assertEqual(trigger.to_dict(), before)
        for invalid in (Trigger(pattern=""), replace(trigger, volume=True),
                        replace(trigger, timer_seconds=float("inf")), replace(trigger, id="")):
            with self.subTest(invalid=invalid.id):
                with self.assertRaises(TriggerExchangeError):
                    encode_binary(invalid)

    def test_malformed_lengths_numbers_masks_and_trailing_data_fail_closed(self) -> None:
        valid = encode_binary(Trigger(pattern="target", id="safe"))
        pattern = varint(1 << FIELDS_V1.index("pattern"))
        mode_mask = varint((1 << FIELDS_V1.index("pattern")) | (1 << FIELDS_V1.index("mode")))
        number_mask = varint((1 << FIELDS_V1.index("pattern")) | (1 << FIELDS_V1.index("cooldown_s")))
        malformed = (
            b"", b"\x80\x00", varint(1 << len(FIELDS_V1)), valid + b"\x00",
            pattern + varint(MAX_BINARY_BYTES + 1), pattern + b"\x02x",
            pattern + b"\x01\xff", mode_mask + b"\x01x\x04",
            number_mask + b"\x01x\xff", number_mask + b"\x01x\x00\x80\x00",
            number_mask + b"\x01x\x01" + struct.pack("<d", float("nan")),
            number_mask + b"\x01x\x01" + struct.pack("<d", -1.0),
        )
        for raw in malformed:
            with self.subTest(raw=raw[:24]):
                with self.assertRaises(TriggerExchangeError):
                    decode_binary(raw)
        for end in range(len(valid)):
            with self.subTest(truncated_at=end):
                with self.assertRaises(TriggerExchangeError):
                    decode_binary(valid[:end])
        with self.assertRaises(TriggerExchangeError):
            decode_binary(b"\x00" * (MAX_BINARY_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
