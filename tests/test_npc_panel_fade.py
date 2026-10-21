"""NPC lifetime integration with real offscreen widgets and delivered Qt events."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QEnterEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QMenu, QWidget

from mnmparse.app.npc_panel import NpcPanel
from mnmparse.npc_wiki import NpcDetails


class NpcPanelFadeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.owner = QWidget()
        self.owner.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.owner.locked = True
        self.owner.click_through = False
        self.owner.opacity = 0.85
        self.owner._cfg = SimpleNamespace(reduced_motion=False)
        self.owner.setGeometry(20, 20, 360, 80)
        self.owner.dock_anchor = lambda _panel: self.owner.geometry()
        self.owner.panels_changed = Mock()
        self.owner.menu_qss = lambda: ""
        self.owner.show()
        flags = (Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                 | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.panel = NpcPanel(self.owner, self.settings, flags)
        self.panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.now = 100.0
        self.panel._lifetime._clock = lambda: self.now
        self.cursor = QPoint(-10000, -10000)
        self.cursor_patch = patch("mnmparse.app.npc_panel.QCursor.pos", side_effect=lambda: self.cursor)
        self.motion_patch = patch("mnmparse.app.npc_panel_lifetime.reduced_motion", side_effect=lambda cfg: cfg.reduced_motion)
        self.cursor_patch.start()
        self.motion_patch.start()

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.owner.close()
        self.owner.deleteLater()
        self.app.processEvents()
        self.cursor_patch.stop()
        self.motion_patch.stop()
        self.settings.sync()
        self.directory.cleanup()

    def present(self):
        self.panel.show_npc("Example Keeper")
        self.assertTrue(self.panel.isVisible())

    def tick(self, seconds):
        self.now += seconds
        self.panel._lifetime._timer.timeout.emit()

    def enter(self, widget):
        local = widget.rect().center()
        self.cursor = widget.mapToGlobal(local)
        event = QEnterEvent(QPointF(local), QPointF(local), QPointF(self.cursor))
        self.app.sendEvent(widget, event)

    def leave(self, widget):
        self.cursor = QPoint(-10000, -10000)
        self.app.sendEvent(widget, QEvent(QEvent.Type.Leave))

    def test_five_second_expiry_and_late_results_do_not_redisplay_card(self):
        self.present()
        self.tick(4.60)
        self.assertEqual(self.panel.windowOpacity(), 1.0)
        self.tick(0.20)
        self.assertGreater(self.panel.windowOpacity(), 0.0)
        self.assertLess(self.panel.windowOpacity(), 1.0)
        self.owner.panels_changed.reset_mock()
        self.tick(0.20)
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.panel._lifetime.expired)
        self.owner.panels_changed.assert_called_once_with()
        self.panel.set_details(NpcDetails("Example Keeper", "12", unique_drops=("Late staff",)))
        self.panel.set_status("A background revision completed")
        self.panel.refresh_style()
        self.panel.configure(common_drops=True)
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.panel._lifetime.expired)

    def test_explicit_same_npc_renews_lifetime_before_and_after_expiry(self):
        self.present()
        self.tick(4.80)
        self.panel.show_npc("Example Keeper")
        self.assertEqual(self.panel.windowOpacity(), 1.0)
        self.tick(4.90)
        self.assertTrue(self.panel.isVisible())
        self.tick(0.10)
        self.assertFalse(self.panel.isVisible())
        self.panel.show_npc("Example Keeper")
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel._lifetime.expired)
        self.tick(5.0)
        self.assertFalse(self.panel.isVisible())

    def test_late_data_after_deadline_expires_before_queued_timeout(self):
        self.present()
        self.now += 5.10
        self.panel.set_details(NpcDetails("Example Keeper", "12"))
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.panel._lifetime.expired)

    def test_real_enter_restores_opacity_and_leave_grants_fresh_five_seconds(self):
        self.present()
        self.tick(4.80)
        self.assertLess(self.panel.windowOpacity(), 1.0)
        self.enter(self.panel)
        self.assertEqual(self.panel.windowOpacity(), 1.0)
        self.tick(40.0)
        self.assertTrue(self.panel.isVisible())
        self.assertFalse(self.panel._lifetime.expired)
        self.leave(self.panel)
        self.tick(4.90)
        self.assertTrue(self.panel.isVisible())
        self.tick(0.10)
        self.assertFalse(self.panel.isVisible())

    def test_real_scrollbar_child_hover_pauses_and_child_leave_restarts_countdown(self):
        self.present()
        self.panel.set_details(NpcDetails("Example Keeper", "12", unique_drops=tuple(f"Example item {i}" for i in range(150))))
        self.assertTrue(self.panel._scroll.isVisible())
        self.tick(4.80)
        self.enter(self.panel._scroll)
        self.assertEqual(self.panel.windowOpacity(), 1.0)
        self.tick(40.0)
        self.assertTrue(self.panel.isVisible())
        self.leave(self.panel._scroll)
        self.app.processEvents()  # Deliver the child's deferred whole-panel check.
        self.assertFalse(self.panel._lifetime._hovered)
        self.tick(5.0)
        self.assertFalse(self.panel.isVisible())

    def test_scrollbar_lost_capture_releases_interaction_and_expires_after_leave(self):
        self.present()
        self.panel.set_details(NpcDetails("Example Keeper", "12", unique_drops=tuple(f"Example item {i}" for i in range(150))))
        scrollbar = self.panel._scroll
        self.assertTrue(scrollbar.isVisible())
        self.enter(scrollbar)
        local = scrollbar.rect().center()
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local),
                            QPointF(scrollbar.mapToGlobal(local)), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.app.sendEvent(scrollbar, press)
        self.assertTrue(self.panel._lifetime._interacting)
        self.leave(scrollbar)
        self.app.processEvents()
        self.assertFalse(self.panel._lifetime._hovered)
        self.tick(40.0)
        self.assertTrue(self.panel.isVisible())
        self.app.sendEvent(scrollbar, QEvent(QEvent.Type.UngrabMouse))
        self.app.processEvents()
        self.assertFalse(self.panel._lifetime._interacting)
        self.tick(4.90)
        self.assertTrue(self.panel.isVisible())
        self.tick(0.10)
        self.assertFalse(self.panel.isVisible())

    def test_context_menu_event_holds_card_until_menu_returns(self):
        self.present()
        self.tick(4.80)
        held = []

        def menu_exec(_position):
            held.append(self.panel._lifetime._interacting)
            self.tick(40.0)
            self.assertTrue(self.panel.isVisible())
            self.assertEqual(self.panel.windowOpacity(), 1.0)
            return None

        position = self.panel.rect().center()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, position, self.panel.mapToGlobal(position))
        class PassiveMenu(QMenu):
            def exec(self, position):
                return menu_exec(position)

        with patch("mnmparse.app.docked_panel.QMenu", PassiveMenu):
            self.app.sendEvent(self.panel, event)
        self.assertEqual(held, [True])
        self.assertFalse(self.panel._lifetime._interacting)
        self.tick(4.90)
        self.assertTrue(self.panel.isVisible())
        self.tick(0.10)
        self.assertFalse(self.panel.isVisible())

    def test_click_through_change_clears_real_hover_hold(self):
        self.present()
        self.enter(self.panel)
        self.tick(40.0)
        self.assertTrue(self.panel.isVisible())
        self.owner.click_through = True
        self.panel.sync()
        self.assertFalse(self.panel._lifetime._hovered)
        self.enter(self.panel)  # Even a stale enter must not hold click-through UI.
        self.tick(5.0)
        self.assertFalse(self.panel.isVisible())

    def test_owner_hidden_during_hover_does_not_revive_expired_card(self):
        self.present()
        self.enter(self.panel)
        self.owner.hide()
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.tick(10.0)
        self.owner.show()
        self.panel.sync()
        self.assertFalse(self.panel.isVisible())
        self.assertTrue(self.panel._lifetime.expired)

    def test_reduced_motion_disables_only_animation_on_real_window(self):
        self.owner._cfg.reduced_motion = True
        self.present()
        self.tick(4.90)
        self.assertTrue(self.panel.isVisible())
        self.assertEqual(self.panel.windowOpacity(), 1.0)
        self.tick(0.10)
        self.assertFalse(self.panel.isVisible())


if __name__ == "__main__":
    unittest.main()
