"""Second audit (2026-10-03): fight snapshots, the Session tab and the clipboard line.

Fight labels name who the group fought, heals on enemies stay out of the group's healing, the
rates are per second of the group's own fighting (``active_duration`` for the clipboard), the
new debuff types find their abilities, and the Session counts the party's crafts, the coin the
viewer received, wrapped split lines, quest rewards and the meter's party roster.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from mnmparse.app import models
from mnmparse.app.models import build_snapshot, merge_snapshots
from mnmparse.export import PRESETS, format_snapshot
from mnmparse.grammar import Event
from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.session import LOOTED_BY_YOU, SessionStats, filter_session
from mnmparse.stats import Encounter, Stats
from mnmparse.vocab import Vocabulary

PLAYER = "Pidef"


def _feed(stats: Stats, lines: list[tuple[float, str]]) -> None:
    for ts, text in lines:
        stats.add(parse_line(text, ts, PLAYER))


def _rows(snap) -> dict:
    return {r.name: r for r in snap.rows}


def _hit(ts: float, actor: str, target: str, amount: int, skill: str | None = None) -> Event:
    kind = "ability_hit" if skill else "melee_hit"
    return Event(ts=ts, kind=kind, text=f"{actor} hits {target}", actor=actor, target=target, amount=amount,
                 skill=skill or "hit")


class FightLabelTests(unittest.TestCase):
    """#16: PvP fights are named after the attackers, never after the group or bystanders."""

    def test_pvp_is_named_after_the_attackers(self) -> None:
        stats = Stats(8.0)
        _feed(stats, [
            (0.0, "Zekabu crushes YOU for 10 points of damage."),
            (1.0, "You crush Zekabu for 12 points of damage."),
            (2.0, "Ravotin crushes YOU for 8 points of damage."),
            (3.0, "a large rat bites YOU for 1 point of damage."),
        ])
        snap = build_snapshot(stats, stats.current(), PLAYER)
        self.assertEqual(snap.label, "Ravotin, Zekabu, a large rat")
        self.assertNotIn(PLAYER, snap.label)

    def test_party_and_outsiders_are_never_in_the_label(self) -> None:
        stats = Stats(8.0)
        stats.add(parse_line("Tovozen has joined the party.", 0.0, PLAYER))
        _feed(stats, [
            (1.0, "Zekabu crushes Tovozen for 9 points of damage."),
            (2.0, "Tovozen crushes Zekabu for 7 points of damage."),
            (3.0, "Imbor crushes a caiman for 4 points of damage."),  # someone else nearby
            (4.0, "a caiman bites Imbor for 2 points of damage."),
        ])
        snap = build_snapshot(stats, stats.current(), PLAYER)
        self.assertEqual(snap.label, "Zekabu, a caiman")

    def test_a_misread_self_hit_does_not_name_the_player(self) -> None:
        stats = Stats(8.0)
        _feed(stats, [
            (0.0, "Tovozen crushes a risen officer for 6 points of damage."),
            (1.0, "Tovozen's Rebuke hits Tovozen for 16 points of Holy Damage."),  # OCR misread
        ])
        self.assertEqual(build_snapshot(stats, stats.current(), PLAYER).label, "a risen officer")

    def test_a_fight_without_targets_is_unknown(self) -> None:
        enc = Encounter(start=0.0, end=0.0)
        self.assertEqual(build_snapshot(Stats(), enc, PLAYER).label, "unknown")


class EnemyHealTests(unittest.TestCase):
    """#16: a heal landing on an enemy is counted on its own, not as the group's healing."""

    def setUp(self) -> None:
        stats = Stats(8.0)
        _feed(stats, [
            (0.0, "Zekabu crushes YOU for 10 points of damage."),
            (1.0, "You crush Zekabu for 12 points of damage."),
            (2.0, "Your Restorative Smite heals Zekabu for 47 Health."),  # it landed on the attacker
            (4.0, "Your Lesser Healing Touch heals Tovozen for 30 Health."),
            (6.0, "Your Lesser Healing Touch heals you for 20 Health."),
            (7.0, "You crush Zekabu for 8 points of damage."),
        ])
        self.snap = build_snapshot(stats, stats.current(), PLAYER)

    def test_healing_on_the_enemy_is_moved_out(self) -> None:
        me = _rows(self.snap)[PLAYER]
        self.assertTrue(_rows(self.snap)["Zekabu"].is_enemy)
        self.assertEqual(me.heals, 50)
        self.assertEqual(me.enemy_heals, 47)
        self.assertEqual(me.max_heal, 30)
        self.assertAlmostEqual(me.hps, round(50 / self.snap.duration, 2))
        self.assertEqual([(s.skill, s.total, s.hits) for s in me.heal_skills], [("Lesser Healing Touch", 50, 2)])
        self.assertEqual(_rows(self.snap)["Zekabu"].healed, 47, "the enemy still received it")

    def test_an_enemys_own_healing_is_untouched(self) -> None:
        stats = Stats(8.0)
        _feed(stats, [
            (0.0, "You crush a skeletal cleric for 5 points of damage."),
            (1.0, "a skeletal cleric's Minor Heal heals a skeletal cleric for 10 Health."),
        ])
        cleric = _rows(build_snapshot(stats, stats.current(), PLAYER))["a skeletal cleric"]
        self.assertEqual((cleric.heals, cleric.enemy_heals), (10, 0))

    def test_zone_summary_adds_enemy_heals(self) -> None:
        merged = merge_snapshots([self.snap, self.snap], key="z", label="zone")
        self.assertEqual(_rows(merged)[PLAYER].enemy_heals, 94)


class GroupTimeTests(unittest.TestCase):
    """#14: the rates are per second of the group's own first to last swing or hit."""

    @staticmethod
    def _encounter(closed: bool = True) -> Encounter:
        enc = Encounter(start=0.0, end=20.0, last_activity=20.0, closed=closed)
        enc.events = [
            _hit(0.0, "a dunes madman", "a famished zombie", 24),  # NPCs fighting each other
            _hit(5.0, "a famished zombie", "a dunes madman", 12),
            _hit(10.0, PLAYER, "a dunes madman", 20),
            _hit(12.0, "a dunes madman", PLAYER, 6),
            _hit(15.0, "Tovozen", "a dunes madman", 30, "Holy Strike"),
            _hit(18.0, "a dunes madman", PLAYER, 3, "Weak Poison"),  # ticks after the last blow
            _hit(20.0, "a dunes madman", PLAYER, 3, "Weak Poison"),
        ]
        return enc

    def test_leading_and_trailing_spans_without_the_group_are_trimmed(self) -> None:
        snap = build_snapshot(Stats(), self._encounter(), PLAYER)
        self.assertEqual((snap.start, snap.end), (0.0, 20.0), "the encounter's start stays for display")
        self.assertAlmostEqual(snap.duration, 5.0)
        self.assertAlmostEqual(snap.active_duration, 5.0)
        self.assertEqual(snap.total_damage, 50)
        self.assertAlmostEqual(snap.raid_dps, 10.0)
        self.assertAlmostEqual(_rows(snap)[PLAYER].dps, 4.0)
        self.assertAlmostEqual(_rows(snap)[PLAYER].dtps, round(12 / 5.0, 2))

    def test_an_open_fight_still_ticks_from_the_groups_first_swing(self) -> None:
        snap = build_snapshot(Stats(), self._encounter(closed=False), PLAYER, now=25.0)
        self.assertAlmostEqual(snap.duration, 15.0)
        self.assertAlmostEqual(snap.active_duration, 5.0, msg="never ticks to now")
        self.assertEqual(snap.end, 25.0)

    def test_outsiders_swings_do_not_stretch_the_fight(self) -> None:
        stats = Stats(8.0)
        stats.add(parse_line("Tovozen has joined the party.", 0.0, PLAYER))
        _feed(stats, [
            (1.0, "You crush a caiman for 10 points of damage."),
            (3.0, "Tovozen crushes a caiman for 10 points of damage."),
            (7.0, "Imbor crushes a caiman for 30 points of damage."),  # another group's swing
        ])
        stats.expire(30.0)
        snap = build_snapshot(stats, stats.history[-1], PLAYER)
        self.assertAlmostEqual(snap.duration, 2.0)
        self.assertAlmostEqual(snap.raid_dps, 10.0)

    def test_a_fight_the_group_never_swung_in_keeps_its_own_span(self) -> None:
        enc = Encounter(start=0.0, end=6.0, last_activity=6.0, closed=True)
        enc.events = [_hit(0.0, "Imbor", "a caiman", 5), _hit(6.0, "Imbor", "a caiman", 5)]
        stats = Stats()
        stats.roster.set_manual("Tovozen", True)  # a known party, without Imbor
        snap = build_snapshot(stats, enc, PLAYER)
        self.assertFalse(snap.ours)
        self.assertAlmostEqual(snap.duration, 6.0)
        self.assertAlmostEqual(snap.active_duration, 6.0)


class ClipboardTimeTests(unittest.TestCase):
    """#36: a copy taken while the fight is open reads like the one taken when it closes."""

    LINES = [
        (100.0, "You crush a caiman for 40 points of damage."),
        (100.0, "Tovozen's Holy Strike hits a caiman for 30 points of Holy Damage."),
        (102.0, "You crush a caiman for 20 points of damage."),
        (103.0, "Tovozen's Heal heals you for 16 Health."),
    ]

    def test_copy_mid_fight_equals_copy_at_close(self) -> None:
        stats = Stats(8.0)
        _feed(stats, self.LINES)
        live = build_snapshot(stats, stats.current(), PLAYER, now=107.0)
        self.assertAlmostEqual(live.duration, 7.0, msg="the meter still ticks")
        self.assertAlmostEqual(live.active_duration, 2.0)
        stats.expire(120.0)
        closed = build_snapshot(stats, stats.history[-1], PLAYER)
        for preset in ("DPS", "Healing", "Everything"):
            self.assertEqual(format_snapshot(live, PRESETS[preset]), format_snapshot(closed, PRESETS[preset]), preset)
        self.assertEqual(format_snapshot(live, PRESETS["DPS"]), "a caiman [0:02] 45.0 DPS - Pidef 30.0, Tovozen 15.0")

    def test_rates_come_from_the_active_duration(self) -> None:
        row = SimpleNamespace(name="Tovozen", damage=300, dps=7.5, share=1.0, max_hit=30, hit_pct=80.0,
                              heals=120, hps=3.0, taken=0, utility=0, is_npc=False)
        snap = SimpleNamespace(label="a caiman", zone="", duration=40.0, active_duration=10.0, start=0.0,
                               total_damage=300, raid_dps=7.5, killed=[], kills=0, encounters=1, rows=[row])
        self.assertEqual(format_snapshot(snap, PRESETS["DPS"]), "a caiman [0:10] 30.0 DPS - Tovozen 30.0")
        self.assertEqual(format_snapshot(snap, PRESETS["Healing"]), "a caiman [0:10] healing - Tovozen 12.0 HPS")
        old = SimpleNamespace(**{**vars(snap), "active_duration": 0.0})  # built elsewhere: printed as it is
        self.assertEqual(format_snapshot(old, PRESETS["DPS"]), "a caiman [0:40] 7.5 DPS - Tovozen 7.5")

    def test_zone_summary_adds_active_durations(self) -> None:
        stats = Stats(8.0)
        _feed(stats, self.LINES)
        stats.expire(120.0)
        one = build_snapshot(stats, stats.history[-1], PLAYER)
        merged = merge_snapshots([one, one], key="z", label="zone")
        self.assertAlmostEqual(merged.active_duration, 4.0)


class DebuffHintTests(unittest.TestCase):
    """#5: the new debuff types are credited to the ability that causes them."""

    def _credited(self, outcome: str, skill: str) -> dict:
        enc = Encounter(start=0.0, end=2.0, last_activity=2.0, closed=True)
        enc.events = [
            _hit(0.0, PLAYER, "a skeletal fighter", 5),
            _hit(1.0, "Tovozen", "a skeletal fighter", 3, skill),
            _hit(1.4, "Gozif", "a skeletal fighter", 4, "Slice"),  # closer, but not the cause
            Event(ts=1.5, kind="debuff", text="debuff", target="a skeletal fighter", outcome=outcome),
        ]
        return _rows(build_snapshot(Stats(), enc, PLAYER))["Tovozen"].debuffs

    def test_hints(self) -> None:
        self.assertEqual(models._DEBUFF_HINTS["slowed"], ("telekinetic", "infusion"))
        self.assertEqual(models._DEBUFF_HINTS["faltering pulse"], ("faltering",))
        self.assertEqual(models._DEBUFF_HINTS["vigor drained"], ("vigor",))
        self.assertEqual(models._DEBUFF_HINTS["chilled"], ("chill", "frost"))
        for outcome, skill in [("slowed", "Telekinetic Infusion"), ("faltering pulse", "Faltering Pulse"),
                               ("vigor drained", "Theft of Vigor"), ("chilled", "Frost Bolt")]:
            self.assertEqual(self._credited(outcome, skill), {outcome: 1}, outcome)


class NameVetoTests(unittest.TestCase):
    """#27: two mobs the vocabulary knows apart are never one victim or one kill-list entry."""

    @staticmethod
    def _vocab() -> Vocabulary:
        vocab = Vocabulary()
        vocab.observe("npc", "a jackal", 12)
        vocab.observe("npc", "a jackal pup", 12)
        return vocab

    LINES = [
        (0.0, "You crush a jackal for 5 points of damage."),
        (0.5, "You crush a jackal pup for 5 points of damage."),
        (1.0, "a jackal bites YOU for 1 point of damage."),
        (1.5, "a jackal pup bites YOU for 1 point of damage."),
        (2.0, "Tovozen kicks a jackal pup."),
        (2.5, "a jackal's casting is interrupted."),
    ]

    def test_cc_credit_needs_the_same_mob(self) -> None:
        plain = Stats(8.0)
        _feed(plain, self.LINES)
        self.assertEqual(_rows(build_snapshot(plain, plain.current(), PLAYER))["Tovozen"].cc, 1,
                         "without a vocabulary the names still look alike")
        known = Stats(8.0, vocab=self._vocab())
        _feed(known, self.LINES)
        self.assertEqual(_rows(build_snapshot(known, known.current(), PLAYER))["Tovozen"].cc, 0)

    def test_session_kill_list_keeps_them_apart(self) -> None:
        s = SessionStats(PLAYER, started=0.0, vocab=self._vocab())
        for ts, text in [(0.0, "You have slain a jackal!")] + [(float(i), "You have slain a jackal pup!") for i in (1, 2, 3)]:
            s.add(parse_line(text, ts, PLAYER))
        self.assertEqual(dict(s.snapshot(now=10.0).kills_by_target), {"a jackal": 1, "a jackal pup": 3})


def _coin(ts: float, looter: str, copper: int, split: int | None = None) -> Event:
    return Event(ts=ts, kind="coin", text=f"{looter} loots {copper} copper coins", actor=looter,
                 target="a dunes madman", copper=copper, split_copper=split)


def _split(ts: float, copper: int) -> Event:
    return Event(ts=ts, kind="coin_split", text=f"{copper} copper coins from a dunes madman's corpse as your split.",
                 split_copper=copper)


class CoinTests(unittest.TestCase):
    """#4/#19 and #23: wrapped split lines, and the coin the viewer actually received."""

    def test_a_wrapped_split_joins_its_loot(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(_coin(10.0, "Tovozen", 43))
        s.add(_split(10.0, 22))
        snap = s.snapshot(now=60.0)
        self.assertEqual(snap.coin_split, 22)
        self.assertEqual(snap.coin_received, 22)
        self.assertEqual(snap.coin_total, 43)
        self.assertEqual(snap.recent[-1].text, "Tovozen looted 43c (a dunes madman), your split 22c")

    def test_the_split_goes_to_the_latest_loot_without_one(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(_coin(10.0, "Tovozen", 30))
        s.add(_coin(11.0, "Gozif", 20, split=10))
        s.add(_split(12.0, 15))
        self.assertEqual(s.snapshot(now=60.0).coin_split, 25)

    def test_a_split_that_does_not_fit_is_ignored(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(_coin(10.0, "Tovozen", 10))
        version = s.version
        s.add(_split(10.0, 22))  # larger than the loot
        s.add(_coin(20.0, "Tovozen", 40))
        s.add(_split(26.0, 20))  # too late
        s.add(_split(26.5, 5))
        snap = s.snapshot(now=60.0)
        self.assertEqual(snap.coin_split, 0)
        self.assertEqual(s.version, version + 1, "only the coin line changed anything")

    def test_a_zero_split_counts_as_a_split(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(_coin(10.0, PLAYER, 1))
        s.add(_split(10.0, 0))  # "O coins ... as your split."
        self.assertEqual(s.snapshot(now=60.0).coin_received, 0)

    def test_me_view_shows_the_coin_received(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(_coin(0.0, PLAYER, 11, split=6))  # "You loot 11 ..., and receive 6 ... as your split."
        s.add(_coin(1.0, PLAYER, 37))  # "You loot 37 ..., and receive" (wrapped)
        s.add(_split(1.0, 19))
        s.add(_coin(2.0, "Tovozen", 40, split=20))
        s.add(_coin(3.0, PLAYER, 200))  # solo: no split line at all
        snap = s.snapshot(now=3600.0)
        mine = filter_session(snap, PLAYER)
        self.assertEqual(mine.coin_total, 6 + 19 + 20 + 200)
        self.assertEqual(mine.coin_by_looter, [(LOOTED_BY_YOU, 11 + 37 + 200)])
        self.assertEqual(mine.coin_split, 6 + 19 + 20)
        self.assertAlmostEqual(mine.coin_per_hour, 245.0)
        self.assertEqual(snap.coin_total, 11 + 37 + 40 + 200, "the group view still shows everything looted")


class RewardTests(unittest.TestCase):
    """#20: a quest hand-in reward is not corpse loot."""

    def test_rewards_are_kept_apart(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(parse_line("--Tovozen loots [Bone Chips] from a skeletal marksman's corpse.--", 0.0, PLAYER))
        s.add(Event(ts=1.0, kind="reward", text="You receive Ancient Chant from Brother Halvic.", actor=PLAYER,
                    target="Brother Halvic", item="Ancient Chant"))
        snap = s.snapshot(now=60.0)
        self.assertEqual(snap.items, 1)
        self.assertEqual(snap.items_by_name, [("Bone Chips", 1)])
        self.assertEqual([(e.looter, e.item, e.source) for e in snap.rewards], [(PLAYER, "Ancient Chant", "Brother Halvic")])
        self.assertNotIn(PLAYER, snap.party)
        mine = filter_session(snap, PLAYER)
        self.assertEqual((mine.items, len(mine.rewards)), (0, 1))


class SessionPartyTests(unittest.TestCase):
    """#17 and #21: the session takes the meter's roster; other players' crafts stay out."""

    LINES = [
        (0.0, "Gozif has slain a skeletal warrior!"),  # a party member who never looted
        (1.0, "Gozif crafts Heavy Cloth Bandage."),
        (2.0, "Imbor crafts Heavy Cloth Bandage(2)."),  # a player nearby, not in the party
        (3.0, "You craft Heavy Cloth Bandage."),
        (4.0, "Gozif has been slain by Zekabu!"),
    ]

    def _session(self, roster=None) -> SessionStats:
        s = SessionStats(PLAYER, started=0.0, roster=roster)
        for ts, text in self.LINES:
            s.add(parse_line(text, ts, PLAYER))
        return s

    def test_the_roster_decides_kills_deaths_and_crafts(self) -> None:
        roster = PartyRoster()
        roster.observe(SimpleNamespace(text="Gozif has joined the party.", kind="status", actor="Gozif", ts=0.0))
        snap = self._session(roster).snapshot(now=60.0)
        self.assertEqual(snap.party, ["Gozif"])
        self.assertEqual((snap.kills, snap.outsider_kills), (1, 0))
        self.assertEqual(dict(snap.deaths_by_player), {"Gozif": 1})
        self.assertEqual(snap.crafts, 2)
        self.assertEqual(dict(snap.crafts_by_crafter), {"Gozif": 1, PLAYER: 1})
        self.assertEqual(snap.outsider_crafts, [("Imbor", 2)])
        self.assertEqual([c[1] for c in snap.craft_entries], ["Gozif", PLAYER])

    def test_set_roster_later(self) -> None:
        s = self._session()
        roster = PartyRoster()
        roster.set_manual("Gozif", True)
        version = s.version
        s.set_roster(roster)
        self.assertGreater(s.version, version)
        self.assertEqual(s.snapshot(now=60.0).kills, 1)

    def test_without_a_known_party_the_evidence_decides(self) -> None:
        snap = self._session(PartyRoster()).snapshot(now=60.0)  # a roster that knows nobody yet
        self.assertEqual((snap.kills, snap.outsider_kills), (0, 1))
        self.assertEqual(snap.crafts, 4, "no party known: every craft counts")
        self.assertEqual(snap.outsider_crafts, [])

    def test_evidence_party_keeps_strangers_crafts_out(self) -> None:
        s = self._session()
        s.add(parse_line("--Gozif loots [Bone Chips] from a skeletal marksman's corpse.--", 5.0, PLAYER))
        snap = s.snapshot(now=60.0)
        self.assertEqual(snap.crafts, 2)
        self.assertEqual(snap.outsider_crafts, [("Imbor", 2)])


class SessionNewKindTests(unittest.TestCase):
    """#5 and #7: a landed Blind reads as such in the feed; your own corpse is not loot."""

    def test_blind_is_labelled_in_the_feed(self) -> None:
        s = SessionStats(PLAYER, started=0.0)
        s.add(parse_line("a skeletal guard is blinded.", 1.0, PLAYER))
        snap = s.snapshot(now=60.0)
        self.assertEqual(dict(snap.cc_by_type), {"blind": 1})
        self.assertEqual(snap.recent[-1].text, "a skeletal guard blinded")

    def test_own_corpse_is_not_loot(self) -> None:
        s = SessionStats(PLAYER, started=0.0, include_personal=True)
        s.add(parse_line("--You loot [Patched Rawhide Gloves] from your corpse.--", 1.0, PLAYER))
        s.add(parse_line("You loot 183 copper coins from your corpse..", 2.0, PLAYER))
        snap = s.snapshot(now=60.0)
        self.assertEqual((snap.items, snap.coin_total, snap.coin_received), (0, 0, 0))
        self.assertEqual(snap.loot, [])


if __name__ == "__main__":
    unittest.main()
