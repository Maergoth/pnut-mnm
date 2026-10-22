"""Cache-first presentation, request deduplication and stale asynchronous results."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from types import SimpleNamespace
import threading
import time
import unittest

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from mnmparse.app.npc_details import NpcDetailsController
from mnmparse.npc_wiki import WikiPageMissing
from mnmparse.parser import parse_line


class Panel(QObject):
    item_hovered = Signal(str)

    def __init__(self):
        super().__init__()
        self.enabled = True
        self.options = {"unique_drops": True, "common_drops": False}
        self.name, self.details, self.status = "", None, ""
        self.items = {}

    def show_npc(self, name):
        self.name = name

    def set_details(self, details, status=""):
        self.details, self.status = details, status

    def set_item_details(self, item):
        self.items[item.name] = item

    def set_item_status(self, name, status):
        self.items[name] = status


class Map(QObject):
    npc_selected = Signal(str)
    npc_zone_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.zones = {}

    def set_notable_npcs(self, zone, names, status=""):
        self.zones[zone] = (names, status)


class Repository:
    def __init__(self):
        self.npcs, self.items, self.zones = {}, {}, {}
        self.calls = []
        self.release = threading.Event()
        self.error = False

    def cached_npc(self, name):
        return self.npcs.get(name)

    def cached_item(self, name):
        return self.items.get(name)

    def cached_notables(self, name):
        return self.zones.get(name)

    def _refresh(self, kind, name, records):
        self.calls.append((kind, name))
        self.release.wait(3)
        if isinstance(self.error, Exception):
            raise self.error
        if self.error:
            raise OSError("wiki offline")
        return records.get(name)

    def refresh_npc(self, name):
        return self._refresh("npc", name, self.npcs)

    def refresh_item(self, name):
        return self._refresh("item", name, self.items)

    def refresh_notables(self, name):
        return self._refresh("notables", name, self.zones)


def npc(name, **kwargs):
    return SimpleNamespace(name=name, common_drops=(), unique_drops=(), **kwargs)


class NpcDetailsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.repo, self.panel, self.map = Repository(), Panel(), Map()
        self.controller = NpcDetailsController(self.repo, self.panel, self.map)

    def tearDown(self):
        self.controller.shutdown()
        self.repo.release.set()
        self.app.processEvents()

    def wait_done(self):
        self.repo.release.set()
        deadline = time.monotonic() + 3
        while self.controller._pending and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.005)
        self.assertFalse(self.controller._pending)

    def test_cached_details_display_before_network_and_considers_deduplicate(self):
        cached = self.repo.npcs["a rat"] = npc("a rat")
        event = parse_line("a rat seems indifferent to your presence.", 0)
        for _ in range(5):
            self.controller.on_message(None, event)
        self.assertIs(self.panel.details, cached)
        self.assertIn("Cached", self.panel.status)
        self.wait_done()
        self.assertEqual(self.repo.calls.count(("npc", "a rat")), 1)
        self.controller.show_npc("a rat")
        self.assertEqual(self.repo.calls.count(("npc", "a rat")), 1)

    def test_slow_previous_target_cannot_replace_new_selection(self):
        first, second = npc("a rat"), npc("a bat")
        self.repo.npcs.update({"a rat": first, "a bat": second})
        self.controller.show_npc("a rat")
        self.controller.show_npc("a bat")
        self.wait_done()
        self.assertEqual(self.panel.name, "a bat")
        self.assertIs(self.panel.details, second)

    def test_outage_preserves_cached_data_and_retries_are_throttled(self):
        self.repo.npcs["a rat"] = npc("a rat")
        self.repo.error = True
        self.controller.show_npc("a rat")
        self.wait_done()
        self.assertEqual(self.panel.details.name, "a rat")
        self.assertIn("unavailable", self.panel.status)
        self.controller.show_npc("a rat")
        self.assertIn("cached", self.panel.status)
        self.assertEqual(self.repo.calls.count(("npc", "a rat")), 1)

    def test_empty_outage_is_distinct_from_no_drops_and_map_uses_same_selection(self):
        self.repo.error = True
        self.map.npc_selected.emit("Named Monster")
        self.wait_done()
        self.assertEqual(self.panel.name, "Named Monster")
        self.assertIsNone(self.panel.details)
        self.assertIn("unavailable", self.panel.status)

    def test_cached_notables_and_item_tooltips_do_not_wait_for_wiki(self):
        self.repo.zones["Night Harbor"] = SimpleNamespace(names=("Named Monster",))
        item = self.repo.items["Staff of Sigils"] = SimpleNamespace(name="Staff of Sigils", lines=("MAGIC",))
        self.map.npc_zone_requested.emit("Night Harbor (West)")
        self.panel.item_hovered.emit("Staff of Sigils")
        self.assertEqual(self.map.zones["Night Harbor"][0], ("Named Monster",))
        self.assertIs(self.panel.items[item.name], item)
        self.wait_done()

    def test_unnamed_considers_and_disabled_panel_do_not_lookup(self):
        self.controller.on_message(None, SimpleNamespace(kind="consider", target=None))
        self.controller.on_message(None, SimpleNamespace(kind="melee_hit", target="a rat"))
        self.panel.enabled = False
        self.controller.show_npc("a rat")
        self.assertFalse(self.repo.calls)
        self.assertFalse(self.controller._pending)

    def test_shutdown_discards_late_results(self):
        self.controller.show_npc("a rat")
        self.controller.shutdown()
        self.repo.release.set()
        self.controller._finished("npc", "a rat", npc("a rat"), "")
        self.assertIsNone(self.panel.details)

    def test_redirected_item_matches_hover_label(self):
        from mnmparse.npc_wiki import ItemDetails
        canonical = ItemDetails("Staff of Sigils", ("MAGIC",), "https://monstersandmemories.miraheze.org/wiki/Staff_of_Sigils")
        self.repo.items["Old Staff"] = canonical
        self.controller.show_item("Old Staff")
        self.assertEqual(self.panel.items["Old Staff"].lines, ("MAGIC",))
        self.wait_done()
        self.assertEqual(self.panel.items["Old Staff"].source, canonical.source)

    def test_explicit_player_considers_do_not_request_npc_pages(self):
        for line in ("Gozif Battle with them would be risky.",
                     "Gozif You would emerge victorious.",
                     "Birch They look weak. You would likely emerge victorious in battle with them.",
                     "birch--They look weak. You would likely emerge victorious in battle with them.",
                     "You would almost certainly be crushed by them in battle. (PvP Aggro)"):
            self.controller.on_message(None, parse_line(line, 0))
        self.assertEqual(self.panel.name, "")
        self.assertFalse(self.repo.calls)

    def test_named_faction_consider_and_legacy_event_use_positive_npc_identity(self):
        cached = self.repo.npcs["Guard Amber"] = npc("Guard Amber")
        text = ("Guard Amber considers you with kindness -- Would you like to die? "
                "You would almost certainly be defeated in battle.")
        event = parse_line(text, 0)
        self.assertEqual(event.outcome, "npc")
        self.controller.on_message(None, event)
        self.assertEqual(self.panel.name, "Guard Amber")
        self.assertIs(self.panel.details, cached)
        self.controller.on_message(None, SimpleNamespace(kind="consider", target="Guard Amber", text=text))
        self.wait_done()
        self.assertEqual(self.repo.calls.count(("npc", "Guard Amber")), 1)

    def test_unknown_or_mismatched_considers_never_lookup_even_with_stale_npc_marker(self):
        for event in (
            SimpleNamespace(kind="consider", target="Guard Amber"),
            SimpleNamespace(kind="consider", target="Guard Amber", text="Guard Amber unexplained prose.", outcome="npc"),
            SimpleNamespace(kind="consider", target="Guard Amber", text="Guard Amber You would emerge victorious.", outcome="npc"),
            SimpleNamespace(kind="consider", target="Guard Amber", text="Guard Birch considers you with kindness."),
            parse_line('Aster says, "Guard Amber considers you with kindness."', 0),
            parse_line("Birch is dead. Their corpse will decay in 5 days and 19 hours.", 0),
        ):
            with self.subTest(event=event):
                self.controller.on_message(None, event)
        self.assertEqual(self.panel.name, "")
        self.assertFalse(self.controller._pending)
        self.assertFalse(self.repo.calls)

    def test_missing_page_is_distinct_from_outage_and_preserves_cached_details(self):
        self.repo.error = WikiPageMissing("confirmed missing")
        self.controller.show_npc("Guard Amber")
        self.wait_done()
        self.assertEqual(self.panel.status, "No wiki page found for this NPC")
        self.assertIsNone(self.panel.details)
        self.assertEqual(self.controller._last_errors[("npc", "Guard Amber")], "page_missing")
        cached = self.repo.npcs["Guard Birch"] = npc("Guard Birch")
        self.controller.show_npc("Guard Birch")
        self.wait_done()
        self.assertIs(self.panel.details, cached)
        self.assertEqual(self.panel.status, "No wiki page found · showing cached details")

    def test_missing_item_and_zone_labels_are_specific(self):
        self.repo.error = WikiPageMissing("confirmed missing")
        self.controller.show_item("Example Staff")
        self.controller.show_notables("Night Harbor")
        self.wait_done()
        self.assertEqual(self.panel.items["Example Staff"], "No wiki page found for this item")
        self.assertEqual(self.map.zones["Night Harbor"], ((), "No wiki page found for this zone"))

    def test_success_after_error_clears_unavailable_and_retains_cached_first_presentation(self):
        for name, error in (("Guard Amber", OSError("offline")),
                            ("Guard Birch", WikiPageMissing("confirmed missing"))):
            with self.subTest(error=type(error).__name__):
                self.repo.error = error
                self.controller.show_npc(name)
                self.wait_done()
                self.assertIsNone(self.panel.details)
                self.assertTrue(self.panel.status)
                self.assertIn(("npc", name), self.controller._last_errors)
                self.repo.error = False
                fresh = self.repo.npcs[name] = npc(name, level="7")
                self.controller._next_check[("npc", name)] = 0
                self.repo.release.clear()
                self.controller.show_npc(name)
                self.assertIs(self.panel.details, fresh)
                self.assertIn("cached", self.panel.status)
                self.wait_done()
                self.assertIs(self.panel.details, fresh)
                self.assertEqual(self.panel.status, "")
                self.assertNotIn(("npc", name), self.controller._last_errors)

    def test_hover_promotes_queued_prefetch_without_duplicate(self):
        self.controller._started = True  # Hold workers to reproduce a slow backlog.
        for name in ("First", "Hovered", "Last"):
            self.controller._request("item", name, background=True)
        self.controller.show_item("Hovered")
        self.assertEqual(self.controller._queue[0], ("item", "Hovered"))
        self.assertEqual(len(self.controller._queue), 3)

    def test_interactive_hover_is_retained_when_prefetch_queue_is_full(self):
        from mnmparse.app.npc_details import MAX_QUEUED
        self.controller._started = True
        for number in range(MAX_QUEUED + 5):
            self.controller._request("item", f"Prefetch {number}", background=True)
        self.controller.show_item("Hovered")
        self.assertEqual(self.controller._queue[0], ("item", "Hovered"))
        self.assertIn(("item", "Hovered"), self.controller._pending)
        self.assertEqual(len(self.controller._queue), MAX_QUEUED)

    def test_disabling_during_lookup_does_not_start_new_item_requests(self):
        self.controller.show_npc("a rat")
        self.repo.npcs["a rat"] = SimpleNamespace(name="a rat", common_drops=(), unique_drops=("Staff",))
        self.panel.enabled = False
        self.wait_done()
        self.assertEqual(self.repo.calls, [("npc", "a rat")])


if __name__ == "__main__":
    unittest.main()
