"""Starter match corrections update old defaults without resetting personal timers."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mnmparse.trigger_presets import PRESETS, RESIST_ID, RESIST_PATTERN
from mnmparse.triggers import Trigger, TriggerStore, match_trigger


INVIS_ID = "637b7466b6"
PHRASE = "You begin to feel yourself appearing"


def previously_seeded(*triggers: Trigger) -> TriggerStore:
    store = TriggerStore()
    store.installed_presets = {preset["id"] for preset in PRESETS}
    store.triggers = list(triggers)
    return store


class PresetMigrationTests(unittest.TestCase):
    def test_fresh_resist_is_immediate_speech_for_own_outgoing_resists(self) -> None:
        store = TriggerStore()
        store.install_presets()
        trigger = store.find(RESIST_ID)
        self.assertIsNotNone(trigger)
        self.assertTrue(trigger.enabled)
        self.assertEqual((trigger.mode, trigger.pattern, trigger.action, trigger.speech),
                         ("regex", RESIST_PATTERN, "speak", "resist"))
        self.assertFalse(trigger.timer)
        for line in ("a gorged ghoul resists your Fear the Dead!",
                     "a bone carver resists your Mallet of Light!",
                     "A Rat RESISTS   YOUR spell!"):
            self.assertIsNotNone(match_trigger(trigger, line))
        for line in ("a scarer tries to cast Fear on YOU, but is resisted!",
                     "Your magical resistance has increased.", "A rat resists another caster's spell."):
            self.assertIsNone(match_trigger(trigger, line))
        self.assertFalse(store.install_presets())

    def test_existing_profiles_do_not_receive_new_resist_default(self) -> None:
        for data in ({"triggers": []}, {"triggers": [Trigger(name="Personal", pattern="my phrase").to_dict()]},
                     {"triggers": [], "installed_presets": [preset["id"] for preset in PRESETS if preset["id"] != RESIST_ID]}):
            with self.subTest(data=data):
                store = TriggerStore()
                store.load_dict(data)
                before = [trigger.to_dict() for trigger in store.triggers]
                store.install_presets()
                self.assertIsNone(store.find(RESIST_ID))
                self.assertTrue(all(row in [trigger.to_dict() for trigger in store.triggers] for row in before))
                self.assertIn(RESIST_ID, store.installed_presets)
                self.assertFalse(store.install_presets())

    def test_resist_deletion_and_existing_profile_skip_survive_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "triggers.json"
            store = TriggerStore(path)
            store.install_presets()
            store.triggers = [trigger for trigger in store.triggers if trigger.id != RESIST_ID]
            store.save()
            restarted = TriggerStore(path)
            self.assertTrue(restarted.load())
            self.assertFalse(restarted.install_presets())
            self.assertIsNone(restarted.find(RESIST_ID))

    def test_explicit_restore_can_add_the_new_starter_to_an_existing_profile(self) -> None:
        store = TriggerStore()
        store.load_dict({"triggers": []})
        store.install_presets()
        self.assertIsNone(store.find(RESIST_ID))
        store.installed_presets.difference_update(preset["id"] for preset in PRESETS)
        self.assertTrue(store.install_presets(include_first_install_only=True))
        self.assertIsNotNone(store.find(RESIST_ID))

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
