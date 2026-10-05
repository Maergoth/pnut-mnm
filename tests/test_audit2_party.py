"""Second audit: the party roster (who counts as the viewer's group) and fight grouping.

The roster requires explicit membership evidence, starts over when the viewer joins a party,
and forgets each saved member on its own clock. Fights are not opened or kept going by mobs
fighting each other, and name merging keeps different mobs apart.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from mnmparse.parser import parse_line
from mnmparse.party import PartyRoster
from mnmparse.stats import Stats, canonical_names, name_similar
from mnmparse.vocab import Vocabulary

VIEWER = "Hobudez"


def _stats(**kw) -> Stats:
    return Stats(8.0, player_name=VIEWER, **kw)


def _feed(stats: Stats, lines: list[tuple[float, str]]) -> None:
    for ts, text in lines:
        stats.add(parse_line(text, ts, VIEWER))


def _observe(roster: PartyRoster, lines: list[tuple[float, str]]) -> None:
    for ts, text in lines:
        roster.observe(parse_line(text, ts, VIEWER))


class ViewerIsNeverAMemberTests(unittest.TestCase):
    def test_own_loot_and_coin_lines(self) -> None:
        stats = _stats()
        _feed(stats, [
            (1.0, "You receive Bone Chips."),
            (2.0, "--You loot [Bone Chips] from a rat's corpse.--"),
            (3.0, "You loot 3 copper coins from a rat's corpse."),
            (4.0, f"{VIEWER} is now the leader of the party."),
        ])
        self.assertEqual(stats.roster.members(), set())
        self.assertFalse(stats.roster.known(), "a solo viewer has no other party members")

    def test_without_a_player_name_the_pronoun_is_enough(self) -> None:
        roster = PartyRoster()
        roster.observe(SimpleNamespace(text="You loot [a rat tail].", kind="loot", actor="You", raw_actor="You", ts=1.0))
        self.assertEqual(roster.members(), set())

    def test_a_saved_roster_with_the_viewer_in_it_is_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "party.json"
            now = time.time()
            path.write_text(json.dumps({"version": 4, "saved": now, "seen": {VIEWER: now, "Pidef": now}}),
                            encoding="utf-8")
            roster = PartyRoster(VIEWER)
            self.assertTrue(roster.load(path))
        self.assertEqual(roster.members(), {"Pidef"})


class NewPartyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = PartyRoster(VIEWER)
        _observe(self.roster, [(1.0, "Tovozen has joined the party."), (2.0, "Wululiso has joined the party.")])
        self.roster.set_manual("Kulepu", True)

    def test_invite_then_join_starts_a_new_party_with_the_inviter(self) -> None:
        version = self.roster.version
        _observe(self.roster, [(100.0, "Gozif has invited you to their party."), (104.0, "You have joined the party.")])
        self.assertEqual(self.roster.seen(), {"Gozif"})
        self.assertEqual(self.roster.members(), {"Gozif", "Kulepu"}, "the user's own choices stay")
        self.assertGreater(self.roster.version, version)

    def test_join_alone_clears_and_an_old_invite_is_not_the_party(self) -> None:
        _observe(self.roster, [(100.0, "Gozif has invited you to their party."), (200.0, "You have joined the party.")])
        self.assertEqual(self.roster.seen(), set())

    def test_misread_join_line_still_counts(self) -> None:
        _observe(self.roster, [(100.0, "ou have joined the party.")])
        self.assertEqual(self.roster.seen(), set())

    def test_viewer_kicked_clears(self) -> None:
        _observe(self.roster, [(100.0, f"{VIEWER} has been kicked from the party.")])
        self.assertEqual(self.roster.seen(), set())

    def test_you_have_been_kicked_clears(self) -> None:
        _observe(self.roster, [(100.0, "You have been kicked from the party.")])
        self.assertEqual(self.roster.seen(), set())

    def test_someone_else_kicked_is_removed(self) -> None:
        _observe(self.roster, [(100.0, "Tovozen has been kicked from the party.")])
        self.assertEqual(self.roster.seen(), {"Wululiso"})


class PartyLineTests(unittest.TestCase):
    def test_corpse_drag_permission_does_not_prove_membership(self) -> None:
        roster = PartyRoster(VIEWER)
        _observe(roster, [
            (1.0, "You give Pidef permission to drag all your existing corpses."),
            (1.0, "You give Tovozen permission to drag all your existing corpses."),
            (1.0, "You give Wululiso permission to drag all your"),  # cut off at the window edge
        ])
        self.assertEqual(roster.members(), set())

    def test_party_member_slain_names_the_member_not_the_killer(self) -> None:
        roster = PartyRoster(VIEWER)
        _observe(roster, [(1.0, "Your party member Palidu has been slain by Grandmaster Obadiah!")])
        self.assertEqual(roster.members(), {"Palidu"})

    def test_junk_and_two_word_names_are_not_members(self) -> None:
        roster = PartyRoster(VIEWER)
        for actor in ("L Pidef", "Pi-def", "a rat", "Gozif2"):
            roster.observe(SimpleNamespace(text="--x loots [Bone Chips] from a rat's corpse.--", kind="loot",
                                           actor=actor, raw_actor=actor, ts=1.0))
        self.assertEqual(roster.members(), set())

    def test_names_go_through_the_canonicaliser(self) -> None:
        roster = PartyRoster(VIEWER, canonical=lambda n: {"Pldef": "Pidef"}.get(n, n))
        _observe(roster, [(1.0, "Pldef has joined the party.")])
        self.assertEqual(roster.members(), {"Pidef"})
        _observe(roster, [(2.0, "Pldef has left the party.")])
        self.assertEqual(roster.members(), set())


class GroupHealTests(unittest.TestCase):
    def test_one_spell_healing_several_players_does_not_prove_membership(self) -> None:
        stats = _stats()
        _feed(stats, [
            (10.0, "Your Restorative Smite heals you for 11 Health."),
            (10.2, "Your Restorative Smite heals Pidef for 11 Health."),
            (11.3, "Your Restorative Smlte heals Tovozen for 11 Health."),  # a misread spell name
        ])
        self.assertEqual(stats.roster.members(), set())

    def test_single_target_heals_on_strangers_add_nobody(self) -> None:
        stats = _stats()
        _feed(stats, [
            (10.0, "Your Healing Touch heals Rupedu for 48 Health."),
            (10.4, "Your Healing Touch heals Rupcdu for 48 Health."),  # the same heal read twice
            (30.0, "Your Healing Touch heals Kulepu for 48 Health."),
            (30.5, "Your Restorative Smite heals Fetuvo for 11 Health."),  # another spell
            (40.0, "Tovozen's Restorative Smite heals Pidef for 11 Health."),  # not the viewer's
            (40.1, "Tovozen's Restorative Smite heals Gozif for 11 Health."),
        ])
        self.assertEqual(stats.roster.members(), set())


def _shared_fight(stats: Stats, t: float, ally: str, *, mob: str = "a caiman") -> None:
    _feed(stats, [
        (t, f"You crush {mob} for 10 points of damage."),
        (t + 1, f"{ally} slashes {mob} for 7 points of damage."),
    ])
    stats.expire(t + 30.0)


class SharedFightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stats = _stats()
        _feed(self.stats, [(0.0, "Tovozen has joined the party.")])  # the party is known

    def test_a_player_fighting_alongside_never_becomes_a_member(self) -> None:
        roster = self.stats.roster
        version = roster.version
        for i in range(10):
            _shared_fight(self.stats, 100.0 * (i + 1), "Zabomir")
            self.assertNotIn("Zabomir", roster.members())
        self.assertEqual(roster.inferred(), set())
        self.assertEqual(roster.seen(), {"Tovozen"})
        self.assertEqual(roster.version, version)

    def test_fighting_another_mob_or_healing_strangers_does_not_count(self) -> None:
        for i in range(4):
            t = 100.0 * (i + 1)
            _feed(self.stats, [
                (t, "You crush a caiman for 10 points of damage."),
                (t + 1, "Rupedu slashes a crocodile for 7 points of damage."),  # their own mob
                (t + 2, "Rupedu's Heal heals Kulepu for 20 Health."),
                (t + 3, "Your pet Fetuvo bites a caiman for 5 points of damage."),
            ])
            self.stats.expire(t + 30.0)
        self.assertEqual(self.stats.roster.members(), {"Tovozen"})

    def test_healing_the_viewer_and_helping_with_combat_do_not_prove_membership(self) -> None:
        for i in range(4):
            t = 100.0 * (i + 1)
            _feed(self.stats, [
                (t, "a caiman bites YOU for 10 points of damage."),
                (t + 1, "Palidu's Heal heals you for 20 Health."),
                (t + 2, "Gozif's Holy Strike hits a caiman for 9 points of Holy Damage."),
            ])
            self.stats.expire(t + 30.0)
        self.assertEqual(self.stats.roster.inferred(), set())
        self.assertEqual(self.stats.roster.members(), {"Tovozen"})

    def test_removing_a_manual_exclusion_does_not_promote_a_shared_fighter(self) -> None:
        roster = self.stats.roster
        roster.set_manual("Zabomir", False)
        for i in range(4):
            _shared_fight(self.stats, 100.0 * (i + 1), "Zabomir")
        self.assertNotIn("Zabomir", roster.members(), "the user's choice wins")
        roster.set_manual("Zabomir", None)
        self.assertNotIn("Zabomir", roster.members())
        _feed(self.stats, [(900.0, "Your party has been disbanded.")])
        self.assertEqual(roster.members(), set())
        for i in range(4):
            _shared_fight(self.stats, 1000.0 + 100.0 * i, "Zabomir")
        self.assertNotIn("Zabomir", roster.members())

    def test_a_restart_does_not_promote_shared_fighters(self) -> None:
        now = time.time()
        for i in range(4):
            _shared_fight(self.stats, now - 600.0 + 100.0 * i, "Zabomir")
        _shared_fight(self.stats, now - 50.0, "Fetuvo")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "party.json"
            self.stats.roster.save(path)
            again = PartyRoster(VIEWER)
            again.load(path)
        self.assertEqual(again.inferred(), set())
        again.note_fight(["Fetuvo"], now)
        again.note_fight(["Fetuvo"], now + 1)
        self.assertNotIn("Fetuvo", again.members())


class ReloadTests(unittest.TestCase):
    def test_members_expire_one_by_one(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "party.json"
            path.write_text(json.dumps({
                "version": 4, "saved": now - 24 * 3600,  # an old stamp no longer drops everyone
                "seen": {"Pidef": now - 3600, "Tovozen": now - 7 * 3600},
                "manual_in": ["Kulepu"], "manual_out": ["Rupedu"],
            }), encoding="utf-8")
            roster = PartyRoster(VIEWER)
            self.assertTrue(roster.load(path))
        self.assertEqual(roster.seen(), {"Pidef"})
        self.assertEqual((roster.manual_in, roster.manual_out), ({"Kulepu"}, {"Rupedu"}))


class NpcInfightingTests(unittest.TestCase):
    def test_mobs_fighting_each_other_never_open_a_fight(self) -> None:
        stats = _stats()
        _feed(stats, [
            (0.0, "a dunes madman hits a famished zombie for 24 points of damage."),
            (2.0, "a sickened ashira's Chilling Essence hits a wandering ghoul for 30 points of Cold Damage."),
            (4.0, "a wandering ghoul tries to hit a sickened ashira, but misses!"),
        ])
        self.assertIsNone(stats.current())
        stats.expire(60.0)
        self.assertEqual(stats.history, [])

    def test_they_join_an_open_fight_but_do_not_keep_it_going(self) -> None:
        stats = _stats()
        _feed(stats, [
            (0.0, "You crush a caiman for 10 points of damage."),
            (5.0, "a dunes madman hits a famished zombie for 24 points of damage."),
            (10.0, "a dunes madman hits a famished zombie for 24 points of damage."),
        ])
        self.assertIsNone(stats.current(), "8 s after the viewer's last swing the fight is over")
        enc = stats.history[0]
        self.assertEqual((enc.start, enc.end), (0.0, 0.0))
        self.assertEqual(len(enc.events), 2, "the line inside the fight is kept as context")
        self.assertEqual(enc.target_names, {"a caiman"})

    def test_their_damage_alone_does_not_make_a_fight_worth_listing(self) -> None:
        stats = _stats()
        _feed(stats, [
            (0.0, "You try to crush a caiman, but miss!"),
            (1.0, "a dunes madman hits a famished zombie for 24 points of damage."),
        ])
        stats.expire(30.0)
        self.assertEqual(stats.history, [])

    def test_the_viewers_pet_still_opens_a_fight(self) -> None:
        stats = _stats()
        _feed(stats, [(0.0, "Your pet Fetuvo bites a caiman for 5 points of damage.")])
        self.assertIsNotNone(stats.current())


class TargetTests(unittest.TestCase):
    def test_a_self_hit_misread_names_nobody(self) -> None:
        stats = _stats()
        _feed(stats, [(0.0, "Pidef's Rebuke II hits Pidef for 16 points of Holy Damage.")])
        self.assertEqual(stats.current().target_names, set())

    def test_pvp_victims_on_the_viewers_side_are_not_targets(self) -> None:
        stats = _stats()
        _feed(stats, [
            (0.0, "Tovozen has joined the party."),
            (1.0, "Zabomir slashes YOU for 20 points of damage."),
            (2.0, "Gozif's Fireball hits Tovozen for 30 points of Fire Damage."),
            (3.0, "You crush Zabomir for 12 points of damage."),
        ])
        self.assertEqual(stats.current().target_names, {"Zabomir"})


def _vocab(**counts: int) -> Vocabulary:
    vocab = Vocabulary()
    for name, n in counts.items():
        vocab.observe("npc" if name.startswith("a ") else "player", name, n)
    return vocab


@unittest.skipUnless(hasattr(Vocabulary, "distinct"), "needs Vocabulary.distinct")
class NameMergeVetoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vocab = _vocab(**{
            "a jackal": 12, "a jackal pup": 39, "a skeletal fighter": 959, "a skeletal cleric": 1181,
            "a crocodile": 700, "a crocodile hatchling": 39, "a skeletal fizhter": 1,
            "a risen officer": 61, "a risen omcer": 11,
        })

    def test_well_attested_different_mobs_never_merge(self) -> None:
        for a, b in [("a jackal pup", "a jackal"), ("a skeletal cleric", "a skeletal fighter"),
                     ("a crocodile hatchling", "a crocodile")]:
            self.assertTrue(name_similar(a, b), "the fuzzy rule alone merges them")
            self.assertFalse(name_similar(a, b, vocab=self.vocab), (a, b))

    def test_ocr_variants_still_merge(self) -> None:
        self.assertTrue(name_similar("a skeletal fizhter", "a skeletal fighter", vocab=self.vocab))
        self.assertTrue(name_similar("a skeletal", "a skeletal fighter", vocab=self.vocab))
        self.assertTrue(name_similar("a risen omcer", "a risen officer", vocab=self.vocab), "the ffi ligature")

    def test_fight_names_keep_the_pup_apart(self) -> None:
        counts = Counter({"a jackal": 28, "a jackal pup": 8})
        self.assertEqual(canonical_names(counts)["a jackal pup"], "a jackal")
        self.assertEqual(canonical_names(counts, vocab=self.vocab)["a jackal pup"], "a jackal pup")
        stats = _stats(vocab=self.vocab)
        _feed(stats, [
            (0.0, "You crush a jackal for 10 points of damage."),
            (1.0, "You crush a jackal pup for 8 points of damage."),
            (2.0, "You have slain a jackal pup!"),
        ])
        enc = stats.current()
        self.assertEqual(stats.label(enc), "a jackal, a jackal pup")
        self.assertEqual(enc.killed_names, {"a jackal pup"})

    def test_an_unknown_variant_joins_the_closest_name(self) -> None:
        vocab = _vocab(**{"a skeletal fighter": 959, "a skeletal knight": 120})
        counts = Counter({"a skeletal fighter": 40, "a skeletal knight": 12, "a skeletal knighti": 1})
        self.assertTrue(name_similar("a skeletal knighti", "a skeletal fighter", vocab=vocab), "both qualify")
        canon = canonical_names(counts, vocab=vocab)
        self.assertEqual(canon["a skeletal knight"], "a skeletal knight")
        self.assertEqual(canon["a skeletal knighti"], "a skeletal knight", "not the commoner fighter")


class NameMergeWithoutDistinctTests(unittest.TestCase):
    def test_a_vocabulary_without_distinct_changes_nothing(self) -> None:
        self.assertTrue(name_similar("a jackal pup", "a jackal", vocab=SimpleNamespace()))


if __name__ == "__main__":
    unittest.main()
