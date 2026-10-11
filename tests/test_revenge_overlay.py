"""Revenge views share overlay presentation and respect stack page ownership."""
from __future__ import annotations

from dataclasses import replace
import csv
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSettings, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton

from mnmparse.app import theme
from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.revenge import RevengeController, RevengeList, RevengePopoutWindow
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

    def popout(self, *, show: bool = True) -> RevengePopoutWindow:
        window = RevengePopoutWindow(self.controller, self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.windows.append(window)
        if show:
            window.show()
        self.app.processEvents()
        return window

    def test_popout_restores_open_state_and_geometry_without_activating(self) -> None:
        saved = QRect(80, 60, 480, 420)
        self.settings.setValue("revenge/geometry", saved)
        self.settings.setValue("revenge/visible", True)
        window = self.popout(show=False)
        self.assertTrue(window.isHidden(), "Construction should not show the overlay before startup is ready")
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        self.assertEqual(self.settings.value("revenge/geometry", QRect()), saved)
        with patch.object(window, "activateWindow", side_effect=AssertionError("Activated game overlay")), \
                patch.object(window, "raise_", side_effect=AssertionError("Raised game overlay")):
            window.restore_state()
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        self.assertEqual(window.geometry(), saved)

    def test_popout_shutdown_preserves_open_preference_position_and_size(self) -> None:
        window = self.popout()
        window.setGeometry(QRect(90, 70, 500, 440))
        self.app.processEvents()
        saved = QRect(window.geometry())
        self.assertEqual(self.settings.value("revenge/geometry", QRect()), saved)
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        window.shutdown()
        self.assertTrue(window.isHidden())
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool),
                        "Application shutdown must not record a user dismissal")
        reopened = self.popout(show=False)
        reopened.restore_state()
        self.app.processEvents()
        self.assertTrue(reopened.isVisible())
        self.assertEqual(reopened.geometry(), saved)

    def test_popout_user_dismissal_is_remembered(self) -> None:
        for dismiss in ("hide", "close"):
            with self.subTest(dismiss=dismiss):
                window = self.popout()
                getattr(window, dismiss)()
                self.assertFalse(self.settings.value("revenge/visible", True, type=bool))
                reopened = self.popout(show=False)
                reopened.restore_state()
                self.assertTrue(reopened.isHidden())

    def test_popout_disabled_feature_preserves_open_preference(self) -> None:
        self.settings.setValue("revenge/visible", True)
        disabled = replace(self.cfg, revenge_enabled=False)
        self.controller.set_config(disabled)
        window = self.popout(show=False)
        window.restore_state()
        self.assertTrue(window.isHidden())
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        self.controller.set_config(self.cfg)
        self.app.processEvents()
        self.assertTrue(window.isVisible())
        self.controller.set_config(disabled)
        self.assertTrue(window.isHidden())
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))
        window.shutdown()
        self.assertTrue(self.settings.value("revenge/visible", False, type=bool))

    def test_popout_saved_geometry_on_missing_monitor_remains_reachable(self) -> None:
        self.settings.setValue("revenge/geometry", QRect(-10000, -10000, 5000, 5000))
        self.settings.setValue("revenge/visible", True)
        window = self.popout(show=False)
        window.restore_state()
        self.app.processEvents()
        screen = QApplication.primaryScreen()
        self.assertIsNotNone(screen)
        self.assertTrue(screen.availableGeometry().contains(window.geometry()),
                        f"Restored {window.geometry()} outside {screen.availableGeometry()}")

    def test_popout_large_text_and_controls_fit_a_narrow_replacement_monitor(self) -> None:
        area = QRect(0, 0, 640, 480)
        screen = Mock()
        screen.availableGeometry.return_value = area
        self.settings.setValue("revenge/geometry", QRect(-10000, -10000, 1500, 1000))
        self.settings.setValue("revenge/visible", True)
        self.settings.setValue("overlay/font_scale", 2.0)
        with patch("mnmparse.app.revenge.QApplication.screens", return_value=[screen]), \
                patch("mnmparse.app.revenge.QApplication.primaryScreen", return_value=screen):
            window = self.popout(show=False)
            window.restore_state()
            window.set_appearance(0.85, 2.0)
            self.app.processEvents()
            self.assertTrue(area.contains(window.geometry()),
                            f"Restored {window.geometry()} outside narrow monitor {area}")
            self.assertEqual(window.font_scale, 2.0)
            for control in (window.close_button, window.list.export_button, window.list.add_button,
                            window.list.guild_export_button, window.list.guild_add_button):
                with self.subTest(control=control.objectName() or control.text()):
                    self.assertTrue(control.parentWidget().rect().contains(control.geometry()))
                    bounds = QRect(control.mapTo(window, QPoint()), control.size())
                    self.assertTrue(window.rect().contains(bounds))
                    if control is not window.close_button:
                        self.assertFalse(window.close_button.geometry().intersects(bounds))

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
        heading = window.list.export_button
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
            local = window.mapFromGlobal(global_point)
            event = QMouseEvent(kind, QPointF(local), QPointF(global_point), button, buttons,
                                Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(window, event)

        def drag(offset: QPoint, *, finish: bool = True) -> None:
            origin = window.mapToGlobal(QPoint(10, 10))
            mouse(QEvent.Type.MouseButtonPress, origin, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
            mouse(QEvent.Type.MouseMove, origin + offset, Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
            if finish:
                mouse(QEvent.Type.MouseButtonRelease, origin + offset,
                      Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton)

        start = window.pos()
        window.set_locked(True)
        drag(QPoint(30, 20))
        self.assertEqual(window.pos(), start)
        self.assertEqual(window.cursor().shape(), Qt.CursorShape.ArrowCursor)
        window.set_locked(False)
        drag(QPoint(30, 20), finish=False)
        self.assertEqual(window.pos(), start + QPoint(30, 20))
        self.assertEqual(window.cursor().shape(), Qt.CursorShape.SizeAllCursor)
        window.set_locked(True)
        current = window.pos()
        mouse(QEvent.Type.MouseMove, window.mapToGlobal(QPoint(60, 60)),
              Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
        self.assertEqual(window.pos(), current)
        self.assertIsNone(window._drag)

    def test_popout_close_is_always_visible_and_remembers_dismissal(self) -> None:
        window = self.popout()
        self.controller.save_attacker("Bandit")
        window.resize(460, 640)
        self.app.processEvents()
        self.assertTrue(window.close_button.isVisible())
        self.assertEqual(window.close_button.text(), "×")
        self.assertEqual(window.close_button.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertFalse(any(label.text() == "Revenge" for label in window.findChildren(QLabel)))
        viewport = window.list.rows_scroll.geometry()
        self.assertGreater(viewport.height(), 320)

        point = QPoint(10, 10)
        event = QMouseEvent(QEvent.Type.MouseMove, QPointF(point), QPointF(window.mapToGlobal(point)),
                            Qt.MouseButton.NoButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(window, event)
        self.app.sendEvent(window, QEvent(QEvent.Type.Leave))
        self.assertTrue(window.close_button.isVisible())
        self.assertEqual(window.list.rows_scroll.geometry(), viewport)

        window.list.add_button.click()
        dialog = window.list._manual_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(window.close_button.isVisible())
        dialog.reject()
        self.assertEqual(window.list.rows_scroll.geometry(), viewport)
        with patch.object(window, "activateWindow", side_effect=AssertionError("Activated game overlay")):
            window.close_button.click()
        self.assertTrue(window.isHidden())
        self.assertFalse(self.settings.value("revenge/visible", True, type=bool))

    def test_player_and_guild_columns_add_export_and_remove_independently(self) -> None:
        window = self.popout()
        self.controller.save_attacker("Bandit")
        window.list.guild_add_button.click()
        dialog = window.list._manual_dialog
        self.assertEqual(dialog.kind, "Guild")
        dialog.setTextValue("Bandit")
        dialog.accept()
        self.app.processEvents()
        self.assertEqual(set(window.list._rows), {"Bandit", ("Guild", "Bandit")})
        player_row = window.list._rows["Bandit"]
        guild_row = window.list._rows[("Guild", "Bandit")]
        self.assertIs(player_row.parentWidget(), window.list.rows_scroll.widget())
        self.assertIs(guild_row.parentWidget(), window.list.guild_rows_scroll.widget())
        self.assertIn("Guild reminder", window.list._labels[("Guild", "Bandit")].toolTip())

        clipboard = self.app.clipboard()
        previous = clipboard.text()
        try:
            window.list.export_button.click()
            self.assertEqual(clipboard.text().rstrip("\r\n"), "Bandit")
            window.list.guild_export_button.click()
            self.assertEqual(clipboard.text().rstrip("\r\n"), "Bandit")
        finally:
            clipboard.setText(previous)

        with patch("mnmparse.app.revenge.QMessageBox.exec", return_value=QMessageBox.StandardButton.Yes):
            guild_row.findChild(QPushButton).click()
        self.assertEqual(set(window.list._rows), {"Bandit"})
        self.controller.set_config(replace(self.cfg, revenge_enabled=False))
        self.assertTrue(window.isHidden())
        self.assertFalse(window.list.add_button.isEnabled())
        self.assertFalse(window.list.guild_add_button.isEnabled())

    def test_popout_remove_confirmation_keeps_cancelled_entry_and_removes_only_selected_list(self) -> None:
        window = self.popout()
        self.controller.save_attacker("Bandit")
        self.controller.save_attacker("Bandit", kind="Guild")
        for key, kind in (("Bandit", "Player"), (("Guild", "Bandit"), "Guild")):
            with self.subTest(kind=kind):
                row = window.list._rows[key]
                remove = row.findChild(QPushButton)
                self.assertIsNotNone(remove)
                questions = []

                def cancel(dialog: QMessageBox) -> QMessageBox.StandardButton:
                    questions.append(dialog)
                    self.assertEqual(dialog.standardButton(dialog.defaultButton()), QMessageBox.StandardButton.Cancel)
                    self.assertEqual(dialog.textFormat(), Qt.TextFormat.PlainText)
                    self.assertIn("Bandit", dialog.text())
                    self.assertEqual(dialog.button(QMessageBox.StandardButton.Yes).text(), "Remove")
                    return QMessageBox.StandardButton.Cancel

                with patch("mnmparse.app.revenge.QMessageBox.exec", new=cancel), \
                        patch.object(self.controller, "remove_saved", wraps=self.controller.remove_saved) as remove_saved:
                    remove.click()
                    self.assertEqual(len(questions), 1)
                    remove_saved.assert_not_called()
                    self.assertIn(key, window.list._rows)
                with patch("mnmparse.app.revenge.QMessageBox.exec",
                           return_value=QMessageBox.StandardButton.Yes), \
                        patch.object(self.controller, "remove_saved", wraps=self.controller.remove_saved) as remove_saved:
                    remove.click()
                    remove_saved.assert_called_once_with("Bandit", kind=kind)
                self.assertNotIn(key, window.list._rows)
                if kind == "Player":
                    self.assertIn(("Guild", "Bandit"), window.list._rows)

    def test_regular_revenge_list_retains_direct_remove(self) -> None:
        listing = RevengeList(self.controller)
        self.windows.append(listing)
        self.controller.save_attacker("Bandit")
        remove = listing._rows["Bandit"].findChild(QPushButton)
        with patch("mnmparse.app.revenge.QMessageBox.exec",
                   side_effect=AssertionError("Regular lists should not open the pop-out confirmation")):
            remove.click()
        self.assertNotIn("Bandit", listing._rows)

    def test_popout_clipboard_actions_review_only_the_chosen_csv_list(self) -> None:
        window = self.popout()
        clipboard = self.app.clipboard()
        previous = clipboard.text()
        try:
            for add_button, export_button, kind, name in (
                    (window.list.add_button, window.list.export_button, "Player", "Raider"),
                    (window.list.guild_add_button, window.list.guild_export_button, "Guild", "Friends")):
                with self.subTest(kind=kind):
                    clipboard.setText(f'{name},"Name, with comma"')
                    self.assertEqual(add_button.text(), "+")
                    self.assertEqual(export_button.text(), "Players" if kind == "Player" else "Guilds")
                    self.assertEqual(export_button.toolTip(), "Copy CSV to Clipboard")
                    with patch("mnmparse.app.revenge.QApplication.clipboard",
                               side_effect=AssertionError("Adding a name should not read the clipboard automatically")):
                        add_button.click()
                    dialog = window.list._manual_dialog
                    self.assertIsNotNone(dialog)
                    self.assertEqual(dialog.kind, kind)
                    self.assertEqual(dialog.name_input.text(), "")
                    self.assertTrue(dialog.name_input.isVisible())
                    self.assertFalse(dialog.preview.isVisible())
                    self.assertFalse(dialog.add_button.isEnabled())
                    self.assertEqual(dialog.from_clipboard.text(), "CSV from clipboard")
                    dialog.from_clipboard.click()
                    self.assertEqual(dialog.preview.toPlainText(), name + "\nName, with comma")
                    key = name if kind == "Player" else (kind, name)
                    quoted_key = "Name, with comma" if kind == "Player" else (kind, "Name, with comma")
                    self.assertNotIn(key, window.list._rows, "Import preview must wait for Add before saving")
                    self.assertNotIn(quoted_key, window.list._rows)
                    dialog.accept()
                    self.app.processEvents()
                    self.assertIn(key, window.list._rows)
                    self.assertIn(quoted_key, window.list._rows)
                    export_button.click()
                    self.assertEqual(set(next(csv.reader([clipboard.text().rstrip("\r\n")]))),
                                     {name, "Name, with comma"})
            self.assertEqual(set(window.list._rows), {"Raider", "Name, with comma", ("Guild", "Friends"),
                                                     ("Guild", "Name, with comma")})
        finally:
            clipboard.setText(previous)

    def test_column_actions_remain_reachable_and_rows_are_tight_in_all_views(self) -> None:
        regular = RevengeList(self.controller)
        self.windows.append(regular)
        regular.resize(520, 420)
        regular.show()
        overlay = self.overlay()
        overlay.set_tab("revenge")
        popout = self.popout()
        for name in ("First", "Second", "Third"):
            self.controller.save_attacker(name)
        self.app.processEvents()
        for listing in (regular, overlay._revenge, popout.list):
            with self.subTest(view=listing.window().objectName() or "main"):
                for heading, add, column in ((listing.export_button, listing.add_button, listing.player_column),
                                             (listing.guild_export_button, listing.guild_add_button,
                                              listing.guild_column)):
                    self.assertIs(heading.parentWidget(), column)
                    self.assertIs(add.parentWidget(), column)
                    self.assertTrue(heading.isVisible())
                    self.assertTrue(add.isVisible())
                    self.assertEqual(add.text(), "+")
                    self.assertEqual(heading.geometry().top(), add.geometry().top())
                    self.assertGreaterEqual(add.geometry().left(), heading.geometry().right())
                first, second = (listing._row_layout.itemAt(i).widget() for i in range(2))
                self.assertLessEqual(second.y() - first.geometry().bottom() - 1, 2)
                self.assertLessEqual(first.height(), first.fontMetrics().height() + 4)
        for control in (popout.list.export_button, popout.list.add_button,
                        popout.list.guild_export_button, popout.list.guild_add_button):
            bounds = QRect(control.mapTo(popout, QPoint()), control.size())
            self.assertFalse(popout.close_button.geometry().intersects(bounds),
                             "The close control must not cover the list actions")
        popout.resize(240, 380)
        self.app.processEvents()
        self.assertEqual(popout.width(), 240)
        self.assertTrue(popout.rect().contains(popout.close_button.geometry()))
        self.assertLessEqual(popout.list.export_button.mapTo(popout, QPoint()).y(), 12,
                             "List headings should share the close row instead of wasting a top strip")
        self.assertLessEqual(popout.list.rows_scroll.mapTo(popout, QPoint()).y(), 36,
                             "The compact header should leave more height for saved names")
        for control in (popout.list.export_button, popout.list.add_button,
                        popout.list.guild_export_button, popout.list.guild_add_button):
            self.assertTrue(control.parentWidget().rect().contains(control.geometry()),
                            f"Column action {control.text()} must fit the narrow list")
            bounds = QRect(control.mapTo(popout, QPoint()), control.size())
            self.assertFalse(popout.close_button.geometry().intersects(bounds))


if __name__ == "__main__":
    unittest.main()
