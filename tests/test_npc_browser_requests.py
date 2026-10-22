"""Browser reference requests share the cache/worker without selecting overlays."""
import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from mnmparse.app.npc_details import NpcDetailsController
from mnmparse.npc_wiki import ItemDetails, NpcDetails, WikiPageMissing
from tests.test_npc_details import Map, Panel, Repository


class NpcBrowserRequestsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.repo, self.panel, self.map = Repository(), Panel(), Map()
        self.panel.enabled = False
        self.controller = NpcDetailsController(self.repo, self.panel, self.map)
        self.npcs, self.items = [], []
        self.controller.npc_updated.connect(lambda *args: self.npcs.append(args))
        self.controller.item_updated.connect(lambda *args: self.items.append(args))

    def tearDown(self):
        self.controller.shutdown()
        self.repo.release.set()
        self.app.processEvents()

    def finish(self):
        self.repo.release.set()
        deadline = time.monotonic() + 3
        while self.controller._pending and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.controller._pending)

    def test_npc_request_is_cache_first_and_enabled_without_opening_overlay(self):
        cached = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "12")
        self.controller.request_npc(" Test_Keeper ")
        self.assertEqual(self.npcs, [("Test Keeper", cached, "Cached wiki details")])
        self.assertEqual(self.panel.name, "")
        self.assertEqual(self.controller._selected, "")
        self.assertIsNone(self.panel.details)
        self.finish()
        self.assertEqual(self.npcs[-1], ("Test Keeper", cached, ""))
        self.assertEqual(self.repo.calls.count(("npc", "Test Keeper")), 1)
        self.assertIsNone(self.panel.details)

    def test_unknown_request_gets_loading_then_background_details_and_reuses_throttle(self):
        self.controller.request_npc("Test Keeper")
        self.assertEqual(self.npcs[-1], ("Test Keeper", None, "Loading wiki details…"))
        fresh = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "14–16")
        self.finish()
        self.assertEqual(self.npcs[-1], ("Test Keeper", fresh, ""))
        self.controller.request_npc("Test Keeper")
        self.assertEqual(self.npcs[-1], ("Test Keeper", fresh, "Cached wiki details"))
        self.assertEqual(self.repo.calls.count(("npc", "Test Keeper")), 1)

    def test_missing_and_unavailable_results_are_distinct_and_cache_survives(self):
        for name, error in (("Missing NPC", WikiPageMissing("missing")), ("Offline NPC", OSError("offline"))):
            self.repo.error = error
            self.controller.request_npc(name)
            self.finish()
            expected = "No wiki page found for this NPC" if isinstance(error, WikiPageMissing) else "Wiki details unavailable · try again later"
            self.assertEqual(self.npcs[-1], (name, None, expected))
        cached = self.repo.npcs["Cached NPC"] = NpcDetails("Cached NPC", "7")
        self.controller.request_npc("Cached NPC")
        self.finish()
        self.assertEqual(self.npcs[-1], ("Cached NPC", cached, "Wiki unavailable · showing cached details"))

    def test_browser_npc_is_not_reselected_when_live_consider_finishes(self):
        browser = self.repo.npcs["Browser NPC"] = NpcDetails("Browser NPC", "7")
        live = self.repo.npcs["Live NPC"] = NpcDetails("Live NPC", "8")
        self.panel.enabled = True
        self.controller.request_npc("Browser NPC")
        self.controller.show_npc("Live NPC")
        self.finish()
        self.assertEqual(self.controller._selected, "Live NPC")
        self.assertIs(self.panel.details, live)
        self.assertTrue(any(name == "Browser NPC" and details is browser for name, details, _ in self.npcs))
        self.assertTrue(any(name == "Live NPC" and details is live for name, details, _ in self.npcs))

    def test_item_request_keeps_hover_alias_in_cache_and_background_signals(self):
        canonical = ItemDetails("Canonical Staff", ("MAGIC",), "https://monstersandmemories.miraheze.org/wiki/Canonical_Staff")
        self.repo.items["Old Staff"] = canonical
        self.controller.request_item(" Old_Staff ")
        self.assertEqual(self.items[-1], ("Old Staff", ItemDetails("Old Staff", canonical.lines, canonical.source), "Cached wiki details"))
        self.assertEqual(self.panel.items, {})
        self.finish()
        name, details, status = self.items[-1]
        self.assertEqual((name, details.name, details.source, status), ("Old Staff", "Old Staff", canonical.source, ""))
        self.assertEqual(self.repo.calls.count(("item", "Old Staff")), 1)

    def test_background_enrichment_uses_same_signals_and_foreground_promotes_queue(self):
        self.controller._started = True
        cached = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "12")
        self.controller._request("npc", "Test Keeper", background=True)
        self.assertEqual(self.npcs[-1], ("Test Keeper", cached, "Cached wiki details"))
        self.controller._request("item", "Other Item", background=True)
        self.controller.request_npc("Test Keeper")
        self.assertEqual(self.controller._queue[0], ("npc", "Test Keeper"))
        self.assertEqual(list(self.controller._queue).count(("npc", "Test Keeper")), 1)
        self.controller._finished("npc", "Test Keeper", cached, "")
        self.assertEqual(self.npcs[-1], ("Test Keeper", cached, ""))
        self.assertIsNone(self.panel.details)

    def test_shutdown_suppresses_cached_requests_and_late_reference_results(self):
        self.controller.shutdown()
        self.controller.request_npc("Test Keeper")
        self.controller.request_item("Old Staff")
        self.controller._finished("npc", "Test Keeper", NpcDetails("Test Keeper", "12"), "")
        self.assertEqual((self.npcs, self.items, self.repo.calls), ([], [], []))
