"""Native aliases change on launch without affecting content or saved settings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))

try:
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QLabel

    HAVE_QT = True
except ImportError:
    HAVE_QT = False

ROOT = Path(__file__).resolve().parents[1]


def _run_python(script: str) -> str:
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], cwd=ROOT,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30, check=True,
    )
    return result.stdout.strip()


class WindowTitleTests(unittest.TestCase):
    def test_titles_are_stable_per_role_but_fresh_in_another_process(self) -> None:
        script = """
import json
from mnmparse.app.window_identity import window_title
roles = ['main', 'map', 'overlay', 'panel:timer_panel', 'panel:attack_bar']
first = [window_title(role) for role in roles]
assert first == [window_title(role) for role in roles]
assert window_title() == first[0]
print(json.dumps(first))
"""
        first = json.loads(_run_python(script))
        second = json.loads(_run_python(script))
        self.assertEqual(len(set(first)), 5)
        self.assertEqual(len(set(second)), 5)
        self.assertTrue(set(first).isdisjoint(second))
        for title in first + second:
            self.assertRegex(title, r"^[0-9a-f]{24}$")


@unittest.skipUnless(HAVE_QT, "PySide6 not installed")
class NativeWindowIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.windows = []

    def tearDown(self) -> None:
        for window in reversed(self.windows):
            if hasattr(window, "shutdown"):
                window.shutdown()
            window.close()
            window.deleteLater()
        self.app.processEvents()
        self.settings.sync()
        self.temp.cleanup()

    def _keep(self, window):
        self.windows.append(window)
        return window

    def test_main_status_changes_keep_alias_and_internal_brand(self) -> None:
        from mnmparse.app import APP_NAME, main
        from mnmparse.config import Config

        cfg = Config(minimize_to_tray=False)
        with patch.dict(main._pages_mod, {}, clear=True):
            window = self._keep(main.MainWindow(main._MissingEngine(cfg), None, cfg, self.settings))
        title = window.windowTitle()
        self.assertRegex(title, r"^[0-9a-f]{24}$")
        for state in ("running", "paused", "waiting", "stopped"):
            window.on_state(state)
            self.assertEqual(window.windowTitle(), title)
        self.assertIn(APP_NAME, [label.text() for label in window.findChildren(QLabel)])

    def test_map_zone_changes_keep_alias_and_visible_zone_selector(self) -> None:
        from mnmparse.app.map_overlay import MapOverlay
        from mnmparse.maps import MapRepository

        window = self._keep(MapOverlay(self.settings, MapRepository(self.root / "cache")))
        title = window.windowTitle()
        self.assertRegex(title, r"^[0-9a-f]{24}$")
        for zone in ("Sungreet Strand", "Shaded Dunes"):
            window.set_zone(zone)
            self.assertEqual(window.windowTitle(), title)
            self.assertEqual(window.zone.currentText(), zone)
            self.assertEqual(self.settings.value("map/zone"), zone)

    def test_overlay_timer_and_attack_windows_have_distinct_aliases(self) -> None:
        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.config import Config

        overlay = self._keep(OverlayWindow(self.settings, Config()))
        titles = [window.windowTitle() for window in (overlay, overlay.timer_panel, overlay.attack_bar)]
        self.assertEqual(len(set(titles)), 3)
        for title in titles:
            self.assertRegex(title, r"^[0-9a-f]{24}$")

    def test_help_and_share_dialogs_have_aliases_and_keep_readable_contents(self) -> None:
        from mnmparse.config import Config
        from mnmparse.app.trigger_help import TriggerHelpDialog
        from mnmparse.app.trigger_share_dialog import TriggerChatExportDialog, TriggerSharePrompt
        from mnmparse.triggers import Trigger

        trigger = Trigger(name="Example timer", pattern="test")
        help_dialog = self._keep(TriggerHelpDialog())
        export_dialog = self._keep(TriggerChatExportDialog(trigger, "share-code", cfg=Config(casual_mode=False, casual_mode_confirmed=True)))
        import_dialog = self._keep(TriggerSharePrompt(trigger, "Example player", cfg=Config(casual_mode=False, casual_mode_confirmed=True)))
        titles = [window.windowTitle() for window in self.windows]
        self.assertEqual(len(set(titles)), 3)
        for title in titles:
            self.assertRegex(title, r"^[0-9a-f]{24}$")
        self.assertIn("Table of contents", help_dialog.browser.toPlainText())
        self.assertEqual(export_dialog.line.toPlainText(), "share-code")
        self.assertIn("Name: Example timer", import_dialog.details.toPlainText())

    def test_display_fallback_is_random_while_settings_namespace_stays_stable(self) -> None:
        script = """
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from PySide6.QtCore import QSettings
from mnmparse.app.main import App
with tempfile.TemporaryDirectory() as tmp:
    settings = QSettings(str(Path(tmp) / 'test.ini'), QSettings.Format.IniFormat)
    with patch('mnmparse.app.main.QSettings', return_value=settings) as factory:
        app = App([])
    print(json.dumps({
        'title': app.applicationDisplayName(),
        'brand': app.applicationName(),
        'namespace': list(factory.call_args.args),
    }))
    app.shutdown()
"""
        data = json.loads(_run_python(script))
        self.assertRegex(data["title"], r"^[0-9a-f]{24}$")
        self.assertEqual(data["brand"], "PNUT M&M")
        self.assertEqual(data["namespace"], ["mnmparse", "MnM Parser"])


if __name__ == "__main__":
    unittest.main()
