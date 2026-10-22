"""Settings gear space remains clear without moving layouts during hover."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QPoint, QRect, QRectF, QSettings, Qt
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.map_overlay import MapOverlay
from mnmparse.app.overlay import OverlayWindow
from mnmparse.app.revenge import RevengeController, RevengePopoutWindow
from mnmparse.config import Config
from mnmparse.maps import MapRepository


class OverlayGearLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.cfg = Config(player_name="Sample", revenge_enabled=True)
        self.overlay = OverlayWindow(self.settings, self.cfg)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.windows = [self.overlay]
        self.controllers = []
        self.overlay.show()
        self.app.processEvents()

    def tearDown(self):
        for window in reversed(self.windows):
            if isinstance(window, MapOverlay):
                window.shutdown()
            else:
                window.close()
            window.deleteLater()
        for controller in self.controllers:
            for name in ("_timer", "_save_timer", "_activity_refresh", "_display_timer"):
                getattr(controller, name).stop()
            controller.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def attach(self, host):
        # This fixture isolates host layout from the separate hover/click behavior.
        gear = QWidget(host)
        gear.setGeometry(4, 4, 22, 22)
        gear.hide()
        host._settings_gear = gear
        host._settings_gear_changed()
        self.app.processEvents()
        return gear

    @staticmethod
    def rect_in_host(widget, host):
        return QRect(widget.mapTo(host, QPoint()), widget.size())

    def test_unregistered_hosts_keep_existing_margins_and_compact_bar_sizes(self):
        self.overlay._settings_gear_changed()
        self.assertEqual(self.overlay.layout().contentsMargins().left(), 10)
        bar = self.overlay._attack_bar
        bar.set_font_px(7.8)
        height = bar.height()
        bar._settings_gear_changed()
        self.assertEqual(bar.height(), height)
        self.assertLess(height, 30)
        timer = self.overlay._timer_panel
        before = timer._row_rect(0)
        timer._settings_gear_changed()
        self.assertEqual(timer._row_rect(0), before)

    def test_combat_header_tabs_body_and_toolbar_clear_gear_at_all_font_scales(self):
        gear = self.attach(self.overlay)
        for scale in (0.6, 1.0, 2.0):
            with self.subTest(scale=scale):
                self.overlay.set_font_scale(scale)
                self.overlay.resize(self.overlay.minimumWidth(), 360)
                self.overlay._set_hovering(True)
                self.app.processEvents()
                for widget in (self.overlay._header, self.overlay._tabs, self.overlay._stack,
                               self.overlay._toolbar):
                    self.assertFalse(gear.geometry().intersects(widget.geometry()))
                    self.assertLessEqual(widget.geometry().right(), self.overlay.width())
                before = self.overlay._header.geometry()
                gear.show()
                gear.hide()
                self.app.processEvents()
                self.assertEqual(self.overlay._header.geometry(), before)

    def test_timer_rows_keep_their_right_edge_and_clear_gear_even_while_hidden(self):
        timer = self.overlay._timer_panel
        timer.set_always_show(True)
        before = timer._row_rect(0)
        gear = self.attach(timer)
        for scale in (0.6, 1.0, 2.0):
            with self.subTest(scale=scale):
                timer.set_font_px(13 * scale)
                row = timer._row_rect(0)
                self.assertGreaterEqual(timer.height(), gear.geometry().bottom() + 4)
                self.assertFalse(QRectF(gear.geometry()).intersects(row))
                self.assertEqual(row.right(), before.right())
                left = row.left()
                gear.show()
                gear.hide()
                self.assertEqual(timer._row_rect(0).left(), left)

    def test_attack_rows_reserve_gear_space_and_tiny_bar_fits_button_height(self):
        bar = self.overlay._attack_bar
        before = bar._row_rect(0, 1)
        gear = self.attach(bar)
        for scale in (0.6, 1.0, 2.0):
            with self.subTest(scale=scale):
                bar.set_font_px(13 * scale)
                for rows in (1, 3):
                    bar._resize_for(rows)
                    for index in range(rows):
                        row = bar._row_rect(index, rows)
                        self.assertFalse(QRectF(gear.geometry()).intersects(row))
                        self.assertEqual(row.right(), before.right())
                    self.assertGreaterEqual(bar.height(), gear.geometry().bottom() + 4)

    def test_map_controls_clear_gear_and_raised_header_does_not_cover_it(self):
        window = MapOverlay(self.settings, MapRepository(Path(self.temp.name) / "maps"))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.windows.append(window)
        window.resize(500, 350)
        window.show()
        self.app.processEvents()
        viewport_size = window.view.viewport().size()
        gear = self.attach(window)
        gear.show()
        window._show_header()
        self.app.processEvents()
        self.assertIs(window.childAt(QPoint(10, 10)), gear)
        for widget in (window.wiki_link, window.notable_npcs, window.drag_handle,
                       window.zone, window.variants, window.fullscreen):
            self.assertFalse(gear.geometry().intersects(self.rect_in_host(widget, window)))
        self.assertEqual(window.view.viewport().size(), viewport_size)
        before = self.rect_in_host(window.wiki_link, window)
        gear.hide()
        self.app.processEvents()
        self.assertEqual(self.rect_in_host(window.wiki_link, window), before)

    def test_revenge_headings_and_controls_clear_gear_after_resizing_and_scaling(self):
        controller = RevengeController(self.cfg, Path(self.temp.name) / "revenge.json")
        self.controllers.append(controller)
        window = RevengePopoutWindow(controller, self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.windows.append(window)
        window.show()
        self.app.processEvents()
        self.assertEqual(window.layout().contentsMargins().left(), 2)
        gear = self.attach(window)
        for scale in (0.6, 1.0, 2.0):
            with self.subTest(scale=scale):
                window.set_appearance(0.85, scale)
                window.resize(window.minimumWidth(), 400)
                self.app.processEvents()
                for widget in (window.list.export_button, window.list.add_button,
                               window.list.guild_export_button, window.close_button):
                    self.assertFalse(gear.geometry().intersects(self.rect_in_host(widget, window)))
                self.assertEqual(window.layout().contentsMargins().left(), 28)


if __name__ == "__main__":
    unittest.main()
