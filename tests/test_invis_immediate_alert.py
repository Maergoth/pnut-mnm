"""Invis Break warns immediately without its old countdown or delayed alerts."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.trigger_presets import (
    INVIS_BREAK_ALERT_REVISION,
    INVIS_BREAK_ID,
    INVIS_BREAK_PATTERN,
    INVIS_BREAK_PATTERN_REVISION,
    PRESETS,
)
from mnmparse.triggers import Trigger, TriggerStore


def legacy_invis(**changes) -> Trigger:
    """The old shipped definition, independent of the new preset defaults."""
    values = {
        "id": INVIS_BREAK_ID, "name": "Invis Break", "pattern": INVIS_BREAK_PATTERN,
        "action": "speak", "sound": "Falling", "speech": "InvisBreak",
        "cooldown_s": 2.0, "timer": True, "timer_seconds": 30.0,
        "timer_warn_s": 1.0, "timer_warn_action": "speak",
    }
    values.update(changes)
    return Trigger(**values)


def seeded_store(*triggers: Trigger) -> TriggerStore:
    store = TriggerStore()
    store.triggers = list(triggers)
    store.installed_presets = {str(preset["id"]) for preset in PRESETS}
    store.installed_presets.add(INVIS_BREAK_PATTERN_REVISION)
    return store


class InvisImmediateMigrationTests(unittest.TestCase):
    def assert_immediate_only(self, trigger: Trigger) -> None:
        self.assertFalse(trigger.timer)
        self.assertEqual(trigger.timer_warn_s, 0)
        self.assertEqual(trigger.timer_warn_action, "none")
        self.assertEqual(trigger.timer_end_action, "none")

    def test_fresh_preset_plays_falling_sound_without_countdown(self) -> None:
        store = TriggerStore()
        store.install_presets()
        trigger = store.find(INVIS_BREAK_ID)
        self.assertIsNotNone(trigger)
        self.assert_immediate_only(trigger)
        self.assertEqual((trigger.action, trigger.sound), ("sound", "Falling"))
        self.assertEqual(trigger.pattern, INVIS_BREAK_PATTERN)
        self.assertFalse(store.install_presets())

    def test_old_preset_loses_only_countdown_and_delayed_alerts(self) -> None:
        trigger = legacy_invis(
            name="My invis alert", enabled=False, action="file", file="my-warning.wav",
            speech="Custom immediate speech", volume=29, cooldown_s=4,
            timer_color="#123456", category="Personal",
        )
        before = trigger.to_dict()
        store = seeded_store(trigger)
        self.assertTrue(store.install_presets())
        self.assert_immediate_only(trigger)
        self.assertEqual(trigger.to_dict(), {
            **before, "timer": False, "timer_warn_s": 0.0,
            "timer_warn_action": "none", "timer_end_action": "none",
        })
        self.assertIn(INVIS_BREAK_ALERT_REVISION, store.installed_presets)
        self.assertFalse(store.install_presets())

    def test_users_starts_match_and_disabled_delayed_alerts_are_migrated(self) -> None:
        trigger = legacy_invis(mode="starts", timer_warn_s=0, timer_end_action="none")
        before = trigger.to_dict()
        store = seeded_store(trigger)
        store.install_presets()
        self.assert_immediate_only(trigger)
        self.assertEqual(trigger.to_dict(), {
            **before, "timer": False, "timer_warn_s": 0.0,
            "timer_warn_action": "none", "timer_end_action": "none",
        })

    def test_original_short_phrase_receives_both_corrections(self) -> None:
        trigger = legacy_invis(pattern="to appear")
        store = seeded_store(trigger)
        store.installed_presets.discard(INVIS_BREAK_PATTERN_REVISION)
        store.install_presets()
        self.assertEqual(trigger.pattern, INVIS_BREAK_PATTERN)
        self.assert_immediate_only(trigger)

    def test_custom_countdowns_matches_and_delayed_cues_are_preserved(self) -> None:
        customizations = (
            {"timer_seconds": 22},
            {"timer_warn_s": 3},
            {"timer_warn_action": "sound"},
            {"timer_warn_speech": "My custom warning"},
            {"timer_end_action": "speak"},
            {"timer_end_sound": "Alert"},
            {"pattern": "my custom phrase"},
            {"mode": "regex"},
            {"mode": "exact"},
        )
        for changes in customizations:
            with self.subTest(changes=changes):
                trigger = legacy_invis(**changes)
                before = trigger.to_dict()
                store = seeded_store(trigger)
                self.assertTrue(store.install_presets())
                self.assertEqual(trigger.to_dict(), before)
                self.assertIn(INVIS_BREAK_ALERT_REVISION, store.installed_presets)
                self.assertFalse(store.install_presets())

    def test_deleted_preset_and_same_name_import_remain_untouched(self) -> None:
        imported = legacy_invis(id="imported-invis")
        other = Trigger(name="Another timer", pattern="foo", timer=True)
        store = seeded_store(imported, other)
        before = [trigger.to_dict() for trigger in store.triggers]
        store.install_presets()
        self.assertIsNone(store.find(INVIS_BREAK_ID))
        self.assertEqual([trigger.to_dict() for trigger in store.triggers], before)
        self.assertIn(INVIS_BREAK_ALERT_REVISION, store.installed_presets)
        self.assertFalse(store.install_presets())

    def test_marker_survives_reload_and_allows_deliberate_timer_reenable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trigger = legacy_invis()
            store = seeded_store(trigger)
            store.path = Path(directory) / "triggers.json"
            store.install_presets()
            trigger.timer = True
            trigger.timer_warn_s = 1
            trigger.timer_warn_action = "speak"
            trigger.timer_end_action = "sound"
            deliberately_edited = trigger.to_dict()
            store.save()
            reloaded = TriggerStore(store.path)
            self.assertTrue(reloaded.load())
            self.assertFalse(reloaded.install_presets())
            self.assertEqual(reloaded.find(INVIS_BREAK_ID).to_dict(), deliberately_edited)


@unittest.skipUnless(sys.platform == "win32", "Qt is exercised on the Windows build")
class InvisImmediateRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def test_trigger_fires_once_without_timer_or_later_audio(self) -> None:
        from mnmparse.app.triggers_runtime import TriggerRunner

        for migrated in (False, True):
            with self.subTest(migrated=migrated):
                store = seeded_store(legacy_invis()) if migrated else TriggerStore()
                store.install_presets()
                with patch("mnmparse.app.triggers_runtime.AudioOut") as audio_type:
                    runner = TriggerRunner(store)
                    runner.set_casual_mode(False)  # This fixture exercises confirmed full-mode labels.
                    audio = audio_type.return_value
                    self.addCleanup(runner._clock.stop)
                    fired = []
                    runner.fired.connect(fired.append)
                    matches = runner.observe(INVIS_BREAK_PATTERN + ".", now=100)
                    self.assertEqual(len(matches), 1)
                    self.assertEqual(len(fired), 1)
                    audio.run.assert_called_once()
                    self.assertEqual(audio.run.call_args.args[0], "speak" if migrated else "sound")
                    self.assertEqual(audio.run.call_args.kwargs["sound"], "Falling")
                    self.assertEqual(runner.board.timers, [])
                    self.assertFalse(runner._clock.isActive())
                    with patch("mnmparse.triggers.time.time", return_value=129):
                        runner._tick()
                    with patch("mnmparse.triggers.time.time", return_value=130):
                        runner._tick()
                    audio.run.assert_called_once()

    def test_restore_starters_keeps_migration_markers_and_personal_reenable(self) -> None:
        from mnmparse.app.pages import TriggersPage

        trigger = legacy_invis()
        store = seeded_store(trigger)
        store.install_presets()
        trigger.timer = True
        trigger.timer_warn_s = 1
        trigger.timer_warn_action = "speak"
        trigger.timer_end_action = "sound"
        before = trigger.to_dict()
        page = SimpleNamespace(
            runner=SimpleNamespace(store=store), _refill_list=Mock(), _schedule_save=Mock(),
        )
        TriggersPage._restore_presets(page)
        self.assertEqual(trigger.to_dict(), before)
        self.assertIn(INVIS_BREAK_ALERT_REVISION, store.installed_presets)
        self.assertIn(INVIS_BREAK_PATTERN_REVISION, store.installed_presets)
        self.assertIsNotNone(store.find("a8089904bf"), "missing Gatekick should be restored")
        self.assertIsNotNone(store.find("357af85f56"), "missing Healkick should be restored")
        page._refill_list.assert_called_once()
        page._schedule_save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
