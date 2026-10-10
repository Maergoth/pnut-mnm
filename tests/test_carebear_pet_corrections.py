"""Carebear can fix your own pet attribution without listing teammate choices."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QMenu

from mnmparse.app.main import App, _MissingEngine
from mnmparse.app.models import build_snapshot
from mnmparse.app.overlay import OverlayWindow, add_own_pet_entries
from mnmparse.app.pages import LivePage
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.privacy import project_encounter
from mnmparse.stats import Stats

PLAYER = "Owner"
CFG = Config(player_name=PLAYER)


def fight(owner=None):
    stats = Stats(player_name=PLAYER)
    stats.roster.set_manual("Tamsin", True)
    stats.roster.set_manual("Wenna", True)
    if owner:
        stats.roster.set_pet_owner("Sprout", owner)
    for ts, line in enumerate(("You crush a rat for 20 points of damage.",
                               "Sprout slashes a rat for 30 points of damage.",
                               "Tamsin crushes a rat for 10 points of damage.",
                               "Wenna crushes a rat for 15 points of damage."), 100):
        stats.add(parse_line(line, ts, PLAYER))
    return build_snapshot(stats, stats.current(), PLAYER, now=105)


class CarebearPetCorrectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "test.ini"), QSettings.Format.IniFormat)
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def keep(self, widget):
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.widgets.append(widget)
        return widget

    def test_menu_assigns_only_to_self_and_clears_only_included_pet(self):
        snap = project_encounter(fight(PLAYER), CFG)
        own = next(row for row in snap.rows if row.is_you)
        menu = self.keep(QMenu())
        calls = []
        handlers = add_own_pet_entries(menu, own, PLAYER, lambda *args: calls.append(args))
        assign = next(action for action in handlers if action.text() == "Assign a pet to me…")
        with patch("mnmparse.app.overlay.QInputDialog.getText", return_value=(" a charmed rat ", True)):
            handlers[assign]()
        clear = next(action for action in handlers if action.text() == "Clear assignment for Sprout")
        handlers[clear]()
        self.assertEqual(calls, [("a charmed rat", PLAYER), ("Sprout", None)])
        captions = " ".join(action.text() for action in handlers)
        self.assertNotIn("Tamsin", captions)
        self.assertNotIn("Wenna", captions)
        average = next(row for row in snap.rows if not row.is_you)
        other_menu = self.keep(QMenu())
        self.assertEqual(add_own_pet_entries(other_menu, average, PLAYER, Mock()), {})
        self.assertEqual(other_menu.actions(), [])

    def test_cancelled_or_empty_pet_name_does_not_change_attribution(self):
        own = next(row for row in project_encounter(fight(), CFG).rows if row.is_you)
        menu = self.keep(QMenu())
        emit = Mock()
        handlers = add_own_pet_entries(menu, own, PLAYER, emit)
        for result in (("Sprout", False), ("  ", True)):
            with self.subTest(result=result), patch("mnmparse.app.overlay.QInputDialog.getText", return_value=result):
                next(iter(handlers.values()))()
        emit.assert_not_called()

    def test_main_and_overlay_context_menus_offer_own_assignment(self):
        snap = fight()
        page = self.keep(LivePage(_MissingEngine(CFG), CFG, self.settings))
        page.set_history([snap])
        self.assertTrue(page.select_key(snap.key))
        overlay = self.keep(OverlayWindow(self.settings, CFG))
        overlay.set_snapshot(snap)
        overlay._flush_snapshot()
        overlay.set_tab("damage")
        overlay._view_mode = "self"
        event = SimpleNamespace(globalPos=lambda: QPoint(0, 0))

        class AssignmentMenu(QMenu):
            def exec(self, _pos):
                return next(action for action in self.actions() if action.text() == "Assign a pet to me…")

        for widget, table in ((page, page._pane.table), (overlay, overlay._table)):
            with self.subTest(surface=type(widget).__name__):
                own = next(row for row in project_encounter(snap, CFG).rows if row.is_you)
                requested = []
                widget.pet_owner_requested.connect(lambda *args: requested.append(args))
                with patch.object(table, "row_at", return_value=own), \
                        patch("mnmparse.app.pages.QMenu", AssignmentMenu), \
                        patch("mnmparse.app.overlay.QMenu", AssignmentMenu), \
                        patch("mnmparse.app.overlay.QInputDialog.getText", return_value=("Sprout", True)):
                    widget.contextMenuEvent(event)
                self.assertEqual(requested, [("Sprout", PLAYER)])

    def test_app_accepts_own_assignment_and_clear_but_rejects_peer_edits(self):
        engine = SimpleNamespace(set_pet_owner=Mock(), pet_owners=lambda: {"Sprout": PLAYER, "PeerPet": "Tamsin"})
        shell = SimpleNamespace(cfg=CFG, engine=engine, window=None)
        App.set_pet_owner(shell, "Sprout", "owner")
        App.set_pet_owner(shell, "Sprout", None)
        App.set_pet_owner(shell, "PeerPet", None)
        App.set_pet_owner(shell, "Sprout", "Tamsin")
        self.assertEqual(engine.set_pet_owner.call_args_list,
                         [unittest.mock.call("Sprout", PLAYER), unittest.mock.call("Sprout", None)])
        shell.cfg = Config()
        App.set_pet_owner(shell, "Sprout", PLAYER)
        self.assertEqual(engine.set_pet_owner.call_count, 2)

    def test_archived_own_assignment_updates_projected_view_and_rejects_peer_assignment(self):
        engine = Mock()
        page = self.keep(LivePage(engine, CFG, self.settings))
        raw = fight()
        assigned = fight(PLAYER)
        engine.correct_archived_encounter.return_value = assigned
        page.set_archive_history("archive-id", [raw])
        self.assertTrue(page.select_key(raw.key))
        self.assertTrue(page.correct_selected_archive(pet="Sprout", owner=PLAYER))
        engine.correct_archived_encounter.assert_called_once_with(raw.key, session_id="archive-id",
                                                                  pet="Sprout", owner=PLAYER)
        shown = page.selected()
        self.assertEqual(next(row for row in shown.rows if row.is_you).damage, 50)
        self.assertFalse(any(row.name in {"Tamsin", "Wenna"} for row in shown.rows))
        engine.correct_archived_encounter.reset_mock()
        self.assertTrue(page.correct_selected_archive(pet="Sprout", owner="Tamsin"))
        self.assertTrue(page.correct_selected_archive(name="Tamsin", in_group=True))
        engine.correct_archived_encounter.assert_not_called()
        engine.correct_archived_encounter.return_value = raw
        self.assertTrue(page.correct_selected_archive(pet="Sprout", owner=None))
        engine.correct_archived_encounter.assert_called_once_with(raw.key, session_id="archive-id",
                                                                  pet="Sprout", owner=None)
        self.assertEqual(next(row for row in page.selected().rows if row.is_you).damage, 20)


if __name__ == "__main__":
    unittest.main()
