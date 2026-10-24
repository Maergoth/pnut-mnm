"""Expanded snapshots stay grouped, navigable and distinct from local bests."""
from __future__ import annotations

import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPersistentModelIndex, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap, QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

from mnmparse.app import theme
from mnmparse.app.leaderboard_page import (
    COLUMNS, RECORD_ROWS, LeaderboardCardDelegate, LeaderboardModel, LeaderboardPage, record_lines, record_tooltip,
)
from mnmparse.leaderboard import COUNTERS, MAX_NUMBER


def sample(number, **changes):
    fields = {field: number + offset for offset, field in enumerate(field for row in RECORD_ROWS for field in row)}
    return {**fields, "alias": "Fictional Amber Finch", "public_key": str(number),
            "submission_id": str(number), "sequence": 1, "captured_at": "2026-10-22T14:00:00Z", **changes}


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
        legacy = {"alias": "Legacy Gray Owl", "public_key": "legacy", "submission_id": "legacy",
                  "sequence": 1, "captured_at": "2026-10-21T14:00:00Z"}
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

    def test_copy_and_submit_labels_distinguish_current_values_from_personal_bests(self):
        self.page.refresh_status()
        self.assertEqual(self.page.copy_button.text(), "Copy current")
        self.assertEqual(self.page.submit_button.text(), "Submit personal bests")
        self.assertIn("Copy uses this reading", self.page.local_tabs.tabToolTip(0))
        self.assertIn("Submit shares these personal bests", self.page.local_tabs.tabToolTip(1))
        self.assertIn("current mini stats", self.page.copy_button.toolTip())

    def test_currency_is_compact_in_public_accessibility_and_both_local_summaries(self):
        coin_column = next(column for column, (field, _label) in enumerate(COLUMNS) if field == "coin_looted")
        for copper, expected in ((0, "0c"), (99, "99c"), (100, "1s"), (1050, "10s 50c"),
                                 (10_000, "1g"), (1_000_000, "1p"), (1_020_304, "1p 2g 3s 4c"),
                                 (None, "—")):
            with self.subTest(copper=copper):
                entry = sample(10, coin_looted=copper)
                self.page.model.set_records([entry])
                index = self.page.model.index(0, coin_column)
                self.assertEqual(index.data(), expected)
                self.assertIn("Coin looted: " + expected, index.data(Qt.ItemDataRole.AccessibleTextRole))
                self.assertEqual(entry["coin_looted"], copper, "display must retain the original numeric amount")
                self.status["coin_looted"] = copper
                self.page.refresh_status()
                for labels in (self.page.totals, self.page.best_totals):
                    self.assertEqual(labels["coin_looted"].text(), "Coin looted: " + expected)
        self.assertEqual(self.page.model.headerData(coin_column, Qt.Orientation.Horizontal), "Coin looted")
        self.assertGreaterEqual(self.page.sort_field.findText("Coin looted"), 0)

    def test_currency_sort_uses_raw_copper_instead_of_formatted_text(self):
        coin_column = next(column for column, (field, _label) in enumerate(COLUMNS) if field == "coin_looted")
        self.page.model.set_records([sample(1, coin_looted=900), sample(2, coin_looted=1050),
                                     sample(3, coin_looted=None)])
        for order, raw, shown in (
            (Qt.SortOrder.AscendingOrder, [900, 1050, None], ["9s", "10s 50c", "—"]),
            (Qt.SortOrder.DescendingOrder, [1050, 900, None], ["10s 50c", "9s", "—"]),
        ):
            with self.subTest(order=order):
                self.page.model.sort(coin_column, order)
                self.assertEqual([entry["coin_looted"] for entry in self.page.model.records], raw)
                self.assertEqual([self.page.model.index(row, coin_column).data() for row in range(3)], shown)

    def test_paint_stacks_larger_alias_words_bold_and_gold(self):
        class RecordingPainter(QPainter):
            def drawText(self, *args):
                if args[-1].startswith("Coin:"):
                    coin_text.append(args[-1])
                if args[-1] in entry["alias"].split():
                    drawn.append((args[-1], args[0].y(), self.font(), self.pen().color().name()))
                return super().drawText(*args)

        entry = sample(10, alias="Curious Peach Cheetah", coin_looted=1050)
        self.page.model.set_records([entry])
        option = QStyleOptionViewItem()
        option.font = self.page.font()
        option.rect = QRect(0, 0, 1800, 180)
        pixmap = QPixmap(option.rect.size())
        pixmap.fill(QColor(theme.BG0))
        drawn, coin_text = [], []
        painter = RecordingPainter(pixmap)
        LeaderboardCardDelegate().paint(painter, option, self.page.model.index(0, 0))
        painter.end()
        self.assertEqual(coin_text, ["Coin: 10s 50c"])
        self.assertEqual([word for word, _y, _font, _color in drawn], ["Curious", "Peach", "Cheetah"])
        self.assertEqual(len({row[1] for row in drawn}), 3)
        self.assertTrue(all(font.bold() and color == theme.ACCENT for _word, _y, font, color in drawn))
        self.assertTrue(all(font.pointSizeF() > option.font.pointSizeF() if option.font.pointSizeF() > 0
                            else font.pixelSize() > option.font.pixelSize() for _word, _y, font, _color in drawn))

    def test_narrow_view_scrolls_full_values_and_keyboard_selects_whole_cards(self):
        # Force real overflow even when a previous shell test installed a smaller font.
        large_values = {field: 1_000_000_000 for row in RECORD_ROWS for field in row}
        large_values.update(stat_total=7_000_000_000, resist_total=8_000_000_000,
                            **{field: MAX_NUMBER for field in COUNTERS})
        self.page.model.set_records([sample(i, alias="Fictional Golden Antelope", **large_values)
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

    def test_submit_censors_and_normalizes_optional_message_before_engine_call(self):
        record = sample(10, message="This **** is fun!")
        self.engine.leaderboard_submission = Mock(return_value=record)
        self.page.ledger = SimpleNamespace(restore_history=Mock())
        self.page.client = SimpleNamespace(submission_unavailable="", busy=False, submit=Mock(return_value=True))
        self.page.message_input.setText("  This\tshit\nis fun!  ")
        self.assertTrue(self.page.submit_stats())
        self.engine.leaderboard_submission.assert_called_once_with(message="This **** is fun!")
        self.page.client.submit.assert_called_once_with(record)
        self.assertEqual(self.page.message_input.text(), "This **** is fun!")

    def test_empty_message_keeps_optional_submission_and_copy_ignores_draft(self):
        self.engine.leaderboard_submission = Mock(return_value=sample(10))
        self.engine.leaderboard_copy_text = Mock(return_value="Local snapshot without a message")
        self.page.ledger = SimpleNamespace(restore_history=Mock())
        self.page.client = SimpleNamespace(submission_unavailable="", busy=False, submit=Mock(return_value=True))
        self.page.message_input.setText("   ")
        self.assertTrue(self.page.submit_stats())
        self.engine.leaderboard_submission.assert_called_once_with()
        self.page.client.submit.reset_mock()
        self.engine.leaderboard_submission.reset_mock()
        self.page.message_input.setText("Draft shit should stay local until Submit")
        self.assertEqual(self.page.copy_stats(), "Local snapshot without a message")
        self.assertEqual(self.page.message_input.text(), "Draft shit should stay local until Submit")
        self.page.client.submit.assert_not_called()
        self.engine.leaderboard_submission.assert_not_called()

    def test_message_field_is_bounded_and_not_reused_by_a_new_page(self):
        self.assertEqual(self.page.message_input.maxLength(), 320)
        self.assertEqual(self.page.message_input.placeholderText(), "Share a short message…")
        self.page.message_input.setText("x" * 400)
        self.assertEqual(len(self.page.message_input.text()), 320)
        another = LeaderboardPage(self.engine, None, None)
        try:
            self.assertEqual(another.message_input.text(), "")
        finally:
            another.deleteLater()

    def test_message_limit_accepts_160_emoji_and_rejects_161_ascii_without_truncating(self):
        self.engine.leaderboard_submission = Mock(return_value=sample(10))
        self.page.ledger = SimpleNamespace(restore_history=Mock())
        self.page.client = SimpleNamespace(submission_unavailable="", busy=False, submit=Mock(return_value=True))
        emoji = "\U0001f409" * 160
        self.page.message_input.setText(emoji)
        self.assertEqual(self.page.message_input.text(), emoji)
        self.assertTrue(self.page.submit_stats())
        self.engine.leaderboard_submission.assert_called_once_with(message=emoji)
        self.engine.leaderboard_submission.reset_mock()
        self.page.client.submit.reset_mock()
        self.page.message_input.setText("x" * 161)
        self.assertFalse(self.page.submit_stats())
        self.assertEqual(self.page.message_input.text(), "x" * 161)
        self.assertEqual(self.page.message.text(), "Use a message of up to 160 characters.")
        self.engine.leaderboard_submission.assert_not_called()
        self.page.client.submit.assert_not_called()

    def test_normalization_length_failure_does_not_claim_missing_stats_or_submit(self):
        self.engine.leaderboard_submission = Mock()
        self.page.ledger = SimpleNamespace(restore_history=Mock())
        self.page.client = SimpleNamespace(submission_unavailable="", busy=False, submit=Mock())
        # One compatibility ligature expands to three letters under normalization.
        self.page.message_input.setText("\ufb03" * 60)
        self.assertFalse(self.page.submit_stats())
        self.assertEqual(self.page.message.text(), "Use a message of up to 160 characters.")
        self.engine.leaderboard_submission.assert_not_called()
        self.page.client.submit.assert_not_called()

    def test_message_tooltip_escapes_markup_and_places_bubble_above_timestamp(self):
        entry = sample(10, message='<a href="https://example.test">Nice & bright</a>')
        tooltip = record_tooltip(entry)
        self.assertIn("&lt;a href=&quot;https://example.test&quot;&gt;Nice &amp; bright&lt;/a&gt;", tooltip.replace("\u200b", ""))
        self.assertNotIn('<a href=', tooltip)
        self.assertLess(tooltip.index("&lt;a"), tooltip.index("Captured (UTC)"))
        self.assertIn("<table", tooltip)
        self.assertIn("&#9662;", tooltip)
        self.assertTrue(all(entry["message"] not in line for line in record_lines(entry)))
        timestamp_only = record_tooltip(sample(10))
        self.assertIn("Captured (UTC): 2026-10-22 14:00:00", timestamp_only)
        self.assertNotIn("<table", timestamp_only)
        unsafe_stamp = record_tooltip({"captured_at": "<b>not a real timestamp</b>"})
        self.assertIn("&lt;b&gt;", unsafe_stamp)
        self.assertNotIn("<b>", unsafe_stamp)

    def test_personal_best_tooltip_does_not_claim_simultaneous_capture(self):
        best = record_tooltip(sample(10, schema_version=3, message="A fictional record update"))
        self.assertIn("Personal bests as of (UTC): 2026-10-22 14:00:00", best)
        self.assertNotIn("Captured (UTC)", best)
        self.assertLess(best.index("fictional"), best.index("Personal bests as of"))
        for version in (None, 2):
            legacy = record_tooltip(sample(10, schema_version=version))
            self.assertIn("Captured (UTC)", legacy)
            self.assertNotIn("Personal bests as of", legacy)

    def test_long_word_and_emoji_tooltips_wrap_without_changing_message(self):
        for message in ("W" * 160, "\U0001f389" * 160, "a\u0301" * 80):
            with self.subTest(message=message[:4]):
                entry = sample(10, message=message)
                document = QTextDocument()
                document.setDefaultFont(self.app.font())
                document.setHtml(record_tooltip(entry))
                document.setTextWidth(340)
                self.assertLessEqual(document.idealWidth(), 420)
                self.assertIn(message, document.toPlainText().replace("\u200b", ""))
                self.assertEqual(entry["message"], message)


if __name__ == "__main__":
    unittest.main()
