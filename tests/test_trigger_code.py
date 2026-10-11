"""Dense clipboard libraries: lossless sharing and whole-import validation."""

from dataclasses import replace
import base64
import math
import random
import string
import unittest
from unittest.mock import patch
import zlib

from mnmparse.trigger_chat import ChatShareAssembler, encode_trigger
from mnmparse.trigger_exchange import (
    CODE_MARKER, TriggerExchangeError, _write_code_varint, export_trigger_code,
    export_trigger_json, merge_triggers, parse_trigger_text,
)
from mnmparse.trigger_binary import encode_binary
from mnmparse.triggers import Trigger


def frame_raw(raw: bytes, *, tail: bytes = b"") -> str:
    compressor = zlib.compressobj(9, wbits=-15)
    compressed = compressor.compress(raw) + compressor.flush() + tail
    token = base64.urlsafe_b64encode(compressed).decode("ascii").rstrip("=")
    return f"{CODE_MARKER} {token} {zlib.crc32(compressed):08x} END"


class ClipboardCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timer = Trigger(name="Paci 日本語", pattern=r"(?P<who>\w+) begins casting Pacify",
                             mode="regex", fuzzy=False, enabled=False, action="file",
                             sound="Dink", file="C:/My sounds/quiet.wav", speech="{who} casts",
                             volume=37, cooldown_s=2.5, timer=True, timer_seconds=110.25,
                             timer_label="{who} Paci", timer_mode="stack", timer_color="#aBcDeF",
                             timer_warn_color="#2468ac", timer_low_color="#Aa0011",
                             timer_low_s=7.5, timer_warn_s=12.5, timer_warn_action="speak",
                             timer_warn_sound="Rising", timer_warn_speech="Wake soon",
                             timer_end_action="none", timer_end_sound="Falling",
                             timer_end_speech="Wake now", category="Control", id="keep-this-id")

    def test_one_code_preserves_all_fields_and_ignores_game_chat_limit(self) -> None:
        source = random.Random(123)
        timer = replace(self.timer, speech="".join(source.choices(string.ascii_letters, k=600)))
        before = timer.to_dict()
        with patch("pathlib.Path.open", side_effect=AssertionError("No file access")):
            code = export_trigger_code([timer])
            incoming = parse_trigger_text(code)
        self.assertTrue(code.startswith(CODE_MARKER + " "))
        self.assertTrue(code.isascii())
        self.assertNotIn("\n", code)
        self.assertGreater(len(code), 255)
        self.assertEqual(incoming[0].to_dict(), before)
        self.assertEqual(timer.to_dict(), before)
        self.assertEqual(ChatShareAssembler().feed(code), [])

    def test_library_code_is_dense_and_duplicate_merges_preserve_local_variant(self) -> None:
        variant = replace(self.timer, timer_seconds=60)
        library = [self.timer, variant, self.timer]
        code = export_trigger_code(library)
        self.assertLess(len(code), len(export_trigger_json(library)) // 2)
        incoming = parse_trigger_text(code)
        self.assertEqual([item.to_dict() for item in incoming], [item.to_dict() for item in library])
        result = merge_triggers([self.timer], incoming)
        self.assertEqual((result.added, result.skipped, result.conflicts), (1, 2, 1))
        self.assertNotEqual(result.triggers[1].id, self.timer.id)
        self.assertEqual(result.triggers[0].to_dict(), self.timer.to_dict())

    def test_library_wrapper_preserves_authored_number_types_and_negative_zero(self) -> None:
        timer = replace(self.timer, volume=37.0, timer_seconds=110, timer_low_s=-0.0)
        imported = parse_trigger_text(export_trigger_code([timer]))[0]
        self.assertIs(type(imported.volume), float)
        self.assertIs(type(imported.timer_seconds), int)
        self.assertEqual(math.copysign(1.0, imported.timer_low_s), -1.0)

    def test_wrapped_clipboard_code_and_legacy_imports_remain_supported(self) -> None:
        code = export_trigger_code([self.timer])
        marker, token, checksum, end = code.split()
        wrapped = "\ufeff" + marker + "\n" + "\n".join(token[i:i + 37] for i in range(0, len(token), 37))
        self.assertEqual(parse_trigger_text(wrapped + "\n" + checksum + "\n" + end), [self.timer])
        self.assertEqual(parse_trigger_text(export_trigger_json([self.timer])), [self.timer])
        simple = Trigger(name="Paci", pattern="Pacify", timer=True, id="paci")
        self.assertEqual(parse_trigger_text(encode_trigger(simple)), [simple])
        self.assertEqual(parse_trigger_text(export_trigger_code([])), [])

    def test_corrupt_incomplete_or_wrong_version_code_never_returns_partial_input(self) -> None:
        code = export_trigger_code([self.timer])
        marker, token, checksum, end = code.split()
        wrong = "00000000" if checksum != "00000000" else "11111111"
        for bad in (code[:-4], code + " ignored", code.replace(marker, "PNUTT2", 1),
                    f"{marker} {token} {wrong} {end}", f"{marker} !{token} {checksum} {end}",
                    code + "\n" + code):
            with self.subTest(code=bad[:25]):
                with self.assertRaises(TriggerExchangeError):
                    parse_trigger_text(bad)

    def test_invalid_second_definition_noncanonical_lengths_and_trailing_data_fail_closed(self) -> None:
        valid = encode_binary(self.timer)
        first = _write_code_varint(len(valid)) + valid
        for raw in (b"\x02" + first + b"\x01\xff", b"\x02" + first,
                    b"\x81\x00" + first, b"\x01\x80\x00", b"\x00trailing"):
            with self.subTest(raw=raw[:8]):
                with self.assertRaises(TriggerExchangeError):
                    parse_trigger_text(frame_raw(raw))
        self.assertEqual(parse_trigger_text(frame_raw(b"\x01" + first)), [self.timer])

    def test_decompression_bomb_and_appended_compressed_stream_fail_closed(self) -> None:
        # The textual code stays small; only the expanded bytes exceed the bound.
        code = frame_raw(b" " * 1025)
        with patch("mnmparse.trigger_exchange.MAX_FILE_BYTES", 1024):
            with self.assertRaisesRegex(TriggerExchangeError, "too large"):
                parse_trigger_text(code)
        valid = encode_binary(self.timer)
        raw = b"\x01" + _write_code_varint(len(valid)) + valid
        with self.assertRaisesRegex(TriggerExchangeError, "invalid|too large"):
            parse_trigger_text(frame_raw(raw, tail=b"extra compressed bytes"))

    def test_export_and_decode_enforce_library_limit_before_any_merge(self) -> None:
        valid = encode_binary(self.timer)
        raw = b"\x02" + 2 * (_write_code_varint(len(valid)) + valid)
        with patch("mnmparse.trigger_exchange.MAX_TRIGGERS", 1):
            with self.assertRaisesRegex(TriggerExchangeError, "Too many"):
                export_trigger_code([self.timer, self.timer])
            with self.assertRaisesRegex(TriggerExchangeError, "Too many"):
                parse_trigger_text(frame_raw(raw))
        with patch("mnmparse.trigger_exchange.MAX_FILE_BYTES", 20):
            with self.assertRaisesRegex(TriggerExchangeError, "too large"):
                export_trigger_code([self.timer])


if __name__ == "__main__":
    unittest.main()
