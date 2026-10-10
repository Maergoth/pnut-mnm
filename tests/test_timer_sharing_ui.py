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

    def tearDown(self) -> None:
        self.page._save_timer.stop()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _write_import(self, triggers: list[dict]) -> Path:
        path = self.root / "shared.json"
        path.write_text(json.dumps({"triggers": triggers}), encoding="utf-8")
        return path

    def _import(self, path: Path) -> None:
        with patch("mnmparse.app.triggers_page.QFileDialog.getOpenFileName", return_value=(str(path), "")):
            self.page._on_import()

    def _export(self, path: Path, *, selected: bool = False) -> None:
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            self.page._on_export(selected=selected)

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

    def test_invalid_file_does_not_partially_import(self) -> None:
        incoming = Trigger(name="Would be valid", pattern="text")
        before = self.runner.store.path.read_bytes()
        self._import(self._write_import([incoming.to_dict(), {"name": "Bad", "timer_seconds": "invalid"}]))
        self.assertEqual(self.runner.store.triggers, [self.existing])
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.runner.save.assert_not_called()
        self.assertIn("Import failed", self.page.sharing_status.text())

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

    def test_export_selected_includes_timer_details_but_not_other_timers(self) -> None:
        from mnmparse.trigger_exchange import read_trigger_file

        self.runner.store.triggers.append(Trigger(name="Other", pattern="other"))
        self.existing.timer_mode = "retain"
        self.existing.timer_color = "#123456"
        self.existing.timer_end_action = "speak"
        self.existing.timer_end_speech = "{label} expired"
        path = self.root / "selected.json"
        self._export(path, selected=True)
        exported = read_trigger_file(path)
        self.assertEqual([t.to_dict() for t in exported], [self.existing.to_dict()])
        self.assertIn("Exported 1", self.page.sharing_status.text())
        self.assertNotIn("settings", json.loads(path.read_text(encoding="utf-8")))

    def test_export_all_includes_triggers_without_countdowns_and_adds_json_suffix(self) -> None:
        from mnmparse.trigger_exchange import read_trigger_file

        self.runner.store.triggers.append(Trigger(name="Gatekick", pattern="gate", timer=False))
        path = self.root / "all"
        self._export(path)
        self.assertEqual([t.name for t in read_trigger_file(path.with_suffix(".json"))], ["Existing", "Gatekick"])
        self.assertIn("Exported 2", self.page.sharing_status.text())

    def test_export_cannot_overwrite_active_settings_file(self) -> None:
        before = self.runner.store.path.read_bytes()
        self._export(self.runner.store.path)
        self.assertEqual(self.runner.store.path.read_bytes(), before)
        self.assertIn("active timer settings", self.page.sharing_status.text())

    def test_export_failure_is_visible(self) -> None:
        with patch("mnmparse.app.triggers_page.export_visible_trigger_file", side_effect=OSError("Access denied")):
            self._export(self.root / "shared.json")
        self.assertIn("Export failed", self.page.sharing_status.text())
        self.assertIn("Access denied", self.page.sharing_status.text())

    def test_external_sound_file_is_explained_on_export_and_import(self) -> None:
        incoming = Trigger(name="Custom sound", pattern="sound", action="file", file="C:/sounds/ping.wav")
        self._import(self._write_import([incoming.to_dict()]))
        self.assertIn("external sound files", self.page.sharing_status.text())
        self._export(self.root / "with-sound.json", selected=True)
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
        self.page._on_delete()
        self.assertFalse(self.page.export_selected.isEnabled())
        self.assertFalse(self.page.export_chat.isEnabled())
        self.assertFalse(self.page.export_all.isEnabled())
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

    def test_oversized_chat_export_explains_file_fallback_without_splitting(self) -> None:
        with patch("mnmparse.trigger_chat.encode_trigger", side_effect=ValueError("Too long for one chat message; use JSON export.")):
            self.page._on_export_chat()
        self.assertIsNone(self.page._chat_export_dialog)
        self.assertIn("use JSON export", self.page.sharing_status.text())

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
