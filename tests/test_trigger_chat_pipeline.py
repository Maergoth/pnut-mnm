"""Shared timer codes survive the real scrolling OCR tracker and engine message path."""
from __future__ import annotations

from dataclasses import dataclass
import os
import textwrap
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.config import Config
from mnmparse.stats import Stats
from mnmparse.tracker import Tracker
from mnmparse.trigger_chat import ChatShareAssembler, encode_trigger
from mnmparse.triggers import Trigger


@dataclass
class _OcrLine:
    x: int
    y: int
    h: int
    text: str


def _frame(rows: list[str]) -> list[_OcrLine]:
    return [_OcrLine(21, 21 + index * 40, 23, text)
            for index, text in enumerate(rows[-12:])]


class TriggerChatPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.trigger = Trigger(name="Paci", pattern="You begin casting Lesser Pacify",
                               timer=True, timer_seconds=110, timer_mode="retain",
                               timer_warn_s=5, timer_color="#33aa77")

    def pipeline(self, chat_lines: list[str], *, width: int = 67):
        """Supply repeat OCR observations, then consume the engine's actual Qt signal."""
        cfg = Config(player_name="Viewer", start_capture_on_launch=False)
        with patch("mnmparse.app.engine._load_vocab_once"):
            engine = Engine(cfg)
        engine._writer = Mock()
        engine._stats = Stats(cfg.encounter_timeout_s)
        tracker = Tracker()
        assembler = ChatShareAssembler()
        received, emitted = [], []

        def on_message(message, event):
            emitted.append((message, event))
            sender = (event.raw_actor or event.actor or "") if event.kind == "chat" else ""
            received.extend(assembler.feed(message.text, sender=sender))

        engine.message.connect(on_message)
        history = [f"You crush a stumbling zombie for {amount} points of damage."
                   for amount in range(1, 13)]
        now = 100.0

        def observe():
            nonlocal now
            for message in tracker.update(_frame(history), now):
                engine._handle_message(message, cfg)
            now += 0.25

        # Cold-start contents are deliberately ignored by Engine; shares arrive later.
        observe()
        observe()
        for line in chat_lines:
            # One in-game send paints all of its wrapped rows in the same frame.
            history.extend(textwrap.wrap(line, width=width, break_long_words=True, break_on_hyphens=False))
            observe()
            observe()
            observe()  # stable rows must never create duplicate incoming shares
        now += tracker.join_timeout_s + 0.1
        observe()
        for message in tracker.flush(now):
            engine._handle_message(message, cfg)
        engine.message.disconnect(on_message)
        engine.deleteLater()
        return received, emitted

    def assert_received_timer(self, received):
        self.assertEqual(len(received), 1)
        actual = received[0].trigger
        for field in ("name", "pattern", "timer", "timer_seconds", "timer_mode",
                      "timer_warn_s", "timer_warn_action", "timer_warn_speech", "timer_color"):
            self.assertEqual(getattr(actual, field), getattr(self.trigger, field), field)

    def test_wrapped_say_messages_reassemble_from_original_ocr_text(self):
        code = encode_trigger(self.trigger)
        self.assertIsInstance(code, str)
        self.assertNotIn("\n", code)
        for width in (44, 67, 95):
            with self.subTest(width=width):
                received, emitted = self.pipeline([f'Maergoth says, "{code}"'], width=width)
                self.assert_received_timer(received)
                self.assertEqual(received[0].sender, "Maergoth")
                self.assertTrue(any(event.kind == "chat" for _message, event in emitted))
                self.assertTrue(all(not message.backlog for message, _event in emitted))

    def test_raw_and_group_unknown_messages_are_still_received(self):
        code = encode_trigger(self.trigger)
        for prefix in ("", "[Group] Maergoth: "):
            with self.subTest(prefix=prefix):
                received, emitted = self.pipeline([prefix + code])
                self.assert_received_timer(received)
                self.assertTrue(all(event.kind == "unknown" for _message, event in emitted))

    def test_consistently_misread_payload_never_becomes_an_incoming_timer(self):
        code = encode_trigger(self.trigger)
        fields = code.split()
        payload = fields[1]
        replacement = "A" if payload[len(payload) // 2] != "A" else "B"
        position = len(payload) // 2
        fields[1] = payload[:position] + replacement + payload[position + 1:]
        code = " ".join(fields)
        received, emitted = self.pipeline([f'Maergoth says, "{code}"'])
        self.assertTrue(emitted)
        self.assertEqual(received, [], "the checksum must reject a consistently wrong OCR reading")


if __name__ == "__main__":
    unittest.main()
