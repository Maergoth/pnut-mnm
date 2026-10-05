"""Who counts: the party roster, enemies and outsiders in a fight, the group-only totals, hit
rates the chat cannot show, and the review fixes around them (export, overlay header, capture
crops, feed scrolling, swing jitter)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse.app.models import build_snapshot
from mnmparse.export import PRESETS, ExportFormat, format_snapshot, has_people
from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def _fight(lines: list[tuple[float, str]], party: tuple[str, ...] = ()) -> tuple[Stats, object]:
    stats = Stats(8.0)
    for name in party:
        stats.add(parse_line(f"{name} has joined the party.", 0.0, PLAYER))
    for ts, text in lines:
        stats.add(parse_line(text, ts, PLAYER))
    return stats, build_snapshot(stats, stats.current(), PLAYER, now=lines[-1][0])


def _rows(snap) -> dict:
    return {r.name: r for r in snap.rows}


class RosterTests(unittest.TestCase):
    def test_party_lines(self) -> None:
        roster = PartyRoster()
        for text, kind, actor in [
            ("Tamsin has joined the party.", "status", "Tamsin"),
            ("Brannoc is now the leader of the party.", "status", "Brannoc"),
            ("Your party member Wenna has slain a rat!", "kill", "Wenna"),
            ("--Corvath loots [Bone Chips] from a rat's corpse.--", "loot", "Corvath"),
        ]:
            roster.observe(SimpleNamespace(text=text, kind=kind, actor=actor, ts=1.0))
        self.assertEqual(roster.members(), {"Tamsin", "Brannoc", "Wenna", "Corvath"})
        roster.observe(SimpleNamespace(text="Wenna has left the party.", kind="status", actor="Wenna", ts=2.0))
        self.assertNotIn("Wenna", roster.members())
        roster.observe(SimpleNamespace(text="You loot [a rat tail].", kind="loot", actor="You", ts=2.0))
        self.assertNotIn("You", roster.members())
        roster.observe(SimpleNamespace(text="Your party has been disbanded.", kind="status", actor=None, ts=3.0))
        self.assertEqual(roster.members(), set())
        self.assertFalse(roster.known())

    def test_manual_choices_win_and_survive_a_restart(self) -> None:
        roster = PartyRoster()
        roster.observe(SimpleNamespace(text="Tamsin has joined the party.", kind="status", actor="Tamsin", ts=1.0))
        roster.set_manual("Kulepu", True)  # a party member's pet
        roster.set_manual("Tamsin", False)
        self.assertEqual(roster.members(), {"Kulepu"})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "party.json"
            roster.save(path)
            again = PartyRoster()
            self.assertTrue(again.load(path))
            self.assertEqual(again.members(), {"Kulepu"})
            data = json.loads(path.read_text(encoding="utf-8"))
            data["saved"] = time.time() - 24 * 3600  # yesterday's group
            path.write_text(json.dumps(data), encoding="utf-8")
            old = PartyRoster()
            old.load(path)
            self.assertEqual(old.seen(), set(), "an old session's party is not reused")
            self.assertEqual(old.manual_in, {"Kulepu"}, "but the user's own choices are")
        roster.set_manual("Tamsin", None)
        self.assertIn("Tamsin", roster.members())


class SidesTests(unittest.TestCase):
    def test_group_total_leaves_out_enemies_and_outsiders(self) -> None:
        _stats, snap = _fight([
            (1, "You crush a stumbling zombie for 60 points of damage."),
            (2, "Rupedu slashes a stumbling zombie for 30 points of damage."),
            (3, "a stumbling zombie bites YOU for 100 points of damage."),
            # the group's last hit ends the fight too, so its own span and the meter's agree
            (4, "Tamsin's Holy Strike hits a stumbling zombie for 40 points of Holy Damage."),
        ], party=("Tamsin",))
        rows = _rows(snap)
        self.assertEqual(snap.total_damage, 100)
        self.assertAlmostEqual(snap.raid_dps, 100 / 3, places=2)
        self.assertAlmostEqual(rows[PLAYER].share, 0.6)
        self.assertTrue(rows["a stumbling zombie"].is_enemy)
        self.assertFalse(rows["Rupedu"].in_group or rows["Rupedu"].is_enemy, "an outsider, not an enemy")
        text = format_snapshot(snap, PRESETS["DPS and share"])
        self.assertEqual(text, "a stumbling zombie [0:03] 33.3 DPS - Maergoth 20.0 (60%) | Tamsin 13.3 (40%)")

    def test_named_enemy_and_pvp(self) -> None:
        _stats, snap = _fight([
            (1, "You crush Grandmaster Obadiah for 60 points of damage."),
            (2, "Grandmaster Obadiah hits Tamsin for 80 points of damage."),
            (3, "Hokabibuve crushes Tamsin for 20 points of damage."),
        ], party=("Tamsin",))
        rows = _rows(snap)
        self.assertTrue(rows["Grandmaster Obadiah"].is_enemy and not rows["Grandmaster Obadiah"].in_group)
        self.assertTrue(rows["Hokabibuve"].is_enemy, "a player who attacks the group is an enemy")
        self.assertEqual(snap.total_damage, 60)
        self.assertNotIn("Obadiah 4", format_snapshot(snap, PRESETS["DPS"]))

    def test_unknown_party_counts_everyone_on_our_side(self) -> None:
        _stats, snap = _fight([
            (1, "You crush a rat for 10 points of damage."),
            (2, "Rupedu slashes a rat for 10 points of damage."),
        ])
        self.assertTrue(_rows(snap)["Rupedu"].in_group, "nothing says who the party is yet")
        self.assertEqual(snap.total_damage, 20)

    def test_manual_override_counts_a_pet(self) -> None:
        stats = Stats(8.0)
        stats.add(parse_line("Tamsin has joined the party.", 0.0, PLAYER))
        stats.roster.set_manual("Kulepu", True)
        for ts, text in [(1, "You crush a rat for 10 points of damage."), (2, "Kulepu bites a rat for 5 points of damage.")]:
            stats.add(parse_line(text, ts, PLAYER))
        snap = build_snapshot(stats, stats.current(), PLAYER, now=2)
        self.assertTrue(_rows(snap)["Kulepu"].in_group)
        self.assertEqual(snap.total_damage, 15)

    def test_kills_counts_every_death(self) -> None:
        _stats, snap = _fight([
            (1, "You crush a skeleton for 5 points of damage."), (1.5, "You have slain a skeleton!"),
            (2, "You crush a skeleton for 5 points of damage."), (2.5, "You have slain a skeleton!"),
        ])
        self.assertEqual(snap.kills, 2)
        self.assertTrue(format_snapshot(snap, ExportFormat("{kills} kills", "{name}")).startswith("2 kills"))


class DeathsTests(unittest.TestCase):
    def test_deaths_per_actor_with_killers(self) -> None:
        _stats, snap = _fight([
            (1, "You crush a skeleton for 5 points of damage."), (2, "a skeleton hits YOU for 50 points of damage."),
            (3, "You have been slain by a skeleton!"), (3.2, "You have been slain by a skeleton!"),  # read twice
            (4, "Tamsin crushes a skeleton for 9 points of damage."), (5, "You have slain a skeleton!"),
            (6, "Tamsin crushes a skeleton for 9 points of damage."), (7, "a skeleton has been slain by Tamsin!"),
            (8, "Your party member Brannoc has been slain by a skeleton!"),
        ], party=("Tamsin",))
        rows = _rows(snap)
        self.assertEqual(rows[PLAYER].deaths, 1, "a death line read twice is one death")
        self.assertEqual(rows[PLAYER].killed_by, {"a skeleton": 1})
        self.assertEqual(rows["a skeleton"].deaths, 2, "every mob of the pull that died")
        self.assertEqual(rows["Tamsin"].deaths, 0)
        self.assertEqual(format_snapshot(snap, ExportFormat("{actors}", "{name} {deaths}", ", ", "damage")),
                         "Tamsin 0, Maergoth 1")


class HiddenMissesTests(unittest.TestCase):
    def test_other_players_hit_rate_is_unknown_until_their_misses_show(self) -> None:
        lines = [
            (1, "You crush a caiman for 10 points of damage."), (2, "You try to crush a caiman, but miss!"),
            (3, "Pusubu crushes a caiman for 12 points of damage."), (4, "a caiman tries to bite YOU, but misses!"),
            (5, "Lepob tries to slash YOU, but misses!"),
            (6, "Pusubu tries to slash a caiman, but a caiman dodges!"),
        ]
        stats, snap = _fight(lines)
        rows = _rows(snap)
        self.assertFalse(stats.others_misses_seen)
        self.assertFalse(rows["Pusubu"].misses_shown)
        self.assertTrue(rows[PLAYER].misses_shown and rows["a caiman"].misses_shown)
        self.assertIn("? hit", format_snapshot(snap, ExportFormat("{actors}", "{name} {hit} hit")))
        stats.add(parse_line("Pusubu tries to crush a caiman, but misses!", 7, PLAYER))
        self.assertTrue(stats.others_misses_seen)
        snap = build_snapshot(stats, stats.current(), PLAYER, now=7)
        self.assertTrue(_rows(snap)["Pusubu"].misses_shown)


class ExportReviewTests(unittest.TestCase):
    @staticmethod
    def _row(name: str, damage: int, heals: int = 0, **kw):
        return SimpleNamespace(name=name, damage=damage, dps=damage / 10, share=kw.get("share", 0.5), max_hit=1,
                               hit_pct=72.6, hps=heals / 10, heals=heals, taken=0, utility=0, is_npc=False,
                               is_enemy=kw.get("enemy", False), in_group=kw.get("group", True),
                               misses_shown=True)

    def _snap(self, rows):
        return SimpleNamespace(label="x", zone="", duration=10, start=0, total_damage=70, raid_dps=7.0, killed=[],
                               kills=0, encounters=1, rows=rows)

    def test_everything_keeps_healers_and_specs_see_decimals(self) -> None:
        snap = self._snap([self._row("Pidef", 70, share=0.434), self._row("Tamsin", 0, 120)])
        self.assertIn("Tamsin 0.0 dps / 12.0 hps", format_snapshot(snap, PRESETS["Everything"]))
        self.assertIn("Pidef 43.4% 43%", format_snapshot(snap, ExportFormat("{actors}", "{name} {share:.1f}% {share}%")))

    def test_auto_copy_skips_a_line_with_nobody(self) -> None:
        self.assertFalse(has_people(self._snap([self._row("Rupedu", 40, group=False)]), PRESETS["DPS"]))
        self.assertFalse(has_people(self._snap([self._row("Pidef", 70)]), PRESETS["Healing"]))
        self.assertTrue(has_people(self._snap([self._row("Pidef", 70)]), PRESETS["DPS"]))

    def test_empty_templates_are_rejected(self) -> None:
        import dataclasses

        from mnmparse.config import Config

        problems = " ".join(dataclasses.replace(Config(), export_line=" ", export_actor="").problems())
        self.assertIn("Line must not be empty", problems)
        self.assertIn("Each person", problems)


class CaptureReviewTests(unittest.TestCase):
    @staticmethod
    def _frame(value: int, size: int = 400):
        return SimpleNamespace(frame_buffer=np.full((size, size, 4), value, dtype=np.uint8))

    def _source(self):
        from mnmparse.capture import WgcWindowSource

        src = WgcWindowSource("x", max_fps=12.0)
        src._thread = SimpleNamespace(is_alive=lambda: True)
        return src

    def feed(self, src, value: int) -> None:
        src._last_kept = 0.0
        src._on_frame(self._frame(value), SimpleNamespace(stop=lambda: None))

    def test_a_new_crop_never_uses_an_old_whole_frame(self) -> None:
        src = self._source()
        self.feed(src, 1)  # whole frame at the start of the session
        src.latest_region((0, 0, 100, 100))
        for v in range(2, 6):
            self.feed(src, v)  # region mode: only the crop is copied
        src._frame_at -= 60.0  # that whole frame is a minute old
        self.assertIsNone(src.latest_region((0, 0, 50, 50)), "old content must not be OCR'd as new")
        self.feed(src, 9)
        self.assertEqual(int(src.latest_region((0, 0, 50, 50))[0, 0, 0]), 9)

    def test_a_cut_for_the_old_crop_is_not_handed_out_for_the_new_one(self) -> None:
        src = self._source()
        self.feed(src, 1)
        src.latest_region((0, 0, 100, 100))
        self.feed(src, 2)
        src._region_cut = (0, 0, 100, 100)
        src._region = (0, 0, 30, 30)  # switched between the cut and the store
        self.assertIsNone(src.latest_region((0, 0, 30, 30)))

    def test_plain_latest_mode_is_throttled_again(self) -> None:
        src = self._source()
        src._on_frame(self._frame(1), SimpleNamespace(stop=lambda: None))
        kept = src.frame_count
        src._on_frame(self._frame(2), SimpleNamespace(stop=lambda: None))  # straight after: dropped
        self.assertEqual(src.frame_count, kept)


@unittest.skipUnless(sys.platform == "win32", "Qt is exercised on the Windows build")
class UiReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([sys.argv[0]])

    def test_mode_glyph_toggles_and_tooltips(self) -> None:
        from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
        from PySide6.QtGui import QHelpEvent
        from PySide6.QtTest import QTest

        from mnmparse.app.overlay import COPY_TIP, OverlayWindow
        from mnmparse.config import Config

        _stats, snap = _fight([(1, "You crush a rat for 10 points of damage."), (2, "a rat bites YOU for 3 points of damage.")])
        with tempfile.TemporaryDirectory() as tmp:
            ov = OverlayWindow(QSettings(str(Path(tmp) / "o.ini"), QSettings.Format.IniFormat), Config(player_name=PLAYER))
            for w in (ov, ov.attack_bar, ov.timer_panel):
                w.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            try:
                ov.resize(440, 240)
                ov.show()
                ov.set_snapshot(snap)
                ov._flush_snapshot()
                header = ov._header
                header.grab()
                self.assertFalse(header._mode_rect.isEmpty())
                before = ov.view_mode
                QTest.mouseClick(header, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                                 header._mode_rect.center().toPoint())
                self.assertNotEqual(ov.view_mode, before)
                shown: list[str] = []
                import mnmparse.app.overlay as overlay_module

                real = overlay_module.QToolTip.showText
                overlay_module.QToolTip.showText = lambda pos, text, w=None: shown.append(text)
                try:
                    for point in (header._copy_rect.center(), header._mode_rect.center(), QPointF(4, 4)):
                        header.event(QHelpEvent(QEvent.Type.ToolTip, point.toPoint(), header.mapToGlobal(point.toPoint())))
                finally:
                    overlay_module.QToolTip.showText = real
                self.assertEqual(shown[0], COPY_TIP)
                self.assertIn("Showing", shown[1])
                self.assertIn("Click the name", shown[2])
                ov.resize(ov.minimumWidth(), 240)
                header.set_reserved_right(header.width() - 60)  # the hover toolbar covers nearly all
                header.grab()
                self.assertTrue(header._copy_rect.isEmpty() and header._mode_rect.isEmpty(),
                                "no room: the glyphs give way to the encounter name")
                _ = QPoint
            finally:
                ov.close()
                ov.deleteLater()

    def test_group_entries_for_a_person(self) -> None:
        from PySide6.QtWidgets import QMenu

        from mnmparse.app.overlay import add_group_entries

        menu = QMenu()
        got: list = []
        outsider = SimpleNamespace(name="Kulepu", in_group=False, is_you=False, is_npc=False, is_enemy=False)
        handlers = add_group_entries(menu, outsider, lambda n, s: got.append((n, s)))
        texts = [a.text() for a in handlers]
        self.assertIn("Count Kulepu as my group", texts)
        for action, run in handlers.items():
            run()
        self.assertEqual(got, [("Kulepu", True), ("Kulepu", None)])
        self.assertEqual(add_group_entries(menu, SimpleNamespace(name="a rat", is_npc=True), got.append), {})
        menu.deleteLater()

    def test_feed_keeps_the_reader_in_place(self) -> None:
        from mnmparse.app.widgets import FeedView

        view = FeedView(max_lines=200)
        view.resize(500, 300)
        view.show()
        try:
            for i in range(200):
                view.append(f"line {i}", "melee_hit", False, 1000.0 + i)
            self.app.processEvents()
            bar = view._edit.verticalScrollBar()
            bar.setValue(bar.maximum() // 3)
            self.app.processEvents()
            top = view._edit.cursorForPosition(view._edit.viewport().rect().topLeft()).block().text()
            for i in range(200, 230):
                view.append(f"line {i}", "melee_hit", False, 1000.0 + i)
            self.app.processEvents()
            self.assertEqual(view._edit.cursorForPosition(view._edit.viewport().rect().topLeft()).block().text(), top)
            self.assertFalse(view._at_bottom())
        finally:
            view.deleteLater()


class SwingJitterTests(unittest.TestCase):
    def test_one_late_read_on_a_fast_weapon_keeps_the_delay(self) -> None:
        from mnmparse.swing import SwingTracker

        frame = 1 / 6
        tracker = SwingTracker()
        shown = []
        for i in range(40):
            read = round((100.0 + 1.3 * i) / frame) * frame + (frame if i == 30 else 0.0)
            tracker.observe(SimpleNamespace(kind="melee_hit", actor="You", skill="crush", ts=read, weapon=None, is_pet=False))
            shown.append(tracker.hands(read)[0].delay)
        self.assertTrue(all(abs(d - 1.33) < 0.05 for d in shown[20:]), shown[20:])


if __name__ == "__main__":
    unittest.main()
