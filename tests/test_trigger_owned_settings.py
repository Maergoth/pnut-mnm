"""Carebear keeps authored settings and captured trigger chat usable normally."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from mnmparse.app.main import _MissingEngine
from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog, TriggerSharePrompt
from mnmparse.app.triggers_page import TriggersPage
from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.config import Config
from mnmparse.trigger_chat import ChatShareAssembler, encode_visible_trigger
from mnmparse.trigger_exchange import parse_trigger_text
from mnmparse.triggers import Trigger, TriggerStore
from mnmparse.triggers import Match


CFG = Config(player_name="Owner", reduced_motion=True)


class TriggerOwnedSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TriggerStore(self.root / "triggers.json")
        self.trigger = Trigger(
            name="Rebuff", pattern="You feel stronger", action="speak", speech="Rebuff soon",
            timer=True, timer_seconds=180, timer_label="Rebuff", timer_warn_action="speak",
            timer_warn_speech="Rebuff soon", timer_end_action="speak", timer_end_speech="Rebuff Rebuff",
        )
        self.store.triggers = [self.trigger]
        self.store.save()
        self.audio = Mock()
        self.audio.voices.return_value = []
        self.audio.devices.return_value = []
        with patch("mnmparse.app.triggers_runtime.AudioOut", return_value=self.audio):
            self.runner = TriggerRunner(self.store)
        self.settings = QSettings(str(self.root / "ui.ini"), QSettings.Format.IniFormat)
        self.page = TriggersPage(_MissingEngine(CFG), CFG, self.settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.set_runner(self.runner)
        self.dialogs = []

    def tearDown(self):
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        if self.page._chat_export_dialog is not None:
            self.page._chat_export_dialog.close()
        self.page._save_timer.stop()
        self.page.close()
        self.page.deleteLater()
        self.runner._clock.stop()
        self.runner.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_saved_names_patterns_and_audio_settings_remain_visible(self):
        item = self.page.list.item(0)
        self.assertIn("Rebuff", item.text())
        self.assertIn("You feel stronger", item.text())
        self.assertNotIn("Captured chat hidden", item.text())
        self.assertIn("You feel stronger", item.toolTip())
        self.assertEqual(self.page.name.text(), "Rebuff")
        self.assertEqual(self.page.pattern.text(), "You feel stronger")
        self.assertEqual(self.page.speech.text(), "Rebuff soon")
        self.assertEqual(self.page.end_speech.text(), "Rebuff Rebuff")
        self.assertTrue(self.page.editor.isEnabled())
        self.assertTrue(self.page.new_button.isEnabled())
        self.assertTrue(self.page.duplicate_button.isEnabled())

    def test_search_matches_saved_patterns_as_well_as_names(self):
        self.page.search.setText("feel stronger")
        self.assertEqual(self.page.list.count(), 1)
        self.page.search.setText("not present")
        self.assertEqual(self.page.list.count(), 0)
        self.page.search.setText("rebuff")
        self.assertEqual(self.page.list.count(), 1)

    def test_edit_duplicate_and_new_definition_work_without_changing_modes(self):
        for field, text in ((self.page.name, "My rebuff"), (self.page.pattern, "You glow"),
                            (self.page.end_speech, "My rebuff faded")):
            field.selectAll()
            QTest.keyClicks(field, text)
        self.assertTrue(self.page.flush_pending_changes())
        saved = TriggerStore(self.store.path)
        self.assertTrue(saved.load())
        self.assertEqual((saved.triggers[0].name, saved.triggers[0].pattern,
                          saved.triggers[0].timer_end_speech),
                         ("My rebuff", "You glow", "My rebuff faded"))
        self.page._on_duplicate()
        self.assertEqual(len(self.store.triggers), 2)
        self.assertEqual(self.page._current.name, "My rebuff (copy)")
        self.assertEqual(self.page._current.pattern, "You glow")
        self.assertNotEqual(self.store.triggers[0].id, self.page._current.id)
        self.page._on_new()
        self.assertEqual(len(self.store.triggers), 3)
        self.assertTrue(self.page.editor.isEnabled())
        self.assertTrue(self.page._cfg.casual_mode)

    def test_manual_test_and_preview_keep_own_regex_damage(self):
        self.trigger.name = "DevoProc"
        self.trigger.pattern = r"^You crush a rat for (?P<damage>\d+) points of damage\.$"
        self.trigger.mode = "regex"
        self.trigger.speech = "Boom Boom {damage}"
        self.trigger.timer_label = "DevoProc"
        self.page._refill_list()
        self.page._load_editor(self.trigger)
        self.page.test_line.setText("You crush a rat for 42 points of damage.")
        self.assertIn("Matches", self.page.test_result.text())
        self.assertIn("42", self.page.test_result.text())
        self.page._preview("speech")
        self.audio.speak.assert_called_with("Boom Boom 42", self.trigger.volume)
        self.page._fire_now()
        self.assertEqual(self.audio.run.call_args.kwargs["speech"], "Boom Boom 42")
        self.assertEqual(self.runner.board.timers[0].label, "DevoProc")

    def test_definition_clipboard_and_chat_export_are_available_in_carebear(self):
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName",
                   side_effect=AssertionError("Timer export must not open a file dialog")):
            self.page._on_export(selected=True)
        exported = parse_trigger_text(self.app.clipboard().text())
        self.assertEqual(len(exported), 1)
        self.assertEqual((exported[0].name, exported[0].pattern, exported[0].timer_end_speech),
                         ("Rebuff", "You feel stronger", "Rebuff Rebuff"))
        self.page._on_export_chat()
        dialog = self.page._chat_export_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.copy.isEnabled())
        dialog._copy_line()
        self.assertEqual(self.app.clipboard().text(), dialog.code)
        received = ChatShareAssembler().feed(dialog.code)
        self.assertEqual(len(received), 1)
        self.assertEqual((received[0].trigger.name, received[0].trigger.pattern),
                         ("Rebuff", "You feel stronger"))

    def test_authored_file_preview_and_static_end_speech_remain_available(self):
        self.page.file.setText("C:/Own Audio/rebuff.wav")
        self.page._preview("file")
        self.audio.play_file.assert_called_with("C:/Own Audio/rebuff.wav", self.trigger.volume)
        self.runner.test(self.trigger, "You feel stronger")
        timer = self.runner.board.timers[0]
        self.runner._alert(timer, "end")
        self.assertEqual(self.audio.run.call_args.kwargs["speech"], "Rebuff Rebuff")

    def test_received_definition_and_sender_are_reviewable_in_both_modes(self):
        dialog = TriggerSharePrompt(self.trigger, "CapturedPeerIdentity", cfg=CFG)
        self.dialogs.append(dialog)
        summary = dialog.details.toPlainText()
        self.assertIn("Name: Rebuff", summary)
        self.assertIn("Chat text: You feel stronger", summary)
        self.assertIn("Rebuff Rebuff", summary)
        self.assertIn("CapturedPeerIdentity", summary)
        self.assertTrue(dialog.import_button.isEnabled())
        full = dataclasses.replace(CFG, casual_mode=False, casual_mode_confirmed=True)
        dialog.set_config(full)
        self.assertIn("CapturedPeerIdentity", dialog.details.toPlainText())
        dialog.set_config(CFG)
        self.assertIn("CapturedPeerIdentity", dialog.details.toPlainText())
        self.assertIn("Chat text: You feel stronger", dialog.details.toPlainText())

    def test_mode_change_keeps_definition_copy_and_captured_history(self):
        full = dataclasses.replace(CFG, casual_mode=False, casual_mode_confirmed=True)
        self.page.set_config(full)
        peer_line = "CapturedPeerIdentity crushes a rat for 12345 points of damage."
        self.page.add_recent_line(peer_line)
        self.page._on_fired(Match(self.trigger, peer_line, "CapturedPeerIdentity"))
        dialog = TriggerChatExportDialog(self.trigger, encode_visible_trigger(self.trigger, cfg=full), cfg=full)
        self.dialogs.append(dialog)
        self.page.set_config(CFG)
        dialog.set_config(CFG)
        self.assertEqual(self.page.name.text(), "Rebuff")
        self.assertEqual(self.page.pattern.text(), "You feel stronger")
        self.assertTrue(self.page.editor.isEnabled())
        self.assertEqual(self.page.recent.count(), 1)
        self.assertIn("CapturedPeerIdentity", self.page.recent.item(0).text())
        self.assertIn("CapturedPeerIdentity", " ".join(self.page._completer_model.stringList()))
        self.assertTrue(dialog.copy.isEnabled())
        self.assertTrue(dialog.code)
        dialog._copy_line()
        self.assertEqual(self.app.clipboard().text(), dialog.code)

    def test_captured_peer_match_history_and_speech_preserve_source(self):
        self.trigger.pattern = r"^(?P<actor>[A-Za-z]+) crushes a rat for (?P<damage>\d+) points of damage\.$"
        self.trigger.mode = "regex"
        self.trigger.timer_label = "Damage alert"
        self.trigger.speech = "Damage alert {damage}"
        self.page._refill_list()
        self.page._load_editor(self.trigger)
        peer_line = "CapturedPeerIdentity crushes a rat for 12345 points of damage."
        self.page.add_recent_line(peer_line)
        self.page._refresh_completions()
        self.runner.observe(peer_line, now=10)
        self.assertEqual(self.page.recent.count(), 1)
        text = self.page.recent.item(0).text() + self.page.recent.item(0).toolTip()
        self.assertIn(peer_line, text)
        self.assertIn("CapturedPeerIdentity", " ".join(self.page._completer_model.stringList()))
        self.assertEqual(self.audio.run.call_args.kwargs["speech"], "Damage alert 12345")
        self.assertIn("Damage alert", self.runner.board.timers[0].label)


if __name__ == "__main__":
    unittest.main()
