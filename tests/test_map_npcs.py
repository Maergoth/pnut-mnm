"""The notable NPC picker keeps each zone's cached results and hover controls."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QCursor, QEnterEvent
from PySide6.QtWidgets import QApplication

from mnmparse.app.map_overlay import MapOverlay


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


if __name__ == "__main__":
    unittest.main()
