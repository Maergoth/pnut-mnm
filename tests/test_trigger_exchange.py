"""Public timer bundles: conversion, validation, safe merging and atomic export."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mnmparse.trigger_exchange import (
    FORMAT, VERSION, TriggerExchangeError, export_trigger_file,
    external_sound_files, merge_triggers, read_trigger_file,
)
from mnmparse.triggers import Trigger, TriggerStore


class ExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "shared-timers.json"
        self.timer = Trigger(name="Pacify", pattern="You begin casting Lesser Pacify", timer=True,
                             timer_seconds=110, timer_mode="retain", enabled=False,
                             timer_color="#123456", timer_warn_color="#aabbcc", timer_low_color="#FF0000",
                             timer_low_s=3, timer_warn_s=5, timer_warn_action="speak",
                             timer_warn_speech="Paci soon", timer_end_action="speak",
                             timer_end_speech="Paci faded", category="Control")

    def write(self, data: object) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    def test_all_settings_survive_public_round_trip_without_global_audio(self) -> None:
        export_trigger_file(self.path, [self.timer])
        bundle = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(set(bundle), {"format", "version", "triggers"})
        self.assertEqual((bundle["format"], bundle["version"]), (FORMAT, VERSION))
        self.assertEqual(read_trigger_file(self.path)[0].to_dict(), self.timer.to_dict())

    def test_reads_old_full_store_and_converts_modes_and_defaults(self) -> None:
        self.write({"version": 1, "settings": {"volume": 12, "voice": "Private voice"},
                    "installed_presets": ["local"], "triggers": [
                        {"name": "One", "pattern": "first", "timer_mode": "restart"},
                        {"name": "Two", "pattern": "second", "timer_mode": "ignore"},
                    ]})
        triggers = read_trigger_file(self.path)
        self.assertEqual([item.timer_mode for item in triggers], ["replace", "retain"])
        self.assertEqual(triggers[0].timer_low_s, 5)
        self.assertEqual(triggers[0].timer_color, "")
        self.assertEqual(triggers[0].volume, 80)
        self.assertTrue(all(item.id for item in triggers))

    def test_reads_single_trigger_and_legacy_list_and_utf8_bom(self) -> None:
        for data in (self.timer.to_dict(), [self.timer.to_dict()]):
            with self.subTest(shape=type(data)):
                self.path.write_text(json.dumps(data), encoding="utf-8-sig")
                self.assertEqual(read_trigger_file(self.path)[0], self.timer)

    def test_bad_shapes_and_versions_are_rejected(self) -> None:
        for data in (None, True, "timer", {}, {"settings": {}}, {"triggers": {}},
                     {"triggers": [None]}, {"triggers": ["bad"]}, {"triggers": [{}]},
                     {"triggers": [], "version": 2}, {"triggers": [], "version": True},
                     {"triggers": [], "format": "other"}, {"triggers": [], "format": FORMAT}):
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(TriggerExchangeError):
                    read_trigger_file(self.path)

    def test_invalid_fields_are_rejected_instead_of_silently_repaired(self) -> None:
        values = {"enabled": "false", "fuzzy": 0, "timer": [], "name": {}, "id": "",
                  "pattern": None, "volume": 101, "timer_seconds": 0, "cooldown_s": -1,
                  "timer_low_s": True, "timer_warn_s": "5", "mode": "unknown",
                  "action": "run-program", "timer_mode": "unknown", "timer_color": "red",
                  "timer_warn_action": "unknown", "timer_end_sound": "unknown", "unknown": 1}
        for key, value in values.items():
            with self.subTest(field=key, value=value):
                self.write({**self.timer.to_dict(), key: value})
                with self.assertRaises(TriggerExchangeError):
                    read_trigger_file(self.path)

    def test_nonfinite_and_overflowing_numbers_rejected(self) -> None:
        for value in (float("nan"), float("inf"), -float("inf"), 10 ** 400):
            with self.subTest(value=value):
                self.write({**self.timer.to_dict(), "timer_seconds": value})
                with self.assertRaisesRegex(TriggerExchangeError, "finite"):
                    read_trigger_file(self.path)
        self.path.write_text('[{"pattern":"hit","timer_seconds":1e9999}]', encoding="utf-8")
        with self.assertRaisesRegex(TriggerExchangeError, "finite"):
            read_trigger_file(self.path)

    def test_invalid_regex_and_missing_file_are_actionable(self) -> None:
        self.write({**self.timer.to_dict(), "mode": "regex", "pattern": "("})
        with self.assertRaisesRegex(TriggerExchangeError, "regular expression is invalid"):
            read_trigger_file(self.path)
        self.write({**self.timer.to_dict(), "action": "file", "file": ""})
        with self.assertRaisesRegex(TriggerExchangeError, "Choose a sound file"):
            read_trigger_file(self.path)

    def test_time_values_larger_than_the_editor_can_represent_are_rejected(self) -> None:
        for field in ("timer_seconds", "timer_warn_s", "timer_low_s", "cooldown_s"):
            with self.subTest(field=field):
                self.write({**self.timer.to_dict(), field: 2 ** 31})
                with self.assertRaisesRegex(TriggerExchangeError, "must not exceed"):
                    read_trigger_file(self.path)

    def test_partial_valid_file_never_returns_partial_import(self) -> None:
        self.write([self.timer.to_dict(), {**self.timer.to_dict(), "volume": -5}])
        with self.assertRaisesRegex(TriggerExchangeError, "Timer 2"):
            read_trigger_file(self.path)

    def test_bad_json_encoding_duplicate_keys_and_io_have_readable_errors(self) -> None:
        for raw in (b'{"triggers":', b'\xff', b'{"triggers":[],"triggers":[]}'):
            self.path.write_bytes(raw)
            with self.assertRaises(TriggerExchangeError):
                read_trigger_file(self.path)
        with self.assertRaisesRegex(TriggerExchangeError, "Could not read"):
            read_trigger_file(self.path.with_name("missing.json"))

    def test_oversized_file_is_rejected_without_loading_all_of_it(self) -> None:
        self.path.write_bytes(b" " * 21)
        with patch("mnmparse.trigger_exchange.MAX_FILE_BYTES", 20):
            with self.assertRaisesRegex(TriggerExchangeError, "too large"):
                read_trigger_file(self.path)

    def test_duplicates_ignore_ids_name_case_and_numeric_format(self) -> None:
        duplicate = replace(self.timer, id="another", name="PACIFY", timer_seconds=110.0,
                            timer_low_color="#ff0000", timer_low_s=3.0)
        result = merge_triggers([self.timer], [duplicate, duplicate])
        self.assertEqual((result.added, result.skipped, result.conflicts), (0, 2, 0))
        self.assertEqual(result.triggers, [self.timer])
        self.assertIsNot(result.triggers[0], self.timer)

    def test_normalized_legacy_defaults_are_duplicates(self) -> None:
        old = {"name": "Gatekick", "pattern": "casting Gate", "timer_mode": "restart"}
        self.write(old)
        imported = read_trigger_file(self.path)
        local = Trigger(name="Gatekick", pattern="casting Gate", timer_mode="replace")
        result = merge_triggers([local], imported)
        self.assertEqual((result.added, result.skipped), (0, 1))

    def test_existing_unfinished_drafts_do_not_block_valid_imports(self) -> None:
        drafts = [Trigger(pattern=""), Trigger(name="Editing regex", mode="regex", pattern="(",
                                                timer_mode="restart", action="file", file="")]
        originals = [draft.to_dict() for draft in drafts]
        result = merge_triggers(drafts, [self.timer])
        self.assertEqual((result.added, result.skipped, result.conflicts), (1, 0, 0))
        self.assertEqual([trigger.to_dict() for trigger in result.triggers[:2]], originals)
        self.assertEqual([trigger.to_dict() for trigger in drafts], originals)
        for local, copied in zip(drafts, result.triggers):
            self.assertIsNot(copied, local)
        self.assertEqual(result.triggers[2], self.timer)

    def test_incoming_unfinished_drafts_remain_invalid(self) -> None:
        existing = Trigger(pattern="")
        with self.assertRaisesRegex(TriggerExchangeError, "pattern is empty"):
            merge_triggers([existing], [Trigger(pattern="")])
        with self.assertRaisesRegex(TriggerExchangeError, "pattern is empty"):
            export_trigger_file(self.path, [existing])

    def test_same_id_changed_content_preserves_both_and_repeat_import_skips(self) -> None:
        incoming = replace(self.timer, timer_seconds=90)
        original_existing, original_incoming = self.timer.to_dict(), incoming.to_dict()
        result = merge_triggers([self.timer], [incoming])
        self.assertEqual((result.added, result.skipped, result.conflicts), (1, 0, 1))
        self.assertEqual(result.triggers[0].id, self.timer.id)
        self.assertNotEqual(result.triggers[1].id, self.timer.id)
        self.assertEqual([timer.timer_seconds for timer in result.triggers], [110, 90])
        repeated = merge_triggers(result.triggers, [incoming])
        self.assertEqual((repeated.added, repeated.skipped, repeated.conflicts), (0, 1, 0))
        self.assertEqual(self.timer.to_dict(), original_existing)
        self.assertEqual(incoming.to_dict(), original_incoming)

    def test_same_name_different_content_is_retained_with_its_unique_id(self) -> None:
        incoming = replace(self.timer, name="PACIFY", id="second", timer_seconds=60)
        result = merge_triggers([self.timer], [incoming])
        self.assertEqual((result.added, result.conflicts), (1, 1))
        self.assertEqual([timer.id for timer in result.triggers], [self.timer.id, "second"])

    def test_disabled_and_custom_color_variants_are_not_lost(self) -> None:
        variants = [replace(self.timer, enabled=True), replace(self.timer, timer_low_color="#000000")]
        result = merge_triggers([self.timer], variants)
        self.assertEqual(result.added, 2)
        self.assertEqual(len({timer.id for timer in result.triggers}), 3)

    def test_export_does_not_open_or_package_sound_files(self) -> None:
        sound = replace(self.timer, action="file", file="Z:/private/voice.wav")
        export_trigger_file(self.path, [sound])
        self.assertEqual(read_trigger_file(self.path)[0].file, sound.file)
        self.assertEqual(external_sound_files([sound, sound, self.timer]), [sound.file])
        self.assertEqual([path.name for path in self.path.parent.iterdir()], [self.path.name])

    def test_atomic_export_failure_preserves_destination_and_cleans_temporary(self) -> None:
        self.path.write_text("original", encoding="utf-8")
        with patch("mnmparse.trigger_exchange.os.replace", side_effect=PermissionError("in use")):
            with self.assertRaisesRegex(TriggerExchangeError, "Could not export"):
                export_trigger_file(self.path, [self.timer])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_invalid_export_leaves_existing_file_intact(self) -> None:
        self.path.write_text("original", encoding="utf-8")
        with self.assertRaises(TriggerExchangeError):
            export_trigger_file(self.path, [self.timer, replace(self.timer, timer_seconds=float("nan"))])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_export_will_not_create_an_unreadably_large_bundle(self) -> None:
        self.path.write_text("original", encoding="utf-8")
        with patch("mnmparse.trigger_exchange.MAX_FILE_BYTES", 20):
            with self.assertRaisesRegex(TriggerExchangeError, "too large"):
                export_trigger_file(self.path, [self.timer])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "original")

    def test_merging_does_not_change_local_global_settings(self) -> None:
        local = TriggerStore()
        local.voice, local.output_device, local.volume = "My voice", "Headphones", 27
        local.installed_presets = {"installed"}
        local.triggers = [self.timer]
        before = local.to_dict()
        self.write({"version": 1, "settings": {"voice": "Someone else", "volume": 99},
                    "triggers": [replace(self.timer, id="new", name="Root", pattern="Root").to_dict()]})
        result = merge_triggers(local.triggers, read_trigger_file(self.path))
        self.assertEqual(result.added, 1)
        self.assertEqual(local.to_dict(), before)


if __name__ == "__main__":
    unittest.main()
