"""Session aggregation: loot, coin, crafting, kills, deaths, CC and the personal-lines gate."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import unittest

from mnmparse.parser import parse_line
from mnmparse.session import LootEntry, SessionStats, filter_session, format_coin

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
    (8.5, "Abepulifif has been slain by a skeletal cleric!"),  # explicit party-member kill identified them
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
    def test_mez_breaks_record_targets_without_guessing_from_nearby_damage(self) -> None:
        session = SessionStats(PLAYER, started=0)
        for ts, text in (
                (1, "a rat is mesmerized."),
                (2, "You crush a rat for 20 points of damage."),
                (2.2, "a rat awakens."),
                (10, "a skeleton is mesmerized."),
                (11, "a skeleton awakens."),
                (11.2, "Povebizu slashes a skeleton for 8 points of damage.")):
            session.add(parse_line(text, ts, PLAYER))
        snap = session.snapshot(now=12)
        self.assertEqual(snap.mez_breaks_by_breaker, [])
        self.assertEqual(snap.mez_break_details, [(2.2, "a rat", None), (11.0, "a skeleton", None)])
        self.assertEqual((snap.mez_breaks_total, snap.mez_breaks_unknown), (2, 2))
        from mnmparse.config import Config
        from mnmparse.privacy import project_session
        self.assertEqual(project_session(snap, Config(player_name=PLAYER)).mez_break_details,
                         snap.mez_break_details, "Carebear retains shared target/time events")

    def test_bounded_mez_details_keep_full_target_count_in_all_modes(self) -> None:
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        session = SessionStats(PLAYER, started=0)
        for index, actor in enumerate(("You",) * 5 + ("Povebizu",) * 3):
            ts = index * 10
            for offset, text in ((0, "a rat is mesmerized."),
                                 (1, f"{actor} crushes a rat for 20 points of damage."),
                                 (1.2, "a rat awakens.")):
                session.add(parse_line(text, ts + offset, PLAYER))
        session.add(parse_line("a skeleton awakens.", 90, PLAYER))
        snap = session.snapshot(now=100, detail_limit=2)
        self.assertEqual((snap.mez_breaks_total, len(snap.mez_break_details)), (9, 2))
        self.assertEqual(snap.mez_breaks_by_breaker, [])
        mine = filter_session(snap, PLAYER)
        self.assertEqual(mine.mez_breaks_by_breaker, [])
        self.assertEqual((mine.mez_breaks_total, mine.mez_breaks_unknown), (9, 9))
        safe = project_session(snap, Config(player_name=PLAYER))
        self.assertEqual(safe.mez_breaks_by_breaker, [])
        self.assertEqual(safe.mez_breaks_total, 9)
        self.assertTrue(all(actor is None for _ts, _mob, actor in safe.mez_break_details))
        self.assertNotIn("Povebizu", str(safe))

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


class SessionGroupProjectionTests(unittest.TestCase):
    def snapshot(self):
        session = SessionStats(PLAYER, started=0)
        texts = ["Tovozen has joined the party.", "Mira has joined the party."]
        texts += ["You loot [Bone Chips] from a rat's corpse."] * 12
        texts += ["Tovozen loots [Bone Chips] from a rat's corpse."] * 3
        texts += ["Mira loots [Worn Bow] from a rat's corpse."] * 2
        texts += ["You loot 2 silver coins from a rat's corpse.",
                  "Tovozen loots 50 copper coins from a rat's corpse, and you receive 10 copper coins from a rat's corpse as your split.",
                  "Mira loots 30 copper coins from a rat's corpse.",
                  "Septimal loots 25 copper coins from a rat's corpse.",
                  "Septimal crafts Cloth Scraps(3).", "Septimal has been slain by a rat!"]
        texts += ["Septimal loots [Secret Diamond] from a rat's corpse."] * 8
        for ts, text in enumerate(texts, 1):
            session.add(parse_line(text, ts, PLAYER))
        session.note_player_status({"xp_percent": 50, "xp_gain_points": 2,
                                    "xp_observed_seconds": 300, "xp_points_per_hour": 24})
        snap = session.snapshot(now=100, detail_limit=2)
        snap.party = ["TOVOZEN", "MIRA"]
        snap.rewards = [LootEntry(90, PLAYER, "Own reward", None),
                        LootEntry(91, "Septimal", "Foreign reward", None)]
        return snap

    def test_hidden_outsiders_leave_complete_group_totals_and_self_coin_received(self):
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        raw = self.snapshot()
        cfg = Config(player_name=PLAYER.lower(), casual_mode=False, casual_mode_confirmed=True)
        shown = project_session(raw, cfg)
        self.assertEqual(shown.items, 17)
        self.assertEqual(shown.items_by_name, [("Bone Chips", 15), ("Worn Bow", 2)])
        self.assertEqual(dict(shown.items_by_looter), {PLAYER: 12, "Tovozen": 3, "Mira": 2})
        self.assertEqual(shown.loot, [], "Bounded details contain only outsiders, but totals stay complete")
        self.assertEqual((shown.coin_total, shown.coin_received, shown.coin_split), (280, 210, 10))
        self.assertEqual(shown.rewards, [raw.rewards[0]])
        self.assertEqual((shown.outsider_deaths, shown.outsider_crafts), ([], []))
        self.assertNotIn("Septimal", json.dumps(asdict(shown)))
        mine = filter_session(shown, PLAYER.lower())
        self.assertEqual((mine.items, mine.coin_total), (12, 210))
        self.assertEqual(mine.coin_by_looter, [("Looted by you (gross)", 200)])
        self.assertEqual(shown.player_status, raw.player_status)
        self.assertEqual(raw.items, 25)
        self.assertIn("Septimal", dict(raw.items_by_looter))
        self.assertIs(project_session(raw, replace(cfg, show_other_groups=True)), raw)

    def test_carebear_averages_use_known_group_totals_even_when_other_groups_are_enabled(self):
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        raw = self.snapshot()
        shown = project_session(raw, Config(player_name=PLAYER, show_other_groups=True))
        self.assertEqual(shown.items, 12)
        self.assertEqual(shown.group_average_count, 3)
        self.assertEqual(shown.group_averages["items"], 5.7)
        self.assertEqual(shown.group_averages["coin_total"], 93)
        self.assertNotIn("Secret Diamond", json.dumps(asdict(shown)))
        self.assertEqual(shown.player_status, raw.player_status)

    def test_legacy_aggregate_counters_keep_totals_without_item_allocations_or_own_cache(self):
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        raw = self.snapshot()
        cfg = Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True)
        legacy = replace(raw, item_looters={})
        shown = project_session(legacy, cfg)
        self.assertEqual(shown.items, 17)
        self.assertEqual(shown.items_by_name, [], "Unknown allocation cannot fabricate item details")
        self.assertEqual(filter_session(shown, PLAYER).items, 12)
        counters_missing = replace(raw, items_by_looter=[])
        shown = project_session(counters_missing, cfg)
        self.assertEqual((shown.items, filter_session(shown, PLAYER).items), (17, 12))
        self.assertEqual(project_session(shown, cfg).items, 17)

    def former_member_session(self, change):
        from mnmparse.party import PartyRoster

        roster = PartyRoster(PLAYER)
        session = SessionStats(PLAYER, started=0, roster=roster)
        texts = ["Tovozen has joined the party.", "Former has joined the party."]
        texts += ["You have slain a rat!"] * 2
        texts += ["Your party member Tovozen has slain a beetle!"]
        texts += ["You crafts Own Item(2).", "Tovozen crafts Group Item(3).",
                  "Former crafts Foreign Item(4).", "Tovozen has been slain by a rat!",
                  "Former has been slain by a rat!"]
        texts += ["Your party member Former has slain a wolf!"] * 4
        for ts, text in enumerate(texts, 1):
            event = parse_line(text, ts, PLAYER)
            roster.observe(event)
            session.add(event)
        before = session.snapshot(now=20, detail_limit=1)
        for ts, text in enumerate(change, 30):
            event = parse_line(text, ts, PLAYER)
            roster.observe(event)
            session.add(event)
        return before, session.snapshot(now=40, detail_limit=1)

    def test_leaving_or_disbanding_hides_former_killers_with_complete_bounded_totals(self):
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        cfg = Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True)
        for change in (["Former has left the party."],
                       ["Your party has been disbanded.", "Tovozen has joined the party."]):
            with self.subTest(change=change):
                before, raw = self.former_member_session(change)
                self.assertEqual(raw.party, ["Tovozen"])
                self.assertEqual(raw.kills, 7, "Historical party kills stay in collected data")
                self.assertEqual(raw.kill_entries[0][1], "Former")
                shown = project_session(raw, cfg)
                self.assertEqual(shown.kills, 3)
                self.assertEqual(dict(shown.kills_by_killer), {PLAYER: 2, "Tovozen": 1})
                self.assertEqual(dict(shown.kills_by_target), {"a rat": 2, "a beetle": 1})
                self.assertEqual(shown.kill_entries, [])
                self.assertEqual((shown.crafts, shown.deaths), (5, 1))
                self.assertNotIn("Former", json.dumps(asdict(shown)))
                mine = filter_session(shown, PLAYER)
                self.assertEqual((mine.kills, mine.crafts), (2, 2))
                self.assertEqual(mine.kills_by_target, [("a rat", 2)])
                self.assertEqual(project_session(shown, cfg).kills_by_target, shown.kills_by_target)
                self.assertIs(project_session(raw, replace(cfg, show_other_groups=True)), raw)
                # A cached historical snapshot may arrive before its roster is
                # refreshed. Its current roster still filters every identity.
                before.party = ["Tovozen"]
                historical = project_session(before, cfg)
                self.assertEqual((historical.kills, historical.crafts, historical.deaths), (3, 5, 1))
                self.assertEqual(dict(historical.crafts_by_item), {"Own Item": 2, "Group Item": 3})
                self.assertNotIn("Former", json.dumps(asdict(historical)))

    def test_legacy_bounded_kills_and_crafts_keep_totals_without_fabricating_breakdowns(self):
        from mnmparse.config import Config
        from mnmparse.privacy import project_session

        before, _raw = self.former_member_session(["Former has left the party."])
        legacy = replace(before, party=["Tovozen"])
        cfg = Config(player_name=PLAYER, casual_mode=False, casual_mode_confirmed=True)
        shown = project_session(legacy, cfg)
        self.assertEqual((shown.kills, shown.crafts, shown.deaths), (3, 5, 1))
        self.assertEqual((shown.kills_by_target, shown.crafts_by_item), ([], []))
        self.assertEqual((shown.kill_entries, shown.craft_entries), ([], []))
        self.assertNotIn("Former", json.dumps(asdict(shown)))



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
            (300.0, "Povebizu has joined the party."),
        ])
        self.assertEqual(dict(snap.deaths_by_player), {"Povebizu": 1})

    def test_nearby_loot_does_not_promote_an_outsider_death(self) -> None:
        snap = self.snap([
            (0.0, "Povebizu has been slain by a skeletal defender!"),
            (300.0, "--Povebizu loots [Bone Chips] from a skeletal marksman's corpse.--"),
        ])
        self.assertEqual(snap.deaths_by_player, [])
        self.assertEqual(snap.outsider_deaths, [("Povebizu", 1)])
        self.assertEqual(snap.party, [])

    def test_empty_shared_roster_does_not_restore_former_members_from_session(self) -> None:
        from mnmparse.party import PartyRoster

        roster = PartyRoster(PLAYER)
        session = SessionStats(PLAYER, started=0.0, roster=roster)
        for ts, text in (
            (1.0, "Povebizu has joined the party."),
            (2.0, "--Povebizu loots [Bone Chips] from a skeletal marksman's corpse.--"),
            (3.0, "Your party has been disbanded."),
            (4.0, "Povebizu has been slain by a skeletal defender!"),
        ):
            event = parse_line(text, ts, PLAYER)
            roster.observe(event)
            session.add(event)
        snap = session.snapshot(now=10.0)
        self.assertEqual(snap.party, [])
        self.assertEqual(snap.deaths_by_player, [])
        self.assertEqual(snap.outsider_deaths, [("Povebizu", 1)])

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
