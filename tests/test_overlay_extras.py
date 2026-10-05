"""Auto-attack timing, overlay hover cards, the encounter dropdown and the docked attack bar."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse.app.models import build_snapshot
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.swing import SwingTracker, estimate_delay

PLAYER = "Maergoth"


@dataclass
class Ev:
    ts: float
    kind: str
    actor: str
    skill: str
    is_pet: bool = False


class SwingTests(unittest.TestCase):
    def test_delay_is_robust_to_lost_lines(self) -> None:
        times = [0.0, 2.5, 5.0, 10.0, 12.5, 15.1, 17.5]  # 7.5 -> 10.0: one swing lost to OCR
        self.assertAlmostEqual(estimate_delay(times), 2.5, delta=0.1)
        self.assertIsNone(estimate_delay([0.0, 2.5]))

    def test_only_your_auto_attack_counts(self) -> None:
        t = SwingTracker(PLAYER)
        self.assertTrue(t.observe(Ev(0.0, "melee_hit", PLAYER, "crush")))
        self.assertTrue(t.observe(Ev(1.0, "melee_miss", PLAYER, "crush")))
        self.assertFalse(t.observe(Ev(1.5, "melee_hit", PLAYER, "kick")), "kick is an ability")
        self.assertFalse(t.observe(Ev(2.0, "melee_hit", "Gozif", "crush")))
        self.assertFalse(t.observe(Ev(2.0, "ability_hit", PLAYER, "Crusader Strike")))

    def test_one_weapon_one_bar(self) -> None:
        t = SwingTracker(PLAYER)
        for i in range(6):
            t.observe(Ev(i * 3.0, "melee_hit", PLAYER, "crush"))
        hands = t.hands(16.0)
        self.assertEqual([(h.label, h.delay) for h in hands], [("crush", 3.0)])
        self.assertAlmostEqual(hands[0].progress(16.5), 1.5 / 3.0)
        self.assertTrue(hands[0].idle(40.0))

    def test_two_weapon_types_two_bars(self) -> None:
        t = SwingTracker(PLAYER)
        for i in range(6):
            t.observe(Ev(i * 2.6, "melee_hit", PLAYER, "slash"))
            t.observe(Ev(i * 2.6 + 0.9 + i * 0.05, "melee_hit", PLAYER, "pierce"))
        hands = t.hands(16.0)
        self.assertEqual(sorted(h.label for h in hands), ["pierce", "slash"])
        delays = {h.label: h.delay for h in hands}
        self.assertAlmostEqual(delays["slash"], 2.6, delta=0.05)
        self.assertAlmostEqual(delays["pierce"], 2.65, delta=0.1)

    def test_two_same_weapons_split_by_rhythm(self) -> None:
        t = SwingTracker(PLAYER)
        times = sorted([i * 3.0 for i in range(8)] + [i * 3.0 + 0.3 for i in range(8)])
        for ts in times:
            t.observe(Ev(ts, "melee_hit", PLAYER, "slash"))
        hands = t.hands(22.0)
        self.assertEqual([h.label for h in hands], ["hand 1", "hand 2"])
        for h in hands:
            self.assertAlmostEqual(h.delay, 3.0, delta=0.1)

    def test_offhand_and_bow_are_their_own_bars(self) -> None:
        from mnmparse.parser import parse_line as pl

        t = SwingTracker(PLAYER)
        for i in range(6):
            t.observe(pl("You slash a skeleton for 5 points of damage.", i * 3.0, PLAYER))
            t.observe(pl("You slash a skeleton with your offhand for 3 points of damage.", i * 2.4 + 0.2, PLAYER))
        hands = t.hands(16.0)
        self.assertEqual([(h.label, h.delay) for h in hands], [("slash", 3.0), ("off hand", 2.4)])
        t.observe(pl("You pierce a skeleton with your bow for 9 points of damage.", 16.5, PLAYER))
        self.assertEqual([h.label for h in t.hands(17.0)], ["slash", "off hand", "bow"])

    def test_bars_keep_their_rows(self) -> None:
        t = SwingTracker(PLAYER)
        orders = set()
        for i in range(60):
            t.observe(Ev(i * 2.6, "melee_hit", PLAYER, "slash"))
            t.observe(Ev(i * 2.65 + 0.9, "melee_hit", PLAYER, "pierce"))
            orders.add(tuple(h.label for h in t.hands(i * 2.6 + 1.0)))
        orders.discard(("slash",))  # the first seconds, before either has three swings
        self.assertEqual(orders, {("slash", "pierce")}, "first seen first, never swapping")

    def test_lost_lines_do_not_skew_the_delay(self) -> None:
        self.assertAlmostEqual(estimate_delay([0.0, 6.0, 9.0, 15.0, 18.0, 24.0, 27.0]), 3.0, delta=0.05)

    def test_same_frame_lines_are_not_a_second_hand(self) -> None:
        t = SwingTracker(PLAYER)
        for i in range(12):
            t.observe(Ev(i * 3.0, "melee_hit", PLAYER, "crush"))
            if i % 3 == 0:
                t.observe(Ev(i * 3.0, "melee_hit", PLAYER, "crush"))  # read twice in one frame
        self.assertEqual([h.label for h in t.hands(34.0)], ["crush"])


def _snap(stats: Stats, enc, player=PLAYER):
    return build_snapshot(stats, enc, player)


def _fights() -> list:
    """Three closed fights in one zone visit, then one open fight."""
    stats = Stats(encounter_timeout_s=5.0)
    stats.add(parse_line("You have entered Night Harbor (West).", 0.0, PLAYER))
    lines = [
        (10.0, "Gozif crushes a skeletal warrior for 10 points of damage."),
        (11.0, "Your party member Gozif has slain a skeletal warrior!"),
        (30.0, "Gozif crushes a skeletal monk for 20 points of damage."),
        (31.0, "Your party member Gozif has slain a skeletal monk!"),
        (50.0, "You crush a skeletal cleric for 7 points of damage."),
        (51.0, "Gozif crushes a skeletal cleric for 30 points of damage."),
        (52.0, "Your party member Gozif has slain a skeletal cleric!"),
    ]
    for ts, text in lines:
        stats.add(parse_line(text, ts, PLAYER))
    stats.expire(60.0)  # the last fight ends at its timeout (kills do not end fights)
    snaps = [_snap(stats, enc) for enc in stats.history]
    stats.add(parse_line("You crush a skeletal knight for 3 points of damage.", 70.0, PLAYER))
    live = build_snapshot(stats, stats.current(), PLAYER, now=71.0)
    return snaps + [live]


@unittest.skipUnless(sys.platform == "win32", "Qt overlay is exercised on the Windows build")
class OverlayExtrasTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def setUp(self) -> None:
        from PySide6.QtCore import QSettings, Qt

        from mnmparse.app.overlay import OverlayWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "o.ini"), QSettings.Format.IniFormat)
        self.overlay = OverlayWindow(self.settings, Config(player_name=PLAYER))
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.overlay.attack_bar.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)

    def tearDown(self) -> None:
        self.overlay.close()
        self.overlay.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def flush(self) -> None:
        self.overlay._flush_snapshot()

    def test_tooltips_show_on_the_inactive_overlay(self) -> None:
        from PySide6.QtCore import Qt

        self.assertTrue(self.overlay.testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips))
        view = self.overlay.meter.view
        self.assertTrue(view.viewport().testAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips))

    def test_dps_and_name_hover_cards(self) -> None:
        from PySide6.QtCore import Qt

        *closed, live = _fights()
        for snap in closed:
            self.overlay.set_snapshot(snap)
        self.overlay.set_snapshot(live)
        self.flush()
        self.overlay.set_tab("overview")
        model = self.overlay.meter._model
        names = [model._rows[i].name for i in range(len(model._rows))]
        row = names.index(PLAYER)
        dps_col = model.column_index("dps")
        dps_tip = model.data(model.index(row, dps_col), Qt.ItemDataRole.ToolTipRole)
        self.assertIn("DPS", dps_tip)
        self.assertIn("crush", dps_tip)
        name_tip = model.data(model.index(row, model.column_index("name")), Qt.ItemDataRole.ToolTipRole)
        self.assertIn("Night Harbor (West)", name_tip)
        self.assertIn("in 2 of 4 fights", name_tip, "the cleric fight and the open one: you were not in the first two")
        summary = self.overlay.zone_summary()
        self.assertEqual(summary.encounters, 4)
        gozif = next(r for r in summary.rows if r.name == "Gozif")
        self.assertEqual(gozif.damage, 60, "Gozif across the whole zone visit")

    def test_browsing_an_earlier_fight_until_new_combat(self) -> None:
        *closed, live = _fights()
        for snap in closed:
            self.overlay.set_snapshot(snap)
        self.flush()
        first = self.overlay.history()[-1]
        self.overlay.show_encounter(first)
        self.assertIs(self.overlay._snap, first)
        self.assertTrue(self.overlay._ended)
        self.overlay.set_snapshot(replace(closed[-1]))  # a refresh of an old fight: stays pinned
        self.assertIs(self.overlay.pinned, first)
        self.overlay.set_snapshot(live)  # new combat
        self.flush()
        self.assertIsNone(self.overlay.pinned)
        self.assertEqual(self.overlay._snap.key, live.key)

    def test_pin_survives_refreshes_of_the_open_fight(self) -> None:
        *closed, live = _fights()
        for snap in closed:
            self.overlay.set_snapshot(snap)
        self.overlay.set_snapshot(live)
        self.flush()
        self.overlay.show_encounter(closed[0])
        self.overlay.set_snapshot(replace(live, duration=live.duration + 1))  # the 1 Hz refresh
        self.flush()
        self.assertIs(self.overlay.pinned, closed[0])
        self.assertIs(self.overlay._snap, closed[0])
        self.overlay.set_snapshot(replace(live, key="999.000", closed=False))  # a new fight
        self.flush()
        self.assertIsNone(self.overlay.pinned)

    def test_a_snapshot_queued_during_the_menu_does_not_replace_the_pick(self) -> None:
        *closed, live = _fights()
        for snap in closed:
            self.overlay.set_snapshot(snap)
        self.flush()
        self.overlay.set_snapshot(closed[-1])  # queued, throttle running
        self.overlay.show_encounter(closed[0])
        self.flush()
        self.assertIs(self.overlay._snap, closed[0])

    def test_hiding_the_bar_from_its_menu_is_remembered(self) -> None:
        seen = []
        self.overlay.attack_bar_changed.connect(seen.append)
        self.overlay.set_attack_bar_enabled(False)
        self.assertEqual(seen, [False])
        self.assertFalse(self.overlay.attack_bar.enabled)
        self.assertFalse(self.settings.value("attack_bar/enabled", True, type=bool))

    def test_zone_summary_can_be_shown(self) -> None:
        *closed, _live = _fights()
        for snap in closed:
            self.overlay.set_snapshot(snap)
        self.flush()
        self.overlay.show_encounter(self.overlay.zone_summary(closed[0]))
        self.assertEqual(self.overlay._snap.encounters, 3)

    def test_attack_bar_docks_and_undocks(self) -> None:
        overlay, bar = self.overlay, self.overlay.attack_bar
        overlay.setGeometry(100, 100, 400, 250)
        overlay.show()
        self.app.processEvents()
        self.assertTrue(bar.isVisible())
        self.assertTrue(bar.docked)
        self.assertEqual(bar.x(), overlay.frameGeometry().left())
        self.assertGreater(bar.y(), overlay.frameGeometry().bottom())
        self.assertEqual(bar.width(), overlay.frameGeometry().width())
        overlay.move(300, 200)
        self.assertEqual(bar.x(), overlay.frameGeometry().left(), "docked: it follows the overlay")
        bar._docked = False
        bar._free_geometry = bar.geometry().translated(0, 300)
        bar.follow()
        overlay.move(310, 210)
        self.assertNotEqual(bar.x(), overlay.frameGeometry().left(), "undocked: it stays put")
        bar.dock()
        self.assertEqual(bar.x(), overlay.frameGeometry().left())
        overlay.hide()
        self.assertFalse(bar.isVisible())

    def test_attack_bar_hands_from_events(self) -> None:
        bar = self.overlay.attack_bar
        for i in range(5):
            self.overlay.observe_event(parse_line("You crush a skeletal warrior for 5 points of damage.", 1000.0 + i * 2.8, PLAYER))
        hands = bar.tracker.hands(1012.0)
        self.assertEqual(len(hands), 1)
        self.assertAlmostEqual(hands[0].delay, 2.8, delta=0.05)


@unittest.skipUnless(sys.platform == "win32", "Qt app is exercised on the Windows build")
class LiveOverlaySettingsTests(unittest.TestCase):
    """Settings > Overlay changes the overlay at once and mirrors changes made on the overlay."""

    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def test_header_cursor_follows_the_lock(self) -> None:
        from PySide6.QtCore import QSettings, Qt

        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "o.ini"), QSettings.Format.IniFormat)
            overlay = OverlayWindow(settings, Config())
            try:
                self.assertTrue(overlay.locked)
                self.assertEqual(overlay._header.cursor().shape(), Qt.CursorShape.ArrowCursor)
                overlay.set_locked(False)
                self.assertEqual(overlay._header.cursor().shape(), Qt.CursorShape.SizeAllCursor)
                self.assertEqual(overlay.attack_bar.cursor().shape(), Qt.CursorShape.SizeAllCursor)
                overlay.set_locked(True)
                self.assertEqual(overlay.attack_bar.cursor().shape(), Qt.CursorShape.ArrowCursor)
            finally:
                overlay.close()
                overlay.deleteLater()
                self.app.processEvents()

    def test_settings_controls_are_live_both_ways(self) -> None:
        from PySide6.QtCore import QSettings, Qt

        from mnmparse.app.main import MainWindow, _MissingEngine
        from mnmparse.app.overlay import OverlayWindow

        with tempfile.TemporaryDirectory() as tmp:
            settings = QSettings(str(Path(tmp) / "o.ini"), QSettings.Format.IniFormat)
            cfg = Config()
            overlay = OverlayWindow(settings, cfg)
            win = MainWindow(_MissingEngine(cfg), overlay, cfg, settings)
            page = win.page("settings")
            applied: list[tuple[str, object]] = []

            def apply(key: str, value: object) -> None:
                applied.append((key, value))
                if key == "opacity":
                    overlay.set_opacity(float(value))

            page.overlay_setting_changed.connect(apply)
            overlay.appearance_changed.connect(lambda op, fs: page.sync_overlay(opacity=op, font_scale=fs))
            try:
                page.overlay_opacity.set_value(55, emit=True)
                self.assertEqual(applied[-1], ("opacity", 0.55))
                self.assertAlmostEqual(overlay.opacity, 0.55)
                overlay.set_opacity(0.4)  # the overlay's own toolbar slider
                self.assertEqual(page.overlay_opacity.value(), 40)
                self.assertEqual(applied[-1], ("opacity", 0.55), "mirroring back does not re-apply")
                self.assertAlmostEqual(page._cfg.overlay_opacity, 0.4, msg="no false unsaved change")
            finally:
                win.close()
                overlay.close()
                win.deleteLater()
                overlay.deleteLater()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
