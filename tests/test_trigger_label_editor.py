"""The trigger label is editable without a countdown and previews captured damage."""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QObject, QSettings, Qt, Signal
    from PySide6.QtWidgets import QApplication

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.config import Config
from mnmparse.triggers import Trigger, TriggerStore


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class TriggerLabelEditorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from mnmparse.app import theme

        cls.app = QApplication.instance() or QApplication([])
        theme.apply_theme(cls.app)

    def setUp(self) -> None:
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage

        class Runner(QObject):
            fired = Signal(object)

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.trigger = Trigger(
            name="Righteous Smite II",
            pattern=r"Your Righteous Smite II hits .+? for (?P<damage>\d+) points of Holy Damage",
            mode="regex", timer=False,
        )
        self.runner = Runner()
        self.runner.store = TriggerStore(self.root / "triggers.json")
        self.runner.store.triggers = [self.trigger]
        self.runner.save = Mock(side_effect=self.runner.store.save)
        self.runner.audio = SimpleNamespace(voices=lambda: [], devices=lambda: [])
        settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.page = TriggersPage(_MissingEngine(Config()), Config(), settings)
        self.page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.page.set_runner(self.runner)

    def tearDown(self) -> None:
        self.page._save_timer.stop()
        self.page.close()
        self.page.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def _edit_label(self, value: str) -> None:
        self.page.timer_label.setText(value)
        self.page.timer_label.textEdited.emit(value)

    def test_label_without_timer_is_visible_editable_and_saved(self) -> None:
        self.page.show()
        self.app.processEvents()
        self.assertTrue(self.page.timer_label.isVisible())
        self.assertTrue(self.page.timer_label.isEnabled())
        self.assertFalse(self.page.duration_row.isEnabled())
        self._edit_label("Righteous Smite II: {damage} damage")
        self.page._save_timer.stop()
        self.page._save_now()

        saved = TriggerStore(self.runner.store.path)
        self.assertTrue(saved.load())
        self.assertFalse(saved.triggers[0].timer)
        self.assertEqual(saved.triggers[0].timer_label, "Righteous Smite II: {damage} damage")

    def test_preview_resolves_damage_and_updates_when_label_changes(self) -> None:
        self._edit_label("Righteous Smite II: {damage} damage")
        self.page.test_line.setText("Your Righteous Smite II hits a skeletal knight for 154 points of Holy Damage.")
        self.assertIn("label “Righteous Smite II: 154 damage”", self.page.test_result.text())
        self.assertIn("Matched text: Your Righteous Smite II hits", self.page.test_result.toolTip())

        self._edit_label("{name}: {damage}!")
        self.assertIn("label “Righteous Smite II: 154!”", self.page.test_result.text())
        self.page.test_line.setText("Your Righteous Smite II hits a skeletal knight for 262 points of Holy Damage.")
        self.assertIn("label “Righteous Smite II: 262!”", self.page.test_result.text())
        self.assertNotIn("154", self.page.test_result.text())

    def test_blank_label_previews_trigger_name_and_preserves_speech_preview(self) -> None:
        self.page.action.setCurrentIndex(self.page.action.findData("speak"))
        self.page.speech.setText("Smite for {damage}")
        self.page.speech.textEdited.emit(self.page.speech.text())
        self._edit_label("")
        self.page.test_line.setText("Your Righteous Smite II hits a skeletal knight for 154 points of Holy Damage.")
        self.assertIn("label “Righteous Smite II”", self.page.test_result.text())
        self.assertIn("says “Smite for 154”", self.page.test_result.text())

    def test_disabling_countdown_keeps_shared_label_editable(self) -> None:
        self._edit_label("{name}: {damage}")
        self.page.timer.setChecked(True)
        self.assertTrue(self.page.duration_row.isEnabled())
        self.page.timer.setChecked(False)
        self.assertTrue(self.page.timer_label.isEnabled())
        self.assertFalse(self.page.duration_row.isEnabled())
        self.assertEqual(self.trigger.timer_label, "{name}: {damage}")


if __name__ == "__main__":
    unittest.main()
