"""Enforceable trigger deadlines, durable edits and presentation privacy boundaries."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import dataclasses
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from mnmparse.config import Config
from mnmparse.trigger_exchange import TriggerExchangeError, validate_trigger
from mnmparse.triggers import MAX_PATTERN_CHARS, Match, Trigger, TriggerStore, match_trigger


class TriggerDeadlineTests(unittest.TestCase):
    def setUp(self):
        from mnmparse import triggers
        triggers._quarantined_patterns.clear()

    def test_catastrophic_regex_is_bounded_and_quarantined(self):
        trigger = Trigger(mode="regex", pattern=r"(?:(?:a+)+)+$")
        start = time.monotonic()
        self.assertIsNone(match_trigger(trigger, "a" * 6000 + "!"))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertIn("time limit", " ".join(trigger.problems()))
        store = TriggerStore()
        store.triggers = [trigger]
        self.assertEqual(store.matches("a" * 6000 + "!"), [])
        self.assertFalse(trigger.enabled)
        self.assertIn(trigger.id, store.match_warnings)
        trigger.enabled = True
        with patch("mnmparse.triggers._compiled", side_effect=AssertionError("Quarantined regex was retried")):
            self.assertIsNone(match_trigger(trigger, "a" * 6000 + "!"))

    def test_many_expensive_patterns_have_one_line_budget(self):
        store = TriggerStore()
        store.triggers = [Trigger(mode="regex", pattern=rf"(?:(?:a+)+)+$(?#pattern{i})") for i in range(20)]
        clock = SimpleNamespace(value=0.0)

        def expensive_match(*_args, **_kwargs):
            clock.value += .011
            return None

        # Separate the store's wall-clock policy from the native engine's CPU
        # deadline, already verified above. This cannot depend on CPU contention
        # or timeout-check scheduling during the full suite.
        with patch("mnmparse.triggers.time.monotonic", side_effect=lambda: clock.value), \
                patch("mnmparse.triggers.match_trigger", side_effect=expensive_match) as match:
            self.assertEqual(store.matches("a" * 6000 + "!"), [])
        self.assertEqual(match.call_count, 5)
        self.assertIn("", store.match_warnings)
        self.assertLess(match.call_count, len(store.triggers), "The line budget must stop before trying every pattern")

    def test_import_rejects_oversized_pattern_before_compilation(self):
        data = Trigger(pattern="a" * (MAX_PATTERN_CHARS + 1), mode="regex").to_dict()
        with patch("mnmparse.triggers.regex.compile", side_effect=AssertionError("Oversized pattern compiled")):
            with self.assertRaisesRegex(TriggerExchangeError, "too long"):
                validate_trigger(data)

    def test_invalid_deep_expression_does_not_raise_recursion_error(self):
        trigger = Trigger(pattern="(" * 1000 + "a" + ")" * 1000, mode="regex")
        self.assertIsNone(match_trigger(trigger, "a"))
        self.assertTrue(trigger.problems())


class TriggerDurabilityTests(unittest.TestCase):
    def test_invalid_store_rejects_entire_restore_without_partial_mutation(self):
        store = TriggerStore()
        store.triggers = [Trigger(name="Preserved", pattern="hits")]
        before = store.to_dict()
        for payload in ({"version": 99, "triggers": []},
                        {"settings": {"volume": 42}, "triggers": [{"name": []}]},
                        {"triggers": [None]}, {"triggers": [{"cooldown_s": float("nan")}]},
                        {"triggers": [{"enabled": "yes"}]}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                store.load_dict(payload)
            self.assertEqual(store.to_dict(), before)

    def test_valid_unfinished_local_edit_survives_reload(self):
        store = TriggerStore()
        store.load_dict({"triggers": [Trigger(pattern="", enabled=False).to_dict()]})
        self.assertEqual(store.triggers[0].pattern, "")
        self.assertFalse(store.triggers[0].enabled)

    def test_concurrent_writers_use_distinct_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "triggers.json"
            replacements = []
            original_replace = os.replace

            def replace(source, destination):
                replacements.append(str(source))
                original_replace(source, destination)

            def save(index):
                store = TriggerStore(path)
                store.triggers = [Trigger(name=f"Timer {index}", pattern="hits")]
                store.save()

            with patch("mnmparse.triggers.os.replace", side_effect=replace):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    list(executor.map(save, range(16)))
            self.assertEqual(len(set(replacements)), 16)
            self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["triggers"]), 1)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])

    def test_failed_replace_preserves_previous_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as folder:
            store = TriggerStore(Path(folder) / "triggers.json")
            store.triggers = [Trigger(name="Before", pattern="hits")]
            store.save()
            original = store.path.read_bytes()
            store.triggers[0].name = "After"
            with patch("mnmparse.triggers.os.replace", side_effect=PermissionError("Locked")):
                with self.assertRaises(PermissionError):
                    store.save()
            self.assertEqual(store.path.read_bytes(), original)
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])


class TriggerPresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        from mnmparse.app.triggers_runtime import TriggerRunner
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TriggerStore(Path(self.tmp.name) / "triggers.json")
        self.trigger = Trigger(name="Peer99 is weak", pattern=r"(?P<who>Peer99) hits for (?P<damage>\d+)",
                               mode="regex", action="speak", speech="{line}; {who} {damage}",
                               timer=True, timer_label="{who} {damage}", timer_warn_s=2,
                               timer_warn_action="speak", timer_warn_speech="Peer99 {label}",
                               timer_end_action="speak", timer_end_speech="Peer99 {name}")
        self.store.triggers = [self.trigger]
        self.runner = TriggerRunner(self.store)
        self.runner.audio.run = Mock()
        self.runner.audio.voices = lambda: []
        self.runner.audio.devices = lambda: []
        self.page = None

    def tearDown(self):
        if self.page is not None:
            self.page._dirty = False
            self.page._save_timer.stop()
            self.page.close()
            self.page.deleteLater()
        self.runner._clock.stop()
        self.runner.audio.stop()
        self.runner.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def make_page(self, *, full=False):
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage
        cfg = Config(casual_mode=not full, casual_mode_confirmed=full)
        self.page = TriggersPage(_MissingEngine(cfg), cfg,
                                 QSettings(str(Path(self.tmp.name) / "test.ini"), QSettings.Format.IniFormat))
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.set_runner(self.runner)
        return self.page

    def test_default_mode_sanitizes_signal_return_timer_and_speech(self):
        signals = []
        self.runner.fired.connect(signals.append)
        result = self.runner.observe("Peer99 hits for 17", now=10)
        self.assertEqual(self.runner.board.timers[0].label, "Timer")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Timer")
        for match in signals + result:
            self.assertEqual(match.groups, {})
            self.assertNotIn("Peer99", json.dumps(dataclasses.asdict(match)))
            self.assertNotIn("17", match.line)
        timer = self.runner.board.timers[0]
        self.runner._alert(timer, "warn")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Timer soon")
        self.runner._alert(timer, "end")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Timer ended")

    def test_confirmed_mode_is_required_and_returning_cancels_audio(self):
        self.runner.set_config(SimpleNamespace(casual_mode=False, casual_mode_confirmed=False))
        self.runner.observe("Peer99 hits for 17", now=10)
        self.assertEqual(self.runner.board.timers[0].label, "Timer")
        self.runner.set_config(SimpleNamespace(casual_mode=False, casual_mode_confirmed=True))
        self.runner.observe("Peer99 hits for 29", now=11)
        self.assertEqual(self.runner.board.timers[0].label, "Peer99 29")
        self.runner.audio._speech_queue = [("Peer99 queued speech", 80, "timer:old")]
        self.runner.set_casual_mode(True)
        self.assertEqual(self.runner.audio._speech_queue, [])
        self.assertEqual(self.runner.board.timers[0].label, "Timer")

    def test_casual_file_action_is_silent_and_manual_timer_keeps_its_authored_name(self):
        self.trigger.action = "file"
        self.trigger.file = "Peer99.wav"
        self.runner.observe("Peer99 hits for 17", now=10)
        self.assertEqual(self.runner.audio.run.call_args.args[0], "none")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["file"], "")
        self.assertEqual(self.runner.start_one_time_timer("Peer99", 10).label, "Peer99")

    def test_muting_survives_save_and_unmute_restores_master(self):
        self.store.volume = 37
        self.runner.set_muted(True)
        self.assertTrue(self.runner.muted)
        self.assertEqual(self.runner.audio.master, 0)
        self.runner.save()
        self.assertEqual(self.runner.audio.master, 0)
        self.runner.set_muted(False)
        self.assertAlmostEqual(self.runner.audio.master, .37)

    def test_switching_mode_scrubs_every_editor_surface_and_dialog_copy(self):
        from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog, _summary
        page = self.make_page(full=True)
        page.add_recent_line("Peer99 hits for 17")
        page._refresh_completions()
        page._on_fired(Match(self.trigger, "Peer99 hits for 17", "Peer99"))
        page.test_line.setText("Peer99 hits for 17")
        cfg = page._cfg
        dialog = TriggerChatExportDialog(self.trigger, "private-share-code", cfg=cfg)
        QApplication.clipboard().setText("unchanged")
        cfg.casual_mode = True
        cfg.casual_mode_confirmed = False
        page.set_config(cfg)
        dialog._copy_line()
        self.assertEqual(QApplication.clipboard().text(), "unchanged")
        self.assertEqual(dialog.line.toPlainText(), "")
        self.assertFalse(page.editor.isEnabled())
        self.assertEqual(page.pattern.text(), "")
        self.assertEqual(page.speech.text(), "")
        self.assertEqual(page.test_line.text(), "")
        self.assertEqual(page.recent.count(), 0)
        self.assertEqual(page._completer_model.stringList(), [])
        self.assertNotIn("Peer99", page.list.item(0).text())
        self.assertNotIn("Peer99", page.list.item(0).toolTip())
        self.assertNotIn("Peer99", _summary(self.trigger, "Peer99", cfg))
        dialog.close()
        dialog.deleteLater()

    def test_casual_export_is_blocked_before_chooser(self):
        page = self.make_page()
        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName", side_effect=AssertionError("Export chooser opened")):
            page._on_export()
        page._on_export_chat()
        self.assertIn("Carebear Mode", page.sharing_status.text())
        self.assertIsNone(page._chat_export_dialog)

    def test_export_rechecks_policy_after_nested_chooser(self):
        page = self.make_page(full=True)
        destination = Path(self.tmp.name) / "Peer99-timers.json"

        def choose(*_args, **_kwargs):
            page.set_config(Config())
            return str(destination), ""

        with patch("mnmparse.app.triggers_page.QFileDialog.getSaveFileName", side_effect=choose):
            page._on_export()
        self.assertFalse(destination.exists())
        self.assertIn("Carebear Mode", page.sharing_status.text())
        self.assertNotIn("Peer99", page.sharing_status.text())

    def test_presentation_sharing_helpers_fail_closed_without_confirmed_policy(self):
        from mnmparse.trigger_chat import encode_visible_trigger
        from mnmparse.trigger_exchange import export_visible_trigger_file, TriggerExchangeError
        trigger = Trigger(name="Peer99", pattern="Peer99")
        destination = Path(self.tmp.name) / "shared.json"
        for cfg in (None, Config(), Config(casual_mode=False, casual_mode_confirmed=False)):
            with self.subTest(cfg=cfg):
                with self.assertRaisesRegex(TriggerExchangeError, "Carebear Mode"):
                    encode_visible_trigger(trigger, cfg=cfg)
                with self.assertRaisesRegex(TriggerExchangeError, "Carebear Mode"):
                    export_visible_trigger_file(destination, [trigger], cfg=cfg)
                self.assertFalse(destination.exists())
        full = Config(casual_mode=False, casual_mode_confirmed=True)
        self.assertTrue(encode_visible_trigger(trigger, cfg=full).startswith("PNUT1 "))
        export_visible_trigger_file(destination, [trigger], cfg=full)
        self.assertIn("Peer99", destination.read_text())

    def test_replacing_config_scrubs_pending_share_and_blocks_stale_copy(self):
        from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog
        page = self.make_page(full=True)
        dialog = TriggerChatExportDialog(self.trigger, "private-share-code", cfg=page._cfg)
        page._chat_export_dialog = dialog
        QApplication.clipboard().setText("unchanged")
        page.set_config(Config())  # Different object, not mutation of the old full Config.
        dialog._copy_line()
        self.assertEqual(QApplication.clipboard().text(), "unchanged")
        self.assertEqual(dialog.title.text(), "Timer")
        self.assertEqual(dialog.line.toPlainText(), "")
        self.assertEqual(dialog.code, "")
        dialog.deleteLater()
        page._chat_export_dialog = None

    def test_reapplying_casual_stops_preexisting_unsafe_audio(self):
        self.runner.audio._speech_queue = [("Peer99", 80, "")]
        self.runner.set_config(Config())
        self.assertEqual(self.runner.audio._speech_queue, [])

    def test_repeated_runner_binding_does_not_duplicate_matches_or_warning_saves(self):
        page = self.make_page(full=True)
        page.set_runner(self.runner)
        page.set_runner(self.runner)
        self.runner.fired.emit(Match(self.trigger, "Peer99 hits for 17", "Peer99"))
        self.assertEqual(page.recent.count(), 1)
        with patch.object(page, "_schedule_save") as schedule:
            self.runner.matching_warning.emit("Matching time limit exceeded")
            schedule.assert_called_once()

    def test_constructing_and_configuring_runner_never_plays_siren_or_audio(self):
        from mnmparse.app.triggers_runtime import AudioOut, TriggerRunner
        with patch.object(AudioOut, "run") as play, patch.object(AudioOut, "play_builtin") as cue:
            other = TriggerRunner(TriggerStore())
            other.set_config(Config())
            play.assert_not_called()
            cue.assert_not_called()
            other.deleteLater()

    def test_flush_saves_latest_edit_before_debounce_and_failure_stays_dirty(self):
        page = self.make_page(full=True)
        original_save = self.runner.save
        page.name.setText("Edited")
        page.name.textEdited.emit("Edited")
        self.assertTrue(page._save_timer.isActive())
        with patch.object(self.runner, "save", side_effect=PermissionError("Disk locked")):
            self.assertFalse(page.flush_pending_changes())
        self.assertTrue(page._dirty)
        self.assertFalse(page.retry_save.isHidden())
        self.assertIn("could not be saved", page.save_status.text())
        self.runner.save = original_save
        self.assertTrue(page.flush_pending_changes())
        self.assertFalse(page._dirty)
        self.assertFalse(page._save_timer.isActive())
        saved = TriggerStore(self.store.path)
        self.assertTrue(saved.load())
        self.assertEqual(saved.triggers[0].name, "Edited")

    def test_delete_undo_restores_original_identity_position_and_definition(self):
        earlier = Trigger(name="Earlier", pattern="early")
        later = Trigger(name="Later", pattern="later")
        self.store.triggers = [earlier, self.trigger, later]
        page = self.make_page(full=True)
        page._current = self.trigger
        page._refill_list()
        original = self.trigger.to_dict()
        page._on_delete()
        self.assertEqual(self.store.triggers, [earlier, later])
        self.assertTrue(page.undo_delete.isEnabled())
        page._on_undo_delete()
        self.assertEqual([t.id for t in self.store.triggers], [earlier.id, self.trigger.id, later.id])
        self.assertEqual(self.store.triggers[1].to_dict(), original)
        self.assertTrue(page.flush_pending_changes())

    def test_timed_out_manual_preview_cannot_fall_back_to_firing(self):
        self.runner.set_casual_mode(False)
        slow = Trigger(pattern=r"(?:(?:a+)+)+$(?#preview)", mode="regex")
        self.runner.test(slow, "a" * 6000 + "!")
        self.runner.audio.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
