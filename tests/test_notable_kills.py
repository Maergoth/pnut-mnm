"""Fresh captured group kills celebrate only current-zone notable NPCs."""
from __future__ import annotations

import dataclasses
import math
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Signal
from PySide6.QtWidgets import QApplication

from mnmparse.app.engine import Engine
from mnmparse.app.notable_kills import MAX_PENDING, PENDING_SECONDS, NotableKillController
from mnmparse.app.profile_tools import settings_payload
from mnmparse.app.triggers_runtime import FANFARE_BPM, FANFARE_SCORE, SAMPLE_RATE, TriggerRunner, _victory_fanfare, builtin_sound_path
from mnmparse.config import Config, load_config, save_config
from mnmparse.npc_wiki import NotableNpcs, NpcDetails
from mnmparse.parser import parse_line
from mnmparse.session import SessionStats
from mnmparse.stats import Stats
from mnmparse.tracker import Message
from mnmparse.triggers import BUILTIN_SOUNDS, TriggerStore
from tests import test_map_lock_wiring as wiring

ZONE = "Dreadlands"
NPC = "Dreadfang"
PLAYER = "SampleHero"


class _Details(QObject):
    notables_updated = Signal(str, object)

    def __init__(self):
        super().__init__()
        self.record = NotableNpcs(ZONE, (NPC, "a rare wolf"))
        self.repo = SimpleNamespace(cached_notables=lambda zone: self.record)
        self.panel = SimpleNamespace(enabled=True)
        self.show_npc = Mock()
        self.ensure_notables = Mock()


class NotableKillControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.details = _Details()
        self.zone, self.active = ZONE, True
        self.controller = NotableKillController(self.details, current_zone=lambda: self.zone, active=lambda: self.active)
        self.celebrated = []
        self.controller.celebrated.connect(self.celebrated.append)

    def tearDown(self):
        self.controller.shutdown()
        self.controller.deleteLater()
        self.details.deleteLater()
        self.app.processEvents()

    def kill(self, text=f"You have slain {NPC}!", ts=100, zone=ZONE):
        event = parse_line(text, ts, PLAYER)
        self.assertEqual(event.kind, "kill")
        self.controller.on_kill(event, zone)
        return event

    def test_current_zone_cache_is_authoritative_and_article_variants_are_unambiguous(self):
        self.kill()
        self.kill("You have slain the rare wolf!", 110)
        self.assertEqual(self.celebrated, [NPC, "a rare wolf"])
        self.details.show_npc.assert_called_with("a rare wolf")
        self.details.record = NotableNpcs(ZONE, ("a rare wolf", "the rare wolf"))
        self.assertEqual(self.controller._canonical("rare wolf", self.details.record, ZONE), "")
        self.assertEqual(len(self.celebrated), 2)
        self.kill("You have slain a rare wolf!", 121)
        self.assertEqual(len(self.celebrated), 3)

    def test_unknown_zone_other_zone_browsing_and_nonnotable_names_cannot_match(self):
        self.kill("You have slain SamplePlayer!")
        self.kill(zone="Night Harbor")
        self.details.record = NotableNpcs("Night Harbor", (NPC,))
        self.kill(ts=102)
        self.zone = ""
        self.kill(ts=103, zone="")
        self.assertEqual(self.celebrated, [])

    def test_browsing_another_map_does_not_change_the_live_hunting_zone(self):
        self.details._zone = "Night Harbor"  # A separate map/browser selection.
        self.controller.set_zone(ZONE)
        self.kill()
        self.assertEqual(self.celebrated, [NPC])
        self.details.notables_updated.emit("Night Harbor", NotableNpcs("Night Harbor", ("Other Keeper",)))
        self.kill(ts=110)
        self.assertEqual(self.celebrated, [NPC, NPC])

    def test_complementary_slay_death_pair_and_exact_redelivery_celebrate_once(self):
        first = self.kill()
        self.kill(f"{NPC} has been slain by {PLAYER}!", 100.1)
        self.controller.on_kill(first, ZONE)
        self.assertEqual(self.celebrated, [NPC])
        self.kill(ts=110)
        self.assertEqual(self.celebrated, [NPC, NPC])

    def test_same_name_mobs_and_fresh_damage_do_not_lose_legitimate_kills(self):
        self.kill(ts=100)
        self.kill(ts=100)  # Two real rows can have identical text and capture timestamps.
        self.assertEqual(self.celebrated, [NPC, NPC])
        self.kill(f"{NPC} has been slain by {PLAYER}!", 100.2)
        self.assertEqual(len(self.celebrated), 2)
        self.controller.on_activity(parse_line(f"You crush {NPC} for 10 points of damage.", 101, PLAYER), ZONE)
        self.kill(f"{NPC} has been slain by {PLAYER}!", 101.1)
        self.assertEqual(len(self.celebrated), 3)

    def test_cold_lookup_is_bounded_and_delivers_fresh_pending_kills_when_ready(self):
        self.details.record = None
        with patch("mnmparse.app.notable_kills.time.monotonic", return_value=1000):
            for index in range(MAX_PENDING + 3):
                self.kill(ts=100 + index * 3)
            self.assertEqual(len(self.controller._pending), MAX_PENDING)
            self.details.record = NotableNpcs(ZONE, (NPC,))
            self.details.notables_updated.emit(ZONE, self.details.record)
        self.assertEqual(len(self.celebrated), MAX_PENDING)
        self.assertEqual(self.controller._pending, [])

    def test_zone_changes_capture_pause_and_old_pending_results_never_open_a_card(self):
        for mode in ("zone", "pause", "expired"):
            with self.subTest(mode=mode):
                self.details.record = None
                self.zone, self.active = ZONE, True
                self.controller.set_zone(ZONE)
                with patch("mnmparse.app.notable_kills.time.monotonic", return_value=1000):
                    self.kill(ts=100 + len(self.controller._seen))
                if mode == "zone":
                    self.zone = "Night Harbor"
                    self.controller.set_zone(self.zone)
                elif mode == "pause":
                    self.active = False
                    self.controller.on_state("paused")
                self.details.record = NotableNpcs(ZONE, (NPC,))
                with patch("mnmparse.app.notable_kills.time.monotonic", return_value=1000 + PENDING_SECONDS + 1):
                    self.details.notables_updated.emit(ZONE, self.details.record)
        self.assertEqual(self.celebrated, [])

    def test_display_preference_and_shutdown_do_not_force_open_a_disabled_card(self):
        self.details.panel.enabled = False
        self.kill()
        self.assertEqual(self.celebrated, [NPC])
        self.details.show_npc.assert_not_called()
        self.controller.shutdown()
        self.kill(ts=200)
        self.assertEqual(self.celebrated, [NPC])


class LiveKillEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config(player_name=PLAYER, log_dir=self.temp.name, start_capture_on_launch=False)
        with patch("mnmparse.app.engine._load_vocab_once", lambda cfg: None):
            self.engine = Engine(self.cfg)
        self.engine._state = "running"
        self.engine._stats = Stats(player_name=PLAYER)
        self.engine._stats.zone_changes.append((0, ZONE))
        self.engine._session_stats = SessionStats(PLAYER)
        self.engine._writer = Mock()
        self.kills, self.activity = [], []
        self.engine.live_group_kill.connect(lambda event, zone: self.kills.append((event, zone)))
        self.engine.live_group_activity.connect(lambda event, zone: self.activity.append((event, zone)))

    def tearDown(self):
        self.engine._writer = None
        self.engine.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def deliver(self, text, ts=100, **flags):
        self.engine._handle_message(Message(text, ts, 2, **flags), self.cfg)

    def test_viewer_explicit_party_and_owned_pet_kills_emit_captured_zone(self):
        self.deliver(f"You have slain {NPC}!")
        self.deliver(f"Your party member SampleFriend has slain {NPC}!", 101)
        self.engine._stats.roster.set_pet_owner("Sprout", PLAYER)
        self.deliver(f"{NPC} has been slain by Sprout!", 102)
        self.deliver(f"You crush {NPC} for 10 points of damage.", 103)
        self.assertEqual(len(self.kills), 3)
        self.assertTrue(all(zone == ZONE and event.target == NPC for event, zone in self.kills))
        self.assertEqual(len(self.activity), 1)

    def test_outsiders_group_exclusions_and_player_or_pet_victims_are_rejected(self):
        self.engine._stats.roster.set_manual("SampleFriend", False)
        self.engine._stats.roster.set_pet_owner("Sprout", PLAYER)
        for index, text in enumerate((f"{NPC} has been slain by SampleStranger!",
                                     f"Your party member SampleFriend has slain {NPC}!",
                                     f"You have been slain by {NPC}!",
                                     f"Your party member SampleFriend has been slain by {NPC}!",
                                     "You have slain Sprout!")):
            self.deliver(text, 100 + index)
        self.assertEqual(self.kills, [])

    def test_backlog_estimated_fragment_paused_and_unknown_zone_have_no_live_action(self):
        text = f"You have slain {NPC}!"
        for flag in ("backlog", "estimated_ts", "fragment"):
            self.deliver(text, 100, **{flag: True})
        for state in ("paused", "no_window", "stopped"):
            self.engine._state = state
            self.deliver(text, 101)
        self.engine._state = "running"
        self.engine._stats.zone_changes.clear()
        self.deliver(text, 102)
        self.assertEqual(self.kills, [])

    def test_pause_resume_during_ocr_rejects_old_kill_but_fresh_desktop_capture_celebrates(self):
        import numpy as np
        from mnmparse.capture import MssRegionSource

        self.cfg = dataclasses.replace(self.cfg, crop=(0, 0, 8, 8), capture_backend="mss",
                                       player_status_enabled=False)
        self.engine.update_config(self.cfg)
        self.engine._source = object.__new__(MssRegionSource)
        self.engine._ocr = Mock()
        tracker = self.engine._tracker = Mock()
        tracker.prev, tracker.scrolled_back, tracker._state = [], False, "live"
        tracker.update.return_value = [Message(f"You have slain {NPC}!", 100, 2)]

        def pause_during_ocr(_image):
            self.engine.set_paused(True)
            self.engine.set_paused(False)
            return []

        self.engine._ocr.read.side_effect = pause_during_ocr
        frame = np.zeros((8, 8, 3), dtype=np.uint8)
        try:
            self.engine._process_frame(frame, self.cfg)
            self.assertEqual(self.kills, [])
            self.engine._ocr.read.side_effect = None
            self.engine._ocr.read.return_value = []
            tracker.update.return_value = [Message(f"You have slain {NPC}!", 101, 2),
                                           Message(f"You crush {NPC} for 10 points of damage.", 102, 2)]
            self.engine._process_frame(frame, self.cfg)
            self.assertEqual(len(self.kills), 1)
            self.assertEqual(len(self.activity), 1)
            self.assertFalse(self.engine._leaderboard_capture_eligible())
        finally:
            self.engine._source = None  # This fixture never opens a native capture session.

    def test_old_reader_configuration_and_source_cannot_emit_live_group_events(self):
        old_cfg = self.cfg
        self.engine.update_config(dataclasses.replace(old_cfg, crop=(0, 0, 8, 8)))
        self.deliver(f"You have slain {NPC}!")
        self.assertEqual(self.kills, [])

        self.cfg = self.engine.config
        self.engine._leaderboard_frame_context = (
            self.engine._player_reader_epoch, object(), self.engine._leaderboard_reader_key(self.cfg))
        self.deliver(f"You have slain {NPC}!", 101)
        self.deliver(f"You crush {NPC} for 10 points of damage.", 102)
        self.assertEqual(self.kills, [])
        self.assertEqual(self.activity, [])
        self.engine._leaderboard_frame_context = None
        self.deliver(f"You have slain {NPC}!", 103)
        self.assertEqual(len(self.kills), 1)

    def test_player_consider_identity_excludes_a_same_named_notable_victim(self):
        self.deliver(f"{NPC} Battle with them would be risky.")
        self.deliver(f"You have slain {NPC}!", 101)
        self.assertEqual(self.kills, [])
        self.deliver(f"{NPC} considers you with kindness -- Battle with them would be risky.", 102)
        self.deliver(f"You have slain {NPC}!", 103)
        self.assertEqual(len(self.kills), 1)

    def test_queued_kill_is_rejected_if_captured_zone_is_no_longer_current(self):
        details = _Details()
        controller = NotableKillController(details, current_zone=lambda: self.engine.current_zone,
                                          active=lambda: self.engine.state == "running")
        self.engine.live_group_kill.connect(controller.on_kill)
        celebrated = []
        controller.celebrated.connect(celebrated.append)
        worker = threading.Thread(target=lambda: self.deliver(f"You have slain {NPC}!"))
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.engine._stats.zone_changes.append((101, "Night Harbor"))
        self.app.processEvents()
        self.assertEqual(celebrated, [])
        controller.shutdown()
        controller.deleteLater()
        details.deleteLater()


class VictoryFanfareTests(unittest.TestCase):
    def test_supplied_fragment_is_generated_at_130_bpm_with_audible_unclipped_samples(self):
        self.assertIn("Victory fanfare", BUILTIN_SOUNDS)
        self.assertEqual(FANFARE_BPM, 130)
        self.assertEqual(tuple(note for note, _beats in FANFARE_SCORE), (74, 74, 74, 74, 70, 72, 74, None, 72, 74))
        self.assertEqual(sum(beats for _note, beats in FANFARE_SCORE), 8)
        samples = _victory_fanfare()
        self.assertAlmostEqual(len(samples) / SAMPLE_RATE, 8 * 60 / 130, places=3)
        self.assertTrue(all(math.isfinite(value) for value in samples))
        self.assertLess(max(abs(value) for value in samples), 1)
        self.assertGreater(max(abs(value) for value in samples), .1)
        with tempfile.TemporaryDirectory() as folder, patch("mnmparse.app.triggers_runtime.project_path", return_value=Path(folder)):
            path = builtin_sound_path("Victory fanfare")
            with wave.open(str(path), "rb") as audio:
                self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, SAMPLE_RATE))
                self.assertEqual(audio.getnframes(), len(samples))
            self.assertEqual(builtin_sound_path("Victory fanfare"), path)


class NotableKillWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wiring.MapLockWiringTests.setUpClass()
        cls.app = QApplication.instance()

    def setUp(self):
        self.fixture = wiring.MapLockWiringTests()
        self.fixture.setUp()
        self.request_patch = patch("mnmparse.app.npc_details.NpcDetailsController._request")
        self.request = self.request_patch.start()
        self.runner = TriggerRunner(TriggerStore(Path(self.fixture.temp.name) / "triggers.json"))
        self.runner.audio = Mock()
        self.runner.audio.voices.return_value = []
        self.runner.audio.devices.return_value = []
        self.host = self.fixture.bootstrap(Config(player_name=PLAYER, setup_complete=True), trigger_runner=self.runner)
        self.host.engine.state = "running"
        self.host.engine.current_zone = ZONE
        self.host.npc_details.repo.cached_notables = Mock(return_value=NotableNpcs(ZONE, (NPC,)))
        self.host.npc_details.repo.cached_npc = Mock(return_value=NpcDetails(NPC, unique_drops=("Sample staff",)))

    def tearDown(self):
        self.host.notable_kills.shutdown()
        self.host.npc_details.shutdown()
        self.request_patch.stop()
        self.fixture.tearDown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def kill(self, ts=100):
        self.host.engine.live_group_kill.emit(parse_line(f"You have slain {NPC}!", ts, PLAYER), ZONE)
        self.app.processEvents()

    def test_live_signal_opens_cached_card_and_uses_existing_audio_without_firing_triggers(self):
        self.assertIs(self.host.notable_kills.parent(), self.host.window)
        with patch.object(self.runner, "fire") as trigger:
            self.kill()
        self.assertEqual(self.host.overlay.npc_panel._name, NPC)
        self.assertEqual(self.host.overlay.npc_panel._details.unique_drops, ("Sample staff",))
        self.runner.audio.play_builtin.assert_called_once_with("Victory fanfare")
        trigger.assert_not_called()
        self.assertEqual(self.runner.board.timers, [])

    def test_sound_toggle_mute_and_display_preferences_remain_independent_and_persist(self):
        page = self.host.window.page("settings")
        self.assertTrue(page.npc_celebration_sound.isChecked())
        page.npc_celebration_sound.setChecked(False)
        self.kill()
        self.runner.audio.play_builtin.assert_not_called()
        self.assertEqual(self.host.overlay.npc_panel._name, NPC)
        self.assertFalse(settings_payload(self.fixture.settings)["values"]["npc/celebration_sound"])
        page.npc_celebration_sound.setChecked(True)
        self.runner.set_muted(True)
        self.kill(110)
        self.runner.audio.play_builtin.assert_not_called()
        self.runner.set_muted(False)
        page.npc_enabled.setChecked(False)
        with patch.object(self.host.npc_details, "show_npc") as show:
            self.kill(120)
            show.assert_not_called()
        self.runner.audio.play_builtin.assert_called_once_with("Victory fanfare")
        cfg = dataclasses.replace(page.form_config(), npc_celebration_sound=False)
        path = Path(self.fixture.temp.name) / "saved.json"
        save_config(cfg, str(path))
        self.assertFalse(load_config(str(path)).npc_celebration_sound)

    def test_settings_preview_plays_fanfare_while_disabled_without_a_kill_or_preference_change(self):
        page = self.host.window.page("settings")
        page.npc_celebration_sound.setChecked(False)
        before = page.form_config()
        self.runner.store.volume = 37
        self.runner.store.output_device = "Sample output"
        self.runner._apply_audio_settings()
        self.runner.audio.configure.assert_called_with(volume=37, voice="", rate=0.0, device="Sample output")
        with patch.object(self.host.npc_details, "show_npc") as show, patch.object(self.runner, "fire") as fire:
            page.npc_celebration_preview.click()
        self.runner.audio.play_builtin.assert_called_once_with("Victory fanfare")
        show.assert_not_called()
        fire.assert_not_called()
        self.assertEqual(self.runner.board.timers, [])
        self.assertTrue(page.npc_celebration_preview.isEnabled())
        self.assertIn("victory fanfare", page.npc_celebration_preview.accessibleName())
        self.assertEqual(page.form_config(), before)
        self.assertFalse(self.fixture.settings.value("npc/celebration_sound", True, type=bool))
        path = Path(self.fixture.temp.name) / "preview-options.json"
        save_config(page.form_config(), str(path))
        self.assertFalse(load_config(str(path)).npc_celebration_sound)

    def test_settings_preview_uses_existing_manual_preview_mute_convention(self):
        page = self.host.window.page("settings")
        page.npc_celebration_sound.setChecked(False)
        self.runner.set_muted(True)
        self.runner.audio.configure.assert_called_with(volume=0, voice="", rate=0.0, device="")
        page.npc_celebration_preview.click()
        self.runner.audio.play_builtin.assert_called_once_with("Victory fanfare")
        self.assertTrue(self.runner.muted)
        self.assertFalse(page.npc_celebration_sound.isChecked())

    def test_demo_history_import_and_late_stopped_signals_do_not_celebrate(self):
        event = parse_line(f"You have slain {NPC}!", 100, PLAYER)
        self.host.engine.message.emit(Message(event.text, 100, 2), event)
        self.runner.audio.play_builtin.assert_not_called()
        self.host._demo_active = True
        self.kill(101)
        self.host._demo_active = False
        self.host.engine.state = "stopped"
        self.kill(102)
        self.runner.audio.play_builtin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
