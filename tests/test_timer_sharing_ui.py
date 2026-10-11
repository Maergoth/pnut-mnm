"""Sharing from the editor preserves the live store and reports failures visibly."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QSettings, Qt, Signal
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.config import Config
from mnmparse.triggers import Trigger, TriggerStore


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TimerSharingUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from mnmparse.app import theme

        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage

        class Runner(QObject):
            fired = Signal(object)

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.runner = Runner()
        self.runner.store = TriggerStore(self.root / "triggers.json")
        self.existing = Trigger(name="Existing", pattern="existing", timer=True)
        self.runner.store.triggers = [self.existing]
        self.runner.store.volume = 37
        self.runner.store.voice = "Personal voice"
        self.runner.store.output_device = "Personal device"
        self.runner.store.save()
        self.runner.save = Mock(side_effect=self.runner.store.save)
        self.runner.audio = SimpleNamespace(voices=lambda: [], devices=lambda: [])
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.page = TriggersPage(_MissingEngine(Config()), Config(casual_mode=False, casual_mode_confirmed=True), settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.set_runner(self.runner)
        self._clipboard_before = self.app.clipboard().text()

    def tearDown(self) -> None:
        for dialog in (self.page._import_dialog, self.page._chat_dialog, self.page._chat_export_dialog):
            if dialog is not None:
                dialog.close()
        self.page._save_timer.stop()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.app.clipboard().setText(self._clipboard_before)
        self.tmp.cleanup()

    def _write_import(self, triggers: list[dict]) -> str:
        return json.dumps({"triggers": triggers})

    def _import(self, text: str) -> None:
        with patch("mnmparse.app.triggers_page.QFileDialog.getOpenFileName") as chooser:
            self.page._on_import()
            self.page._import_dialog.text.setPlainText(text)
            self.page._import_dialog.import_button.click()
        chooser.assert_not_called()

    def _export(self, *, selected: bool = False) -> str:
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName") as chooser:
            self.page._on_export(selected=selected)
        chooser.assert_not_called()
        return self.app.clipboard().text()

    def test_import_saves_immediately_and_preserves_global_audio(self) -> None:
        incoming = Trigger(name="Gatekick", pattern="begins casting gate", timer=False, action="speak", speech="Kick")
        self.page.search.setText("existing")
        self._import(self._write_import([incoming.to_dict()]))
        saved = TriggerStore(self.runner.store.path)
        self.assertTrue(saved.load())
        self.assertEqual([t.name for t in saved.triggers], ["Existing", "Gatekick"])
        self.assertEqual((saved.volume, saved.voice, saved.output_device), (37, "Personal voice", "Personal device"))
        self.assertEqual(self.page._current.name, "Gatekick")
        self.assertEqual(self.page.search.text(), "")
        self.assertIn("Imported 1", self.page.sharing_status.text())
        self.assertFalse(self.page.sharing_status.isHidden())
        self.runner.save.assert_called_once()
        self.assertFalse(self.page._save_timer.isActive())

    def test_duplicate_import_does_not_create_or_save_another_timer(self) -> None:
        self._import(self._write_import([self.existing.to_dict()]))
        self.assertEqual(self.runner.store.triggers, [self.existing])
        self.assertIn("skipped 1 duplicates", self.page.sharing_status.text())
        self.runner.save.assert_not_called()

    def test_conflicting_version_is_preserved_as_a_separate_copy(self) -> None:
        incoming = self.existing.to_dict()
        incoming.update(timer_seconds=90, timer_mode="retain", timer_color="#123456")
        self._import(self._write_import([incoming]))
        self.assertEqual(self.runner.store.triggers[0].to_dict(), self.existing.to_dict())
        self.assertEqual(self.existing.timer_seconds, 30)
        added = self.runner.store.triggers[1]
        self.assertNotEqual(added.id, self.existing.id)
        self.assertEqual((added.timer_seconds, added.timer_mode, added.timer_color), (90, "retain", "#123456"))
        self.assertIn("separate copies: 1", self.page.sharing_status.text())

    def test_invalid_json_does_not_partially_import_and_keeps_text_for_correction(self) -> None:
        incoming = Trigger(name="Would be valid", pattern="text")
        before = self.runner.store.path.read_bytes()
        self._import(self._write_import([incoming.to_dict(), {"name": "Bad", "timer_seconds": "invalid"}]))
        self.assertEqual(self.runner.store.triggers, [self.existing])
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.runner.save.assert_not_called()
        self.assertIn("Import failed", self.page.sharing_status.text())
        self.assertIsNotNone(self.page._import_dialog)
        self.assertFalse(self.page._import_dialog.error.isHidden())
        self.assertIn("Would be valid", self.page._import_dialog.text.toPlainText())

    def test_save_failure_restores_live_store_and_pending_edits(self) -> None:
        self.page._schedule_save()
        previous = self.runner.store.triggers
        before = self.runner.store.path.read_bytes()
        self.runner.save.side_effect = OSError("Disk full")
        self._import(self._write_import([Trigger(name="Imported", pattern="new").to_dict()]))
        self.assertIs(self.runner.store.triggers, previous)
        self.assertIs(self.page._current, self.existing)
        self.assertEqual(self.page.list.count(), 1)
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertTrue(self.page._save_timer.isActive())
        self.assertIn("could not save", self.page.sharing_status.text())
        self.assertIn("Disk full", self.page.sharing_status.text())
        self.assertIsNotNone(self.page._import_dialog)
        self.assertIn("Imported", self.page._import_dialog.text.toPlainText())
        self.assertIn("Disk full", self.page._import_dialog.error.text())

    def test_text_import_accepts_small_and_large_json_without_reading_clipboard_automatically(self) -> None:
        self.app.clipboard().setText("keep clipboard private until Paste")
        self.page._on_import()
        dialog = self.page._import_dialog
        self.assertEqual(dialog.text.toPlainText(), "")
        self.assertFalse(dialog.import_button.isEnabled())
        single = Trigger(name="Single", pattern="one").to_dict()
        dialog.text.setPlainText(json.dumps(single))
        dialog.import_button.click()
        self.assertEqual([t.name for t in self.runner.store.triggers], ["Existing", "Single"])
        incoming = [Trigger(name=f"Timer {index}", pattern=f"phrase {index}" * 30).to_dict()
                    for index in range(20)]
        text = self._write_import(incoming)
        self.assertGreater(len(text), 10_000)
        self.app.clipboard().setText(text)
        self.page._on_import()
        dialog = self.page._import_dialog
        dialog.from_clipboard.click()
        self.assertEqual(dialog.text.toPlainText(), text)
        self.assertEqual(len(self.runner.store.triggers), 2, "Pasting must not import automatically")
        dialog.import_button.click()
        self.assertEqual(len(self.runner.store.triggers), 22)
        self.assertEqual(self.app.clipboard().text(), text)

    def test_pasted_chat_code_imports_and_cancelled_text_leaves_store_unchanged(self) -> None:
        from mnmparse.trigger_chat import encode_visible_trigger

        trigger = Trigger(name="Rebuff", pattern="You feel stronger", timer=True)
        self.page.set_config(Config())  # Authored timers also remain available in Carebear.
        self._import(encode_visible_trigger(trigger))
        self.assertEqual(self.runner.store.triggers[-1].to_dict(), trigger.to_dict())
        before = self.runner.store.path.read_bytes()
        self.page._on_import()
        dialog = self.page._import_dialog
        dialog.text.setPlainText(self._write_import([Trigger(name="Cancelled", pattern="cancel").to_dict()]))
        dialog.reject()
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertEqual([t.name for t in self.runner.store.triggers], ["Existing", "Rebuff"])

    def test_text_import_dialog_fits_and_has_readable_dark_surface(self) -> None:
        from tests.test_layout import clipped_widgets

        self.page._on_import()
        dialog = self.page._import_dialog
        dialog.resize(dialog.minimumSize())
        self.app.processEvents()
        self.assertEqual(clipped_widgets(dialog), [])
        self.assertLess(dialog.grab().toImage().pixelColor(5, 5).lightness(), 50)

    def test_open_file_loads_json_and_compact_data_into_same_text_box_before_import(self) -> None:
        from mnmparse.trigger_exchange import export_trigger_code

        definitions = [Trigger(name="JSON rebuff", pattern="you feel stronger", timer=True),
                       Trigger(name="Compact root", pattern="you are rooted", timer=True)]
        data = [json.dumps(definitions[0].to_dict()), export_trigger_code([definitions[1]])]
        for index, text in enumerate(data):
            with self.subTest(format=index):
                source = self.root / ("shared.json" if index == 0 else "shared.pnut")
                source.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
                before = self.runner.store.path.read_bytes()
                self.page._on_import()
                dialog = self.page._import_dialog
                with patch("mnmparse.app.trigger_share_dialog.QFileDialog.getOpenFileName",
                           return_value=(str(source), "")):
                    dialog.open_file.click()
                self.assertEqual(dialog.text.toPlainText(), text)
                self.assertEqual(self.runner.store.path.read_bytes(), before)
                self.assertEqual(len(self.runner.store.triggers), index + 1)
                dialog.import_button.click()
                self.assertEqual(self.runner.store.triggers[-1].to_dict(), definitions[index].to_dict())

    def test_open_file_errors_and_cancel_preserve_pasted_text_and_live_store(self) -> None:
        self.page._on_import()
        dialog = self.page._import_dialog
        text = self._write_import([Trigger(name="Pasted", pattern="pasted").to_dict()])
        dialog.text.setPlainText(text)
        before = self.runner.store.path.read_bytes()
        invalid = self.root / "invalid.txt"
        invalid.write_bytes(b"\xff")
        large = self.root / "large.txt"
        large.write_bytes(b"x" * 65)
        for source in (invalid, large, self.root / "missing.pnut"):
            with self.subTest(source=source.name), patch(
                    "mnmparse.app.trigger_share_dialog.MAX_FILE_BYTES", 64), patch(
                    "mnmparse.app.trigger_share_dialog.QFileDialog.getOpenFileName",
                    return_value=(str(source), "")):
                dialog.open_file.click()
            self.assertEqual(dialog.text.toPlainText(), text)
            self.assertIn("Could not open timer data", dialog.error.text())
            self.assertFalse(dialog.error.isHidden())
            self.assertEqual(self.runner.store.path.read_bytes(), before)
        with patch("mnmparse.app.trigger_share_dialog.QFileDialog.getOpenFileName",
                   return_value=("", "")):
            dialog.open_file.click()
        self.assertEqual(dialog.text.toPlainText(), text)
        self.assertEqual(self.runner.store.triggers, [self.existing])

    def test_export_selected_includes_timer_details_but_not_other_timers(self) -> None:
        from mnmparse.trigger_exchange import parse_trigger_text

        self.runner.store.triggers.append(Trigger(name="Other", pattern="other"))
        self.existing.timer_mode = "retain"
        self.existing.timer_color = "#123456"
        self.existing.timer_end_action = "speak"
        self.existing.timer_end_speech = "{label} expired"
        text = self._export(selected=True)
        exported = parse_trigger_text(text)
        self.assertEqual([t.to_dict() for t in exported], [self.existing.to_dict()])
        self.assertIn("Copied 1", self.page.sharing_status.text())
        self.assertTrue(text.startswith("PNUTT1"))
        self.assertNotIn("\n", text)

    def test_export_all_copies_triggers_without_countdowns(self) -> None:
        from mnmparse.trigger_exchange import parse_trigger_text

        self.runner.store.triggers.append(Trigger(name="Gatekick", pattern="gate", timer=False))
        text = self._export()
        self.assertEqual([t.name for t in parse_trigger_text(text)], ["Existing", "Gatekick"])
        self.assertIn("Copied 2", self.page.sharing_status.text())

    def test_clipboard_export_does_not_touch_active_settings_file(self) -> None:
        before = self.runner.store.path.read_bytes()
        self._export()
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertIn("Copied 1", self.page.sharing_status.text())

    def test_file_export_uses_same_compact_data_as_clipboard_and_keeps_clipboard(self) -> None:
        from mnmparse.trigger_exchange import parse_trigger_text

        code = self._export(selected=True)
        self.app.clipboard().setText("leave clipboard intact")
        self.runner.store.path = None  # In-memory stores can share definitions too.
        destination = self.root / "shared"
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName",
                   return_value=(str(destination), "")):
            self.page._on_export(selected=True, to_file=True)
        actual = destination.with_suffix(".pnut").read_text(encoding="utf-8")
        self.assertEqual(actual, code)
        self.assertEqual([t.to_dict() for t in parse_trigger_text(actual)], [self.existing.to_dict()])
        self.assertEqual(self.app.clipboard().text(), "leave clipboard intact")
        self.assertIn("Saved 1", self.page.sharing_status.text())

    def test_file_export_cancel_and_active_settings_protection_preserve_data(self) -> None:
        before = self.runner.store.path.read_bytes()
        self.app.clipboard().setText("unchanged")
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName", return_value=("", "")):
            self.page._on_export(to_file=True)
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName",
                   return_value=(str(self.runner.store.path), "")):
            self.page._on_export(to_file=True)
        self.assertIn("active timer settings file", self.page.sharing_status.text())
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertEqual(self.app.clipboard().text(), "unchanged")

    def test_file_export_write_failure_is_reported_and_preserves_existing_destination(self) -> None:
        destination = self.root / "shared.pnut"
        destination.write_text("older shared data", encoding="utf-8")
        output = Mock()
        output.open.return_value = True
        output.write.return_value = -1
        output.errorString.return_value = "Disk full"
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName",
                   return_value=(str(destination), "")), patch(
                   "mnmparse.app.triggers_page.QSaveFile", return_value=output):
            self.page._on_export(to_file=True)
        self.assertEqual(destination.read_text(encoding="utf-8"), "older shared data")
        self.assertIn("Export failed: Disk full", self.page.sharing_status.text())
        output.cancelWriting.assert_called_once()
        output.commit.assert_not_called()

    def test_export_failure_is_visible(self) -> None:
        self.app.clipboard().setText("unchanged")
        with patch("mnmparse.app.triggers_page.export_trigger_code", side_effect=OSError("Access denied")):
            self._export()
        self.assertIn("Export failed", self.page.sharing_status.text())
        self.assertIn("Access denied", self.page.sharing_status.text())
        self.assertEqual(self.app.clipboard().text(), "unchanged")

    def test_external_sound_file_is_explained_on_export_and_import(self) -> None:
        incoming = Trigger(name="Custom sound", pattern="sound", action="file", file="C:/sounds/ping.wav")
        self._import(self._write_import([incoming.to_dict()]))
        self.assertIn("external sound files", self.page.sharing_status.text())
        self._export(selected=True)
        self.assertIn("Sound files are not included", self.page.sharing_status.text())

    def test_imported_values_above_default_editor_ranges_are_retained_when_edited(self) -> None:
        incoming = Trigger(name="Long timer", pattern="long", timer=True, timer_seconds=43200,
                           timer_warn_s=7200, timer_low_s=5400, cooldown_s=7200)
        self._import(self._write_import([incoming.to_dict()]))
        self.page.name.setText("Renamed timer")
        self.page._on_edit()
        imported = self.page._current
        self.assertEqual((imported.timer_seconds, imported.timer_warn_s, imported.timer_low_s, imported.cooldown_s),
                         (43200, 7200, 5400, 7200))

    def test_export_actions_follow_selection_and_empty_store(self) -> None:
        self.assertTrue(self.page.export_selected.isEnabled())
        self.assertTrue(self.page.export_chat.isEnabled())
        self.assertTrue(self.page.export_all.isEnabled())
        self.assertTrue(self.page.save_selected.isEnabled())
        self.assertTrue(self.page.save_all.isEnabled())
        self.page._on_delete()
        self.assertFalse(self.page.export_selected.isEnabled())
        self.assertFalse(self.page.export_chat.isEnabled())
        self.assertFalse(self.page.export_all.isEnabled())
        self.assertFalse(self.page.save_selected.isEnabled())
        self.assertFalse(self.page.save_all.isEnabled())
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName") as dialog:
            self.page._on_export(selected=True)
        dialog.assert_not_called()
        self.assertIn("Select a timer", self.page.sharing_status.text())

    def _share(self, name: str = "Shared timer") -> SimpleNamespace:
        trigger = Trigger(name=name, pattern=f"start {name}", timer=True, timer_mode="retain",
                          timer_color="#123456", timer_seconds=90, action="speak", speech="Start now",
                          timer_end_action="speak", timer_end_speech="{label} expired")
        return SimpleNamespace(trigger=trigger, sender="Friendly Player", share_id=trigger.id)

    def test_received_chat_share_waits_for_explicit_review_and_import(self) -> None:
        share = self._share()
        notices = []
        self.page.chat_share_pending.connect(notices.append)
        before = self.runner.store.path.read_bytes()
        self.page.offer_chat_share(share)
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertEqual(self.runner.store.triggers, [self.existing])
        self.assertIsNone(self.page._chat_dialog)
        self.assertFalse(self.page.chat_notice.isHidden())
        self.assertIn(share.trigger.name, notices[-1])
        self.runner.save.assert_not_called()

        self.page.review_chat_shares()
        dialog = self.page._chat_dialog
        self.assertEqual(dialog.windowModality(), Qt.WindowModality.NonModal)
        dialog.resize(dialog.minimumSize())
        self.app.processEvents()
        from tests.test_layout import clipped_widgets

        self.assertEqual(clipped_widgets(dialog), [])
        details = dialog.details.toPlainText()
        for value in (share.sender, share.trigger.name, share.trigger.pattern, "90 seconds", "Retain",
                      "#123456", "Start now", "{label} expired"):
            self.assertIn(value, details)
        self.assertEqual(self.runner.store.triggers, [self.existing])
        dialog.import_button.click()
        self.assertEqual(len(self.runner.store.triggers), 2)
        self.assertEqual(self.runner.store.triggers[-1].to_dict(), share.trigger.to_dict())
        self.runner.save.assert_called_once()
        self.assertIsNone(self.page._chat_dialog)
        self.assertEqual(notices[-1], "")
        self.assertTrue(self.page.chat_notice.isHidden())

    def test_dismiss_chat_share_updates_queue_without_changing_timers(self) -> None:
        notices = []
        self.page.chat_share_pending.connect(notices.append)
        first, second = self._share("First"), self._share("Second")
        self.page.offer_chat_share(first)
        self.page.offer_chat_share(second)
        self.page.offer_chat_share(first)
        self.assertEqual(len(self.page._pending_shares), 2)
        self.page.review_chat_shares()
        self.page._chat_dialog.dismiss_button.click()
        self.assertEqual(len(self.page._pending_shares), 1)
        self.assertIn("Second", notices[-1])
        self.page.review_chat_shares()
        self.page._chat_dialog.reject()
        self.assertEqual(notices[-1], "")
        self.runner.save.assert_not_called()
        self.assertEqual(self.runner.store.triggers, [self.existing])

    def test_chat_import_save_failure_keeps_review_open_for_retry(self) -> None:
        share = self._share()
        self.page.offer_chat_share(share)
        self.page.review_chat_shares()
        dialog = self.page._chat_dialog
        self.runner.save.side_effect = OSError("Disk full")
        dialog.import_button.click()
        self.assertIs(self.page._chat_dialog, dialog)
        self.assertEqual(len(self.page._pending_shares), 1)
        self.assertEqual(self.runner.store.triggers, [self.existing])
        self.assertIn("Disk full", dialog.error.text())
        self.runner.save.side_effect = self.runner.store.save
        dialog.import_button.click()
        self.assertIsNone(self.page._chat_dialog)
        self.assertEqual(len(self.runner.store.triggers), 2)

    def test_existing_chat_share_is_ignored_and_queue_is_bounded(self) -> None:
        from mnmparse.app.triggers_page import MAX_PENDING_SHARES

        self.page.offer_chat_share(SimpleNamespace(trigger=self.existing, sender="Friend", share_id="existing"))
        self.assertFalse(self.page._pending_shares)
        for index in range(MAX_PENDING_SHARES + 2):
            self.page.offer_chat_share(self._share(f"Timer {index}"))
        self.assertEqual(len(self.page._pending_shares), MAX_PENDING_SHARES)
        self.assertIn("queue is full", self.page.sharing_status.text())
        self.runner.save.assert_not_called()

        retry = self._share("Retry after queue clears")
        self.assertFalse(self.page.offer_chat_share(retry))
        self.page.review_chat_shares()
        self.page._chat_dialog.reject()
        self.assertTrue(self.page.offer_chat_share(retry))
        self.assertEqual(len(self.page._pending_shares), MAX_PENDING_SHARES)

    def test_shared_name_is_one_line_in_passive_notice(self) -> None:
        share = self._share("\n" * 77 + "Root\tprotection")
        notices = []
        self.page.chat_share_pending.connect(notices.append)
        self.page.offer_chat_share(share)
        self.assertEqual(len(notices), 1)
        self.assertNotIn("\n", notices[0])
        self.assertNotIn("\t", notices[0])
        self.assertIn("Root protection", notices[0])
        self.assertEqual(self.page._pending_shares[0].trigger.name, share.trigger.name)

    def test_game_chat_export_copies_entire_selected_timer_as_one_message(self) -> None:
        from mnmparse.trigger_chat import ChatShareAssembler

        other = Trigger(name="Other timer", pattern="other timer", timer=True)
        self.runner.store.triggers.append(other)
        clipboard = QApplication.clipboard()
        previous_clipboard = clipboard.text()
        try:
            clipboard.setText("unchanged until Copy")
            self.page._on_export_chat()
            dialog = self.page._chat_export_dialog
            self.assertIsNotNone(dialog)
            dialog.resize(dialog.minimumSize())
            self.app.processEvents()
            from tests.test_layout import clipped_widgets

            self.assertEqual(clipped_widgets(dialog), [])
            self.assertEqual(clipboard.text(), "unchanged until Copy")
            self.assertFalse(hasattr(dialog, "previous"))
            self.assertFalse(hasattr(dialog, "next"))
            self.assertIsInstance(dialog.code, str)
            self.assertIn(str(len(dialog.code)), dialog.character_count.text())
            assembler = ChatShareAssembler()
            dialog.copy.click()
            self.assertEqual(clipboard.text(), dialog.code)
            self.assertEqual(dialog.line.toPlainText(), dialog.code)
            self.assertNotIn("\n", clipboard.text())
            self.assertNotIn("\r", clipboard.text())
            received = assembler.feed(clipboard.text(), sender="Friend")
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].trigger.name, self.existing.name)
            self.assertEqual(received[0].trigger.pattern, self.existing.pattern)
            dialog.reject()
            self.assertIsNone(self.page._chat_export_dialog)
        finally:
            clipboard.setText(previous_clipboard)

    def test_invalid_chat_export_is_reported_without_a_dialog(self) -> None:
        self.existing.pattern = ""
        self.page._on_export_chat()
        self.assertIsNone(self.page._chat_export_dialog)
        self.assertIn("Could not share", self.page.sharing_status.text())

    def test_game_chat_preview_rejects_oversized_code_without_copying(self) -> None:
        from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog

        self.app.clipboard().setText("unchanged")
        with self.assertRaisesRegex(ValueError, "at most 255"):
            TriggerChatExportDialog(self.existing, "x" * 256)
        self.assertEqual(self.app.clipboard().text(), "unchanged")

    def test_oversized_chat_export_explains_clipboard_fallback_without_splitting(self) -> None:
        with patch("mnmparse.trigger_chat.encode_trigger", side_effect=ValueError("Too long for one chat message; use Copy selected instead.")):
            self.page._on_export_chat()
        self.assertIsNone(self.page._chat_export_dialog)
        self.assertIn("use Copy selected", self.page.sharing_status.text())

    def test_review_includes_sound_file_for_warning_and_end_actions(self) -> None:
        share = self._share()
        share.trigger.timer_warn_s = 10
        share.trigger.timer_warn_action = "file"
        share.trigger.timer_end_action = "file"
        share.trigger.file = "C:/sounds/timer.wav"
        self.page.offer_chat_share(share)
        self.page.review_chat_shares()
        text = self.page._chat_dialog.details.toPlainText()
        self.assertIn("Warning action: Play sound file: C:/sounds/timer.wav", text)
        self.assertIn("When it ends: Play sound file: C:/sounds/timer.wav", text)
        self.page._chat_dialog.reject()


if __name__ == "__main__":
    unittest.main()
