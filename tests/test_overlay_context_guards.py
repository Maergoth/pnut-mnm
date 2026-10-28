"""Encounter controls follow the displayed overlay context, rather than its cached fight."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from mnmparse.app.models import build_snapshot
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats


PLAYER = "Reader"
MOB = "Dreadfang"


def _fight():
    stats = Stats(player_name=PLAYER)
    stats.add(parse_line(f"You crush {MOB} for 40 points of damage.", 100.0, PLAYER))
    stats.add(parse_line(f"{MOB} bites YOU for 30 points of damage.", 101.0, PLAYER))
    return build_snapshot(stats, stats.current(), PLAYER, now=106.0)


@unittest.skipUnless(sys.platform == "win32", "Qt overlay is exercised on the Windows build")
class OverlayContextGuardsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        from PySide6.QtCore import QSettings, Qt
        from mnmparse.app.overlay import OverlayWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "overlay.ini"), QSettings.Format.IniFormat)
        self.overlay = OverlayWindow(self.settings, Config(
            player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True, revenge_enabled=True))
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.overlay.attack_bar.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.overlay.setGeometry(100, 100, 820, 350)
        self.overlay.set_locked(True)
        self.snap = _fight()
        self.overlay.show_encounter(self.snap)
        self.overlay.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.overlay.close()
        self.overlay.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_feed_and_revenge_clear_the_cached_fight_header_and_hit_targets(self) -> None:
        from PySide6.QtCore import QPointF, QRectF

        header = self.overlay._header
        for mode, tab in ((mode, tab) for mode in ("group", "self") for tab in ("feed", "revenge")):
            with self.subTest(mode=mode, tab=tab):
                self.overlay.set_view_mode(mode)
                self.overlay.set_tab("damage")
                header.grab()
                self.assertFalse(header._copy_rect.isEmpty())
                self.assertFalse(header._label_rect.isEmpty())
                self.overlay.set_tab(tab)
                self.assertEqual((header.label, header.total, header.raid_dps, header.duration), ("", 0, 0.0, 0.0))
                self.assertFalse(header.has_data)
                self.assertEqual(header._base_tip, "")
                self.assertTrue(header._label_rect.isEmpty(), "clear stale targets before the next repaint")
                self.assertTrue(header._copy_rect.isEmpty())
                self.assertTrue(header._mode_rect.isEmpty())
                header.grab()
                self.assertTrue(header._label_rect.isEmpty())
                # Even a late release from an earlier combat paint cannot invoke these controls.
                header._copy_rect = header._mode_rect = QRectF(0, 0, 30, 30)
                header.has_data = True
                self.assertFalse(header._on_copy(QPointF(5, 5)))
                self.assertFalse(header._on_mode(QPointF(5, 5)))
        self.overlay.set_view_mode("group")
        self.overlay.set_tab("damage")
        header.grab()
        self.assertEqual(header.label, self.snap.label)
        self.assertIs(self.overlay.pinned, self.snap)
        self.assertFalse(header._copy_rect.isEmpty())

    def test_feed_and_revenge_do_not_offer_or_invoke_cached_fight_actions(self) -> None:
        from PySide6.QtCore import QPoint

        copied = []
        self.overlay.copy_requested.connect(copied.append)
        enemy = next(row for row in self.snap.rows if row.is_npc or row.is_enemy)
        self.overlay._timer_panel.runner = Mock()
        event = SimpleNamespace(globalPos=lambda: QPoint())
        for mode, tab in ((mode, tab) for mode in ("group", "self") for tab in ("feed", "revenge")):
            with self.subTest(mode=mode, tab=tab):
                self.overlay.set_view_mode(mode)
                self.overlay.set_tab(tab)
                self.overlay.copy_current()
                self.assertEqual(self.overlay._respawn_names_at(QPoint(), enemy), [])
                with patch("mnmparse.app.overlay.QMenu") as menu, patch(
                        "mnmparse.app.overlay.RespawnTimerDialog") as dialog, patch.object(
                        self.overlay._table, "row_at", return_value=enemy) as row_at:
                    self.overlay.open_encounter_menu(QPoint())
                    self.overlay.contextMenuEvent(event)
                    self.overlay.start_respawn_timer(MOB)
                menu.assert_not_called()
                dialog.assert_not_called()
                row_at.assert_not_called()
        self.assertEqual(copied, [])
        self.overlay._timer_panel.runner.start_one_time_timer.assert_not_called()
        self.overlay._timer_panel.runner = None

    def test_clicking_noncombat_header_does_not_toggle_the_hidden_fight(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        for mode, tab in ((mode, tab) for mode in ("group", "self") for tab in ("feed", "revenge")):
            with self.subTest(mode=mode, tab=tab):
                self.overlay.set_view_mode(mode)
                self.overlay.set_tab(tab)
                with patch.object(self.overlay, "toggle_view_mode") as toggle, patch.object(
                        self.overlay, "open_encounter_menu") as browse:
                    QTest.mouseClick(self.overlay._header, Qt.MouseButton.LeftButton)
                toggle.assert_not_called()
                browse.assert_not_called()

    def test_all_combat_tabs_keep_their_header_copy_and_respawn_target(self) -> None:
        from PySide6.QtCore import QPoint

        copied = []
        self.overlay.copy_requested.connect(copied.append)
        enemy = next(row for row in self.snap.rows if row.is_npc or row.is_enemy)
        for tab in ("overview", "damage", "healing", "taken"):
            with self.subTest(tab=tab):
                self.overlay.set_tab(tab)
                self.overlay._header.grab()
                self.assertEqual(self.overlay._header.label, self.snap.label)
                self.assertEqual(self.overlay._header.total, self.snap.total_damage)
                self.assertFalse(self.overlay._header._copy_rect.isEmpty())
                self.overlay.copy_current()
                self.assertEqual(self.overlay._respawn_names_at(QPoint(), enemy), [MOB])
        self.assertEqual(copied, [self.snap] * 4)

    def test_self_header_is_zero_when_only_another_group_member_acted(self) -> None:
        rows = [replace(row, name="Ally", is_you=False) if row.is_you else row for row in self.snap.rows]
        self.overlay.show_encounter(replace(self.snap, rows=rows))
        self.overlay.set_view_mode("self")
        self.assertEqual((self.overlay._header.total, self.overlay._header.raid_dps), (0, 0.0))
        self.assertIn(f"{PLAYER} · self", self.overlay._header.label)

    def test_self_header_resolves_case_insensitive_owner_and_legacy_pet_only_rows(self) -> None:
        owner = next(row for row in self.snap.rows if row.is_you)
        ally = replace(owner, name="Ally", is_you=False, damage=200, dps=50.0)
        for pet_only in (False, True):
            with self.subTest(pet_only=pet_only):
                own = replace(owner, name="Pet" if pet_only else PLAYER.lower(), is_you=False,
                              is_pet=pet_only, pet_owner=PLAYER.lower() if pet_only else "")
                snap = replace(self.snap, rows=[ally, own], total_damage=240, raid_dps=60.0)
                self.overlay.show_encounter(snap)
                self.overlay.set_view_mode("self")
                self.assertEqual(self.overlay._header.total, owner.damage)
                self.assertEqual(self.overlay._header.raid_dps, owner.dps)

    def test_session_keeps_its_own_header_mode_and_pinned_context(self) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.overlay.set_tab("session")
        header = self.overlay._header
        header.grab()
        self.assertEqual(header.label, "Whole session · all zones")
        self.assertEqual((header.total, header.raid_dps, header.duration), (0, 0.0, 0.0))
        self.assertTrue(header._copy_rect.isEmpty())
        self.assertTrue(header._label_rect.isEmpty())
        self.assertFalse(header._mode_rect.isEmpty())
        self.assertIn("All zones in this session", header._base_tip)
        QTest.mouseClick(header, Qt.MouseButton.LeftButton, pos=header._mode_rect.center().toPoint())
        self.assertEqual(self.overlay.view_mode, "self")
        self.assertIs(self.overlay.pinned, self.snap)
        self.assertEqual(self.overlay._session._view_mode, "self")


if __name__ == "__main__":
    unittest.main()
