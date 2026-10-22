"""Periodic reference revalidation stays cache-first, bounded and offline-safe."""
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from mnmparse.app.npc_details import MAX_WATCHED, PERIODIC_BATCH, NpcDetailsController
from mnmparse.npc_wiki import ItemDetails, NpcDetails, NotableNpcs
from tests.test_npc_details import Map, Panel, Repository


class PeriodicWikiRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = 1000.0
        self.clock_patch = patch("mnmparse.app.npc_details.time.monotonic", side_effect=lambda: self.now)
        self.clock_patch.start()
        self.repo, self.panel, self.map = Repository(), Panel(), Map()
        self.repo.release.set()
        self.controller = NpcDetailsController(self.repo, self.panel, self.map)
        self.references = []
        self.controller.npc_updated.connect(lambda *args: self.references.append(args))

    def tearDown(self):
        self.controller.shutdown()
        self.repo.release.set()
        for obj in (self.controller, self.panel, self.map):
            obj.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.clock_patch.stop()

    def finish(self):
        deadline = time.perf_counter() + 2
        while self.controller._pending and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.assertFalse(self.controller._pending)

    def test_npc_item_and_zone_recheck_without_another_consider_hover_or_selection(self):
        old_npc = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "7")
        old_item = self.repo.items["Test Staff"] = ItemDetails("Test Staff", ("AC: 2",))
        self.repo.zones["Night Harbor"] = NotableNpcs("Night Harbor")
        self.controller.request_npc("Test Keeper")
        self.controller.show_item("Test Staff")
        self.controller.show_notables("Night Harbor")
        self.assertIs(self.references[0][1], old_npc)
        self.assertEqual(self.panel.items["Test Staff"], old_item)
        self.finish()
        first_calls = list(self.repo.calls)
        self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "9")
        self.repo.items["Test Staff"] = ItemDetails("Test Staff", ("AC: 4",))
        self.repo.zones["Night Harbor"] = NotableNpcs("Night Harbor", ("New Keeper",))
        self.repo.npcs["New Keeper"] = NpcDetails("New Keeper", "12")
        self.now += 299
        self.controller._revalidate()
        self.assertEqual(self.repo.calls, first_calls)
        self.now += 1
        self.controller._revalidate()
        self.assertLessEqual(len(self.controller._pending), PERIODIC_BATCH)
        self.finish()
        self.controller._revalidate()
        self.finish()
        self.assertEqual(next(details.level for name, details, _ in reversed(self.references)
                              if name == "Test Keeper"), "9")
        self.assertEqual(self.panel.items["Test Staff"].lines, ("AC: 4",))
        self.assertEqual(self.map.zones["Night Harbor"][0], ("New Keeper",))
        for key in (("npc", "Test Keeper"), ("item", "Test Staff"), ("notables", "Night Harbor")):
            self.assertEqual(self.repo.calls.count(key), 2)
        self.assertEqual(self.panel.name, "")  # A background refresh never considers an NPC.

    def test_outage_keeps_cache_then_retries_automatically_after_one_minute(self):
        cached = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "7")
        self.repo.error = OSError("offline")
        self.controller.request_npc("Test Keeper")
        self.assertIs(self.references[-1][1], cached)
        self.finish()
        self.assertEqual(self.references[-1], ("Test Keeper", cached, "Wiki unavailable · showing cached details"))
        self.now += 59
        self.controller._revalidate()
        self.assertEqual(len(self.repo.calls), 1)
        self.repo.error = False
        fresh = self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "10")
        self.now += 1
        self.controller._revalidate()
        self.finish()
        self.assertEqual(self.references[-1], ("Test Keeper", fresh, ""))
        self.assertNotIn(("npc", "Test Keeper"), self.controller._last_errors)

    def test_owned_qtimer_drives_due_refresh_without_another_public_request(self):
        self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "7")
        self.controller.request_npc("Test Keeper")
        self.finish()
        self.repo.npcs["Test Keeper"] = NpcDetails("Test Keeper", "11")
        self.now += 300
        self.controller._refresh_timer.setInterval(1)
        deadline = time.perf_counter() + 2
        while len(self.repo.calls) < 2 and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(.002)
        self.finish()
        self.assertEqual(self.repo.calls, [("npc", "Test Keeper")] * 2)
        self.assertEqual(self.references[-1][1].level, "11")

    def test_timer_and_watchlist_are_bounded_with_foreground_protected_from_prefetch(self):
        self.assertFalse(self.controller._refresh_timer.isActive())
        self.controller._started = True  # Inspect scheduling without launching network workers.
        self.controller.request_npc("Foreground Keeper")
        for number in range(MAX_WATCHED * 3):
            self.controller._request("item", f"Prefetch {number}", background=True)
            # Simulate a completed job so the shared queue does not obscure
            # the independent watchlist limit.
            self.controller._queue.clear()
            self.controller._pending.clear()
        self.assertEqual(len(self.controller._watched), MAX_WATCHED)
        self.assertIn(("npc", "Foreground Keeper"), self.controller._watched)
        self.assertTrue(self.controller._refresh_timer.isActive())
        self.now += 300
        self.controller._revalidate()
        self.assertEqual(len(self.controller._queue), PERIODIC_BATCH)
        self.controller._revalidate()
        self.assertEqual(len(self.controller._queue), PERIODIC_BATCH * 2)
        self.controller.shutdown()
        self.assertFalse(self.controller._refresh_timer.isActive())
        self.assertFalse(self.controller._watched)
        self.controller._revalidate()
        self.assertFalse(self.controller._pending)

    def test_shared_outage_pauses_automatic_items_and_zones_but_not_explicit_hover(self):
        self.controller._started = True
        self.controller._enrichment_retry_at = self.now + 60
        for key in (("item", "Prefetched Staff"), ("notables", "Night Harbor"), ("npc", "Prefetched Keeper")):
            self.assertFalse(self.controller._request(*key, background=True))
        self.assertFalse(self.controller._pending)
        self.controller.show_item("Explicit Staff")
        self.assertIn(("item", "Explicit Staff"), self.controller._pending)


if __name__ == "__main__":
    unittest.main()
