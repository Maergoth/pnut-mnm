"""Revenge views share overlay presentation and respect stack page ownership."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication

from mnmparse.app import theme
from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.revenge import RevengeController, RevengePopoutWindow
from mnmparse.config import Config


class RevengeOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.cfg = Config(player_name="Hero", revenge_enabled=True)
        self.controller = RevengeController(self.cfg, Path(self.tmp.name) / "revenge.json")
        self.windows = []

    def tearDown(self) -> None:
        for window in self.windows:
            window.close()
            window.deleteLater()
        for name in ("_timer", "_save_timer", "_activity_refresh", "_display_timer"):
            getattr(self.controller, name).stop()
        self.controller.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def overlay(self) -> OverlayWindow:
        window = OverlayWindow(self.settings, self.cfg)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        window.set_revenge_controller(self.controller)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def popout(self) -> RevengePopoutWindow:
        window = RevengePopoutWindow(self.controller, self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        return window

    def test_refresh_does_not_reveal_revenge_controls_on_other_tabs(self) -> None:
        window = self.overlay()
        for tab in ("overview", "damage", "healing", "taken", "feed", "session"):
            with self.subTest(tab=tab):
                window.set_tab(tab)
                self.controller.save_attacker("Bandit")
                self.controller._activity_refresh.timeout.emit()
                self.controller._display_timer.timeout.emit()
                self.controller.set_config(self.cfg)
                self.app.processEvents()
                self.assertIsNot(window._stack.currentWidget(), window._revenge_page)
                self.assertTrue(window._revenge_page.isHidden())
                self.assertFalse(window._revenge.isVisible())
                self.assertFalse(window._revenge.add_button.isVisible())
                self.assertFalse(window._revenge.pop_out_button.isVisible())

        window.set_tab("revenge")
        self.controller.saved_changed.emit()
        self.app.processEvents()
        self.assertIs(window._stack.currentWidget(), window._revenge_page)
        self.assertTrue(window._revenge.isVisible())
        self.assertTrue(window._revenge.add_button.isVisible())

    def test_disabled_feature_and_rebinding_keep_stack_visibility_consistent(self) -> None:
        window = self.overlay()
        window.set_tab("revenge")
        disabled = replace(self.cfg, revenge_enabled=False)
        self.controller.set_config(disabled)
        window.set_config(disabled)
        self.assertEqual(window.tab, "damage")
        self.assertTrue(window._revenge_page.isHidden())
        self.controller.set_config(self.cfg)
        window.set_config(self.cfg)
        self.controller.saved_changed.emit()
        self.assertFalse(window._revenge.isVisible())
        old_page = window._revenge_page
        window.set_revenge_controller(self.controller)
        self.assertTrue(old_page.isHidden())
        self.assertFalse(window._revenge.isVisible())

    def test_popout_reads_shared_appearance_and_lock_without_activating(self) -> None:
        self.settings.setValue("overlay/locked", False)
        self.settings.setValue("overlay/opacity", 0.35)
        self.settings.setValue("overlay/font_scale", 1.5)
        window = self.popout()
        self.assertFalse(window.locked)
        self.assertAlmostEqual(window.opacity, 0.35)
        self.assertAlmostEqual(window.font_scale, 1.5)
        self.assertTrue(window._grip.isVisible())
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        with patch.object(window, "activateWindow", side_effect=AssertionError("Activated game overlay")), \
                patch.object(window, "raise_", side_effect=AssertionError("Raised game overlay")):
            window.show()
            window.set_appearance(0.45, 1.25)
            window.set_locked(True)
        self.assertTrue(window.locked)
        self.assertTrue(window._grip.isHidden())
        self.assertTrue(window.list.add_button.isEnabled())

    def test_popout_background_alpha_changes_without_fading_text(self) -> None:
        window = self.popout()
        window.set_appearance(0.25, 1.0)
        self.app.processEvents()

        def render() -> QImage:
            image = QImage(window.size(), QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(Qt.GlobalColor.transparent)
            painter = QPainter(image)
            window.render(painter, QPoint())
            painter.end()
            return image

        first = render()
        center = QPoint(window.width() // 2, window.height() - 16)
        self.assertLessEqual(abs(first.pixelColor(center).alpha() - 64), 1)
        self.assertEqual(first.pixelColor(0, 0).alpha(), 0)
        heading = window.header.heading
        top_left = heading.mapTo(window, QPoint())
        alpha = max(first.pixelColor(top_left.x() + x, top_left.y() + y).alpha()
                    for x in range(heading.width()) for y in range(heading.height()))
        self.assertGreaterEqual(alpha, 250, "Heading glyphs should remain opaque")
        window.set_appearance(0.75, 2.0)
        self.app.processEvents()
        second = render()
        center = QPoint(window.width() // 2, window.height() - 16)
        self.assertLessEqual(abs(second.pixelColor(center).alpha() - 191), 1)
        self.assertEqual(heading.font().pixelSize(), round(theme.OVERLAY_FONT_PX * 2))
        self.assertEqual(window.windowOpacity(), 1.0)

    def test_popout_drag_respects_lock_and_cancels_an_active_drag(self) -> None:
        window = self.popout()
        window.move(40, 40)

        def mouse(kind: QEvent.Type, global_point: QPoint, button: Qt.MouseButton,
                  buttons: Qt.MouseButton) -> None:
            local = window.header.mapFromGlobal(global_point)
            event = QMouseEvent(kind, QPointF(local), QPointF(global_point), button, buttons,
                                Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(window.header, event)

        def drag(offset: QPoint, *, finish: bool = True) -> None:
            origin = window.header.mapToGlobal(QPoint(10, 10))
            mouse(QEvent.Type.MouseButtonPress, origin, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            mouse(QEvent.Type.MouseMove, origin + offset, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
            if finish:
                mouse(QEvent.Type.MouseButtonRelease, origin + offset,
                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)

        start = window.pos()
        window.set_locked(True)
        drag(QPoint(30, 20))
        self.assertEqual(window.pos(), start)
        self.assertEqual(window.header.cursor().shape(), Qt.CursorShape.ArrowCursor)
        window.set_locked(False)
        drag(QPoint(30, 20), finish=False)
        self.assertEqual(window.pos(), start + QPoint(30, 20))
        self.assertEqual(window.header.cursor().shape(), Qt.CursorShape.SizeAllCursor)
        window.set_locked(True)
        current = window.pos()
        mouse(QEvent.Type.MouseMove, window.header.mapToGlobal(QPoint(60, 60)),
              Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        self.assertEqual(window.pos(), current)
        self.assertIsNone(window.header._drag)


if __name__ == "__main__":
    unittest.main()
