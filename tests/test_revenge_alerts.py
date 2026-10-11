"""Saved-name sightings are quiet, literal notifications independent of recruitment."""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.revenge import RevengeController
from mnmparse.app.timer_panel import TimerPanel, TriggerPopup
from mnmparse.app.triggers_runtime import TriggerRunner
from mnmparse.config import Config, load_config, save_config
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.triggers import Match, Trigger, TriggerStore


class RevengeAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 100.0
        self.elapsed = 10.0
        self.cfg = Config(player_name="Hero", revenge_enabled=True)
        self.controller = RevengeController(self.cfg, Path(self.tmp.name) / "revenge.json",
                                            clock=lambda: self.now, monotonic_clock=lambda: self.elapsed)
        self.alerts = []
        self.controller.alert_requested.connect(self.alerts.append)

    def tearDown(self):
        for timer in (self.controller._timer, self.controller._save_timer,
                      self.controller._activity_refresh, self.controller._display_timer):
            timer.stop()
        self.controller.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def save(self, *names):
        for name in names:
            self.assertTrue(self.controller.save_attacker(name))

    def event(self, text="", **kwargs):
        return Event(self.now, "melee_hit", text, **kwargs)

    def test_sightings_include_actor_target_and_unknown_text_without_own_identity(self):
        self.cfg.player_name = ""
        self.save("Bandit", "Other", "Stranger")
        self.controller.on_message(None, self.event(actor="Bandit", target="Other"))
        self.controller.on_message(None, Event(self.now, "unknown", "Stranger's spell lands."))
        self.assertEqual(self.alerts, ["Bandit", "Other", "Stranger"])
        self.assertEqual(self.controller.pending_candidates, ())

    def test_names_are_case_insensitive_literal_whole_names_not_substrings(self):
        self.save("Bandit")
        for line in ("OtherBandit attacks.", "Bandits attack.", "Bandit_name attacks.", "Bendit attacks.",
                     "You saw Bandit attacks.", "Someone loots a Bandit mask."):
            self.assertEqual(self.controller.observe_saved(line), ())
        self.assertEqual(self.controller.observe_saved("bAnDiT's spell lands."), ("Bandit",))

    def test_manual_unicode_spaces_symbols_and_regex_characters_remain_literal(self):
        self.save("Éowyn 🦋", "a named NPC", "D4rk-Name_", "[One]+(Two)", "{match}")
        for name in self.controller.saved_names:
            self.assertEqual(self.controller.observe_saved(f"{name} attacks."), (name,))
        self.assertEqual(self.controller.observe_saved("OneTwo attacks."), ())

    def test_repeats_rearm_only_after_each_name_has_gone_quiet(self):
        self.save("Bandit", "Other")
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ("Bandit",))
        self.elapsed = 39.0
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ())
        self.assertEqual(self.controller.observe_saved("Other attacks."), ("Other",))
        self.elapsed = 68.9
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ())
        self.elapsed = 98.9
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ("Bandit",))
        self.assertEqual(self.controller.observe_saved("Other attacks."), ("Other",))

    def test_quiet_gap_uses_monotonic_time_despite_wall_clock_change(self):
        self.save("Bandit")
        self.controller.observe_saved("Bandit attacks.")
        self.now = 1_000_000
        self.elapsed = 11
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ())
        self.now = 1
        self.elapsed = 41
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ("Bandit",))

    def test_zero_quiet_gap_deliberately_allows_each_new_line(self):
        self.save("Bandit")
        self.controller.set_config(dataclasses.replace(self.cfg, revenge_alert_cooldown=0))
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ("Bandit",))
        self.assertEqual(self.controller.observe_saved("Bandit attacks again."), ("Bandit",))

    def test_display_limits_do_not_exclude_saved_names_from_alerts(self):
        self.save("Old")
        self.now += 2 * 86400
        self.save("Recent", "Newest")
        self.controller.set_config(dataclasses.replace(self.cfg, revenge_days=1, revenge_entries=1))
        self.assertNotIn("Old", self.controller.saved_names)
        self.assertEqual(self.controller.observe_saved(self.event("Old attacks Recent.", actor="Old", target="Recent")),
                         ("Old", "Recent"))
        self.assertEqual(self.controller.saved_total, 3)

    def test_disabling_alerts_keeps_recruitment_and_saved_activity(self):
        self.save("Bandit")
        self.controller.set_config(dataclasses.replace(self.cfg, revenge_alert_enabled=False))
        self.now += 10
        self.controller.on_message(None, self.event("Bandit hits Other.", actor="Bandit", target="Other"))
        self.assertEqual(self.alerts, [])
        self.assertEqual(self.controller._saved["bandit"].last_activity, self.now)
        self.controller.on_message(None, self.event(actor="Newplayer", target="Hero"))
        self.assertEqual([entry.name for entry in self.controller.pending_candidates], ["Newplayer"])
        self.controller.set_config(dataclasses.replace(self.cfg, revenge_enabled=False))
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ())

    def test_backlog_and_pre_enable_queued_messages_cannot_alert(self):
        self.save("Bandit")
        event = self.event("Bandit attacks.", actor="Bandit")
        self.controller.on_message(SimpleNamespace(backlog=True), event)
        self.controller.set_config(dataclasses.replace(self.cfg, revenge_alert_enabled=False))
        self.now += 1
        self.controller.set_config(self.cfg)
        self.controller.on_message(None, event)
        self.assertEqual(self.alerts, [])
        self.now += 1
        self.controller.on_message(None, self.event("Bandit attacks.", actor="Bandit"))
        self.assertEqual(self.alerts, ["Bandit"])

    def test_new_session_removal_and_reload_reset_sighting_state(self):
        self.save("Bandit")
        self.controller.observe_saved("Bandit attacks.")
        self.controller.new_session()
        self.now += 1
        self.controller.on_message(None, self.event("Bandit attacks.", actor="Bandit"))
        self.assertEqual(self.alerts, ["Bandit", "Bandit"])
        self.assertTrue(self.controller.remove_saved("Bandit"))
        self.assertNotIn("bandit", self.controller._alert_last_seen)
        self.save("Bandit")
        self.controller.observe_saved("Bandit attacks.")
        self.controller._load()
        self.assertEqual(self.controller.observe_saved("Bandit attacks."), ("Bandit",))

    def test_alert_payload_and_storage_keep_names_and_dates_without_combat_source(self):
        self.save("Bandit")
        self.now += 1
        source = "Bandit hits Teammate for 999 damage."
        event = self.event(source, actor="Bandit", target="Teammate", amount=999)
        before = dataclasses.asdict(event)
        self.controller.on_message(SimpleNamespace(text=source), event)
        self.assertEqual(self.alerts, ["Bandit"])
        self.assertEqual(dataclasses.asdict(event), before)
        self.assertTrue(self.controller.flush_pending_changes())
        self.assertNotIn(source, self.controller.path.read_text(encoding="utf-8"))
        self.assertEqual(set(json.loads(self.controller.path.read_text())["entries"][0]),
                         {"name", "added_at", "last_activity"})

    def test_loot_items_and_spell_names_do_not_refresh_or_alert_for_saved_players(self):
        self.save("Tooth", "Nice")
        before = {name: entry.last_activity for name, entry in self.controller._saved.items()}
        self.now += 1
        lines = (
            "--Shillelagh loots [Wolf Tooth) from a dismal cur's corpse.--",
            "You loot [Nice Tooth] from a dismal cur's corpse.",
            "Someone begins casting Nice Tooth.",
            "Someone's Nice Tooth hits YOU for 17 points of Fire Damage.",
        )
        for line in lines:
            with self.subTest(line=line):
                event = parse_line(line, self.now, "Hero")
                self.assertNotEqual(event.kind, "unknown")
                self.controller.on_message(SimpleNamespace(text=line), event)
        self.assertEqual(self.alerts, [])
        self.assertEqual({name: entry.last_activity for name, entry in self.controller._saved.items()}, before)
        attack = parse_line("Tooth's Flameburst hits YOU for 17 points of Fire Damage.", self.now, "Hero")
        self.assertEqual(self.controller.observe_saved(attack), ("Tooth",))

    def test_parsed_events_never_fall_back_to_text_when_participants_do_not_match(self):
        self.save("Tooth", "Nice")
        for event in (
            Event(self.now, "cast", "Tooth's spell lands.", actor="Someone", skill="Nice Tooth"),
            Event(self.now, "reward", "Tooth appears.", item="Tooth"),
            Event(self.now, "unknown", "Tooth's spell lands.", actor="Someone"),
        ):
            with self.subTest(kind=event.kind):
                self.assertEqual(self.controller.observe_saved(event), ())
        event = SimpleNamespace(kind="melee_hit", text="Someone hits YOU.", raw_target="nIcE")
        self.assertEqual(self.controller.observe_saved(event), ("Nice",))

    def test_unreadable_fragments_only_match_literal_leading_subjects(self):
        self.save("Tooth", "Nice")
        for line in ("Wolf Tooth", "You loot [Tooth]", "Someone casts Nice Tooth", "Tooth Fairy attacks.",
                     "Tooth of the wolf", "Nice work!", "Tooth_name attacks."):
            with self.subTest(line=line):
                self.assertEqual(self.controller.observe_saved(line), ())
        self.assertEqual(self.controller.observe_saved("  tOoTh’s spell lands."), ("Tooth",))
        self.assertEqual(self.controller.observe_saved("Nice attacks."), ("Nice",))

    def test_worker_sightings_are_delivered_on_gui_thread(self):
        class Feed(QObject):
            message = Signal(object, object)

        self.save("Bandit")
        feed = Feed()
        feed.message.connect(self.controller.on_message)
        threads = []
        self.controller.alert_requested.connect(lambda _name: threads.append(threading.get_ident()))
        event = self.event("Bandit attacks.", actor="Bandit")
        worker = threading.Thread(target=lambda: feed.message.emit(None, event))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.alerts, [])
        self.app.processEvents()
        self.assertEqual(self.alerts, ["Bandit"])
        self.assertEqual(threads, [threading.get_ident()])


class RevengeAlertConfigTests(unittest.TestCase):
    def test_defaults_and_preferences_round_trip(self):
        cfg = Config()
        self.assertFalse(cfg.revenge_enabled)
        self.assertEqual((cfg.revenge_alert_enabled, cfg.revenge_alert_flash, cfg.revenge_alert_sound,
                          cfg.revenge_alert_volume, cfg.revenge_alert_cooldown, cfg.revenge_alert_duration),
                         (True, True, "Dink", 25, 30.0, 4.0))
        changed = dataclasses.replace(cfg, revenge_alert_enabled=False, revenge_alert_flash=False,
                                      revenge_alert_sound="", revenge_alert_volume=12,
                                      revenge_alert_cooldown=90.5, revenge_alert_duration=8.5)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            save_config(changed, str(path))
            self.assertEqual(load_config(str(path)), changed)

    def test_bad_preferences_fall_back_to_defaults_and_validate(self):
        bad = {"revenge_alert_enabled": None, "revenge_alert_flash": [], "revenge_alert_sound": "unknown",
               "revenge_alert_volume": 101, "revenge_alert_cooldown": float("nan"), "revenge_alert_duration": 0}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(bad), encoding="utf-8")
            self.assertEqual(load_config(str(path)), Config())
        for field, value in bad.items():
            with self.subTest(field=field):
                self.assertTrue(any(field in issue for issue in dataclasses.replace(Config(), **{field: value}).problems()))
        for field in ("revenge_alert_cooldown", "revenge_alert_duration"):
            for value in (float("inf"), True, "10", 10 ** 400):
                with self.subTest(field=field, value=str(value)[:25]):
                    self.assertTrue(any(field in issue for issue in dataclasses.replace(Config(), **{field: value}).problems()))


class RevengeNotificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.owner = QWidget()
        self.owner._cfg = Config()
        self.owner.locked = True
        self.owner.click_through = False
        self.owner.opacity = 0.85
        self.owner.resize(600, 100)
        self.owner.panels_changed = Mock()
        self.owner.dock_anchor = lambda _panel: self.owner.frameGeometry()
        settings = QSettings(str(Path(self.tmp.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.panel = TimerPanel(self.owner, settings, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint)
        self.runner = TriggerRunner(TriggerStore(Path(self.tmp.name) / "triggers.json"))
        self.runner.audio.run = Mock()
        self.panel.set_runner(self.runner)

    def tearDown(self):
        self.panel.close()
        self.owner.close()
        self.runner._clock.stop()
        self.runner.audio.stop()
        self.panel.deleteLater()
        self.owner.deleteLater()
        self.runner.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def test_ephemeral_alert_uses_normal_popup_without_timer_or_store_mutation(self):
        trigger = Trigger(name="Revenge", timer_label="Revenge: {player}", action="sound", sound="Dink", volume=25)
        match = Match(trigger, "", "Bandit", {"player": "Bandit"}, notification_flash=True)
        events = []
        self.runner.fired.connect(events.append)
        with patch("mnmparse.app.timer_panel.time.monotonic", return_value=100):
            self.runner.fire(match)
        popup = self.panel._popups[0]
        self.assertEqual((popup.label, popup.seconds), ("Revenge: Bandit", 4))
        self.assertEqual(self.runner.board.timers, [])
        self.assertEqual(self.runner.store.triggers, [])
        self.assertEqual(events, [match])
        self.runner.audio.run.assert_called_once_with("sound", sound="Dink", file="", speech="Bandit", volume=25, scope="")
        self.assertEqual(popup.opacity(103.5), 0.5)
        self.assertEqual(popup.opacity(104), 0)
        with patch("mnmparse.app.timer_panel.time.monotonic", return_value=104):
            self.assertTrue(self.panel._prune_popups())
        self.assertEqual(self.panel._popups, [])

    def test_custom_duration_fades_at_end_and_flash_is_one_subtle_pulse(self):
        popup = TriggerPopup("Revenge", 100, "#ffffff", seconds=8, flash=True)
        self.assertEqual(popup.opacity(106), 1)
        self.assertEqual(popup.opacity(107.5), 0.5)
        self.assertAlmostEqual(popup.flash_strength(100.35), 1)
        for stamp in (100, 100.7, 101, 108):
            self.assertEqual(popup.flash_strength(stamp), 0)
        match = Match(Trigger(name="Revenge", action="none"), "", "", notification_seconds=8)
        with patch("mnmparse.app.timer_panel.time.monotonic", return_value=100):
            self.runner.fire(match)
        with patch("mnmparse.app.timer_panel.time.monotonic", return_value=107.5):
            self.assertFalse(self.panel._prune_popups())
        with patch("mnmparse.app.timer_panel.time.monotonic", return_value=108):
            self.assertTrue(self.panel._prune_popups())

    def test_reduced_motion_suppresses_flash_and_bad_duration_uses_normal_lifetime(self):
        self.owner._cfg = Config(reduced_motion=True)
        match = Match(Trigger(name="Revenge", action="none"), "", "", notification_flash=True,
                      notification_seconds=float("nan"))
        self.runner.fire(match)
        popup = self.panel._popups[-1]
        self.assertFalse(popup.flash)
        self.assertEqual(popup.seconds, 4)
        self.assertEqual(popup.flash_strength(popup.created_at + 0.35), 0)

    def test_manual_name_placeholder_text_is_literal(self):
        trigger = Trigger(name="Revenge", timer_label="Revenge: {player}", action="none")
        self.runner.fire(Match(trigger, "", "{match}", {"player": "{match}"}))
        self.assertEqual(self.panel._popups[-1].label, "Revenge: {match}")

    def test_flash_notification_paints_at_normal_and_peak_intensity(self):
        match = Match(Trigger(name="Revenge", action="none"), "", "", notification_flash=True)
        with patch("mnmparse.app.morality.reduced_motion", return_value=False), \
                patch("mnmparse.app.timer_panel.time.monotonic", return_value=100):
            self.runner.fire(match)
        self.assertTrue(self.panel._popups[-1].flash)
        for stamp in (100, 100.35, 101):
            with patch("mnmparse.app.timer_panel.time.monotonic", return_value=stamp):
                self.assertFalse(self.panel.grab().isNull())


if __name__ == "__main__":
    unittest.main()
