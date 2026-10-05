"""Session aggregation: loot, coin, crafting, kills, deaths, CC and the personal-lines gate."""

from __future__ import annotations

import unittest

from mnmparse.parser import parse_line
from mnmparse.session import SessionStats, format_coin

PLAYER = "Maergoth"

LINES = [
    (0.0, "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--"),
    (1.0, "--Abepulifif loots [Bone Chips] from a skeletal warrior's corpse.--"),
    (2.0, "--Povebizu loots [Worn Bow] from a skeletal marksman's corpse.--"),
    (3.0, "Povebizu loots 7 copper coins from a skeletal warrior's corpse, and you receive 1 copper coin from a skeletal warrior's corpse as your split."),
    (4.0, "You loot 2 silver coins from a goblin scout's corpse."),
    (5.0, "Povebizu crafts Cloth Scraps(3)."),
    (6.0, "Your party member Abepulifif has slain a skeletal marksman!"),
    (7.0, "You have slain a skeletal warrior!"),
    (8.0, "Cigezisi has been slain by a skeletal cleric!"),  # another group nearby: not counted
    (8.5, "Abepulifif has been slain by a skeletal cleric!"),  # a looter, so in the party
    (9.0, "a skeletal defender is mesmerized."),
    (10.0, "a skeletal cleric's casting is interrupted."),
    (11.0, "Fozo is stunned."),
    (12.0, "Your skill in Bludgeoning has increased! (12)"),
    (13.0, "Your faction standing with Easthymn Freebooters got better."),
    (14.0, "You gain party experience!"),
]


def feed(session: SessionStats) -> None:
    for ts, text in LINES:
        session.add(parse_line(text, ts, PLAYER))


class SessionTests(unittest.TestCase):
    def test_counts(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        feed(s)
        s.note_encounter(30.0)
        snap = s.snapshot(now=600.0)
        self.assertEqual(snap.items, 3)
        self.assertEqual(snap.items_by_name[0], ("Bone Chips", 2))
        self.assertEqual(dict(snap.items_by_looter), {"Abepulifif": 2, "Povebizu": 1})
        self.assertEqual(snap.coin_total, 7 + 200)  # 2 silver = 200 copper
        self.assertEqual(dict(snap.coin_by_looter), {"Povebizu": 7, PLAYER: 200})
        self.assertEqual(snap.coin_split, 1)
        self.assertEqual(snap.crafts, 3)
        self.assertEqual(snap.kills, 2)
        self.assertEqual(dict(snap.kills_by_killer), {"Abepulifif": 1, PLAYER: 1})
        self.assertEqual(snap.deaths, 1)
        self.assertEqual(dict(snap.deaths_by_player), {"Abepulifif": 1})
        self.assertEqual(snap.outsider_deaths, [("Cigezisi", 1)])
        self.assertEqual(snap.cc_total, 3)
        self.assertEqual(dict(snap.cc_by_type), {"interrupt": 1, "stun": 1, "mez": 1})
        self.assertEqual((snap.cc_on_npcs, snap.cc_on_players), (2, 1))
        self.assertEqual(snap.encounters, 1)
        self.assertEqual(snap.combat_seconds, 30.0)
        self.assertAlmostEqual(snap.kills_per_hour, 12.0)
        self.assertEqual(len(snap.recent), 13)  # personal lines excluded by default

    def test_personal_lines_excluded_by_default(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        feed(s)
        snap = s.snapshot(now=100.0)
        self.assertFalse(snap.personal_included)
        self.assertEqual(snap.skill_ups, [])
        self.assertEqual(snap.faction, [])

    def test_personal_lines_included_when_enabled(self) -> None:
        s = SessionStats(PLAYER, include_personal=True, started=0.0)
        feed(s)
        snap = s.snapshot(now=100.0)
        self.assertEqual(snap.skill_ups, [("Bludgeoning", 12)])
        self.assertEqual(snap.faction, [("Easthymn Freebooters", 1)])
        self.assertEqual(snap.xp_ticks, 1)

    def test_format_coin(self) -> None:
        self.assertEqual(format_coin(0), "0c")
        self.assertEqual(format_coin(7), "7c")
        self.assertEqual(format_coin(234), "2s 34c")
        self.assertEqual(format_coin(10_203), "1g 2s 3c")
        self.assertEqual(format_coin(1_020_304), "1p 2g 3s 4c")
        self.assertEqual(format_coin(2_000_000), "2p")



class DeathAndKillTests(unittest.TestCase):
    """The death counter (2026-10-02 audit): only the party, named mobs are kills,
    re-read lines count once, Feign Death is not a death."""

    def snap(self, lines: list[tuple[float, str]]):
        s = SessionStats(PLAYER, started=0.0)
        for ts, text in lines:
            s.add(parse_line(text, ts, PLAYER))
        return s.snapshot(now=1000.0)

    def test_named_mob_killed_by_the_party_is_a_kill(self) -> None:
        snap = self.snap([
            (0.0, "Your party member Gozif has slain Grandmaster Obadiah!"),
            (5.0, "You have slain Grandmaster Gildas!"),
        ])
        self.assertEqual(snap.deaths, 0)
        self.assertEqual(snap.kills, 2)
        self.assertEqual(dict(snap.kills_by_target), {"Grandmaster Obadiah": 1, "Grandmaster Gildas": 1})

    def test_other_groups_do_not_count(self) -> None:
        snap = self.snap([
            (0.0, "Your party member Gozif has slain a skeletal warrior!"),
            (0.5, "a skeletal monk has been slain by Sopurimem!"),  # Sopurimem plays nearby
            (1.0, "Sopurimem has been slain by Grandmaster Obadiah!"),
            (2.0, "a skeletal warrior has been slain by Cigezisi!"),
            (3.0, "Modavarug has been slain by a dunes scarab!"),
        ])
        self.assertEqual((snap.deaths, snap.kills), (0, 1))
        self.assertEqual(dict(snap.outsider_deaths), {"Sopurimem": 1, "Modavarug": 1})
        self.assertEqual(snap.outsider_kills, 2)
        self.assertEqual(snap.party, ["Gozif"])

    def test_a_reread_death_counts_once(self) -> None:
        snap = self.snap([
            (0.0, "Your party member Dogabetarolem has slain a skeletal fighter!"),
            (10.0, "Dogabetarolem has been slain by a skeletal warrior!"),
            (82.0, "Dogabetarolem has been slain by a skeletal warrior!"),  # 72 s later: a second death
            (100.0, "Dogabetarolem has been slain by a skeletal warrior!"),  # 18 s later: the same one again
            (101.0, "You have been slain by a skeletal priest!"),
            (130.0, "You have been slain bv a skeletal priest!"),  # OCR "bv", 29 s later
        ])
        self.assertEqual(dict(snap.deaths_by_player), {"Dogabetarolem": 2, PLAYER: 1})

    def test_party_member_recognised_after_their_death(self) -> None:
        snap = self.snap([
            (0.0, "Povebizu has been slain by a skeletal defender!"),
            (300.0, "--Povebizu loots [Bone Chips] from a skeletal marksman's corpse.--"),
        ])
        self.assertEqual(dict(snap.deaths_by_player), {"Povebizu": 1})

    def test_feign_death_is_not_a_death(self) -> None:
        snap = self.snap([
            (0.0, "Davina begins casting Feign Death."),
            (1.0, "Davina has died."),
        ])
        self.assertEqual((snap.deaths, snap.outsider_deaths), (0, []))

    def test_vocabulary_folds_misread_items_and_names(self) -> None:
        from mnmparse.vocab import Vocabulary

        vocab = Vocabulary()
        from mnmparse.stats import Stats

        stats = Stats(12.0, vocab=vocab)
        s = SessionStats(PLAYER, started=0.0, vocab=vocab)
        lines = [(float(i), "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--") for i in range(6)]
        lines += [(10.0, "--Abepulifif loots [Bone Ohips] from a skeletal marksman's corpse.--")]
        lines += [(11.0, "--Abepulifcf loots [Bone Chips] from a skeletal marksman's corpse.--")]
        for ts, text in lines:
            ev = parse_line(text, ts, PLAYER)
            stats.add(ev)  # feeds the vocabulary, as the engine does
            s.add(ev)
        snap = s.snapshot(now=60.0)
        self.assertEqual(snap.items_by_name, [("Bone Chips", 8)])
        self.assertEqual(snap.items_by_looter, [("Abepulifif", 8)])
        self.assertEqual(snap.item_looters["Bone Chips"], [("Abepulifif", 8)])

if __name__ == "__main__":
    unittest.main()
