"""Pet ownership, per-owner output, and expandable per-person loot."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from mnmparse.app.models import build_snapshot, merge_snapshots, owner_row, self_rows
from mnmparse.config import Config
from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.session import SessionStats
from mnmparse.stats import Stats

PLAYER = "Maergoth"


def fight() -> Stats:
    stats = Stats(player_name=PLAYER)
    for ts, line in enumerate([
        "Tamsin has joined the party.",
        "You crush a rat for 20 points of damage.",
        "Kulepu slashes a rat for 30 points of damage.",
        "Tamsin crushes a rat for 10 points of damage.",
    ]):
        stats.add(parse_line(line, 100.0 + ts, PLAYER))
    return stats


class AttributionTests(unittest.TestCase):
    def test_saved_owner_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "party.json"
            roster = PartyRoster(PLAYER)
            roster.set_pet_owner("a charmed rat", "Tamsin")
            roster.set_pet_owner("Kulepu", "You")
            roster.set_pet_owner("Tamsin", "Kulepu")  # ownership cycles rejected
            roster.save(path)
            loaded = PartyRoster(PLAYER)
            loaded.load(path)
            self.assertEqual(loaded.pet_owners(), {"a charmed rat": "Tamsin", "Kulepu": PLAYER})
            loaded.set_pet_owner("Kulepu", None)
            self.assertNotIn("Kulepu", loaded.pet_owners())

    def test_group_follows_owner_and_rollup_never_duplicates_total(self) -> None:
        stats = fight()
        stats.roster.set_pet_owner("Kulepu", "Tamsin")
        snap = build_snapshot(stats, stats.current(), PLAYER)
        pet = next(r for r in snap.rows if r.name == "Kulepu")
        self.assertTrue(pet.is_pet and pet.in_group)
        self.assertEqual(pet.pet_owner, "Tamsin")
        self.assertFalse(pet.misses_shown, "another player's pet misses may be hidden")
        self.assertEqual(snap.total_damage, 60)
        self.assertEqual(owner_row(snap, "Tamsin").damage, 40)
        self.assertEqual(sum(r.damage for r in snap.rows if r.in_group), 60)
        stats.roster.set_manual("Tamsin", False)
        excluded = build_snapshot(stats, stats.current(), PLAYER)
        self.assertEqual(excluded.total_damage, 20)
        self.assertFalse(next(r for r in excluded.rows if r.name == "Kulepu").in_group)

    def test_your_pet_in_self_breakdown_and_zone_summary(self) -> None:
        stats = fight()
        stats.roster.set_pet_owner("Kulepu", PLAYER)
        snap = build_snapshot(stats, stats.current(), PLAYER)
        mine = self_rows(snap, "damage", PLAYER)
        self.assertEqual(sum(r.damage for r in mine), 50)
        self.assertTrue(any(r.name.startswith("Kulepu: ") for r in mine))
        self.assertEqual(owner_row(snap, PLAYER).damage, 50)
        summary = merge_snapshots([snap, snap], key="zone", label="zone")
        self.assertEqual(owner_row(summary, PLAYER).damage, 100)
        self.assertIn("Tamsin", summary.group_members)

    def test_assigned_npc_pet_opens_fight_without_owner_acting(self) -> None:
        stats = Stats(player_name=PLAYER)
        stats.roster.set_pet_owner("a charmed rat", PLAYER)
        stats.add(parse_line("a charmed rat bites a bat for 12 points of damage.", 100, PLAYER))
        self.assertIsNotNone(stats.current())
        snap = build_snapshot(stats, stats.current(), PLAYER)
        pet = next(r for r in snap.rows if r.name == "a charmed rat")
        self.assertTrue(snap.ours and pet.in_group and pet.is_pet)
        self.assertFalse(pet.is_enemy or pet.is_npc)
        self.assertEqual(snap.total_damage, 12)
        self.assertEqual(sum(r.damage for r in self_rows(snap, "damage", PLAYER)), 12)
        self.assertEqual(snap.label, "a bat")

    def test_saved_npc_pet_is_applied_before_importing_events(self) -> None:
        from mnmparse.importer import import_file, import_files
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pet.log"
            path.write_text("[Mon Oct 05 12:00:00 2026] a charmed rat bites a bat for 12 points of damage.\n", encoding="utf-8")
            options = dict(player_name=PLAYER, pet_owners={"a charmed rat": PLAYER})
            for result in (import_file(path, **options), import_files([path], **options)[0]):
                self.assertEqual(len(result.encounters), 1)
                self.assertEqual(result.encounters[0].total_damage, 12)
                self.assertTrue(result.encounters[0].ours)


class AttributionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_menu_allows_npc_and_absent_member_and_clear(self) -> None:
        from PySide6.QtWidgets import QMenu
        from mnmparse.app.overlay import add_pet_entries
        stats = fight()
        stats.roster.set_manual("Wenna", True)
        snap = build_snapshot(stats, stats.current(), PLAYER)
        row = next(r for r in snap.rows if r.name == "a rat")
        calls = []
        menu = QMenu()
        handlers = add_pet_entries(menu, row, snap, lambda *args: calls.append(args))
        chosen = next(action for action in handlers if action.text() == "Wenna")
        handlers[chosen]()
        self.assertEqual(calls, [("a rat", "Wenna")])
        menu.close()

    def test_reassign_refreshes_recent_closed_fight_even_same_group(self) -> None:
        from mnmparse.app.engine import Engine
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(Config(player_name=PLAYER, log_dir=directory))
            stats = engine._install_stats(engine.config)
            now = time.time()
            for offset, line in enumerate([
                "Tamsin has joined the party.",
                "You crush a rat for 20 points of damage.",
                "Kulepu slashes a rat for 30 points of damage.",
            ]):
                stats.add(parse_line(line, now - 3 + offset, PLAYER))
            stats.roster.set_pet_owner("Kulepu", PLAYER)
            enc = stats.expire(now + 30)
            engine._history.append(build_snapshot(stats, enc, PLAYER))
            updates = []
            engine.encounter_updated.connect(updates.append)
            engine.set_pet_owner("Kulepu", "Tamsin")
            self.assertEqual(len(updates), 1)
            self.assertEqual(next(r for r in updates[0].rows if r.name == "Kulepu").pet_owner, "Tamsin")
            engine._finish()
            engine.set_pet_owner("Kulepu", PLAYER)
            self.assertEqual(len(updates), 2)
            self.assertEqual(next(r for r in updates[1].rows if r.name == "Kulepu").pet_owner, PLAYER)

    def test_imported_selected_fight_rebuilds_and_restores_clear(self) -> None:
        from PySide6.QtCore import QSettings
        from mnmparse.app.engine import Engine
        from mnmparse.app.pages import LivePage
        with tempfile.TemporaryDirectory() as directory:
            cfg = Config(player_name=PLAYER, log_dir=directory)
            engine = Engine(cfg)
            engine.set_pet_owner("Kulepu", PLAYER)
            path = Path(directory) / "example.log"
            path.write_text("\n".join(f"[Mon Oct 05 12:00:0{i} 2026] {line}" for i, line in enumerate([
                "Tamsin has joined the party.", "You crush a rat for 20 points of damage.",
                "Kulepu slashes a rat for 30 points of damage.", "Tamsin crushes a rat for 10 points of damage.",
            ])), encoding="utf-8")
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            page = LivePage(engine, cfg, settings)
            page.show()
            self.assertEqual(page.import_files([str(path)]), (1, 0))
            self.assertEqual(next(r for r in page.selected().rows if r.name == "Kulepu").pet_owner, PLAYER)
            page.set_pet_owner("Kulepu", "Tamsin")
            self.assertEqual(next(r for r in page.selected().rows if r.name == "Kulepu").pet_owner, "Tamsin")
            page.set_pet_owner("Kulepu", None)
            pet = next(r for r in page.selected().rows if r.name == "Kulepu")
            self.assertFalse(pet.is_pet or pet.in_group)
            page.close()

    def test_loot_subtotals_and_expansion_survive_refresh(self) -> None:
        from mnmparse.app.session_view import SessionView
        session = SessionStats(PLAYER, started=100)
        for offset, (looter, item) in enumerate([
            ("Tamsin", "Bone Chips"), ("Tamsin", "Bone Chips"),
            ("Tamsin", "Rat Tail"), ("Wenna", "Bone Chips"),
        ]):
            session.add(parse_line(f"--{looter} loots [{item}] from a rat's corpse.--", 100 + offset, PLAYER))
        for compact in (False, True):
            view = SessionView(compact=compact)
            snap = session.snapshot(now=110)
            view.set_snapshot(snap)
            top = view._tree.topLevelItem(0)
            self.assertEqual(top.text(1), "4")
            self.assertEqual([(top.child(i).text(0), top.child(i).text(1)) for i in range(2)],
                             [("Tamsin", "3"), ("Wenna", "1")])
            person = top.child(0)
            self.assertEqual([(person.child(i).text(0), person.child(i).text(1)) for i in range(2)],
                             [("Bone Chips", "2"), ("Rat Tail", "1")])
            person.setExpanded(True)
            view.set_snapshot(snap)
            self.assertTrue(view._tree.topLevelItem(0).child(0).isExpanded())
            view.close()


if __name__ == "__main__":
    unittest.main()
