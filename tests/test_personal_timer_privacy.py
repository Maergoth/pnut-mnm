"""Personal countdown titles remain useful without replaying captured player data."""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.config import Config
from mnmparse.privacy import safe_static_trigger_title
from mnmparse.trigger_presets import PRESETS
from mnmparse.triggers import Match, Trigger, TriggerStore


class PersonalTimerPrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name="Owner")
        self.store = TriggerStore(Path(self.tmp.name) / "triggers.json")
        self.trigger = Trigger(name="Armor rebuff", pattern="You begin casting Armor",
                               timer=True, timer_seconds=600, action="speak", speech="{line}",
                               timer_warn_action="speak", timer_end_action="speak")
        self.store.triggers = [self.trigger]
        self.runner = TriggerRunner(self.store)
        self.runner.set_config(self.cfg)
        self.runner.audio.run = Mock()

    def tearDown(self):
        self.runner._clock.stop()
        self.runner.audio.stop()
        self.runner.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_personal_rebuff_keeps_title_and_withholds_source_and_captures(self):
        self.trigger.mode = "regex"
        self.trigger.pattern = r"^You begin casting (?P<spell>Armor)\.$"
        self.trigger.timer_label = "{spell} {line}"
        result = self.runner.observe("You begin casting Armor.", now=100)
        timer = self.runner.board.timers[0]
        self.assertEqual(timer.label, "Armor rebuff")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Armor rebuff")
        self.assertEqual(result[0].trigger.name, "Armor rebuff")
        self.assertEqual(result[0].groups, {})
        self.assertEqual(result[0].text, "")
        self.assertNotIn("You begin casting Armor", json.dumps(dataclasses.asdict(result[0])))
        self.runner._alert(timer, "warn")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Armor rebuff soon")
        self.runner._alert(timer, "end")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Armor rebuff ended")

    def test_self_status_and_possessive_buff_expiry_are_personal(self):
        for line in ("You begin to feel refreshed.", "Your Armor wears off.", "Owner begins casting Armor."):
            with self.subTest(line=line):
                self.runner.fire(Match(self.trigger, line, line), now=100)
                self.assertEqual(self.runner.board.timers[0].label, "Armor rebuff")
        self.runner.set_config(Config())
        self.runner.fire(Match(self.trigger, "Your Armor wears off.", ""), now=100)
        self.assertEqual(self.runner.board.timers[0].label, "Armor rebuff")
        self.runner.fire(Match(self.trigger, "Owner begins casting Armor.", ""), now=101)
        self.assertEqual(self.runner.board.timers[0].label, "Timer")

    def test_own_heal_cannot_expose_a_peers_identity_in_static_title(self):
        self.trigger.name = "Tamsin heals"
        self.trigger.timer_label = "Tamsin countdown"
        self.runner.fire(Match(self.trigger, "Your Heal heals Tamsin for 100 Health.", "",
                               {"who": "Tamsin", "damage": "100"}), now=100)
        self.assertEqual(self.runner.board.timers[0].label, "Timer")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Timer")

    def test_titles_survive_mode_changes_replacements_and_manual_restart(self):
        self.runner.set_config(dataclasses.replace(self.cfg, casual_mode=False, casual_mode_confirmed=True))
        self.runner.fire(Match(self.trigger, "You begin casting Armor.", ""), now=100)
        self.runner.set_config(self.cfg)
        self.runner.set_config(self.cfg)
        self.assertEqual(self.runner.board.timers[0].label, "Armor rebuff")
        self.trigger.name = "Shield rebuff"
        self.runner.fire(Match(self.trigger, "You begin casting Armor.", ""), now=101)
        self.assertEqual(self.runner.board.timers[0].label, "Shield rebuff")
        self.runner.set_config(Config(player_name="Different"))
        self.assertEqual(self.runner.board.timers[0].label, "Timer")
        manual = self.runner.start_one_time_timer("My rebuff", 10, now=100, keep_until_dismissed=True)
        self.runner.set_config(self.cfg)
        manual.ended = True
        self.assertTrue(self.runner.restart_timer(manual.id, now=115))
        self.assertEqual(manual.label, "My rebuff")

    def test_definition_titles_require_own_literal_provenance_or_exact_public_preset(self):
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Armor rebuff")
        for pattern in ("Your Armor wears off.", "You begin to feel refreshed."):
            self.trigger.pattern = pattern
            self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Armor rebuff")
        self.trigger.pattern = "Armor"
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Timer")
        self.trigger.mode = "regex"
        self.trigger.pattern = r"^Your Armor wears off\.$"
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Armor rebuff")
        self.trigger.pattern = r"Your Armor wears off\.$"
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Timer")
        self.trigger.pattern = r"^Your Armor wears off\.$|Tamsin dies"
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Timer")
        public = Trigger.from_dict(PRESETS[0])
        public.timer_label = "{who} {damage}"
        self.assertEqual(safe_static_trigger_title(public, self.cfg), "Gatekick")
        public.name = "Tamsin is weak"
        self.assertEqual(safe_static_trigger_title(public, self.cfg), "Timer")

    def test_page_preserves_approved_recent_title_and_hides_raw_peer_match(self):
        from mnmparse.app.main import _MissingEngine
        from mnmparse.app.triggers_page import TriggersPage

        page = TriggersPage(_MissingEngine(self.cfg), self.cfg,
                            QSettings(str(Path(self.tmp.name) / "test.ini"), QSettings.Format.IniFormat))
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        page.set_runner(self.runner)
        try:
            self.assertIn("Armor rebuff", page.list.item(0).text())
            self.runner.observe("You begin casting Armor.", now=100)
            self.assertIn("Armor rebuff", page.recent.item(0).text())
            self.assertNotIn("You begin casting Armor", page.recent.item(0).text())
            page._on_fired(Match(Trigger(name="Tamsin is weak"), "Tamsin hits for 17", ""))
            self.assertNotIn("Tamsin", page.recent.item(0).text())
        finally:
            page._dirty = False
            page._save_timer.stop()
            page.close()
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
