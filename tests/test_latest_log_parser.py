"""Parser/session regressions from the October 8–9 installed-log audit."""

from __future__ import annotations

import unittest

from mnmparse.parser import parse_line, split_fused
from mnmparse.app.models import build_snapshot
from mnmparse.session import SessionStats
from mnmparse.stats import Stats


class LatestDamageMessageTests(unittest.TestCase):
    def test_undead_bonus_damage_preserves_spell_target_and_amount(self):
        for actor, skill, amount, dtype in [
            ("Yami", "Poisonous Deluge", 5, "Poison"),
            ("Yami", "Rot Flesh", 6, "Disease"),
            ("Yami", "Surging Shadows", 1, "Corruption"),
            ("Your", "Surging Shadows", 3, "Corruption"),
        ]:
            with self.subTest(skill=skill, amount=amount, actor=actor):
                possessive = "Your" if actor == "Your" else actor + "'s"
                points = "point" if amount == 1 else "points"
                text = (f"{possessive} {skill} bites deeper into the undead, dealing {amount} "
                        f"extra {points} of {dtype} Damage to a dusty skeleton.")
                ev = parse_line(text, 7, "Aster")
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.amount, ev.skill, ev.dtype, ev.outcome),
                                 ("ability_hit", "Aster" if actor == "Your" else actor,
                                  "a dusty skeleton", amount, skill, dtype + " Damage", "hit"))
                self.assertEqual((ev.text, ev.ts), (text, 7))

    def test_bonus_damage_adds_to_regular_tick_in_combat_totals(self):
        stats = Stats()
        for ts, text in enumerate([
            "Yami's Poisonous Deluge hits a dusty skeleton for 10 points of Poison Damage.",
            "Yami's Poisonous Deluge bites deeper into the undead, dealing 5 extra points of Poison Damage to a dusty skeleton.",
        ]):
            stats.add(parse_line(text, ts))
        self.assertEqual(stats.current().damage_events, 2)
        self.assertEqual(stats.actor_table(stats.current())[0]["damage"], 15)

    def test_profuse_bleeding_is_a_damage_effect_not_utility(self):
        for subject in ("a dusty skeleton", "You", "a Bloodynose frightener's pet"):
            with self.subTest(subject=subject):
                ev = parse_line(f"{subject} begins to bleed profusely.", 1, "Aster")
                self.assertEqual((ev.kind, ev.target, ev.outcome, ev.amount),
                                 ("damage_effect", "Aster" if subject == "You" else subject, "bleeding", None))

    def test_bonus_damage_does_not_swallow_next_message(self):
        first = "Yami's Rot Flesh bites deeper into the undead, dealing 6 extra points of Disease Damage to a dusty skeleton."
        second = "Aster hits a dusty skeleton for 5 points of damage."
        self.assertEqual(split_fused(first + " " + second), [first, second])


class LatestUtilityMessageTests(unittest.TestCase):
    def test_exposing_shot_keeps_real_target(self):
        for actor, target in (("Aster", "a restless skeleton"), ("You", "a dusty skeleton"),
                              ("an archer", "YOU")):
            with self.subTest(actor=actor, target=target):
                ev = parse_line(f"{actor} fires an exposing shot at {target}.", 1, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill),
                                 ("status", "Birch" if actor == "You" else actor,
                                  "Birch" if target == "YOU" else target, "Exposing Shot"))

    def test_dispel_names_skill_and_supports_lowercase_viewer(self):
        for text, actor, target, outcome in [
            ("Aster dispels magic from you. (Root)", "Aster", "Birch", "root"),
            ("Aster dispels magic from a dusty skeleton. (Holy Fortitude)", "Aster", "a dusty skeleton", "holy fortitude"),
            ("You dispel magic from a dusty skeleton.", "Birch", "a dusty skeleton", None),
        ]:
            with self.subTest(text=text):
                ev = parse_line(text, 1, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome),
                                 ("status", actor, target, "Dispel Magic", outcome))

    def test_direct_nondamage_player_action_retains_actor_target_skill(self):
        for verb, skill in (("purges", "purge"), ("intimidates", "intimidate")):
            ev = parse_line(f"Aster {verb} a dusty skeleton.", 1)
            self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill),
                             ("status", "Aster", "a dusty skeleton", skill))

    def test_weak_result_is_debuff_without_assumed_actor(self):
        ev = parse_line("a dusty skeleton looks weak.", 1)
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome),
                         ("debuff", None, "a dusty skeleton", None, "weakened"))

    def test_web_break_and_fear_fade_remain_fades(self):
        for text, target, outcome in [
            ("a dusty skeleton breaks free of the webs.", "a dusty skeleton", "root"),
            ("a dusty skeleton is no longer intimidated.", "a dusty skeleton", "fear"),
            ("You break free of the webs.", "Aster", "root"),
        ]:
            ev = parse_line(text, 1, "Aster")
            self.assertEqual((ev.kind, ev.target, ev.outcome), ("cc_fade", target, outcome))

    def test_chat_and_garbled_fragments_are_not_direct_utility_actions(self):
        for text in (
            'Aster says, "Aster fires an exposing shot at a dusty skeleton."',
            "exposing shot at a dusty skeleton.",
            "Aster fires an exposing shot at",
            "dealing 5 extra points of Poison Damage to a dusty skeleton.",
            "Aster tries to cast Root on a dusty skeleton, but is resisted!",
        ):
            with self.subTest(text=text):
                ev = parse_line(text, 1)
                self.assertFalse(ev.kind == "status" and ev.actor and ev.target and ev.skill)


class LatestCoinMessageTests(unittest.TestCase):
    def test_multidenomination_gross_and_share(self):
        text = ("Aster loots 1 silver, and 31 copper coins from a restless skeleton's corpse, "
                "and you receive 1 silver, and 6 copper coins from a restless skeleton's corpse as your split.")
        ev = parse_line(text, 1, "Birch")
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.amount, ev.copper, ev.split_copper),
                         ("coin", "Aster", "a restless skeleton", 131, 131, 106))
        session = SessionStats("Birch", started=0)
        session.add(ev)
        snap = session.snapshot(now=2)
        self.assertEqual((snap.coin_total, snap.coin_split, snap.coin_received), (131, 106, 106))
        self.assertEqual(dict(snap.coin_by_looter), {"Aster": 131})

    def test_viewer_loot_single_share_and_zero_share(self):
        for share, expected in (("4 copper coins", 4), ("O coins", 0)):
            ev = parse_line("You loot 1 silver, and 22 copper coins from a dusty skeleton's corpse, "
                            f"and receive {share} as your split.", 1, "Aster")
            self.assertEqual((ev.kind, ev.actor, ev.copper, ev.split_copper), ("coin", "Aster", 122, expected))

    def test_multidenomination_share_cannot_exceed_gross(self):
        ev = parse_line("Aster loots 1 silver, and 22 copper coins from a dusty skeleton's corpse, "
                        "and receive 1 silver, and 23 copper coins as your split.", 1)
        self.assertEqual((ev.kind, ev.copper, ev.split_copper), ("coin", 122, None))

    def test_share_does_not_sum_money_from_a_following_message(self):
        ev = parse_line("Aster loots 1 silver, and 22 copper coins from a dusty skeleton's corpse, "
                        "and receive 4 copper coins as your split. "
                        "Birch loots 10 copper coins from a restless skeleton's corpse.", 1)
        self.assertEqual((ev.kind, ev.copper, ev.split_copper), ("coin", 122, 4))

    def test_wrapped_multidenomination_share(self):
        ev = parse_line("1 silver, and 6 copper coins from a restless skeleton's corpse as your split.", 1)
        self.assertEqual((ev.kind, ev.target, ev.amount, ev.split_copper),
                         ("coin_split", "a restless skeleton", 106, 106))

    def test_all_denominations_and_price_separators(self):
        for money in ("1 platinum, 2 gold, 3 silver, and 4 copper coins",
                      "1 platinum and 2 gold and 3 silver and 4 copper coins"):
            ev = parse_line(f"Aster loots {money} from a dusty skeleton's corpse.", 1)
            self.assertEqual((ev.kind, ev.copper), ("coin", 1_020_304))


class LatestNoncombatMessageTests(unittest.TestCase):
    def test_known_cannot_attack_reason_with_ocr_terminal_bang(self):
        reasons = ("must face your target", "must be able to see your target", "need a target")
        for reason in reasons:
            for suffix in ("l", "I", "1", "|"):
                with self.subTest(reason=reason, suffix=suffix):
                    ev = parse_line(f"You {reason} to use that ability{suffix}", 1, "Aster")
                    self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome),
                                     ("cannot_attack", "Aster", None, None, reason))
        ev = parse_line("Your target is too far away to use that abilityl", 1, "Aster")
        self.assertEqual((ev.kind, ev.outcome), ("cannot_attack", "too far away"))

    def test_cannot_attack_ocr_line_never_credits_utility(self):
        stats = Stats(player_name="Aster")
        stats.add(parse_line("Your Strike hits a dusty skeleton for 5 points of damage.", 0, "Aster"))
        for ts, reason in enumerate(("must face your target", "must be able to see your target"), 1):
            stats.add(parse_line(f"You {reason} to use that abilityl", ts, "Aster"))
        row = next(r for r in build_snapshot(stats, stats.current(), "Aster").rows if r.name == "Aster")
        self.assertEqual((row.damage, row.utility), (5, 0))

    def test_experience_known_word_with_ocr_terminal_bang(self):
        for party in ("", "party "):
            for suffix in ("l", "I", "1", "|"):
                with self.subTest(party=party, suffix=suffix):
                    ev = parse_line(f"You gain {party}experience{suffix}", 1)
                    self.assertEqual(ev.kind, "experience")
        # Repair only the one punctuation glyph after the complete known word.
        self.assertNotEqual(parse_line("You gain party experiencell", 1).kind, "experience")

    def test_npc_corpse_consider_does_not_create_a_kill(self):
        ev = parse_line("a Bloodynose sergeant is dead. Their corpse will decay in 7 minutes. "
                        "Anyone can loot this corpse in 4 minutes.", 1)
        self.assertEqual((ev.kind, ev.target), ("consider", "a Bloodynose sergeant"))
        session = SessionStats("Aster", started=0)
        session.add(ev)
        self.assertEqual(session.snapshot(now=2).kills, 0)

    def test_disease_recovery_is_status_not_landed_action(self):
        for disease in ("Infectious Disease", "Malady"):
            ev = parse_line(f"You feel healthy again. ({disease})", 1, "Aster")
            self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill), ("status", "Aster", None, disease))


if __name__ == "__main__":
    unittest.main()
