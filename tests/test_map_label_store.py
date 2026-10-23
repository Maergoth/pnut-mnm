"""Map-image labels persist independently without rewriting unrelated settings."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import UUID

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from mnmparse.map_labels import MAX_NAME, MAX_NOTES, MapLabel, MapLabelStore, for_settings, map_key


MAP = "https://wiki.example.test/images/a/ab/Fictional_Zone_Level1.png"
OTHER_MAP = "https://wiki.example.test/images/a/ab/Fictional_Zone_Level2.png"


def label(number=1, **changes):
    return replace(MapLabel(str(UUID(int=number)), MAP, .25, .75, "Fictional camp", "Bring supplies.", "camp"), **changes)


class MapLabelStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "labels.ini"
        self.handles, self.stores = [], []
        self.settings = self.handle()
        self.store = self.new_store()
        self.changes = []
        self.store.changed.connect(self.changes.append)

    def tearDown(self):
        for obj in self.stores + self.handles:
            obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.temp.cleanup()

    def handle(self):
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        settings.setFallbacksEnabled(False)
        self.handles.append(settings)
        return settings

    def new_store(self, settings=None):
        store = MapLabelStore(settings if settings is not None else self.handle())
        self.stores.append(store)
        return store

    def test_new_settings_handle_and_independent_store_reload_saved_label(self):
        original = label(id=UUID(int=1).hex, name="  Fictional camp  ", notes="  First\r\nSecond\rThird  ")
        self.store.save(original)
        self.assertTrue(self.path.is_file())
        self.assertGreater(self.path.stat().st_size, 0)
        reloaded = self.new_store(self.handle())
        self.assertIsNot(reloaded, self.store)
        expected = replace(original, id=str(UUID(int=1)), name="Fictional camp", notes="First\nSecond\nThird")
        self.assertEqual(reloaded.labels(MAP), [expected])
        self.assertEqual(reloaded.get(UUID(int=1).hex), expected)
        self.assertEqual(self.changes, [MAP])

    def test_edits_and_deletion_preserve_other_labels_and_settings_groups(self):
        self.settings.setValue("map/zone", "Fictional Zone")
        self.settings.setValue("other/group/key", "leave intact")
        first, second, upstairs = label(1), label(2, name="Fictional merchant", icon="merchant"), label(3, map_url=OTHER_MAP)
        for entry in (first, second, upstairs):
            self.store.save(entry)
        other = self.new_store()
        other.save(replace(second, notes="Edited by another map view."))
        self.store.save(replace(first, name="Updated camp"))
        self.store.remove(UUID(first.id).hex)
        reloaded = self.new_store()
        self.assertEqual(reloaded.labels(MAP), [replace(second, notes="Edited by another map view.")])
        self.assertEqual(reloaded.labels(OTHER_MAP), [upstairs])
        self.assertIsNone(reloaded.get(first.id))
        self.settings.sync()
        self.assertEqual(self.settings.value("map/zone"), "Fictional Zone")
        self.assertEqual(self.settings.value("other/group/key"), "leave intact")
        self.changes.clear()
        self.store.remove(first.id)
        self.store.remove("not a UUID")
        self.assertEqual(self.changes, [])

    def test_shared_factory_ignores_caller_group_and_settings_handle_identity(self):
        grouped = self.handle()
        grouped.beginGroup("zones/browser")
        grouped.setValue("selection", "Fictional Zone")
        other = self.handle()
        other.beginGroup("map")
        first = for_settings(grouped)
        self.stores.append(first)
        self.assertIs(first, for_settings(other))
        self.assertIs(first, MapLabelStore.for_settings(self.handle()))
        first.save(label())
        self.assertEqual(self.new_store().labels(MAP), [label()])
        disk = self.handle()
        disk.sync()
        self.assertIn("map_labels/" + label().id, disk.allKeys())
        self.assertFalse(any(key.startswith("zones/browser/map_labels/") for key in disk.allKeys()))
        self.assertEqual(disk.value("zones/browser/selection"), "Fictional Zone")

    def test_floor_variants_and_filename_case_remain_distinct_map_images(self):
        maps = (MAP, OTHER_MAP, MAP.replace("Level1.png", "Level1_Alternate.png"), MAP.replace("Level1", "level1"))
        for index, url in enumerate(maps, 1):
            self.store.save(label(index, map_url=url, name=f"Fictional marker {index}"))
        reloaded = self.new_store()
        for index, url in enumerate(maps, 1):
            self.assertEqual([entry.id for entry in reloaded.labels(url)], [str(UUID(int=index))])

    def test_original_and_thumbnail_query_urls_share_only_the_same_filename(self):
        thumbnail = "https://WIKI.EXAMPLE.TEST:443/images/thumb/a/ab/Fictional_Zone_Level1.png/320px-Fictional_Zone_Level1.png?fresh=1#section"
        self.assertEqual(map_key(thumbnail), MAP)
        self.assertEqual(map_key(MAP + "?revision=2#note"), MAP)
        self.store.save(label(map_url=thumbnail))
        self.assertEqual(self.store.labels(MAP), [label()])
        self.assertEqual(self.store.labels(MAP + "?another=3"), [label()])
        self.assertNotEqual(map_key(MAP), map_key(MAP.replace("Level1", "level1")))
        self.assertNotEqual(map_key(MAP), map_key(OTHER_MAP))

    def test_malformed_records_are_ignored_independently_of_valid_neighbors(self):
        self.store.save(label())
        invalid = [
            {"x": float("nan")}, {"x": True}, {"x": 10**400}, {"y": -1},
            {"name": "x" * (MAX_NAME + 1)}, {"notes": "x" * (MAX_NOTES + 1)},
            {"name": "line\nbreak"}, {"notes": "hidden\x00control"}, {"icon": "unknown"},
            {"id": "invalid"}, {"extra": "not a field"},
        ]
        for number, changes in enumerate(invalid, 2):
            entry = asdict(label(number))
            entry.update(changes)
            self.settings.setValue("map_labels/" + str(UUID(int=number)), json.dumps(entry))
        duplicate = asdict(label(40))
        raw = json.dumps(duplicate)
        self.settings.setValue("map_labels/" + label(40).id, raw[:-1] + ',"name":"duplicate"}')
        self.settings.setValue("map_labels/" + label(41).id, "not JSON")
        self.settings.setValue("map_labels/" + label(42).id, '"' + "x" * 9000 + '"')
        self.settings.setValue("map_labels/invalid-key", json.dumps(asdict(label(43))))
        self.settings.setValue("unrelated/key", "keep me")
        self.settings.sync()
        self.assertEqual(self.new_store().labels(MAP), [label()])
        self.assertIsNone(self.store.get(label(40).id))
        self.assertEqual(self.settings.value("unrelated/key"), "keep me")

    def test_invalid_saves_fail_without_writing_or_emitting(self):
        self.store.save(label())
        before = self.path.read_bytes()
        self.changes.clear()
        for changes in ({"x": True}, {"x": float("nan")}, {"x": float("inf")}, {"x": 10**400},
                        {"x": -.01}, {"y": 1.01}, {"name": ""}, {"name": "a\nb"},
                        {"notes": "a\x00b"}, {"name": "x" * 81}, {"notes": "x" * 501},
                        {"id": "not a UUID"}, {"icon": "unknown"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.save(label(2, **changes))
        self.assertEqual(self.changes, [])
        self.assertEqual(self.new_store().labels(MAP), [label()])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unicode_codepoint_limits_allow_emoji_and_notes_tabs(self):
        entry = label(name="\U0001f409" * MAX_NAME, notes="\U0001f389" * MAX_NOTES)
        self.store.save(entry)
        self.assertEqual(self.new_store().get(entry.id), entry)
        self.store.save(replace(entry, notes="Fictional\tmerchant\nOpen at dawn. 👩\u200d🚀"))
        self.assertEqual(self.new_store().get(entry.id).notes, "Fictional\tmerchant\nOpen at dawn. 👩\u200d🚀")

    def test_noop_save_is_quiet_and_moving_a_label_notifies_both_map_images(self):
        self.store.save(label())
        self.changes.clear()
        self.store.save(label())
        self.assertEqual(self.changes, [])
        self.store.save(label(map_url=OTHER_MAP))
        self.assertEqual(self.changes, [MAP, OTHER_MAP])
        self.assertEqual(self.store.labels(MAP), [])
        self.assertEqual(self.store.labels(OTHER_MAP), [label(map_url=OTHER_MAP)])

    def test_failed_postwrite_rolls_back_only_affected_key_without_signaling(self):
        first, other = label(), label(2, name="Neighbor")
        self.store.save(first)
        self.store.save(other)
        self.settings.setValue("other/key", "unchanged")
        self.settings.sync()
        self.changes.clear()
        for action in (lambda: self.store.save(replace(first, notes="Failed edit")),
                       lambda: self.store.save(label(3)), lambda: self.store.remove(first.id)):
            writes = []
            real_sync = self.store._sync

            def fail_after_mutation(*, writing=False):
                if writing:
                    writes.append(True)
                    if len(writes) == 2:
                        raise IOError("Simulated settings write failure")
                real_sync(writing=writing)

            with patch.object(self.store, "_sync", side_effect=fail_after_mutation), self.assertRaises(IOError):
                action()
            self.assertEqual(self.changes, [])
            reloaded = self.new_store()
            self.assertEqual(reloaded.get(first.id), first)
            self.assertEqual(reloaded.get(other.id), other)
            self.assertIsNone(reloaded.get(label(3).id))
            self.assertEqual(self.handle().value("other/key"), "unchanged")


if __name__ == "__main__":
    unittest.main()
