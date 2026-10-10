"""Carebear Mode changes performance comparisons without changing chat triggers."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.config import Config
from mnmparse.privacy import safe_static_trigger_title, safe_trigger_definition, safe_trigger_match
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
                               timer=True, timer_seconds=600, action="speak", speech="Rebuff",
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

    def test_rebuff_preserves_spell_substitution_and_custom_alert_speech(self):
        self.trigger.mode = "regex"
        self.trigger.pattern = r"^You begin casting (?P<spell>Armor)\.$"
        self.trigger.timer_label = "{spell} rebuff"
        self.trigger.speech = "Rebuff {spell}"
        self.trigger.timer_warn_speech = "Check {spell}"
        self.trigger.timer_end_speech = "Rebuff Rebuff"
        result = self.runner.observe("You begin casting Armor.", now=100)
        timer = self.runner.board.timers[0]
        self.assertEqual(timer.label, "Armor rebuff")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Rebuff Armor")
        self.assertIs(result[0].trigger, self.trigger)
        self.assertEqual(result[0].groups["spell"], "Armor")
        self.assertEqual(result[0].line, "You begin casting Armor.")
        self.assertIs(safe_trigger_match(result[0], self.cfg), result[0])
        self.runner._alert(timer, "warn")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Check Armor")
        self.runner._alert(timer, "end")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Rebuff Rebuff")

    def test_installed_proc_style_keeps_own_damage_and_authored_speech(self):
        self.trigger.name = "DevoProc (copy)"
        self.trigger.mode = "regex"
        self.trigger.pattern = r"Your Righteous Smite II hits .+? for (?P<damage>\d+) points of Holy Damage"
        self.trigger.timer_label = "Righteous Smite II: {damage} damage"
        self.trigger.speech = "Boom Boom {damage}"
        matches = self.runner.observe("Your Righteous Smite II hits Tamsin for 42 points of Holy Damage.", now=100)
        self.assertEqual(matches[0].groups["damage"], "42")
        self.assertIn("Tamsin", matches[0].line)
        self.assertIn("Righteous Smite II", matches[0].line)
        self.assertEqual(self.runner.board.timers[0].label, "Righteous Smite II: 42 damage")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Boom Boom 42")

    def test_capture_is_unchanged_even_without_configured_identity(self):
        self.runner.set_config(Config())
        for line in ("You begin to feel refreshed.", "Your Armor wears off."):
            with self.subTest(line=line):
                match = Match(self.trigger, line, line)
                projected = safe_trigger_match(match, Config())
                self.assertIs(projected, match)
                self.assertEqual(projected.line, line)
                self.runner.fire(match, now=100)
                self.assertEqual(self.runner.board.timers[0].label, "Armor rebuff")
        other = safe_trigger_match(Match(self.trigger, "Owner begins casting Armor.", ""), Config())
        self.assertEqual(other.line, "Owner begins casting Armor.")
        self.assertEqual(other.trigger.name, "Armor rebuff")

    def test_own_heal_keeps_normal_amount_and_target_placeholders(self):
        self.trigger.name = "My healing alert"
        self.trigger.timer_label = "Heal {damage} {who}"
        self.trigger.speech = "{name}: {damage} {who} {line}"
        self.runner.fire(Match(self.trigger, "Your Heal heals Tamsin for 100 Health.", "Tamsin",
                               {"who": "Tamsin", "damage": "100"}), now=100)
        timer = self.runner.board.timers[0]
        self.assertEqual(timer.label, "Heal 100 Tamsin")
        self.assertEqual(timer.template_values["who"], "Tamsin")
        self.assertEqual(timer.template_values["damage"], "100")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"],
                         "My healing alert: 100 Tamsin Your Heal heals Tamsin for 100 Health.")

    def test_mode_changes_keep_captured_values_available_to_running_alerts(self):
        self.trigger.mode = "regex"
        self.trigger.pattern = r"(?P<who>Tamsin) crushes a rat for (?P<damage>\d+) points of damage\."
        self.trigger.timer_label = "Damage alert {who} {damage}"
        self.trigger.timer_warn_speech = "Warning {who} {damage}"
        self.trigger.timer_end_speech = "Finished {who} {damage}"
        self.runner.set_config(dataclasses.replace(self.cfg, casual_mode=False, casual_mode_confirmed=True))
        self.runner.observe("Tamsin crushes a rat for 12345 points of damage.", now=100)
        timer = self.runner.board.timers[0]
        self.assertEqual(timer.label, "Damage alert Tamsin 12345")
        self.runner.set_config(self.cfg)
        self.assertEqual(timer.label, "Damage alert Tamsin 12345")
        for which, expected in (("warn", "Warning Tamsin 12345"), ("end", "Finished Tamsin 12345")):
            self.runner._alert(timer, which)
            self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], expected)

    def test_titles_and_own_values_survive_repeated_configuration_and_manual_restart(self):
        self.trigger.mode = "regex"
        self.trigger.pattern = r"You begin casting (?P<spell>Armor)"
        self.trigger.timer_label = "{spell} rebuff"
        self.runner.set_config(dataclasses.replace(self.cfg, casual_mode=False, casual_mode_confirmed=True))
        self.runner.observe("You begin casting Armor.", now=100)
        self.runner.set_config(self.cfg)
        self.runner.set_config(self.cfg)
        timer = self.runner.board.timers[0]
        self.assertEqual(timer.label, "Armor rebuff")
        self.assertEqual(timer.template_values["spell"], "Armor")
        self.trigger.name = "Shield rebuff"
        self.runner.observe("You begin casting Armor.", now=101)
        self.assertEqual(self.runner.board.timers[0].label, "Armor rebuff")
        manual = self.runner.start_one_time_timer("My rebuff", 10, now=100, keep_until_dismissed=True)
        self.runner.set_config(self.cfg)
        manual.ended = True
        self.assertTrue(self.runner.restart_timer(manual.id, now=115))
        self.assertEqual(manual.label, "My rebuff")

    def test_definitions_and_captured_source_are_visible_in_carebear(self):
        self.trigger.name = "Tamsin alert"
        self.trigger.pattern = r"(?P<who>Tamsin) heals .+ for (?P<damage>\d+)"
        self.trigger.mode = "regex"
        self.trigger.speech = "My custom Tamsin alert"
        self.assertEqual(safe_static_trigger_title(self.trigger, self.cfg), "Tamsin alert")
        self.assertEqual(safe_trigger_definition(self.trigger, self.cfg), self.trigger.to_dict())
        projected = safe_trigger_match(Match(self.trigger, "Tamsin heals Owner for 12345",
                                             "Tamsin", {"who": "Tamsin", "damage": "12345"}), self.cfg)
        self.assertEqual(projected.line, "Tamsin heals Owner for 12345")
        self.assertEqual(projected.text, "Tamsin")
        self.assertEqual(projected.groups, {"who": "Tamsin", "damage": "12345"})
        self.assertEqual(projected.values()["name"], "Tamsin alert")

    def test_disabled_trigger_test_preserves_safe_own_captures(self):
        self.trigger.enabled = False
        self.trigger.mode = "regex"
        self.trigger.pattern = r"You crush a rat for (?P<damage>\d+) points of damage\."
        self.trigger.speech = "Boom Boom {damage}"
        self.runner.test(self.trigger, "You crush a rat for 42 points of damage.")
        self.assertEqual(self.runner.audio.run.call_args.kwargs["speech"], "Boom Boom 42")
        self.assertFalse(self.trigger.enabled)


if __name__ == "__main__":
    unittest.main()
