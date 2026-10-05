"""Regression tests for the desktop app's widgets, pages and overlay (offscreen Qt).

Covers the review fixes:

* the first header click on a numeric meter column sorts descending (``InitialSortOrderRole``);
* ``feed_color`` honours the parsed ``is_player_target`` flag (a hit on the player
  phrased with the character name, not YOU, is DANGER);
* ``LivePage.add_encounter`` keeps the user's selection on an older encounter;
* ``SettingsPage.problems_for`` rejects an empty window title;
* the details panel skips rebuilding the skills table for unchanged rows;
* the crop picker rejects a spin-box value that would collapse the crop;
* a programmatic ``OverlayWindow.close()`` (app shutdown) does not report "hidden";
* the overlay window flags include ``WindowDoesNotAcceptFocus`` and the spec set.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":  # real Windows fonts on the offscreen platform (realistic text widths)
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
    from PySide6.QtTest import QTest  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402

    HAVE_QT = True
except ImportError:  # pragma: no cover - environment dependent
    HAVE_QT = False

from mnmparse.app import theme  # noqa: E402
from mnmparse.app.models import ActorRow, EncounterSnapshot, SkillRow  # noqa: E402
from mnmparse.config import Config  # noqa: E402


def _qapp() -> Any:
    app = QApplication.instance()
    return app if app is not None else QApplication([sys.argv[0]])


def _actor(name: str, damage: int, *, is_you: bool = False, skills: list[SkillRow] | None = None) -> ActorRow:
    return ActorRow(
        name=name, damage=damage, dps=damage / 10.0, taken=0, dtps=0.0, heals=0, hps=0.0, healed=0,
        swings=4, hits=3, misses=1, hit_pct=75.0, max_hit=damage, avg_hit=float(damage),
        share=0.0, color=theme.actor_color(name, is_you=is_you), is_you=is_you, is_npc=False, is_pet=False,
        skills=skills if skills is not None else [SkillRow("melee", 3, damage, damage, float(damage), 3, 1)],
    )


def _snapshot(
    key: float,
    rows: list[ActorRow],
    *,
    closed: bool = True,
    zone: str = "",
    label: str = "a stumbling zombie",
    zone_since: float = 0.0,
) -> EncounterSnapshot:
    total = sum(r.damage for r in rows)
    for r in rows:
        r.share = r.damage / total if total else 0.0
    return EncounterSnapshot(
        key=f"{key:.3f}", label=label, start=key, end=key + 10.0, duration=10.0,
        closed=closed, event_count=len(rows), total_damage=total, raid_dps=total / 10.0, killed=[], rows=rows,
        zone=zone, zone_since=zone_since if zone else 0.0,
    )


def _settings(tmp: str) -> QSettings:
    return QSettings(str(Path(tmp) / "test.ini"), QSettings.Format.IniFormat)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class MeterSortTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_initial_sort_order_helper(self) -> None:
        from mnmparse.app.widgets import initial_sort_order

        for key in ("damage", "dps", "share", "hit_pct", "max_hit", "heals", "taken"):
            self.assertEqual(initial_sort_order(key), Qt.SortOrder.DescendingOrder, key)
        for key in ("name", "rank"):
            self.assertEqual(initial_sort_order(key), Qt.SortOrder.AscendingOrder, key)

    def test_model_header_initial_sort_role(self) -> None:
        from mnmparse.app.widgets import MeterTable

        table = MeterTable(compact=False)
        try:
            model = table._model
            role = Qt.ItemDataRole.InitialSortOrderRole
            for i, col in enumerate(model.columns):
                expected = Qt.SortOrder.AscendingOrder if col.key in ("name", "rank") else Qt.SortOrder.DescendingOrder
                self.assertEqual(model.headerData(i, Qt.Orientation.Horizontal, role), expected, col.key)
            self.assertEqual(model.headerData(1, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole), "Name")
        finally:
            table.deleteLater()
            self.app.processEvents()

    def test_first_header_click_sorts_numeric_column_descending(self) -> None:
        from mnmparse.app.widgets import MeterTable

        table = MeterTable(compact=False)
        try:
            table.resize(700, 300)
            table.show()
            self.app.processEvents()
            rows = [_actor("Pidef", 34, is_you=True), _actor("Tovozen", 15), _actor("Fluffy", 7)]
            table.set_rows(_snapshot(1.0, rows).rows, "damage")
            self.assertEqual(table.sort, ("damage", True))
            changes: list[tuple[str, bool]] = []
            table.sort_changed.connect(lambda k, d: changes.append((k, d)))

            header = table._header
            col = table._model.column_index("dps")
            x = header.sectionViewportPosition(col) + header.sectionSize(col) // 2
            QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, header.height() // 2))
            self.app.processEvents()
            self.assertEqual(changes[-1], ("dps", True), "first click on DPS must sort biggest first")
            self.assertEqual(table._proxy.index(0, 0).data(Qt.ItemDataRole.DisplayRole), "1")

            QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, header.height() // 2))
            self.app.processEvents()
            self.assertEqual(changes[-1], ("dps", False), "second click flips to ascending")

            col = table._model.column_index("name")
            x = header.sectionViewportPosition(col) + header.sectionSize(col) // 2
            QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, header.height() // 2))
            self.app.processEvents()
            self.assertEqual(changes[-1], ("name", False), "names start A-Z")
        finally:
            table.deleteLater()
            self.app.processEvents()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class FeedColorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_parsed_target_flag_beats_word_scan(self) -> None:
        from mnmparse.app.widgets import feed_color

        line = "a rat hits Maergoth for 3 points of damage."
        self.assertEqual(feed_color(line, "melee_hit", False), theme.TEXT, "no YOU in the text")
        self.assertEqual(feed_color(line, "melee_hit", False, is_player_target=True), theme.DANGER)
        self.assertEqual(feed_color(line, "melee_hit", False, is_player_target=False), theme.TEXT)
        you = "a rat hits YOU for 3 points of damage."
        self.assertEqual(feed_color(you, "melee_hit", False), theme.DANGER)
        self.assertEqual(feed_color(you, "melee_hit", False, is_player_target=None), theme.DANGER)
        self.assertEqual(feed_color("You crush a rat for 3 points of damage.", "melee_hit", True), theme.YOU)

    def test_feed_view_append_keeps_flag(self) -> None:
        from mnmparse.app.widgets import FeedView

        view = FeedView(max_lines=10)
        try:
            view.append("a rat hits Maergoth for 3 points of damage.", "melee_hit", False, 1.0, is_player_target=True)
            view.append("a rat hits Tovozen for 3 points of damage.", "melee_hit", False, 2.0)
            entries = view.entries()
            self.assertEqual([e.is_player_target for e in entries], [True, None])
            self.assertIn(theme.DANGER, view._render(entries[0]))
            self.assertIn(theme.TEXT, view._render(entries[1]))
        finally:
            view.deleteLater()
            self.app.processEvents()

    def test_main_append_helper_falls_back(self) -> None:
        from mnmparse.app.main import _append_feed, is_player_target
        from mnmparse.parser import parse_line

        ev = parse_line("a rat hits Maergoth for 3 points of damage.", 1.0, "Maergoth")
        self.assertTrue(is_player_target(ev, "Maergoth"))
        calls: list[Any] = []

        def new_style(text: str, kind: str, action: bool, ts: float, is_player_target: bool | None = None) -> None:
            calls.append(("new", text, kind, action, ts, is_player_target))

        def old_style(text: str, kind: str, action: bool, ts: float) -> None:
            calls.append(("old", text, kind, action, ts))

        _append_feed(new_style, "t", "melee_hit", False, 1.0, True)
        _append_feed(old_style, "t", "melee_hit", False, 1.0, True)
        self.assertEqual(calls[0], ("new", "t", "melee_hit", False, 1.0, True))
        self.assertEqual(calls[1], ("old", "t", "melee_hit", False, 1.0))


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class LivePageTests(unittest.TestCase):
    """The merged Live page: encounter list with zone tabs, live row, selection following."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def _page(self, tmp: str, *, shown: bool = True) -> Any:
        from PySide6.QtCore import Qt

        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import LivePage

        page = LivePage(_MissingEngine(Config()), Config(), _settings(tmp))
        if shown:  # an unseen page defers its redraws (see test_off_screen_page_catches_up_when_shown)
            page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            page.show()
        return page

    def test_off_screen_page_catches_up_when_shown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp, shown=False)
            try:
                first = _snapshot(100.0, [_actor("Pidef", 34, is_you=True)])
                second = _snapshot(200.0, [_actor("Pidef", 10, is_you=True)])
                page.add_encounter(first)
                page.set_snapshot(second)
                self.assertEqual(page.listed_keys(), [], "nothing is drawn while nobody sees the page")
                from PySide6.QtCore import Qt

                page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
                page.show()
                self.assertEqual(page.listed_keys(), [second.key, first.key], "one redraw when it shows")
                self.assertEqual(page.selected().key, second.key)
            finally:
                page.deleteLater()

    def test_new_encounter_does_not_steal_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                first = _snapshot(100.0, [_actor("Pidef", 34, is_you=True)])
                second = _snapshot(200.0, [_actor("Pidef", 10, is_you=True)])
                third = _snapshot(300.0, [_actor("Pidef", 5, is_you=True)])
                page.add_encounter(first)
                self.assertEqual(page.selected().key, first.key, "the first encounter is selected")
                page.add_encounter(second)
                self.assertEqual(page.selected().key, second.key, "following the newest")

                # The user goes back to read the first fight...
                self.assertTrue(page.select_key(first.key))
                self.assertEqual(page.selected().key, first.key)
                page.add_encounter(third)
                self.assertEqual(page.selected().key, first.key, "a new encounter must not yank the view")
                self.assertEqual(page.listed_keys(), [third.key, second.key, first.key])

                # ...and the newest again: now it follows.
                page.select_key(third.key)
                fourth = _snapshot(400.0, [_actor("Pidef", 1, is_you=True)])
                page.add_encounter(fourth)
                self.assertEqual(page.selected().key, fourth.key)
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_live_row_is_pinned_and_followed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                page.add_encounter(_snapshot(100.0, [_actor("Pidef", 34, is_you=True)]))
                live = _snapshot(50.0, [_actor("Pidef", 3, is_you=True)], closed=False)  # older start, still first
                page.set_snapshot(live)
                self.assertEqual(page.listed_keys()[0], live.key, "the open fight is pinned at the top")
                self.assertEqual(page.selected().key, live.key)
                self.assertFalse(page.selected().closed)
                self.assertTrue(page._reset.isEnabled())
                self.assertIn("1 live", page._count.text())
                newer = _snapshot(50.0, [_actor("Pidef", 9, is_you=True)], closed=False)
                page.set_snapshot(newer)
                self.assertEqual(page.selected().total_damage, 9, "live numbers refresh in place")
                self.assertEqual(page.listed_keys(), [live.key, "100.000"], "no duplicate row for the same fight")
                closed = _snapshot(50.0, [_actor("Pidef", 9, is_you=True)], closed=True)
                page.add_encounter(closed)
                page.set_snapshot(closed)
                self.assertFalse(page._reset.isEnabled())
                self.assertNotIn("live", page._count.text())
                self.assertEqual(page.listed_keys(), ["100.000", live.key], "closed: back in start order")
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_zone_headers_group_visits_and_summarise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                you = lambda dmg: [_actor("Pidef", dmg, is_you=True)]  # noqa: E731
                page.add_encounter(_snapshot(100.0, you(1)))  # before any zone line
                page.add_encounter(_snapshot(300.0, you(3), zone="Night Harbor (East)", zone_since=250.0))
                page.add_encounter(_snapshot(400.0, you(4), zone="Wyrmsbane Tomb", zone_since=350.0))
                page.add_encounter(_snapshot(500.0, you(5), zone="Wyrmsbane Tomb", zone_since=350.0))
                page.add_encounter(_snapshot(900.0, you(9), zone="Night Harbor (East)", zone_since=850.0))  # back again
                self.assertEqual(
                    page.zone_headers(),
                    [("Night Harbor (East)", 1), ("Wyrmsbane Tomb", 2), ("Night Harbor (East)", 1), ("Unknown zone", 1)],
                    "one header per visit, newest first",
                )
                self.assertEqual(page.listed_keys(), ["900.000", "500.000", "400.000", "300.000", "100.000"])
                tops = [page._tree.topLevelItem(i) for i in range(page._tree.topLevelItemCount())]
                self.assertEqual([t.isExpanded() for t in tops], [True, False, False, False], "older visits collapsed")
                self.assertEqual(page.selected().key, "900.000", "following the newest fight")

                self.assertTrue(page.select_zone(1))
                summary = page.selected()
                self.assertEqual(summary.encounters, 2)
                self.assertEqual(summary.total_damage, 9)
                self.assertEqual(summary.duration, 20.0, "time in combat: the durations added up")
                self.assertEqual(summary.label, "Wyrmsbane Tomb")
                self.assertEqual([(r.name, r.damage) for r in summary.rows], [("Pidef", 9)])

                # the user is reading the summary: a new fight does not move the selection
                page.add_encounter(_snapshot(950.0, you(2), zone="Night Harbor (East)", zone_since=850.0))
                self.assertEqual(page.selected().label, "Wyrmsbane Tomb")
                self.assertEqual(page.zone_headers()[0], ("Night Harbor (East)", 2))
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_live_fight_updates_its_zone_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                you = lambda dmg: [_actor("Pidef", dmg, is_you=True)]  # noqa: E731
                page.add_encounter(_snapshot(100.0, you(10), zone="Wyrmsbane Tomb", zone_since=50.0))
                page.set_snapshot(_snapshot(200.0, you(3), closed=False, zone="Wyrmsbane Tomb", zone_since=50.0))
                page.select_zone(0)
                self.assertEqual(page.selected().total_damage, 13)
                self.assertFalse(page.selected().closed, "includes the open fight")
                page.set_snapshot(_snapshot(200.0, you(7), closed=False, zone="Wyrmsbane Tomb", zone_since=50.0))
                self.assertEqual(page.selected().total_damage, 17, "the summary follows the live numbers")
                self.assertTrue(page._reset.isEnabled(), "Reset works whatever is selected")
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_other_groups_fights_hidden_unless_asked_for(self) -> None:
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                ours = _snapshot(100.0, [_actor("Pidef", 5, is_you=True)])
                theirs = dataclasses.replace(_snapshot(200.0, [_actor("Cigezisi", 9)]), ours=False)
                page.add_encounter(ours)
                page.add_encounter(theirs)
                self.assertEqual(page.listed_keys(), [ours.key])
                self.assertIn("1 fight of other groups", page._count.toolTip())
                page.set_config(Config(show_other_groups=True))
                self.assertEqual(page.listed_keys(), [theirs.key, ours.key])
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_same_fight_imported_twice_is_listed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            page = self._page(tmp)
            try:
                rows = [_actor("Pidef", 5, is_you=True)]
                self.assertTrue(page.add_encounter(_snapshot(100.0, rows), source="combat_2026-10-02"))
                again = _snapshot(100.4, [_actor("Pidef", 5, is_you=True)])  # the .jsonl stamps fractions
                self.assertFalse(page.add_encounter(again, source="events_2026-10-02"))
                self.assertEqual(len(page.listed_keys()), 1)
                live = _snapshot(100.0, [_actor("Pidef", 5, is_you=True)])
                self.assertTrue(page.add_encounter(live), "live encounters are never skipped")
            finally:
                page.deleteLater()
                self.app.processEvents()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class CaptureWarningTests(unittest.TestCase):
    """The top bar has no diagnostics any more: a dismissable warning when the chat is covered
    or unreadable, a smaller one on the overlay, and the details on the Settings page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_warning_text(self) -> None:
        from mnmparse.app.widgets import capture_warning

        self.assertIsNone(capture_warning({"state": "running", "occluded_recent": False, "garbled": False}))
        short, detail = capture_warning({"occluded_recent": True})
        self.assertIn("covered", short.lower())
        short, detail = capture_warning({"garbled": True, "unreadable_pct": 55.0})
        self.assertIn("55%", detail)

    def test_latch_holds_a_dismissal_until_the_problem_clears(self) -> None:
        from mnmparse.app.widgets import WarningLatch

        latch = WarningLatch(clear_s=20.0)
        self.assertTrue(latch.update(True, now=0.0))
        latch.dismiss()
        self.assertFalse(latch.update(True, now=5.0), "dismissed while the problem lasts")
        self.assertFalse(latch.update(False, now=10.0))
        self.assertFalse(latch.update(True, now=12.0), "came back too soon: still dismissed")
        self.assertFalse(latch.update(False, now=40.0), "gone long enough: the dismissal is spent")
        self.assertTrue(latch.update(True, now=41.0), "a new occurrence shows again")

    def test_window_banner_and_settings_status(self) -> None:
        from mnmparse.app.main import MainWindow, _MissingEngine

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            win = MainWindow(_MissingEngine(cfg), None, cfg, _settings(tmp))
            win.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            try:
                win.show()
                win.on_state("running")
                status = {"state": "running", "window_found": True, "fps": 6.0, "ocr_ms": 16.0, "messages": 14,
                          "occluded": 3, "occluded_recent": True, "unreadable_pct": 5.0, "garbled": False, "replays": 2}
                win.on_status(status)
                self.assertTrue(win.warning_visible())
                win._dismiss_warning()
                win.on_status(status)
                self.assertFalse(win.warning_visible(), "stays dismissed while the chat is still covered")
                settings_page = win.page("settings")
                values = settings_page._status_values
                self.assertEqual(values["state"].text(), "Reading the Combat chat")
                self.assertEqual(values["window"].text(), "found")
                self.assertEqual(values["messages"].text(), "14")
                self.assertEqual(values["replays"].text(), "2")
                win.on_state("stopped")
                self.assertFalse(win.warning_visible())
            finally:
                win.close()
                win.deleteLater()
                self.app.processEvents()

    def test_overlay_pill(self) -> None:
        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            overlay = OverlayWindow(_settings(tmp), Config())
            overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            try:
                overlay.show()
                overlay.set_status({"state": "running", "garbled": True, "unreadable_pct": 60.0})
                self.assertTrue(overlay.warning_visible())
                overlay.dismiss_warning()
                self.assertFalse(overlay.warning_visible())
                overlay.set_status({"state": "running", "garbled": True, "unreadable_pct": 60.0})
                self.assertFalse(overlay.warning_visible())
                overlay.set_status({"state": "stopped", "garbled": True})
                self.assertFalse(overlay.warning_visible())
            finally:
                overlay.close()
                overlay.deleteLater()
                self.app.processEvents()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class SettingsAndDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_empty_window_title_is_a_problem(self) -> None:
        from mnmparse.app.pages import SettingsPage

        self.assertEqual(SettingsPage.problems_for(Config()), [])
        problems = SettingsPage.problems_for(Config(window_title="   "))
        self.assertEqual(len(problems), 1)
        self.assertIn("Window title", problems[0])

    def test_settings_form_revalidates_on_title_edit(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.pages import SettingsPage

        with tempfile.TemporaryDirectory() as tmp:
            page = SettingsPage(_MissingEngine(Config()), Config(), _settings(tmp))
            try:
                self.assertTrue(page._save.isEnabled())
                page.window_title.setText("")
                self.assertFalse(page._save.isEnabled(), "Save must disable as soon as the title is cleared")
                self.assertIn("Window title", page._problems.text())
                page.window_title.setText("Monsters and Memories")
                self.assertTrue(page._save.isEnabled())
            finally:
                page.deleteLater()
                self.app.processEvents()

    def test_details_panel_skips_unchanged_rows(self) -> None:
        from mnmparse.app.pages import _DetailsPanel

        panel = _DetailsPanel()
        try:
            fills: list[int] = []
            original = panel._fill_skills

            def counting(skills: Any, **kwargs: Any) -> None:
                fills.append(1)
                original(skills, **kwargs)

            panel._fill_skills = counting  # type: ignore[method-assign]
            row = _actor("Pidef", 34, is_you=True)
            panel.set_actor(row)
            self.assertEqual(len(fills), 1)
            self.assertEqual(panel._skills.rowCount(), 1)
            panel.set_actor(_actor("Pidef", 34, is_you=True))  # identical snapshot: no work
            self.assertEqual(len(fills), 1)
            ticked = _actor("Pidef", 34, is_you=True)
            ticked.dps = 3.1  # duration tick: numbers change, skills do not
            panel.set_actor(ticked)
            self.assertEqual(len(fills), 1, "unchanged skills must not rebuild the table")
            self.assertEqual(panel._cells["dps"].text(), "3.1")
            more = _actor("Pidef", 40, is_you=True, skills=[SkillRow("crush", 3, 34, 20, 11.3, 3, 1), SkillRow("kick", 1, 6, 6, 6.0, 1, 0)])
            panel.set_actor(more)
            self.assertEqual(len(fills), 2)
            self.assertEqual(panel._skills.rowCount(), 2)
            panel.set_actor(None)
            self.assertEqual(panel._skills.rowCount(), 0)
            panel.set_actor(None)
        finally:
            panel.deleteLater()
            self.app.processEvents()

    def test_crop_spin_rejects_collapse(self) -> None:
        from mnmparse.app.crop_picker import CropPicker
        from mnmparse.app.main import _MissingEngine

        picker = CropPicker(_MissingEngine(Config()), Config())
        try:
            picker.set_crop((0, 60, 700, 600))
            self.assertEqual(picker.crop(), (0, 60, 700, 600))
            picker._spins["left"].setValue(5000)
            self.assertEqual(picker.crop(), (0, 60, 700, 600), "a left edge past the right edge is rejected")
            self.assertEqual(picker._spins["left"].value(), 0, "the spin box snaps back")
            self.assertIn("rejected", picker._status.text())
            picker._spins["top"].setValue(650)
            self.assertEqual(picker.crop(), (0, 60, 700, 600))
            picker._spins["right"].setValue(4)
            self.assertEqual(picker.crop(), (0, 60, 700, 600))
            picker._spins["left"].setValue(20)
            self.assertEqual(picker.crop(), (20, 60, 700, 600), "a valid edit still applies")
            picker._spins["bottom"].setValue(620)
            self.assertEqual(picker.crop(), (20, 60, 700, 620))
        finally:
            picker.deleteLater()
            self.app.processEvents()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class OverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = _qapp()

    def test_flags_are_spec_set_plus_no_focus(self) -> None:
        from mnmparse.app import overlay as overlay_mod

        flags = overlay_mod.OVERLAY_FLAGS
        for flag in (
            Qt.WindowType.FramelessWindowHint,
            Qt.WindowType.WindowStaysOnTopHint,
            Qt.WindowType.Tool,
            Qt.WindowType.WindowDoesNotAcceptFocus,
        ):
            self.assertTrue(flags & flag, flag)

    def test_programmatic_close_does_not_report_hidden(self) -> None:
        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings = _settings(tmp)
            overlay = OverlayWindow(settings, Config())
            try:
                seen: list[bool] = []
                overlay.visibility_changed.connect(seen.append)
                overlay.show()
                self.app.processEvents()
                self.assertEqual(seen, [True])
                self.assertTrue(overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
                overlay.close()  # what App.shutdown does
                self.app.processEvents()
                self.assertEqual(seen, [True], "shutdown must not look like the user turning the overlay off")
                self.assertFalse(overlay.isVisible())
            finally:
                overlay.deleteLater()
                self.app.processEvents()

    def test_user_hide_still_reports(self) -> None:
        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            overlay = OverlayWindow(_settings(tmp), Config())
            try:
                seen: list[bool] = []
                overlay.visibility_changed.connect(seen.append)
                overlay.show()
                overlay.hide()  # the toolbar's "Hide overlay" button calls hide()
                self.app.processEvents()
                self.assertEqual(seen, [True, False])
            finally:
                overlay.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
