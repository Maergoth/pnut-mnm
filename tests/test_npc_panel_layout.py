"""NPC column reflow, independent panel widths, and horizontal edge gestures."""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt
from PySide6.QtGui import QFontMetricsF, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.npc_panel import NpcPanel


class NpcPanelLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.settings = QSettings(str(Path(self.directory.name) / "preview.ini"), QSettings.Format.IniFormat)
        self.owner = QWidget()
        self.owner.locked = False
        self.owner.click_through = False
        self.owner.opacity = 0.85
        self.owner.setGeometry(20, 20, 280, 100)
        self.owner.dock_anchor = lambda _panel: self.owner.geometry()
        self.owner.panels_changed = lambda: None
        self.owner.menu_qss = lambda: ""
        self.owner.show()
        self.area = QRect(0, 0, 1600, 1000)
        self.screen = SimpleNamespace(availableGeometry=lambda: QRect(self.area))
        self.patches = ExitStack()
        self.patches.enter_context(patch.object(self.owner, "screen", return_value=self.screen))
        self.panels = []
        self.panel = self.make_panel()

    def make_panel(self):
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        panel = NpcPanel(self.owner, self.settings, flags)
        self.patches.enter_context(patch.object(panel, "screen", return_value=self.screen))
        self.panels.append(panel)
        panel.show_npc("Layout sample NPC")
        return panel

    def tearDown(self):
        for panel in self.panels:
            panel.close()
            panel.deleteLater()
        self.owner.close()
        self.owner.deleteLater()
        self.app.processEvents()
        self.patches.close()
        self.settings.sync()
        self.directory.cleanup()

    @staticmethod
    def details(*, count=24, long=False, all_fields=False):
        suffix = " with several words for wrapping across the column" if long else ""
        return SimpleNamespace(
            name="Layout sample NPC", level="23–25",
            common_drops=tuple(f"Common sample {i}{suffix}" for i in range(6)) if all_fields else (),
            unique_drops=tuple(f"Unique sample {i}{suffix}" for i in range(count)),
            quests=tuple(f"Sample quest {i}{suffix}" for i in range(3)) if all_fields else (),
            reputation=("Sample allies +5", "Sample rivals −10") if all_fields else (),
            source="",
        )

    def populate(self, *, count=24, long=False, all_fields=False):
        details = self.details(count=count, long=long, all_fields=all_fields)
        self.panel.configure(common_drops=all_fields, quests=all_fields, reputation=all_fields)
        self.panel.set_details(details)
        self.app.processEvents()
        return details

    def assert_items_once(self, details):
        items = [row.item for _rect, row in self.panel._row_rects if row.item]
        self.assertEqual(Counter(items), Counter(details.common_drops + details.unique_drops))

    def assert_rows_fit(self):
        panel = self.panel
        for rect, row in panel._row_rects:
            self.assertGreater(rect.width(), 0)
            self.assertGreaterEqual(rect.left(), panel._body.left() - 1)
            self.assertLessEqual(rect.right(), panel._body.right() + 1)
            font = panel._heading_font if row.kind == "heading" else panel._font
            needed = QFontMetricsF(font).boundingRect(
                QRectF(0, 0, rect.width(), 100000), int(Qt.TextFlag.TextWordWrap), row.text,
            ).height()
            self.assertGreaterEqual(rect.height() + 1, needed, row.text)
        by_column = {}
        for rect, row in panel._row_rects:
            by_column.setdefault(round(rect.left(), 2), []).append((rect, row))
        self.assertEqual(len(by_column), panel._column_count)
        for rows in by_column.values():
            ordered = sorted(rows, key=lambda pair: pair[0].top())
            for (previous, _row), (following, _next_row) in zip(ordered, ordered[1:]):
                self.assertLessEqual(previous.bottom(), following.top() + 1)

    def test_width_and_font_matrix_wraps_every_item_once_without_overlapping_columns(self):
        details = self.populate(count=35, long=True, all_fields=True)
        for scale in (0.6, 1.0, 2.0):
            for width in (260, 500, 900):
                with self.subTest(scale=scale, width=width):
                    self.panel.set_font_px(13 * scale)
                    self.panel.set_panel_width(width)
                    self.app.processEvents()
                    self.assertEqual(self.panel.width(), width)
                    self.assertGreaterEqual(self.panel._column_count, 1)
                    self.assert_items_once(details)
                    self.assert_rows_fit()

    def test_widening_long_content_uses_columns_before_vertical_scroll(self):
        details = self.populate(count=24)
        self.panel.set_panel_width(260)
        self.assertEqual(self.panel._column_count, 1)
        self.assertGreater(self.panel._scroll.maximum(), 0)
        self.panel.set_panel_width(900)
        self.assertGreater(self.panel._column_count, 1)
        self.assertEqual(self.panel._scroll.maximum(), 0)
        self.assertLessEqual(self.panel._body.height(), 280)
        self.assert_items_once(details)
        self.assert_rows_fit()

    def test_short_content_does_not_spread_into_unneeded_columns(self):
        self.populate(count=2)
        self.panel.set_panel_width(900)
        self.assertEqual(self.panel._column_count, 1)
        self.assertEqual(self.panel._scroll.maximum(), 0)

    def test_unbroken_long_item_token_wraps_inside_its_column(self):
        item = "Sample" + "M" * 100
        details = self.details(count=0)
        details.unique_drops = (item,)
        self.panel.configure(level=False)
        self.panel.set_details(details)
        self.panel.set_font_px(26)
        self.panel.set_panel_width(260)
        rect, row = next((rect, row) for rect, row in self.panel._row_rects if row.item == item)
        fm = QFontMetricsF(self.panel._font)
        self.assertGreater(fm.horizontalAdvance(item), rect.width() * 3)
        wrapped = fm.boundingRect(QRectF(0, 0, rect.width(), 100000),
                                  int(Qt.TextFlag.TextWrapAnywhere), row.text)
        self.assertGreater(rect.height(), fm.height() * 3)
        self.assertGreaterEqual(rect.height() + 1, wrapped.height())
        self.assertLessEqual(rect.right(), self.panel._body.right())
        self.assert_items_once(details)

    def test_section_headings_stay_with_their_first_row_when_columns_break(self):
        self.populate(count=29, all_fields=True)
        self.panel.set_panel_width(900)
        self.assertGreater(self.panel._column_count, 1)
        columns = {}
        for rect, row in self.panel._row_rects:
            columns.setdefault(round(rect.left(), 2), []).append((rect, row))
        for rows in columns.values():
            rows.sort(key=lambda pair: pair[0].top())
            for index, (_rect, row) in enumerate(rows):
                if row.kind == "heading":
                    self.assertLess(index, len(rows) - 1, row.text)
                    self.assertNotEqual(rows[index + 1][1].kind, "heading", row.text)

    def test_item_hit_testing_reaches_every_column_and_scrolled_last_item(self):
        details = self.populate(count=140)
        self.panel.set_panel_width(900)
        self.assertGreater(self.panel._column_count, 1)
        self.assertGreater(self.panel._scroll.maximum(), 0)
        seen_columns = set()
        for rect, row in self.panel._row_rects:
            if not row.item:
                continue
            offset = max(0, min(self.panel._scroll.maximum(), round(rect.top())))
            self.panel._scroll.setValue(offset)
            point = rect.center().toPoint() + QPoint(0, int(self.panel._body.top()) - offset)
            if self.panel._body.contains(point):
                self.assertEqual(self.panel._item_at(point), row.item)
                seen_columns.add(round(rect.left(), 2))
        self.assertEqual(len(seen_columns), self.panel._column_count)
        self.panel._scroll.setValue(self.panel._scroll.maximum())
        last_rect, last_row = next((rect, row) for rect, row in reversed(self.panel._row_rects)
                                  if row.item == details.unique_drops[-1])
        point = last_rect.center().toPoint() + QPoint(
            0, int(self.panel._body.top()) - self.panel._scroll.value(),
        )
        self.assertEqual(self.panel._item_at(point), last_row.item)

    def test_available_screen_height_caps_the_panel_and_keeps_overflow_scrollable(self):
        self.area = QRect(0, 0, 1600, 240)
        details = self.populate(count=120, long=True, all_fields=True)
        self.panel.set_panel_width(900)
        self.panel.set_status("Cached data")
        self.app.processEvents()
        anchor = self.owner.dock_anchor(self.panel)
        available = max(anchor.top() - self.area.top() - 4, self.area.bottom() - anchor.bottom() - 4)
        self.assertLessEqual(self.panel.height(), available)
        self.assertTrue(self.area.contains(self.panel.geometry()))
        self.assertLess(self.panel._body.bottom(), self.panel.height())
        self.assertGreater(self.panel._scroll.maximum(), 0)
        self.assert_items_once(details)
        self.assert_rows_fit()

    def test_custom_width_survives_docking_owner_movement_and_font_reflow(self):
        self.populate(count=24)
        self.panel.set_panel_width(500)
        self.assertTrue(self.panel.docked)
        self.assertEqual(self.settings.value("npc_panel/width", type=int), 500)
        self.owner.setGeometry(60, 50, 340, 100)
        self.panel.follow()
        self.panel.set_font_px(26)
        self.panel.dock()
        self.assertTrue(self.panel.docked)
        self.assertEqual(self.panel.width(), 500)
        self.assertEqual(self.panel.x(), self.owner.geometry().left())
        self.settings.sync()
        reopened = self.make_panel()
        reopened.set_details(self.details())
        self.assertEqual(reopened.width(), 500)
        self.assertTrue(reopened.docked)

    def test_detached_custom_width_survives_content_updates_and_redocking(self):
        self.populate(count=24)
        self.panel._docked = False
        self.panel._free_geometry = QRect(350, 200, 280, self.panel.height())
        self.panel.sync()
        self.panel.set_panel_width(500)
        self.panel.set_font_px(20)
        self.panel.set_details(self.details(count=9, long=True))
        self.assertEqual(self.panel.width(), 500)
        self.assertEqual(self.panel.pos(), QPoint(350, 200))
        self.assertFalse(self.panel.docked)
        self.panel.dock()
        self.assertEqual(self.panel.width(), 500)
        self.assertEqual(self.panel.x(), self.owner.dock_anchor(self.panel).left())

    def test_detached_panel_is_clamped_to_available_screen_after_width_or_height_changes(self):
        self.populate(count=24)
        self.panel._docked = False
        self.panel._free_geometry = QRect(1450, 950, 280, self.panel.height())
        self.panel.sync()
        self.panel.set_panel_width(500)
        self.panel.set_font_px(26)
        self.app.processEvents()
        self.assertEqual(self.panel.width(), 500)
        self.assertTrue(self.area.contains(self.panel.geometry()))

    def mouse(self, kind, global_position, *, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        event = QMouseEvent(kind, QPointF(self.panel.mapFromGlobal(global_position)), QPointF(global_position),
                            button, buttons, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.panel, event)

    def start_edge_drag(self, edge):
        local = QPoint(1 if edge == "left" else self.panel.width() - 2, self.panel.height() // 2)
        start = self.panel.mapToGlobal(local)
        self.mouse(QEvent.Type.MouseButtonPress, start,
                   button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
        return start

    def drag_edge(self, edge, delta):
        start = self.start_edge_drag(edge)
        self.mouse(QEvent.Type.MouseMove, start + QPoint(delta, 0), buttons=Qt.MouseButton.LeftButton)
        self.mouse(QEvent.Type.MouseButtonRelease, start + QPoint(delta, 0), button=Qt.MouseButton.LeftButton)
        self.app.processEvents()

    def test_right_edge_drag_changes_width_without_undocking_or_moving_owner(self):
        self.populate(count=10)
        self.panel.set_panel_width(500)
        owner_geometry, left = self.owner.geometry(), self.panel.x()
        self.drag_edge("right", 90)
        self.assertEqual(self.panel.width(), 590)
        self.assertTrue(self.panel.docked)
        self.assertEqual(self.panel.x(), left)
        self.assertEqual(self.owner.geometry(), owner_geometry)
        self.assertEqual(self.settings.value("npc_panel/width", type=int), 590)

    def test_locked_and_click_through_edge_drags_do_not_resize_move_or_undock(self):
        self.populate(count=10)
        self.panel.set_panel_width(500)
        for locked, click_through in ((True, False), (False, True)):
            with self.subTest(locked=locked, click_through=click_through):
                self.owner.locked, self.owner.click_through = locked, click_through
                self.panel.sync()
                before = self.panel.geometry()
                self.drag_edge("right", 90)
                self.assertEqual(self.panel.geometry(), before)
                self.assertTrue(self.panel.docked)

    def test_left_edge_drag_keeps_right_edge_fixed_and_persists_detached_width(self):
        self.populate(count=10)
        self.owner.move(200, 20)
        self.panel.set_panel_width(500)
        self.panel.follow()
        right = self.panel.geometry().right()
        self.drag_edge("left", -70)
        self.assertEqual(self.panel.width(), 570)
        self.assertEqual(self.panel.geometry().right(), right)
        self.assertFalse(self.panel.docked)
        self.assertFalse(self.settings.value("npc_panel/docked", True, type=bool))
        self.assertEqual(self.settings.value("npc_panel/width", type=int), 570)
        self.assertEqual(self.settings.value("npc_panel/geometry"), self.panel.geometry())

    def test_left_edge_click_without_drag_does_not_detach(self):
        self.populate(count=10)
        self.panel.set_panel_width(500)
        before = self.panel.geometry()
        self.drag_edge("left", 0)
        self.assertTrue(self.panel.docked)
        self.assertEqual(self.panel.geometry(), before)

    def test_resize_out_and_back_restores_original_width_before_release(self):
        self.populate(count=10)
        self.owner.setGeometry(200, 20, 280, 100)
        for edge, delta in (("left", -70), ("right", 70)):
            with self.subTest(edge=edge):
                self.panel.dock()
                self.panel.set_panel_width(500)
                start = self.start_edge_drag(edge)
                self.mouse(QEvent.Type.MouseMove, start + QPoint(delta, 0), buttons=Qt.MouseButton.LeftButton)
                self.assertEqual(self.panel.width(), 570)
                self.mouse(QEvent.Type.MouseMove, start, buttons=Qt.MouseButton.LeftButton)
                self.assertEqual(self.panel.width(), 500)
                self.mouse(QEvent.Type.MouseButtonRelease, start, button=Qt.MouseButton.LeftButton)
                self.assertEqual(self.settings.value("npc_panel/width", type=int), 500)

    def test_hiding_during_resize_cancels_gesture_and_no_button_motion_cannot_resize(self):
        self.populate(count=10)
        self.panel.set_panel_width(500)
        start = self.start_edge_drag("right")
        self.mouse(QEvent.Type.MouseMove, start + QPoint(80, 0), buttons=Qt.MouseButton.LeftButton)
        self.assertEqual(self.panel.width(), 580)
        self.panel.hide()
        self.assertIsNone(self.panel._resize_origin)
        self.assertFalse(self.panel._lifetime._interacting)
        self.assertEqual(self.settings.value("npc_panel/width", type=int), 580)
        self.mouse(QEvent.Type.MouseMove, start + QPoint(160, 0))
        self.assertEqual(self.panel.width(), 580)
        self.panel.sync()
        self.assertTrue(self.panel.isVisible())
        self.mouse(QEvent.Type.MouseMove, start + QPoint(200, 0))
        self.assertEqual(self.panel.width(), 580)
        self.assertTrue(self.panel.docked)

    def test_lock_or_click_through_mid_resize_clears_state_and_saves_chosen_width(self):
        self.populate(count=10)
        for change in ("lock", "click_through"):
            with self.subTest(change=change):
                try:
                    self.owner.locked = self.owner.click_through = False
                    self.panel.sync()
                    self.panel.set_panel_width(500)
                    start = self.start_edge_drag("right")
                    self.mouse(QEvent.Type.MouseMove, start + QPoint(80, 0), buttons=Qt.MouseButton.LeftButton)
                    self.assertEqual(self.panel.width(), 580)
                    if change == "lock":
                        self.owner.locked = True
                        # OverlayWindow.set_locked updates each detached panel's cursor.
                        self.panel.update_cursor()
                    else:
                        self.owner.click_through = True
                        # OverlayWindow.set_click_through synchronizes each panel.
                        self.panel.sync()
                    self.assertIsNone(self.panel._resize_origin)
                    self.assertEqual(self.panel._resize_edge, "")
                    self.assertFalse(self.panel._lifetime._interacting)
                    self.assertEqual(self.settings.value("npc_panel/width", type=int), 580)
                    before = self.panel.geometry()
                    self.mouse(QEvent.Type.MouseMove, start + QPoint(160, 0))
                    self.mouse(QEvent.Type.MouseButtonRelease, start + QPoint(160, 0), button=Qt.MouseButton.LeftButton)
                    self.assertEqual(self.panel.geometry(), before)
                    self.assertTrue(self.panel.docked)
                finally:
                    self.panel.hide()

    def test_docked_panel_uses_owner_screen_when_its_own_screen_is_stale(self):
        self.populate(count=24)
        owner_area = QRect(1600, 0, 1200, 600)
        owner_screen = SimpleNamespace(availableGeometry=lambda: QRect(owner_area))
        with patch.object(self.owner, "screen", return_value=owner_screen):
            self.owner.setGeometry(1680, 100, 280, 100)
            self.panel.set_panel_width(500)
            self.panel.follow()
            self.assertTrue(self.panel.docked)
            self.assertEqual(self.panel.x(), self.owner.dock_anchor(self.panel).left())
            self.assertTrue(owner_area.contains(self.panel.geometry()))

    def test_detached_panel_uses_its_own_screen_when_owner_is_on_another_screen(self):
        self.populate(count=24)
        owner_area = QRect(1600, 0, 1200, 600)
        owner_screen = SimpleNamespace(availableGeometry=lambda: QRect(owner_area))
        with patch.object(self.owner, "screen", return_value=owner_screen):
            self.owner.setGeometry(1680, 100, 280, 100)
            self.panel._docked = False
            self.panel._free_geometry = QRect(350, 200, 500, self.panel.height())
            self.panel.sync()
            self.panel.set_panel_width(1300)
            self.assertEqual(self.panel.width(), 1300)
            self.assertFalse(self.panel.docked)
            self.assertTrue(self.area.contains(self.panel.geometry()))


if __name__ == "__main__":
    unittest.main()
