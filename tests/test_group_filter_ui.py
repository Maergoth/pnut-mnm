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

from mnmparse.app.models import ActorRow, EncounterSnapshot, build_snapshot, snapshot_rows_for_tab
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats


def fight(key: int, *, ours: bool = True, closed: bool = True) -> EncounterSnapshot:
    return EncounterSnapshot(
        key=str(key), label=f"Fight {key}", start=float(key), end=float(key + 10),
        duration=10.0, closed=closed, event_count=1, total_damage=0, raid_dps=0.0,
        killed=[], zone="Test zone", zone_since=1.0, ours=ours,
    )


def mixed_fight() -> EncounterSnapshot:
    """Nearby combat overlaps our fight without becoming part of our group."""
    def actor(name: str, **flags) -> ActorRow:
        return ActorRow(
            name=name, damage=10, dps=1, taken=5, dtps=0.5, heals=3, hps=0.3,
            healed=3, swings=1, hits=1, misses=0, hit_pct=100, max_hit=10,
            avg_hit=10, share=1 / 3, color="#86b998", is_you=False,
            is_npc=False, is_pet=False, **flags,
        )

    own = replace(actor("Owner"), is_you=True)
    member = actor("Tamsin")
    included = replace(actor("Knownpet"), is_pet=True, pet_owner="Owner")
    outsider = actor("Nearby", in_group=False)
    unassigned = replace(actor("Fluffy", in_group=False), is_pet=True)
    enemy = actor("Enemyplayer", in_group=False, is_enemy=True)
    npc = replace(actor("a rat", in_group=False), is_npc=True)
    return replace(fight(100), rows=[own, member, included, outsider, unassigned, enemy, npc],
                   total_damage=30, raid_dps=3, group_members=["Owner", "Tamsin"])


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

    def test_mixed_fight_row_filter_preserves_group_totals_and_raw_candidates(self) -> None:
        snap = mixed_fight()
        raw_rows = list(snap.rows)
        hidden = {"Nearby", "Fluffy"}
        for tab in ("overview", "damage", "healing", "taken"):
            with self.subTest(tab=tab):
                all_rows = snapshot_rows_for_tab(snap, tab)
                filtered = snapshot_rows_for_tab(snap, tab, show_other_groups=False)
                self.assertEqual({row.name for row in filtered}, {row.name for row in all_rows} - hidden)
                self.assertEqual({row.name for row in snapshot_rows_for_tab(snap, tab, show_other_groups=True)},
                                 {row.name for row in raw_rows})
                self.assertTrue(all(any(row is original for original in raw_rows) for row in filtered))
        self.assertEqual(snap.rows, raw_rows, "hidden combatants remain available to Add pet")
        self.assertEqual(snap.total_damage, 30)
        self.assertEqual([row.share for row in snap.rows[:3]], [1 / 3] * 3)

    def test_constructor_and_direct_config_changes_filter_mixed_fight_in_every_meter(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.app.pages import LivePage

        snap = mixed_fight()
        cfg = Config(player_name="Owner", show_other_groups=False,
                     casual_mode=False, casual_mode_confirmed=True)
        overlay = OverlayWindow(self.settings, cfg)
        page = LivePage(_MissingEngine(cfg), cfg, self.settings)
        for widget in (page, overlay):
            widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.show()
        try:
            page.set_history([snap])
            self.assertTrue(page.select_key(snap.key))
            overlay.set_snapshot(snap)
            overlay._flush_snapshot()
            for change, show in enumerate((False, True, False)):
                if change:
                    updated = replace(cfg, show_other_groups=show)
                    page.set_config(updated)
                    overlay.set_config(updated)
                expected = {row.name for row in snap.rows} - (set() if show else {"Nearby", "Fluffy"})
                for tab in ("overview", "damage", "healing", "taken"):
                    with self.subTest(show=show, tab=tab):
                        page.set_metric(tab)
                        overlay.set_tab(tab)
                        self.assertEqual({row.name for row in page._pane.table._model._rows}, expected)
                        self.assertEqual({row.name for row in overlay.meter._model._rows}, expected)
                self.assertEqual(page.selected().total_damage, 30)
                self.assertEqual(overlay._snap.total_damage, 30)
            self.assertIs(page._raw_snaps[snap.key], snap)
            self.assertIs(overlay._raw_live, snap)
            self.assertEqual({row.name for row in snap.rows},
                             {"Owner", "Tamsin", "Knownpet", "Nearby", "Fluffy", "Enemyplayer", "a rat"})
        finally:
            for widget in (page, overlay):
                widget.close()
                widget.deleteLater()
            self.app.processEvents()

    def test_hiding_selected_outsider_clears_their_meter_details(self) -> None:
        pane = self.page._pane
        pane.set_snapshot(mixed_fight())
        pane.set_metric("damage")
        model = pane.table._model
        row = next(i for i, actor in enumerate(model._rows) if actor.name == "Nearby")
        pane.table.view.selectRow(pane.table._proxy.mapFromSource(model.index(row, 0)).row())
        pane._on_row_selected(pane.table.selected_row())
        self.assertEqual(pane.details._name.text(), "Nearby")
        pane.set_show_other_groups(False)
        self.assertNotEqual(pane.details._name.text(), "Nearby")
        selected = pane.table.selected_row()
        self.assertTrue(selected is None or selected.name not in {"Nearby", "Fluffy"})
        self.assertNotIn("Nearby", {row.name for row in pane.table._model._rows})

    def test_assigning_and_clearing_pet_refreshes_hidden_meters_and_group_totals(self) -> None:
        cfg = Config(player_name="Owner", show_other_groups=False,
                     casual_mode=False, casual_mode_confirmed=True)
        self.page.set_config(cfg)
        self.overlay.set_config(cfg)
        self.page.set_metric("damage")
        self.overlay.set_tab("damage")
        stats = Stats(player_name="Owner")
        stats.roster.set_manual("Tamsin", True)
        for ts, line in enumerate(("You crush a rat for 10 points of damage.",
                                   "Tamsin slashes a rat for 20 points of damage.",
                                   "Fluffy bites a rat for 10 points of damage.",
                                   "Nearby crushes a rat for 40 points of damage."), 100):
            stats.add(parse_line(line, ts, "Owner"))
        for owner, amount in ((None, 20), ("Tamsin", 30), (None, 20)):
            with self.subTest(owner=owner, amount=amount):
                stats.roster.set_pet_owner("Fluffy", owner)
                snap = build_snapshot(stats, stats.current(), "Owner", now=106)
                self.page.set_snapshot(snap)
                self.overlay.set_snapshot(snap)
                self.overlay._flush_snapshot()
                for table in (self.page._pane.table, self.overlay.meter):
                    rows = {row.name: row for row in table._model._rows}
                    self.assertEqual(set(rows), {"Owner", "Tamsin"})
                    self.assertEqual(rows["Tamsin"].damage, amount)
                    self.assertEqual(rows["Tamsin"].attributed_pets, ["Fluffy"] if owner else [])
                self.assertEqual(snap.total_damage, amount + 10)
                self.assertIn("Nearby", {row.name for row in snap.rows})
                if owner is None:
                    self.assertIn("Fluffy", {row.name for row in snap.rows})


if __name__ == "__main__":
    unittest.main()
