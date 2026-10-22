"""Hover gears float over existing content without changing overlay geometry."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
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
        # Isolate host layout from the shared gear's separate hover/click tests.
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

    @staticmethod
    def margins(widget):
        value = widget.layout().contentsMargins()
        return value.left(), value.top(), value.right(), value.bottom()

    def test_combat_gear_preserves_header_tabs_body_toolbar_and_minimum_width(self):
        def state(scale):
            self.overlay.set_font_scale(scale)
            self.overlay.resize(self.overlay.minimumWidth(), 360)
            self.overlay._set_hovering(True)
            self.app.processEvents()
            return (self.margins(self.overlay), self.overlay.minimumWidth(), self.overlay.needed_min_width(),
                    tuple(widget.geometry() for widget in (self.overlay._header, self.overlay._tabs,
                                                           self.overlay._stack, self.overlay._toolbar)))

        before = {scale: state(scale) for scale in (0.6, 1.0, 2.0)}
        gear = self.attach(self.overlay)
        for scale in before:
            with self.subTest(scale=scale):
                self.assertEqual(state(scale), before[scale])
                self.assertEqual(self.margins(self.overlay), (10, 4, 10, 8))
                self.assertTrue(gear.geometry().intersects(self.overlay._header.geometry()))
                gear.show()
                gear.hide()
                self.assertEqual(state(scale), before[scale])

    def test_timer_gear_does_not_inset_rows_or_enlarge_compact_height(self):
        timer = self.overlay._timer_panel
        timer.set_always_show(True)

        def state(scale):
            timer.set_font_px(13 * scale)
            return timer.height(), timer._row_rect(0)

        before = {scale: state(scale) for scale in (0.6, 1.0, 2.0)}
        gear = self.attach(timer)
        for scale in before:
            with self.subTest(scale=scale):
                self.assertEqual(state(scale), before[scale])
                self.assertTrue(QRectF(gear.geometry()).intersects(timer._row_rect(0)))
                gear.show()
                gear.hide()
                self.assertEqual(state(scale), before[scale])

    def test_attack_gear_does_not_inset_rows_or_add_minimum_height(self):
        bar = self.overlay._attack_bar

        def state(scale, rows):
            bar.set_font_px(13 * scale)
            bar._resize_for(rows)
            return bar.height(), tuple(bar._row_rect(index, rows) for index in range(rows))

        before = {(scale, rows): state(scale, rows) for scale in (0.6, 1.0, 2.0) for rows in (1, 3)}
        gear = self.attach(bar)
        for (scale, rows), expected in before.items():
            with self.subTest(scale=scale, rows=rows):
                self.assertEqual(state(scale, rows), expected)
                self.assertTrue(QRectF(gear.geometry()).intersects(bar._row_rect(0, rows)))
        self.assertLess(before[0.6, 1][0], 30)

    def test_map_gear_floats_above_header_without_moving_controls_or_viewport(self):
        window = MapOverlay(self.settings, MapRepository(Path(self.temp.name) / "maps"))
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.windows.append(window)
        window.resize(500, 350)
        window.show()
        window._show_header()
        self.app.processEvents()
        controls = (window.wiki_link, window.notable_npcs, window.drag_handle,
                    window.zone, window.variants, window.fullscreen)

        def state():
            return (self.margins(window.header), window.view.viewport().size(), window.header.geometry(),
                    tuple(self.rect_in_host(widget, window) for widget in controls))

        before = state()
        gear = self.attach(window)
        gear.show()
        window._show_header()
        self.app.processEvents()
        self.assertEqual(state(), before)
        self.assertIs(window.childAt(QPoint(10, 10)), gear)
        self.assertTrue(gear.geometry().intersects(self.rect_in_host(window.wiki_link, window)))
        gear.hide()
        self.app.processEvents()
        self.assertEqual(state(), before)

    def test_revenge_gear_preserves_margins_controls_and_scaled_minimum_width(self):
        controller = RevengeController(self.cfg, Path(self.temp.name) / "revenge.json")
        self.controllers.append(controller)
        window = RevengePopoutWindow(controller, self.settings)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.windows.append(window)
        window.show()
        controls = (window.list.export_button, window.list.add_button,
                    window.list.guild_export_button, window.close_button)

        def state(scale):
            window.set_appearance(0.85, scale)
            window.resize(window.minimumWidth(), 400)
            self.app.processEvents()
            return (self.margins(window), window.minimumWidth(), window.size(),
                    tuple(self.rect_in_host(widget, window) for widget in controls))

        before = {scale: state(scale) for scale in (0.6, 1.0, 2.0)}
        self.attach(window)
        for scale, expected in before.items():
            with self.subTest(scale=scale):
                self.assertEqual(state(scale), expected)
                self.assertEqual(self.margins(window), (2, 2, 2, 8))

    def test_npc_gear_preserves_title_body_rows_and_height_at_all_font_scales(self):
        panel = self.overlay.npc_panel
        panel.show_npc("Fictional Sample Keeper")
        panel.set_details(SimpleNamespace(name="Fictional Sample Keeper", level="23-25",
            common_drops=(), unique_drops=("Fictional staff",), quests=(), reputation=()))

        def state(scale):
            panel.set_font_px(13 * scale)
            self.app.processEvents()
            return (panel.size(), panel.minimumSize(), panel._title_rect(), panel._name_rect(),
                    panel._body, tuple(rect for rect, row in panel._row_rects))

        before = {scale: state(scale) for scale in (0.6, 1.0, 2.0)}
        gear = self.attach(panel)
        for scale, expected in before.items():
            with self.subTest(scale=scale):
                self.assertEqual(state(scale), expected)
                self.assertTrue(panel._title_rect().intersects(gear.geometry()))
                gear.show()
                gear.hide()
                self.assertEqual(state(scale), expected)


if __name__ == "__main__":
    unittest.main()
