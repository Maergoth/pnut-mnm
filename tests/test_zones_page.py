"""The main Zones browser stays independent and usable with overlays disabled."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PySide6.QtCore import QObject, QPoint, QRect, QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QWidget

from mnmparse.app.map_overlay import MapOverlay, _Load
from mnmparse.app.npc_details import NpcDetailsController
from mnmparse.app.zones_page import ZonesPage
from mnmparse.config import Config
from mnmparse.maps import MapImage, MapRepository
from mnmparse.npc_wiki import ItemDetails, NpcDetails, NotableNpcs, wiki_url
from tests.test_npc_details import Map, Panel, Repository


class _ZoneEngine(QObject):
    zone_changed = Signal(str)

    def __init__(self, zone="Night Harbor"):
        super().__init__()
        self.current_zone = zone


class ZonesPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.temp.name) / "ui.ini"), QSettings.Format.IniFormat)
        self.settings.setValue("map/default_source", "images")
        self.cfg = Config(npc_enabled=False, overlay_enabled=False)
        self.engine = _ZoneEngine()
        self.repo = MapRepository(Path(self.temp.name) / "maps")
        self.worker = patch("mnmparse.app.map_overlay._Load.start")
        self.worker.start()
        self.pages = []
        self.page = self.make_page()
        self.reference, self.panel = Repository(), Panel()
        self.panel.enabled = False
        self.controller = NpcDetailsController(self.reference, self.panel, Map())
        self.controller._started = True  # Deterministic queue; no network worker.
        self.page.bind(self.controller)

    def tearDown(self):
        self.controller.shutdown()
        for page in self.pages:
            page.shutdown()
            page.close()
            page.deleteLater()
        self.app.processEvents()
        self.worker.stop()
        self.temp.cleanup()

    def make_page(self, engine=None):
        page = ZonesPage(engine or self.engine, self.cfg, self.settings, repository=self.repo)
        page.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        page.resize(1000, 600)
        self.pages.append(page)
        return page

    def show(self):
        self.page.show()
        self.app.processEvents()

    def test_embedded_header_is_permanent_and_native_overlay_controls_are_inert(self):
        self.show()
        view = self.page.map
        self.assertFalse(view.isWindow())
        self.assertTrue(view.header.isVisible())
        self.assertLess(view.header.geometry().bottom(), view._map_stack.geometry().top())
        self.assertFalse(view.drag_handle.isVisible())
        self.assertFalse(view.fullscreen.isVisible())
        self.assertFalse(view.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
        self.assertEqual(view._resize_edges(QPoint(1, 1)), Qt.Edge(0))
        geometry = self.page.geometry()
        view._hide_header()
        view._begin_drag(view.header, QPoint(50, 50))
        view.toggle_fullscreen()
        view.exit_fullscreen()
        view.set_appearance(.2, 1)
        self.assertTrue(view.header.isVisible())
        self.assertIsNone(view._drag_offset)
        self.assertFalse(self.page.isFullScreen())
        self.assertEqual(self.page.geometry(), geometry)
        self.assertEqual(view._opacity, 1.0)

    def test_current_zone_seeds_once_then_manual_choice_survives_live_zone_changes(self):
        self.assertEqual(self.page.map.current_zone, "Night Harbor")
        self.page.map.set_zone("Shaded Dunes")
        self.engine.zone_changed.emit("Sungreet Strand")
        self.page.map.on_message(None, type("Event", (), {"kind": "zone", "target": "Ancient Crypt"})())
        self.assertEqual(self.page.map.current_zone, "Shaded Dunes")
        restored = self.make_page(_ZoneEngine("Blacktide Bay"))
        self.assertEqual(restored.map.current_zone, "Shaded Dunes")
        self.assertFalse(self.settings.contains("map/zone"))

    def test_first_live_zone_can_seed_empty_browser_but_never_interrupts_an_edit(self):
        self.settings.remove("zones/zone")
        engine = _ZoneEngine("")
        page = self.make_page(engine)
        self.assertEqual(page.map.current_zone, "")
        engine.zone_changed.emit("Night Harbor")
        engine.zone_changed.emit("Shaded Dunes")
        self.assertEqual(page.map.current_zone, "Night Harbor")
        self.settings.remove("zones/zone")
        editing = self.make_page(engine=_ZoneEngine(""))
        editing.map.zone.lineEdit().textEdited.emit("Sha")
        editing.seed_initial_zone("Night Harbor")
        self.assertEqual(editing.map.current_zone, "")

    def test_map_view_state_is_separate_from_overlay_geometry_fullscreen_and_zoom(self):
        self.settings.setValue("map/zone", "Sungreet Strand")
        self.settings.setValue("map/geometry", QRect(100, 100, 500, 300))
        self.settings.setValue("map/fullscreen", True)
        self.settings.setValue("map/view", {"sentinel": "live"})
        before = {key: self.settings.value(key) for key in self.settings.allKeys() if key.startswith("map/")}
        self.show()
        image = QImage(1200, 800, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkGreen)
        entry = MapImage("Fictional Map", "https://monstersandmemories.miraheze.org/wiki/Special:FilePath/Sample.png", wiki_url("Night Harbor"))
        view = self.page.map
        view._ready(view._token, ([entry], image, True, entry.url), "")
        view.view.scale(1.5, 1.5)
        view.view._fit = False
        view.save_state()
        self.assertEqual({key: self.settings.value(key) for key in before}, before)
        self.assertEqual(self.settings.value("zones/view")["zone"], "Night Harbor")
        self.assertFalse(self.settings.contains("zones/fullscreen"))
        self.assertFalse(self.settings.contains("zones/geometry"))

    def test_cached_details_remain_visible_with_npc_overlay_disabled_and_live_considers(self):
        cached = self.reference.npcs["Fictional Keeper"] = NpcDetails(
            "Fictional Keeper", "12–15", ("Shared Reed",), ("Gilded Staff", "Shared Reed"),
            ("A Fictional Quest",), ("Gained: Fictional Keepers",), wiki_url("Fictional Keeper"))
        self.show()
        self.page.map.npc_selected.emit("Fictional Keeper")
        self.assertEqual(self.page.details.selected_name, "Fictional Keeper")
        text = self.page.details.browser.toPlainText()
        self.assertIn("Level: 12–15", text)
        self.assertIn("Gilded Staff", text)
        self.assertNotIn("Shared Reed", text)
        self.assertNotIn("A Fictional Quest", text)
        self.assertFalse(self.panel.enabled)
        self.controller.show_npc("Other Live NPC")
        self.controller._finished("npc", "Other Live NPC", NpcDetails("Other Live NPC", "30"), "")
        self.assertIs(self.page.details._details, cached)
        self.assertEqual(self.page.details.selected_name, "Fictional Keeper")

    def test_display_preferences_empty_fields_and_common_precedence_apply_to_main_pane(self):
        self.settings.setValue("npc/level", False)
        self.settings.setValue("npc/common_drops", True)
        self.settings.setValue("npc/quests", True)
        self.page.details.select_npc("Fictional Keeper")
        self.page.details.npc_updated("Fictional Keeper", NpcDetails(
            "Fictional Keeper", "Unknown", ("Shared Reed",), ("Shared Reed", "Gilded Staff"), ("None",)), "")
        text = self.page.details.browser.toPlainText()
        self.assertEqual(text.count("Shared Reed"), 1)
        self.assertLess(text.index("Unique drops"), text.index("Common drops"))
        self.assertNotIn("Level", text)
        self.assertNotIn("Quests", text)
        self.assertNotIn("Not available", text)

    def test_live_kill_celebration_opens_overlay_without_replacing_browser_selection(self):
        from mnmparse.app.notable_kills import NotableKillController
        from mnmparse.parser import parse_line
        browser_npc = self.reference.npcs["Fictional Keeper"] = NpcDetails("Fictional Keeper", "12")
        self.reference.npcs["Live Champion"] = NpcDetails("Live Champion", "30")
        self.reference.zones["Night Harbor"] = NotableNpcs("Night Harbor", ("Live Champion",))
        self.panel.enabled = True
        self.page.map.npc_selected.emit("Fictional Keeper")
        self.page.map.set_zone("Shaded Dunes")
        celebration = NotableKillController(self.controller, current_zone=lambda: "Night Harbor", active=lambda: True)
        try:
            celebration.on_kill(parse_line("You have slain Live Champion!", 100), "Night Harbor")
            self.assertEqual(self.panel.name, "Live Champion")
            self.assertEqual(self.page.map.current_zone, "Shaded Dunes")
            self.assertEqual(self.page.details.selected_name, "Fictional Keeper")
            self.assertIs(self.page.details._details, browser_npc)
        finally:
            celebration.shutdown()
            celebration.deleteLater()

    def test_item_hover_is_lazy_cache_first_and_wiki_links_use_canonical_sources(self):
        self.reference.npcs["Fictional Keeper"] = NpcDetails("Fictional Keeper", "12", unique_drops=("Old Staff",))
        canonical = wiki_url("Canonical Staff")
        self.reference.items["Old Staff"] = ItemDetails("Canonical Staff", ("MAGIC", "INT: +6", "Race: ALL"), canonical)
        self.show()
        self.page.map.npc_selected.emit("Fictional Keeper")
        details = self.page.details
        self.assertNotIn(("item", "Old Staff"), self.controller._pending)
        details._hover_link(QUrl("item:0"))
        self.assertIn(("item", "Old Staff"), self.controller._pending)
        self.assertIn("Race: ALL", details._tooltip._stats.text())
        self.assertTrue(details._tooltip.isVisible())
        with patch("mnmparse.app.zones_page.QDesktopServices.openUrl") as opened:
            details._open_link(QUrl("item:0"))
            self.assertEqual(opened.call_args.args[0].toString(), canonical)
            details._open_link(QUrl("npc:current"))
            self.assertEqual(opened.call_args.args[0].toString(), wiki_url("Fictional Keeper"))
        self.page.hide()
        self.controller.item_updated.emit("Old Staff", self.reference.items["Old Staff"], "")
        self.assertFalse(details._tooltip.isVisible())

    def test_html_is_plain_data_and_untrusted_sources_cannot_open_other_origins(self):
        view = self.page.details
        view.select_npc("<b>Fictional & Keeper</b>")
        view.npc_updated(view.selected_name, NpcDetails(view.selected_name, "12", source="file:///private.txt"), "Wiki unavailable")
        self.assertIn("<b>Fictional & Keeper</b>", view.browser.toPlainText())
        with patch("mnmparse.app.zones_page.QDesktopServices.openUrl") as opened:
            view._open_link(QUrl("https://untrusted.invalid"))
            opened.assert_not_called()
            view._open_link(QUrl("npc:current"))
            self.assertEqual(opened.call_args.args[0].host(), "monstersandmemories.miraheze.org")

    def test_embedded_worker_uses_catalog_and_cached_pixels_without_discovery_network(self):
        entry = MapImage("Fictional Map", "https://monstersandmemories.miraheze.org/wiki/Special:FilePath/Sample.png", wiki_url("Night Harbor"))
        image = QImage(10, 10, QImage.Format.Format_RGB32)
        image.fill(Qt.GlobalColor.darkGreen)
        repo = Mock(spec=MapRepository)
        repo.known_maps.return_value = [entry]
        repo.image.return_value = b"cached"
        worker = _Load(1, repo, "Night Harbor", None, False, threading.Semaphore(1), cached_first=True)
        received = []
        worker.signal.ready.connect(lambda *_args: received.append(_args))
        with patch("mnmparse.app.map_overlay._decode_map_image", return_value=image):
            worker.run()
        repo.maps.assert_not_called()
        repo.image.assert_called_once_with(entry, refresh=False)
        self.assertEqual(received[0][2], "")

    def test_main_navigation_and_zones_layout_fit_minimum_and_wide_windows(self):
        from mnmparse.app.main import MainWindow, _MissingEngine
        from tests.test_layout import clipped_widgets
        window = MainWindow(_MissingEngine(self.cfg), None, self.cfg, self.settings, map_repository=self.repo)
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        try:
            window.show()
            window.show_page("zones")
            page = window.page("zones")
            self.assertIsInstance(page, ZonesPage)
            self.assertIn("zones", window._nav_buttons)
            for width, height in ((980, 640), (1500, 900)):
                window.resize(width, height)
                for _ in range(4):
                    self.app.processEvents()
                self.assertEqual(clipped_widgets(window), [])
                self.assertTrue(page.map.header.isVisible())
                self.assertTrue(page.details.browser.isVisible())
        finally:
            window.page("zones").shutdown()
            window.prepare_quit()
            window.close()
            window.deleteLater()


def _bootstrap_probe(root: Path):
    from mnmparse.app import APP_VERSION
    from mnmparse.app.main import App, _MissingEngine
    from mnmparse.app.tray import TrayIcon
    from mnmparse.app.zones_page import BrowserOnlyNpcPanel
    settings = QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat)
    settings.setValue("map/default_source", "images")
    settings.setValue("app/whats_new_version", APP_VERSION)
    reference = Repository()
    reference.npcs["Fictional Keeper"] = NpcDetails("Fictional Keeper", "12–15", unique_drops=("Gilded Staff",))
    reference.zones["Night Harbor"] = NotableNpcs("Night Harbor", ("Fictional Keeper",), wiki_url("Night Harbor"))
    with patch("mnmparse.app.main.QSettings", return_value=settings):
        app = App([])
    cfg = Config(player_name="FictionalHero", setup_complete=True, start_capture_on_launch=False,
                 overlay_enabled=False, npc_enabled=False, log_dir=str(root / "logs"))
    with patch.object(app, "_make_engine", side_effect=lambda cfg: _MissingEngine(cfg)), \
         patch.object(app, "_make_overlay", return_value=None), \
         patch.object(app, "_make_trigger_runner", return_value=None), \
         patch("mnmparse.npc_wiki.WikiRepository", return_value=reference), \
         patch("mnmparse.app.map_overlay._Load.start"), \
         patch("mnmparse.app.main.QSystemTrayIcon.isSystemTrayAvailable", return_value=False):
        app.bootstrap(cfg, config_path=root / "config.json")
        app.npc_details._started = True
        page = app.window.page("zones")
        assert isinstance(app.npc_details.panel, BrowserOnlyNpcPanel)
        assert page.map.repo is app.map_overlay.repo
        app.window.show_page("zones")
        page.map.set_zone("Night Harbor")
        app.processEvents()
        assert page.map.isVisible()
        assert page.map.notable_npcs_menu.actions()[0].data() == "Fictional Keeper"
        page.map.notable_npcs_menu.actions()[0].trigger()
        assert "Gilded Staff" in page.details.browser.toPlainText()
        app.set_overlay_visible(False)
        assert page.map.isVisible()
        with patch.object(page.map, "reload_cached_map") as reloaded:
            app._on_map_download_finished("Done")
            reloaded.assert_called_once()
        app.shutdown()
        assert page.map._closing
    return {"ok": True}


class ZonesAppIntegrationTests(unittest.TestCase):
    def test_main_browser_shared_cache_works_without_any_combat_overlay(self):
        with tempfile.TemporaryDirectory() as folder:
            code = "from pathlib import Path; from tests.test_zones_page import _bootstrap_probe; import json,sys; print(json.dumps(_bootstrap_probe(Path(sys.argv[1]))))"
            child = subprocess.run([sys.executable, "-c", code, folder], cwd=Path(__file__).parents[1],
                                   env=dict(os.environ, QT_QPA_PLATFORM="offscreen"), capture_output=True, text=True, timeout=30)
            self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
            self.assertTrue(json.loads(child.stdout.strip().splitlines()[-1])["ok"])


if __name__ == "__main__":
    unittest.main()
