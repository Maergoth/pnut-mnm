"""History paging and corrections retain their original durable session."""
from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from mnmparse.app.pages import LivePage, SessionPage
from mnmparse.config import Config
from tests.test_app_widgets import _snapshot, _actor


class HistoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "test.ini"), QSettings.Format.IniFormat)
        self.cfg = Config(player_name="Hero", casual_mode=False, casual_mode_confirmed=True,
                          history_recent_fights=2)

    def tearDown(self):
        self.app.processEvents()
        self.temp.cleanup()

    def test_history_bounds_every_cache_and_retains_recent_keys(self):
        page = LivePage(Mock(), self.cfg, self.settings)
        try:
            for index in range(5):
                page.add_encounter(_snapshot(float(index * 60), [_actor("Hero", 10, is_you=True)]), "imported")
            page.refresh_if_stale()
            self.assertEqual(set(page._raw_snaps), {"imported:180.000", "imported:240.000"})
            self.assertEqual(set(page._sources), set(page._raw_snaps))
            self.assertEqual(len(page._intervals), 2)
            self.assertEqual(set(page._snaps), set(page._raw_snaps))
        finally:
            page.deleteLater()

    def test_selected_archive_correction_uses_uuid_and_updates_view(self):
        engine = Mock()
        page = LivePage(engine, self.cfg, self.settings)
        raw = _snapshot(100, [_actor("Hero", 10, is_you=True)])
        revised = dataclasses.replace(raw, total_damage=30)
        engine.correct_archived_encounter.return_value = revised
        corrected = []
        page.archive_corrected.connect(corrected.append)
        try:
            page.set_archive_history("uuid-import", [raw])
            self.assertTrue(page.select_key(raw.key))
            self.assertTrue(page.correct_selected_archive(name="Peer", in_group=True))
            engine.correct_archived_encounter.assert_called_once_with(
                raw.key, session_id="uuid-import", name="Peer", in_group=True)
            self.assertEqual(page._raw_snaps[raw.key].total_damage, 30)
            self.assertEqual(corrected, ["uuid-import"])
            active = dataclasses.replace(raw, total_damage=999, closed=False)
            page.set_snapshot(active)
            page.add_encounter(dataclasses.replace(active, closed=True))
            page.update_encounter(active)
            self.assertEqual(page._raw_snaps[raw.key].total_damage, 30)
            self.assertEqual(page._archive_sources[raw.key], "uuid-import")
            page.set_config(dataclasses.replace(self.cfg, casual_mode=True))
            self.assertFalse(page.correct_selected_archive(name="Peer", in_group=True))
        finally:
            page.deleteLater()

    def test_failed_session_reset_preserves_session_and_prompt_eligibility(self):
        class Engine:
            is_running = False
            def reset_session(self):
                return False
        page = SessionPage(Engine(), self.cfg, self.settings)
        resets = []
        page.new_session_requested.connect(lambda: resets.append(True))
        try:
            page._on_reset()
            self.assertEqual(resets, [])
            self.assertIn("remains active", page._status.text())
        finally:
            page.deleteLater()

    def test_archive_pages_have_bounded_queries_and_live_selection_restores_history(self):
        class Engine:
            is_running = False
            def archived_sessions(self):
                return [{"id": "uuid-import", "started": 100}]
            def archived_session(self, session_id):
                return None
            def history(self):
                return []
            archived_encounters = Mock(side_effect=lambda session_id, **kw: [
                _snapshot(300 - kw["offset"] * 60, [_actor("Hero", 10, is_you=True)])
                for _ in range(2 if kw["offset"] == 0 else 1)])
        engine = Engine()
        page = SessionPage(engine, self.cfg, self.settings)
        deliveries = []
        page.archive_selected.connect(lambda session_id, snaps: deliveries.append((session_id, len(snaps))))
        try:
            page._source.setCurrentIndex(1)
            engine.archived_encounters.assert_called_with("uuid-import", limit=2, offset=0)
            page._page_archive(1)
            engine.archived_encounters.assert_called_with("uuid-import", limit=2, offset=2)
            self.assertFalse(page._older.isEnabled())
            self.assertTrue(page._newer.isEnabled())
            self.assertEqual(deliveries[-1], ("uuid-import", 1))
            page._source.setCurrentIndex(0)
            self.assertEqual(deliveries[-1], ("", 0))
            self.assertTrue(page._older.isHidden())
        finally:
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
