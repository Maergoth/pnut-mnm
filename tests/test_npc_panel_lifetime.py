"""Deterministic NPC-card lifetime checks, without sleeping or visible windows."""
from __future__ import annotations

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication

from mnmparse.app.npc_panel_lifetime import NpcPanelLifetime


class _Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Panel(QObject):
    """Only the QWidget surface used by the helper; never creates a window."""

    def __init__(self):
        super().__init__()
        self.visible = True
        self.enabled = True
        self.opacity = 1.0
        self.opacity_changes = []
        self.hides = 0
        self.owner_visible = True
        self.restacked = 0
        self._owner = SimpleNamespace(
            click_through=False,
            isVisible=lambda: self.owner_visible,
            panels_changed=self._restack,
            _cfg=SimpleNamespace(reduced_motion=False),
        )
        self.lifetime = None

    def _restack(self):
        self.restacked += 1

    def isVisible(self):
        return self.visible

    def setWindowOpacity(self, value):
        self.opacity = value
        self.opacity_changes.append(value)

    def hide(self):
        self.visible = False
        self.hides += 1
        if self.lifetime is not None:
            self.lifetime.hidden()


class NpcPanelLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.clock = _Clock()
        self.panel = _Panel()
        self.lifetime = NpcPanelLifetime(self.panel, self.clock)
        self.panel.lifetime = self.lifetime
        # System animation preferences must not make the tests host-dependent.
        self.motion = patch("mnmparse.app.npc_panel_lifetime.reduced_motion", side_effect=lambda cfg: cfg.reduced_motion)
        self.motion.start()

    def tearDown(self):
        self.lifetime.stop()
        self.panel.deleteLater()
        self.app.processEvents()
        self.motion.stop()

    def advance(self, seconds):
        self.clock.advance(seconds)
        self.lifetime.sync()

    def test_initial_background_sync_cannot_start_lifetime(self):
        self.assertTrue(self.lifetime.expired)
        self.lifetime.sync()
        self.assertTrue(self.lifetime.expired)
        self.assertFalse(self.lifetime._timer.isActive())
        self.assertEqual(self.panel.hides, 0)

    def test_fades_only_final_350ms_and_hides_at_five_seconds(self):
        self.lifetime.present()
        self.assertFalse(self.lifetime.expired)
        self.assertTrue(self.lifetime._timer.isActive())
        self.advance(4.60)
        self.assertEqual(self.panel.opacity, 1.0)
        self.advance(0.20)
        self.assertAlmostEqual(self.panel.opacity, 0.20 / 0.35)
        self.assertTrue(self.panel.visible)
        self.advance(0.20)
        self.assertTrue(self.lifetime.expired)
        self.assertFalse(self.panel.visible)
        self.assertEqual(self.panel.hides, 1)
        self.assertEqual(self.panel.restacked, 1)
        self.assertEqual(self.panel.opacity, 1.0)
        self.assertFalse(self.lifetime._timer.isActive())

    def test_hover_during_fade_restores_opacity_and_pauses(self):
        self.lifetime.present()
        self.advance(4.80)
        self.assertLess(self.panel.opacity, 1.0)
        self.lifetime.set_hovered(True)
        self.assertEqual(self.panel.opacity, 1.0)
        self.advance(50.0)
        self.assertFalse(self.lifetime.expired)
        self.assertTrue(self.panel.visible)
        self.assertFalse(self.lifetime._timer.isActive())
        self.lifetime.set_hovered(False)
        self.advance(4.99)
        self.assertFalse(self.lifetime.expired)
        self.advance(0.01)
        self.assertTrue(self.lifetime.expired)

    def test_repeated_leave_and_background_sync_do_not_renew_deadline(self):
        self.lifetime.present()
        self.advance(3.0)
        self.lifetime.set_hovered(False)
        self.lifetime.set_interacting(False)
        for _ in range(4):
            self.advance(0.50)
        self.assertTrue(self.lifetime.expired)

    def test_interaction_pauses_until_both_hover_and_interaction_clear(self):
        self.lifetime.present()
        self.lifetime.set_hovered(True)
        self.lifetime.set_interacting(True)
        self.lifetime.set_hovered(False)
        self.advance(50.0)
        self.assertFalse(self.lifetime.expired)
        self.lifetime.set_interacting(False)
        self.advance(5.0)
        self.assertTrue(self.lifetime.expired)

    def test_late_background_updates_do_not_resurrect_expired_card(self):
        self.lifetime.present()
        self.advance(5.0)
        self.lifetime.sync()
        self.lifetime.set_hovered(True)
        self.lifetime.set_interacting(True)
        self.advance(50.0)
        self.assertTrue(self.lifetime.expired)
        self.assertEqual(self.panel.hides, 1)
        self.assertFalse(self.panel.visible)

    def test_delayed_hover_or_press_after_deadline_cannot_preempt_timer_expiry(self):
        for method in (self.lifetime.set_hovered, self.lifetime.set_interacting):
            with self.subTest(method=method.__name__):
                self.panel.visible = True
                self.lifetime.present()
                self.clock.advance(5.10)  # Enter/press arrives before queued timeout.
                method(True)
                self.assertTrue(self.lifetime.expired)
                self.assertFalse(self.panel.visible)

    def test_explicit_same_npc_presentation_starts_new_five_seconds(self):
        self.lifetime.present()
        self.advance(4.80)
        self.lifetime.present()
        self.assertEqual(self.panel.opacity, 1.0)
        self.advance(4.99)
        self.assertFalse(self.lifetime.expired)
        self.advance(0.01)
        self.assertTrue(self.lifetime.expired)
        self.lifetime.present()
        self.assertFalse(self.lifetime.expired)
        self.assertFalse(self.panel.visible)  # Panel sync owns showing it again.

    def test_hidden_owner_continues_deadline_and_does_not_reappear_after_expiry(self):
        self.lifetime.present()
        self.advance(2.0)
        self.panel.owner_visible = False
        self.panel.hide()
        self.advance(10.0)
        self.assertTrue(self.lifetime.expired)
        self.panel.owner_visible = True
        self.lifetime.sync()
        self.assertTrue(self.lifetime.expired)
        self.assertFalse(self.panel.visible)
        self.assertEqual(self.panel.restacked, 0)

    def test_hiding_paused_card_clears_stale_hover_and_interaction(self):
        self.lifetime.present()
        self.lifetime.set_hovered(True)
        self.lifetime.set_interacting(True)
        self.panel.owner_visible = False
        self.panel.hide()
        self.advance(5.0)
        self.assertTrue(self.lifetime.expired)

    def test_click_through_cannot_pin_existing_or_new_hover(self):
        self.lifetime.present()
        self.lifetime.set_hovered(True)
        self.advance(20.0)
        self.panel._owner.click_through = True
        self.lifetime.sync()
        self.lifetime.set_hovered(True)
        self.lifetime.set_interacting(True)
        self.advance(5.0)
        self.assertTrue(self.lifetime.expired)

    def test_disabled_panel_clears_interaction_and_expires_while_hidden(self):
        self.lifetime.present()
        self.lifetime.set_interacting(True)
        self.panel.enabled = False
        self.panel.hide()
        self.advance(5.0)
        self.assertTrue(self.lifetime.expired)

    def test_reduced_motion_skips_fade_and_keeps_five_second_deadline(self):
        self.panel._owner._cfg.reduced_motion = True
        self.lifetime.present()
        self.advance(4.90)
        self.assertEqual(self.panel.opacity, 1.0)
        self.advance(0.10)
        self.assertTrue(self.lifetime.expired)
        self.assertEqual(self.panel.opacity_changes, [])

    def test_reduced_motion_change_during_fade_restores_opacity(self):
        self.lifetime.present()
        self.advance(4.80)
        self.assertLess(self.panel.opacity, 1.0)
        self.panel._owner._cfg.reduced_motion = True
        self.lifetime.sync()
        self.assertEqual(self.panel.opacity, 1.0)
        self.advance(0.20)
        self.assertTrue(self.lifetime.expired)

    def test_present_before_panel_show_still_sets_original_deadline(self):
        self.panel.visible = False
        self.lifetime.present()
        self.advance(2.0)
        self.panel.visible = True
        self.lifetime.sync()
        self.advance(3.0)
        self.assertTrue(self.lifetime.expired)

    def test_present_before_panel_show_schedules_fade_without_a_later_sync(self):
        self.panel.visible = False
        self.lifetime.present()
        self.assertLessEqual(self.lifetime._timer.interval(), 4651)
        self.panel.visible = True
        self.clock.advance(4.80)
        self.lifetime._timer.timeout.emit()
        self.assertAlmostEqual(self.panel.opacity, 0.20 / 0.35)
        self.assertFalse(self.lifetime.expired)

    def test_stop_during_fade_is_final_and_cancels_timer(self):
        self.lifetime.present()
        self.advance(4.80)
        self.lifetime.stop()
        self.assertEqual(self.panel.opacity, 1.0)
        self.assertTrue(self.lifetime.expired)
        self.lifetime.present()
        self.lifetime.set_hovered(True)
        self.lifetime.sync()
        self.lifetime.hidden()
        self.assertTrue(self.lifetime.expired)
        self.assertFalse(self.lifetime._timer.isActive())
        self.assertEqual(self.panel.hides, 0)


if __name__ == "__main__":
    unittest.main()
