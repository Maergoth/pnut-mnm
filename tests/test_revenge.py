"""PvP prompts require opt-in/full mode, expire once, and persist only on +."""
from __future__ import annotations

import json
from datetime import datetime
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from mnmparse.app.revenge import RevengeController, RevengeEntry, RevengeList, RevengePopoutWindow, RevengePrompts, plausible_attacker
from mnmparse.grammar import Event


def settings(*, enabled=True, casual=False, days=30, entries=100):
    return SimpleNamespace(player_name="Hero", revenge_enabled=enabled,
                           casual_mode=casual, casual_mode_confirmed=not casual,
                           revenge_days=days, revenge_entries=entries)


class RevengeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "revenge.json"
        self.now = 100.0
        self.controller = RevengeController(settings(), self.path, clock=lambda: self.now)
        self.widgets = []

    def tearDown(self):
        self.controller._timer.stop()
        self.controller._save_timer.stop()
        self.controller._activity_refresh.stop()
        self.controller._display_timer.stop()
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.controller.deleteLater()
        self.app.processEvents()
        self.tmp.cleanup()

    def attack(self, name="Bandit", *, target="Hero", kind="melee_hit"):
        return Event(self.now, kind, "Raw source must not be displayed", actor=name, target=target, amount=17)

    def test_candidate_names_are_ascii_single_words_and_not_self_or_articles(self):
        for name in ("A", "An", "The", "a", "Hero", "hero", "You", "yourself", "a goblin",
                     "Bad-Name", "D4rk", "Éowyn", "Player_", "The Goblin", ""):
            with self.subTest(name=name):
                self.assertFalse(plausible_attacker(name, "Hero"))
                self.assertFalse(self.controller.observe(self.attack(name)))
        self.assertTrue(self.controller.observe(self.attack("NamedNPC")), "Recognizing named NPCs is intentionally left to the user")

    def test_requires_incoming_attack_on_self_not_heal_or_outgoing_damage(self):
        self.assertFalse(self.controller.observe(self.attack(target="Teammate")))
        self.assertFalse(self.controller.observe(self.attack(kind="heal")))
        self.assertFalse(self.controller.observe(self.attack("Hero", target="Bandit")))
        self.assertTrue(self.controller.observe(self.attack(target="You")))
        self.assertFalse(self.path.exists(), "Observing an attack must not persist a name")

    def test_default_disabled_and_unconfirmed_full_mode_are_hidden(self):
        self.controller.set_config(SimpleNamespace(player_name="Hero"))
        self.assertFalse(self.controller.observe(self.attack()))
        self.assertEqual(self.controller.pending_candidates, ())
        self.controller.set_config(SimpleNamespace(player_name="Hero", revenge_enabled=True,
                                                   casual_mode=False, casual_mode_confirmed=False))
        self.assertFalse(self.controller.observe(self.attack()))
        self.assertEqual(self.controller.pending_candidates, ())

    def test_once_per_attacker_per_session_with_thirty_second_expiry(self):
        self.assertTrue(self.controller.observe(self.attack()))
        self.assertFalse(self.controller.observe(self.attack("bAnDiT")))
        self.now = 129.9
        self.controller.expire()
        self.assertEqual(len(self.controller.pending_candidates), 1)
        self.now = 130
        self.controller.expire()
        self.assertEqual(self.controller.pending_candidates, ())
        self.assertFalse(self.controller.observe(self.attack()), "Expired named NPCs must not nag again this session")
        self.controller.new_session()
        self.assertTrue(self.controller.observe(self.attack()))

    def test_plus_persists_across_sessions_and_remove_is_atomic(self):
        self.controller.observe(self.attack())
        self.assertTrue(self.controller.save_attacker("Bandit"))
        self.assertEqual(self.controller.pending_candidates, ())
        self.assertEqual(json.loads(self.path.read_text()), {"schema": 2, "entries": [
            {"name": "Bandit", "added_at": 100, "last_activity": 100}]})
        self.controller.new_session()
        self.assertEqual(self.controller.saved_names, ("Bandit",))
        self.assertFalse(self.controller.observe(self.attack()))
        reloaded = RevengeController(settings(), self.path, clock=lambda: self.now)
        self.assertEqual(reloaded.saved_names, ("Bandit",))
        reloaded._timer.stop()
        reloaded.deleteLater()
        self.assertTrue(self.controller.remove_saved("bandit"))
        self.assertEqual(json.loads(self.path.read_text())["entries"], [])

    def test_failed_save_keeps_previous_file_and_candidate_for_retry(self):
        self.controller.observe(self.attack("First"))
        self.controller.save_attacker("First")
        before = self.path.read_bytes()
        self.controller.observe(self.attack("Second"))
        with patch("mnmparse.app.revenge.atomic_json", side_effect=PermissionError("Locked")):
            self.assertFalse(self.controller.save_attacker("Second"))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.controller.saved_names, ("First",))
        self.assertEqual([c.name for c in self.controller.pending_candidates], ["Second"])
        self.assertTrue(self.controller.save_attacker("Second"))

    def test_switching_casual_clears_pending_and_never_resurfaces_old_candidates(self):
        self.controller.observe(self.attack("First"))
        self.controller.save_attacker("First")
        self.controller.observe(self.attack("Second"))
        self.controller.set_config(settings(casual=True))
        self.assertEqual(self.controller.pending_candidates, ())
        self.assertEqual(self.controller.saved_names, ())
        self.assertFalse(self.controller.observe(self.attack("Third")))
        self.controller.set_config(settings())
        self.assertEqual(self.controller.saved_names, ("First",))
        self.assertFalse(self.controller.observe(self.attack("Second")))
        self.assertFalse(self.controller.observe(self.attack("Third")))
        self.assertTrue(self.controller.observe(self.attack("Fourth")))

    def test_widgets_plus_on_right_and_mode_change_removes_all_identity_text(self):
        prompts, saved = RevengePrompts(self.controller), RevengeList(self.controller)
        self.widgets.extend((prompts, saved))
        self.controller.observe(self.attack("Bandit"))
        plus = next(button for button in prompts.findChildren(QPushButton) if button.text() == "+")
        self.assertEqual(plus.accessibleName(), "Save Bandit to revenge list")
        plus.click()
        self.app.processEvents()
        self.assertEqual(self.controller.saved_names, ("Bandit",))
        self.controller.set_config(settings(casual=True))
        self.app.processEvents()
        self.app.sendPostedEvents(None, 0)
        self.assertEqual(prompts._rows, {})
        self.assertEqual(saved._rows, {})
        for widget in (prompts, saved):
            for label in widget.findChildren(QLabel):
                if not label.isHidden():
                    self.assertNotIn("Bandit", label.text())

    def test_raw_parser_path_is_supported_without_retaining_source(self):
        self.assertTrue(self.controller.observe("Bandit hits YOU for 12 points of damage."))
        candidate = self.controller.pending_candidates[0]
        self.assertEqual(candidate.name, "Bandit")
        self.assertFalse(hasattr(candidate, "text"))
        self.assertFalse(hasattr(candidate, "damage"))

    def test_worker_messages_are_queued_to_controller_gui_thread(self):
        from PySide6.QtCore import QObject, Signal

        class Feed(QObject):
            message = Signal(object, object)

        feed = Feed()
        feed.message.connect(self.controller.on_message)
        observed_threads = []
        gui_thread = threading.get_ident()
        self.controller._clock = lambda: (observed_threads.append(threading.get_ident()), self.now)[1]
        attack = self.attack()
        worker = threading.Thread(target=lambda: feed.message.emit(None, attack))
        worker.start()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller.pending_candidates, (), "Worker emission must not mutate the GUI-owned controller directly")
        self.assertFalse(self.controller._timer.isActive())
        self.app.processEvents()
        self.assertEqual([candidate.name for candidate in self.controller.pending_candidates], ["Bandit"])
        self.assertTrue(self.controller._timer.isActive())
        self.assertTrue(observed_threads)
        self.assertEqual(set(observed_threads), {gui_thread})

    def test_older_queued_attack_cannot_resurface_after_enabling_or_leaving_casual(self):
        from PySide6.QtCore import QObject, Signal

        class Feed(QObject):
            message = Signal(object, object)

        feed = Feed()
        feed.message.connect(self.controller.on_message)
        for hidden in (settings(casual=True), settings(enabled=False)):
            with self.subTest(hidden=hidden):
                self.now = 100
                self.controller.new_session()
                self.controller.set_config(hidden)
                attack = self.attack("Older")
                worker = threading.Thread(target=lambda: feed.message.emit(None, attack))
                worker.start()
                worker.join(1)
                self.assertFalse(worker.is_alive())
                self.now = 101
                self.controller.set_config(settings())
                self.app.processEvents()
                self.assertEqual(self.controller.pending_candidates, ())
                self.now = 102
                self.assertFalse(self.controller.observe(self.attack("Older")), "The hidden attack still counts for this session's once-per-attacker rule")
                self.controller.on_message(None, self.attack("Newer"))
                self.assertEqual([entry.name for entry in self.controller.pending_candidates], ["Newer"])

    def test_manual_entry_works_in_each_widget_without_pending_attack(self):
        first, second = RevengeList(self.controller), RevengeList(self.controller)
        self.widgets.extend((first, second))
        for widget, name in ((first, "Manualone"), (second, "Manualtwo")):
            widget.name_input.setText("   ")
            self.assertFalse(widget.add_button.isEnabled())
            widget.name_input.setText(name)
            self.assertTrue(widget.add_button.isEnabled())
            widget.add_button.click()
            self.assertEqual(widget.name_input.text(), "")
        self.assertEqual(self.controller.saved_names, ("Manualone", "Manualtwo"))
        self.controller.set_config(settings(casual=True))
        self.assertTrue(first.name_input.isHidden())
        self.assertFalse(first.add_button.isEnabled())
        self.assertFalse(self.controller.save_attacker("Manualthree"))

    def test_manual_names_accept_arbitrary_text_and_persist_in_both_schemas(self):
        names = ["a rat", "Hero", "A", "An", "The", "Éowyn 🦋", "D4rk-Name_", "<b>A&B</b>", "Line\nBreak"]
        for name in names:
            self.assertTrue(self.controller.save_attacker(name), name)
        before = self.path.read_bytes()
        self.controller._load()
        self.assertEqual(set(self.controller.saved_names), set(names))
        self.assertEqual(self.path.read_bytes(), before)
        self.path.write_text(json.dumps({"schema": 1, "names": names}, ensure_ascii=False), encoding="utf-8")
        self.controller._load()
        self.assertEqual(set(self.controller.saved_names), set(names))
        self.assertTrue(self.controller.save_attacker("  another arbitrary name  "))
        self.assertIn("another arbitrary name", self.controller.saved_names)
        self.assertEqual(set(self.controller.saved_names), {*names, "another arbitrary name"})

    def test_manual_field_keeps_native_length_and_markup_tooltips_are_literal_and_scrubbed(self):
        from html import escape
        from PySide6.QtCore import Qt
        widget = RevengeList(self.controller)
        self.widgets.append(widget)
        self.assertEqual(widget.name_input.maxLength(), 32767)
        name = "<b>A&B</b> 😢"
        widget.name_input.setText(name)
        self.assertTrue(widget.add_button.isEnabled())
        widget.add_button.click()
        row = widget._rows[name]
        label = next(label for label in row.findChildren(QLabel) if label.text() == name)
        self.assertEqual(label.textFormat(), Qt.TextFormat.PlainText)
        self.assertEqual(label.toolTip(), "<span>" + escape(name) + "</span>")
        widget.add_button.setFocus()
        self.controller.set_config(settings(casual=True))
        self.assertEqual(label.text(), "")
        self.assertEqual(label.toolTip(), "")

    def test_manual_add_remains_available_after_automatic_prompt_expiry(self):
        self.controller.observe(self.attack("Bandit"))
        self.now = 130
        self.assertFalse(self.controller.save_attacker("Bandit", from_prompt=True))
        self.assertEqual(self.controller.pending_candidates, ())
        self.assertTrue(self.controller.save_attacker("Bandit"))

    def test_storage_larger_than_old_256k_limit_loads_but_oversized_save_preserves_file(self):
        from mnmparse.app.revenge import MAX_STORAGE_BYTES
        self.controller.save_attacker("Original")
        before = self.path.read_bytes()
        # 90 unique native-length Unicode entries exceed 8 MB, while each name
        # individually remains allowed. Failed serialization cannot mutate disk.
        huge = {str(i): RevengeEntry(str(i) + "🦋" * 32000, 100, 100) for i in range(90)}
        with patch("mnmparse.app.revenge.atomic_json") as write:
            self.assertFalse(self.controller._persist(huge))
            write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.controller.saved_names, ("Original",))
        entries = [{"name": str(i) + "🦋" * 25000, "added_at": None, "last_activity": None} for i in range(4)]
        self.path.write_text(json.dumps({"schema": 2, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        self.assertGreater(self.path.stat().st_size, 256 * 1024)
        self.assertLess(self.path.stat().st_size, MAX_STORAGE_BYTES)
        self.controller._load()
        self.assertEqual(set(self.controller.saved_names), {entry["name"] for entry in entries})

    def test_compact_overlay_add_uses_focusable_dialog_and_scrubs_on_mode_change(self):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QLineEdit
        widget = RevengeList(self.controller, compact=True)
        self.widgets.append(widget)
        widget.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        widget.show()
        self.app.processEvents()
        self.assertTrue(widget.name_input.isHidden())
        widget.add_button.click()
        dialog = widget._manual_dialog
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        QTest.keyClicks(dialog.findChild(QLineEdit), "Manualplayer")
        dialog.accept()
        self.assertEqual(self.controller.saved_names, ("Manualplayer",))
        widget.add_button.click()
        pending = widget._manual_dialog
        pending.setTextValue("Unconfirmedplayer")
        self.controller.set_config(settings(casual=True))
        self.assertIsNone(widget._manual_dialog)
        self.assertEqual(pending.textValue(), "")
        self.assertNotIn("Unconfirmedplayer", self.path.read_text())

    def test_large_lists_scroll_and_disabling_hides_whole_widgets(self):
        from PySide6.QtCore import Qt
        self.controller._saved = {f"player{chr(65 + i // 26)}{chr(65 + i % 26)}".casefold():
                                  RevengeEntry(f"Player{chr(65 + i // 26)}{chr(65 + i % 26)}", None, None) for i in range(300)}
        widget, prompts = RevengeList(self.controller, compact=True), RevengePrompts(self.controller)
        self.widgets.extend((widget, prompts))
        widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        widget.resize(320, 240)
        widget.show()
        self.app.processEvents()
        self.assertLessEqual(widget.height(), 260)
        self.assertGreater(widget.rows_scroll.verticalScrollBar().maximum(), 0)
        self.controller.set_config(settings(enabled=False))
        self.assertTrue(widget.isHidden())
        self.assertTrue(prompts.isHidden())

    def test_legacy_names_remain_visible_with_unknown_dates_and_migrate_without_loss(self):
        self.path.write_text(json.dumps({"schema": 1, "names": ["Older", "Legacy"]}))
        self.controller._load()
        self.controller.set_config(settings(days=1, entries=0))
        self.assertEqual(set(self.controller.saved_names), {"Older", "Legacy"})
        self.assertTrue(all(entry.added_at is None and entry.last_activity is None
                            for entry in self.controller.saved_entries))
        self.now += 1
        self.controller.save_attacker("Newest")
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["schema"], 2)
        self.assertEqual({entry["name"] for entry in saved["entries"]}, {"Older", "Legacy", "Newest"})
        self.assertEqual(self.controller.saved_names[0], "Newest")
        self.assertEqual(next(entry for entry in saved["entries"] if entry["name"] == "Older")["last_activity"], None)

    def test_day_and_count_filters_only_change_display_and_all_restores_every_entry(self):
        self.now = 10 * 86400
        self.controller.save_attacker("Oldest")
        self.now += 5 * 86400
        self.controller.save_attacker("Middle")
        self.now += 5 * 86400
        self.controller.save_attacker("Newest")
        before = self.path.read_bytes()
        self.controller.set_config(settings(days=6, entries=1))
        self.assertEqual(self.controller.saved_names, ("Newest",))
        self.assertEqual(self.controller.saved_total, 3)
        self.controller.set_config(settings(days=6, entries=0))
        self.assertEqual(self.controller.saved_names, ("Newest", "Middle"))
        self.controller.set_config(settings(days=0, entries=0))
        self.assertEqual(self.controller.saved_names, ("Newest", "Middle", "Oldest"))
        self.assertEqual(self.path.read_bytes(), before, "Changing limits must never rewrite or delete stored entries")

    def test_partial_dates_use_newest_known_added_or_attack_time_for_sort_and_age(self):
        self.now = 20 * 86400
        entries = [
            {"name": "AddedOnly", "added_at": 19 * 86400, "last_activity": None},
            {"name": "AddedNewer", "added_at": 20 * 86400, "last_activity": 10 * 86400},
            {"name": "ExpiredAddedOnly", "added_at": 10 * 86400, "last_activity": None},
            {"name": "LegacyUnknown", "added_at": None, "last_activity": None}]
        self.path.write_text(json.dumps({"schema": 2, "entries": entries}))
        self.controller._load()
        self.controller.set_config(settings(days=2, entries=0))
        self.assertEqual(self.controller.saved_names, ("AddedNewer", "AddedOnly", "LegacyUnknown"))
        self.assertEqual(self.controller.saved_total, 4)
        widget = RevengeList(self.controller)
        self.widgets.append(widget)
        self.assertEqual(widget._dates["AddedNewer"].text(), datetime.fromtimestamp(20 * 86400).strftime("%b %d, %Y"))

    def test_saved_attacks_update_newest_activity_and_flush_one_coalesced_checkpoint(self):
        self.controller.save_attacker("First")
        self.now = 101
        self.controller.save_attacker("Second")
        before = self.path.read_bytes()
        from mnmparse.app.revenge import atomic_json
        with patch("mnmparse.app.revenge.atomic_json", wraps=atomic_json) as write:
            for now in range(102, 122):
                self.now = float(now)
                self.assertFalse(self.controller.observe(self.attack("First")))
            write.assert_not_called()
            self.assertEqual(self.controller.pending_candidates, ())
            self.assertEqual(self.controller.saved_names, ("First", "Second"))
            self.assertEqual(self.path.read_bytes(), before)
            self.assertTrue(self.controller._save_timer.isActive())
            self.assertTrue(self.controller.flush_pending_changes())
            write.assert_called_once()
        first = next(entry for entry in json.loads(self.path.read_text())["entries"] if entry["name"] == "First")
        self.assertEqual(first, {"name": "First", "added_at": 100, "last_activity": 121})
        self.assertFalse(self.controller._dirty)
        self.assertFalse(self.controller._save_timer.isActive())

    def test_failed_activity_flush_remains_retryable_and_restore_cancels_old_checkpoint(self):
        self.controller.save_attacker("Saved")
        self.now = 110
        self.controller.observe(self.attack("Saved"))
        before = self.path.read_bytes()
        with patch("mnmparse.app.revenge.atomic_json", side_effect=PermissionError("Locked")):
            self.assertFalse(self.controller.flush_pending_changes())
        self.assertTrue(self.controller._dirty)
        self.assertEqual(self.path.read_bytes(), before)
        restored = {"schema": 2, "entries": [{"name": "Restored", "added_at": 50, "last_activity": 90}]}
        self.path.write_text(json.dumps(restored))
        self.controller._load()
        self.assertFalse(self.controller._dirty)
        self.assertFalse(self.controller._save_timer.isActive())
        self.assertFalse(self.controller._activity_refresh.isActive())
        self.assertEqual(self.controller.saved_names, ("Restored",))
        self.assertTrue(self.controller.flush_pending_changes())
        self.assertEqual(json.loads(self.path.read_text()), restored)

    def test_open_view_reorders_existing_rows_and_age_timer_refreshes_without_attacks(self):
        self.now = 10 * 86400
        self.controller.save_attacker("First")
        self.now += 1
        self.controller.save_attacker("Second")
        widget = RevengeList(self.controller)
        self.widgets.append(widget)
        widget.show()
        self.app.processEvents()
        self.assertIs(widget._row_layout.itemAt(0).widget(), widget._rows["Second"])
        self.now += 1
        self.controller.observe(self.attack("First"))
        self.controller._activity_refresh.timeout.emit()
        self.assertIs(widget._row_layout.itemAt(0).widget(), widget._rows["First"])
        self.assertIn("Last activity", widget._dates["First"].toolTip())
        self.controller.set_config(settings(days=1, entries=0))
        self.now += 2 * 86400
        self.assertTrue(self.controller._display_timer.isActive())
        self.controller._display_timer.timeout.emit()
        self.assertEqual(widget._rows, {})
        self.assertEqual(self.controller.saved_total, 2)
        self.controller.set_config(settings(days=0, entries=0))
        self.assertEqual(set(widget._rows), {"First", "Second"})

    def test_schema2_invalid_date_or_duplicate_preserves_existing_model_and_file(self):
        self.controller.save_attacker("Saved")
        for value in (True, -1, float("nan"), float("inf"), 253402300800):
            bad = {"schema": 2, "entries": [{"name": "Other", "added_at": value, "last_activity": 100}]}
            self.path.write_text(json.dumps(bad))
            before = self.path.read_bytes()
            self.controller._load()
            self.assertEqual(self.controller.saved_names, ("Saved",))
            self.assertEqual(self.path.read_bytes(), before)
        entry = {"name": "Same", "added_at": 100, "last_activity": 100}
        self.path.write_text(json.dumps({"schema": 2, "entries": [entry, {**entry, "name": "same"}]}))
        self.controller._load()
        self.assertEqual(self.controller.saved_names, ("Saved",))

    def test_popout_requests_share_one_controller_and_passive_window_scrubs_everywhere(self):
        from PySide6.QtCore import Qt
        first, second = RevengeList(self.controller), RevengeList(self.controller, compact=True)
        window = RevengePopoutWindow(self.controller)
        self.widgets.extend((first, second, window))
        requests = []
        self.controller.pop_out_requested.connect(lambda: requests.append(True))
        for widget in (first, second):
            widget.pop_out_button.click()
        self.assertEqual(requests, [True, True])
        self.assertTrue(window.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        with patch.object(window, "activateWindow", side_effect=AssertionError("Activated game overlay")), \
                patch.object(window, "raise_", side_effect=AssertionError("Raised game overlay")):
            window.show()
        self.controller.save_attacker("Bandit")
        self.assertEqual(set(window.list._rows), {"Bandit"})
        window.list.add_button.click()
        dialog = window.list._manual_dialog
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus)
        dialog.setTextValue("Unconfirmed")
        self.controller.set_config(settings(casual=True))
        self.app.processEvents()
        self.assertEqual(dialog.textValue(), "")
        self.assertIsNone(window.list._manual_dialog)
        for widget in (first, second, window):
            for label in widget.findChildren(QLabel):
                self.assertNotIn("Bandit", label.text())
                self.assertNotIn("Bandit", label.toolTip())
        first.pop_out_button.click()
        self.assertEqual(requests, [True, True])
        self.controller.set_config(settings(enabled=False))
        self.assertTrue(window.isHidden())
        self.assertTrue(first.isHidden())
        self.assertTrue(second.isHidden())


if __name__ == "__main__":
    unittest.main()
