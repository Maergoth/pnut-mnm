"""Starter match corrections update old defaults without resetting personal timers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mnmparse.trigger_presets import PRESETS
from mnmparse.triggers import Trigger, TriggerStore, match_trigger


INVIS_ID = "637b7466b6"
PHRASE = "You begin to feel yourself appearing"


def previously_seeded(*triggers: Trigger) -> TriggerStore:
    store = TriggerStore()
    store.installed_presets = {preset["id"] for preset in PRESETS}
    store.triggers = list(triggers)
    return store


class PresetMigrationTests(unittest.TestCase):
    def test_fresh_invis_break_matches_the_full_actual_phrase(self) -> None:
        store = TriggerStore()
        self.assertTrue(store.install_presets())
        trigger = store.find(INVIS_ID)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger.pattern, PHRASE)
        self.assertIsNotNone(match_trigger(trigger, f"{PHRASE}."))
        self.assertIsNone(match_trigger(trigger, "A hidden creature begins to appear."))
        self.assertFalse(store.install_presets())

    def test_original_match_is_corrected_without_changing_custom_timer_or_audio_settings(self) -> None:
        trigger = Trigger(
            id=INVIS_ID, name="My invis warning", pattern="to appear", mode="contains",
            fuzzy=False, enabled=False, action="file", file="my-sound.wav", volume=35,
            speech="My speech", cooldown_s=4, timer=True, timer_seconds=22,
            timer_mode="retain", timer_label="Invisible", timer_color="#123456",
            timer_warn_s=3, timer_warn_action="speak", timer_warn_speech="Almost visible",
            timer_end_action="none", category="Personal",
        )
        before = trigger.to_dict()
        store = previously_seeded(trigger)
        self.assertTrue(store.install_presets())
        self.assertIs(store.find(INVIS_ID), trigger)
        self.assertEqual(trigger.to_dict(), {**before, "pattern": PHRASE})
        self.assertFalse(store.install_presets())

    def test_custom_patterns_and_match_modes_are_preserved(self) -> None:
        for pattern, mode in (("my custom pattern", "contains"), ("TO APPEAR", "contains"),
                              ("to appear", "exact"), ("to appear", "starts"), ("to appear", "regex")):
            with self.subTest(pattern=pattern, mode=mode):
                trigger = Trigger(id=INVIS_ID, pattern=pattern, mode=mode)
                before = trigger.to_dict()
                store = previously_seeded(trigger)
                self.assertTrue(store.install_presets(), "record that this migration was considered")
                self.assertEqual(trigger.to_dict(), before)
                self.assertFalse(store.install_presets())

    def test_deleted_preset_and_unrelated_same_name_import_stay_untouched(self) -> None:
        imported = Trigger(id="another-id", name="Invis Break", pattern="to appear")
        store = previously_seeded(imported)
        before = imported.to_dict()
        self.assertTrue(store.install_presets())
        self.assertIsNone(store.find(INVIS_ID), "a deleted default must not be recreated")
        self.assertEqual([t.to_dict() for t in store.triggers], [before])
        self.assertFalse(store.install_presets())

    def test_only_matching_stable_id_is_migrated(self) -> None:
        imported = Trigger(id="another-id", name="Invis Break", pattern="to appear")
        original = Trigger(id=INVIS_ID, name="Renamed", pattern="to appear")
        store = previously_seeded(imported, original)
        store.install_presets()
        self.assertEqual(original.pattern, PHRASE)
        self.assertEqual(imported.pattern, "to appear")

    def test_one_time_marker_survives_save_and_preserves_later_deliberate_revert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            original = Trigger(id=INVIS_ID, pattern="to appear")
            store = previously_seeded(original)
            store.path = Path(tmp) / "triggers.json"
            store.install_presets()
            original.pattern = "to appear"  # deliberate edit after the correction was applied
            store.save()
            again = TriggerStore(store.path)
            self.assertTrue(again.load())
            self.assertFalse(again.install_presets())
            self.assertEqual(again.find(INVIS_ID).pattern, "to appear")


if __name__ == "__main__":
    unittest.main()
