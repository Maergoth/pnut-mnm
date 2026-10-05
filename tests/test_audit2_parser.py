"""Parsing shapes from the 2026-10-03 log audit (findings #1-#9, #15, #19, #28, #29): dropped
apostrophes, thrown weapons and garbled hit tails, the final "!" read as l, coin splits, crowd
control and debuff results, garbled casts, everyday game messages, /con text and the experience
line glued to a faction line."""

from __future__ import annotations

import unittest

from mnmparse import grammar
from mnmparse.app.models import build_snapshot
from mnmparse.interrupts import CC_CATEGORIES, CC_LABELS, cc_categories, load_cc_table
from mnmparse.parser import NameCompleter, normalize_ocr, parse_line, split_fused
from mnmparse.stats import Stats

PLAYER = "Pidef"


def parse(text: str, ts: float = 0.0):
    return parse_line(text, ts, PLAYER)


def taught(*lines: str) -> NameCompleter:
    """A NameCompleter that has seen ``lines``."""
    names = NameCompleter()
    for text in lines:
        names.observe(parse(text))
    return names


class DroppedApostropheTests(unittest.TestCase):
    """#1 / #15 / #28: "<Name>s <Ability> hits" is an ability hit, not a melee "<Name>s <Ability>"."""

    def test_capital_inside_the_name(self) -> None:
        ev = parse("PaLidus Holy Strike hits a roving ghoul for 15 points of Holy Damage.")
        self.assertEqual((ev.kind, ev.actor, ev.skill, ev.amount), ("ability_hit", "PaLidu", "Holy Strike", 15))

    def test_npc_possessive(self) -> None:
        ev = parse("a dunes madmans Strike hits YOU for 4 points of damage!")
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.amount),
                         ("ability_hit", "a dunes madman", PLAYER, "Strike", 4))

    def test_adjective_of_a_clipped_line_is_no_possessor(self) -> None:
        line = "Righteous Smite hits a skeletal warrior for 5 points of damage."
        self.assertEqual(normalize_ocr(line), line)

    def test_named_npc_stays_one_actor(self) -> None:
        names = taught("Gozif's Holy Strike hits a skeletal cleric for 9 points of Holy Damage.")
        ev = parse("Toilmaster Verith hits YOU for 12 points of damage.")
        names.observe(ev)
        self.assertEqual((ev.kind, ev.actor, ev.amount), ("melee_hit", "Toilmaster Verith", 12))

    def test_melee_actor_ending_in_a_known_ability_is_split(self) -> None:
        names = taught("Gozif's Censuring Strike hits a skeletal cleric for 5 points of damage.")
        ev = parse("Gozi Censuring Strike hits a skeletal cleric for 7 points of damage.")
        self.assertEqual((ev.kind, ev.actor), ("melee_hit", "Gozi Censuring Strike"))
        names.observe(ev)
        self.assertEqual((ev.kind, ev.actor, ev.raw_actor, ev.skill, ev.target, ev.amount),
                         ("ability_hit", "Gozi", "Gozi", "Censuring Strike", "a skeletal cleric", 7))

    def test_npc_actor_ending_in_a_known_ability_is_split(self) -> None:
        names = taught("a skeletal defender's Strike hits Wululiso for 3 points of damage.")
        ev = parse("a skeletal defender Strike hits YOU for 4 points of damage!")
        names.observe(ev)
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill), ("ability_hit", "a skeletal defender", PLAYER, "Strike"))

    def test_unknown_ability_is_not_split(self) -> None:
        names = taught("Gozif's Censuring Strike hits a skeletal cleric for 5 points of damage.")
        ev = parse("Gozi Whistling Slash hits a skeletal cleric for 7 points of damage.")
        names.observe(ev)
        self.assertEqual((ev.kind, ev.actor), ("melee_hit", "Gozi Whistling Slash"))


class HitLineRepairTests(unittest.TestCase):
    """#2: thrown weapons, split block digits, glued "of", garbled "points", junk and cut-off tails."""

    def test_thrown_weapon(self) -> None:
        ev = parse("Palidu throws at a caiman for 5 points of damage.")
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.amount), ("melee_hit", "Palidu", "a caiman", "throw", 5))

    def test_split_block_digits(self) -> None:
        ev = parse("a caiman bites YOU for 11 points of damage. (Block 1 1)")
        self.assertEqual((ev.kind, ev.target, ev.amount, ev.blocked), ("melee_hit", PLAYER, 11, 11))

    def test_garbled_points_of(self) -> None:
        for line, amount, dtype in [
            ("Gozif's Sanctified Hammer hits a crocodile for 4 points ofHoly Damage.", 4, "Holy Damage"),
            ("Gozif's Whistling Slash hits a skeletal cleric for 12 points'of damage.", 12, "damage"),
            ("Gozif's Sanctified Weapon hits a skeletal cleric for 2 points f Holy Damage", 2, "Holy Damage"),
            ("Gozif's Whistling Slash hits a skeletal warrior for 10 POI ts of damage.", 10, "damage"),
            ("Gozif's Holy Strike hits a skeletal cleric for 17 POHIts of Holy Damage", 17, "Holy Damage"),
            ("Gozif's Censuring Strike hits a skeletal cleric for 5 points of damage:-", 5, "damage"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor, ev.amount, ev.dtype), ("ability_hit", "Gozif", amount, dtype))

    def test_points_for_is_left_alone(self) -> None:
        line = "Gozif gets 10 points for style."
        self.assertEqual(normalize_ocr(line), line)

    def test_line_cut_off_after_the_number(self) -> None:
        for line, kind, amount in [
            ("Your Sanctified Weapon hits a skeletal cleric for 2", "ability_hit", 2),
            ("a roving ghoul slashes YOU for IO points of", "melee_hit", 10),
            ("Gozif crushes a skeletal fighter for 41 points", "melee_hit", 41),
            ("Tovozen's Heal heals Wululiso for 56", "heal", 56),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.amount, ev.text), (kind, amount, line))

    def test_garbled_damage_word_is_not_completed(self) -> None:
        ev = parse("Gozif's Slice hits a for 3+oints,ofrBleed Damag")
        self.assertEqual((ev.kind, ev.amount), ("ability_partial", None))


class FinalBangTests(unittest.TestCase):
    """#3 / #29: the final "!" read as l / I / 1 / |."""

    def test_after_message_words(self) -> None:
        for line, kind, outcome in [
            ("You try to crush a giant fire beetle, but missl", "melee_miss", "miss"),
            ("a giant fire beetle tries to bite YOU, but missesl", "melee_miss", "miss"),
            ("a crocodile tries to bite YOU, but YOU parryl", "melee_miss", "parry"),
            ("Your ability missesI", "ability_miss", "miss"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.outcome), (kind, outcome))
        ev = parse("Gozif ambushes their victiml")
        self.assertEqual((ev.kind, ev.actor), ("status", "Gozif"))
        ev = parse("Your skill in Alteration has increasedl (47)")
        self.assertEqual((ev.kind, ev.skill, ev.amount), ("personal", "Alteration", 47))
        ev = parse("a dunes madman's Blast of Sleet hits YOU for 12 points of Cold Damagel")
        self.assertEqual(ev.dtype, "Cold Damage")

    def test_a_cut_off_word_is_not_repaired(self) -> None:
        self.assertEqual(normalize_ocr("for 3 points of Hol"), "for 3 points of Hol")

    def test_known_names_lose_the_glyph(self) -> None:
        names = taught(
            "Tovozen crushes a crocodile for 5 points of damage.",
            "You begin casting Blind.",
        )
        cases = [
            ("You have slain a crocodilel", "target", "a crocodile"),
            ("Your party member Tovozen has slain a crocodileI", "target", "a crocodile"),
            ("a caiman has been slain by Tovozenl", "actor", "Tovozen"),
            ("a crocodile resists your Blindl", "skill", "Blind"),
            ("a caiman has been slain by Kivabol", "actor", "Kivabol"),  # never seen: left alone
        ]
        for line, field, want in cases:
            with self.subTest(line=line):
                ev = parse(line)
                names.observe(ev)
                self.assertEqual(getattr(ev, field), want)

    def test_fused_kill_and_attack_toggle_are_split(self) -> None:
        self.assertEqual(
            split_fused("Your party member Gozif has slain a skeletal monkl Stopped attacking.", PLAYER),
            ["Your party member Gozif has slain a skeletal monkl", "Stopped attacking."],
        )
        parts = split_fused("Your party member Gozif has slain a skeletal knightl You gain party experience!", PLAYER)
        self.assertEqual([parse(p).kind for p in parts], ["kill", "experience"])


class CoinSplitTests(unittest.TestCase):
    """#4 / #19: the viewer's share of a coin loot."""

    def test_clipped_or_misread_denominations(self) -> None:
        tail = "corpse as your split."
        for line, split in [
            (f"Gozif loots 13 copper coins from a roving ghoul's corpse, and you receive 2 coppe ghoul's {tail}", 2),
            (f"Gozif loots 7 copper coins from a skeletal cleric's corpse, and you receive 1 cc cleric's {tail}", 1),
            (f"Gozif loots 15 copper coins from a skeletal warrior's corpse, and you receive 3 COI warrior's {tail}", 3),
            (f"Gozif loots 20 copper coins from a skeletal cavalier's corpse, and you receive 4 cavalier's {tail}", 4),
            (f"Gozif loots 13 copper coins from a skeletal priest's corpse, and you receive 3 priest's {tail}", 3),
            (f"Gozif loots 2 copper coins from a rotting skeleton's corpse, and you receive O coins from a rotting skeleton's {tail}", 0),
            ("You loot 3 copper coins from an ashira scout's corpse, and receive 1 copper as your split.", 1),
            ("Gozif loots 2 silver coins from a dunes madman's corpse, and you receive 50 c madman's corpse as your split.", 50),
            ("Gozif loots 2 silver coins from a dunes madman's corpse, and you receive 1 s madman's corpse as your split.", 100),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.split_copper), ("coin", split))

    def test_split_larger_than_the_loot_is_a_misread(self) -> None:
        ev = parse("Gozif loots 7 copper coins from a dunes madman's corpse, and you receive 12 copper coins from a dunes madman's corpse as your split.")
        self.assertEqual((ev.kind, ev.copper, ev.split_copper), ("coin", 7, None))

    def test_wrapped_split_line(self) -> None:
        first = parse("Gozif loots 43 copper coins from a dunes madman's corpse, and you receive")
        self.assertEqual((first.kind, first.copper, first.split_copper), ("coin", 43, None))
        ev = parse("22 copper coins from a dunes madman's corpse as your split.")
        self.assertEqual((ev.kind, ev.split_copper, ev.actor, ev.target), ("coin_split", 22, None, "a dunes madman"))
        ev = parse("O coins from a rotting skeleton's corpse as your split.")
        self.assertEqual((ev.kind, ev.split_copper, ev.actor), ("coin_split", 0, None))

    def test_name_cut_off_before_the_split(self) -> None:
        ev = parse("Gozif loots 9 copper coins from a risen and you receive 2 copper corpse as your split.")
        self.assertEqual((ev.kind, ev.actor, ev.copper, ev.split_copper), ("coin", "Gozif", 9, 2))

    def test_split_coin_amount_digits(self) -> None:
        ev = parse("Gozif loots 1 1 copper coins from a skeletal warrior's corpse, and you")
        self.assertEqual((ev.kind, ev.copper), ("coin", 11))

    def test_loot_line_fused_with_a_coin_line(self) -> None:
        parts = split_fused(
            "--Gozif loots [Tattered Cloth Gloves] from a skeletal cleric's Gozif loots 8 copper coins "
            "from a skeletal fighter's corpse, and you",
            PLAYER,
        )
        self.assertEqual(len(parts), 2)
        self.assertEqual((parse(parts[1]).kind, parse(parts[1]).copper), ("coin", 8))

    def test_quest_reward_is_not_loot(self) -> None:
        ev = parse("You receive Ancient Chant from Jalwa Noor.")
        self.assertEqual((ev.kind, ev.actor, ev.item, ev.target), ("reward", PLAYER, "Ancient Chant", "Jalwa Noor"))
        self.assertEqual(parse("You receive Dusty Lute.").kind, "reward")
        self.assertEqual(parse("--Gozif loots [Bone Chips] from a skeletal monk's corpse.--").kind, "loot")


class CrowdControlTests(unittest.TestCase):
    """#5: crowd-control and debuff result lines, and the archer attempts that name the caster."""

    def test_landed_lines(self) -> None:
        for line, kind, target, outcome in [
            ("Tovozen is blinded.", "cc", "Tovozen", "blind"),
            ("a skeletal guard is pinned.", "cc", "a skeletal guard", "root"),
            ("a skeletal guard is no longer pinned.", "cc_fade", "a skeletal guard", "root"),
            ("a crocodile adheres to the ground.", "cc", "a crocodile", "root"),
            ("a crocodile comes unstuck.", "cc_fade", "a crocodile", "root"),
            ("You are bound by a net shot.", "cc", PLAYER, "root"),
            ("You are no longer bound.", "cc_fade", PLAYER, "root"),
            ("You are slowed by a snaring shot.", "cc", PLAYER, "snare"),
            ("You are no longer slowed by a snaring shot.", "cc_fade", PLAYER, "snare"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.target, ev.outcome), (kind, target, outcome))
        self.assertEqual(parse("You are bound by a net shot.").skill, "a net shot")

    def test_debuff_lines(self) -> None:
        for line, target, kind in [
            ("a crocodile's movements begin to slow.", "a crocodile", "slowed"),
            ("a caiman's heart begins beating irregularly.", "a caiman", "faltering pulse"),
            ("a caiman reels as vigor flows from their body.", "a caiman", "vigor drained"),
            ("a desert bat is chilled to the bone.", "a desert bat", "chilled"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.target, ev.outcome), ("debuff", target, kind))
        self.assertEqual(parse("Tovozen's body surges as vigor flows into them.").kind, "status")

    def test_archer_attempts_name_the_caster(self) -> None:
        for line, actor, skill in [
            ("Tozuvek snares YOU!", "Tozuvek", "Snaring Shot"),
            ("Pemiruk fires a net shot at YOUI", "Pemiruk", "Net Shot"),
            ("Pemiruk interrupts YOU!", "Pemiruk", "Interrupting Shot"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill), ("status", actor, PLAYER, skill))

    def test_attempt_after_an_immunity_is_skipped(self) -> None:
        names = NameCompleter()
        immune = parse("YOU are temporarily IMMUNE to Tozuvek's Snaring Shotl", 10.0)
        self.assertEqual((immune.kind, immune.actor, immune.target, immune.outcome, immune.skill),
                         ("status", PLAYER, "Tozuvek", "immune", None))
        names.observe(immune)
        other = parse("Pemiruk snares YOU!", 10.0)  # someone else's attempt still counts
        names.observe(other)
        self.assertEqual(other.skill, "Snaring Shot")
        stopped = parse("Tozuvek snares YOU!", 10.0)
        names.observe(stopped)
        self.assertEqual((stopped.skill, stopped.outcome), (None, "immune"))
        later = parse("Tozuvek snares YOU!", 20.0)  # only the one right after the immunity
        names.observe(later)
        self.assertEqual(later.skill, "Snaring Shot")

    def test_cc_table(self) -> None:
        table = load_cc_table()
        self.assertEqual(cc_categories("Snaring Shot", table), frozenset({"snare"}))
        self.assertEqual(cc_categories("Blind", table), frozenset({"blind"}))
        self.assertIn("blind", CC_CATEGORIES)
        self.assertEqual(CC_LABELS["blind"], "Blinds")

    def test_landed_blind_and_pvp_snare_are_credited(self) -> None:
        def rows(lines):
            stats, names = Stats(encounter_timeout_s=12.0), NameCompleter()
            for ts, text in lines:
                ev = parse(text, ts)
                names.observe(ev)
                stats.add(ev)
            snap = build_snapshot(stats, stats.current() or stats.history[-1], PLAYER)
            return {r.name: r for r in snap.rows}

        r = rows([
            (0.0, "You crush a skeletal guard for 5 points of damage."),
            (1.0, "You begin casting Blind."),
            (2.5, "a skeletal guard is blinded."),
        ])
        self.assertEqual(r[PLAYER].cc_types, {"blind": 1})
        r = rows([
            (0.0, "Tozuvek pierces YOU with their bow for 26 points of damage."),
            (1.0, "You are slowed by a snaring shot."),
            (1.0, "Tozuvek snares YOU!"),
            (5.0, "YOU are temporarily IMMUNE to Tozuvek's Snaring Shot!"),
            (5.0, "Tozuvek snares YOU!"),
        ])
        self.assertEqual((r["Tozuvek"].cc_types, r["Tozuvek"].cc_attempts), ({"snare": 1}, 1))


class GarbledCastTests(unittest.TestCase):
    """#6: "casting" is never the verb of a generic status line."""

    def test_no_phantom_npc(self) -> None:
        for line in ("a skeletal cleric bezms casting Holy Fortitude.", "a skeletal cleric egins casting Gate."):
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill), ("status", "a skeletal cleric", None, None))

    def test_other_generic_status_lines_still_parse(self) -> None:
        ev = parse("Gozif jabs a stumbling zombie.")
        self.assertEqual((ev.kind, ev.actor, ev.skill, ev.target), ("status", "Gozif", "jab", "a stumbling zombie"))


class EverydayMessageTests(unittest.TestCase):
    """#7: real game messages that had no rule (and counted as unreadable)."""

    def test_own_corpse_is_not_loot(self) -> None:
        ev = parse("--You loot [Patched Rawhide Gloves] from your corpse.--")
        self.assertEqual((ev.kind, ev.item, ev.outcome), ("personal", "Patched Rawhide Gloves", "corpse"))
        self.assertEqual(parse("--You loot [Trainee's SpellbookJ from your corpse.--").item, "Trainee's Spellbook")
        ev = parse("You loot 183 copper coins from your corpse..")
        self.assertEqual((ev.kind, ev.amount, ev.copper), ("personal", 183, None))

    def test_vendor(self) -> None:
        for line, outcome, item, amount, copper in [
            ("You sell Mountain Flatbread (x4) for 4 copper coins.", "sell", "Mountain Flatbread", 4, 4),
            ("You sell Bone Chips for 1 copper coin.", "sell", "Bone Chips", 1, 1),
            ("You sell Cracked Staff for 5 silver coins.", "sell", "Cracked Staff", 1, 500),
            ("You buy Water Flask for 1 silver, and 20 copper coins.", "buy", "Water Flask", 1, 120),
            ("You buy Scroll: Hallowed Ground for IO silver, and 45 copper coins.", "buy", "Scroll: Hallowed Ground", 1, 1045),
            ("You train Wagoneering for 5 copper.", "train", "Wagoneering", 1, 5),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor, ev.outcome, ev.item, ev.amount, ev.copper),
                                 ("vendor", PLAYER, outcome, item, amount, copper))

    def test_personal_and_system_lines(self) -> None:
        for line in (
            "Someone in your party is too high level for you to receive experience from this kill.",
            "Someone in your party is too high level for you to receive experience from",
            "Players in Monsters & Memories",
            "There are 35 players in Shaded Dunes who match your search.",
            "Your camp will be prepared in 25 seconds.",
            "You cannot invite Tovozen, they are already in a party.",
            "You cannot invite anyone to the party, as you are not the leader.",
            "No ability memorized in Gem 8.",
            "You have not received any tells, so you cannot reply.",
        ):
            with self.subTest(line=line):
                self.assertEqual(parse(line).kind, "personal")
        self.assertEqual(parse("Someone in your party is too high level for you to receive experience from this").outcome,
                         "too high level")

    def test_harvest(self) -> None:
        for line in ("You harvest [Copper Ore)!", "You harvest [Copper Orel!", "You harvest [Copper Ore) I"):
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.item), ("personal", "Copper Ore"))

    def test_chat(self) -> None:
        for line, actor in [
            ('a watchman says, "Hail! How may I assist you?"', "a watchman"),
            ('You say, "Hail, Mayana."', PLAYER),
            ('You sav, "Hail, Mayana."', PLAYER),
            ('Jalwa Noor says. "Listen closely"', "Jalwa Noor"),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor), ("chat", actor))

    def test_level_up(self) -> None:
        for line, actor, level in [
            ("Gozif has leveled upl They are now level 71", "Gozif", 7),
            ("Gozif has leveled up! They are now level 81", "Gozif", 8),
            ("Gozif has leveled upl They are now level 111", "Gozif", 11),
            ("Gozif has leveled upl They are now level 1 II", "Gozif", 11),
            ("Gozif has leveled up! They are now level 10!", "Gozif", 10),
            ("Gozif has leveled up! They are now level 11 !", "Gozif", 11),
            ("You are now level IO!", PLAYER, 10),
            ("You are now level 11!", PLAYER, 11),
            ("You have leveled up!", PLAYER, None),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.actor, ev.amount), ("level_up", actor, level))

    def test_corpse_consider(self) -> None:
        ev = parse("Tovozen is dead. Their corpse will decay in 5 days and 19 hours.")
        self.assertEqual((ev.kind, ev.target), ("consider", "Tovozen"))

    def test_new_kinds_are_listed(self) -> None:
        for kind in ("coin_split", "reward", "vendor", "chat", "level_up"):
            self.assertIn(kind, grammar.KINDS)


class ConsiderTests(unittest.TestCase):
    """#8: every /con text is "consider", with the considered mob or player as the target."""

    def test_con_lines(self) -> None:
        for line, target in [
            ("a crocodile seems indifferent to your presence Would you like to die? You would almost certainly be defeated in battle.", "a crocodile"),
            ("a caiman views you as a threat It might prove to be a diffcult battle.", "a caiman"),
            ("a willowisp views you as a threat", "a willowisp"),
            ("an archaeologist appears uneasy with you Battle would be quite risky.", "an archaeologist"),
            ("Toilmaster Verith seems indifferent to your presence Would you like to die?", "Toilmaster Verith"),
            ("Tovozen Battle with them would be quite risky.", "Tovozen"),
            ("Tovozen You would probably be defeated by them in battle.", "Tovozen"),
            ("You would almost certainly be defeated in battle.", None),
            ("You would likely be defeated in battle.", None),
            ("Battle with them would be fairly in your favor.", None),
        ]:
            with self.subTest(line=line):
                ev = parse(line)
                self.assertEqual((ev.kind, ev.target, ev.actor), ("consider", target, None))


class ExperienceSplitTests(unittest.TestCase):
    """#9: the experience line glued to the end of another message is always cut off."""

    def test_faction_line_swallowing_the_experience_line(self) -> None:
        for line in (
            "Your faction standing with Denizens of Wyrmsbane Tomb cannot possibly get a You gain party experience!",
            "Your faction standing with Denizens of Wyrmsbane Tomb cann possibly get a ou gain party experience!",
            "Your faction standing with Denizens of Wyrmsbane Tomb got worsev You gain party ekperience!",
        ):
            with self.subTest(line=line):
                parts = split_fused(line, PLAYER)
                self.assertEqual([parse(p).kind for p in parts], ["personal", "experience"])

    def test_experience_at_the_start_is_not_split(self) -> None:
        self.assertEqual(split_fused("You gain party experience!", PLAYER), ["You gain party experience!"])
        self.assertEqual(parse("You zain experience!").kind, "experience")


if __name__ == "__main__":
    unittest.main()
