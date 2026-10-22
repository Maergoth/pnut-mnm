"""Sanitized effect results and expiration notices from the October 21 audit."""
from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot
from mnmparse.parser import NameCompleter, parse_line, split_fused
from mnmparse.stats import Stats


MOB = "a bone carver"


def actor_row(lines, actor="Aster"):
    stats = Stats(player_name=actor)
    for ts, text in enumerate(lines):
        stats.add(parse_line(text, ts, actor))
    snap = build_snapshot(stats, stats.current(), actor)
    return next(row for row in snap.rows if row.name == actor)


class WebRootResultTests(unittest.TestCase):
    def test_web_result_preserves_victim_and_root_without_inventing_a_caster(self):
        for victim, expected in ((MOB, MOB), ("You", "Birch"), ("Aster", "Aster"),
                                 ("a frightener's pet", "a frightener's pet")):
            with self.subTest(victim=victim):
                copula = "are" if victim == "You" else "is"
                text = f"{victim} {copula} anchored to the ground by webs."
                ev = parse_line(text, 3, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome, ev.amount),
                                 ("cc", None, expected, "webs", "root", None))
                self.assertEqual((ev.text, ev.ts), (text, 3))

    def test_web_application_and_existing_break_message_have_distinct_directions(self):
        applied = parse_line(f"{MOB} is anchored to the ground by webs.", 1)
        faded = parse_line(f"{MOB} breaks free of the webs.", 2)
        self.assertEqual((applied.kind, applied.target, applied.outcome), ("cc", MOB, "root"))
        self.assertEqual((faded.kind, faded.target, faded.outcome), ("cc_fade", MOB, "root"))

    def test_nearby_damage_root_cast_and_resisted_web_do_not_earn_web_credit(self):
        row = actor_row([
            f"Aster hits {MOB} for 10 points of damage.",
            "Aster begins casting Root.",
            f"Aster tries to cast Anchoring Web on {MOB}, but is resisted!",
            f"{MOB} is anchored to the ground by webs.",
            f"{MOB} breaks free of the webs.",
        ])
        self.assertEqual((row.damage, row.cc_attempts, row.cc, row.utility), (10, 1, 0, 0))
        self.assertEqual(row.cc_skills, {})

    def test_ordinary_root_result_can_still_match_its_real_cast(self):
        row = actor_row([
            f"Aster hits {MOB} for 10 points of damage.",
            "Aster begins casting Root.",
            f"{MOB} is rooted to the ground.",
        ])
        self.assertEqual((row.cc_attempts, row.cc, row.cc_skills), (1, 1, {"Root": 1}))


class SpellExpirationTests(unittest.TestCase):
    def test_wear_off_preserves_caster_target_full_spell_and_expiration(self):
        cases = (
            (f"Your Mark of the Crusader wears off of {MOB}.", "Birch", MOB, "Mark of the Crusader"),
            (f"Aster's Root wears off of {MOB}.", "Aster", MOB, "Root"),
            ("a restless adept's Holy Fortitude wears off of You.", "a restless adept", "Birch", "Holy Fortitude"),
            ("Your Minor Communion of Regeneration II wears off of yourself.", "Birch", "Birch", "Minor Communion of Regeneration II"),
            ("Your Haste wears off.", "Birch", None, "Haste"),
        )
        for text, caster, target, skill in cases:
            with self.subTest(text=text):
                ev = parse_line(text, 7, "Birch")
                self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome, ev.amount),
                                 ("status", caster, target, skill, "fade", None))
                self.assertEqual((ev.text, ev.ts), (text, 7))

    def test_expiration_is_not_a_cc_attempt_debuff_source_taunt_or_default_utility(self):
        row = actor_row([
            f"Aster hits {MOB} for 10 points of damage.",
            f"Aster's Root wears off of {MOB}.",
            f"{MOB} is rooted to the ground.",
            f"Aster's Interdiction wears off of {MOB}.",
            f"{MOB} is condemned.",
            f"Aster's Mark of the Crusader wears off of {MOB}.",
            f"Aster's Taunt wears off of {MOB}.",
        ])
        self.assertEqual((row.damage, row.cc_attempts, row.cc, row.utility, row.taunts), (10, 0, 0, 0, 0))
        self.assertEqual((row.cc_skills, row.debuff_skills), ({}, {}))

    def test_expiration_does_not_teach_spell_words_as_a_player_name(self):
        names = NameCompleter()
        for skill in ("Root", "Mark of the Crusader"):
            names.observe(parse_line(f"Your {skill} wears off of {MOB}.", 1, "Birch"))
        self.assertNotIn("Your Root", names._players)
        self.assertNotIn("Your Mark", names._players)
        self.assertIn(MOB, names._npcs)

    def test_wear_off_followed_by_damage_stays_two_messages(self):
        fade = f"Your Root wears off of {MOB}."
        hit = f"Aster hits {MOB} for 5 points of damage."
        self.assertEqual(split_fused(fade + " " + hit), [fade, hit])


class UnattributedEffectStatusTests(unittest.TestCase):
    def test_poison_and_holy_marks_keep_explicit_victims_without_assumed_source(self):
        for victim, expected in ((MOB, MOB), ("You", "Birch"), ("Aster", "Aster")):
            for effect in ("weakened by poison", "marked with holy runes"):
                with self.subTest(victim=victim, effect=effect):
                    copula = "are" if victim == "You" else "is"
                    ev = parse_line(f"{victim} {copula} {effect}.", 1, "Birch")
                    self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.outcome),
                                     ("status", None, expected, None, effect))

    def test_unattributed_effects_do_not_assign_utility_to_nearby_actions(self):
        row = actor_row([
            f"Aster hits {MOB} for 10 points of damage.",
            f"Aster's Strike hits {MOB} for 5 points of damage.",
            f"{MOB} is weakened by poison.",
            f"{MOB} is marked with holy runes.",
        ])
        self.assertEqual((row.damage, row.utility, row.cc), (15, 0, 0))

    def test_quoted_or_incomplete_effects_do_not_become_landed_results(self):
        chat = parse_line(f'Aster says, "{MOB} is anchored to the ground by webs."', 1)
        self.assertEqual(chat.kind, "chat")
        for text in (f"{MOB} is anchored to the ground by", f"Your Root wears off of",
                     f"{MOB} is marked with holy", f"{MOB} is weakened by"):
            with self.subTest(text=text):
                ev = parse_line(text, 1)
                self.assertNotEqual(ev.kind, "cc")
                self.assertNotEqual(ev.outcome, "fade")


if __name__ == "__main__":
    unittest.main()
