"""Real shell routes and startup modes in disposable offscreen processes."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


def probe(directory: str, selftest: bool):
    from unittest.mock import Mock, patch
    from PySide6.QtCore import QSettings
    from mnmparse.app.main import App, _MissingPage
    from mnmparse.config import Config
    from mnmparse.capture import WgcWindowSource
    from mnmparse.leaderboard import READING_FIELDS, clipboard_text, make_snapshot, utc_stamp, validate_snapshot
    from mnmparse.player_status import PlayerStatusReading
    from tests.test_layout import clipped_widgets
    root = Path(directory)
    settings = QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat)
    settings.setValue("main/page", "leaderboard")
    settings.setValue("map/visible", False)
    cfg = Config(player_name="FictionalHero", setup_complete=True, start_capture_on_launch=False,
                 log_dir=str(root / "logs"))
    with patch("mnmparse.app.main.QSettings", return_value=settings), \
         patch.dict(os.environ, {"PNUT_LEADERBOARD_SUBMIT_URL": "http://127.0.0.1:8765/snapshots"}), \
         patch("mnmparse.app.leaderboard_client._fetch") as fetch:
        app = App([])
        app.bootstrap(cfg, config_path=root / "config.json", selftest_seconds=100 if selftest else None)
        # Do not pump startup timers in self-test mode: this check never opens capture.
        page = app.window.page("leaderboard")
        assert not isinstance(page, _MissingPage)
        if selftest:
            assert app._leaderboard_ledger is None
            assert not (root / "leaderboard-state.bin").exists()
            assert not page.submit_button.isEnabled()
            app.shutdown()
            return
        assert app._leaderboard_ledger is not None
        assert app.engine._protected_ledger is app._leaderboard_ledger
        assert (root / "leaderboard-state.bin").is_file()
        fetch.assert_not_called()
        for size in ((980, 640), (1500, 900)):
            app.window.resize(*size)
            app.window.layout().activate()
            app.window._stack.layout().activate()
            page.layout().activate()
            assert not clipped_widgets(page), clipped_widgets(page)
        options = app.window.page("settings")
        options.capture_backend.setCurrentText("mss")
        assert not options.leaderboard_capture_warning.isHidden()
        assert "Window Capture" in options.leaderboard_capture_warning.text()
        assert "not eligible" in options.leaderboard_capture_warning.text()
        options.capture_backend.setCurrentText("wgc")
        assert options.leaderboard_capture_warning.isHidden()
        snapshot = make_snapshot({"max_health": 123, "max_mana": 50, "captured_at": utc_stamp(), "capture_backend": "window"},
                                 dict(personal_kills=2, personal_deaths=0, high_hit=100, coin_looted=1200), bytes([1]) * 32, 10)
        app.engine.leaderboard_status = lambda: {**snapshot, "eligible": True, "copy_eligible": True, "submitted_day": "", "error": ""}
        app.engine.leaderboard_view = lambda: (app.engine.leaderboard_status(), snapshot)
        app.engine.leaderboard_submission = Mock(return_value=snapshot)
        app.engine.leaderboard_copy_text = lambda: clipboard_text(snapshot)
        app._read_area_gears.refresh = Mock()
        with patch.object(app._leaderboard_client, "submit", return_value=True) as submit:
            app._on_read_area_action("copy_stats")
            assert "Max Health: 123" in app.clipboard().text()
            submit.assert_not_called()
            app.engine.leaderboard_submission.assert_not_called()
            page.message_input.setText("  This shit is fun!  ")
            app._on_read_area_action("submit_stats")
            submit.assert_called_once_with(snapshot)
            app.engine.leaderboard_submission.assert_called_once_with(message="This **** is fun!")
        # The same live engine reading feeds current Copy and independent best-value Submit.
        # No network, capture source, installed profile or real game data is used.
        for name in ("leaderboard_status", "leaderboard_view", "leaderboard_submission", "leaderboard_copy_text"):
            delattr(app.engine, name)
        app.engine._source = object.__new__(WgcWindowSource)
        app.engine._state = "running"
        app.engine._window_found = True
        first_fields = dict.fromkeys(READING_FIELDS, 1)
        first_fields.update(strength=100, armor_class=500, resist_cold=100)
        current_fields = dict.fromkeys(READING_FIELDS, 1)
        current_fields.update(stamina=80, armor_class=20, resist_corruption=80)
        now = time.time()
        app.engine._observe_player_status(PlayerStatusReading(900, 900, (0, 0, 8, 8), (), mana_max=700,
                                                              **first_fields), app.engine._cfg, now - 1, (8, 8, 3))
        app.engine._observe_player_status(PlayerStatusReading(100, 100, (0, 0, 8, 8), (), mana_max=50,
                                                              **current_fields), app.engine._cfg, now, (8, 8, 3))
        with patch.object(app._leaderboard_client, "submit", return_value=True) as submit:
            app._on_read_area_action("copy_stats")
            local = app.clipboard().text()
            for expected in ("Max Health: 100", "Max Mana: 50", "Strength: 1", "Stamina: 80",
                             "Armor Class: 20", "Stat Total: 86", "Resist Total: 87"):
                assert expected in local, expected
            submit.assert_not_called()
            app._on_read_area_action("submit_stats")
            submit.assert_called_once()
            personal_bests = validate_snapshot(submit.call_args.args[0])
            for field, expected in dict(schema_version=3, max_health=900, max_mana=700, strength=100,
                                        stamina=80, armor_class=500, stat_total=106, resist_total=107).items():
                assert personal_bests[field] == expected, field
            assert personal_bests["message"] == "This **** is fun!"
        app.engine._source = None
        app.engine._state = "stopped"
        app.show_demo()
        assert not page.copy_button.isEnabled()
        assert not page.submit_button.isEnabled()
        app._leave_demo()
        assert not page.copy_button.isEnabled()
        fetch.assert_not_called()
        app.shutdown()
        assert app._leaderboard_client._closed


class WiringTests(unittest.TestCase):
    def run_probe(self, selftest):
        with tempfile.TemporaryDirectory() as temporary:
            env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
            result = subprocess.run([sys.executable, "-m", "tests.test_leaderboard_wiring", temporary, str(int(selftest))],
                                    capture_output=True, text=True, timeout=45, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_normal_startup_is_cache_only_and_routes_explicit_actions(self):
        self.run_probe(False)

    def test_selftest_does_not_create_ledger_or_network_client(self):
        self.run_probe(True)


if __name__ == "__main__" and len(sys.argv) == 3:
    probe(sys.argv[1], bool(int(sys.argv[2])))
