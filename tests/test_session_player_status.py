"""Measured own XP survives session views and archives; text imports invent none."""
from __future__ import annotations

import dataclasses
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from mnmparse.app.session_view import SessionView
from mnmparse.config import Config
from mnmparse.importer import import_file
from mnmparse.privacy import project_session
from mnmparse.session import SessionStats, filter_session
from mnmparse.session_archive import SessionArchive
from tests.test_layout import clipped_widgets

PLAYER = "PreviewHero"


def status():
    return {
        "xp_percent": 94.8, "xp_gain_points": 3.0, "xp_observed_seconds": 600.0,
        "xp_points_per_hour": 18.0, "xp_level_ups": 0,
        "xp_rate_kind": "rolling", "xp_rate_window_seconds": 3600.0,
        "xp_rate_gain_points": 3.0, "xp_rate_observed_seconds": 600.0,
    }


class SessionPlayerStatusTests(unittest.TestCase):
    def test_note_copies_status_updates_revision_only_when_changed_and_snapshots_do_not_alias(self):
        session = SessionStats(PLAYER, started=0)
        summary = status()
        self.assertTrue(session.note_player_status(summary))
        version = session.version
        self.assertFalse(session.note_player_status(summary))
        self.assertEqual(session.version, version)
        summary["xp_gain_points"] = 99
        first = session.snapshot(now=600)
        self.assertEqual(first.player_status["xp_gain_points"], 3)
        first.player_status["xp_gain_points"] = 999
        later = session.snapshot(now=601)
        self.assertEqual(later.player_status["xp_gain_points"], 3)
        self.assertEqual(first.revision, later.revision)

    def test_status_whitelist_rejects_hp_per_kill_and_invalid_values(self):
        session = SessionStats(PLAYER, started=0)
        summary = status()
        summary.update(hp_percent=5, hp_below10_episodes=2, hp_observed_seconds=123)
        summary.update(xp_kill_gains=[{"ts": 123, "target": "rat", "points": 4}], xp_per_kill_points=4,
                       xp_matched_kills=3, last_seen=123)
        session.note_player_status(summary)
        snap = session.snapshot(now=600, detail_limit=3)
        self.assertEqual(snap.player_status, status())
        session.note_player_status({"xp_percent": 101, "xp_gain_points": float("nan"),
                                    "xp_observed_seconds": -1, "xp_level_ups": True,
                                    "xp_points_per_hour": None})
        self.assertEqual(session.player_status, {"xp_points_per_hour": None, "xp_rate_kind": "session"})

    def test_recent_metadata_determines_rate_without_recomputing_from_session_totals(self):
        session = SessionStats(PLAYER, started=0)
        summary = status()
        summary.update(xp_gain_points=30, xp_observed_seconds=3600, xp_points_per_hour=999)
        session.note_player_status(summary)
        snap = session.snapshot(now=3600).player_status
        self.assertEqual(snap["xp_points_per_hour"], 18)
        self.assertEqual(snap["xp_gain_points"], 30)
        self.assertEqual(snap["xp_observed_seconds"], 3600)

    def test_invalid_recent_metadata_cannot_masquerade_as_current_or_legacy_rate(self):
        for key, value in (("xp_rate_window_seconds", 3601), ("xp_rate_window_seconds", True),
                           ("xp_rate_gain_points", -1), ("xp_rate_gain_points", float("nan")),
                           ("xp_rate_gain_points", 1e308), ("xp_rate_gain_points", 10 ** 1000),
                           ("xp_rate_observed_seconds", 3601), ("xp_rate_observed_seconds", True),
                           ("xp_rate_observed_seconds", 0), ("xp_rate_observed_seconds", None)):
            with self.subTest(key=key, value=value):
                session = SessionStats(PLAYER, started=0)
                summary = dict(status(), **{key: value})
                session.note_player_status(summary)
                snap = session.snapshot(now=600).player_status
                self.assertEqual(snap["xp_rate_kind"], "rolling")
                self.assertIsNone(snap["xp_points_per_hour"])
                self.assertEqual(snap["xp_rate_observed_seconds"], 0)
                self.assertEqual(snap["xp_gain_points"], 3)
                json.dumps(snap, allow_nan=False)

    def test_legacy_rate_is_labeled_as_a_session_average_and_archives_preserve_that(self):
        legacy = {key: value for key, value in status().items() if not key.startswith("xp_rate_")}
        session = SessionStats(PLAYER, started=0)
        session.note_player_status(legacy)
        self.assertEqual(session.player_status["xp_rate_kind"], "session")
        self.assertEqual(session.player_status["xp_points_per_hour"], 18)
        with tempfile.TemporaryDirectory() as directory:
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            archive.save_session("old-average", session)
            with closing(sqlite3.connect(archive.path)) as db, db:
                state, preview = db.execute("SELECT state,preview FROM sessions WHERE id='old-average'").fetchone()
                state, preview = json.loads(state), json.loads(preview)
                state["player_status"].pop("xp_rate_kind")
                preview["player_status"].pop("xp_rate_kind")
                db.execute("UPDATE sessions SET state=?,preview=? WHERE id='old-average'",
                           (json.dumps(state), json.dumps(preview)))
            for restored in (archive.restore("old-average"), archive.preview("old-average")):
                self.assertEqual(restored.player_status["xp_rate_kind"], "session")
                self.assertEqual(restored.player_status["xp_points_per_hour"], 18)

    def test_carebear_and_self_projection_retain_own_xp(self):
        session = SessionStats(PLAYER, started=0)
        session.note_player_status(status())
        snap = session.snapshot(now=600)
        own = filter_session(snap, PLAYER)
        protected = project_session(snap, Config(player_name=PLAYER))
        self.assertEqual(own.player_status, snap.player_status)
        self.assertEqual(protected.player_status, snap.player_status)
        self.assertEqual(protected.privacy_mode, "casual")
        self.assertEqual(dataclasses.asdict(protected)["player_status"]["xp_gain_points"], 3)

    def test_archive_roundtrip_preserves_xp_with_any_preview_detail_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            session = SessionStats(PLAYER, started=100)
            session.note_player_status(status())
            archive.save_session("measured", session)
            restored = archive.restore("measured")
            self.assertEqual(restored.player_status, session.player_status)
            preview = archive.preview("measured", detail_limit=1)
            self.assertEqual(preview.player_status, status())
            self.assertEqual(archive.preview("measured", detail_limit=0).player_status, status())

    def test_legacy_archive_without_player_status_restores_empty_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = SessionArchive(Path(directory) / "sessions.sqlite3")
            session = SessionStats(PLAYER, started=100)
            archive.save_session("legacy", session)
            with closing(sqlite3.connect(archive.path)) as db, db:
                state, preview = db.execute("SELECT state,preview FROM sessions WHERE id='legacy'").fetchone()
                state, preview = json.loads(state), json.loads(preview)
                state.pop("player_status", None)
                preview.pop("player_status", None)
                db.execute("UPDATE sessions SET state=?,preview=? WHERE id='legacy'",
                           (json.dumps(state), json.dumps(preview)))
            self.assertEqual(archive.restore("legacy").player_status, {})
            self.assertEqual(archive.preview("legacy").player_status, {})

    def test_text_import_has_experience_ticks_but_no_bar_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "combat.log"
            path.write_text("You gain party experience!\n", encoding="utf-8")
            result = import_file(path, player_name=PLAYER, include_personal=True)
            self.assertEqual(result.session.xp_ticks, 1)
            self.assertEqual(result.session.player_status, {})


class SessionPlayerStatusViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.widgets = []

    def tearDown(self):
        for widget in self.widgets:
            widget.close()
            widget.deleteLater()
        self.app.processEvents()

    def view(self, compact=False):
        view = SessionView(compact)
        view.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        view.set_config(Config(player_name=PLAYER))
        view.resize(720, 600)
        view.show()
        self.widgets.append(view)
        return view

    @staticmethod
    def category(view, key):
        return next((view._tree.topLevelItem(index) for index in range(view._tree.topLevelItemCount())
                     if view._tree.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole) == key), None)

    def test_full_and_overlay_views_show_estimated_own_xp_and_preserve_expansion(self):
        session = SessionStats(PLAYER, started=0)
        session.note_player_status(status())
        snap = session.snapshot(now=600)
        for compact in (False, True):
            with self.subTest(compact=compact):
                view = self.view(compact)
                view.set_snapshot(snap)
                self.app.processEvents()
                top = self.category(view, "experience")
                self.assertIsNotNone(top)
                self.assertEqual(top.text(1), "~3.00%")
                text = " ".join(top.child(index).text(column) for index in range(top.childCount()) for column in range(3))
                self.assertIn("~94.8%", text)
                self.assertIn("Measured time", text)
                self.assertNotIn("kill", text)
                self.assertNotIn("HP", text)
                self.assertIn("XP ~18.0%/h", view._footer.text())
                self.assertIn("30-second bar comparisons", view._footer.toolTip())
                self.assertIn("hour", view._footer.toolTip())
                top.setExpanded(True)
                view.set_snapshot(snap)
                self.assertTrue(self.category(view, "experience").isExpanded())
                self.app.processEvents()
                self.assertEqual(clipped_widgets(view), [])

    def test_unmeasured_sessions_omit_xp_and_keep_only_session_rate_tooltip(self):
        view = self.view()
        session = SessionStats(PLAYER, started=0)
        session.note_player_status(status())
        view.set_snapshot(session.snapshot(now=600))
        view.set_snapshot(SessionStats(PLAYER, started=0).snapshot(now=600))
        self.assertIsNone(self.category(view, "experience"))
        self.assertNotIn("XP", view._footer.text())
        self.assertIn("Rolling one-hour rates", view._footer.toolTip())
        self.assertNotIn("30-second", view._footer.toolTip())
        view.set_snapshot(None)
        self.assertEqual(view._footer.toolTip(), "")

    def test_legacy_average_has_explicit_session_label_in_both_views(self):
        session = SessionStats(PLAYER, started=0)
        session.note_player_status({key: value for key, value in status().items()
                                    if not key.startswith("xp_rate_")})
        for compact in (False, True):
            with self.subTest(compact=compact):
                view = self.view(compact)
                view.set_snapshot(session.snapshot(now=600))
                self.assertIn("XP session ~18.0%/h", view._footer.text())
                self.assertIn("older version", view._footer.toolTip())

    def test_collecting_state_shows_current_bar_without_fake_rate_or_gain(self):
        view = self.view()
        session = SessionStats(PLAYER, started=0)
        session.note_player_status({"xp_percent": 94.8, "xp_gain_points": 0,
                                    "xp_observed_seconds": 0, "xp_points_per_hour": None,
                                    "xp_level_ups": 0, "xp_rate_kind": "rolling",
                                    "xp_rate_window_seconds": 3600, "xp_rate_gain_points": 0,
                                    "xp_rate_observed_seconds": 0})
        view.set_snapshot(session.snapshot(now=600))
        top = self.category(view, "experience")
        self.assertEqual(top.childCount(), 1)
        self.assertEqual(top.text(1), "Collecting…")
        self.assertEqual(top.child(0).text(1), "~94.8%")
        self.assertIn("XP collecting…", view._footer.text())
        self.assertNotIn("%/h", view._footer.text())
        self.assertIn("30 seconds", view._footer.toolTip())


if __name__ == "__main__":
    unittest.main()
