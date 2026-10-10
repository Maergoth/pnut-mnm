"""Learned spellings: conflate OCR variants of the same name.

The game repeats a small vocabulary of names (players, NPCs, items, zones, abilities),
and the OCR misreads a character or two now and then: ``Bone Ohips`` for ``Bone Chips``,
``Night HarbOF (East)`` for ``Night Harbor (East)``, ``Dogabetarozem``, ``Tomb of the
Last Wvrmsbane``.  :class:`Vocabulary` counts every name per category, plus every word
of every message, and maps a reading to a better-attested spelling when

* they have the same words except for one or two character edits inside a word (case,
  accents, look-alike digits and stray punctuation are ignored; words of three letters or
  less must match exactly, so "Cap" and "Cape" or "Axe" and "Mace" never merge; a first
  word that lost its leading letters to clipping, "ght Harbor", also matches, and so does
  the word after an NPC's article, "a eletal warrior"), and
* the evidence is lopsided: the better spelling was seen at least twice as often, or the
  words that differ are at least four times as common elsewhere in the chat.

The spelling a group of variants is shown with is the one whose differing words are the
common ones: "Tomb of the Last Wvrmsbane" was read twice and "...Wyrmsbane" once, but the
word "Wyrmsbane" appears in hundreds of other lines, so that spelling wins.  The counts can
be saved and loaded, so spellings learned in earlier sessions correct the first readings
of a new one.

Player and NPC names are learned from combat lines only (:data:`NAME_KINDS`): the status
catch-all, /con text and the viewer's own lines (faction standings, "Beginning to memorize
...") name things that are not combatants.  Loading a saved file drops the entries that
cannot be names at all (see :func:`could_be_name`).

The class is thread-safe: the engine thread observes while the GUI thread reads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .grammar import NPC, PLAYER, YOU_TOKENS, is_npc_name, is_you

log = logging.getLogger(__name__)

__all__ = [
    "CATEGORIES", "GLOBAL", "NAME_KINDS", "Vocabulary", "close_spellings", "could_be_name", "edit_distance",
    "edit_limit", "fold", "observe_event",
]

#: Name categories kept apart (a player and an item never conflate).
CATEGORIES: tuple[str, ...] = ("player", "npc", "item", "zone", "skill")

#: Event kinds whose actor and target are combatants, the only names learned.  Not
#: ``status`` (the catch-all takes the first words of any sentence: "The", "Beginning",
#: "a caiman views you"), ``consider``, ``personal`` (faction names), ``unknown``, nor any
#: other kind (coin splits, rewards, crafting...).
NAME_KINDS: frozenset[str] = frozenset({
    "melee_hit", "melee_miss", "ability_hit", "ability_partial", "ability_miss", "env_damage",
    "heal", "cast", "interrupt", "resist", "fizzle", "kill", "loot", "coin",
    "cc", "cc_fade", "debuff", "damage_effect", "aggro", "awaken",
})

#: A rarer spelling conflates into a better one seen at least this many times as often...
COUNT_DOMINANCE = 2.0
#: ...or whose differing words are at least this many times as common in the chat.
WORD_DOMINANCE = 4.0
#: Names shorter than this (after folding) are never conflated: too little to go on.
MIN_LENGTH = 5
#: A name that begins longer names ("a skeletal" in "a skeletal warrior", "a skeletal
#: cleric") read less than this share as often as all of them together is a cut-off
#: reading of one of them, not a mob of its own (unlike "a jackal" beside "a jackal pup").
CUT_OFF_SHARE = 0.02
#: Saved files keep names / words seen at least this often, at most this many per table.
SAVE_MIN_COUNT = 2
SAVE_MAX_ENTRIES = 5000

_EDGE_JUNK_RX = re.compile(r"^[^0-9A-Za-z(\[]+|[^0-9A-Za-z)\]]+$")
_WORD_RX = re.compile(r"[a-z0-9']+")
_LOOKALIKE_DIGITS = str.maketrans({"0": "o", "1": "l", "5": "s", "8": "b", "9": "g", "|": "l"})


def clean(name: str) -> str:
    """``name`` without stray punctuation at either end (``"Night Harbor (East).-"``)."""
    return _EDGE_JUNK_RX.sub("", name.strip())


def fold(name: str) -> str:
    """Comparison form: cleaned, accents removed, case-folded, look-alike digits as letters."""
    text = unicodedata.normalize("NFKD", clean(name))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().translate(_LOOKALIKE_DIGITS).split())


def edit_limit(length: int) -> int:
    """Character edits allowed between two spellings of a name this long (in total)."""
    if length < MIN_LENGTH:
        return 0
    if length <= 8:
        return 1
    if length <= 16:
        return 2
    return 3


def word_limit(length: int) -> int:
    """Character edits allowed inside one word this long."""
    if length <= 3:
        return 0
    if length <= 6:
        return 1
    if length <= 12:
        return 2
    return 3


_BARE_RX = re.compile(r"^[^0-9a-z]+|[^0-9a-z]+$")
_ARTICLES = frozenset({"a", "an", "the"})


def close_spellings(a: str, b: str) -> bool:
    """True when the folded spellings ``a`` and ``b`` may be the same name misread."""
    if a == b:
        return True
    total = edit_limit(min(len(a), len(b)))
    if total == 0:
        return False
    aw, bw = a.split(), b.split()
    if len(aw) != len(bw):
        # a split or fused word ("Night H bor"): judged on the whole string, strictly
        limit = min(2, total)
        return edit_distance(a, b, limit) <= limit
    # the word that may have lost its leading letters: the first, or the one after an
    # NPC's article ("a eletal warrior" for "a skeletal warrior"; at least four letters
    # left there, so "a rat" and "a brat" stay apart)
    clipped, keep = (1, 4) if aw[0] == bw[0] and aw[0] in _ARTICLES else (0, 2)
    used = 0
    for i, (x, y) in enumerate(zip(aw, bw)):
        x, y = _BARE_RX.sub("", x), _BARE_RX.sub("", y)
        if x == y:
            continue
        if i == clipped and min(len(x), len(y)) >= keep and (x.endswith(y) or y.endswith(x)):
            used += abs(len(x) - len(y))  # leading letters clipped off
            continue
        limit = word_limit(min(len(x), len(y)))
        if limit == 0:
            return False
        d = edit_distance(x, y, limit)
        if d > limit:
            return False
        used += d
    return used <= total


def edit_distance(a: str, b: str, limit: int) -> int:
    """Levenshtein distance of ``a`` and ``b``, or ``limit + 1`` once it exceeds ``limit``."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        best = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            best = min(best, value)
        if best > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _words(text: str) -> list[str]:
    return _WORD_RX.findall(fold(text))


_PLAYER_SHAPE_RX = re.compile(PLAYER)
_NPC_SHAPE_RX = re.compile(NPC)
_YOU_WORDS = frozenset(t.casefold() for t in YOU_TOKENS)
#: Words the game starts its own sentences with ("The call to arms fades.", "This ability
#: is not available right now.", "Beginning to memorize ..."): never a name on their own.
_SENTENCE_WORDS = frozenset({
    "a", "an", "the", "this", "that", "there", "someone", "something",
    "starting", "stopped", "beginning", "entering", "loading", "welcome",
})


def could_be_name(category: str, name: str | None) -> bool:
    """False for a ``player`` or ``npc`` entry that cannot be a combatant's name.

    That is one the grammar's name patterns cannot produce ("Denizens of Wyrmsbane Tomb",
    "a skeletal priest is"), one with a "you" in it ("a caiman views you", "Your Blind"),
    or a word the game starts its own sentences with ("The", "Beginning").  Names in the
    other categories are not checked.
    """
    if not name:
        return False
    if category not in ("player", "npc"):
        return True
    name = clean(name)
    shape = _NPC_SHAPE_RX if category == "npc" else _PLAYER_SHAPE_RX
    if shape.fullmatch(name) is None:
        return False
    words = name.casefold().split()
    if any(w in _YOU_WORDS for w in words):
        return False
    return not (len(words) == 1 and words[0] in _SENTENCE_WORDS)


class Vocabulary:
    """Counts of names per category and of chat words; maps misreadings to spellings."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._names: dict[str, Counter[str]] = {c: Counter() for c in CATEGORIES}
        self._words: Counter[str] = Counter()
        self._version = 0
        #: per category: (version, variant -> spelling, spelling -> readings of its group)
        self._cache: dict[str, tuple[int, dict[str, str], Counter[str]]] = {}
        #: per category: name -> folded spelling, and name -> the names close to it.  Worked out
        #: once per new name, so rebuilding a mapping never compares every pair again (with a few
        #: hundred learned names that cost ~100 ms of Python per rebuild, several times a second
        #: in a fight, and starved the GUI thread).
        self._folded: dict[str, dict[str, str]] = {c: {} for c in CATEGORIES}
        self._near: dict[str, dict[str, set[str]]] = {c: {} for c in CATEGORIES}

    # -- learning ------------------------------------------------------------------------
    def observe(self, category: str, name: str | None, n: int = 1) -> None:
        """Count one reading of ``name`` in ``category`` (unknown categories are ignored)."""
        if not name or category not in self._names:
            return
        name = clean(name)
        if not name:
            return
        with self._lock:
            self._names[category][name] += n
            self._version += 1

    def observe_text(self, text: str | None) -> None:
        """Count the words of one chat line (the evidence for which spelling is right)."""
        if not text:
            return
        words = _words(text)
        if not words:
            return
        with self._lock:
            self._words.update(words)
            self._version += 1

    def clear(self) -> None:
        with self._lock:
            for counter in self._names.values():
                counter.clear()
            self._words.clear()
            self._cache.clear()
            for table in (*self._folded.values(), *self._near.values()):
                table.clear()
            self._version += 1

    # -- lookup --------------------------------------------------------------------------
    def canonical(self, category: str, name: str | None) -> str | None:
        """The best-attested spelling of ``name`` (``name`` itself, cleaned, when none)."""
        if not name:
            return name
        mapping = self._mapping(category)
        cleaned = clean(name)
        return mapping.get(cleaned, mapping.get(name, cleaned or name))

    def canonical_map(self, category: str, names: Iterable[str]) -> dict[str, str]:
        """``{name: canonical spelling}`` for every name in ``names``."""
        mapping = self._mapping(category)
        out: dict[str, str] = {}
        for name in names:
            if name:
                cleaned = clean(name)
                out[name] = mapping.get(cleaned, cleaned or name)
        return out

    def fold_counts(self, category: str, counts: Counter[str] | dict[str, int]) -> Counter[str]:
        """``counts`` with the keys mapped to their canonical spellings and summed."""
        mapping = self.canonical_map(category, counts.keys())
        folded: Counter[str] = Counter()
        for name, n in counts.items():
            folded[mapping.get(name, name)] += n
        return folded

    def word_count(self, word: str) -> int:
        with self._lock:
            return self._words.get(fold(word), 0)

    def distinct(self, category: str, a: str | None, b: str | None, *, min_count: int = 10) -> bool:
        """True when ``a`` and ``b`` are two different, well-attested names.

        That is: both are known, they map to two different spellings (neither is a variant
        of the other, nor are both variants of a third), each spelling's group (it and its
        variants) was read at least ``min_count`` times, and the two spellings are not
        close (:func:`close_spellings`: one may still be a misreading of the other, the
        counts just do not tell which).  "a jackal" and "a jackal pup" seen a dozen times
        each are distinct; "a eletal warrior" (a variant of "a skeletal warrior"), an
        unknown name or a rare one is not, and neither is a cut-off reading such as "a
        skeletal" next to the far more common names it begins (see :data:`CUT_OFF_SHARE`).
        A few dict lookups once the mapping is built (plus one pass over the spellings when
        one name begins the other), for vetoing fuzzy merges pair by pair.
        """
        if not a or not b:
            return False
        with self._lock:
            mapping = self._mapping(category)
            totals = self._cache[category][2]
            sa, sb = mapping.get(clean(a)), mapping.get(clean(b))
            if sa is None or sb is None or sa == sb:
                return False
            if totals[sa] < min_count or totals[sb] < min_count:
                return False
            if sb in self._near[category].get(sa, ()):
                return False
            folded = self._folded[category]
            short, long_ = sorted((sa, sb), key=lambda s: len(folded[s]))
            prefix = folded[short] + " "
            if folded[long_].startswith(prefix):
                begun = sum(n for s, n in totals.items() if folded[s].startswith(prefix))
                if totals[short] < CUT_OFF_SHARE * begun:
                    return False
            return True

    def _mapping(self, category: str) -> dict[str, str]:
        """Variant -> canonical spelling for ``category`` (cached until new observations)."""
        with self._lock:
            cached = self._cache.get(category)
            if cached is not None and cached[0] == self._version:
                return cached[1]
            counts = dict(self._names.get(category, {}))
            words = self._words
            folded, near = self._neighbours(category, counts)
            mapping = self._build(counts, words, folded=folded, near=near)
            totals: Counter[str] = Counter()
            for name, n in counts.items():
                totals[mapping[name]] += n
            self._cache[category] = (self._version, mapping, totals)
            return mapping

    def _neighbours(self, category: str, counts: dict[str, int]) -> tuple[dict[str, str], dict[str, set[str]]]:
        """Folded spelling and close spellings of every name in ``counts`` (new names only
        are compared, each against the names already known)."""
        folded = self._folded.setdefault(category, {})
        near = self._near.setdefault(category, {})
        for name in counts:
            if name in folded:
                continue
            f = fold(name)
            mine = near.setdefault(name, set())
            for other, other_fold in folded.items():
                if close_spellings(f, other_fold):
                    mine.add(other)
                    near.setdefault(other, set()).add(name)
            folded[name] = f
        return folded, near

    @classmethod
    def _build(
        cls,
        counts: dict[str, int],
        words: Counter[str],
        *,
        folded: dict[str, str] | None = None,
        near: dict[str, set[str]] | None = None,
    ) -> dict[str, str]:
        """Greedy clustering, most frequent spellings first.

        Each spelling joins the first group it is close to when that group's spelling
        dominates it; when the newcomer's differing words are the much more common ones,
        it becomes the group's spelling instead.  With ``folded``/``near`` (see
        :meth:`_neighbours`) only the groups whose spelling is close are looked at; without
        them every group is compared (the same result, much slower).
        """
        if not counts:
            return {}
        ranked = sorted(counts, key=lambda n: (counts[n], -len(n), n), reverse=True)
        groups: list[list[Any]] = []  # [spelling, folded, members]
        index: dict[str, int] = {}  # current group spelling -> its group's position
        for name in ranked:
            f = folded[name] if folded is not None and name in folded else fold(name)
            if near is not None:
                candidates = sorted(index[o] for o in near.get(name, ()) if o in index)
            else:
                candidates = [i for i, g in enumerate(groups) if close_spellings(f, g[1])]
            for gi in candidates:
                group = groups[gi]
                spelling, sfold, members = group
                if f != sfold and cls._words_better(name, spelling, words):
                    members.append(spelling)
                    del index[spelling]
                    group[0], group[1] = name, f
                    index[name] = gi
                    break
                if f == sfold or cls._dominates(spelling, name, counts, words):
                    members.append(name)
                    break
            else:
                index[name] = len(groups)
                groups.append([name, f, []])
        mapping: dict[str, str] = {}
        for spelling, _folded, members in groups:
            mapping[spelling] = spelling
            for member in members:
                mapping[member] = spelling
        return mapping

    @staticmethod
    def _differing_words(a: str, b: str) -> list[tuple[str, str]] | None:
        aw, bw = _words(a), _words(b)
        if len(aw) != len(bw):
            return None
        return [(x, y) for x, y in zip(aw, bw) if x != y]

    @classmethod
    def _words_better(cls, a: str, b: str, words: Counter[str]) -> bool:
        """True when every word ``a`` has in place of ``b``'s is far more common in the chat."""
        differing = cls._differing_words(a, b)
        if not differing:
            return False
        return all(words.get(x, 0) >= WORD_DOMINANCE * max(1, words.get(y, 0)) for x, y in differing)

    @classmethod
    def _dominates(cls, good: str, rare: str, counts: dict[str, int], words: Counter[str]) -> bool:
        if counts.get(good, 0) >= COUNT_DOMINANCE * counts.get(rare, 0):
            return True
        differing = cls._differing_words(good, rare)
        if differing is None:
            return False
        if not differing:
            return True
        return all(words.get(g, 0) >= WORD_DOMINANCE * max(1, words.get(r, 0)) for g, r in differing)

    # -- persistence ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            names = {
                cat: dict(c for c in counter.most_common(SAVE_MAX_ENTRIES) if c[1] >= SAVE_MIN_COUNT)
                for cat, counter in self._names.items()
            }
            words = dict(w for w in self._words.most_common(SAVE_MAX_ENTRIES) if w[1] >= SAVE_MIN_COUNT)
        return {"version": 1, "names": names, "words": words}

    def merge_dict(self, data: dict[str, Any]) -> None:
        """Add the counts of a saved :meth:`to_dict` payload.

        Player and NPC entries that cannot be names (:func:`could_be_name`) are dropped:
        files saved before names were learned from combat lines only hold some.
        """
        if not isinstance(data, dict):
            return
        dropped = 0
        with self._lock:
            for cat, table in (data.get("names") or {}).items():
                if cat in self._names and isinstance(table, dict):
                    for name, n in table.items():
                        if isinstance(name, str) and isinstance(n, int) and n > 0:
                            if not could_be_name(cat, name):
                                dropped += 1
                                continue
                            self._names[cat][name] += n
            for word, n in (data.get("words") or {}).items():
                if isinstance(word, str) and isinstance(n, int) and n > 0:
                    self._words[word] += n
            self._version += 1
        if dropped:
            log.info("learned spellings: dropped %d saved entries that are not names", dropped)

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def load(self, path: str | Path) -> bool:
        path = Path(path)
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("could not read %s: %s", path, exc)
            return False
        self.merge_dict(data)
        return True


#: The app-wide vocabulary (the engine and imports feed it; views read it).
GLOBAL = Vocabulary()


def observe_event(vocab: Vocabulary | None, ev: Any) -> None:
    """Feed one parsed event's names and words to ``vocab`` (``None`` does nothing).

    The words of every line count; actor and target names only from :data:`NAME_KINDS`.
    """
    if vocab is None or ev is None:
        return
    vocab.observe_text(getattr(ev, "text", None))
    if getattr(ev, "kind", "") in NAME_KINDS:
        for name in (getattr(ev, "actor", None), getattr(ev, "target", None)):
            if not name or is_you(name) or name in ("them", "em", "yourself"):
                continue
            category = "npc" if is_npc_name(name) else "player"
            if could_be_name(category, name):
                vocab.observe(category, name)
    if getattr(ev, "kind", "") == "zone" and getattr(ev, "target", None):
        vocab.observe("zone", ev.target)
    if getattr(ev, "item", None):
        vocab.observe("item", ev.item)
    if getattr(ev, "skill", None) and getattr(ev, "kind", "") in ("ability_hit", "ability_miss", "heal", "cast", "debuff"):
        vocab.observe("skill", ev.skill)
