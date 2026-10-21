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

from PySide6.QtCore import QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QFontMetricsF
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

    def test_default_sections_show_level_and_unique_drops_and_unknown_is_explicit(self) -> None:
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.panel.show_npc("Sample Keeper")
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel.options, dict(enabled=True, level=True, common_drops=False,
                                                unique_drops=True, quests=False, reputation=False))
        self.assertIn("Level: Unknown", self.row_text())
        self.assertIn("Unique drops", self.row_text())
        self.assertNotIn("Common drops", self.row_text())
        self.panel.set_details(self.details(unique_drops=()))
        self.assertIn("Level: 23–25", self.row_text())
        self.assertIn("Not available in wiki data", self.row_text())
        self.assertNotIn("None", self.row_text())

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
