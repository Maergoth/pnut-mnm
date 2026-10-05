"""Self-view rows, per-source breakdowns, the Session category tree and the overlay's
lock / click-through semantics."""

from __future__ import annotations

import os
import sys
import unittest

from mnmparse.app.models import build_snapshot, self_rows
from mnmparse.parser import parse_line
from mnmparse.session import SessionStats
from mnmparse.stats import Stats

PLAYER = "Maergoth"

LINES = [
    (0.0, "You crush a skeletal cleric for 5 points of damage."),
    (1.0, "You crush a skeletal cleric for 7 points of damage."),
    (2.0, "Your Crusader Strike hits a skeletal cleric for 12 points of damage."),
    (3.0, "You try to crush a skeletal cleric, but miss!"),
    (4.0, "Your Lesser Healing Touch heals you for 22 Health."),
    (5.0, "Tovozen's Heal heals you for 57 Health."),
    (6.0, "a skeletal cleric hits YOU for 9 points of damage."),
    (7.0, "a skeletal cleric's Shock hits YOU for 4 points of Holy Damage!"),
    (8.0, "You kick a skeletal cleric."),
    (8.5, "a skeletal cleric's casting is interrupted."),
]


def _snap():
    stats = Stats(encounter_timeout_s=12.0)
    for ts, text in LINES:
        stats.add(parse_line(text, ts, PLAYER))
    enc = stats.current() or stats.history[-1]
    return build_snapshot(stats, enc, PLAYER)


class SelfRowsTests(unittest.TestCase):
    def test_breakdowns_on_the_player_row(self) -> None:
        snap = _snap()
        me = next(r for r in snap.rows if r.is_you)
        self.assertEqual([(s.skill, s.total) for s in me.skills], [("Crusader Strike", 12), ("crush", 12)])
        self.assertEqual([(s.skill, s.total, s.hits) for s in me.heal_skills], [("Lesser Healing Touch", 22, 1)])
        self.assertEqual(
            [(s.skill, s.total) for s in me.taken_from],
            [("a skeletal cleric: hit", 9), ("a skeletal cleric: Shock", 4)],
        )
        self.assertEqual(me.cc_skills, {"kick": 1})

    def test_self_rows_per_tab(self) -> None:
        snap = _snap()
        damage = self_rows(snap, "damage", PLAYER)
        self.assertEqual([r.name for r in damage], ["Crusader Strike", "crush"])
        self.assertAlmostEqual(sum(r.share for r in damage), 1.0)
        crush = damage[1]
        self.assertEqual((crush.hits, crush.misses, crush.swings), (2, 1, 3))
        healing = self_rows(snap, "healing", PLAYER)
        self.assertEqual([(r.name, r.heals) for r in healing], [("Lesser Healing Touch", 22)])
        taken = self_rows(snap, "taken", PLAYER)
        self.assertEqual([(r.name, r.taken) for r in taken], [("a skeletal cleric: hit", 9), ("a skeletal cleric: Shock", 4)])
        overview = self_rows(snap, "overview", PLAYER)
        names = {r.name: r for r in overview}
        self.assertEqual(names["kick"].cc, 1)
        self.assertEqual(names["Lesser Healing Touch"].heals, 22)

    def test_no_player_row_gives_no_rows(self) -> None:
        stats = Stats(12.0)
        stats.add(parse_line("Tovozen crushes a skeletal cleric for 5 points of damage.", 0.0, PLAYER))
        snap = build_snapshot(stats, stats.current(), PLAYER)
        self.assertEqual(self_rows(snap, "damage", PLAYER), [])


class SessionDataTests(unittest.TestCase):
    def test_item_looters_and_zones(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        for ts, text in [
            (0, "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--"),
            (1, "--Povebizu loots [Bone Chips] from a skeletal warrior's corpse.--"),
            (2, "--Abepulifif loots [Bone Chips] from a skeletal warrior's corpse.--"),
            (3, "You have entered Night Harbor (East)."),
            (4, "Entering Night Harbor (East)."),
            (5, "You have entered Wyrmsbane Tomb."),
        ]:
            s.add(parse_line(text, float(ts), PLAYER))
        snap = s.snapshot(now=10.0)
        self.assertEqual(snap.item_looters["Bone Chips"], [("Abepulifif", 2), ("Povebizu", 1)])
        self.assertEqual(snap.zones, ["Night Harbor (East)", "Wyrmsbane Tomb"])


@unittest.skipUnless(sys.platform == "win32", "Qt widgets are exercised on the Windows build")
class QtViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_session_tree_categories(self) -> None:
        from PySide6.QtCore import Qt

        from mnmparse.app.session_view import SessionView

        s = SessionStats(PLAYER, started=0.0)
        s.add(parse_line("--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--", 0.0, PLAYER))
        s.add(parse_line("a skeletal defender is mesmerized.", 1.0, PLAYER))  # combat: must not appear
        view = SessionView(compact=True)
        view.set_snapshot(s.snapshot(now=5.0))
        tree = view._tree
        titles = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
        self.assertEqual(titles, ["Items looted", "Coin looted", "Kills", "Deaths", "Crafted", "Zones", "Mez breaks"])
        items = tree.topLevelItem(0)
        self.assertEqual(items.text(1), "1")
        self.assertEqual(items.child(0).text(0), "Abepulifif")
        self.assertEqual(items.child(0).text(1), "1")
        self.assertEqual(items.child(0).child(0).text(0), "Bone Chips")
        self.assertIn("Abepulifif", items.toolTip(0))
        self.assertTrue(items.isExpanded())
        self.assertEqual(items.data(0, Qt.ItemDataRole.UserRole), "items")

    def test_overlay_lock_and_click_through_flags(self) -> None:
        from PySide6.QtCore import QSettings, Qt

        from mnmparse.app.overlay import OverlayWindow
        from mnmparse.config import Config

        settings = QSettings("mnmparse", "MnM Parser-tests")
        settings.clear()
        overlay = OverlayWindow(settings, Config())
        self.assertTrue(overlay.locked)
        self.assertFalse(overlay.click_through)
        self.assertFalse(bool(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput))
        overlay.set_click_through(True)
        self.assertTrue(bool(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput))
        overlay.set_click_through(False)
        self.assertFalse(bool(overlay.windowFlags() & Qt.WindowType.WindowTransparentForInput))
        self.assertTrue(bool(overlay.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus))
        self.assertEqual(overlay.view_mode, "group")
        overlay.toggle_view_mode()
        self.assertEqual(overlay.view_mode, "self")
        overlay.set_snapshot(_snap())
        overlay.set_tab("damage")
        settings.clear()


class OtherGroupsTests(unittest.TestCase):
    """The Combat chat shows other groups fighting nearby; their fights are not ours."""

    def _snap(self, lines):
        stats = Stats(encounter_timeout_s=12.0)
        for ts, text in lines:
            stats.add(parse_line(text, ts, PLAYER))
        enc = stats.current() or stats.history[-1]
        return build_snapshot(stats, enc, PLAYER)

    def test_a_fight_without_the_group_is_not_ours(self) -> None:
        snap = self._snap([(0.0, "Cigezisi crushes a skeletal warrior for 5 points of damage.")])
        self.assertFalse(snap.ours)

    def test_the_viewer_or_a_party_member_makes_it_ours(self) -> None:
        self.assertTrue(self._snap([(0.0, "You crush a skeletal warrior for 5 points of damage.")]).ours)
        snap = self._snap([
            (0.0, "Povebizu has joined the party."),
            (60.0, "Povebizu slashes a skeletal warrior for 9 points of damage."),
        ])
        self.assertTrue(snap.ours)


class UtilityTests(unittest.TestCase):
    def test_debuffs_aggro_and_prevented(self) -> None:
        stats = Stats(encounter_timeout_s=12.0)
        for ts, text in [
            (0.0, "You crush a skeletal fighter for 5 points of damage."),  # opens the encounter
            (0.5, "Povebizu begins casting Interdiction."),
            (1.5, "a skeletal fighter is condemned."),  # cast had no target: credited through the debuff-spell list
            (2.0, "Sididuzek's Barbed Arrow bleeds a skeletal fighter for 3 points of Bleed Damage."),
            (2.5, "a skeletal fighter is struck by a barbed arrow."),
            (3.0, "Pidef's Slice hits a skeletal fighter for 4 points of Bleed Damage."),
            (3.5, "a skeletal fighter is bleeding out."),
            (4.0, "a skeletal fighter looks angry at Wululiso."),
            (5.0, "Dogabetarolem kicks a skeletal fighter."),
            (5.5, "a skeletal fighter's casting is interrupted."),
            (5.6, "a skeletal fighter looks mentally exhausted."),  # lockout: folded into the interrupt
            (6.0, "a skeletal fighter crushes Wululiso for 7 points of damage. (Block 6)"),
            (7.0, "a skeletal fighter pierces Wululiso for 19 points of damage (1 absorbed)."),
        ]:
            stats.add(parse_line(text, ts, PLAYER))
        snap = build_snapshot(stats, stats.current() or stats.history[-1], PLAYER)
        rows = {r.name: r for r in snap.rows}
        self.assertEqual(rows["Povebizu"].debuffs, {"condemned": 1})
        self.assertEqual(rows["Sididuzek"].debuffs, {"barbed arrow": 1})
        self.assertEqual(rows["Pidef"].debuffs, {"bleeding": 1})
        self.assertEqual((rows["Dogabetarolem"].cc, rows["Dogabetarolem"].utility), (1, 1))
        self.assertEqual((rows["Wululiso"].aggro, rows["Wululiso"].utility), (1, 1))
        self.assertEqual(rows["Wululiso"].prevented, 7)
        self.assertEqual(rows["Wululiso"].taken, 26)
        self.assertEqual(rows["Povebizu"].utility, 1)
        self.assertEqual(rows["Povebizu"].debuff_skills, {"Interdiction": 1})

    def test_session_self_filter_and_mez_breaks(self) -> None:
        from mnmparse.session import filter_session

        s = SessionStats(PLAYER, started=0.0)
        for ts, text in [
            (0, "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--"),
            (1, "--You loot [Worn Bow] from a skeletal marksman's corpse.--"),
            (2, "You loot 3 copper coins from a skeletal marksman's corpse."),
            (3, "Povebizu loots 9 copper coins from a skeletal warrior's corpse, and you receive 2 copper coins from a skeletal warrior's corpse as your split."),
            (4, "You have slain a skeletal warrior!"),
            (5, "Your party member Abepulifif has slain a skeletal marksman!"),
            (6, "a skeletal warrior awakens."),
            (7, "You craft Heavy Cloth Bandage."),
        ]:
            s.add(parse_line(text, float(ts), PLAYER))
        snap = s.snapshot(now=10.0)
        self.assertEqual(snap.items, 2)
        self.assertEqual(snap.mez_breaks, [(6.0, "a skeletal warrior")])
        mine = filter_session(snap, PLAYER)
        self.assertEqual(mine.items, 1)
        self.assertEqual(mine.items_by_name, [("Worn Bow", 1)])
        self.assertEqual(mine.coin_total, 3 + 2, "received: the solo loot plus the split from Povebizu's")
        self.assertEqual(mine.coin_by_looter, [("Looted by you (gross)", 3)])
        self.assertEqual(mine.coin_split, 2)
        self.assertEqual((mine.kills, mine.kills_by_target), (1, [("a skeletal warrior", 1)]))
        self.assertEqual(mine.crafts, 1)
        self.assertEqual(mine.mez_breaks, snap.mez_breaks)


if __name__ == "__main__":
    unittest.main()
