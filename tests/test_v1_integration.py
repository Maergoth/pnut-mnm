"""Exercise real application wiring in a disposable, offscreen process."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def _probe(root: Path):
    import dataclasses
    from unittest.mock import patch
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QLabel
    from mnmparse.app.revenge import RevengeList
    from mnmparse.app.main import App, _MissingPage
    from mnmparse.config import Config, save_config, load_config
    from mnmparse.parser import parse_line
    settings = QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat)
    cfg = Config(player_name="Hero", log_dir=str(root / "logs"), start_capture_on_launch=False,
                 setup_complete=True, reduced_motion=True)
    config_path = root / "selected.json"
    save_config(cfg, str(config_path))
    with patch("mnmparse.app.main.QSettings", return_value=settings):
        app = App([])
    app.bootstrap(cfg, config_path=config_path)
    assert not any(isinstance(page, _MissingPage) for page in app.window._pages.values())
    assert "Casual Mode" in app.window._mode_button.text()
    assert not app.triggers.store.path.samefile(Path(__file__).parents[1] / "triggers.json") if (Path(__file__).parents[1] / "triggers.json").exists() else True
    assert app.triggers.store.path == root / "triggers.json"
    assert "revenge" not in dict(app.overlay._tabs.visible_tabs())
    options = app.window.page("settings")
    options._on_morality_mode(False)  # The separate switch tests exercise the actual pledge.
    cfg = dataclasses.replace(app.cfg, revenge_enabled=True)
    options.load(cfg)
    options.revenge_days.setValue(7)
    options.revenge_entries.setValue(1)
    options.save()
    assert (app.cfg.revenge_days, app.cfg.revenge_entries) == (7, 1)
    assert options.revenge_days.isEnabled() and options.revenge_entries.isEnabled()
    assert "revenge" in dict(app.overlay._tabs.visible_tabs())
    app.overlay.set_tab("revenge")
    assert app.revenge.observe(parse_line("Bandit hits YOU for 12 points of damage.", 100, "Hero"))
    assert app.revenge.save_attacker("Bandit")
    assert app.revenge.save_attacker("a rat")
    assert app.revenge.remove_saved("a rat")
    app.overlay._revenge.pop_out_button.click()
    app.processEvents()
    assert app.revenge_popout.isVisible()
    popout = app.revenge_popout
    app.revenge.pop_out_requested.emit()
    assert app.revenge_popout is popout
    lists = popout.findChildren(RevengeList)
    assert len(lists) == 1
    assert "Bandit" in lists[0]._rows
    assert app.revenge.save_attacker("Raider")
    app.processEvents()
    assert app.revenge.saved_names == ("Raider",)
    assert set(lists[0]._rows) == {"Raider"}
    options.revenge_entries.setValue(0)
    options.save()
    app.processEvents()
    assert set(app.revenge.saved_names) == {"Bandit", "Raider"}
    options._on_morality_mode(True)
    app.processEvents()
    assert app.revenge.saved_names == ()
    for surface in (app.overlay, app.window.page("triggers"), app.revenge_popout):
        assert not any(name in label.text() for label in surface.findChildren(QLabel)
                       for name in ("Bandit", "Raider"))
    cfg = dataclasses.replace(app.cfg, revenge_enabled=False)
    options.load(cfg)
    options.save()
    assert app.overlay.tab != "revenge"
    assert "revenge" not in dict(app.overlay._tabs.visible_tabs())
    assert app._revenge_box.isHidden()
    assert app.revenge_popout.isHidden()
    assert not options.revenge_days.isEnabled() and not options.revenge_entries.isEnabled()
    app.revenge.pop_out_requested.emit()
    app.processEvents()
    assert app.revenge_popout.isHidden()
    tools = options.profile_tools
    assert tools.save_profile("Local test")
    tools.create_backup(root / "preferences.zip")
    tools.restore_backup(root / "preferences.zip")
    assert app.cfg.casual_mode and not app.cfg.casual_mode_confirmed
    app.show_demo()
    assert not app.window._demo_banner.isHidden()
    assert not app.window.page("live")._csv.isEnabled()
    assert not app.window.page("session")._export.isEnabled()
    assert app.copy_snapshot(app.window.page("live").selected()) == ""
    app._leave_demo()
    assert app.window._demo_banner.isHidden()
    assert load_config(str(config_path)).casual_mode
    app.set_map_visible(False)
    app.overlay.set_attack_bar_enabled(False)
    assert app.cfg.attack_bar and not settings.value("attack_bar/enabled", True, type=bool)
    app.open_setup()
    wizard = app._setup_wizard
    assert not wizard.options_page.display_map.isChecked()
    assert not wizard.options_page.attack_bar.isChecked(), "Setup must use the current bar state"
    wizard.options_page.display_map.setChecked(True)
    wizard.options_page.revenge_enabled.setChecked(True)
    wizard.options_page.attack_bar.setChecked(False)
    wizard.options_page.export_auto.setChecked(True)
    with patch.object(app, "play_sound") as audio:
        wizard.character_page.morality.siren_requested.emit()
        audio.assert_called_once_with("Siren")
    wizard.character_page.morality.mode_requested.emit(False)
    assert not wizard._draft.casual_mode and wizard._draft.casual_mode_confirmed
    assert app.cfg.casual_mode and load_config(str(config_path)).casual_mode
    wizard.reject()
    assert app.cfg.casual_mode
    assert not app.cfg.revenge_enabled and app.cfg.attack_bar and not app.cfg.export_auto
    assert not app.window._map_button.isChecked()
    assert not settings.value("map/visible", True, type=bool)
    assert not app.overlay.attack_bar.enabled
    with patch("mnmparse.config.save_config", side_effect=OSError("test write failure")):
        app._finish_setup(dataclasses.replace(app.cfg, revenge_enabled=True, attack_bar=False, export_auto=True),
                          display_map=True)
    assert not app.cfg.revenge_enabled and app.cfg.attack_bar and not app.cfg.export_auto
    assert not app.window._map_button.isChecked()
    assert not settings.value("map/visible", True, type=bool)
    assert not settings.value("attack_bar/enabled", True, type=bool)
    app.open_setup()
    wizard = app._setup_wizard
    wizard.character_page.morality.mode_requested.emit(False)
    wizard.next()
    import numpy as np
    wizard.crop_picker.set_frame(np.full((720, 1280, 3), 99, dtype=np.uint8))
    wizard._begin_ocr()
    wizard._ocr_completed(3)
    wizard.next()
    assert wizard.currentId() == 2
    wizard.options_page.display_map.setChecked(True)
    wizard.options_page.revenge_enabled.setChecked(True)
    wizard.options_page.attack_bar.setChecked(False)
    wizard.options_page.export_auto.setChecked(True)
    wizard.next()
    wizard.accept()
    app.processEvents()
    saved = load_config(str(config_path))
    assert saved.setup_complete and not saved.casual_mode and saved.casual_mode_confirmed
    assert not app.cfg.casual_mode and not app.engine.config.casual_mode
    assert not options.morality.switch.isChecked()
    assert saved.revenge_enabled and not saved.attack_bar and saved.export_auto
    assert options.revenge_enabled.isChecked() and not options.attack_bar.isChecked() and options.export_auto.isChecked()
    assert not app.overlay.attack_bar.enabled
    assert not settings.value("attack_bar/enabled", True, type=bool)
    assert app.window._map_button.isChecked() and settings.value("map/visible", False, type=bool)
    assert "revenge" in dict(app.overlay._tabs.visible_tabs())
    assert app.window._mode_button.text() == "🔥 Elitist Scumbag Mode 🔥"
    assert wizard.crop_picker._frame is None
    assert not app.engine.is_running
    overlay = app.overlay
    try:
        app.overlay = None
        app._finish_setup(dataclasses.replace(app.cfg, attack_bar=True))
        assert settings.value("attack_bar/enabled", False, type=bool), "Saving must update the bar preference without an overlay"
    finally:
        app.overlay = overlay
    app.on_config_changed(app.cfg)
    assert app.overlay.attack_bar.enabled
    app.shutdown()
    return {"ok": True, "surfaces": "main, settings, setup, triggers, overlay, demo, backup"}


class V1IntegrationTests(unittest.TestCase):
    def test_real_app_profile_revenge_privacy_demo_and_backup_wiring(self):
        with tempfile.TemporaryDirectory() as folder:
            code = "from pathlib import Path; from tests.test_v1_integration import _probe; import json,sys; print(json.dumps(_probe(Path(sys.argv[1]))))"
            child = subprocess.run([sys.executable, "-c", code, folder], cwd=Path(__file__).parents[1],
                                   env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertTrue(json.loads(child.stdout.strip().splitlines()[-1])["ok"])


if __name__ == "__main__":
    unittest.main()
