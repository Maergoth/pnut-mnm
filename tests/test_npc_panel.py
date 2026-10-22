"""Considered NPC content, passive item previews, and bounded docking behavior."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QDesktopServices, QFontMetricsF, QMouseEvent
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.npc_panel import NpcPanel


class NpcPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.owner = QWidget()
        self.owner.locked = True
        self.owner.click_through = False
        self.owner.opacity = 0.8
        self.owner.setGeometry(20, 20, 280, 100)
        self.owner.dock_anchor = lambda panel: self.owner.geometry()
        self.owner.panels_changed = lambda: None
        self.owner.menu_qss = lambda: ""
        self.owner.show()
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.panel = NpcPanel(self.owner, self.settings, flags)
        self.panel.resize(280, self.panel.height())

    def tearDown(self) -> None:
        self.panel.close()
        self.panel.deleteLater()
        self.owner.close()
        self.owner.deleteLater()
        self.app.processEvents()
        self.settings.sync()
        self.directory.cleanup()

    @staticmethod
    def details(**overrides):
        values = dict(name="Sample Keeper", level="23–25", common_drops=("Sample coin",),
                      unique_drops=("Sample sigil staff",), quests=("Sample quest",),
                      reputation=("Sample watchers +10", "Sample rivals −10"), source="https://example.invalid")
        values.update(overrides)
        return SimpleNamespace(**values)

    def row_text(self) -> str:
        return "\n".join(row.text for row in self.panel._rows)

    def mouse(self, kind, global_position, *, button=Qt.MouseButton.NoButton, buttons=Qt.MouseButton.NoButton):
        event = QMouseEvent(kind, QPointF(self.panel.mapFromGlobal(global_position)), QPointF(global_position),
                            button, buttons, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(self.panel, event)

    def click(self, local, *, button=Qt.MouseButton.LeftButton):
        position = self.panel.mapToGlobal(local)
        self.mouse(QEvent.Type.MouseButtonPress, position, button=button, buttons=button)
        self.mouse(QEvent.Type.MouseButtonRelease, position, button=button)

    def item_point(self):
        rect, _ = next((rect, row) for rect, row in self.panel._row_rects if row.item)
        return rect.center().toPoint() + QPoint(0, int(self.panel._body.top()) - self.panel._scroll.value())

    def test_default_fields_wait_for_data_without_empty_headings_or_placeholders(self) -> None:
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.panel.show_npc("Sample Keeper")
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel.options, dict(enabled=True, level=True, common_drops=False,
                                                unique_drops=True, quests=False, reputation=False))
        self.assertEqual(self.row_text(), "")
        self.assertEqual(self.panel._status, "Loading NPC information…")
        self.panel.set_details(self.details(unique_drops=()))
        self.assertEqual(self.row_text(), "Level: 23–25")

    def test_unavailable_fields_are_hidden_while_request_status_remains_visible(self) -> None:
        self.panel.show_npc("Sample Keeper")
        self.panel.configure(common_drops=True, quests=True, reputation=True)
        self.panel.set_details(self.details(level="Unknown", common_drops=(), unique_drops=(),
            quests=("Not available in wiki data",), reputation=("Lost: None", "Gained: Unknown")),
            "Wiki unavailable; showing cached information")
        self.assertEqual(self.row_text(), "")
        self.assertEqual(self.panel._status, "Wiki unavailable; showing cached information")
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel._scroll.maximum(), 0)

    def test_common_drops_exclude_overlap_from_unique_even_when_common_is_disabled(self) -> None:
        # Model-less cached objects must obey the same rule as fresh records.
        details = self.details(common_drops=("Sample_Coin",),
                               unique_drops=("  sample   COIN ", "Sample sigil staff"))
        self.panel.show_npc(details.name)
        self.panel.set_details(details)
        self.assertEqual([row.item for row in self.panel._rows if row.item], ["Sample sigil staff"])
        self.panel.configure(common_drops=True)
        self.assertEqual([row.item for row in self.panel._rows if row.item], ["Sample sigil staff", "Sample_Coin"])
        self.assertLess(self.row_text().index("Unique drops"), self.row_text().index("Common drops"))
        self.panel.configure(common_drops=False)
        self.panel.set_details(self.details(common_drops=("Sample coin",), unique_drops=("SAMPLE_COIN",)))
        self.assertNotIn("Unique drops", self.row_text())
        self.assertFalse(any(row.item for row in self.panel._rows))

    def test_refresh_removes_tooltip_for_item_now_only_in_hidden_common_drops(self) -> None:
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details(common_drops=()))
        self.panel._hover_item("Sample sigil staff", QPoint(300, 300))
        self.assertTrue(self.panel._tooltip.isVisible())
        self.panel.set_details(self.details(common_drops=("Sample_sigil_staff",)))
        self.assertFalse(self.panel._tooltip.isVisible())
        self.assertEqual(self.panel._hovered_item, "")
        self.assertNotIn("Unique drops", self.row_text())

    def test_npc_and_item_clicks_open_canonical_wiki_pages_and_keep_hover_tooltips(self):
        self.panel.show_npc("Sample Keeper")
        details = self.details(source="https://monstersandmemories.miraheze.org/wiki/Canonical_Keeper")
        self.panel.set_details(details)
        self.panel.set_item_details(SimpleNamespace(name="Sample sigil staff", lines=("MAGIC",),
            source="https://monstersandmemories.miraheze.org/wiki/Canonical_Staff"))
        self.panel._hover_item("Sample sigil staff", QPoint(300, 300))
        self.assertTrue(self.panel._tooltip.isVisible())
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.click(self.panel._name_rect().center().toPoint())
            self.click(self.item_point())
        self.assertEqual([call.args[0].toString() for call in opened.call_args_list], [
            "https://monstersandmemories.miraheze.org/wiki/Canonical_Keeper",
            "https://monstersandmemories.miraheze.org/wiki/Canonical_Staff"])
        self.assertIn("Sample sigil staff", self.row_text())

    def test_untrusted_or_absent_sources_fall_back_to_the_exact_wiki_title(self):
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details(source="https://example.invalid/wiki/Wrong"))
        self.panel.set_item_details(SimpleNamespace(name="Sample sigil staff", lines=("MAGIC",),
            source="javascript:alert(1)"))
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.click(self.panel._name_rect().center().toPoint())
            self.click(self.item_point())
        self.assertEqual([call.args[0].toString() for call in opened.call_args_list], [
            "https://monstersandmemories.miraheze.org/wiki/Sample_Keeper",
            "https://monstersandmemories.miraheze.org/wiki/Sample_sigil_staff"])
        self.panel.set_details(self.details(source="https://[invalid"))
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.click(self.panel._name_rect().center().toPoint())
        self.assertEqual(opened.call_args.args[0].toString(),
                         "https://monstersandmemories.miraheze.org/wiki/Sample_Keeper")

    def test_drag_resize_right_click_click_through_and_changed_target_never_open_links(self):
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        title = self.panel.mapToGlobal(self.panel._name_rect().center().toPoint())
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            # A locked label still needs click slop, including out-and-back movement.
            self.mouse(QEvent.Type.MouseButtonPress, title, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, title + QPoint(30, 0), buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, title, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonRelease, title, button=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonPress, title, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonRelease, self.panel.mapToGlobal(self.item_point()), button=Qt.MouseButton.LeftButton)
            self.click(self.item_point(), button=Qt.MouseButton.RightButton)
            self.owner.locked = False
            self.panel.sync()
            title = self.panel.mapToGlobal(self.panel._name_rect().center().toPoint())
            self.mouse(QEvent.Type.MouseButtonPress, title, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, title + QPoint(45, 25), buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonRelease, title + QPoint(45, 25), button=Qt.MouseButton.LeftButton)
            self.assertFalse(self.panel.docked)
            corner = self.panel.mapToGlobal(self.panel._grip_rect().center())
            self.mouse(QEvent.Type.MouseButtonPress, corner, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, corner + QPoint(45, 25), buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonRelease, corner + QPoint(45, 25), button=Qt.MouseButton.LeftButton)
            self.owner.click_through = True
            self.panel.sync()
            self.click(self.panel._name_rect().center().toPoint())
            opened.assert_not_called()

    def test_links_use_hand_cursor_but_edges_and_grip_have_resize_priority(self):
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        title, item = self.panel._name_rect().center().toPoint(), self.item_point()
        self.assertEqual(self.panel._cursor_at(title), Qt.CursorShape.PointingHandCursor)
        self.assertEqual(self.panel._cursor_at(item), Qt.CursorShape.PointingHandCursor)
        self.owner.locked = False
        self.assertEqual(self.panel._cursor_at(self.panel._grip_rect().center()), Qt.CursorShape.SizeFDiagCursor)
        self.assertEqual(self.panel._cursor_at(QPoint(self.panel.width() // 2, 1)), Qt.CursorShape.SizeVerCursor)
        self.assertEqual(self.panel._cursor_at(QPoint(1, 1)), Qt.CursorShape.SizeFDiagCursor)
        self.assertEqual(self.panel._cursor_at(QPoint(self.panel.width() - 2, 1)), Qt.CursorShape.SizeBDiagCursor)
        self.owner.click_through = True
        self.assertEqual(self.panel._cursor_at(title), Qt.CursorShape.ArrowCursor)

    def test_locked_link_click_allows_small_pointer_motion_without_becoming_a_drag(self):
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        title = self.panel.mapToGlobal(self.panel._name_rect().center().toPoint())
        with patch.object(QDesktopServices, "openUrl", return_value=True) as opened:
            self.mouse(QEvent.Type.MouseButtonPress, title, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseMove, title + QPoint(1, 0), buttons=Qt.MouseButton.LeftButton)
            self.mouse(QEvent.Type.MouseButtonRelease, title + QPoint(1, 0), button=Qt.MouseButton.LeftButton)
        opened.assert_called_once()
        self.assertTrue(self.panel.docked)

    def test_settings_gear_does_not_shift_title_painting_or_link_hit_test(self):
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        title, name, body = self.panel._title_rect(), self.panel._name_rect(), self.panel._body
        self.panel._settings_gear = object()
        self.panel._settings_gear_changed()
        self.assertEqual(self.panel._title_rect(), title)
        self.assertEqual(self.panel._name_rect(), name)
        self.assertEqual(self.panel._body, body)
        self.assertEqual(self.panel._title_rect().left(), 10)
        self.assertEqual(self.panel._link_at(self.panel._name_rect().center().toPoint()), ("npc", "Sample Keeper"))

    def test_small_font_gear_floats_over_existing_content_without_adding_height(self):
        from mnmparse.app.overlay_settings import OverlaySettingsGear

        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        self.panel.set_font_px(7.8)
        before = self.panel.height(), self.panel._body, self.panel._title_rect()
        gear = OverlaySettingsGear(self.panel, lambda: None, label="Considered NPC")
        self.panel._settings_gear = gear
        self.panel._settings_gear_changed()
        first_row = self.panel._row_rects[0][0].translated(0, self.panel._body.top())
        self.assertEqual((self.panel.height(), self.panel._body, self.panel._title_rect()), before)
        self.assertTrue(first_row.intersects(gear.geometry()))
        self.assertEqual(self.panel._link_at(self.panel._name_rect().center().toPoint()), ("npc", "Sample Keeper"))

    def test_preferences_filter_sections_without_discarding_cached_details(self) -> None:
        details = self.details()
        self.panel.show_npc(details.name)
        self.panel.set_details(details, "Cached wiki data")
        self.panel.configure(level=False, unique_drops=False, common_drops=True, quests=True, reputation=True)
        text = self.row_text()
        self.assertNotIn("Level:", text)
        self.assertNotIn("Unique drops", text)
        for expected in ("Common drops", "Sample coin", "Sample quest", "Sample watchers +10", "Sample rivals −10"):
            self.assertIn(expected, text)
        self.panel.configure(enabled=False)
        self.assertFalse(self.panel.isVisible())
        self.panel.configure(enabled=True, level=True)
        self.assertIs(self.panel._details, details)
        self.assertIn("Level: 23–25", self.row_text())
        self.assertEqual(self.panel._status, "Cached wiki data")

    def test_status_failure_and_reselect_preserve_cached_content_but_new_npc_clears_it(self) -> None:
        self.panel.show_npc("Sample Keeper")
        details = self.details()
        self.panel.set_details(details)
        self.panel.set_status("Wiki unavailable; showing cached information")
        self.panel.show_npc("Sample Keeper")
        self.assertIs(self.panel._details, details)
        self.assertIn("Sample sigil staff", self.row_text())
        self.panel.show_npc("Another sample NPC")
        self.assertIsNone(self.panel._details)
        self.assertNotIn("Sample sigil staff", self.row_text())

    def test_long_lists_wrap_at_narrow_width_and_scroll_with_a_bounded_panel(self) -> None:
        self.owner.resize(280, 100)
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details(unique_drops=tuple(f"Sample very long item name with several extra words number {i}" for i in range(60))))
        self.app.processEvents()
        self.assertEqual(self.panel.width(), 280)
        self.assertLess(self.panel.height(), 360)
        self.assertGreater(self.panel._scroll.maximum(), 0)
        self.assertTrue(self.panel._scroll.isVisible())
        self.assertTrue(all(rect.right() < self.panel._scroll.x() for rect, _ in self.panel._row_rects))
        self.assertTrue(any(rect.height() > QFontMetricsF(self.panel._font).height() * 1.8
                            for rect, row in self.panel._row_rects if row.item))
        self.panel._scroll.setValue(self.panel._scroll.maximum())
        last_rect, last_row = self.panel._row_rects[-1]
        screen_point = last_rect.center().toPoint() + QPoint(0, int(self.panel._body.top()) - self.panel._scroll.value())
        self.assertEqual(self.panel._item_at(screen_point), last_row.item)

    def test_hover_reads_cache_then_updates_same_passive_tooltip_without_reopening(self) -> None:
        self.panel.show_npc("Sample Keeper")
        self.panel.set_details(self.details())
        seen = []
        self.panel.item_hovered.connect(seen.append)
        item = SimpleNamespace(name="Sample sigil staff", lines=(
            "MAGIC", "Slot: PRIMARY SECONDARY", "Weapon DMG: 15    ATK Delay: 33    Skill: BLG",
            "INT: +6  CHA: +2", "Effect:",
            "Runeskin II (On Click. Any Slot. Cast Time: 10s. Level: 30)",
            "Weight: 7.5    Size: MEDIUM", "Class: ENC", "Race: ALL"), source="")
        self.panel.set_item_details(item)
        self.panel._hover_item(item.name, QPoint(300, 300))
        tooltip = self.panel._tooltip
        self.assertEqual(seen, [item.name])
        self.assertEqual(tooltip._stats.text(), "\n".join(item.lines))
        self.assertTrue(tooltip.isVisible())
        self.assertTrue(tooltip.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(tooltip.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.assertGreaterEqual(tooltip._stats.height(), tooltip._stats.heightForWidth(tooltip._stats.width()))
        with patch.object(tooltip, "show") as show:
            self.panel.set_item_details(SimpleNamespace(name=item.name, lines=("MAGIC", "INT: +6"), source=""))
            self.assertEqual(tooltip._stats.text(), "MAGIC\nINT: +6")
            show.assert_not_called()
        self.panel.set_item_status(item.name, "Wiki unavailable; showing cached item")
        self.assertEqual(tooltip._stats.text(), "MAGIC\nINT: +6")
        self.assertIn("cached", tooltip._note.text())
        self.panel._hover_item(item.name, QPoint(300, 300))
        self.assertEqual(len(seen), 1)
        self.panel._clear_hover()
        self.panel._hover_item(item.name, QPoint(300, 300))
        self.assertEqual(seen, [item.name, item.name])

    def test_tooltip_displays_plain_wiki_text_and_clears_when_input_or_panel_hidden(self) -> None:
        self.panel.show_npc("Sample Keeper")
        self.panel._hover_item("<b>Untrusted name</b>", QPoint(300, 300))
        self.assertEqual(self.panel._tooltip._name.textFormat(), Qt.TextFormat.PlainText)
        self.assertIn("Loading", self.panel._tooltip._note.text())
        self.owner.click_through = True
        self.panel.sync()
        self.assertFalse(self.panel._tooltip.isVisible())
        self.assertTrue(self.panel.windowFlags() & Qt.WindowType.WindowTransparentForInput)
        self.panel._hover_item("Sample item", QPoint(300, 300))
        self.assertFalse(self.panel._tooltip.isVisible())
        self.owner.click_through = False
        self.panel.sync()
        self.panel._hover_item("Sample item", QPoint(300, 300))
        self.owner.hide()
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.assertFalse(self.panel._tooltip.isVisible())
        self.owner.show()
        self.panel.sync()
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel._name, "Sample Keeper")

    def test_free_geometry_remains_detached_and_dock_uses_owner_anchor(self) -> None:
        self.panel.show_npc("Sample Keeper")
        self.panel._docked = False
        self.panel._free_geometry = QRect(360, 210, 340, self.panel.height())
        self.panel.sync()
        self.assertEqual((self.panel.x(), self.panel.y(), self.panel.width()), (360, 210, 340))
        self.panel.set_font_px(20)
        self.assertEqual((self.panel.x(), self.panel.y(), self.panel.width()), (360, 210, 340))
        self.panel.dock()
        self.assertTrue(self.panel.docked)
        self.assertEqual(self.panel.x(), self.owner.geometry().left())
        self.assertEqual(self.panel.width(), self.owner.width())

    def test_real_overlay_moves_scales_hides_and_restores_the_selected_npc(self) -> None:
        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.config import Config

        owner = OverlayWindow(self.settings, Config())
        try:
            owner.setGeometry(20, 20, 390, 160)
            owner.show()
            panel = owner.npc_panel
            panel.show_npc("Sample Keeper")
            panel.set_details(self.details())
            self.app.processEvents()
            self.assertTrue(panel.isVisible())
            self.assertEqual(panel.width(), owner.width())
            owner.set_font_scale(1.5)
            owner.set_opacity(0.5)
            owner.move(35, 35)
            self.app.processEvents()
            self.assertEqual(panel.width(), owner.width())
            self.assertEqual(panel.x(), owner.frameGeometry().left())
            self.assertAlmostEqual(panel._px, 19.5)
            owner.set_click_through(True)
            self.assertTrue(panel.windowFlags() & Qt.WindowType.WindowTransparentForInput)
            owner.hide()
            self.assertFalse(panel.isVisible())
            owner.show()
            self.assertTrue(panel.isVisible())
            self.assertIn("Sample sigil staff", "\n".join(row.text for row in panel._rows))
        finally:
            owner.close()
            owner.deleteLater()
            self.app.processEvents()
if __name__ == "__main__":
    unittest.main()
