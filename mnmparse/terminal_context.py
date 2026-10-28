"""Conservative evidence that a final OCR glyph represents an exclamation mark.

This only answers the tracker's wrapped-line question. It never repairs text or
changes row matching: an independently learned complete name must explain the
entire final field of one of the supported sentence shapes.
"""

from __future__ import annotations

import re

from .grammar import NAME, PLAYER, is_you
from .parser import normalize_ocr, parse_line


# Lowercase i is an ordinary final letter in names and has no observed example
# in the delayed Resist lines. Do not extend this set by case folding.
_BANG_GLYPHS = frozenset("lI1|")
_RESIST_RX = re.compile(rf"^(?:{NAME})\s+resists\s+(?:your|Your)\s+(?P<value>.+)$")
_KILL_RX = re.compile(
    rf"^(?:Your\s+party\s+member\s+{PLAYER}|{NAME})"
    rf"\s+(?:has|have)\s+slain\s+(?P<value>(?:{NAME})[lI1|])$"
)
_SPELL_KINDS = frozenset({"cast", "ability_hit", "heal", "resist"})
_NAME_KINDS = _SPELL_KINDS | {"kill", "melee_hit", "melee_miss"}
_VALUE_RX = re.compile(r"[A-Za-z][A-Za-z0-9' -]{0,159}")
_ROMAN_OR_LOOKALIKE_RX = re.compile(r"[IVXLCDMl1|]+")
_GLUED_ROMAN_RANK_RX = re.compile(r"[a-z][IVXLCDM]+$")
_MAX_TEXT = 1024


class TerminalContext:
    """Ephemeral, bounded vocabulary learned from confirmed complete messages.

    ``trustworthy`` must come from the tracker's exact good-quality row votes,
    before recording emission. A frame count alone does not supply that proof.
    Joined messages are excluded from learning even if both parts were good:
    this keeps a mistaken join from teaching a new complete spelling.
    """

    MAX_VALUES = 512

    def __init__(self) -> None:
        self._spells: dict[str, None] = {}
        self._names: dict[str, None] = {}
        self._spells_saturated = False
        self._names_saturated = False

    def observe(
        self,
        text: str,
        *,
        trustworthy: bool,
        backlog: bool = False,
        estimated_ts: bool = False,
        fragment: bool = False,
        joined: bool = False,
    ) -> None:
        """Learn only an actual punctuated, trustworthy, live, whole message."""
        if (
            not trustworthy or backlog or estimated_ts or fragment or joined
            or not text or len(text) > _MAX_TEXT or text.rstrip()[-1:] not in ".!?"
        ):
            return
        event = parse_line(text, 0.0)
        if event.kind not in _NAME_KINDS or event.estimated or event.estimated_ts:
            return
        if event.kind in _SPELL_KINDS:
            self._remember(self._spells, event.skill)
        for name in (event.raw_actor, event.target):
            if name and not is_you(name):
                self._remember(self._names, name)

    def is_terminal(self, text: str) -> bool:
        """Recognize one glued bang lookalike; the caller still proves row trust.

        Literal punctuation, unknown spellings, separated glyphs and ambiguous
        ranks or known longer names deliberately return false. The normal
        joining/timeout path continues to handle those messages.
        """
        if not text or len(text) > _MAX_TEXT:
            return False
        raw = text.rstrip()
        if raw[-1:] not in _BANG_GLYPHS or len(raw) < 2 or not raw[-2].isalnum():
            return False
        # The parser already turns some final I/1/| readings into '!'. Keep this
        # decision independent of that repair and preserve the actual last glyph.
        clean = normalize_ocr(raw[:-1]) + raw[-1]
        resist = _RESIST_RX.fullmatch(clean)
        if resist is not None:
            value = resist.group("value")
            return not self._spells_saturated and self._explained(value, self._spells)
        kill = _KILL_RX.fullmatch(clean)
        if kill is not None:
            value = kill.group("value")
            return not self._names_saturated and self._explained(value, self._names)
        return False

    def _remember(self, vocabulary: dict[str, None], value: str | None) -> None:
        if not value or _VALUE_RX.fullmatch(value) is None:
            return
        if value in vocabulary:
            return
        if len(vocabulary) >= self.MAX_VALUES:
            # Forgetting a full/longer spelling could wrongly authorize a shorter
            # one. Once we cannot retain all evidence, use ordinary joining for
            # this vocabulary for the remainder of the tracker instance.
            if vocabulary is self._spells:
                self._spells_saturated = True
            else:
                self._names_saturated = True
            return
        vocabulary[value] = None

    @staticmethod
    def _explained(value: str, vocabulary: dict[str, None]) -> bool:
        if value in vocabulary:
            return False  # an independently observed full spelling always wins
        stem = value[:-1]
        if not stem or stem not in vocabulary:
            return False
        # Spell I -> Spell II and Spell I -> Spell Il are ambiguous ranks. The
        # glyph must be glued to a normal word, never to a standalone rank.
        if _ROMAN_OR_LOOKALIKE_RX.fullmatch(value.rsplit(" ", 1)[-1]):
            return False
        if _GLUED_ROMAN_RANK_RX.search(stem.rsplit(" ", 1)[-1]):
            return False
        # A known longer spelling may really wrap here; do not preempt its tail.
        return not any(
            known.startswith(stem + " ") or known.startswith(value + " ")
            for known in vocabulary
        )
