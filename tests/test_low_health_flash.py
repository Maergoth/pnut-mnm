"""Persistent, passive low-HP vignette and its resolution-scaled rendering."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import QRect, Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.low_health import EDGE_FRACTION, LOW_HEALTH_COLOR, OVERBURDENED_COLOR, LowHealthFlash, PULSE_SECONDS


class LowHealthFlashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.flash = LowHealthFlash()
        self.flash.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.now = 100.0
        self.clock = patch("mnmparse.app.low_health.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()

    def tearDown(self):
        self.flash.close()
        self.flash.deleteLater()
        self.app.processEvents()
        self.clock.stop()

    def render(self, width=1280, height=720, *, reduced_motion=False):
        self.flash.flash(QRect(0, 0, width, height), reduced_motion=reduced_motion)
        return self.flash.grab().toImage()

    def alpha(self, image, x, y):
        scale_x = image.width() / self.flash.width()
        scale_y = image.height() / self.flash.height()
        return image.pixelColor(round(x * scale_x), round(y * scale_y)).alpha()

    def test_warning_survives_many_pulses_and_ends_only_when_stopped(self):
        self.render()
        self.assertTrue(self.flash.active)
        self.assertTrue(self.flash.isVisible())
        for age in (1.4, 2.0, 60.0, 3600.0):
            with self.subTest(age=age):
                self.now = 100.0 + age
                self.flash._tick()
                self.assertTrue(self.flash.active)
                self.assertTrue(self.flash.isVisible())
                self.assertTrue(self.flash._timer.isActive())
                self.assertGreater(self.alpha(self.flash.grab().toImage(), 2, 360), 0)
        self.flash.stop()
        self.assertFalse(self.flash.active)
        self.assertTrue(self.flash.isHidden())
        self.assertFalse(self.flash._timer.isActive())
        self.now += 100
        self.flash._tick()  # An already queued frame cannot revive it.
        self.assertTrue(self.flash.isHidden())
        self.assertFalse(self.flash._timer.isActive())

    def test_pulse_is_gentle_continuous_and_does_not_fade_away(self):
        self.render()
        peak = self.flash._strength()
        self.now += PULSE_SECONDS / 2
        trough = self.flash._strength()
        self.now += PULSE_SECONDS / 2
        self.assertAlmostEqual(self.flash._strength(), peak)
        self.assertGreaterEqual(trough, 0.25)
        self.assertLessEqual(peak, 0.50)
        self.assertGreater(peak, trough)
        self.assertLess(peak - trough, 0.20)

    def test_repeated_health_updates_preserve_phase_and_update_geometry(self):
        self.render()
        self.now += 0.7
        previous_strength = self.flash._strength()
        self.flash.flash(QRect(100, 50, 1920, 1080))
        self.assertEqual(self.flash.geometry(), QRect(100, 50, 1920, 1080))
        self.assertEqual(self.flash._started, 100.0)
        self.assertAlmostEqual(self.flash._strength(), previous_strength)
        self.flash.stop()
        self.now = 200.0
        self.flash.flash(QRect(0, 0, 1280, 720))
        self.assertEqual(self.flash._started, 200.0)
        self.assertAlmostEqual(self.flash._strength(), 0.45)

    def test_reduced_motion_is_static_persistent_and_does_not_run_animation_timer(self):
        first = self.render(reduced_motion=True)
        self.assertFalse(self.flash._timer.isActive())
        self.now += 1000
        self.flash._tick()
        later = self.flash.grab().toImage()
        self.assertEqual(first, later)
        self.assertTrue(self.flash.active)
        self.assertTrue(self.flash.isVisible())
        self.assertFalse(self.flash._timer.isActive())
        self.flash.flash(QRect(0, 0, 1280, 720), reduced_motion=False)
        self.assertTrue(self.flash._timer.isActive())
        self.assertEqual(self.flash._started, 100.0)
        self.flash.flash(QRect(0, 0, 1280, 720), reduced_motion=True)
        self.assertFalse(self.flash._timer.isActive())

    def test_vignette_scales_with_shorter_dimension_and_feathers_to_transparency(self):
        samples = []
        for width, height in ((1280, 720), (2560, 1440), (3840, 2160), (3440, 1440), (640, 360)):
            with self.subTest(size=(width, height)):
                image = self.render(width, height, reduced_motion=True)
                short = min(width, height)
                depth = round(short * EDGE_FRACTION)
                center_x, center_y = width // 2, height // 2
                self.assertGreater(self.alpha(image, center_x, round(short * 0.08)), 0)
                self.assertEqual(self.alpha(image, center_x, depth + 2), 0)
                self.assertEqual(self.alpha(image, center_x, center_y), 0)
                self.assertEqual(self.alpha(image, depth + 2, center_y), 0)
                self.assertGreater(self.alpha(image, round(depth * 0.1), center_y),
                                   self.alpha(image, round(depth * 0.5), center_y))
                self.assertGreater(self.alpha(image, round(depth * 0.5), center_y),
                                   self.alpha(image, round(depth * 0.9), center_y))
                self.assertEqual(self.alpha(image, center_x, height - depth - 2), 0)
                self.assertGreater(self.alpha(image, center_x, height - round(short * 0.08)), 0)
                self.assertGreater(self.alpha(image, width - round(short * 0.08), center_y), 0)
                samples.append(self.alpha(image, round(short * 0.06), center_y))
        self.assertLessEqual(max(samples) - min(samples), 3)

    def test_window_remains_passive_and_no_focus_or_input_flags_are_lost(self):
        self.render()
        for flag in (Qt.WindowType.WindowDoesNotAcceptFocus, Qt.WindowType.WindowTransparentForInput,
                     Qt.WindowType.WindowStaysOnTopHint, Qt.WindowType.FramelessWindowHint):
            self.assertTrue(self.flash.windowFlags() & flag)
        for attribute in (Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.assertTrue(self.flash.testAttribute(attribute))
        self.assertEqual(self.flash.focusPolicy(), Qt.FocusPolicy.NoFocus)

    def test_empty_geometry_cannot_start_or_replace_an_active_warning(self):
        self.flash.flash(QRect())
        self.assertFalse(self.flash.active)
        self.assertFalse(self.flash._timer.isActive())
        self.render()
        previous = self.flash.geometry()
        self.flash.flash(QRect())
        self.assertEqual(self.flash.geometry(), previous)
        self.assertTrue(self.flash.active)

    def test_gray_weight_warning_keeps_the_red_warnings_coverage_and_pulse_phase(self):
        self.render()
        self.now += 0.6
        phase = self.flash._started
        self.flash.set_warning_color(OVERBURDENED_COLOR)
        gray = self.flash.grab().toImage()
        pixel = gray.pixelColor(4, gray.height() // 2)
        self.assertEqual(pixel.red(), pixel.green())
        self.assertEqual(pixel.green(), pixel.blue())
        self.assertGreater(pixel.alpha(), 0)
        self.assertEqual(self.flash._started, phase)
        self.assertEqual(gray.pixelColor(gray.width() // 2, gray.height() // 2).alpha(), 0)
        self.flash.set_warning_color(LOW_HEALTH_COLOR)
        restored = self.flash.grab().toImage().pixelColor(4, gray.height() // 2)
        self.assertGreater(restored.red(), restored.green())
        self.assertEqual(restored.alpha(), pixel.alpha())
        self.assertEqual(self.flash._started, phase)


if __name__ == "__main__":
    unittest.main()
