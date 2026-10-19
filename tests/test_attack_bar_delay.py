"""The auto-attack menu saves a weapon-delay seed without replacing learned timing."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMenu, QWidget

from mnmparse.app import theme
from mnmparse.app.attack_bar import AttackBar
from mnmparse.app.window_identity import window_title
from mnmparse.parser import parse_line


class AttackBarDelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "settings.ini")
        self.settings = QSettings(self.path, QSettings.Format.IniFormat)
        self.owner = QWidget()
        self.owner._cfg = SimpleNamespace(player_name="Birch")
        self.owner.menu_qss = lambda: "QMenu { background: #18222c; color: #ffffff; }"
        self.bars: list[AttackBar] = []

    def tearDown(self) -> None:
        for bar in self.bars:
            bar._timer.stop()
            bar.close()
            bar.deleteLater()
        self.owner.close()
        self.owner.deleteLater()
        self.app.processEvents()
        self.settings.sync()
        self.directory.cleanup()

    def bar(self, settings: QSettings | None = None) -> AttackBar:
        bar = AttackBar(self.owner, settings or self.settings, Qt.WindowType.Tool)
        self.bars.append(bar)
        return bar

    def edit(self, bar: AttackBar, value: int, *, accepted: bool = True) -> None:
        def execute(dialog: QInputDialog) -> QDialog.DialogCode:
            dialog.setIntValue(value)
            return QDialog.DialogCode.Accepted if accepted else QDialog.DialogCode.Rejected

        menu = QMenu(bar)
        handlers = bar.add_menu_entries(menu, None)
        action = next(action for action in menu.actions() if action.text() == "Weapon Delay")
        try:
            with patch.object(QInputDialog, "exec", execute):
                handlers[action]()
        finally:
            menu.deleteLater()

    @staticmethod
    def swing(bar: AttackBar, ts: float):
        bar.tracker.observe(parse_line("You crush a rat for 10 points of damage.", ts, "Birch"))
        return bar.tracker.hands(ts)[0]

    def test_context_menu_opens_themed_integer_dialog_with_units_and_seed_help(self) -> None:
        bar = self.bar()
        seen = []

        def menu_execute(menu: QMenu, position: QPoint):
            self.assertEqual(menu.styleSheet(), self.owner.menu_qss())
            self.assertEqual(position, QPoint(12, 34))
            names = [action.text() for action in menu.actions()]
            self.assertIn("Hide auto-attack bar", names)
            self.assertIn("Snap to overlay", names)
            return next(action for action in menu.actions() if action.text() == "Weapon Delay")

        def dialog_execute(dialog: QInputDialog):
            seen.append(dialog.intValue())
            self.assertEqual(dialog.windowTitle(), window_title("weapon-delay"))
            self.assertIn(theme.BG0, dialog.styleSheet())
            self.assertEqual(dialog.inputMode(), QInputDialog.InputMode.IntInput)
            self.assertEqual(dialog.intMinimum(), 0)
            self.assertIn("36 = 3.6 seconds", dialog.labelText())
            self.assertIn("0 for automatic", dialog.labelText())
            self.assertIn("starting estimate", dialog.labelText())
            self.assertIn("actual swings", dialog.labelText())
            return QDialog.DialogCode.Rejected

        class Menu(QMenu):
            def exec(self, position: QPoint):
                return menu_execute(self, position)

        with patch("mnmparse.app.docked_panel.QMenu", Menu), patch.object(QInputDialog, "exec", dialog_execute):
            bar.contextMenuEvent(SimpleNamespace(globalPos=lambda: QPoint(12, 34)))
        self.assertEqual(seen, [0])
        self.assertFalse(self.settings.contains("attack_bar/weapon_delay"))

    def test_36_becomes_a_3_6_second_starting_delay_and_persists(self) -> None:
        bar = self.bar()
        self.edit(bar, 36)
        self.assertEqual(self.settings.value("attack_bar/weapon_delay", type=int), 36)
        hand = self.swing(bar, time.time())
        self.assertEqual((hand.delay, hand.swings), (3.6, 1))

        self.settings.sync()
        restored = self.bar(QSettings(self.path, QSettings.Format.IniFormat))
        self.assertEqual(self.swing(restored, time.time()).delay, 3.6)

        def execute(dialog: QInputDialog):
            self.assertEqual(dialog.intValue(), 36)
            return QDialog.DialogCode.Rejected

        with patch.object(QInputDialog, "exec", execute):
            restored._edit_weapon_delay()

    def test_cancel_preserves_saved_value_and_starting_delay(self) -> None:
        self.settings.setValue("attack_bar/weapon_delay", 36)
        bar = self.bar()
        self.edit(bar, 50, accepted=False)
        self.assertEqual(self.settings.value("attack_bar/weapon_delay", type=int), 36)
        self.assertEqual(self.swing(bar, time.time()).delay, 3.6)

    def test_zero_removes_the_starting_delay(self) -> None:
        self.settings.setValue("attack_bar/weapon_delay", 36)
        bar = self.bar()
        self.edit(bar, 0)
        self.assertEqual(self.settings.value("attack_bar/weapon_delay", type=int), 0)
        hand = self.swing(bar, time.time())
        self.assertIsNone(hand.delay)
        self.assertEqual(hand.swings, 1)

    def test_changing_or_clearing_delay_keeps_learned_timing_and_swings(self) -> None:
        bar = self.bar()
        start = time.time() - 9
        for index in range(4):
            hand = self.swing(bar, start + index * 3)
        self.assertEqual(hand.delay, 3.0)
        previous = (hand.delay, hand.last, hand.swings)
        for value in (36, 0):
            with self.subTest(value=value):
                self.edit(bar, value)
                hand = bar.tracker.hands(time.time())[0]
                self.assertEqual((hand.delay, hand.last, hand.swings), previous)

    def test_invalid_saved_delay_falls_back_to_automatic(self) -> None:
        for value in ("invalid", -1, 1000):
            with self.subTest(value=value):
                self.settings.setValue("attack_bar/weapon_delay", value)
                self.assertIsNone(self.swing(self.bar(), time.time()).delay)

    def test_seed_restarts_an_idle_visible_bar_without_a_new_swing(self) -> None:
        bar = self.bar()
        with patch("mnmparse.app.attack_bar.time.time", return_value=100.0):
            bar.show()
            bar.observe(parse_line("You crush a rat for 10 points of damage.", 100, "Birch"))
        with patch("mnmparse.app.attack_bar.time.time", return_value=109.0):
            bar._tick()
        self.assertFalse(bar._timer.isActive())
        with patch("mnmparse.app.attack_bar.time.time", return_value=110.0):
            self.edit(bar, 200)
        self.assertTrue(bar._timer.isActive())
        hand = bar.tracker.hands(110)[0]
        self.assertEqual((hand.delay, hand.last, hand.swings), (20.0, 100.0, 1))
        self.assertEqual(hand.progress(110), 0.5)

    def test_restored_delay_seeds_an_existing_bar_but_does_not_start_it_while_hidden(self) -> None:
        bar = self.bar()
        self.settings.setValue("attack_bar/weapon_delay", 36)
        bar.reload_preferences()
        self.assertFalse(bar._timer.isActive())
        self.assertEqual(self.swing(bar, time.time()).delay, 3.6)


if __name__ == "__main__":
    unittest.main()
