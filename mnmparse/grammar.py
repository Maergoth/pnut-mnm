"""Regex grammar for the Monsters & Memories combat window (SPEC section 3).

This is a pure *data* module: compiled regular expressions with named
groups, a small verb-lemma table and the :class:`Event` record that
:func:`mnmparse.parser.parse_line` produces.  It imports nothing from the
rest of the package so any module can import it without cycles
(``parser`` re-exports :class:`Event`, so ``from mnmparse.parser import
Event`` works too).

Rule order matters.  :data:`RULES` is tried top to bottom and the first
match wins, so the more specific shapes (kill, cannot_attack, heal,
ability_hit) come before the looser ones (melee, cast, status).

Name forms (see SPEC section 3):

* player names are a single capitalised word (``Tovozen``), including the
  pronoun forms ``You`` / ``Your`` / ``YOU``;
* NPC names are an article followed by lowercase words
  (``a stumbling zombie``).

Every rule is anchored at both ends; the shared ``TAIL`` tolerates the
terminal punctuation the game prints (``.`` ``!``) as well as the commas,
colons and dashes OCR sometimes substitutes for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "Event",
    "KINDS",
    "YOU_TOKENS",
    "PLAYER",
    "NPC",
    "NAME",
    "RULES",
    "DAMAGE_EFFECT_OUTCOMES",
    "VERB_LEMMAS",
    "lemmatize",
    "is_npc_name",
    "is_you",
]


@dataclass
class Event:
    """One parsed combat-window message.

    ``ts`` is the time the message was first seen (seconds, ``time.time()``
    style); ``text`` is the message exactly as it was given to the parser.
    ``actor`` and ``target`` are already mapped through the configured
    player name (``You``/``YOU``/``Your`` -> player); ``raw_actor`` keeps
    the original actor token so the mapping is reversible.

    ``outcome`` is ``"hit"`` for damaging events, the avoidance verb lemma
    (``"miss"``, ``"dodge"``, ``"parry"``, ``"block"``, ``"riposte"`` ...)
    for ``melee_miss``, and the reason text for ``cannot_attack``.
    """

    ts: float
    kind: str
    text: str
    actor: str | None = None
    target: str | None = None
    amount: int | None = None
    skill: str | None = None
    dtype: str | None = None
    outcome: str | None = None
    raw_actor: str | None = None
    absorbed: int | None = None  #: "(2 absorbed)" mitigation on a hit line
    blocked: int | None = None  #: "(Block 6)" mitigation on a hit line
    weapon: str | None = None  #: "with their offhand" / "with their bow"
    is_pet: bool = False  #: the actor was written as "Your pet <Name>"
    estimated: bool = False  #: Dummy Fix: the number was unreadable; ``amount`` is the zone average
    item: str | None = None  #: looted / crafted item name
    copper: int | None = None  #: coin amount in copper (1 pp = 10 gp = 100 sp = 1000 cp)
    split_copper: int | None = None  #: the viewer's share of a coin loot, in copper


KINDS: tuple[str, ...] = (
    "melee_hit",
    "melee_miss",
    "ability_hit",
    "ability_partial",  # "Gozif's Slice hits a for 3+oints,ofr..." : who used which ability, rest unreadable
    "damage_effect",  # damage-over-time applied, with no damage amount: "X is bleeding out."
    "env_damage",  # "YOU take 3 damage from falling!": damage taken from the world, no attacker
    "ability_miss",
    "heal",
    "cast",
    "interrupt",
    "resist",
    "fizzle",
    "kill",
    "experience",
    "marker",
    "status",
    "cannot_attack",
    "loot",  # "--Abepulifif loots [Bone Chips] from a skeletal marksman's corpse.--"
    "coin",  # "Povebizu loots 7 copper coins from X's corpse, and you receive 1 copper coin ... as your split."
    "coin_split",  # "22 copper coins from a dunes madman's corpse as your split." (a coin line's wrapped end)
    "reward",  # "You receive Ancient Chant from Jalwa Noor." (a quest hand-in; no corpse)
    "vendor",  # "You sell Cracked Staff for 5 silver coins." (outcome sell / buy / train, copper = price)
    "craft",  # "Povebizu crafts Heavy Cloth Bandage."
    "cc",  # crowd control landed: "a skeletal defender is mesmerized." / "You are stunned!"
    "cc_fade",  # "a skeletal defender is no longer mesmerized."
    "personal",  # lines only the viewer gets about themselves: skill-ups, faction, PvP opt-in, eating
    "consider",  # "a Wyrmsbane crusader seethes at you, ready to strike" (/con text, viewer only)
    "zone",  # "You have entered Night Harbor (East)."
    "debuff",  # debuff landed on a victim: "a skeletal fighter is condemned." (outcome = type)
    "aggro",  # "a skeletal warrior looks angry at Wululiso." (actor = mob, target = who holds it now)
    "awaken",  # "a skeletal warrior awakens." = a mesmerize effect broke
    "chat",  # "a watchman says, "Hail! How may I assist you?"" (actor = speaker)
    "level_up",  # "Gozif has leveled up! They are now level 7!" (amount = the new level, when given)
    "unknown",
)

DEBUFF_OUTCOMES: dict[str, str] = {
    "condemned": "condemned",
    "frays": "resist down",
    "fray": "resist down",
    "weaken": "arcane weakened",  # "X's arcane defenses weaken." (Arcane Infusion)
    "tormented": "tormented",
    "exhausted": "lockout",
    "slow": "slowed",  # "X's movements begin to slow."
    "irregularly": "faltering pulse",  # "X's heart begins beating irregularly."
    "vigor": "vigor drained",  # "X reels as vigor flows from their body." (Theft of Vigor)
    "chilled": "chilled",  # "X is chilled to the bone."
    "weak": "weakened",  # "X looks weak."; source is inferred from a recent attempt, not the text
}
"""Debuff result-line keyword -> type shown in the Utility breakdown."""

DAMAGE_EFFECT_OUTCOMES: dict[str, str] = {
    "bleed": "bleeding",
    "barbed": "barbed arrow",
}
"""Damage-only application messages; their numbered ticks account for the damage."""

CC_OUTCOMES: dict[str, str] = {
    "stunned": "stun",
    "dazed": "stun",
    "mesmerized": "mez",
    "rooted": "root",
    "immobilized": "root",
    "held": "root",
    "pinned": "root",  # Pinning Shot
    "bound": "root",  # "You are bound by a net shot."
    "adhere": "root",  # "X adheres to the ground."
    "unstuck": "root",  # "X comes unstuck." (the cc_fade of the line above)
    "silenced": "silence",
    "snared": "snare",
    "slowed": "snare",  # "You are slowed by a snaring shot."
    "feared": "fear",
    "intimidated": "fear",
    "charmed": "charm",
    "blinded": "blind",
}
"""Result-line adjective -> crowd-control category."""

#: 100 copper = 1 silver, 100 silver = 1 gold, 100 gold = 1 platinum.
COIN_IN_COPPER: dict[str, int] = {"platinum": 1_000_000, "gold": 10_000, "silver": 100, "copper": 1}
"""Every ``Event.kind`` the parser can emit."""

YOU_TOKENS: frozenset[str] = frozenset(
    {"You", "Your", "YOU", "YOUR", "you", "your", "yourself", "Yourself"}
)
"""Actor/target tokens that denote the local player."""

# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

PLAYER: str = r"[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*){0,2}"
"""A player name (one capitalised word, also You / Your / YOU) or a named NPC of up
to three capitalised words (``Toilmaster Verith``)."""

_NPC_STOP: str = r"(?!(?:with|for|but|on|to|from|is|are|has|have|was|were)\b)"
NPC: str = rf"(?:a|an|the)\s+[A-Za-z][a-z\-]*(?:\s+{_NPC_STOP}[A-Za-z][a-z\-]*)*(?:'s\s+pet)?"
"""An NPC name: a lowercase article followed by one or more words, which may be
capitalised proper nouns (``a Wyrmsbane crusader``, ``a Dustrend priest``).  The words
that introduce the rest of a sentence (``with their bow``, ``for 3 points``,
``but they absorb``) can never be part of the name. An NPC's unnamed pet
(``a Bloodynose frightener's pet``) is a distinct combatant."""

NAME: str = rf"(?:{PLAYER}|{NPC})"
"""Either name form.  Reused as the actor and target sub-pattern."""

_POSSESSIVE: str = rf"(?P<actor>Your|{NAME}'s(?!\s+pet\b))"
"""``Your <Skill>`` or ``<Name>'s <Skill>``.  The ``'s`` is mandatory: with it optional,
"a skeletal warrior hits X for 1 point of damage" parsed as actor "a skeletal" using an
ability called "warrior" (the NPC name split in two).  The parser re-inserts an
apostrophe the OCR dropped ("Tovozens Heal", "Tovozen s Heal") before matching."""

_DTYPE: str = r"[A-Za-z]+(?:\s+[A-Za-z]+)*"
_AMOUNT: str = r"(?P<amount>\d+)"
_POINTS: str = r"points?"
_COIN_PART: str = r"[\dIlO|]+\s+(?:platinum|gold|silver|copper)(?:\s+coins?)?"
_MULTI_COIN: str = rf"{_COIN_PART}(?:,?\s+(?:and\s+)?{_COIN_PART})+"
_TAIL: str = r"""\s*[.!?,:;'"\-]*\s*$"""
_SENTENCE_END: str = r"\s*[.!?]+\s*$"
_NOT_FUNCTION_WORD: str = (
    r"(?!(?:in|at|to|on|of|with|from|for|by|and|but|is|are|was|has|have|the|a|an)\b)"
)

_MODS: str = r"(?:\s+with\s+(?:their|your|his|her|its)\s+(?P<weapon>[a-z]+))?"
"""Optional weapon-slot modifier on melee lines: ``with their offhand`` / ``with their bow``."""

_MARK: str = (
    r"(?:\s*[.!]?\s*\((?:(?P<absorbed>\d+)\s+absorbed|Block\s+(?P<blocked>\d+)"
    r"|(?P<crit>Critical|Crippling\s+Blow))\)){0,3}"
)
"""Trailing hit markers, including combined ``(2 absorbed). (Block 6)`` mitigation."""

_DTYPE_WORDS: str = r"(?:Holy|Bleed|Fire|Cold|Magic|Corruption|Damage|Health|Points|Of)"
"""Capitalised words that start a wrapped *tail* fragment, never a message."""

_ABILITY_VERBS: str = (
    r"(?:hits|bleeds|burns|shocks|blasts|smites|scorches|freezes|chills|strikes|pierces|slashes|"
    r"crushes|wounds|zaps|drains|sears|blights|rends|lashes|stings)"
)
"""Verbs seen between an ability name and its target: ``hits`` for most, ``bleeds`` for
damage-over-time ticks (``X's Barbed Arrow bleeds Y for 3 points of Bleed Damage.``)."""

_NPC_GREEDY: str = r"(?:a|an|the)\s+[A-Za-z][a-z\-]*(?:\s+[a-z][a-z\-]*)*"
"""An NPC name with no stop words (for the status catch-all, where the sentence shape
after the name decides where the name ends)."""

_CC_WORDS: str = (
    r"stunned|mesmerized|rooted|immobilized|silenced|snared|feared|intimidated|charmed|dazed|held|pinned|bound|slowed|blinded"
)
"""``X is <word>`` / ``X is no longer <word>``: a crowd-control result (see :data:`CC_OUTCOMES`)."""

_RULE_SOURCES: list[tuple[str, str]] = [
    # ---- MARKER on its own row (the hit line it belongs to was emitted just before) --
    ("marker", rf"^\(\s*(?:Block\s+(?P<blocked>\d+)|(?P<absorbed>\d+)\s+absorbed|(?P<crit>Critical|Crippling\s+Blow))\s*\){_TAIL}"),
    # ---- KILL -----------------------------------------------------------
    ("kill", rf"^Your\s+party\s+member\s+(?P<target>{PLAYER})\s+has\s+been\s+slain\s+by\s+(?P<actor>{NAME}){_TAIL}"),
    ("kill", rf"^Your\s+party\s+member\s+(?P<actor>{PLAYER})\s+has\s+slain\s+(?P<target>{NAME}){_TAIL}"),
    ("kill", rf"^(?P<target>{NAME})\s+(?:has|have)\s+been\s+slain\s+by\s+(?P<actor>{NAME}){_TAIL}"),
    ("kill", rf"^(?P<actor>{NAME})\s+(?:has|have)\s+slain\s+(?P<target>{NAME}){_TAIL}"),
    # ---- EXPERIENCE -----------------------------------------------------
    ("experience", rf"^You\s+[gz]ain\s+(?:party\s+)?e[xk]perience[!.lI1|]?(?:\s*-|e)?{_TAIL}"),
    ("level_up", rf"^(?P<actor>You|{PLAYER})\s+(?:has|have)\s+leveled\s+up[!lI1|]?(?:\s*They\s+are\s+now\s+level\s+(?P<level>[\dIlO|][\dIlO| ]*!?))?{_TAIL}"),
    ("level_up", rf"^(?P<actor>You)\s+are\s+now\s+level\s+(?P<level>[\dIlO|][\dIlO| ]*!?){_TAIL}"),
    # ---- LOOT / COIN / CRAFT (the "--" frame around loot lines is stripped by the parser) --
    # Your own corpse (after a death): your own things back, not loot.
    ("personal", rf"^You\s+loot\s+(?:(?P<amount>\d+)\s+(?P<dtype>platinum|gold|silver|copper)\s+coins?|[\[\(]?(?P<item>[^\[\]\(\)]+?)[\]\)JlI|]?)\s+from\s+your\s+(?P<outcome>corpse)\s*[.\-\s]*$"),
    ("coin", rf"^(?P<actor>You|{PLAYER})\s+loots?\s+(?P<price>{_MULTI_COIN})\s+from\s+(?P<target>{NAME})(?:'s|s)?\s+corpse(?P<split>.*)$"),
    ("coin", rf"^(?P<actor>You|{PLAYER})\s+loots?\s+(?P<amount>\d+)\s+(?P<dtype>platinum|gold|silver|copper)\s+coins?\s+from\s+(?P<target>{NAME})(?:'s|s)?\s+corpse(?P<split>.*)$"),
    # the NPC name and "'s corpse," cut off by the window edge: "Gozif loots 9 copper coins from
    # a risen and you receive 2 copper corpse as your split."
    ("coin", rf"^(?P<actor>You|{PLAYER})\s+loots?\s+(?P<amount>\d+)\s+(?P<dtype>platinum|gold|silver|copper)\s+coins?\s+from\s+(?P<target>(?:a|an|the)\s+[a-z][a-z\-]*(?:\s+[a-z][a-z\-]*)*?)(?P<split>,?\s+and\s+(?:you\s+)?rece\w*\s.*)$"),
    # the end of a coin line that wrapped onto its own row (the split may name no denomination)
    ("coin_split", rf"^(?P<price>{_MULTI_COIN})(?:\s+from\s+(?P<target>{NAME})(?:'s|s)?\s+corpse)?\s+(?:as\s+)?your\s+split{_TAIL}"),
    ("coin_split", rf"^(?P<amount>\d+|O)\s+(?:(?P<dtype>platinum|gold|silver|copper)\s+)?coins?(?:\s+from\s+(?P<target>{NAME})(?:'s|s)?\s+corpse)?\s+(?:as\s+)?your\s+split{_TAIL}"),
    ("loot", rf"^(?P<actor>You|{PLAYER})\s+loots?\s+[\[\(]?(?P<item>[^\[\]\(\)]+?)[\]\)]?\s+from\s+(?P<target>{NAME})(?:'s|s)?\s+corpse\s*[.\-\s]*$"),
    ("reward", rf"^(?P<actor>You)\s+receive\s+[\[\(]?(?P<item>[^\[\]\(\)]+?)[\]\)]?(?:\s+from\s+(?P<target>{NAME}))?{_TAIL}"),
    ("craft", rf"^(?P<actor>You|{PLAYER})\s+crafts?\s+(?P<item>.+?)(?:\s*\((?P<amount>\d+)\))?{_TAIL}"),
    # "You sell Water Flask (x3) for 3 copper coins." / "You buy Ration for 1 silver, and 20 copper
    # coins." / "You train Wagoneering for 5 copper."  (the price is read by the parser)
    ("vendor", rf"^(?P<actor>You)\s+(?P<outcome>sell|buy|train)\s+(?P<item>.+?)(?:\s*\(x?\s*(?P<amount>\d+)\))?\s+for\s+(?P<price>[\dIlO|]+\s+(?:platinum|gold|silver|copper)\b.*?)(?:\s+coins?)?{_TAIL}"),
    # ---- PERSONAL (only the viewer sees these about themselves; excluded from group stats) --
    # (also "got worsev", "cann possibly get", "&nnot possi ly get": the experience line glued
    # to its end is cut off before this, see parser.split_fused)
    ("personal", rf"^Your\s+faction\s+standing\s+with\s+(?P<target>.+?)\s+(?:got\s*(?P<outcome>better|worse)[a-z]?[.!,]?|(?:\S+\s+)?possi\w*\b.*)$"),
    ("personal", r"^Yo\w*\s+facti\w*\s+standi\w*\b[^\d]*$"),  # garbled or cut off; no second message
    ("personal", r"^[Tt]his\s+ability\s+is\s+not\s+available\s+ri\w*\s+now\b.*$"),
    ("personal", r"^You\s+are\s+already\s+casting\s+another\s+ability\b.*$"),
    ("personal", r"^You\s+cannot\s+cast\s+spells\s+from\s+that\s+school\s+of\s+magic[lI1|]?\b.*$"),
    ("personal", r"^You\s+have\s+everything\s+you\s+need,\s+but\s+.+$"),
    ("personal", r"^It\s+is\s+locked,\s+and\s+you\s+are\s+not\s+holding\s+the\s+key\b.*$"),
    ("personal", r"^Your\s+skin\s+tingles\b.*$"),
    ("personal", r"^You\s+(?:are\s+(?:starving|hungry|thirsty)\b.*|have\s+nothin\w*\s+to\s+(?:eat|drink)\b.*)$"),
    # no experience for the viewer from this kill (outcome "too high level")
    ("personal", r"^Someone\s+in\s+your\s+party\s+is\s+(?P<outcome>too\s+high\s+level)\s+for\s+you\s+to\s+receive\s+experience\b.*$"),
    ("personal", rf"^You\s+harvest\s+[\[\(]?(?P<item>[^\[\]\(\)]+?)[\]\)JlI|]?(?:\s*[lI1|])?{_TAIL}"),
    ("personal", r"^(?:Your\s+camp\s+will\s+be\s+prepared\s+in|You\s+cannot\s+invite|You\s+have\s+not\s+received\s+any\s+tells|No\s+ability\s+memorized\s+in\s+Gem)\b.*$"),
    # /who: "Players in Monsters & Memories" + "There are 35 players in Shaded Dunes who match your search."
    ("personal", r"^(?:Players\s+in\s+Monsters\b|There\s+(?:are|is)\s+\d+\s+players?\s+in\s+.+?\s+who\s+match\b).*$"),
    # ---- CHAT shown in the Combat window: "Mayana says, "Hail!"" / "You say, "Hail, Mayana."" --
    ("chat", rf"""^(?P<actor>You|{NAME})\s+(?:says|say|sav)\s*[,.:;]\s*["'].*$"""),
    # ---- CONSIDER / NPC taunt text shown to the viewer ----------------------------------
    ("consider", rf"^(?P<target>{NAME})\s+(?:(?:seethes|glares|glowers|scowls|regards|looks)\s+at\s+you|seems\s+indifferent\s+to\s+your\s+presence|views\s+you\s+as\s+a\s+threat|appears\s+uneasy\s+with\s+you)\b.*$"),
    # a player: "Gozif Battle with them would be quite risky." / "Gozif You would probably be defeated by them in battle."
    ("consider", rf"^(?P<target>{PLAYER})\s+(?:Battle\s+with\s+them\s+would|You\s+would\s+(?:\w+\s+){{0,3}}(?:be\s+defeated|emerge\s+victorious))\b.*$"),
    ("consider", r"^(?:Would\s+you\s+like\s+to\s+die|You\s+would\s+(?:\w+\s+){0,2}be\s+defeated|be\s+defeated\s+in\s+battle|Battle\s+with\s+them\s+would|What\s+would\s+you\s+like\s+your\s+tombstone)\b.*$"),
    # a corpse: "Gozif is dead. Their corpse will decay in 5 days and 19 hours."
    ("consider", rf"^(?P<target>{NAME})\s+is\s+dead\W+Their\s+corpse\s+will\s+decay\b.*$"),
    # ---- ZONE changes --------------------------------------------------------------------
    ("zone", r"^(?:Entering|You\s+have\s+entered)\s+(?P<target>.+?)\.?$"),
    ("zone", r"^Loading,?\s+please\s+wait\b.*$"),
    ("personal", rf"^Your\s+skill\s+in\s+(?P<skill>.+?)\s+has\s+increased!?\s*(?:\((?P<amount>\d+)\))?{_TAIL}"),
    ("personal", rf"^(?P<actor>You|{PLAYER})\s+opts?\s+into\s+higher\s+level\s+PvP{_TAIL}"),
    ("personal", rf"^You\s+are\s+no\s+longer\s+opted\s+into\s+higher\s+level\s+PvP(?:\s+combat)?{_TAIL}"),
    ("personal", rf"^You\s+(?:eat|drink)\s+(?P<item>.+?){_TAIL}"),
    ("personal", rf"^(?:There\s+is\s+nothing\s+to\s+forage\s+here|You\s+forage\s+.+?|You\s+have\s+foraged\s+.+?){_TAIL}"),
    ("personal", rf"^You\s+(?:cannot\s+scribe|must\s+be\s+level|have\s+learned)\b.*$"),
    ("personal", r"^Welcome\s+to\s+Monsters\b.*$"),
    # ---- DEBUFF results / aggro / mez breaks -----------------------------------------
    ("debuff", rf"^(?P<target>{NAME})\s+(?:is|are)\s+(?P<outcome>condemned){_TAIL}"),
    ("damage_effect", rf"^(?P<target>{NAME})\s+(?:is|are)\s+(?P<outcome>bleed)ing\s+out{_TAIL}"),
    ("damage_effect", rf"^(?P<target>{NAME})\s+begins?\s+to\s+(?P<outcome>bleed)\s+profusely{_TAIL}"),
    ("damage_effect", rf"^(?P<target>{NAME})\s+(?:is|are)\s+struck\s+by\s+a\s+(?P<outcome>barbed)\s+arrow{_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})'s\s+magical\s+resistance\s+(?P<outcome>frays?){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})'s\s+arcane\s+defenses\s+(?P<outcome>weaken){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})'s\s+mind\s+is\s+(?P<outcome>tormented){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})\s+looks?\s+mentally\s+(?P<outcome>exhausted){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})'s\s+movements\s+begin\s+to\s+(?P<outcome>slow){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})'s\s+heart\s+begins\s+beating\s+(?P<outcome>irregularly){_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})\s+reels\s+as\s+(?P<outcome>vigor)\s+flows\s+from\s+their\s+body{_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})\s+(?:is|are)\s+(?P<outcome>chilled)\s+to\s+the\s+bone{_TAIL}"),
    ("debuff", rf"^(?P<target>{NAME})\s+looks?\s+(?P<outcome>weak){_TAIL}"),
    ("aggro", rf"^(?P<actor>{NAME})\s+looks\s+angry\s+at\s+(?P<target>{NAME}){_TAIL}"),
    ("awaken", rf"^(?P<target>{NAME})\s+awakens{_TAIL}"),
    # ---- CROWD CONTROL results -------------------------------------------
    ("cc_fade", rf"^(?P<target>{NAME})\s+(?:is|are)\s+no\s+longer\s+(?P<outcome>{_CC_WORDS})(?:\s+by\s+(?P<skill>[a-z][a-z' ]+?))?{_TAIL}"),
    ("cc_fade", rf"^(?P<target>{NAME})\s+breaks?\s+free\s+from\s+(?P<skill>.+?){_TAIL}"),
    ("cc_fade", rf"^(?P<target>{NAME})\s+breaks?\s+free\s+of\s+(?P<skill>the\s+webs){_TAIL}"),
    ("cc_fade", rf"^(?P<target>{NAME})\s+comes?\s+(?P<outcome>unstuck){_TAIL}"),
    ("cc", rf"^(?P<target>{NAME})\s+(?:is|are)\s+(?P<outcome>{_CC_WORDS})(?:\s+to\s+the\s+ground)?(?:\s+by\s+(?P<skill>[a-z][a-z' ]+?))?{_TAIL}"),
    ("cc", rf"^(?P<target>{NAME})\s+(?P<outcome>adhere)s?\s+to\s+the\s+ground{_TAIL}"),
    # ---- CANNOT ATTACK (not a miss) -------------------------------------
    ("cannot_attack", rf"^(?P<actor>{NAME})\s+(?:try|tries)\s+to\s+attack\s*[,.]?\s*but\s+(?P<outcome>[^.!?]+?){_TAIL}"),
    ("cannot_attack", rf"^You\s+(?P<outcome>must\s+face\s+your\s+target|must\s+be\s+able\s+to\s+see\s+your\s+target|need\s+a\s+target)\s+to\s+use\s+that\s+ability[lI1|]?{_TAIL}"),
    ("cannot_attack", rf"^Your\s+target\s+is\s+(?P<outcome>too\s+far\s+away)\s+to\s+use\s+that\s+ability[lI1|]?{_TAIL}"),
    # ---- HEAL -----------------------------------------------------------
    # Only the possessive form has been observed ("Tovozen's Heal heals X").  A
    # bare "<Name> heals X" rule is deliberately absent: the clipped fragment
    # "Heal heals Wululiso for 56 Health." would otherwise yield actor "Heal".
    ("heal", rf"^{_POSSESSIVE}\s+(?P<skill>.+?)\s+heals\s+(?P<target>{NAME}|yourself|you|them|em)\s+for\s+{_AMOUNT}\s+(?P<dtype>[A-Za-z]+){_TAIL}"),
    # ---- RESIST / FIZZLE / ABILITY MISS ---------------------------------
    ("resist", rf"^(?P<actor>{NAME})\s+(?:try|tries)\s+to\s+cast\s+(?P<skill>.+?)\s+on\s+(?P<target>{NAME})[,.]?\s*but\s+is\s+resisted{_TAIL}"),
    ("resist", rf"^(?P<target>{NAME})\s+resists\s+(?P<actor>your|Your|{NAME}(?:'s)?)\s+(?P<skill>.+?){_TAIL}"),
    ("fizzle", rf"^{_POSSESSIVE}\s+spell\s+fizzles{_TAIL}"),
    ("ability_miss", rf"^{_POSSESSIVE}\s+ability\s+misses{_TAIL}"),
    # ---- ABILITY / SPELL / PROC HIT -------------------------------------
    # Separate numbered damage in addition to the spell's regular hit/tick.
    ("ability_hit", rf"^{_POSSESSIVE}\s+(?P<skill>.+?)\s+bites\s+deeper\s+into\s+the\s+undead,\s+dealing\s+{_AMOUNT}\s+extra\s+{_POINTS}\s+of\s+(?P<dtype>{_DTYPE})\s+to\s+(?P<target>{NAME}){_MARK}{_TAIL}"),
    ("ability_hit", rf"^{_POSSESSIVE}\s+(?P<skill>.+?)\s+(?P<verb>{_ABILITY_VERBS})\s+(?P<target>{NAME})\s+for\s+{_AMOUNT}\s+{_POINTS}\s+of\s+(?P<dtype>{_DTYPE}){_MARK}{_TAIL}"),
    # ---- MELEE MISS / AVOIDANCE -----------------------------------------
    ("melee_miss", rf"^(?P<actor>{NAME})\s+(?:try|tries)\s+to\s+(?P<skill>[a-z]{{3,}})\s+(?:at\s+)?(?P<target>{NAME}){_MODS}\s*[,.]?\s*but\s+(?P<outcome>miss(?:es)?|they\s+absorb(?:\s+the\s+(?:attack|blow))?|(?:you|{NAME})\s+[a-z]+(?:\s+[a-z]+)*){_TAIL}"),
    ("melee_miss", rf"^(?P<actor>{NAME})\s+(?P<skill>[a-z]{{3,}})\s+(?P<target>{NAME}){_MODS}\s*[,.]?\s*but\s+(?P<outcome>they\s+absorb(?:\s+the\s+(?:attack|blow))?|[a-z]+\s+absorbs?\s+the\s+(?:attack|blow)){_TAIL}"),
    # ---- MELEE HIT ------------------------------------------------------
    # (also thrown weapons: "Gozif throws at a caiman for 5 points of damage.")
    ("melee_hit", rf"^(?P<actor>{NAME})\s+(?P<skill>[a-z]{{3,}})\s+(?:at\s+)?(?P<target>{NAME}){_MODS}\s+for\s+{_AMOUNT}\s+{_POINTS}\s+of\s+(?P<dtype>{_DTYPE}){_MARK}{_TAIL}"),
    # ---- CASTING --------------------------------------------------------
    ("cast", rf"^(?P<actor>{NAME})\s+begins?\s+casting\s+(?P<skill>.+?){_TAIL}"),
    # ---- ENVIRONMENT: "YOU take 3 damage from falling!" (no attacker; only damage taken) ------
    ("env_damage", rf"^(?P<target>YOU|You|{NAME})\s+takes?\s+{_AMOUNT}\s+(?:points?\s+of\s+)?damage\s+from\s+(?P<skill>[a-z]+(?:\s+[a-z]+){{0,2}}){_TAIL}"),
    # an area stun with no target: "Fipuduzuleg smashes the ground around them with great force."
    ("cast", rf"^(?P<actor>{NAME})\s+(?P<skill>smash)es\s+the\s+[gz]round\b.*$"),
    ("interrupt", rf"^{_POSSESSIVE}\s+(?:casting|spell)\s+(?:is|was|has\s+been)\s+interrupted{_TAIL}"),
    # ---- STATE / STATUS (no numbers) ------------------------------------
    ("status", rf"^(?:Starting\s+to\s+attack|Stopped\s+attacking){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+(?P<skill>dispel)s?\s+magic\s+from\s+(?P<target>you|{NAME})(?:\s*[.!]?\s*\((?P<outcome>[^()]+)\))?{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+fires?\s+an\s+(?P<skill>exposing)\s+shot\s+at\s+(?P<target>YOU|you|{NAME}){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+feels?\s+healthy\s+again[.!]?\s*\((?P<skill>[^()]+)\){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+places?\s+some\s+more\s+wood\s+on\s+the\s+campfire[.!]?\s+It\s+burns\s+brighter\s+than\s+before{_TAIL}"),
    # "YOU are temporarily IMMUNE to Tozuvek's Snaring Shot!": the attempt landed nothing (outcome
    # "immune", target = the caster; no skill, so it is never counted as an attempt itself)
    ("status", rf"^(?P<actor>{NAME})\s+(?:is|are)\s+(?:temporarily\s+)?(?P<outcome>IMMUNE)\s+to\s+(?P<target>{NAME})(?:'s|s)\s+\S.*?{_TAIL}"),
    # an archer's ability on a player names the archer: "Tozuvek snares YOU!", "Pemiruk fires a net
    # shot at YOU!", "Pemiruk interrupts YOU!" (attempts; the parser names the ability)
    ("status", rf"^(?P<actor>{NAME})\s+(?P<skill>snares|interrupts)\s+(?P<target>YOU|you|{NAME}){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+fires\s+a\s+(?P<skill>net)\s+shot\s+at\s+(?P<target>YOU|you|{NAME}){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+(?:is\s+|are\s+)?no\s+longer\s+(?P<outcome>[a-z][a-z ]*?){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+(?P<skill>charges?)\s+at\s+(?P<target>{NAME}){_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+loses\s+interest\s+in\s+(?P<target>{NAME}){_TAIL}"),
    ("status", rf"^{_POSSESSIVE}\s+(?:armor|armour|shield|weapon)\s+breaks{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+becomes?\s+spiritually\s+connected\s+to\s+the\s+divine{_TAIL}"),
    ("status", rf"^{_POSSESSIVE}\s+spiritual\s+connection\s+is\s+severed{_TAIL}"),
    ("status", rf"^{_POSSESSIVE}\s+fervor\s+subsides{_TAIL}"),
    ("status", rf"^{_POSSESSIVE}\s+devotion\s+is\s+rewarded{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+lets?\s+loose\s+an?\s+empowering\s+battle\s+cry{_TAIL}"),
    ("status", rf"^The\s+warmth\s+of\s+the\s+campfire\s+leaves\s+you{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+appears?{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+begins?\s+to\s+sneak{_TAIL}"),
    ("status", rf"^(?P<actor>{NAME})\s+ambushes?\s+(?:their|his|her|its|your)\s+victim{_TAIL}"),
    # Observed live 2026-10-01 (rogue stealth): "Pidef slips into the shadows and disappears."
    ("status", rf"^(?P<actor>{NAME})\s+slips?\s+into\s+the\s+shadows(?:\s+and\s+disappears?)?{_TAIL}"),
    # Generic "<Actor> <verbs> <Target>." (e.g. "Pidef jabs a stumbling zombie.").
    # Requires real sentence punctuation so wrapped fragments do not match.  Never "casting":
    # "a skeletal cleric bezms casting Holy Fortitude." is a garbled cast, not an action on a
    # "Holy Fortitude" by "a skeletal cleric bezms".
    ("status", rf"^(?P<actor>{NAME})\s+(?P<skill>{_NOT_FUNCTION_WORD}(?!casting\b)[a-z]+)\s+(?P<target>{NAME}){_SENTENCE_END}"),
    # ---- STATUS catch-all (SPEC 3b): a sentence without numbers is a status line ---
    # Player / pronoun subject: at least one more word, real sentence punctuation, no
    # digits; capitalised damage-type words that start a wrapped tail are excluded.
    ("status", rf"^(?!{_DTYPE_WORDS}\b)(?P<actor>{PLAYER}(?:'s)?)(?:\s+[A-Za-z'\-]+)+{_SENTENCE_END}"),
    # NPC subject: the longest article phrase that is followed either by a possessive
    # ("a skeletal warrior's magical resistance frays.") or by a verb-like word
    # ("a skeletal warrior looks mentally exhausted.", "a zombie laborer kicks Cigezisi.").
    ("status", rf"^(?P<actor>{_NPC_GREEDY})(?:'s\s+[a-z][a-z'\-]*|\s+(?:is|are|has|have|was|were|no|[a-z]+s)\b)[^\d]*{_SENTENCE_END}"),
    ("status", rf"^The\s+[A-Za-z'\-]+(?:\s+[A-Za-z'\-]+)+{_SENTENCE_END}"),
]

RULES: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(pattern)) for kind, pattern in _RULE_SOURCES
]
"""Ordered ``(kind, compiled regex)`` pairs.  First match wins.

Named groups used (where applicable): ``actor``, ``target``, ``amount``,
``skill``, ``dtype``, ``outcome``.
"""

# --------------------------------------------------------------------------
# Verb lemmas
# --------------------------------------------------------------------------

VERB_LEMMAS: dict[str, str] = {
    "crushes": "crush",
    "pierces": "pierce",
    "slashes": "slash",
    "bites": "bite",
    "bashes": "bash",
    "kicks": "kick",
    "jabs": "jab",
    "hits": "hit",
    "punches": "punch",
    "claws": "claw",
    "stings": "sting",
    "gores": "gore",
    "mauls": "maul",
    "smashes": "smash",
    "slices": "slice",
    "stabs": "stab",
    "strikes": "strike",
    "dodges": "dodge",
    "parries": "parry",
    "blocks": "block",
    "ripostes": "riposte",
    "misses": "miss",
    "resists": "resist",
    "tries": "try",
}
"""Third-person verb -> lemma for the verbs seen so far."""


def lemmatize(verb: str) -> str:
    """Return the base form of a present-tense verb.

    Known verbs come from :data:`VERB_LEMMAS`; anything else uses a small
    generic rule: ``-ies`` -> ``-y``, ``-es`` after ``s``/``sh``/``ch``/``x``/``z``
    is stripped, otherwise a trailing ``s`` is stripped (but never ``ss``).
    Second-person forms (``crush``) are returned unchanged.
    """
    v = verb.strip().lower()
    if not v:
        return v
    if v in VERB_LEMMAS:
        return VERB_LEMMAS[v]
    if v.endswith("ies") and len(v) > 4:
        return v[:-3] + "y"
    if v.endswith("es") and v[:-2].endswith(("s", "sh", "ch", "x", "z")):
        return v[:-2]
    if v.endswith("s") and not v.endswith("ss"):
        return v[:-1]
    return v


_NPC_RX = re.compile(r"^(?:a|an|the)\s+[A-Za-z]")


#: Text that can only be the start of a new message, never the rest of a wrapped one:
#: "You ...", "Your ...", "Name's Ability ...", "Name verbs ...", "a mob verbs ...", the
#: framed loot line, "Starting/Stopped attacking".  Used to keep two messages apart that
#: the OCR (or a cut-off line end) ran together.
MESSAGE_START_RX = re.compile(
    r"^(?:"
    r"(?:You|Your)\b"
    r"|--"
    r"|(?:Starting|Stopped)\s+(?:to\s+)?attack"
    r"|[A-Z][a-z]{2,}'s\s+[A-Z]"
    r"|[A-Z][a-z]{2,}\s+(?:hits|slashes|crushes|pierces|punches|kicks|bashes|bites|claws|"
    r"begins|tries|has|have|loots|crafts|heals|is|looks|staggers|casts)\b"
    r"|(?:a|an|the)\s+(?:[A-Za-z][a-z\-]*\s+){1,4}?(?:hits|slashes|crushes|pierces|punches|kicks|"
    r"bashes|bites|claws|mauls|stings|begins|tries|has|is|looks|loses|staggers|resists|awakens)\b"
    r"|(?:a|an|the)\s+(?:[A-Za-z][a-z\-]*\s+){0,3}?[A-Za-z][a-z\-]*'s\s+[A-Z]"
    r")"
)


def starts_message(text: str) -> bool:
    """True when ``text`` begins like a complete message (see :data:`MESSAGE_START_RX`)."""
    return MESSAGE_START_RX.match(text.strip()) is not None


def is_npc_name(name: str | None) -> bool:
    """True when ``name`` has the NPC shape (``a stumbling zombie``)."""
    return bool(name) and _NPC_RX.match(name) is not None


def is_you(token: str | None) -> bool:
    """True when ``token`` is one of the local-player pronoun forms."""
    return token is not None and token in YOU_TOKENS
