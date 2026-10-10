"""Encounter visibility changes take effect in both meters and their history immediately."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.app.models import EncounterSnapshot
from mnmparse.config import Config


def fight(key: int, *, ours: bool = True, closed: bool = True) -> EncounterSnapshot:
    return EncounterSnapshot(
        key=str(key), label=f"Fight {key}", start=float(key), end=float(key + 10),
        duration=10.0, closed=closed, event_count=1, total_damage=0, raid_dps=0.0,
        killed=[], zone="Test zone", zone_since=1.0, ours=ours,
    )


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class GroupFilterUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def setUp(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.app.pages import LivePage

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)
        cfg = Config(show_other_groups=True, casual_mode=False, casual_mode_confirmed=True)
        self.overlay = OverlayWindow(self.settings, cfg)
        self.page = LivePage(_MissingEngine(cfg), cfg, self.settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.show()

    def tearDown(self) -> None:
        self.overlay.close()
        self.overlay.deleteLater()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_disabling_filter_clears_unrelated_live_and_pending_fights(self) -> None:
        for flushed in (False, True):
            with self.subTest(flushed=flushed):
                self.overlay.set_show_other_groups(True)
                self.overlay.set_snapshot(fight(100, ours=False, closed=False))
                if flushed:
                    self.overlay._flush_snapshot()
                self.overlay.set_show_other_groups(False)
                self.assertIsNone(self.overlay._snap)
                self.overlay._flush_snapshot()
                self.assertIsNone(self.overlay._snap, "queued snapshots cannot restore a hidden fight")

    def test_hidden_history_is_retained_for_reenabling_and_cannot_be_pinned(self) -> None:
        ours, theirs = fight(100), fight(200, ours=False)
        self.overlay.set_history([ours, theirs])
        self.overlay.set_snapshot(theirs)
        self.overlay._flush_snapshot()
        self.overlay.show_encounter(theirs)
        self.overlay.set_show_other_groups(False)
        self.assertEqual([s.key for s in self.overlay.history()], [ours.key])
        self.assertIsNone(self.overlay.pinned)
        self.assertEqual(self.overlay._snap.key, ours.key)
        self.overlay.show_encounter(theirs)
        self.assertEqual(self.overlay._snap.key, ours.key)
        self.overlay.set_show_other_groups(True)
        self.assertEqual([s.key for s in self.overlay.history()], [theirs.key, ours.key])
        self.assertEqual(self.overlay._snap.key, theirs.key)

    def test_pinned_zone_summary_excludes_hidden_fights_and_empty_zone_clears(self) -> None:
        ours, theirs = fight(100), fight(200, ours=False)
        self.overlay.set_history([ours, theirs])
        self.overlay.show_encounter(self.overlay.zone_summary(theirs))
        old_summary = self.overlay._snap
        self.assertEqual(self.overlay._snap.encounters, 2)
        self.overlay.set_show_other_groups(False)
        self.assertEqual(self.overlay._snap.encounters, 1)
        self.assertEqual(self.overlay.pinned.encounters, 1)
        self.overlay.show_encounter(old_summary)
        self.assertEqual(self.overlay._snap.encounters, 1, "a stale menu choice must obey the current filter")
        self.overlay.update_encounter(replace(ours, ours=False))
        self.assertIsNone(self.overlay._snap)
        self.assertIsNone(self.overlay.pinned)
        self.assertIsNone(self.overlay.zone_summary(ours), "old menu references cannot restore reclassified fights")

    def test_reclassified_live_pending_and_pinned_encounters_disappear(self) -> None:
        self.overlay.set_show_other_groups(False)
        ours = fight(100)
        self.overlay.set_snapshot(ours)
        self.overlay._flush_snapshot()
        self.overlay.show_encounter(ours)
        self.overlay.update_encounter(replace(ours, ours=False))
        self.assertIsNone(self.overlay._snap)
        self.assertIsNone(self.overlay.pinned)
        live = fight(200, closed=False)
        self.overlay.set_snapshot(live)
        self.overlay.set_snapshot(replace(live, ours=False))
        self.overlay._flush_snapshot()
        self.assertIsNone(self.overlay._snap)
        self.overlay.set_snapshot(live)
        self.overlay._flush_snapshot()
        self.overlay.set_snapshot(replace(live, ours=False))
        self.assertIsNone(self.overlay._snap)

    def test_pinned_summary_updates_when_live_fight_becomes_unrelated(self) -> None:
        self.overlay.set_show_other_groups(False)
        ours, live = fight(100), fight(200, closed=False)
        self.overlay.set_history([ours])
        self.overlay.set_snapshot(live)
        self.overlay._flush_snapshot()
        self.overlay.show_encounter(self.overlay.zone_summary(live))
        self.assertEqual(self.overlay._snap.encounters, 2)
        self.overlay.set_snapshot(replace(live, ours=False))
        self.assertEqual(self.overlay._snap.encounters, 1)

    def test_stale_fight_menu_reference_uses_latest_membership(self) -> None:
        self.overlay.set_show_other_groups(False)
        for closed in (True, False):
            with self.subTest(closed=closed):
                old = fight(100 if closed else 200, closed=closed)
                self.overlay.set_snapshot(old)
                self.overlay._flush_snapshot()
                corrected = replace(old, ours=False)
                if closed:
                    self.overlay.update_encounter(corrected)
                else:
                    self.overlay.set_snapshot(corrected)
                self.overlay.show_encounter(old)
                self.assertIsNone(self.overlay._snap, "a stale menu action must not resurrect a hidden fight")
                self.assertIsNone(self.overlay.pinned)

    def test_main_page_selection_and_zone_totals_follow_filter_changes(self) -> None:
        ours, theirs = fight(100), fight(200, ours=False)
        self.page.set_history([ours, theirs])
        self.page.select_zone()
        self.assertEqual(self.page.selected().encounters, 2)
        self.page.set_config(Config(show_other_groups=False, casual_mode=False, casual_mode_confirmed=True))
        self.assertEqual(self.page.listed_keys(), [ours.key])
        self.assertEqual(self.page.selected().encounters, 1)
        self.page.set_config(Config(show_other_groups=True, casual_mode=False, casual_mode_confirmed=True))
        self.page.select_key(theirs.key)
        self.page.set_config(Config(show_other_groups=False, casual_mode=False, casual_mode_confirmed=True))
        self.assertEqual(self.page.selected().key, ours.key)
        self.page.add_encounter(replace(ours, ours=False))
        self.assertEqual(self.page.listed_keys(), [])
        self.assertIsNone(self.page.selected())

    def test_main_page_live_fight_clears_when_filter_disabled(self) -> None:
        self.page.set_snapshot(fight(100, ours=False, closed=False))
        self.assertIsNotNone(self.page.selected())
        self.page.set_config(Config(show_other_groups=False, casual_mode=False, casual_mode_confirmed=True))
        self.assertEqual(self.page.listed_keys(), [])
        self.assertIsNone(self.page.selected())


if __name__ == "__main__":
    unittest.main()
