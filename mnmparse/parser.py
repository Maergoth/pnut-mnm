"""Turn one combat-window message into an :class:`Event` (SPEC section 6).

The parser is stateless.  :func:`parse_line` first repairs the OCR noise
listed in SPEC section 3 (:func:`normalize_ocr`), then tries
:data:`mnmparse.grammar.RULES` in order and builds an :class:`Event` from
the first match.  Anything that matches no rule becomes ``kind="unknown"``;
the function never raises.

The returned ``Event.text`` is the message exactly as it was given (the
raw combat log already holds that text, and keeping it verbatim makes the
events file traceable back to the log line).
"""

from __future__ import annotations

import logging
import re

from .grammar import CC_OUTCOMES, COIN_IN_COPPER, DEBUFF_OUTCOMES, RULES, Event, is_you, lemmatize, starts_message

#: "... and you receive 2 copper coins ... as your split": the number and the word after it (a
#: denomination, or the OCR's remains of one: "2 coppe", "1 cc", "3 COI", "O coins", "4 cavalier's")
_SPLIT_RX = re.compile(r"\brece\w*\s+(\d+|O)\b(?:\s+([A-Za-z']+))?", re.IGNORECASE)
_COIN_PART_RX = re.compile(r"([\dIlO|]+)\s+(platinum|gold|silver|copper)\b")

__all__ = ["Event", "NameCompleter", "parse_line", "normalize_ocr", "map_player", "miss_outcome"]

log = logging.getLogger(__name__)

# Kinds whose ``skill`` group is a plain verb that should be lemmatized.
_VERB_SKILL_KINDS: frozenset[str] = frozenset({"melee_hit", "melee_miss", "status"})

# --------------------------------------------------------------------------
# OCR noise normalisation
# --------------------------------------------------------------------------

_QUOTE_MAP = str.maketrans(
    {
        "’": "'",  # right single quote
        "‘": "'",  # left single quote
        "‛": "'",
        "`": "'",  # grave accent
        "´": "'",  # acute accent
        "“": '"',
        "”": '"',
    }
)
_DIGIT_MAP = str.maketrans({"I": "1", "l": "1", "|": "1", "O": "0", "o": "0"})

_LOG_TIMESTAMP_RX = re.compile(r"^\[[^\]]{0,40}\]\s*")
#: Anything before the first letter, except the number that starts a wrapped coin split
#: ("22 copper coins from X's corpse as your split.").
_LEADING_JUNK_RX = re.compile(r"^(?!\d+\s+(?:platinum|gold|silver|copper|coins?)\b)[^A-Za-z(]+")
_STRAY_TAIL_RX = re.compile(r"[^\x20-\x7e]+\s*$")
_PAREN_ZERO_RX = re.compile(r"(?<=\d)\(\)")  # "2()" -> "20" (a 0 glyph read as parentheses)
_SPACED_POSSESSIVE_RX = re.compile(r"\b([A-Z][a-z]+) s (?=[A-Z])")  # "Tovozen s Heal" -> "Tovozen's Heal"
_PET_RX = re.compile(r"^Your\s+pet\s+([A-Z][A-Za-z]*)\b")

#: Whole-word OCR misreads seen in the recordings (SPEC 3c-3f).  Applied before any
#: digit/letter spacing so "D0ints" is repaired as one token.
_WORD_FIXES: dict[str, str] = {
    "trv": "try", "vou": "you", "Vou": "You", "vour": "your", "Vour": "Your", "awav": "away",
    "crv": "cry", "thev": "they", "angrv": "angry", "Holv": "Holy", "polnts": "points",
    "potnts": "points", "poilifs": "points", "D0ints": "points", "D0int": "point",
    "Domts": "points", "Dierces": "pierces", "Dierce": "pierce", "Weaoon": "Weapon",
    "emoowering": "empowering", "emoowerine": "empowering", "emoowerinz": "empowering",
    "iabs": "jabs", "stain": "slain", "ofdamage": "of damage", "uppercutsa": "uppercuts a",
    "crushesa": "crushes a", "stumblihg": "stumbling", "skeletOn": "skeleton", "iS": "is",
    "IS": "is", "St0DDed": "Stopped", "abilitv": "ability", "poins": "points", "fr": "for",
    "aooears": "appears", "boint": "point", "boints": "points",
    "DOint": "point", "DOints": "points", "Daladin": "paladin", "mazical": "magical",
    "castin": "casting", "Holv": "Holy", "Damas": "Damage", "damee": "damage", "dtmage": "damage",
    "coppercoins": "copper coins", "gotbetter": "got better", "gotworse": "got worse",
    "interruoted": "interrupted", "experiencer": "experience!",
    "bezins": "begins", "castinz": "casting", "gasting": "casting", "rizht": "right",
    "damaze": "damage", "damaqe": "damage", "Damaze": "Damage", "tarting": "Starting",
    "topped": "Stopped", "oading": "Loading", "nothinz": "nothing", "N'lagic": "Magic",
    "N'lazic": "Magic", "Mazic": "Magic",
    # death / kill lines (2026-10-02 audit): "slain bv", "Your Dartv member", "You h4ve slain"
    "bv": "by", "Dartv": "party", "oarty": "party", "partv": "party", "membev": "member",
    "h4ve": "have", "heve": "have", "iackal": "jackal", "iackals": "jackals",
}
_FUSED_CASTING_RX = re.compile(r"\bbegi\w{0,2}s?\s*[cgs]ast\w{2,4}\b")  # "beginsgasting", "begiwcastifig"
# A missing space after the actor, constrained to recognisable cast/loot messages.
_FUSED_ACTOR_RX = re.compile(r"^([A-Z][A-Za-z]*?)(begins(?=\s+casting\b)|loots(?=\s+[\[(]))")
_DIGIT_IN_WORD_RX = re.compile(r"(?<=[A-Za-z])[0159](?=[a-z])")  # "Dogabetaro1em" -> "Dogabetarolem"
_DIGIT_IN_WORD_MAP = {"0": "o", "1": "l", "5": "s", "9": "g"}
_MID_OF_RX = re.compile(r"(?<=\s)Of(?=\s)")  # "points Of damage" -> "points of damage"
#: "(Block I)" -> "(Block 1)", "(Block 1 1)" -> "(Block 11)"
_MARKER_DIGITS_RX = re.compile(r"\((Block\s+|)([\dIl|Oo]+(?:\s[\dIl|Oo]+)*)(\s+absorbed|)\)")


def _fix_marker_digits(m: re.Match[str]) -> str:
    digits = m.group(2).replace(" ", "").translate(_DIGIT_MAP)
    return f"({m.group(1)}{digits}{m.group(3)})" if digits.isdigit() else m.group(0)
_WORD_FIX_RX = re.compile(
    r"(?<![A-Za-z])(" + "|".join(map(re.escape, sorted(_WORD_FIXES, key=len, reverse=True))) + r")(?![A-Za-z])"
)
_WS_RX = re.compile(r"\s+")
_HYPHEN_AS_SPACE_RX = re.compile(r"(?<=[A-Za-z])-(?=[A-Za-z])")
_MINUS_BEFORE_NUMBER_RX = re.compile(r"(?<![A-Za-z0-9])-(?=\d)")
_LETTER_DIGIT_RX = re.compile(r"(?<=[a-z])(?=\d)")
_DIGIT_LETTER_RX = re.compile(r"(?<=\d)(?=[A-Za-z])")
_POINTS_TYPO_RX = re.compile(r"\bpo(?:ilifs|lnts|irits|ints|nts|infs)\b")
#: "for 10 POI ts of", "for 17 POHIts of", "for 1 POInt of": "points" read in capitals
_POINTS_CAPS_RX = re.compile(r"\b(for\s+\d+\s+)P[O0][A-Za-z|]{0,3}?\s?[tT][sS]?\b")
#: "points ofHoly Damage", "points'of damage", "points f Holy Damage" -> "points of ..."
_POINTS_OF_RX = re.compile(r"\b(points?)\W{0,3}?(?:of|f)(?:\b|(?=[A-Z]))\s*(?=\S)")
#: "loots 1 1 copper coins" -> "loots 11 copper coins"
_COIN_AMOUNT_RX = re.compile(
    r"\b(loots?\s+)([\dIl|O]+(?:\s[\dIl|O]+)+)(?=\s+(?:platinum|gold|silver|copper)\b)"
)
_DAMAGE_TYPO_RX = re.compile(r"(?<![A-Za-z])(?:dama ?g?e|Carnage|clanma#|Aamage|Tamage)(?![A-Za-z])")
_AMOUNT_CTX_RX = re.compile(r"\bfor ([-\dIl|Oo ]+?) (points?|Health|Mana|Stamina)\b")
#: Junk glued to a readable number: "for' 14 points", "for '2 Health", "for 6? points".
_AMOUNT_JUNK_RX = re.compile(r"\bfor\s*[^\w\s]*\s*(\d+)\s*[^\w\s]*\s*(points?|Health|Mana|Stamina)\b")
#: A hit or heal amount that is not a number at all: "for $ points", "for points", "for ea t Health".
_GARBLED_AMOUNT_RX = re.compile(r"\bfor\b(?P<amt>[^.!]{0,12}?)\s*\b(?P<unit>points?|Health)\b")
#: "Povebizus Slice hits" -> "Povebizu's Slice hits": only the first word of the line is the
#: possessor (also with a capital inside it: "GoZifs Slice hits"), and an adjective such as
#: "Righteous" in a clipped "Your Righteous Smite hits" is never one.
_DROPPED_APOSTROPHE_RX = re.compile(
    r"^([A-Z][A-Za-z]+?)(?<!ou)s (?=[A-Z][A-Za-z']*(?: (?:[a-z]+ )?[A-Z][A-Za-z']*)* (?:hits|heals)\b)"
)
#: The NPC form: "a dunes madmans Strike hits YOU" -> "a dunes madman's Strike hits YOU".
_DROPPED_NPC_APOSTROPHE_RX = re.compile(
    r"^((?:a|an|the) [a-z]+(?: [a-z]+)*?)s (?=[A-Z][A-Za-z']*(?: [A-Z]\w*)* (?:hits|heals)\b)"
)
#: The s lost instead: "ovozen' Heal heals Wululiso" -> "ovozen's Heal heals Wululiso"
_BARE_APOSTROPHE_RX = re.compile(r"^([A-Za-z][a-z]{2,})' (?=[A-Z][a-z])")
_YOU_BANG_END_RX = re.compile(r"\bYOU[lI1|]+[.!]?\s*$")
_YOU_BANG_MID_RX = re.compile(r"\bYOU[lI1|]+(?=[\s,.])")
#: The line's final "!" read as l / I / 1 / | after a word that ends a message ("but missesl",
#: "ambushes their victiml", "has increasedl (47)").  Only whole known words, so a line cut off in
#: the middle of a word ("for 3 points of Hol") is left alone.
_BANG_WORD_RX = re.compile(
    r"\b(miss|misses|parry|parries|dodge|dodges|block|blocks|riposte|ripostes|victim|increased|up|"
    r"damage|Damage|Shot)[lI1|](?=\s*(?:\(\d+\))?\s*$)"
)
#: No word ends in a lower-case letter and then I / 1 / |: that is the final "!" ("a crocodileI").
#: (A final l is left to :class:`NameCompleter`, which knows the names: "a jackal" is real.)
_BANG_AFTER_LOWER_RX = re.compile(r"(?<=[a-z])[I1|](?=\s*$)")
_YOU_TYPO_RX = re.compile(r"^(?:Vou|Nou|Hou|Yau|Qou|you|ou)\b")
_YOUR_TYPO_RX = re.compile(r"^(?:Vour|Nour|Hour|your|our)\b")


def _fix_amount(m: re.Match[str]) -> str:
    """Join split digits and map I/l/|/O look-alikes inside ``for <n> point``."""
    raw = m.group(1).replace(" ", "").replace("-", "")
    digits = raw.translate(_DIGIT_MAP)
    if digits.isdigit():
        return f"for {digits} {m.group(2)}"
    return m.group(0)


def _fix_coin_amount(m: re.Match[str]) -> str:
    digits = m.group(2).replace(" ", "").translate(_DIGIT_MAP)
    return m.group(1) + digits if digits.isdigit() else m.group(0)


def normalize_ocr(text: str) -> str:
    """Repair the OCR noise listed in SPEC section 3 before matching.

    Steps, in order:

    * typographic quotes -> ASCII, whitespace collapsed, an EQ-style
      ``[timestamp]`` prefix and any leading non-letter junk removed;
    * a hyphen between two letters is a mis-read space (``you-try``,
      ``Strike-hits``); a ``-`` directly before a number is dropped
      (``for -1 point``);
    * missing spaces at letter/digit boundaries are restored
      (``for2points``, ``56Health``);
    * ``poilifs``/``polnts`` -> ``points``; ``dama e``/``Carnage``/``clanma#``
      -> ``damage``;
    * between ``for`` and ``point``/``Health``: ``1 1``, ``I I``, ``II``
      -> ``11``, lone ``I``/``l`` -> ``1``;
    * ``Tovozens Heal heals`` -> ``Tovozen's Heal heals`` when the next
      words look like an ability followed by ``hits``/``heals`` (also
      ``a dunes madmans Strike hits``);
    * ``Vou``/``you`` at the start of the line -> ``You`` (and ``your`` ->
      ``Your``);
    * a final ``!`` read as ``l``/``I``/``1``/``|`` after ``YOU`` or a word
      that ends a message (``but missesl``) is put back;
    * ``points ofHoly``, ``points'of``, ``points f``, ``POI ts`` -> ``points of``,
      ``(Block 1 1)`` -> ``(Block 11)``, ``loots 1 1 copper`` -> ``loots 11 copper``.
    """
    s = text.translate(_QUOTE_MAP)
    s = _WS_RX.sub(" ", s).strip()
    s = _LOG_TIMESTAMP_RX.sub("", s)
    s = _LEADING_JUNK_RX.sub("", s)
    s = _STRAY_TAIL_RX.sub("", s).rstrip()
    s = s.replace("�", "")  # a dropped glyph in the middle of a word ("poin�s")
    s = _WS_RX.sub(" ", s).strip()
    s = _WORD_FIX_RX.sub(lambda m: _WORD_FIXES[m.group(1)], s)
    # "kicks YOUI" / "hits YOUl for 5": the "!" (or nothing) after YOU read as l / I / 1 / |
    s = _YOU_BANG_END_RX.sub("YOU!", s)
    s = _YOU_BANG_MID_RX.sub("YOU", s)
    s = _BANG_WORD_RX.sub(r"\1!", s)
    s = _BANG_AFTER_LOWER_RX.sub("!", s)
    s = _FUSED_CASTING_RX.sub("begins casting", s)
    s = _FUSED_ACTOR_RX.sub(r"\1 \2", s)
    s = _MID_OF_RX.sub("of", s)
    s = _MARKER_DIGITS_RX.sub(_fix_marker_digits, s)
    s = _DIGIT_IN_WORD_RX.sub(lambda m: _DIGIT_IN_WORD_MAP[m.group(0)], s)
    s = _PAREN_ZERO_RX.sub("0", s)
    s = _SPACED_POSSESSIVE_RX.sub(r"\1's ", s)
    s = _HYPHEN_AS_SPACE_RX.sub(" ", s)
    s = _MINUS_BEFORE_NUMBER_RX.sub("", s)
    s = _LETTER_DIGIT_RX.sub(" ", s)
    s = _DIGIT_LETTER_RX.sub(" ", s)
    s = _WS_RX.sub(" ", s).strip()
    s = _POINTS_CAPS_RX.sub(r"\1points", s)
    s = _POINTS_TYPO_RX.sub("points", s)
    s = _DAMAGE_TYPO_RX.sub("damage", s)
    s = _POINTS_OF_RX.sub(r"\1 of ", s)
    s = _AMOUNT_JUNK_RX.sub(r"for \1 \2", s)
    s = _AMOUNT_CTX_RX.sub(_fix_amount, s)
    s = _COIN_AMOUNT_RX.sub(_fix_coin_amount, s)
    s = _DROPPED_APOSTROPHE_RX.sub(r"\1's ", s)
    s = _DROPPED_NPC_APOSTROPHE_RX.sub(r"\1's ", s)
    s = _BARE_APOSTROPHE_RX.sub(r"\1's ", s)
    s = _YOU_TYPO_RX.sub("You", s)
    s = _YOUR_TYPO_RX.sub("Your", s)
    return s


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _clean(value: str | None) -> str | None:
    """Collapse whitespace in a captured group; ``None`` stays ``None``."""
    if value is None:
        return None
    value = _WS_RX.sub(" ", value).strip()
    return value or None


def map_player(name: str | None, player_name: str = "") -> str | None:
    """Map the pronoun forms ``You``/``Your``/``YOU``/``yourself`` to the player.

    Returns ``player_name`` when given, else the canonical ``"You"``.  Any
    other name is returned unchanged.
    """
    if name is None:
        return None
    if is_you(name):
        return player_name or "You"
    return name


def miss_outcome(outcome_text: str | None, target: str | None) -> str:
    """Reduce the ``but ...!`` clause of a melee miss to one lemma.

    ``"miss"``/``"misses"`` -> ``"miss"``; ``"Wululiso dodges"`` ->
    ``"dodge"``; ``"a stumbling zombie parries"`` -> ``"parry"``;
    ``"you dodge"`` -> ``"dodge"``; ``"Wululiso blocks with their shield"``
    -> ``"block"``.
    """
    text = (outcome_text or "").strip().rstrip("!.,;: ")
    if not text:
        return "miss"
    if re.fullmatch(r"miss(?:es)?", text, flags=re.IGNORECASE):
        return "miss"
    if re.search(r"\babsorbs?\b", text, flags=re.IGNORECASE):
        return "absorb"
    rest = text
    if target and rest.lower().startswith(target.lower()):
        rest = rest[len(target):].strip()
    elif re.match(r"(?i)you\b", rest):
        rest = rest[3:].strip()
    words = rest.split()
    if not words:
        # The clause was just the name (OCR dropped the verb); best effort.
        return lemmatize(text.split()[-1])
    return lemmatize(words[0])


#: Skill names a rule captures as a bare verb but that have a proper name.  "Tozuvek snares YOU!",
#: "Pemiruk fires a net shot at YOU!" and "Pemiruk interrupts YOU!" are how the game shows an
#: archer's Snaring Shot, Net Shot and Interrupting Shot landing on a player.
_SKILL_NAMES = {
    "smash": "Ground Smash", "snare": "Snaring Shot", "net": "Net Shot", "interrupt": "Interrupting Shot",
}

#: Highest level a "They are now level N" line can name (a misread larger number is a "!" read
#: as a digit: "level 71" is level 7).
MAX_LEVEL = 60


def _denomination(token: str | None) -> str | None:
    """The coin denomination ``token`` starts to spell ("c", "coppe" -> copper), else ``None``."""
    word = (token or "").lower()
    if not word:
        return None
    return next((name for name in COIN_IN_COPPER if name.startswith(word)), None)


def _split_copper(split_text: str, looted: str, total: int) -> int | None:
    """The viewer's share in "... and you receive <n> <denomination> ... as your split".

    A clipped or misread denomination ("2 coppe", "1 cc", "3 COI", "O coins", "4 cavalier's
    corpse") is the looted one; ``None`` when no number can be read or the share would be larger
    than the loot (a misread)."""
    m = _SPLIT_RX.search(split_text)
    if m is None:
        return None
    count = 0 if m.group(1) in ("O", "o") else int(m.group(1))
    value = count * COIN_IN_COPPER[_denomination(m.group(2)) or looted]
    return value if value <= total else None


def _price_copper(text: str | None) -> int | None:
    """``"1 silver, and 20 copper"`` -> 120."""
    total, seen = 0, False
    for m in _COIN_PART_RX.finditer(text or ""):
        digits = m.group(1).translate(_DIGIT_MAP)
        if digits.isdigit():
            total += int(digits) * COIN_IN_COPPER[m.group(2).lower()]
            seen = True
    return total if seen else None


def _level(text: str | None) -> int | None:
    """The level in "They are now level 7!", allowing for the "!" read as a digit ("level 71",
    "level 1 II") or dropped; ``None`` when it is not a plausible level."""
    raw = (text or "").replace(" ", "")
    bang = raw.endswith("!")
    raw = raw.rstrip("!")
    candidates = [raw]
    if not bang and len(raw) > 1 and raw[-1] in "1lI|":
        candidates.insert(0, raw[:-1])  # the "!" read as a 1 is far likelier than a dropped "!"
    for cand in candidates:
        digits = cand.translate(_DIGIT_MAP)
        if digits.isdigit() and 2 <= int(digits) <= MAX_LEVEL:
            return int(digits)
    return None


def _build(kind: str, m: re.Match[str], text: str, ts: float, player_name: str) -> Event:
    """Create an :class:`Event` from a rule match."""
    g = m.groupdict()
    raw_actor = _clean(g.get("actor"))
    if raw_actor and raw_actor.endswith("'s"):
        raw_actor = raw_actor[:-2]
    raw_target = _clean(g.get("target"))
    if kind == "cannot_attack" and raw_actor is None:
        raw_actor = "You"
    actor = map_player(raw_actor, player_name)
    if raw_target and raw_target.lower() == "them":
        target = actor  # "X's Life Draw heals them for 12 Health." is a self-heal
    else:
        target = map_player(raw_target, player_name)

    skill = _clean(g.get("skill"))
    if skill and kind in _VERB_SKILL_KINDS and skill.islower() and " " not in skill:
        skill = lemmatize(skill)

    dtype = _clean(g.get("dtype"))

    def _int(name: str) -> int | None:
        value = (g.get(name) or "").translate(_DIGIT_MAP)  # "O coins ... as your split" is 0
        return int(value) if value.isdigit() else None

    amount = _int("amount")
    absorbed = _int("absorbed")
    blocked = _int("blocked")
    weapon = _clean(g.get("weapon"))
    crit = bool(g.get("crit"))
    item = _clean(g.get("item"))
    copper: int | None = None
    split_copper: int | None = None
    if kind == "coin":
        looted = (dtype or "copper").lower()
        copper = (amount or 0) * COIN_IN_COPPER.get(looted, 1)
        split_copper = _split_copper(g.get("split") or "", looted, copper)
        amount = copper
    elif kind == "coin_split":
        # no denomination printed ("O coins ..."): copper, the only one loot has shown so far
        split_copper = (amount or 0) * COIN_IN_COPPER.get((dtype or "copper").lower(), 1)
        amount = split_copper
    elif kind == "vendor":
        copper = _price_copper(g.get("price"))
        amount = amount or 1
    elif kind == "level_up":
        amount = _level(g.get("level"))

    outcome = _clean(g.get("outcome"))
    if kind in ("melee_hit", "ability_hit"):
        outcome = "critical" if crit else "hit"
    elif kind == "melee_miss":
        outcome = miss_outcome(outcome, raw_target)
    elif kind == "cannot_attack" and outcome:
        outcome = outcome.lower().rstrip(" .!?,")
    elif kind == "marker":
        if blocked is not None:
            outcome, amount = "block", blocked
        elif absorbed is not None:
            outcome, amount = "absorb", absorbed
        else:
            outcome = "critical"
    elif kind in ("resist", "fizzle"):
        outcome = kind
    elif kind == "ability_miss":
        outcome = "miss"
    elif kind in ("cc", "cc_fade"):
        # "<victim> is stunned by scintillating lights." -> category "stun", source in skill
        if kind == "cc_fade" and outcome is None:
            outcome = "root"  # "X breaks free from the clinging roots."
        outcome = CC_OUTCOMES.get((outcome or "").lower(), outcome)
    elif kind == "debuff":
        outcome = DEBUFF_OUTCOMES.get((outcome or "").lower(), outcome)
    elif kind in ("personal", "status") and outcome:
        outcome = outcome.lower()  # "YOU are temporarily IMMUNE to ..." -> "immune"

    return Event(
        ts=ts,
        kind=kind,
        text=text,
        actor=actor,
        target=target,
        amount=amount,
        skill=skill,
        dtype=dtype,
        outcome=outcome,
        raw_actor=raw_actor,
        absorbed=absorbed,
        blocked=blocked,
        weapon=weapon,
        item=item,
        copper=copper,
        split_copper=split_copper,
    )


# --------------------------------------------------------------------------
# Clipped-name completion
# --------------------------------------------------------------------------

_LEAD_WORD_RX = re.compile(r"^([A-Za-z][a-z\-]*)(\s+|'s\b|'(?=\s))")
_ARTICLES = frozenset({"a", "an", "the"})
_BANG_GLYPHS = "lI1|"
#: The ability named by "YOU are temporarily IMMUNE to Tozuvek's Snaring Shot!"
_IMMUNE_ABILITY_RX = re.compile(r"\bIMMUNE\s+to\s+.+?(?:'s|s)\s+(?P<ability>.+?)[\s.!?,:;'\"\-]*$")
#: An attempt this soon after the immunity line is the one the immunity stopped.
IMMUNE_ATTEMPT_S = 2.0
_SKILL_SOURCE_KINDS = frozenset({"ability_hit", "heal", "cast", "resist"})


class NameCompleter:
    """Repairs lines with what the lines before them taught: names and abilities seen so far.

    Feed every parsed event to :meth:`observe` (which first fixes it, see :meth:`repair`); call
    :meth:`complete` on a line the parser could not classify.  A leading lowercase token that
    is the tail of a known player name (at most two characters lost: ``bepulifif pierces
    ...``) is replaced by that name; a leading phrase that equals a known NPC name minus its
    article (``skeletal defender hits ...``) gets the article back.
    """

    MAX_LOST = 2

    def __init__(self) -> None:
        self._players: dict[str, int] = {}
        self._npcs: dict[str, int] = {}
        self._skills: dict[str, int] = {}
        self._immune: tuple[float, str | None, str] | None = None  #: (ts, caster, ability) of the last immunity

    def observe(self, ev: Event) -> None:
        """Repair ``ev`` in place (see :meth:`repair`), then learn its names and ability."""
        self.repair(ev)
        if ev.kind in ("unknown", "personal", "consider", "zone", "chat"):
            return
        for raw in (ev.raw_actor, ev.target):
            if not raw or is_you(raw):
                continue
            if _NPC_START_RX.match(raw):
                self._npcs[raw] = self._npcs.get(raw, 0) + 1
            elif raw[:1].isupper() and raw.replace(" ", "").isalpha():
                self._players[raw] = self._players.get(raw, 0) + 1
        if ev.kind in _SKILL_SOURCE_KINDS and ev.skill:
            self._skills[ev.skill] = self._skills.get(ev.skill, 0) + 1

    def repair(self, ev: Event) -> Event:
        """Fix ``ev`` in place with what earlier lines taught, and return it.

        * The line's final "!" read as l / I / 1 / | and glued to the name or ability it ends
          with ("You have slain a crocodilel", "a crocodile resists your Blindl") is dropped
          when the name without it was seen before.
        * A melee hit whose actor ends in a known ability lost the "'s" in between ("Gozif Holy
          Strike hits X", "a dunes madman Strike hits YOU") and becomes that ability's hit.  A
          named NPC ("Toilmaster Verith hits YOU") ends in no ability and is left alone.
        * The crowd-control attempt right after "YOU are temporarily IMMUNE to <caster>'s
          <ability>" landed nothing and is no attempt: its skill is cleared, outcome "immune".
        """
        if ev.kind == "unknown":
            return ev
        last = (ev.text or "").rstrip()[-1:]
        if last and last in _BANG_GLYPHS:
            self._drop_bang_glyph(ev)
        if ev.kind == "melee_hit" and " " in (ev.raw_actor or ""):
            self._split_ability(ev)
        if ev.kind == "status":
            self._skip_immune_attempt(ev)
        return ev

    def _known(self, name: str, field: str) -> bool:
        if field == "skill":
            return name in self._skills
        return name in self._players or name in self._npcs

    def _drop_bang_glyph(self, ev: Event) -> None:
        clean = normalize_ocr(ev.text)
        for field in ("target", "actor", "skill"):
            value = getattr(ev, field)
            if not value or value[-1:] not in _BANG_GLYPHS or not clean.endswith(value):
                continue
            stem = value[:-1].rstrip()
            if stem and self._known(stem, field):
                setattr(ev, field, stem)
                if field == "actor":
                    ev.raw_actor = stem
            return

    def _split_ability(self, ev: Event) -> None:
        raw = ev.raw_actor or ""
        words = raw.split()
        cut = next(  # the longest known ability first
            (i for i in range(1, len(words)) if words[i][:1].isupper() and " ".join(words[i:]) in self._skills),
            None,
        )
        if cut is None:
            return
        head = " ".join(words[:cut])
        clean = normalize_ocr(ev.text)
        if not clean.startswith(raw + " "):
            return
        fixed = parse_line(f"{head}'s {clean[len(head) + 1:]}", ev.ts)
        if fixed.kind == "ability_hit" and fixed.actor == head:
            ev.kind, ev.actor, ev.raw_actor = fixed.kind, fixed.actor, fixed.raw_actor
            ev.skill, ev.dtype, ev.outcome = fixed.skill, fixed.dtype, fixed.outcome

    def _skip_immune_attempt(self, ev: Event) -> None:
        if ev.outcome == "immune":
            m = _IMMUNE_ABILITY_RX.search(normalize_ocr(ev.text))
            if m is not None:
                self._immune = (float(ev.ts), ev.target, m.group("ability").lower())
            return
        last = self._immune
        if (
            last is not None and ev.skill and ev.actor == last[1]
            and ev.skill.lower() == last[2] and 0.0 <= float(ev.ts) - last[0] <= IMMUNE_ATTEMPT_S
        ):
            ev.skill, ev.outcome = None, "immune"
            self._immune = None

    def complete(self, text: str) -> str | None:
        """Return the repaired line, or ``None`` when no confident completion exists."""
        m = _LEAD_WORD_RX.match(text)
        if m is None:
            return None
        word = m.group(1)
        if word[:1].isupper() or word.lower() in _ARTICLES:
            return None
        lowered = word.lower()
        best: str | None = None
        best_count = 1  # require a name seen at least twice
        for name, count in self._players.items():
            low = name.lower()
            lost = len(low) - len(lowered)
            if 0 < lost <= self.MAX_LOST and low.endswith(lowered) and count > best_count:
                best, best_count = name, count
        if best is not None:
            return best + text[len(word):]
        rest = text
        for name, count in sorted(self._npcs.items(), key=lambda kv: -kv[1]):
            parts = name.split(" ", 1)
            if len(parts) == 2 and count >= 2 and rest.startswith(parts[1] + " "):
                return parts[0] + " " + rest
        return None


_NPC_START_RX = re.compile(r"^(?:a|an|the)\s+[A-Za-z]")


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


#: Ability, heal and cast names never contain these: such a "name" is two messages fused.
_FUSED_SKILL_RX = re.compile(r"\b(?:hits?|heals?|slain|points?|loots?|begins?)\b|\bfor \d+")
_FUSED_NAME_RX = re.compile(
    r"\b(?:hits?|heals?|looks|has|have|begins?|tries|slashes|crushes|pierces|kicks|bites|loots)\b"
)
_SKILL_KINDS = frozenset({"ability_hit", "ability_miss", "heal", "cast", "resist", "debuff"})
_SPLIT_CANDIDATE_RX = re.compile(r"(?<=\S)\s+(?=\S)")


def parse_garbled_amount(text: str, ts: float, player_name: str = "") -> Event | None:
    """Dummy Fix: ``text`` read as a hit or heal whose number could not be read.

    Returns the event with ``amount=None`` and ``estimated=True`` (the caller fills in the
    zone's average, see :meth:`mnmparse.stats.Stats.estimate_amount`), or ``None`` when the
    line is not such a hit or heal (or its number is readable after all).
    """
    clean = normalize_ocr(text)
    m = _GARBLED_AMOUNT_RX.search(clean)
    if m is None or re.fullmatch(r"\s*\d+\s*", m.group("amt") or ""):
        return None
    fixed = clean[: m.start()] + f"for 0 {m.group('unit')}" + clean[m.end():]
    ev = parse_line(fixed, ts, player_name)
    if ev.kind not in ("melee_hit", "ability_hit", "heal") or looks_fused(ev):
        return None
    ev.text = text
    ev.amount = None
    ev.estimated = True
    return ev


#: Small words an ability name may contain in lower case ("Pact of Renewal", "Mend the
#: Dead", "Invisibility versus Undead").
_NAME_FUNCTION_WORDS = frozenset({"of", "the", "versus", "and", "to", "a", "an", "in", "on", "for", "from", "with", "at", "by"})


def _garbled_skill(skill: str | None) -> bool:
    """An ability name with two or more lower-case words that are not small function words
    is OCR garbage ("snirit LIAI connecl in a skeletal defender's Strike") or two messages
    run together; ability names are Title Case."""
    if not skill:
        return False
    loose = [w for w in skill.split() if w[:1].islower() and w.strip("'.,:;!?") not in _NAME_FUNCTION_WORDS]
    return len(loose) >= 2


#: "Gozif's Slice hits a for 3+oints,ofrBleed Damag": the ability and who used it are readable
#: even when the rest is not.  Credited for crowd control and debuffs; never counted as damage.
_PARTIAL_ABILITY_RX = re.compile(
    r"^(?:(?P<actor>[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]+)?|(?:a|an|the)\s+[a-z]+(?:\s+[a-z]+){0,2})'s|(?P<you>Your))\s+"
    r"(?P<skill>[A-Z][a-z']+(?:\s+(?:of\s+|the\s+)?[A-Z][a-z']+){0,3}(?:\s+[IVX]{1,4})?)\s+(?:hits?|heals?)\b"
)


def _salvage_ability(clean: str, ts: float, text: str, player_name: str) -> Event | None:
    m = _PARTIAL_ABILITY_RX.match(clean)
    if m is None:
        return None
    actor = (player_name or "You") if m.group("you") else m.group("actor")
    return Event(ts=ts, kind="ability_partial", text=text, actor=actor, skill=m.group("skill"))


def looks_fused(ev: Event) -> bool:
    """True when a parsed event swallowed a second message (see :func:`split_fused`)."""
    if ev.kind in _SKILL_KINDS and ev.skill and (_FUSED_SKILL_RX.search(ev.skill) or len(ev.skill.split()) > 5):
        return True
    for name in (ev.actor, ev.target):
        if name and (len(name.split()) > 5 or _FUSED_NAME_RX.search(name)):
            return True
    return bool(ev.dtype and len(ev.dtype.split()) > 3)


def split_fused(text: str, player_name: str = "", *, depth: int = 2) -> list[str]:
    """``text`` as one or more messages.

    The OCR (or a line end it lost) sometimes runs two chat messages together: "Gozif's
    Slice hits a skeletal warrior for 3 points of Bleed a skeletal cleric begins casting
    Stun."  Parsed as one, the second message's damage was credited to the first one's
    attacker.  When the line parses badly (unknown, or an ability name / combatant that
    contains a second sentence), it is cut at the first place where a new message starts
    and the rest parses on its own.
    """
    if depth <= 0 or not text:
        return [text]
    forced = _FORCED_SPLIT_RX.search(text)
    if forced is not None:
        # a message that always starts a new one, whatever the first part parsed as: "Your
        # faction standing with X cannot possibly get a You gain party experience!"
        left, right = text[: forced.start()].strip(), text[forced.end():].strip()
        return [*split_fused(left, player_name, depth=depth), *split_fused(right, player_name, depth=depth)]
    ev = parse_line(text, 0.0, player_name)
    if ev.kind not in ("unknown", "ability_partial") and not looks_fused(ev):
        return [text]
    for m in _SPLIT_CANDIDATE_RX.finditer(text):
        left, right = text[: m.start()].strip(), text[m.end():].strip()
        if len(left) < 12 or not starts_message(right):
            continue
        if not _parses_cleanly(left, player_name):
            completed = _complete_truncated(left, player_name)
            if completed is not None:
                left = completed
            elif not (
                len(left.split()) >= 4
                and (
                    _LOOT_START_RX.match(right)
                    or (not left.rstrip().endswith(("'s", "s'")) and _CLEAN_START_RX.match(right))
                )
                and _parses_cleanly(right, player_name)
            ):
                continue  # the cut is inside a message ("a skeletal cleric's | Shock hits ...")
            # else: the first message is unreadable, but a clean one follows; cut it off so
            # its numbers are not credited to the first message's attacker
        return [left, *split_fused(right, player_name, depth=depth - 1)]
    return [text]


#: A second message that can be trusted to start here (not a capitalised ability word).
_CLEAN_START_RX = re.compile(r"^(?:(?:a|an|the)\s|You\b|Your\b|--|[A-Z][a-z]{2,}'s\s)")
#: A loot line: no ability is called "<Name> loots", so it starts a message even right after a
#: cut-off "... from a skeletal monk's".
_LOOT_START_RX = re.compile(r"^-*[A-Z][a-z]{2,}\s+loots?\s")
#: Where a new message starts whatever the line around it parsed as: the experience line (also
#: "ou gain", "You zain", "ekperience"), and the attack toggles after a kill line whose "!" was
#: read as l / I / 1 / | ("... has slain a skeletal monkl Stopped attacking.").
_FORCED_SPLIT_RX = re.compile(
    r"(?<=\S)\s+(?=[Yy]?ou\s+[gz]ain\s+(?:party\s+)?e[xk]perience\b)"
    r"|(?<=[a-z][lI1|])\s+(?=(?:Stopped\s+attacking|Starting\s+to\s+attack|You\s+gain)\b)"
)


def _parses_cleanly(text: str, player_name: str) -> bool:
    ev = parse_line(text, 0.0, player_name)
    return ev.kind not in ("unknown", "marker", "ability_partial") and not looks_fused(ev)


_TRUNCATED_ENDINGS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"\bfor\s+\d+$"), (" points of damage.", " Health.")),
    (re.compile(r"\bfor\s+\d+\s+points?$"), (" of damage.",)),
    (re.compile(r"\bfor\s+\d+\s+points?\s+of$"), (" damage.",)),
    (re.compile(r"\bfor\s+\d+\s+points?\s+of\s+[A-Za-z]+(?:\s+[A-Za-z]+)?$"), (" Damage.", ".")),
)


#: Whole lines are completed only when they stop right after the number, "points" or "of": with
#: a word after "of" the end may be a cut-off damage type or a second message.
_WHOLE_LINE_ENDINGS = _TRUNCATED_ENDINGS[:3]


def _complete_truncated(
    text: str, player_name: str, endings_table: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = _TRUNCATED_ENDINGS
) -> str | None:
    """A hit or heal whose end was lost ("... hits a skeletal fighter for 41"), completed so it
    parses; ``None`` when ``text`` is not one.  The number was read, so nothing is invented."""
    stem = re.sub(r"[^0-9A-Za-z]+$", "", text)
    for rx, endings in endings_table:
        if rx.search(stem):
            for ending in endings:
                if parse_line(stem + ending, 0.0, player_name).kind in ("melee_hit", "ability_hit", "heal"):
                    return stem + ending
    return None


def parse_line(text: str, ts: float, player_name: str = "") -> Event:
    """Parse one combat message into an :class:`Event`.

    ``text`` is the message as emitted by the tracker (wrapped lines
    already joined).  ``ts`` is the time it was first seen.  When
    ``player_name`` is given, ``You``/``Your``/``YOU`` map to it; otherwise
    they map to ``"You"``.  Unknown or garbled text yields
    ``kind="unknown"`` with ``text`` preserved; this function never raises.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    try:
        clean = normalize_ocr(text)
        is_pet = False
        pet = _PET_RX.match(clean)
        if pet is not None:
            # "Your pet Fobaf tries to crush X" -> "Fobaf tries to crush X", flagged.
            clean = pet.group(1) + clean[pet.end():]
            is_pet = True
        if clean:
            for kind, rx in RULES:
                m = rx.match(clean)
                if m is not None:
                    ev = _build(kind, m, text, ts, player_name)
                    ev.is_pet = is_pet
                    if ev.skill in _SKILL_NAMES:
                        ev.skill = _SKILL_NAMES[ev.skill]
                    if ev.kind in _SKILL_KINDS and _garbled_skill(ev.skill):
                        log.debug("garbled ability name, line left unread: %r", text)
                        break
                    return ev
            else:
                # "... hits a skeletal fighter for 41 points of": the window cut the line after the
                # number was read (the wrapped rest, if any, arrives on its own as "damage.")
                completed = _complete_truncated(clean, player_name, _WHOLE_LINE_ENDINGS)
                if completed is not None:
                    ev = parse_line(completed, ts, player_name)
                    ev.text, ev.is_pet = text, is_pet
                    return ev
        partial = _salvage_ability(clean if "clean" in locals() else "", ts, text, player_name)
        if partial is not None:
            return partial
        log.debug("unparsed line: %r", text)
    except Exception:  # noqa: BLE001 - the contract is "never raise"
        log.exception("parse_line failed on %r", text)
    return Event(ts=ts, kind="unknown", text=text)
