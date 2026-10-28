"""The shared Session view presents complete gathered-item counts in either density."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app import theme
from mnmparse.app.session_view import SessionView
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.session import SessionStats


class HarvestSessionViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def view(self, compact=False):
        view = SessionView(compact)
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.set_config(Config(player_name="Hero"))
        view.resize(380 if compact else 900, 500)
        view.show()
        self.widgets.append(view)
        return view

    @staticmethod
    def category(view, key="harvests"):
        return next(view._tree.topLevelItem(index) for index in range(view._tree.topLevelItemCount())
                    if view._tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) == key)

    @staticmethod
    def gathered_session():
        session = SessionStats("Hero", include_personal=False, started=0)
        for index, item in enumerate(("Wood", "Resin", "Wood")):
            session.add(parse_line(f"You harvest [{item}]!", ts=index + 1, player_name="Hero"))
        return session

    def test_main_and_compact_views_show_total_and_expanded_item_breakdown(self):
        session = self.gathered_session()
        for compact in (False, True):
            with self.subTest(compact=compact):
                view = self.view(compact)
                view.set_snapshot(session.snapshot(now=10))
                self.app.processEvents()
                category = self.category(view)
                self.assertEqual([category.text(0), category.text(1)], ["Harvested", "3"])
                self.assertTrue(category.isExpanded())
                self.assertEqual([(category.child(i).text(0), category.child(i).text(1))
                                  for i in range(category.childCount())], [("Wood", "2"), ("Resin", "1")])
                self.assertIn("Wood: 2", category.toolTip(0))
                self.assertIn("Resin: 1", category.toolTip(0))
                self.assertEqual(self.category(view, "items").text(1), "0")
                self.assertEqual(self.category(view, "crafts").text(1), "0")

    def test_collapse_choice_survives_a_new_harvest_and_snapshot_refresh(self):
        session = self.gathered_session()
        view = self.view()
        view.set_snapshot(session.snapshot(now=10))
        self.category(view).setExpanded(False)
        session.add(parse_line("You harvest [Resin]!", ts=11, player_name="Hero"))
        view.set_snapshot(session.snapshot(now=12))
        self.assertFalse(self.category(view).isExpanded())
        self.assertEqual(self.category(view).text(1), "4")

    def test_breakdown_contains_every_material_even_beyond_other_category_preview_limits(self):
        session = SessionStats("Hero", started=0)
        for index in range(45):
            session.add(parse_line(f"You harvest [Material {index}]!", ts=index + 1, player_name="Hero"))
        view = self.view()
        view.set_snapshot(session.snapshot(now=60, detail_limit=1))
        category = self.category(view)
        self.assertEqual(category.text(1), "45")
        self.assertEqual(category.childCount(), 45)

    def test_zero_state_and_new_session_clear_previous_materials(self):
        view = self.view()
        view.set_snapshot(self.gathered_session().snapshot(now=10))
        view.set_snapshot(SessionStats("Hero", started=20).snapshot(now=21))
        category = self.category(view)
        self.assertEqual(category.text(1), "0")
        self.assertEqual(category.childCount(), 0)
        self.assertIn("Nothing harvested yet", category.toolTip(0))


if __name__ == "__main__":
    unittest.main()
