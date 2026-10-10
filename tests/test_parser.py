"""Table-driven tests for mnmparse.grammar / mnmparse.parser (SPEC section 3 and 6)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from mnmparse import grammar  # noqa: E402
from mnmparse.parser import Event, map_player, miss_outcome, normalize_ocr, parse_line  # noqa: E402

ZOMBIE = "a stumbling zombie"

# Every verbatim line from SPEC section 3, plus the "expect also" forms.
# Values not listed in the expectation are not asserted; ``None`` asserts None.
SPEC_LINES: list[tuple[str, dict[str, object]]] = [
    # ---- MELEE HIT ------------------------------------------------------
    ("You crush a stumbling zombie for 8 points of damage.",
     dict(kind="melee_hit", actor="You", target=ZOMBIE, amount=8, skill="crush", dtype="damage", outcome="hit", raw_actor="You")),
    ("You crush a stumbling zombie for 1 point of damage.",
     dict(kind="melee_hit", actor="You", target=ZOMBIE, amount=1, skill="crush", dtype="damage", outcome="hit")),
    ("Tovozen crushes a stumbling zombie for 2 points of damage.",
     dict(kind="melee_hit", actor="Tovozen", target=ZOMBIE, amount=2, skill="crush", dtype="damage", outcome="hit")),
    ("a stumbling zombie bites Wululiso for 20 points of damage.",
     dict(kind="melee_hit", actor=ZOMBIE, target="Wululiso", amount=20, skill="bite", dtype="damage", outcome="hit")),
    ("a stumbling zombie bites YOU for 20 points of damage.",
     dict(kind="melee_hit", actor=ZOMBIE, target="You", amount=20, skill="bite", dtype="damage", outcome="hit")),
    # ---- MELEE MISS / AVOIDANCE -----------------------------------------
    ("You try to crush a stumbling zombie, but miss!",
     dict(kind="melee_miss", actor="You", target=ZOMBIE, amount=None, skill="crush", outcome="miss")),
    ("a stumbling zombie tries to bite YOU, but misses!",
     dict(kind="melee_miss", actor=ZOMBIE, target="You", amount=None, skill="bite", outcome="miss")),
    ("a stumbling zombie tries to bite Wululiso, but Wululiso dodges!",
     dict(kind="melee_miss", actor=ZOMBIE, target="Wululiso", skill="bite", outcome="dodge")),
    ("Wululiso tries to pierce a stumbling zombie, but a stumbling zombie dodges!",
     dict(kind="melee_miss", actor="Wululiso", target=ZOMBIE, skill="pierce", outcome="dodge")),
    ("Wululiso tries to pierce a stumbling zombie, but a stumbling zombie parries!",
     dict(kind="melee_miss", actor="Wululiso", target=ZOMBIE, skill="pierce", outcome="parry")),
    ("a stumbling zombie tries to bite Wululiso, but Wululiso blocks!",
     dict(kind="melee_miss", actor=ZOMBIE, target="Wululiso", skill="bite", outcome="block")),
    ("a stumbling zombie tries to bite Wululiso, but Wululiso ripostes!",
     dict(kind="melee_miss", actor=ZOMBIE, target="Wululiso", skill="bite", outcome="riposte")),
    ("a stumbling zombie tries to bite Wululiso, but Wululiso blocks with their shield!",
     dict(kind="melee_miss", actor=ZOMBIE, target="Wululiso", skill="bite", outcome="block")),
    ("a stumbling zombie tries to bite YOU, but you dodge!",
     dict(kind="melee_miss", actor=ZOMBIE, target="You", skill="bite", outcome="dodge")),
    # ---- CANNOT ATTACK --------------------------------------------------
    ("You try to attack, but you must face your target.",
     dict(kind="cannot_attack", actor="You", target=None, amount=None, outcome="you must face your target")),
    ("You try to attack, but you are too far away.",
     dict(kind="cannot_attack", actor="You", target=None, amount=None, outcome="you are too far away")),
    # ---- ABILITY / SPELL / PROC HIT -------------------------------------
    ("Pidef's Backstab hits a stumbling zombie for 24 points of damage.",
     dict(kind="ability_hit", actor="Pidef", target=ZOMBIE, amount=24, skill="Backstab", dtype="damage", outcome="hit", raw_actor="Pidef")),
    ("Tovozen's Holy Strike hits a stumbling zombie for 10 points of Holy Damage.",
     dict(kind="ability_hit", actor="Tovozen", target=ZOMBIE, amount=10, skill="Holy Strike", dtype="Holy Damage", outcome="hit")),
    ("Your Cudgel of Light hits a stumbling zombie for 3 points of Holy Damage.",
     dict(kind="ability_hit", actor="You", target=ZOMBIE, amount=3, skill="Cudgel of Light", dtype="Holy Damage", outcome="hit", raw_actor="Your")),
    ("Your Sanctified Weapon hits a stumbling zombie for 2 points of Holy Damage.",
     dict(kind="ability_hit", actor="You", target=ZOMBIE, amount=2, skill="Sanctified Weapon", dtype="Holy Damage", outcome="hit")),
    ("Pidef's Slice hits a stumbling zombie for 3 points of Bleed Damage.",
     dict(kind="ability_hit", actor="Pidef", target=ZOMBIE, amount=3, skill="Slice", dtype="Bleed Damage", outcome="hit")),
    ("Wululiso's Rend hits a stumbling zombie for 20 points of damage.",
     dict(kind="ability_hit", actor="Wululiso", target=ZOMBIE, amount=20, skill="Rend", dtype="damage", outcome="hit")),
    ("a stumbling zombie's Strike hits Wululiso for 6 points of damage.",
     dict(kind="ability_hit", actor=ZOMBIE, target="Wululiso", amount=6, skill="Strike", dtype="damage", outcome="hit")),
    # ---- HEAL -----------------------------------------------------------
    ("Tovozen's Heal heals Wululiso for 56 Health.",
     dict(kind="heal", actor="Tovozen", target="Wululiso", amount=56, skill="Heal", dtype="Health")),
    ("Tovozen's Lesser Heal heals Wululiso for 27 Health.",
     dict(kind="heal", actor="Tovozen", target="Wululiso", amount=27, skill="Lesser Heal", dtype="Health")),
    # ---- CASTING --------------------------------------------------------
    ("You begin casting Cudgel of Light.",
     dict(kind="cast", actor="You", skill="Cudgel of Light", amount=None, target=None)),
    ("Tovozen begins casting Heal.",
     dict(kind="cast", actor="Tovozen", skill="Heal", amount=None)),
    ("Your casting is interrupted.",
     dict(kind="interrupt", actor="You", raw_actor="Your", amount=None)),
    # ---- KILL -----------------------------------------------------------
    ("Your party member Pidef has slain a stumbling zombie!",
     dict(kind="kill", actor="Pidef", target=ZOMBIE, amount=None)),
    ("You have slain a stumbling zombie!",
     dict(kind="kill", actor="You", target=ZOMBIE)),
    ("a stumbling zombie has been slain by Pidef!",
     dict(kind="kill", actor="Pidef", target=ZOMBIE)),
    ("You have been slain by a stumbling zombie!",
     dict(kind="kill", actor=ZOMBIE, target="You")),
    # ---- STATE / STATUS -------------------------------------------------
    ("Starting to attack.", dict(kind="status", amount=None)),
    ("Stopped attacking.", dict(kind="status", amount=None)),
    ("Pidef jabs a stumbling zombie.", dict(kind="status", actor="Pidef", target=ZOMBIE, skill="jab", amount=None)),
    ("Pidef ambushes their victim!", dict(kind="status", actor="Pidef")),
    ("Pidef appears.", dict(kind="status", actor="Pidef")),
    ("Pidef begins to sneak.", dict(kind="status", actor="Pidef")),
    ("a stumbling zombie's armor breaks.", dict(kind="status", actor=ZOMBIE)),
    ("a stumbling zombie is bleeding out.", dict(kind="damage_effect", target=ZOMBIE, outcome="bleeding")),
    ("a stumbling zombie is struck by a barbed arrow.", dict(kind="damage_effect", target=ZOMBIE, outcome="barbed arrow")),
    ("a stumbling zombie loses interest in Pidef.", dict(kind="status", actor=ZOMBIE, target="Pidef")),
    ("Tovozen becomes spiritually connected to the divine.", dict(kind="status", actor="Tovozen")),
    ("Tovozen's spiritual connection is severed.", dict(kind="status", actor="Tovozen")),
    ("Your fervor subsides.", dict(kind="status", actor="You", raw_actor="Your")),
    ("Tovozen's devotion is rewarded!", dict(kind="status", actor="Tovozen")),
    ("Tovozen lets loose an empowering battle cry.", dict(kind="status", actor="Tovozen")),
    ("The warmth of the campfire leaves you.", dict(kind="status", amount=None)),
    # Seen live in the Combat window on 2026-10-01 (not in the SPEC list).
    ("Pidef slips into the shadows and disappears.", dict(kind="status", actor="Pidef")),
]

# Complete messages seen live on 2026-10-01 (snapshot of the running game), wrapped halves joined.
LIVE_LINES: list[tuple[str, dict[str, object]]] = [
    ("Wululiso's Rend hits a stumbling zombie for 22 points of damage.",
     dict(kind="ability_hit", actor="Wululiso", target=ZOMBIE, amount=22, skill="Rend", dtype="damage")),
    ("Sigito's Opening Strike hits a stumbling zombie for 4 points of damage.",
     dict(kind="ability_hit", actor="Sigito", target=ZOMBIE, amount=4, skill="Opening Strike", dtype="damage")),
    ("Sigito's Finishing Blow hits a stumbling zombie for 21 points of damage.",
     dict(kind="ability_hit", actor="Sigito", amount=21, skill="Finishing Blow")),
    ("Your Crusader Strike hits a stumbling zombie for 4 points of damage.",
     dict(kind="ability_hit", actor="You", raw_actor="Your", amount=4, skill="Crusader Strike")),
    ("a stumbling zombie pierces Wululiso for I point of damage.",
     dict(kind="melee_hit", actor=ZOMBIE, target="Wululiso", amount=1, skill="pierce")),
    ("Pidef pierces a stumbling zombie for 7 points of damage.",
     dict(kind="melee_hit", actor="Pidef", amount=7, skill="pierce")),
]

# Wrapped messages from SPEC section 3, joined with a single space as the tracker does.
WRAPPED_LINES: list[tuple[str, dict[str, object]]] = [
    ("Pidef's Backstab hits a stumbling zombie for 24 points of" + " " + "damage.",
     dict(kind="ability_hit", actor="Pidef", target=ZOMBIE, amount=24, skill="Backstab", dtype="damage")),
    ("Wululiso tries to pierce a stumbling zombie, but a stumbling" + " " + "zombie dodges!",
     dict(kind="melee_miss", actor="Wululiso", target=ZOMBIE, skill="pierce", outcome="dodge")),
    ("Your Cudgel of Light hits a stumbling zombie for 3 points of" + " " + "Holy Damage.",
     dict(kind="ability_hit", actor="You", amount=3, skill="Cudgel of Light", dtype="Holy Damage")),
    ("a stumbling zombie tries to bite Wululiso, but Wululiso" + " " + "dodges!",
     dict(kind="melee_miss", actor=ZOMBIE, target="Wululiso", outcome="dodge")),
]

# OCR noise from SPEC section 3 / 6 and the fixture.
NOISE_LINES: list[tuple[str, dict[str, object]]] = [
    ("a stumbling zombie bites Wululiso for 1 1 points of damage.",
     dict(kind="melee_hit", actor=ZOMBIE, target="Wululiso", amount=11, dtype="damage")),
    ("a stumbling zombie bites Wululiso for I I points of damage.",
     dict(kind="melee_hit", amount=11, dtype="damage")),
    ("a stumbling zombie bites Wululiso for II points of damage.",
     dict(kind="melee_hit", amount=11, dtype="damage")),
    ("Wululiso pierces a stumbling zombie for I point of damage.",
     dict(kind="melee_hit", actor="Wululiso", amount=1, skill="pierce", dtype="damage")),
    ("Wululiso pierces a stumbling zombie for l point of damage.",
     dict(kind="melee_hit", amount=1)),
    ("You crush a stumbling zombie for -1 point of damage.",
     dict(kind="melee_hit", actor="You", amount=1, dtype="damage")),
    ("a stumbling zombie's Strike hits Wululiso for 6 poilifs of damage.",
     dict(kind="ability_hit", actor=ZOMBIE, target="Wululiso", amount=6, skill="Strike", dtype="damage")),
    ("Tovozen crushes a stumbling zombie for 2 points of Carnage.",
     dict(kind="melee_hit", actor="Tovozen", amount=2, skill="crush", dtype="damage")),
    ("Tovozen crushes a stumbling zombie for 2 points of dama e",
     dict(kind="melee_hit", amount=2, dtype="damage")),
    ("a stumbling zombie's Strike hits Wululiso for 6 points of clanma#.",
     dict(kind="ability_hit", amount=6, dtype="damage")),
    ("Tovozens Heal heals Wululiso for 56 Health.",
     dict(kind="heal", actor="Tovozen", target="Wululiso", amount=56, skill="Heal")),
    ("Tovozen crushes a stumbli mbie-for2points of Carnage.",
     dict(kind="melee_hit", actor="Tovozen", amount=2, dtype="damage")),
    ("you-try to attack, but you are too far away.",
     dict(kind="cannot_attack", actor="You", outcome="you are too far away")),
    ("Vou try to attack, but you are too far away.",
     dict(kind="cannot_attack", actor="You")),
    ("your party member Pidef has slain a stumbling zombie!",
     dict(kind="kill", actor="Pidef", target=ZOMBIE)),
    ("'Tovozen begins casting Heal.",
     dict(kind="cast", actor="Tovozen", skill="Heal")),
    ("Wululiso pierces a stumbling zombie for 12 points of damage:",
     dict(kind="melee_hit", amount=12, dtype="damage")),
    ("a stumbling zombie bites Wululiso for 20 points of damage,",
     dict(kind="melee_hit", amount=20, dtype="damage")),
    ("Tovozen’s Heal heals Wululiso for 56 Health.",
     dict(kind="heal", actor="Tovozen", amount=56, skill="Heal")),
    ("[Wed Oct 01 17:11:03 2026] You crush a stumbling zombie for 8 points of damage.",
     dict(kind="melee_hit", actor="You", amount=8)),
]

UNKNOWN_LINES: list[str] = [
    "",
    "   ",
    "zomme ror 1",
    "Holv Dama",
    "i",
    "Tovozen crushes a stumbli",
    "Wululiso's Rend hits a stumbling zombie for 20 points of",  # unjoined first half
    "damage.",  # unjoined second half
    "a stumbling zombie bites YOU or20.points of damage.",
    "zombie for 45B�nts of",
    "Heal heals Wululiso for 56 Health.",  # clipped top line: must not become actor "Heal"
    "eal heals Wululiso for 56 Health.",
    "lorem ipsum dolor sit amet",  # no sentence shape: no article/name subject, no punctuation
]


class ParseTableTests(unittest.TestCase):
    """Every verbatim SPEC line parses to the expected fields."""

    def _check(self, line: str, expected: dict[str, object], player_name: str = "") -> Event:
        ev = parse_line(line, 123.5, player_name)
        self.assertIsInstance(ev, Event)
        self.assertEqual(ev.ts, 123.5)
        self.assertEqual(ev.text, line, "Event.text must be the original line")
        for key, want in expected.items():
            self.assertEqual(getattr(ev, key), want, f"{line!r}: field {key}")
        return ev

    def test_spec_lines(self) -> None:
        for line, expected in SPEC_LINES:
            with self.subTest(line=line):
                self._check(line, expected)

    def test_wrapped_lines_joined_with_space(self) -> None:
        for line, expected in WRAPPED_LINES:
            with self.subTest(line=line):
                self._check(line, expected)

    def test_ocr_noise_lines(self) -> None:
        for line, expected in NOISE_LINES:
            with self.subTest(line=line):
                self._check(line, expected)

    def test_you_with_a_misread_exclamation_mark(self) -> None:
        # live lines 2026-10-02: "a famished zombie kicks YOUI", "a sickened ashira kicks YOUI"
        for line in ("a famished zombie kicks YOUI", "a sickened ashira kicks YOUl"):
            ev = parse_line(line, 1.0, "Maergoth")
            self.assertEqual((ev.kind, ev.target, ev.skill), ("status", "Maergoth", "kick"), line)
        ev = parse_line("a skeletal warrior hits YOUl for 5 points of damage.", 1.0, "Maergoth")
        self.assertEqual((ev.kind, ev.target, ev.amount), ("melee_hit", "Maergoth", 5))

    def test_falling_damage(self) -> None:
        ev = parse_line("YOU take 3 damage from falling!", 1.0, "Maergoth")
        self.assertEqual((ev.kind, ev.actor, ev.target, ev.skill, ev.amount), ("env_damage", None, "Maergoth", "falling", 3))

    def test_cut_off_ability_line(self) -> None:
        # cut off right after "of": the number was read, so the hit counts (audit 2026-10-03)
        ev = parse_line("Wululiso's Rend hits a stumbling zombie for 20 points of", 1.0)
        self.assertEqual((ev.kind, ev.actor, ev.skill, ev.amount), ("ability_hit", "Wululiso", "Rend", 20))
        # who used which ability is readable; the damage is not: never counted as damage
        ev = parse_line("Gozif's Slice hits a for 3+oints,ofrBleed Damag", 1.0)
        self.assertEqual((ev.kind, ev.actor, ev.skill), ("ability_partial", "Gozif", "Slice"))

    def test_live_lines(self) -> None:
        for line, expected in LIVE_LINES:
            with self.subTest(line=line):
                self._check(line, expected)

    def test_unknown_lines(self) -> None:
        for line in UNKNOWN_LINES:
            if line.startswith("Wululiso's Rend hits"):
                continue  # completed as a hit: see test_cut_off_ability_line
            with self.subTest(line=line):
                ev = parse_line(line, 1.0)
                self.assertEqual(ev.kind, "unknown")
                self.assertEqual(ev.text, line)
                self.assertIsNone(ev.actor)
                self.assertIsNone(ev.amount)

    def test_every_spec_line_is_not_unknown(self) -> None:
        for line, _ in SPEC_LINES + WRAPPED_LINES + NOISE_LINES:
            with self.subTest(line=line):
                self.assertNotEqual(parse_line(line, 0.0).kind, "unknown")

    def test_kind_is_always_a_known_kind(self) -> None:
        for line, _ in SPEC_LINES + WRAPPED_LINES + NOISE_LINES:
            self.assertIn(parse_line(line, 0.0).kind, grammar.KINDS)

    def test_never_raises_on_garbage(self) -> None:
        for bad in [None, 42, "\x00\x01", "!" * 500, "a " * 300 + "zombie"]:
            ev = parse_line(bad, 0.0)  # type: ignore[arg-type]
            self.assertEqual(ev.kind, "unknown")


class PlayerNameMappingTests(unittest.TestCase):
    def test_you_forms_map_to_player_name(self) -> None:
        cases = [
            ("You crush a stumbling zombie for 8 points of damage.", "actor", "You"),
            ("Your Cudgel of Light hits a stumbling zombie for 3 points of Holy Damage.", "actor", "Your"),
            ("Your casting is interrupted.", "actor", "Your"),
            ("You begin casting Cudgel of Light.", "actor", "You"),
            ("You have slain a stumbling zombie!", "actor", "You"),
        ]
        for line, field_name, raw in cases:
            with self.subTest(line=line):
                ev = parse_line(line, 0.0, "Maergoth")
                self.assertEqual(getattr(ev, field_name), "Maergoth")
                self.assertEqual(ev.raw_actor, raw)

    def test_you_as_target_maps_to_player_name(self) -> None:
        ev = parse_line("a stumbling zombie bites YOU for 20 points of damage.", 0.0, "Maergoth")
        self.assertEqual(ev.actor, ZOMBIE)
        self.assertEqual(ev.target, "Maergoth")
        self.assertEqual(ev.raw_actor, ZOMBIE)
        ev = parse_line("You have been slain by a stumbling zombie!", 0.0, "Maergoth")
        self.assertEqual(ev.target, "Maergoth")
        self.assertEqual(ev.actor, ZOMBIE)

    def test_without_player_name_you_is_canonical(self) -> None:
        ev = parse_line("a stumbling zombie bites YOU for 20 points of damage.", 0.0)
        self.assertEqual(ev.target, "You")
        ev = parse_line("Your Cudgel of Light hits a stumbling zombie for 3 points of Holy Damage.", 0.0)
        self.assertEqual(ev.actor, "You")
        self.assertEqual(ev.raw_actor, "Your")

    def test_other_names_untouched(self) -> None:
        ev = parse_line("Tovozen crushes a stumbling zombie for 2 points of damage.", 0.0, "Maergoth")
        self.assertEqual(ev.actor, "Tovozen")
        self.assertEqual(ev.raw_actor, "Tovozen")

    def test_map_player_helper(self) -> None:
        self.assertEqual(map_player("YOU", "Pidef"), "Pidef")
        self.assertEqual(map_player("Your", ""), "You")
        self.assertEqual(map_player("yourself", "Pidef"), "Pidef")
        self.assertEqual(map_player("Tovozen", "Pidef"), "Tovozen")
        self.assertIsNone(map_player(None, "Pidef"))


class NormalizeOcrTests(unittest.TestCase):
    def test_split_digits(self) -> None:
        self.assertEqual(normalize_ocr("x for 1 1 points of damage."), "x for 11 points of damage.")
        self.assertEqual(normalize_ocr("x for I I points of damage."), "x for 11 points of damage.")
        self.assertEqual(normalize_ocr("x for II points of damage."), "x for 11 points of damage.")
        self.assertEqual(normalize_ocr("x for I point of damage."), "x for 1 point of damage.")
        self.assertEqual(normalize_ocr("x for -1 point of damage."), "x for 1 point of damage.")

    def test_word_fixes(self) -> None:
        self.assertEqual(normalize_ocr("for 6 poilifs of damage."), "for 6 points of damage.")
        self.assertEqual(normalize_ocr("for 6 polnts of damage."), "for 6 points of damage.")
        self.assertEqual(normalize_ocr("for 2 points of Carnage."), "for 2 points of damage.")
        self.assertEqual(normalize_ocr("for 2 points of dama e"), "for 2 points of damage")
        self.assertEqual(normalize_ocr("for 2 points of clanma#."), "for 2 points of damage.")
        # Capitalised damage types are left alone.
        self.assertEqual(normalize_ocr("for 2 points of Holy Damage."), "for 2 points of Holy Damage.")

    def test_dropped_apostrophe(self) -> None:
        self.assertEqual(normalize_ocr("Tovozens Heal heals Wululiso for 56 Health."),
                         "Tovozen's Heal heals Wululiso for 56 Health.")
        self.assertEqual(normalize_ocr("Pidefs Backstab hits a stumbling zombie for 24 points of damage."),
                         "Pidef's Backstab hits a stumbling zombie for 24 points of damage.")
        # Not applied to plain melee lines (verb is lowercase).
        self.assertEqual(normalize_ocr("Pidef pierces a stumbling zombie for 2 points of damage."),
                         "Pidef pierces a stumbling zombie for 2 points of damage.")

    def test_you_variants_and_leading_junk(self) -> None:
        self.assertEqual(normalize_ocr("you-try to attack, but you are too far away."),
                         "You try to attack, but you are too far away.")
        self.assertEqual(normalize_ocr("Vou try to attack, but you are too far away."),
                         "You try to attack, but you are too far away.")
        self.assertEqual(normalize_ocr("{Your party member Pidef has slain a stumbling zombie!"),
                         "Your party member Pidef has slain a stumbling zombie!")
        self.assertEqual(normalize_ocr("  Stopped   attacking.  "), "Stopped attacking.")

    def test_missing_spaces_around_numbers(self) -> None:
        self.assertEqual(normalize_ocr("zombie for2points of damage."), "zombie for 2 points of damage.")
        self.assertEqual(normalize_ocr("Wululiso for 56Health."), "Wululiso for 56 Health.")


class GrammarHelperTests(unittest.TestCase):
    def test_rules_shape_and_order(self) -> None:
        kinds = [k for k, _ in grammar.RULES]
        self.assertTrue(all(k in grammar.KINDS for k in kinds))
        for _, rx in grammar.RULES:
            self.assertTrue(hasattr(rx, "match"))
        # Specific rules precede looser ones.
        self.assertLess(kinds.index("kill"), kinds.index("melee_hit"))
        self.assertLess(kinds.index("cannot_attack"), kinds.index("melee_miss"))
        self.assertLess(kinds.index("heal"), kinds.index("ability_hit"))
        self.assertLess(kinds.index("ability_hit"), kinds.index("melee_hit"))
        self.assertLess(kinds.index("melee_miss"), kinds.index("melee_hit"))
        self.assertLess(kinds.index("melee_hit"), kinds.index("status"))
        self.assertEqual(kinds[-1], "status")

    def test_lemmatize(self) -> None:
        table = {
            "crushes": "crush", "pierces": "pierce", "slashes": "slash", "bites": "bite",
            "bashes": "bash", "kicks": "kick", "jabs": "jab", "hits": "hit", "punches": "punch",
            "claws": "claw", "stings": "sting", "gores": "gore", "mauls": "maul", "smashes": "smash",
            "dodges": "dodge", "parries": "parry", "blocks": "block", "ripostes": "riposte",
            "misses": "miss", "miss": "miss", "crush": "crush", "dodge": "dodge",
            # generic fallback
            "lashes": "lash", "thrashes": "thrash", "scratches": "scratch", "whips": "whip",
            "slices": "slice", "flies": "fly",
        }
        for verb, lemma in table.items():
            with self.subTest(verb=verb):
                self.assertEqual(grammar.lemmatize(verb), lemma)

    def test_is_npc_name(self) -> None:
        self.assertTrue(grammar.is_npc_name("a stumbling zombie"))
        self.assertTrue(grammar.is_npc_name("an orc pawn"))
        self.assertTrue(grammar.is_npc_name("the rat king"))
        self.assertFalse(grammar.is_npc_name("Tovozen"))
        self.assertFalse(grammar.is_npc_name("You"))
        self.assertFalse(grammar.is_npc_name(None))

    def test_miss_outcome(self) -> None:
        self.assertEqual(miss_outcome("miss", ZOMBIE), "miss")
        self.assertEqual(miss_outcome("misses", "YOU"), "miss")
        self.assertEqual(miss_outcome("Wululiso dodges", "Wululiso"), "dodge")
        self.assertEqual(miss_outcome("a stumbling zombie parries", ZOMBIE), "parry")
        self.assertEqual(miss_outcome("you dodge", "YOU"), "dodge")
        self.assertEqual(miss_outcome("Wululiso blocks with their shield", "Wululiso"), "block")
        self.assertEqual(miss_outcome(None, "Wululiso"), "miss")


class FixtureSmokeTests(unittest.TestCase):
    """Every anonymized OCR line in the fixture parses without raising and the
    clean, complete lines are classified (not unknown)."""

    FIXTURE = _ROOT / "tests" / "fixtures" / "burst_ocr.json"

    def test_fixture_lines_never_raise_and_known_lines_classify(self) -> None:
        if not self.FIXTURE.exists():
            self.skipTest("fixture missing")
        data = json.loads(self.FIXTURE.read_text(encoding="utf-8"))
        texts = {ln["text"] for fr in data["frames"] for ln in fr["lines"]}
        self.assertGreater(len(texts), 50)
        kinds = {t: parse_line(t, 0.0).kind for t in texts}
        self.assertTrue(all(k in grammar.KINDS for k in kinds.values()))
        must_classify = {
            "Nirek pierces a stumbling zombie for 2 points of damage.": "melee_hit",
            "a stumbling zombie bites Talalino for I I points of damage.": "melee_hit",
            "a stumbling zombie bites Talalino for 1 1 points of damage.": "melee_hit",
            "Talalino pierces a stumbling zombie for I point of damage.": "melee_hit",
            "You crush a stumbling zombie for -1 point of damage.": "melee_hit",
            "You try to attack, but you must face your target.": "cannot_attack",
            "Your casting is interrupted.": "interrupt",
            "Davaren begins casting Heal.": "cast",
            "a stumbling zombie tries to bite YOU, but misses!": "melee_miss",
            "You try to crush a stumbling zombie, but miss!": "melee_miss",
            "Davarens Heal heals Talalino for 56 Health.": "heal",
            "Davaren's Heal heals Talalino for 56 Health.": "heal",
            "Nirek's Slice hits a stumbling zombie for 6 points of damage.": "ability_hit",
            "Your party member Nirek has slain a stumbling zombie!": "kill",
            "a stumbling zombie's armor breaks.": "status",
            "Stopped attacking.": "status",
            "Nirek jabs a stumbling zombie.": "status",
        }
        for text, kind in must_classify.items():
            with self.subTest(text=text):
                self.assertIn(text, texts, "fixture no longer contains this line")
                self.assertEqual(kinds[text], kind)
        self.assertEqual(parse_line("a stumbling zombie bites Talalino for I I points of damage.", 0.0).amount, 11)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
