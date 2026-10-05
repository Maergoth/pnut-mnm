"""Single-message timer sharing, OCR tolerance and bounded decoding."""

from dataclasses import fields, replace
import json
import random
import string
import unittest

from mnmparse import trigger_chat as chat
from mnmparse.trigger_exchange import TriggerExchangeError
from mnmparse.triggers import Trigger


def frame_payload(raw: bytes) -> str:
    """Build a correctly checksummed envelope around deliberately invalid JSON."""
    return chat._frame(chat._compress(raw))


def sparse_payload(trigger: Trigger) -> bytes:
    data = trigger.to_dict()
    changed = [(index, data[key]) for index, key in enumerate(chat.FIELDS_V1)
               if data[key] != chat.DEFAULTS_V1[index]]
    return json.dumps([sum(1 << index for index, _ in changed),
                       [value for _, value in changed]], separators=(",", ":")).encode()


class ChatCodecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.trigger = Trigger(name="Paci", pattern="Pacify", timer=True, timer_seconds=110.25,
                               id="paci", enabled=False, timer_mode="retain")
        self.message = chat.encode_trigger(self.trigger)

    def test_one_message_round_trip_preserves_every_current_field(self) -> None:
        self.assertEqual(set(chat.FIELDS_V1), {item.name for item in fields(Trigger)})
        self.assertEqual(len(chat.FIELDS_V1), len(chat.DEFAULTS_V1))
        self.assertIsInstance(self.message, str)
        self.assertNotIn("\n", self.message)
        self.assertTrue(self.message.isascii())
        self.assertLessEqual(len(self.message), 255)
        received = chat.ChatShareAssembler().feed(self.message, "Maergoth")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].trigger.to_dict(), self.trigger.to_dict())
        self.assertEqual(received[0].sender, "Maergoth")

    def test_each_custom_setting_including_colors_id_and_fractions_is_lossless(self) -> None:
        custom = dict(
            name="Paci II", pattern=r"(?P<mob>\w+) fades", mode="regex", fuzzy=False, enabled=True,
            action="speak", sound="Dink", file="C:/My sounds/cue.wav", speech="{mob} fades",
            volume=37, cooldown_s=3.75, timer=False, timer_seconds=421.25,
            timer_label="Paci {mob}", timer_mode="stack", timer_color="#ABCDEF",
            timer_warn_color="#2468ac", timer_low_color="#Aa0011", timer_low_s=7.5,
            timer_warn_s=12.5, timer_warn_action="speak", timer_warn_sound="Rising",
            timer_warn_speech="Wake soon", timer_end_action="none", timer_end_sound="Falling",
            timer_end_speech="Wake now", category="Control", id="retain-this-id",
        )
        self.assertEqual(set(custom), set(chat.FIELDS_V1))
        for field, value in custom.items():
            with self.subTest(field=field):
                trigger = replace(self.trigger, **{field: value})
                received = chat.ChatShareAssembler().feed(chat.encode_trigger(trigger))
                self.assertEqual(received[0].trigger.to_dict(), trigger.to_dict())

    def test_multiple_independent_chat_messages_in_one_capture(self) -> None:
        other = replace(self.trigger, name="Root", pattern="Root", id="root")
        text = f'Maergoth says, "{self.message}"\nMaergoth says, "{chat.encode_trigger(other)}"'
        shares = chat.ChatShareAssembler().feed(text, "Maergoth")
        self.assertEqual([share.trigger for share in shares], [self.trigger, other])

    def test_ocr_spaces_case_and_hex_letter_aliases_are_tolerated(self) -> None:
        words = self.message.split()
        body = " \n".join(" ".join(word) for word in words[1:-1])
        body = body.translate(str.maketrans("01", "OI"))
        noisy = f"PNUT1 {body} END".lower()
        received = chat.ChatShareAssembler().feed(noisy)
        self.assertEqual([share.trigger for share in received], [self.trigger])

    def test_visually_wrapped_message_across_calls_preserves_first_sender(self) -> None:
        assembler = chat.ChatShareAssembler()
        received = assembler.feed(f'Maergoth says, "{self.message[:49]}', "Maergoth")
        received += assembler.feed(self.message[49:-2])
        received += assembler.feed(self.message[-2:] + '"')
        self.assertEqual([share.trigger for share in received], [self.trigger])
        self.assertEqual(received[0].sender, "Maergoth")

    def test_marker_letter_aliases_require_a_complete_valid_checksum(self) -> None:
        for marker in ("PNUTI", "PNUTL"):
            message = self.message.replace("PNUT1", marker)
            self.assertEqual(chat.ChatShareAssembler().feed(message)[0].trigger, self.trigger)

    def test_incomplete_corrupt_message_never_emits_and_valid_resend_recovers(self) -> None:
        assembler = chat.ChatShareAssembler()
        self.assertEqual(assembler.feed(self.message[:-4]), [])
        damaged = self.message.split()
        damaged[1] = ("A" if damaged[1][0] != "A" else "B") + damaged[1][1:]
        self.assertEqual(assembler.feed(" ".join(damaged)), [])
        self.assertEqual([share.trigger for share in assembler.feed(self.message)], [self.trigger])

    def test_checksum_covers_the_complete_payload(self) -> None:
        words = self.message.split()
        words[-2] = "0000" if words[-2] != "0000" else "1111"
        self.assertEqual(chat.ChatShareAssembler().feed(" ".join(words)), [])

    def test_only_exact_supported_marker_versions_are_considered(self) -> None:
        for prefix in ("PNUT2", "PNUT01", "APNUT1", "PNUT", "PNUT99"):
            with self.subTest(prefix=prefix):
                text = self.message.replace("PNUT1", prefix)
                self.assertEqual(chat.ChatShareAssembler().feed(text), [])
        self.assertEqual(chat.ChatShareAssembler().feed("Please import a timer and execute this text"), [])

    def test_invalid_masks_and_multi_timer_payloads_never_emit(self) -> None:
        for payload in ({"triggers": []}, [True, []], [-1, []], [1 << len(chat.FIELDS_V1), ["x"]],
                        [1, []], [1, ["x", "x"]], [0, [], []], [0, [[1, ["Paci"]], [1, ["Root"]]]]):
            with self.subTest(payload=payload):
                message = frame_payload(json.dumps(payload).encode())
                self.assertEqual(chat.ChatShareAssembler().feed(message), [])

    def test_payload_receives_normal_trigger_validation(self) -> None:
        for field, value in (("timer_seconds", -1), ("enabled", "true"), ("pattern", ""),
                             ("timer_mode", "overwrite"), ("timer_color", "bad")):
            with self.subTest(field=field):
                trigger = replace(self.trigger, **{field: value})
                message = frame_payload(sparse_payload(trigger))
                self.assertEqual(chat.ChatShareAssembler().feed(message), [])

    def test_bounded_decompression_rejects_a_compressed_bomb(self) -> None:
        # Fits the wire budget even though decompression exceeds its hard bound.
        raw = b" " * (chat.MAX_PAYLOAD_BYTES + 1)
        message = frame_payload(raw)
        self.assertLessEqual(len(message), chat.MAX_CHAT_LINE)
        self.assertEqual(chat.ChatShareAssembler().feed(message), [])

    def test_trailing_compressed_stream_is_rejected(self) -> None:
        compressed = chat._compress(sparse_payload(self.trigger)) + chat._compress(b"extra")
        with self.assertRaises(TriggerExchangeError):
            chat._decode(compressed)

    def test_encoder_rejects_lists_drafts_and_oversize_without_splitting_or_dropping_settings(self) -> None:
        for trigger in ([self.trigger], Trigger(pattern="")):
            with self.subTest(kind=type(trigger)):
                with self.assertRaises(TriggerExchangeError):
                    chat.encode_trigger(trigger)
        source = random.Random(12)
        long_speech = "".join(source.choice(string.ascii_letters) for _ in range(600))
        trigger = replace(self.trigger, speech=long_speech)
        before = trigger.to_dict()
        with self.assertRaisesRegex(TriggerExchangeError, "one 255-character.*JSON export"):
            chat.encode_trigger(trigger)
        self.assertEqual(trigger.to_dict(), before)

    def test_complete_oversized_frames_are_ignored(self) -> None:
        message = f"PNUT1 {'A' * 260} 0000 0000 END"
        self.assertEqual(chat.ChatShareAssembler().feed(message), [])

    def test_fragment_expiration_allows_a_clean_resend(self) -> None:
        now = [0.0]
        assembler = chat.ChatShareAssembler(clock=lambda: now[0])
        assembler.feed(self.message[:40], "Maergoth")
        now[0] += chat.FRAGMENT_TTL + 1
        self.assertEqual(assembler.feed(self.message[40:]), [])
        self.assertEqual(len(assembler.feed(self.message)), 1)

    def test_duplicate_suppression_forget_and_expiration(self) -> None:
        now = [0.0]
        assembler = chat.ChatShareAssembler(clock=lambda: now[0])
        received = assembler.feed(self.message)
        self.assertEqual(len(received), 1)
        self.assertEqual(assembler.feed(self.message), [])
        assembler.forget(received[0].share_id)
        self.assertEqual(len(assembler.feed(self.message)), 1)
        now[0] += chat.SEEN_TTL + 1
        self.assertEqual(len(assembler.feed(self.message)), 1)

    def test_pending_fragment_and_seen_caches_are_bounded(self) -> None:
        assembler = chat.ChatShareAssembler()
        for index in range(chat.MAX_PENDING + 5):
            assembler.feed(self.message[:30], f"Player{index}")
        self.assertLessEqual(len(assembler._fragments), chat.MAX_PENDING)
        for index in range(chat.MAX_SEEN + 5):
            message = chat.encode_trigger(replace(self.trigger, name=f"Timer{index}"))
            assembler.feed(message, "Player")
        self.assertLessEqual(len(assembler._seen), chat.MAX_SEEN)

    def test_ambiguous_senderless_continuation_does_not_mix_senders(self) -> None:
        assembler = chat.ChatShareAssembler()
        assembler.feed(self.message[:40], "Alice")
        assembler.feed(self.message[:40], "Bob")
        self.assertEqual(assembler.feed(self.message[40:]), [])
        self.assertEqual(len(assembler._seen), 0)

    def test_oversized_capture_and_fragment_are_ignored(self) -> None:
        assembler = chat.ChatShareAssembler()
        self.assertEqual(assembler.feed("x" * (chat.MAX_INPUT_CHARS + 1)), [])
        assembler.feed(self.message[:40])
        self.assertEqual(assembler.feed("A" * chat.MAX_FRAGMENT_CHARS), [])
        self.assertEqual(len(assembler._fragments), 0)


if __name__ == "__main__":
    unittest.main()
