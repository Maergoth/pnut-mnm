"""The notable NPC picker keeps each zone's cached results and hover controls."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QCursor, QEnterEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.app.notable_npc_menu import NotableNpcMenu


class MapNotableNPCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "map.ini"), QSettings.Format.IniFormat)
        self.worker = patch("mnmparse.app.map_overlay._Load.start")
        self.worker.start()
        self.window = MapOverlay(self.settings, Mock())
        self.requests = []
        self.selections = []
        self.window.npc_zone_requested.connect(self.requests.append)
        self.window.npc_selected.connect(self.selections.append)

    def tearDown(self):
        self.window.shutdown()
        self.window.deleteLater()
        self.app.processEvents()
        self.worker.stop()
        self.temp.cleanup()

    def rows(self):
        return [a.text() for a in self.window.notable_npcs_menu.actions() if not a.isSeparator()]

    def narrow_screen(self, menu):
        """Exercise overflow even when CI provides a wide desktop."""
        area = QRect(self.app.primaryScreen().availableGeometry())
        area.setWidth(min(800, area.width()))
        screen = Mock()
        screen.availableGeometry.return_value = area
        self.enterContext(patch.object(menu, "screen", return_value=screen))
        return area

    @staticmethod
    def close_popup(menu):
        # deleteLater alone leaves a failed standalone popup grabbing input
        # until DeferredDelete runs, which processEvents does not guarantee.
        if isValid(menu):
            menu.close()
            menu.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_hidden_zone_changes_defer_request_until_map_is_opened(self):
        self.assertFalse(self.window.notable_npcs.isEnabled())
        self.window.set_zone("Sungreet Strand")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.rows(), ["Loading notable NPCs…"])
        self.window.show()
        self.app.processEvents()
        self.assertEqual(self.requests, ["Sungreet Strand"])
        self.window.set_zone("Night Harbor")
        self.assertEqual(self.requests[-1], "Night Harbor")
        self.assertEqual(self.window.notable_npcs.accessibleName(), "Notable NPCs")

    def test_switching_zones_never_displays_a_late_result_for_another_zone(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Captain Brine"])
        self.window.set_zone("Night Harbor")
        self.assertEqual(self.rows(), ["Loading notable NPCs…"])
        self.window.set_notable_npcs("Sungreet Strand", ["Captain Brine", "A Strand Horror"])
        self.assertEqual(self.rows(), ["Loading notable NPCs…"])
        self.window.set_notable_npcs("Night Harbor", ["A Harbor Watcher"])
        self.assertEqual(self.rows(), ["A Harbor Watcher"])
        self.window.set_zone("Sungreet Strand")
        self.assertEqual(self.rows(), ["A Strand Horror", "Captain Brine"])

    def test_cached_names_remain_selectable_when_refresh_is_unavailable(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", [" Captain Brine ", "captain brine"],
                                     "Wiki unavailable; showing cached NPCs")
        actions = self.window.notable_npcs_menu.actions()
        self.assertEqual(self.rows(), ["Captain Brine", "Wiki unavailable; showing cached NPCs"])
        self.assertTrue(actions[0].isEnabled())
        self.assertFalse(actions[-1].isEnabled())
        actions[0].trigger()
        self.assertEqual(self.selections, ["Captain Brine"])

    def test_unknown_and_failed_lists_do_not_claim_the_zone_has_no_npcs(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", [], "Notable NPCs unavailable")
        self.assertEqual(self.rows(), ["Notable NPCs unavailable"])
        self.assertFalse(self.window.notable_npcs_menu.actions()[0].isEnabled())
        self.window.set_notable_npcs("Sungreet Strand", [])
        self.assertEqual(self.rows(), ["No notable NPCs listed"])

    def test_selected_name_is_literal_and_previous_zone_cannot_select_a_target(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Bill & Ben"])
        action = self.window.notable_npcs_menu.actions()[0]
        self.assertEqual(action.text(), "Bill && Ben")
        action.trigger()
        self.assertEqual(self.selections, ["Bill & Ben"])
        self.window.set_zone("Night Harbor")
        self.window._select_npc("Sungreet Strand", "Bill & Ben")
        self.assertEqual(self.selections, ["Bill & Ben"])

    def test_menu_requests_current_zone_and_keeps_header_visible_until_it_closes(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Captain Brine"])
        self.window.show()
        self.app.processEvents()
        self.requests.clear()
        self.window.notable_npcs_menu.popup(self.window.mapToGlobal(QPoint(50, 50)))
        self.app.processEvents()
        self.assertEqual(self.requests, ["Sungreet Strand"])
        self.assertTrue(self.window.header.isVisible())
        cursor = QCursor.pos()
        try:
            # Move the offscreen cursor as well: hiding a widget underneath the
            # actual cursor synthesizes another Enter event for its parent.
            QCursor.setPos(self.window.mapToGlobal(QPoint(200, 200)))
            self.window._hide_header()
            self.assertTrue(self.window.header.isVisible())
            self.window.notable_npcs_menu.hide()
            self.app.processEvents()
            self.window._hide_header()
            self.assertFalse(self.window.header.isVisible())
        finally:
            QCursor.setPos(cursor)

    def test_monster_button_hover_reveals_header_and_shares_font_scale(self):
        self.window.show()
        self.app.processEvents()
        self.window.header.hide()
        button = self.window.notable_npcs
        point = button.rect().center()
        event = QEnterEvent(QPointF(point), QPointF(point), QPointF(button.mapToGlobal(point)))
        self.app.sendEvent(button, event)
        self.assertTrue(self.window.header.isVisible())
        self.assertEqual(button.cursor().shape(), Qt.CursorShape.PointingHandCursor)
        self.window.set_appearance(0.5, 2.0)
        self.assertEqual(button.size(), self.window.wiki_link.size())
        self.assertGreater(button.iconSize().width(), 20)

    def test_level_precedes_literal_name_and_metadata_uses_normalized_titles(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs(
            "Sungreet Strand", ["Bill & Ben", "Fictional Keeper", "Unknown Shade"],
            levels={"BILL_&_BEN": "12 - 15", "fictional keeper": "30", "Unknown Shade": "Unknown"},
            unique_loot=[" BILL & BEN ", "fictional_keeper"], quests=["bill_&_ben"])
        actions = self.window.notable_npcs_menu.actions()
        self.assertEqual(self.rows(), ["Lv. 12–15  Bill && Ben", "Lv. 30  Fictional Keeper", "Unknown Shade"])
        self.assertEqual([action.data() for action in actions], ["Bill & Ben", "Fictional Keeper", "Unknown Shade"])
        self.assertTrue(actions[0].property("notableUniqueLoot"))
        self.assertTrue(actions[0].property("notableQuests"))
        self.assertTrue(actions[1].property("notableUniqueLoot"))
        self.assertFalse(actions[1].property("notableQuests"))
        self.assertIn("Unique loot", actions[0].toolTip())
        self.assertIn("quests", actions[0].toolTip())
        actions[0].trigger()
        self.assertEqual(self.selections, ["Bill & Ben"])

    def test_metadata_only_update_preserves_native_actions_and_keyboard_selection(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper", "Sand Specter"])
        menu = self.window.notable_npcs_menu
        menu.popup(QPoint(20, 20))
        self.app.processEvents()
        actions = menu.actions()
        menu.setActiveAction(actions[1])
        before_y = [menu.actionGeometry(action).y() for action in actions]
        self.window.set_notable_npcs(
            "Sungreet Strand", ["Fictional Keeper", "Sand Specter"],
            levels={"Fictional Keeper": "12–15", "Sand Specter": "18"},
            unique_loot=["Fictional Keeper"], quests=["Sand Specter"])
        self.app.processEvents()
        self.assertEqual(menu.actions(), actions)
        self.assertIs(menu.activeAction(), actions[1])
        self.assertEqual([menu.actionGeometry(action).y() for action in actions], before_y)
        self.assertEqual(self.rows(), ["Lv. 12–15  Fictional Keeper", "Lv. 18  Sand Specter"])
        self.assertTrue(actions[1].property("notableQuests"))
        QTest.keyClick(menu, Qt.Key.Key_Return)
        self.assertEqual(self.selections, ["Sand Specter"])

    def test_membership_changes_wait_for_close_without_retargeting_current_row(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper", "Sand Specter"])
        menu = self.window.notable_npcs_menu
        menu.popup(QPoint(20, 20))
        self.app.processEvents()
        original = menu.actions()
        menu.setActiveAction(original[0])
        self.window.set_notable_npcs("Sungreet Strand", ["A New Arrival", "Fictional Keeper"], "Refreshing…")
        self.app.processEvents()
        self.assertEqual(menu.actions(), original)
        self.assertIs(menu.activeAction(), original[0])
        self.assertEqual([action.data() for action in menu.actions()], ["Fictional Keeper", "Sand Specter"])
        original[0].trigger()
        self.assertEqual(self.selections, ["Fictional Keeper"])
        menu.hide()
        self.app.processEvents()
        self.assertEqual(self.rows(), ["A New Arrival", "Fictional Keeper", "Refreshing…"])

    def test_zone_change_while_open_never_relabels_or_selects_previous_zone(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"], levels={"Fictional Keeper": "10"})
        menu = self.window.notable_npcs_menu
        menu.popup(QPoint(20, 20))
        self.app.processEvents()
        old_action = menu.actions()[0]
        self.window.set_zone("Night Harbor")
        self.window.set_notable_npcs("Night Harbor", ["Other Keeper"], levels={"Other Keeper": "35"})
        self.assertIs(menu.actions()[0], old_action)
        self.assertEqual(old_action.text(), "Lv. 10  Fictional Keeper")
        old_action.trigger()
        self.assertEqual(self.selections, [])
        menu.hide()
        self.app.processEvents()
        self.assertEqual(self.rows(), ["Lv. 35  Other Keeper"])

    def test_live_level_growth_is_clamped_and_does_not_shrink_from_pointer(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"])
        menu = self.window.notable_npcs_menu
        area = self.app.primaryScreen().availableGeometry()
        menu.popup(QPoint(area.right() - 5, area.top() + 20))
        self.app.processEvents()
        action = menu.actions()[0]
        old_geometry = menu.geometry()
        menu.setActiveAction(action)
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"], levels={"Fictional Keeper": "12–15"})
        self.app.processEvents()
        self.assertTrue(area.contains(menu.geometry()))
        self.assertEqual(menu.y(), old_geometry.y())
        self.assertIs(menu.activeAction(), action)
        expanded_width = menu.width()
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"])
        self.app.processEvents()
        self.assertGreaterEqual(menu.width(), expanded_width)
        menu.hide()
        self.app.processEvents()
        self.assertEqual(menu.minimumWidth(), 0)

    def test_both_badges_render_separately_and_fit_scaled_native_rows(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"],
                                     levels={"Fictional Keeper": "12–15"},
                                     unique_loot=["Fictional Keeper"], quests=["Fictional Keeper"])
        menu = self.window.notable_npcs_menu
        for scale in (0.6, 1.0, 2.0):
            with self.subTest(scale=scale):
                self.window.set_appearance(0.85, scale)
                menu.popup(QPoint(20, 20))
                self.app.processEvents()
                action = menu.actions()[0]
                sword, feather = menu.badge_rects(action)
                row = menu.actionGeometry(action)
                self.assertFalse(sword.intersects(feather))
                self.assertTrue(row.contains(sword.toAlignedRect()))
                self.assertTrue(row.contains(feather.toAlignedRect()))
                self.assertGreaterEqual(sword.width(), 14)
                image = menu.grab().toImage()
                for rect, channel in ((sword, "gold"), (feather, "blue")):
                    colored = 0
                    for y in range(int(rect.top()), int(rect.bottom()) + 1):
                        for x in range(int(rect.left()), int(rect.right()) + 1):
                            c = image.pixelColor(x, y)
                            colored += (c.red() > c.blue() + 35 if channel == "gold"
                                        else c.blue() > c.red() + 35)
                    self.assertGreater(colored, 5)
                menu.hide()
                self.app.processEvents()

    def test_long_valid_level_growth_stays_on_screen_after_native_popup_layout(self):
        # Exercise the native popup inset as well as the themed map menu.
        native = NotableNpcMenu()
        self.addCleanup(self.close_popup, native)
        font = native.font()
        font.setPixelSize(24)
        native.setFont(font)
        action = native.addAction("Fictional Keeper")
        action.setData("Fictional Keeper")
        area = self.narrow_screen(native)
        native.popup(QPoint(area.right() - 3, area.bottom() - 3))
        self.app.processEvents()
        native.setActiveAction(action)
        old_y = native.y()
        level = " / ".join(str(n) for n in range(10, 30))
        native.refresh_metadata({"fictional keeper": level}, {"fictional keeper"}, {"fictional keeper"})
        for _ in range(3):
            self.app.processEvents()
        self.assertTrue(area.contains(native.geometry()))
        self.assertLessEqual(native.actionGeometry(action).right(), native.rect().right())
        self.assertEqual(native.y(), old_y)
        self.assertIs(native.activeAction(), action)
        self.assertEqual(action.data(), "Fictional Keeper")
        self.assertIn("…", action.text())
        self.assertTrue(action.text().endswith("Fictional Keeper"))
        self.assertIn(level, action.toolTip())
        for rect in native.badge_rects(action):
            self.assertTrue(native.rect().contains(rect.toAlignedRect()))
        native.hide()
        self.app.processEvents()

    def test_initial_long_level_reserves_visible_badges_and_keeps_literal_selection(self):
        self.window.set_zone("Sungreet Strand")
        self.window.set_appearance(0.85, 2.0)
        level = " / ".join(str(n) for n in range(10, 30))
        self.window.set_notable_npcs("Sungreet Strand", ["Fictional Keeper"],
                                     levels={"Fictional Keeper": level},
                                     unique_loot=["Fictional Keeper"], quests=["Fictional Keeper"])
        menu = self.window.notable_npcs_menu
        area = self.narrow_screen(menu)
        menu.popup(QPoint(area.right() - 3, area.bottom() - 3))
        self.app.processEvents()
        self.assertTrue(area.contains(menu.geometry()))
        action = menu.actions()[0]
        self.assertIn("…", action.text())
        self.assertTrue(action.text().endswith("Fictional Keeper"))
        for rect in menu.badge_rects(action):
            self.assertTrue(menu.rect().contains(rect.toAlignedRect()))
        action.trigger()
        self.assertEqual(self.selections, ["Fictional Keeper"])
        menu.hide()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
