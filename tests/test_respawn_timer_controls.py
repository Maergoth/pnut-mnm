"""Expired NPC countdown controls remain accessible without changing panel dragging."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSettings, Qt
    from PySide6.QtGui import QFont, QFontMetricsF, QMouseEvent, QPainter, QPalette, QWheelEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.triggers import TriggerStore

if HAVE_QT:
    from mnmparse.app.timer_panel import MAX_ROWS, TimerPanel, timer_color
    from mnmparse.app.triggers_runtime import TriggerRunner
    from mnmparse.app.theme import apply_theme

    class _Owner(QWidget):
        locked = True
        click_through = False
        opacity = 0.85

        def __init__(self):
            super().__init__()
            self.setGeometry(20, 20, 560, 100)
            self.panels_changed = Mock()

        def dock_anchor(self, panel):
            return self.frameGeometry()


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class RespawnTimerControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        stylesheet = self.app.styleSheet()
        font = QFont(self.app.font())
        palette = QPalette(self.app.palette())
        self.addCleanup(self.app.setStyleSheet, stylesheet)
        self.addCleanup(self.app.setFont, font)
        self.addCleanup(self.app.setPalette, palette)
        apply_theme(self.app)
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.tmp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.owner = _Owner()
        self.owner.show()
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.panel = TimerPanel(self.owner, self.settings, flags)
        self.runner = TriggerRunner(TriggerStore(Path(self.tmp.name) / "triggers.json"))
        self.runner.set_casual_mode(False)  # This fixture exercises confirmed full-mode labels.
        self.runner.audio = Mock()
        self.clock_patch = patch("mnmparse.app.timer_panel.time.time", return_value=1000.0)
        self.clock = self.clock_patch.start()
        self.panel.set_runner(self.runner)

    def tearDown(self):
        self.panel.close()
        self.owner.close()
        self.runner._clock.stop()
        self.panel.deleteLater()
        self.owner.deleteLater()
        self.runner.deleteLater()
        self.app.processEvents()
        self.clock_patch.stop()
        self.tmp.cleanup()

    def countdown(self, label="a skeletal warrior respawn", *, seconds=30, ended=True, retained=True):
        start = self.clock.return_value - seconds if ended else self.clock.return_value
        timer = self.runner.start_one_time_timer(label, seconds, now=start, keep_until_dismissed=retained)
        self.runner._tick()
        self.runner._clock.stop()
        self.app.processEvents()
        self.panel.grab()
        return timer

    def rendered_text(self):
        drawn = []

        class Painter(QPainter):
            def drawText(self, rect, flags, text):
                drawn.append((QRectF(rect), text, self.font()))
                return super().drawText(rect, flags, text)

        with patch("mnmparse.app.timer_panel.QPainter", Painter):
            self.panel.grab()
        return drawn

    def make_free(self, width=560):
        self.panel._docked = False
        self.panel._free_geometry = QRect(30, 250, width, self.panel.height())
        self.panel.follow()
        self.app.processEvents()

    def click(self, button):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
        self.app.processEvents()
        self.panel.grab()

    def test_expired_npc_replaces_counter_with_static_no_focus_buttons(self):
        timer = self.countdown()
        restart, dismiss = self.panel._expired_buttons[timer.id]
        self.assertEqual((restart.text(), dismiss.text()), ("", ""))
        self.assertNotEqual(restart.icon().pixmap(restart.iconSize()).toImage(),
                            dismiss.icon().pixmap(dismiss.iconSize()).toImage())
        for button, action in ((restart, "Restart"), (dismiss, "Dismiss")):
            self.assertTrue(button.isVisible())
            self.assertEqual(button.focusPolicy(), Qt.FocusPolicy.NoFocus)
            self.assertFalse(button.icon().isNull())
            self.assertIn(action, button.accessibleName())
            self.assertIn(timer.label, button.accessibleName())
            self.assertIn(action, button.toolTip())
            self.assertIn(timer.label, button.toolTip())
        self.assertEqual([text for _, text, _ in self.rendered_text()], [timer.label])
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel._anim.isActive())
        self.assertFalse(self.runner._clock.isActive())
        self.assertEqual(timer_color(timer, 1000).rgba(), timer_color(timer, 1000.25).rgba())

    def test_restart_and_dismiss_clicks_work_while_locked_or_unlocked(self):
        for locked in (True, False):
            with self.subTest(locked=locked):
                self.runner.clear_timers()
                self.owner.locked = locked
                self.panel.dock()
                timer = self.countdown()
                geometry = QRect(self.panel.geometry())
                restart, _ = self.panel._expired_buttons[timer.id]
                self.click(restart)
                self.assertFalse(timer.ended)
                self.assertTrue(self.panel.docked)
                self.assertEqual(self.panel.geometry(), geometry)
                self.assertIsNone(self.panel._press)
                self.assertIsNone(self.panel._drag_offset)
                self.clock.return_value = timer.start + timer.duration
                self.runner._tick()
                self.runner._clock.stop()
                _, dismiss = self.panel._expired_buttons[timer.id]
                self.click(dismiss)
                self.assertEqual(self.runner.board.timers, [])
                self.assertTrue(self.panel.docked)
                self.assertIsNone(self.panel._press)

    def test_releasing_outside_button_cancels_without_dragging_panel(self):
        self.owner.locked = False
        timer = self.countdown()
        restart, _ = self.panel._expired_buttons[timer.id]
        geometry = QRect(self.panel.geometry())
        point = restart.rect().center()
        outside = QPoint(-30, point.y())
        QTest.mousePress(restart, Qt.MouseButton.LeftButton, pos=point)
        QTest.mouseMove(restart, outside)
        QTest.mouseRelease(restart, Qt.MouseButton.LeftButton, pos=outside)
        self.app.processEvents()
        self.assertTrue(timer.ended)
        self.assertEqual(self.runner.board.timers, [timer])
        self.assertEqual(self.panel.geometry(), geometry)
        self.assertTrue(self.panel.docked)
        self.assertIsNone(self.panel._press)

    def test_double_click_buttons_never_dock_a_free_panel(self):
        self.owner.locked = False
        for action_index in (0, 1):
            with self.subTest(action_index=action_index):
                self.runner.clear_timers()
                timer = self.countdown()
                self.make_free()
                button = self.panel._expired_buttons[timer.id][action_index]
                QTest.mouseDClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
                QTest.mouseRelease(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
                self.app.processEvents()
                self.assertFalse(self.panel.docked)
                self.assertIsNone(self.panel._press)
                self.assertIsNone(self.panel._drag_offset)

    def test_dragging_an_ordinary_label_still_undocks(self):
        self.owner.locked = False
        self.countdown(ended=False)
        point = QPoint(70, self.panel._row_h() // 2 + 5)
        QTest.mousePress(self.panel, Qt.MouseButton.LeftButton, pos=point)
        global_point = self.panel.mapToGlobal(point + QPoint(100, 100))
        move = QMouseEvent(QEvent.Type.MouseMove, QPointF(point + QPoint(100, 100)), QPointF(global_point),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(self.panel, move)
        QTest.mouseRelease(self.panel, Qt.MouseButton.LeftButton, pos=point)
        self.app.processEvents()
        self.assertFalse(self.panel.docked)
        self.assertFalse(self.settings.value("timer_panel/docked", True, type=bool))
        self.assertIsNone(self.panel._press)
        self.assertIsNone(self.panel._drag_offset)

    def test_restart_preserves_duration_and_defaults_and_restores_counter(self):
        defaults = json.dumps({"dreadlands": 754})
        self.settings.setValue("overlay/respawn_zone_durations", defaults)
        timer = self.countdown(seconds=754)
        timer.warned = True
        self.clock.return_value = 2000
        self.click(self.panel._expired_buttons[timer.id][0])
        self.assertEqual((timer.start, timer.duration, timer.label), (2000, 754, "a skeletal warrior respawn"))
        self.assertTrue(timer.keep_until_dismissed)
        self.assertFalse(timer.ended)
        self.assertFalse(timer.warned)
        self.assertNotIn(timer.id, self.panel._expired_buttons)
        self.assertEqual([text for _, text, _ in self.rendered_text()], ["12:34", timer.label])
        self.assertEqual(self.settings.value("overlay/respawn_zone_durations"), defaults)
        self.assertTrue(self.panel._anim.isActive())

    def test_dismiss_removes_only_the_chosen_timer(self):
        first = self.countdown("a skeletal warrior respawn")
        second = self.countdown("a rat respawn")
        self.click(self.panel._expired_buttons[first.id][1])
        self.assertEqual(self.runner.board.timers, [second])
        self.assertNotIn(first.id, self.panel._expired_buttons)
        self.assertIn(second.id, self.panel._expired_buttons)
        self.assertTrue(self.panel.isVisible())

    def test_ordinary_expiry_keeps_existing_linger_behavior(self):
        retained = self.countdown("NPC respawn", seconds=10)
        ordinary = self.countdown("Chat timer", seconds=10, retained=False)
        self.assertIn(retained.id, self.panel._expired_buttons)
        self.assertNotIn(ordinary.id, self.panel._expired_buttons)
        self.assertIn("0:00", [text for _, text, _ in self.rendered_text()])
        self.clock.return_value += self.runner.board.LINGER_S + 1
        self.runner._tick()
        self.app.processEvents()
        self.assertEqual(self.runner.board.timers, [retained])
        self.assertIn(retained.id, self.panel._expired_buttons)
        self.assertFalse(self.panel._anim.isActive())

    def test_scroll_exposes_hidden_timers_and_clamps_after_dismiss(self):
        timers = [self.countdown(f"NPC {index:02d} respawn") for index in range(MAX_ROWS + 1)]
        self.assertTrue(self.panel._scroll.isVisible())
        self.assertEqual(self.panel._scroll.maximum(), 1)
        self.assertNotIn(timers[-1].id, self.panel._expired_buttons)
        point = self.panel.rect().center()
        wheel = QWheelEvent(QPointF(point), QPointF(self.panel.mapToGlobal(point)), QPoint(), QPoint(0, -120),
                            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                            Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(self.panel, wheel)
        self.app.processEvents()
        self.panel.grab()
        self.assertTrue(wheel.isAccepted())
        self.assertEqual(self.panel._scroll.value(), 1)
        self.assertIn(timers[-1].id, self.panel._expired_buttons)
        self.assertNotIn(timers[0].id, self.panel._expired_buttons)
        self.assertEqual(len(self.panel._rows), MAX_ROWS)
        self.click(self.panel._expired_buttons[timers[-1].id][1])
        self.assertEqual((self.panel._scroll.minimum(), self.panel._scroll.maximum(), self.panel._scroll.value()),
                         (0, 0, 0))
        self.assertFalse(self.panel._scroll.isVisible())
        self.assertEqual(set(self.panel._expired_buttons), {timer.id for timer in timers[:-1]})

    def test_controls_and_labels_fit_at_all_font_scales_and_widths(self):
        self.countdown("a very ancient skeletal warrior of the northern crypts respawn")
        for px in (7.8, 13, 26):
            with self.subTest(px=px):
                self.panel.set_font_px(px)
                minimum = self.panel.minimumWidth()
                self.assertGreater(minimum, 0)
                for width in (minimum, max(560, minimum)):
                    with self.subTest(width=width):
                        self.owner.resize(width, 100)
                        self.panel.dock()
                        self.panel.sync()
                        self.app.processEvents()
                        self.assert_controls_fit()
                self.make_free(width=100)
                self.assertGreaterEqual(self.panel.width(), minimum)
                self.assert_controls_fit()

    def assert_controls_fit(self):
        rendered = self.rendered_text()
        bounds = QRectF(self.panel.rect())
        for rect, text, font in rendered:
            self.assertTrue(bounds.contains(rect), f"Text rect escapes panel: {text}")
            self.assertGreater(rect.width(), 0)
            self.assertGreaterEqual(rect.height(), QFontMetricsF(font).height())
            self.assertLessEqual(QFontMetricsF(font).horizontalAdvance(text), rect.width() + 1)
        for timer_id, (restart, dismiss) in self.panel._expired_buttons.items():
            self.assertLess(restart.geometry().right(), dismiss.geometry().left())
            label_rect = rendered[0][0]
            self.assertLessEqual(label_rect.right(), restart.geometry().left())
            for button, action in ((restart, "Restart"), (dismiss, "Dismiss")):
                self.assertTrue(bounds.contains(QRectF(button.geometry())))
                self.assertEqual(button.text(), "")
                self.assertEqual(button.width(), button.height())
                self.assertFalse(button.icon().isNull())
                self.assertIn(action, button.accessibleName())
                self.assertIn(action, button.toolTip())
                icon_size = button.iconSize()
                self.assertGreater(icon_size.width(), 0)
                self.assertGreater(icon_size.height(), 0)
                self.assertLessEqual(icon_size.width(), button.width() - 2)
                self.assertLessEqual(icon_size.height(), button.height() - 2)
                # Icons must contain visible strokes at normal and high-DPI sizes.
                for resolution in (icon_size, icon_size * 2):
                    icon = button.icon().pixmap(resolution).toImage()
                    self.assertFalse(icon.isNull())
                    self.assertTrue(any(icon.pixelColor(x, y).alpha() > 0
                                        for y in range(icon.height()) for x in range(icon.width())),
                                    f"{action} icon is blank at {resolution}")


if __name__ == "__main__":
    unittest.main()
