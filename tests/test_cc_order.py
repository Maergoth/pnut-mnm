"""Crowd control printed before its cause (2026-10-02 dunes madman fight) and taunts as utility."""

from __future__ import annotations

import unittest

from mnmparse.app.models import build_snapshot
from mnmparse.parser import parse_line
from mnmparse.stats import Stats

PLAYER = "Maergoth"

# Verbatim from logs/combat_2026-10-02_173222.log (lines 1193-1318, trimmed).
MADMAN = [
    (0.0, "Your Cudgel of Light hits a dunes madman for 10 points of Holy Damage."),
    (5.0, "a dunes madman begins casting Snare."),
    (6.0, "a dunes madman's casting is interrupted."),
    (6.0, "Famafenezo kicks a dunes madman."),
    (18.0, "a dunes madman's arcane defenses weaken."),
    (18.0, "Famafenezo's Arcane Infusion hits a dunes madman for 11 points of Magic Damage."),
    (20.0, "a dunes madman begins casting Snare."),
    (22.0, "You are snared by clinging roots."),
    (35.0, "a dunes madman is stunned by an electric arc."),
    (35.0, "Famafenezo's Electric Arc hits a dunes madman for 13 points of Electric Damage."),
    (42.0, "Your Shield Slam hits a dunes madman for O points of damage."),
    (42.0, "You crush a dunes madman for 12 points of damage."),
    (44.0, "a dunes madman begins casting Lesser Heal."),
    (46.0, "a dunes madman's casting is interrupted."),
    (46.0, "Famafenezo kicks a dunes madman."),
    (50.0, "You taunt a dunes madman."),
    (51.0, "You crush a dunes madman for 7 points of damage."),
]


def rows(lines):
    stats = Stats(encounter_timeout_s=12.0)
    end = max(ts for ts, _ in lines)
    filler = [(float(t), "Gozif crushes a dunes madman for 1 point of damage.") for t in range(1, int(end) + 1, 4)]
    for ts, text in sorted(lines + filler, key=lambda x: x[0]):  # the fight goes on in between
        stats.add(parse_line(text, ts, PLAYER))
    snap = build_snapshot(stats, stats.current() or stats.history[-1], PLAYER)
    return {r.name: r for r in snap.rows}


class EffectBeforeCauseTests(unittest.TestCase):
    def test_madman_fight(self) -> None:
        r = rows(MADMAN)
        fama = r["Famafenezo"]
        self.assertEqual(fama.cc_types, {"interrupt": 2, "stun": 1}, "kicks printed after the interrupt still count")
        self.assertEqual(fama.cc_skills, {"kick": 2, "Electric Arc": 1})
        self.assertEqual(fama.debuffs, {"arcane weakened": 1}, "arcane defenses weaken = Arcane Infusion")
        self.assertEqual(fama.debuff_skills, {"Arcane Infusion": 1})
        me = r[PLAYER]
        self.assertEqual((me.cc, me.cc_attempts), (0, 1), "the slam landed nothing: he was not casting yet")
        self.assertEqual((me.aggro, me.taunts, me.utility), (1, 1, 1), "the taunt is utility")
        self.assertEqual(r["a dunes madman"].cc_types, {"snare": 1}, "its Snare on you is its crowd control")

    def test_cause_before_effect_still_works(self) -> None:
        r = rows([
            (0.0, "You crush a skeletal fighter for 5 points of damage."),
            (5.0, "Dogabetarolem kicks a skeletal fighter."),
            (5.5, "a skeletal fighter's casting is interrupted."),
        ])
        self.assertEqual(r["Dogabetarolem"].cc, 1)

    def test_closest_attempt_wins(self) -> None:
        # Two kicks: one 2.5 s before the interrupt line, one printed right after it.
        r = rows([
            (0.0, "You crush a skeletal fighter for 5 points of damage."),
            (2.5, "Dogabetarolem kicks a skeletal fighter."),
            (5.0, "a skeletal fighter's casting is interrupted."),
            (5.0, "Famafenezo kicks a skeletal fighter."),
        ])
        self.assertEqual((r["Famafenezo"].cc, r["Dogabetarolem"].cc), (1, 0))

    def test_an_angry_line_after_a_taunt_is_not_counted_twice(self) -> None:
        r = rows([
            (0.0, "Povebizu slashes a skeletal fighter for 5 points of damage."),
            (1.0, "Povebizu taunts a skeletal fighter."),
            (1.5, "a skeletal fighter looks angry at Povebizu."),
            (20.0, "a skeletal fighter looks angry at Povebizu."),
        ])
        self.assertEqual((r["Povebizu"].aggro, r["Povebizu"].taunts), (2, 1))



class AuditRuleTests(unittest.TestCase):
    """Rules from the 2026-10-02 attribution audit (733 effects checked by independent readers)."""

    def test_a_kick_on_someone_else_is_not_the_cause(self) -> None:
        r = rows([
            (0.0, "Povebizu slashes a skeletal warrior for 5 points of damage."),
            (1.0, "Palidu begins casting Lesser Charm."),
            (2.0, "a skeletal warrior kicks Povebizu."),
            (3.0, "Palidu's casting is interrupted."),
        ])
        self.assertEqual(r["a skeletal warrior"].cc, 0)

    def test_a_kick_before_the_cast_began_is_not_its_interrupt(self) -> None:
        r = rows([
            (0.0, "Gozif crushes a skeletal cavalier for 5 points of damage."),
            (1.0, "Dogabetarolem kicks a skeletal cavalier."),
            (1.5, "a skeletal cavalier begins casting Minor Heal."),
            (2.5, "a skeletal cavalier's casting is interrupted."),
            (2.5, "Your Shield Slam hits a skeletal cavalier for 0 points of damage."),
        ])
        self.assertEqual((r["Dogabetarolem"].cc, r[PLAYER].cc), (0, 1))

    def test_a_spell_started_before_the_victims_cast_still_counts(self) -> None:
        r = rows([
            (0.0, "Gozif crushes a skeletal priest for 5 points of damage."),
            (1.0, "Povebizu begins casting Interdiction."),
            (2.0, "a skeletal priest begins casting Root."),
            (3.0, "a skeletal priest is condemned."),
            (3.0, "a skeletal priest's casting is interrupted."),
        ])
        self.assertEqual(r["Povebizu"].cc_types, {"interrupt": 1})
        self.assertEqual(r["Povebizu"].debuffs, {"condemned": 1})

    def test_pvp_kicks_count(self) -> None:
        r = rows([
            (0.0, "Hokabibuve crushes Fipuduzuleg for 6 points of damage."),
            (0.5, "Fipuduzuleg begins casting Pact of Renewal."),
            (1.0, "Fipuduzuleg's casting is interrupted."),
            (1.0, "Buforuvuba kicks Fipuduzuleg."),
        ])
        self.assertEqual(r["Buforuvuba"].cc, 1)

    def test_a_stun_interrupts_a_caster(self) -> None:
        r = rows([
            (0.0, "Gozif crushes a skeletal cavalier for 5 points of damage."),
            (1.0, "a skeletal cavalier begins casting Minor Heal."),
            (2.0, "a skeletal cavalier's casting is interrupted."),
            (2.0, "a skeletal cavalier is stunned."),
            (2.0, "Gozif uppercuts a skeletal cavalier."),
        ])
        self.assertEqual(r["Gozif"].cc_types, {"interrupt": 1, "stun": 1})

    def test_npc_bash_and_slam_and_area_smash_stun(self) -> None:
        r = rows([
            (0.0, "a skeletal defender hits YOU for 5 points of damage."),
            (1.0, "You are stunned."),
            (1.0, "a skeletal defender's Bash hits YOU for 3 points of damage!"),
            (6.0, "a jackal is stunned."),
            (6.0, "Fipuduzuleg smashes the ground around them with great force."),
            (6.0, "a jackal pup is stunned."),
        ])
        self.assertEqual(r["a skeletal defender"].cc_types, {"stun": 1})
        self.assertEqual(r["Fipuduzuleg"].cc_types, {"stun": 2})
        self.assertEqual(r["Fipuduzuleg"].cc_skills, {"Ground Smash": 2})

    def test_debuff_sources(self) -> None:
        r = rows([
            (0.0, "Povebizu slashes a skeletal marksman for 8 points of damage."),
            (0.5, "Gozif's Slice hits a for 3+oints,ofrBleed Damag"),  # garbled, salvaged
            (0.5, "a skeletal marksman is bleeding out."),
            (5.0, "Abepulifif's Screaming Vocalization hits a skeletal marksman for 12 points of Magic Damage."),
            (5.5, "a skeletal marksman's magical resistance frays."),
            (5.5, "Povebizu's Distress hits a skeletal marksman for 9 points of Magic Damage."),
            (8.0, "Hudokara pierces a skeletal marksman with their bow for 8 points of damage."),
            (8.0, "a skeletal marksman is struck by a barbed arrow."),
        ])
        self.assertEqual(r["Gozif"].debuff_skills, {"Slice": 1}, "not Povebizu's slash")
        self.assertEqual(r["Povebizu"].debuff_skills, {"Distress": 1}, "Distress, not Screaming Vocalization")
        self.assertEqual(r["Hudokara"].debuff_skills, {"Barbed Arrow": 1})
        self.assertEqual(r["Abepulifif"].debuffs, {})

    def test_a_pull_debuff_before_the_first_hit_counts(self) -> None:
        stats = Stats(encounter_timeout_s=12.0)
        for ts, text in [
            (0.0, "Povebizu begins casting Distress."),
            (1.0, "a skeletal marksman's magical resistance frays."),
            (1.0, "Povebizu's Distress hits a skeletal marksman for 9 points of Magic Damage."),
            (2.0, "Gozif crushes a skeletal marksman for 4 points of damage."),
        ]:
            stats.add(parse_line(text, ts, PLAYER))
        snap = build_snapshot(stats, stats.current(), PLAYER)
        pove = next(r for r in snap.rows if r.name == "Povebizu")
        self.assertEqual(pove.debuffs, {"resist down": 1})
        self.assertEqual(snap.start, 1.0, "the fight still starts at the first damage")



class EnvironmentDamageTests(unittest.TestCase):
    def test_falling_is_damage_taken_not_a_damage_dealer(self) -> None:
        r = rows([
            (0.0, "You crush a famished zombie for 5 points of damage."),
            (1.0, "YOU take 3 damage from falling!"),
        ])
        me = r[PLAYER]
        self.assertEqual(me.taken, 3)
        self.assertEqual([(s.skill, s.total) for s in me.taken_from if s.skill.startswith("environment")], [("environment: falling", 3)])
        self.assertNotIn("falling", r)


if __name__ == "__main__":
    unittest.main()
