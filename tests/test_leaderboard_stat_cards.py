"""Expanded snapshots stay grouped, navigable and distinct from local bests."""
from __future__ import annotations

import os
from types import SimpleNamespace
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPersistentModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from mnmparse.app import theme
from mnmparse.app.leaderboard_page import (
    COLUMNS, RECORD_ROWS, LeaderboardCardDelegate, LeaderboardModel, LeaderboardPage, record_lines,
)


def sample(number, **changes):
    fields = {field: number + offset for offset, field in enumerate(field for row in RECORD_ROWS for field in row)}
    return {**fields, "alias": "Fictional Amber Finch", "public_key": str(number),
            "submission_id": str(number), "captured_at": "2026-10-22T14:00:00Z", **changes}


class StatCardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.status = {"alias": "Fictional Amber Finch", "eligible": True, "copy_eligible": True,
                       "records": {}, "submitted_day": "", "error": ""}
        self.current = sample(10)
        self.engine = SimpleNamespace(leaderboard_status=lambda: self.status,
                                      player_stat_snapshot=lambda: self.current)
        self.page = LeaderboardPage(self.engine, None, None)
        self.page.ledger = object()

    def tearDown(self):
        self.page.close()
        self.page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_all_fields_form_three_lines_without_visible_timestamp(self):
        entry = sample(10)
        lines = record_lines(entry)
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith(entry["alias"] + " · ") for line in lines))
        self.assertEqual(len({field for row in RECORD_ROWS for field in row}), 24)
        self.assertIn("CHA:", lines[1])
        self.assertIn("Resist Total:", lines[2])
        self.assertTrue(all(entry["captured_at"] not in line for line in lines))
        self.page.model.set_records([entry])
        index = self.page.model.index(0, 0)
        self.assertEqual(index.data(Qt.ItemDataRole.AccessibleTextRole).splitlines(), list(lines))
        self.assertIn("2026-10-22 14:00:00", index.data(Qt.ItemDataRole.ToolTipRole))

    def test_every_numeric_sort_keeps_legacy_missing_last_and_cards_intact(self):
        model = LeaderboardModel()
        fields = [field for row in RECORD_ROWS for field in row]
        legacy = {"alias": "Legacy Gray Owl", "public_key": "legacy", "submission_id": "legacy"}
        for field in fields:
            with self.subTest(field=field):
                model.set_records([legacy, sample(1, **{field: 100}), sample(2, **{field: -9})])
                column = next(i for i, entry in enumerate(COLUMNS) if entry[0] == field)
                for order, expected in ((Qt.SortOrder.AscendingOrder, [-9, 100, None]),
                                        (Qt.SortOrder.DescendingOrder, [100, -9, None])):
                    model.sort(column, order)
                    self.assertEqual([entry.get(field) for entry in model.records], expected)
                    self.assertEqual(model.rowCount(), 3)
                    self.assertTrue(all(len(record_lines(entry)) == 3 for entry in model.records))
        self.assertIn("STR: —", record_lines(legacy)[1])

    def test_sort_ties_and_selected_snapshot_remain_stable(self):
        model = LeaderboardModel()
        entries = [sample(3, high_hit=100), sample(1, high_hit=100), sample(2, high_hit=9)]
        model.set_records(entries)
        chosen = QPersistentModelIndex(model.index(next(i for i, entry in enumerate(model.records)
                                                      if entry["public_key"] == "3"), 0))
        for order in (Qt.SortOrder.AscendingOrder, Qt.SortOrder.DescendingOrder):
            model.sort(5, order)
            self.assertEqual(model.records[chosen.row()]["public_key"], "3")
            self.assertEqual([entry["public_key"] for entry in model.records if entry["high_hit"] == 100], ["1", "3"])

    def test_local_best_is_separate_and_never_invents_missing_values_or_totals(self):
        self.status["records"] = {
            "strength": {"value": 90, "captured_at": "2026-10-21T12:00:00Z"},
            "stat_total": {"value": 345, "captured_at": "2026-10-21T13:00:00Z"},
        }
        self.current.update(strength=12, stat_total=140, resist_magic=None)
        self.page.refresh_status()
        self.assertEqual(self.page.totals["strength"].text(), "STR: 12")
        self.assertEqual(self.page.best_totals["strength"].text(), "STR: 90")
        self.assertEqual(self.page.best_totals["stat_total"].text(), "Stat Total: 345")
        self.assertIn("2026-10-21T12:00:00Z", self.page.best_totals["strength"].toolTip())
        self.assertEqual(self.page.totals["resist_magic"].text(), "Magic: —")
        self.assertEqual(self.page.best_totals["resist_magic"].text(), "Magic: —")
        self.current = None
        self.status["records"] = {}
        self.page.refresh_status()
        self.assertEqual(self.page.totals["strength"].text(), "STR: —")
        self.assertEqual(self.page.best_totals["strength"].text(), "STR: —")

    def test_paint_repeats_full_alias_bold_and_gold_on_each_line(self):
        class RecordingPainter(QPainter):
            def drawText(self, *args):
                if args[-1] == entry["alias"]:
                    drawn.append((args[0].y(), self.font().bold(), self.pen().color().name()))
                return super().drawText(*args)

        entry = sample(10, alias="Fictional Golden Antelope")
        self.page.model.set_records([entry])
        option = QStyleOptionViewItem()
        option.font = self.page.font()
        option.rect = QRect(0, 0, 1800, 180)
        pixmap = QPixmap(option.rect.size())
        pixmap.fill(QColor(theme.BG0))
        drawn = []
        painter = RecordingPainter(pixmap)
        LeaderboardCardDelegate().paint(painter, option, self.page.model.index(0, 0))
        painter.end()
        self.assertEqual(len(drawn), 3)
        self.assertEqual(len({row[0] for row in drawn}), 3)
        self.assertTrue(all(bold and color == theme.ACCENT for _y, bold, color in drawn))

    def test_narrow_view_scrolls_full_values_and_keyboard_selects_whole_cards(self):
        self.page.model.set_records([sample(i, alias="Fictional Golden Antelope", high_hit=1_000_000_000)
                                     for i in range(10)])
        self.page.resize(700, 740)
        self.page.show()
        self.app.processEvents()
        view = self.page.table
        self.assertGreater(view.horizontalScrollBar().maximum(), 0)
        self.assertGreater(view.verticalScrollBar().maximum(), 0)
        view.setCurrentIndex(self.page.model.index(0, 0))
        QTest.keyClick(view, Qt.Key.Key_Down)
        self.assertEqual(view.currentIndex().row(), 1)
        chosen = self.page.model.records[1]["public_key"]
        column = next(i for i, entry in enumerate(COLUMNS) if entry[0] == "charisma")
        self.page.sort_field.setCurrentIndex(self.page.sort_field.findData(column))
        self.assertEqual(self.page.model.records[view.currentIndex().row()]["public_key"], chosen)
        view.horizontalScrollBar().setValue(view.horizontalScrollBar().maximum())
        self.assertGreater(view.horizontalScrollBar().value(), 0)


if __name__ == "__main__":
    unittest.main()
