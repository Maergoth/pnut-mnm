"""The supplied Fizzle recording is portable and existing trigger profiles stay intact."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.triggers_runtime import AudioOut, TriggerRunner, builtin_sound_path
from mnmparse.trigger_presets import FIZZLE_ID, PRESETS
from mnmparse.triggers import BUILTIN_SOUNDS, Trigger, TriggerStore, match_trigger

ASSET_SHA = "cc1fa5c8b23bd4d227ac7a7c6e890012f80e45e9c2593200f9c6914357738947"


class FizzleStarterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_supplied_mp3_is_unchanged_and_resolves_from_source_and_frozen_bundle(self):
        self.assertIn("Fizzle", BUILTIN_SOUNDS)
        source = builtin_sound_path("Fizzle")
        self.assertEqual(source.name, "fizzle.mp3")
        self.assertEqual(source.stat().st_size, 7457)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), ASSET_SHA)
        with tempfile.TemporaryDirectory() as temporary:
            bundled = Path(temporary) / "assets" / "bundled_sounds" / "fizzle.mp3"
            bundled.parent.mkdir(parents=True)
            shutil.copyfile(source, bundled)
            with patch("mnmparse.app.triggers_runtime.sys._MEIPASS", temporary, create=True):
                self.assertEqual(builtin_sound_path("Fizzle"), bundled)
                self.assertEqual(hashlib.sha256(builtin_sound_path("Fizzle").read_bytes()).hexdigest(), ASSET_SHA)

    def test_fresh_default_retains_local_match_volume_and_no_timer_behavior(self):
        store = TriggerStore()
        store.install_presets()
        trigger = store.find(FIZZLE_ID)
        self.assertIsNotNone(trigger)
        self.assertEqual((trigger.pattern, trigger.mode, trigger.fuzzy, trigger.enabled),
                         ("Your spell fizzles!", "contains", True, True))
        self.assertEqual((trigger.action, trigger.sound, trigger.volume, trigger.cooldown_s, trigger.timer),
                         ("sound", "Fizzle", 80, 0, False))
        self.assertFalse(trigger.file)
        for text in ("Your spell fizzles!", "YOUR SPELL FIZZLES!", "Your speli fizzles!"):
            self.assertIsNotNone(match_trigger(trigger, text), text)
        for text in ("A ghoul's spell fizzles!", "You resisted a spell!", "Your spell succeeds!"):
            self.assertIsNone(match_trigger(trigger, text), text)

    def test_existing_profiles_and_custom_fizzle_are_preserved_until_explicit_restore(self):
        for rows in ([], [Trigger(id=FIZZLE_ID, name="My fizzle", pattern="custom", enabled=False,
                                 action="file", file="Z:/my-sound.mp3").to_dict()]):
            with self.subTest(rows=rows):
                store = TriggerStore()
                store.load_dict({"triggers": rows})
                store.install_presets()
                existing = [trigger.to_dict() for trigger in store.triggers if trigger.id == FIZZLE_ID]
                self.assertEqual(existing, rows)
                store.installed_presets.difference_update(preset["id"] for preset in PRESETS)
                store.install_presets(include_first_install_only=True)
                if rows:
                    self.assertEqual(store.find(FIZZLE_ID).to_dict(), rows[0])
                else:
                    self.assertEqual(store.find(FIZZLE_ID).sound, "Fizzle")

    def test_runtime_routes_exact_bundled_mp3_through_existing_file_audio_without_timer(self):
        store = TriggerStore()
        store.install_presets()
        store.triggers = [store.find(FIZZLE_ID)]
        runner = TriggerRunner(store)
        try:
            with patch.object(runner.audio, "play_file") as play:
                self.assertEqual(len(runner.observe("Your spell fizzles!", 100)), 1)
                play.assert_called_once_with(str(builtin_sound_path("Fizzle")), 80)
                self.assertEqual(runner.board.timers, [])
        finally:
            runner.close()
            runner.deleteLater()

    def test_bundled_sound_delegation_does_not_replace_configured_master_or_output(self):
        audio = AudioOut()
        audio.master, audio.device = .37, "Sample output"
        try:
            with patch.object(audio, "play_file") as play:
                audio.play_builtin("Fizzle", 80)
                play.assert_called_once_with(str(builtin_sound_path("Fizzle")), 80)
            self.assertEqual((audio.master, audio.device), (.37, "Sample output"))
        finally:
            audio.stop()
            audio.deleteLater()
