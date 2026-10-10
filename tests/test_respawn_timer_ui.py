"""NPC context-menu targeting and zone defaults for one-time countdowns."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QSettings, QTimer, Qt
    from PySide6.QtGui import QContextMenuEvent
    from PySide6.QtWidgets import QApplication, QDialog, QMenu

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

from mnmparse.app.models import build_snapshot
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.stats import Stats
from mnmparse.triggers import Trigger, TriggerStore

if HAVE_QT:
    from mnmparse.app.overlay import OverlayWindow
    from mnmparse.app.respawn_timer_dialog import DEFAULT_RESPAWN_SECONDS, MAX_RESPAWN_SECONDS, RespawnTimerDialog
    from mnmparse.app.triggers_runtime import TriggerRunner


PLAYER = "Maergoth"
NAMED_MOB = "Dreadfang"
COMMON_MOB = "a skeletal warrior"
ZONE = "Dreadlands"
OTHER_ZONE = "Northern Crypts"
RESPAWN_ACTION = "Start respawn timer…"
ZONE_DURATIONS = "overlay/respawn_zone_durations"
LEGACY_MOB_DURATIONS = "overlay/respawn_durations"


def fight(*, multi: bool = False, zone: str = ZONE):
    stats = Stats(player_name=PLAYER)
    lines = [
        f"You crush {NAMED_MOB} for 40 points of damage.",
        f"{NAMED_MOB} bites {PLAYER} for 30 points of damage.",
    ]
    if multi:
        lines.extend([
            f"You crush {COMMON_MOB} for 20 points of damage.",
            f"{COMMON_MOB} slashes {PLAYER} for 10 points of damage.",
        ])
    for offset, line in enumerate(lines):
        stats.add(parse_line(line, 100 + offset, PLAYER))
    return replace(build_snapshot(stats, stats.current(), PLAYER, now=106), zone=zone)


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class RespawnTimerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings_path = str(Path(self.tmp.name) / "ui.ini")
        self.settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        store = TriggerStore(Path(self.tmp.name) / "triggers.json")
        store.triggers = [Trigger(name="Existing", pattern="existing")]
        self.runner = TriggerRunner(store)
        self.runner.set_casual_mode(False)  # This fixture exercises confirmed full-mode labels.
        self.runner.audio = Mock()
        self.windows = []
        self.overlay = self.make_overlay(self.settings)

    def tearDown(self) -> None:
        for window in self.windows:
            window.close()
            window.deleteLater()
        self.runner._clock.stop()
        self.runner.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def make_overlay(self, settings):
        overlay = OverlayWindow(settings, Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True))
        self.windows.append(overlay)
        overlay.set_trigger_runner(self.runner)
        overlay.setGeometry(50, 50, 800, 350)
        overlay.show()
        self.app.processEvents()
        return overlay

    def show_fight(self, *, multi=False, zone=ZONE):
        snap = fight(multi=multi, zone=zone)
        self.overlay.set_snapshot(snap)
        self.overlay._flush_snapshot()
        self.overlay.set_tab("overview")
        self.app.processEvents()
        self.overlay._header.grab()  # Ensure the painted label's hit rect is current.
        return snap

    def header_target(self):
        self.overlay._header.grab()
        return self.overlay._header, self.overlay._header._label_rect.center().toPoint()

    def row_target(self, name):
        meter = self.overlay.meter
        view = meter.view
        column = meter._model.column_index("name")
        for position in range(meter._proxy.rowCount()):
            index = meter._proxy.index(position, column)
            source = meter._proxy.mapToSource(index)
            if meter._model.row_object(source.row()).name == name:
                view.scrollTo(index)
                self.app.processEvents()
                return view.viewport(), view.visualRect(index).center()
        self.fail(f"No displayed row for {name}")

    def context_menu(self, widget, local_pos, *, choice=None, while_open=None):
        seen = []

        def choose(menu, _global_pos):
            actions = menu.actions()
            seen.append([action.text() for action in actions if not action.isSeparator()])
            respawn = next((action for action in actions if action.text() == RESPAWN_ACTION), None)
            if while_open is not None:
                while_open()
            if choice is None:
                return None
            self.assertIsNotNone(respawn)
            if respawn.menu() is not None:
                names = [action.text() for action in respawn.menu().actions()]
                seen.append(names)
                return next(action for action in respawn.menu().actions() if action.text() == choice)
            self.assertEqual(choice, RESPAWN_ACTION)
            return respawn

        class PickingMenu(QMenu):
            def exec(self, global_pos):
                return choose(self, global_pos)

        event = QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, local_pos, widget.mapToGlobal(local_pos),
        )
        # PySide's native QMenu.exec bypasses assigning that native class slot;
        # use a genuine QMenu subclass with its exec overridden instead.
        with patch("mnmparse.app.overlay.QMenu", PickingMenu):
            QApplication.sendEvent(widget, event)
        self.assertTrue(seen, "The child widget must propagate its context menu to the overlay")
        return seen

    def dialog_response(self, *, seconds=None, accept=True, inspect=None, while_open=None):
        def execute(dialog):
            if inspect is not None:
                inspect(dialog)
            if seconds is not None:
                dialog.minutes.setValue(seconds // 60)
                dialog.seconds.setValue(seconds % 60)
            def finish():
                try:
                    if while_open is not None:
                        while_open()
                finally:
                    (dialog.accept if accept else dialog.reject)()

            QTimer.singleShot(0, finish)
            return QDialog.exec(dialog)

        return patch.object(RespawnTimerDialog, "exec", execute)

    def test_named_enemy_header_propagates_and_starts_real_countdown(self):
        snap = self.show_fight()
        enemy = next(row for row in snap.rows if row.name == NAMED_MOB)
        self.assertTrue(enemy.is_enemy)
        self.assertFalse(enemy.is_npc, "Named mobs have enemy classification without an article")
        before = self.runner.store.to_dict()
        with self.dialog_response(seconds=754):
            self.context_menu(*self.header_target(), choice=RESPAWN_ACTION)
        timer, = self.runner.board.timers
        self.assertEqual((timer.label, timer.duration), (f"{NAMED_MOB} respawn", 754))
        self.assertTrue(timer.keep_until_dismissed)
        self.assertIn(timer, self.overlay.timer_panel.timers())
        self.assertEqual(self.runner.store.to_dict(), before)
        self.runner.audio.run.assert_not_called()

    def test_sorted_meter_viewport_targets_the_clicked_npc(self):
        self.show_fight(multi=True)
        self.overlay.meter.set_sort("name", False)
        self.app.processEvents()
        with self.dialog_response(seconds=90):
            self.context_menu(*self.row_target(COMMON_MOB), choice=RESPAWN_ACTION)
        timer, = self.runner.board.timers
        self.assertEqual(timer.label, f"{COMMON_MOB} respawn")

    def test_multi_mob_header_offers_separate_names(self):
        self.show_fight(multi=True)
        with self.dialog_response(seconds=127):
            menus = self.context_menu(*self.header_target(), choice=NAMED_MOB)
        self.assertEqual(menus[1], [COMMON_MOB, NAMED_MOB])
        self.assertEqual(self.runner.board.timers[0].label, f"{NAMED_MOB} respawn")

    def test_header_keeps_actual_mob_name_in_self_feed_and_session_views(self):
        self.show_fight()
        for mode, tab in (("self", "damage"), ("group", "feed"), ("self", "session")):
            with self.subTest(mode=mode, tab=tab):
                self.runner.clear_timers()
                self.overlay.set_view_mode(mode)
                self.overlay.set_tab(tab)
                self.app.processEvents()
                with self.dialog_response(seconds=120):
                    self.context_menu(*self.header_target(), choice=RESPAWN_ACTION)
                self.assertEqual(self.runner.board.timers[0].label, f"{NAMED_MOB} respawn")

    def test_zone_summary_header_never_starts_a_timer_for_the_zone_title(self):
        snap = self.show_fight(multi=True)
        summary = replace(snap, key="zone:Dreadlands|100", label="Dreadlands", encounters=2)
        self.overlay._snap = summary
        self.overlay._refresh_header()
        self.overlay._refresh_rows()
        self.app.processEvents()
        with patch.object(self.overlay, "start_respawn_timer") as start:
            menu, = self.context_menu(*self.header_target())
        self.assertNotIn(RESPAWN_ACTION, menu)
        start.assert_not_called()
        with self.dialog_response(seconds=30):
            self.context_menu(*self.row_target(NAMED_MOB), choice=RESPAWN_ACTION)
        self.assertEqual(self.runner.board.timers[0].label, f"{NAMED_MOB} respawn")

    def test_players_pets_and_empty_header_area_offer_no_respawn_timer(self):
        snap = self.show_fight()
        pet = replace(next(row for row in snap.rows if row.name == NAMED_MOB),
                      name="Fluffy", is_enemy=False, is_npc=True, is_pet=True, in_group=True)
        self.overlay._snap = replace(snap, rows=[*snap.rows, pet])
        self.overlay._refresh_rows()
        self.app.processEvents()
        for name in (PLAYER, "Fluffy"):
            with self.subTest(name=name):
                menu, = self.context_menu(*self.row_target(name))
                self.assertNotIn(RESPAWN_ACTION, menu)
        header, _ = self.header_target()
        menu, = self.context_menu(header, header.rect().bottomRight())
        self.assertNotIn(RESPAWN_ACTION, menu)

    def test_respawn_action_requires_a_timer_runner(self):
        self.show_fight()
        self.overlay.set_trigger_runner(None)
        menu, = self.context_menu(*self.header_target())
        self.assertNotIn(RESPAWN_ACTION, menu)

    def test_latest_duration_is_shared_by_mobs_in_one_zone_and_separate_between_zones(self):
        self.show_fight(multi=True)
        initial_defaults = []
        with self.dialog_response(seconds=754, inspect=lambda dialog: initial_defaults.append(dialog.duration())):
            self.overlay.start_respawn_timer(NAMED_MOB)
        with self.dialog_response(seconds=42, inspect=lambda dialog: initial_defaults.append(dialog.duration())):
            self.overlay.start_respawn_timer(COMMON_MOB)
        with self.dialog_response(seconds=91, inspect=lambda dialog: initial_defaults.append(dialog.duration())):
            self.overlay.start_respawn_timer(NAMED_MOB, zone=OTHER_ZONE)
        self.assertEqual(initial_defaults, [DEFAULT_RESPAWN_SECONDS, 754, DEFAULT_RESPAWN_SECONDS])
        self.assertEqual(json.loads(self.settings.value(ZONE_DURATIONS)),
                         {ZONE.casefold(): 42, OTHER_ZONE.casefold(): 91})

    def test_zone_defaults_survive_new_settings_overlay_and_case_spacing_changes(self):
        with self.dialog_response(seconds=754):
            self.overlay.start_respawn_timer(NAMED_MOB, zone=ZONE)
        with self.dialog_response(seconds=42):
            self.overlay.start_respawn_timer(NAMED_MOB, zone=OTHER_ZONE)
        settings = QSettings(self.settings_path, QSettings.Format.IniFormat)
        settings.sync()
        self.assertEqual(json.loads(settings.value(ZONE_DURATIONS)),
                         {ZONE.casefold(): 754, OTHER_ZONE.casefold(): 42})
        self.overlay = self.make_overlay(settings)
        restored = []
        self.show_fight(zone=OTHER_ZONE)
        with self.dialog_response(accept=False, inspect=lambda dialog: restored.append(dialog.duration())):
            self.overlay.start_respawn_timer(COMMON_MOB)
            self.overlay.start_respawn_timer("a wandering ghoul", zone="  DREADLANDS  ")
            self.overlay.start_respawn_timer(COMMON_MOB, zone=" NORTHERN   CRYPTS ")
        self.show_fight(zone=ZONE)
        with self.dialog_response(accept=False, inspect=lambda dialog: restored.append(dialog.duration())):
            self.overlay.start_respawn_timer(COMMON_MOB)
        self.assertEqual(restored, [42, 754, 42, 754])

    def test_menu_and_dialog_keep_the_selected_fights_zone_when_snapshot_changes(self):
        for target in ("header", "row"):
            with self.subTest(target=target):
                self.runner.clear_timers()
                self.settings.setValue(ZONE_DURATIONS, json.dumps({ZONE.casefold(): 754, OTHER_ZONE.casefold(): 42}))
                self.show_fight(multi=True, zone=ZONE)
                defaults = []

                with self.dialog_response(seconds=123, inspect=lambda dialog: defaults.append(dialog.duration()),
                                          while_open=lambda: self.show_fight(zone="Later Zone")):
                    widget, point = self.header_target() if target == "header" else self.row_target(COMMON_MOB)
                    self.context_menu(widget, point, choice=NAMED_MOB if target == "header" else RESPAWN_ACTION,
                                      while_open=lambda: self.show_fight(zone=OTHER_ZONE))
                self.assertEqual(defaults, [754])
                self.assertEqual(json.loads(self.settings.value(ZONE_DURATIONS)),
                                 {ZONE.casefold(): 123, OTHER_ZONE.casefold(): 42})
                timer, = self.runner.board.timers
                mob = NAMED_MOB if target == "header" else COMMON_MOB
                self.assertEqual((timer.label, timer.duration), (f"{mob} respawn", 123))

    def test_unknown_zones_always_use_the_default_without_remembering_a_duration(self):
        saved = json.dumps({ZONE.casefold(): 754})
        self.settings.setValue(ZONE_DURATIONS, saved)
        defaults = []
        for zone in (None, "", " \t "):
            with self.subTest(zone=zone):
                with self.dialog_response(seconds=42, inspect=lambda dialog: defaults.append(dialog.duration())):
                    self.overlay.start_respawn_timer(NAMED_MOB, zone=zone)
                self.assertEqual(self.settings.value(ZONE_DURATIONS), saved)
        self.show_fight(zone=" \t ")
        with self.dialog_response(seconds=91, inspect=lambda dialog: defaults.append(dialog.duration())):
            self.overlay.start_respawn_timer(COMMON_MOB)
        self.assertEqual(defaults, [DEFAULT_RESPAWN_SECONDS] * 4)
        self.assertEqual(self.settings.value(ZONE_DURATIONS), saved)
        self.assertEqual([timer.duration for timer in self.runner.board.timers], [42, 42, 42, 91])

    def test_legacy_mob_defaults_are_ignored_and_preserved(self):
        legacy = json.dumps({NAMED_MOB.casefold(): 754, ZONE.casefold(): 999})
        self.settings.setValue(LEGACY_MOB_DURATIONS, legacy)
        self.show_fight()
        defaults = []
        with self.dialog_response(seconds=42, inspect=lambda dialog: defaults.append(dialog.duration())):
            self.overlay.start_respawn_timer(NAMED_MOB)
        self.assertEqual(defaults, [DEFAULT_RESPAWN_SECONDS])
        self.assertEqual(self.settings.value(LEGACY_MOB_DURATIONS), legacy)
        self.assertEqual(json.loads(self.settings.value(ZONE_DURATIONS)), {ZONE.casefold(): 42})

    def test_invalid_saved_zone_values_are_ignored(self):
        self.show_fight()
        for value in (0, -1, MAX_RESPAWN_SECONDS + 1, True, 42.5, "754", None, [], {}):
            with self.subTest(value=value):
                saved = json.dumps({ZONE.casefold(): value})
                self.settings.setValue(ZONE_DURATIONS, saved)
                defaults = []
                with self.dialog_response(accept=False, inspect=lambda dialog: defaults.append(dialog.duration())):
                    self.overlay.start_respawn_timer(NAMED_MOB)
                self.assertEqual(defaults, [DEFAULT_RESPAWN_SECONDS])
                self.assertEqual(self.settings.value(ZONE_DURATIONS), saved)
        for saved in ("not JSON", "[]", "null", "754"):
            with self.subTest(saved=saved):
                self.settings.setValue(ZONE_DURATIONS, saved)
                defaults = []
                with self.dialog_response(accept=False, inspect=lambda dialog: defaults.append(dialog.duration())):
                    self.overlay.start_respawn_timer(NAMED_MOB)
                self.assertEqual(defaults, [DEFAULT_RESPAWN_SECONDS])
                self.assertEqual(self.settings.value(ZONE_DURATIONS), saved)
        self.assertEqual(self.runner.board.timers, [])

    def test_cancelled_edits_never_save_or_start(self):
        self.show_fight()
        with self.dialog_response(seconds=754):
            self.overlay.start_respawn_timer(NAMED_MOB)
        before = self.settings.value(ZONE_DURATIONS)
        previous = list(self.runner.board.timers)
        with self.dialog_response(seconds=5, accept=False):
            self.overlay.start_respawn_timer(NAMED_MOB)
            self.overlay.start_respawn_timer(COMMON_MOB)
            self.overlay.start_respawn_timer(NAMED_MOB, zone=OTHER_ZONE)
        self.assertEqual(self.settings.value(ZONE_DURATIONS), before)
        self.assertEqual(self.runner.board.timers, previous)

    def test_zero_duration_cannot_be_accepted_saved_or_started(self):
        self.show_fight()
        def execute(dialog):
            dialog.minutes.setValue(0)
            dialog.seconds.setValue(0)
            self.assertFalse(dialog.start_button.isEnabled())
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            QTimer.singleShot(0, dialog.reject)
            return QDialog.exec(dialog)

        with patch.object(RespawnTimerDialog, "exec", execute):
            self.overlay.start_respawn_timer(NAMED_MOB)
        self.assertEqual(self.runner.board.timers, [])
        self.assertFalse(self.settings.contains(ZONE_DURATIONS))

    def test_dialog_uses_focusable_normal_flags_and_minute_second_controls(self):
        dialog = RespawnTimerDialog(NAMED_MOB, 754, self.overlay)
        self.windows.append(dialog)
        flags = dialog.windowFlags()
        self.assertFalse(flags & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertFalse(flags & Qt.WindowType.WindowStaysOnTopHint)
        self.assertFalse(flags & Qt.WindowType.FramelessWindowHint)
        self.assertFalse(dialog.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual((dialog.minutes.value(), dialog.seconds.value(), dialog.duration()), (12, 34, 754))
        self.assertEqual((dialog.minutes.minimum(), dialog.minutes.maximum()), (0, 600))
        self.assertEqual((dialog.seconds.minimum(), dialog.seconds.maximum()), (0, 59))
        self.assertNotEqual(dialog.minutes.focusPolicy(), Qt.FocusPolicy.NoFocus)
        self.assertTrue(dialog.start_button.isEnabled())
        dialog.minutes.setValue(0)
        dialog.seconds.setValue(0)
        self.assertFalse(dialog.start_button.isEnabled())
        dialog.seconds.setValue(1)
        self.assertTrue(dialog.start_button.isEnabled())


if __name__ == "__main__":
    unittest.main()
