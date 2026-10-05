"""Scrolling-text de-duplication and wrapped-line joining for the combat window.

The combat chat window is read off the screen several times per second.  Each frame
yields a list of OCR lines (``x``, ``y``, ``h``, ``text``) for the visual rows that are
currently visible.  Consecutive frames overlap almost completely; the window only
scrolls up when new messages arrive.  :class:`Tracker` turns that stream of
overlapping, noisy snapshots into a stream of distinct :class:`Message` objects:

* OCR lines that share a vertical band are merged into one visual *row* (the engine
  sometimes splits a row into fragments).
* Rows are aligned with the rows of the previous frame by their vertical offset: the
  window scrolls up by some distance ``dy``; every previously known row should reappear
  ``dy`` pixels higher.  ``dy`` is found by voting over all pairs of similar texts, so
  garbled rows, dropped rows and fractional scroll distances (wrapped continuation rows
  are spaced slightly tighter than new messages) do not break the alignment.
* Every logical row collects the text variants observed for it across frames, together
  with how trustworthy the observation was: the clipped top row is never trusted, a row
  whose left edge is off the text margin (first glyph clipped, or junk prepended), a
  row with an abnormal height or a row assembled from fragments is trusted less.  A row
  is emitted once ``min_frames`` trustworthy observations agree; its text is the variant
  that best agrees with everything observed.  Rows that scroll off the window before
  that, and rows still pending at :meth:`Tracker.flush`, are emitted with their best
  available text.
* Emission is in window order.  A row that cannot become ready (it was only ever seen
  garbled) is resolved after ``stale_s`` so that it does not block the rows below it.
* A message whose text does not end with terminal punctuation is the first half of a
  wrapped message; it is held and joined with the next message, or emitted on its own
  after ``join_timeout_s``.
* Old content is never emitted twice.  Every emitted row gets a sequence number and the
  last ``HISTORY_ROWS`` are remembered.  When the window shows old rows again (the chat
  was scrolled back with the mouse wheel, or re-rendered after a death or a zone change),
  they are recognised and skipped: after a jump the whole frame is aligned with that
  history; a burst of new rows directly under an old row is checked against the rows
  that originally followed it as a block; and a row appearing below an old row is
  compared with the rows that originally followed it.  A burst under the newest row is
  always new: group spells print the same block of lines on every cast.
* When a frame cannot be aligned with the previous one (a "jump"), rows seen in only one
  frame are held back rather than emitted: the jump may be one bad frame after which
  the same rows show again.  They are released at the first frame that aligns again,
  minus the ones that are still (or again) in the window.  Rows that appear after a jump,
  a scroll-back or an occlusion arrived at some unknown time since the window was last
  in sync: their times are spread evenly over that gap and flagged as estimated.
* The history can be saved and loaded (:meth:`Tracker.export_state`,
  :meth:`Tracker.import_state`), so that a restarted app recognises the rows already on
  screen as old.  Without it, the rows of the very first frame are flagged as backlog.

Only the ``.x``, ``.y``, ``.h`` and ``.text`` attributes of the line objects are used,
so any object with those attributes (``ocr.OcrLine`` or a test stand-in) works.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import statistics
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from mnmparse.grammar import starts_message

log = logging.getLogger(__name__)

#: Characters that end a complete message (the game wraps long messages onto a second
#: visual row without any marker; only the last row ends with one of these).
TERMINAL_PUNCTUATION = ".!?\"'”’"

#: Fallback geometry (pixels in the OCR input image) until a frame lets us measure it.
DEFAULT_ROW_PITCH = 40.0
DEFAULT_LINE_HEIGHT = 23.0

#: Two observations of the same row "agree" when their normalized similarity is at
#: least this high (one or two characters of OCR jitter on a typical line).
AGREE_SIMILARITY = 0.90

#: Weight of an alignment vote that comes from the unreliable top row.
TOP_ROW_WEIGHT = 0.5

#: Alignment vote weights by match strength.  Exact (normalized) repeats are what a
#: static or scrolled window produces; fuzzy matches also arise between *different*
#: messages of the same template ("…for 3 points" / "…for 5 points"), so they must not
#: be able to outvote the exact ones.
WEIGHT_EXACT = 1.0
WEIGHT_CLOSE = 0.6  #: ratio >= AGREE_SIMILARITY
WEIGHT_LOOSE = 0.3  #: ratio >= Tracker.similarity

#: The winning alignment must explain at least this fraction of the overlapping rows
#: (weighted), and never less than one exact match's worth.
MIN_ALIGN_FRACTION = 0.4
MIN_ALIGN_SCORE = 1.0

#: Rows with fewer alphanumeric characters than this are OCR junk (a stray glyph at
#: the window edge, a scrollbar) and are ignored entirely.
MIN_ROW_ALNUM = 3

#: Observation quality tiers.
TIER_TOP = 0  #: the clipped top row: alignment only, never a text vote
TIER_WEAK = 1  #: off-margin, odd height or fragmented: text vote of last resort
TIER_GOOD = 2  #: a clean reading from a reliable position

#: Occlusion guard (SPEC section 7 step 0).  The game's character sheet / inventory
#: auto-opens over the chat window on loot; for those frames the chat rows are clipped
#: to a few characters and foreign UI text appears further right on the same rows.
DEFAULT_COMPLETE_LEN = 50.0  #: typical length of a complete row until measured
FRAGMENT_RATIO = 0.5  #: an unterminated row shorter than this * complete length is a fragment
OCCLUSION_FRACTION = 0.4  #: frames with at least this fraction of fragment rows are skipped
JOIN_MIN_RATIO = 0.7  #: a held first half must be at least this * complete length
JOIN_MAX_TAIL_RATIO = 0.7  #: a wrapped second half is shorter than this * complete length
CHAR_W_PER_H = 0.45  #: average glyph advance as a fraction of the OCR line height
FOREIGN_X_PX = 60  #: a row whose first fragment starts further right than this is foreign
CONTINUATION_SLACK_PX = (30, 80)  #: a fragment may start this far before/after the previous one's end

#: A visual row that is only a mitigation / critical marker, e.g. "(Block 6)".  The game
#: prints it after the period of a hit line; it wraps onto its own row when the line is full.
MARKER_ONLY_RX = re.compile(
    r"^\(\s*(?:Block\s+\d+|\d+\s+absorbed|Critical|Crippling\s+Blow)\s*\)[.!]?$", re.IGNORECASE
)

#: A message spans at most this many visual lines (coin splits take three).
MAX_JOIN_PARTS = 4

#: Emitted visual rows remembered for replay detection.  The window shows about a dozen
#: rows; scrolling it back with the mouse wheel can re-show content from several screens
#: earlier, and none of it may be emitted again.
HISTORY_ROWS = 400
#: A frame that adds at least this many new rows at once is checked against the history
#: as a block (normal scrolling adds one or two rows per frame)...
REPLAY_BLOCK_ROWS = 4
#: ...and is old content when at least this many of them line up with it, in order.
REPLAY_BLOCK_MATCHES = 3
#: A new bottom row directly under an emitted row whose successors are no longer in the
#: window is compared with the rows that followed that row when it was first shown.
REPLAY_CHAIN_SIMILARITY = 0.85
#: When the clean readings of a row disagree (OCR jitter), the row waits until one variant
#: was read identically twice, but never longer than this many trustworthy frames.
CONSENSUS_MAX_FRAMES = 4
#: Rows held back over a run of jump frames are released after this many jump frames in
#: a row even if no frame aligns (the window shows something else entirely).
JUMP_HOLD_MAX_FRAMES = 10
#: A held row matching a window row on its own (no matched neighbour) is the same row
#: only when it has at least this many alphanumeric characters.
DISTINCT_ROW_ALNUM = 20
#: Window state changes (jumped, scrolled back, occluded, back in sync) are logged at
#: INFO at most once per this many seconds per kind.
STATE_LOG_INTERVAL_S = 10.0
#: Format version of :meth:`Tracker.export_state`.
STATE_VERSION = 1

#: Words a wrapped first half can stop on: function words, possessives, numbers and the
#: capitalised damage-type words that precede "Damage" ("... for 16 points of Holy" + "Damage.").
#: "receive" ends the first half of a wrapped coin split ("..., and you receive" +
#: "22 copper coins from ... as your split."); "receiv" is its clipped OCR reading.
CONNECTOR_WORDS = frozenset(
    {
        "a", "an", "the", "and", "but", "or", "for", "of", "with", "from", "by", "to", "on",
        "at", "in", "is", "are", "was", "were", "their", "his", "her", "its", "your",
        "points", "point", "no", "not", "receive", "receiv",
        "holy", "bleed", "fire", "cold", "magic", "corruption", "electric", "electricity",
        "physical", "nature", "poison", "disease", "arcane", "shadow", "divine",
    }
)
_ARTICLE_START_RX = re.compile(r"^(?:a|an|the)\s+[a-z]")
_TRAILING_PUNCT_RX = re.compile(r"[^\w']+$")


def _ends_with_connector(text: str) -> bool:
    """True when the last word of ``text`` means the sentence cannot end there."""
    words = _TRAILING_PUNCT_RX.sub("", text.strip()).split()
    if not words:
        return False
    last = words[-1].strip("'").lower()
    if not last:
        return False
    return last in CONNECTOR_WORDS or last.isdigit() or last.endswith("'s") or last.endswith("s'")


class LineLike(Protocol):
    """The subset of ``ocr.OcrLine`` the tracker relies on."""

    x: int
    y: int
    h: int
    text: str


@dataclass
class Message:
    """A de-duplicated combat message.

    ``first_seen`` is the ``now`` of the frame in which the row first appeared (for a
    joined message: when its first half appeared); ``frames_seen`` is the number of
    frames in which the row was observed (for a joined message: the smaller count of
    the two halves).

    ``backlog`` marks a row that was already on screen in the first frame of a cold
    start (no history, no imported state): it may have been logged by an earlier run
    and its real time is unknown.  ``estimated_ts`` marks a row that appeared after a
    jump, a scroll-back or an occlusion: it arrived at some point while the window was
    out of sync, so ``first_seen`` was spread evenly over that gap.
    """

    text: str
    first_seen: float
    frames_seen: int
    fragment: bool = False  #: a short unterminated piece that was deliberately not joined
    backlog: bool = False  #: already on screen when a cold-started tracker saw its first frame
    estimated_ts: bool = False  #: ``first_seen`` was spread over a jump / scroll-back / occlusion gap


# --------------------------------------------------------------------------------------
# Text normalization / similarity
# --------------------------------------------------------------------------------------

_APOSTROPHES = "'’‘`"
_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
_DIGITISH_RE = re.compile(r"^[0-9ilo]+$")


def normalize(text: str) -> str:
    """Normalize OCR text for fuzzy comparison.

    Lower-cases, drops apostrophes, turns every other run of non-alphanumerics into a
    single space, collapses whitespace, and inside tokens that look like numbers maps
    ``I``/``l`` to ``1`` and ``O`` to ``0`` (i.e. when those letters sit next to digits:
    ``"1l"`` -> ``"11"``, ``"lO"`` -> ``"10"``; a lone ``"I"`` is left alone).
    """
    lowered = text.lower()
    for ch in _APOSTROPHES:
        lowered = lowered.replace(ch, "")
    tokens: list[str] = []
    for tok in _NON_ALNUM_RE.sub(" ", lowered).split():
        if _DIGITISH_RE.match(tok) and any(c.isdigit() for c in tok):
            tok = tok.replace("i", "1").replace("l", "1").replace("o", "0")
        tokens.append(tok)
    return " ".join(tokens)


def _ratio(na: str, nb: str) -> float:
    """difflib ratio of two already-normalized strings."""
    if na == nb:
        return 1.0
    if not na or not nb:
        return 0.0
    return difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()


def _at_least(na: str, nb: str, threshold: float) -> bool:
    """Cheap-first test for ``_ratio(na, nb) >= threshold``."""
    if na == nb:
        return True
    if not na or not nb:
        return False
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
        return False
    return sm.ratio() >= threshold


def similar(a: str, b: str) -> float:
    """Similarity in ``[0, 1]`` of two raw OCR texts (difflib ratio on normalized text)."""
    return _ratio(normalize(a), normalize(b))


_NON_DIGIT_RE = re.compile(r"\D")


_DIGIT_LOOKALIKES = {"i": "1", "l": "1", "o": "0"}


def _digits(norm: str) -> str:
    """The digits of a normalized text, in order (``"for 18 points"`` -> ``"18"``).

    A lone ``i``/``l``/``o`` word is the OCR's reading of a lone ``1``/``0``
    ("for I point").
    """
    if not any(c.isdigit() for c in norm) and not any(t in _DIGIT_LOOKALIKES for t in norm.split()):
        return ""
    return "".join(_DIGIT_LOOKALIKES.get(t) or _NON_DIGIT_RE.sub("", t) for t in norm.split())


def _numbers_agree(a: str, b: str) -> bool:
    """False only when both texts carry numbers and the numbers differ.

    A reading whose digits were garbled into letters (or clipped away) has no digits
    and still matches; "... for 68 points" and "... for 72 points" never do.
    """
    da, db = _digits(a), _digits(b)
    return da == db or not da or not db


def _match_weight(reps: Sequence[str], norm: str, threshold: float) -> float:
    """Alignment vote weight of ``norm`` against the variants ``reps`` (0 = no match).

    Texts whose numbers differ never match: combat text repeats the same sentence with
    different numbers all the time ("... for 68 points" / "... for 72 points"), and
    treating those as one row makes a scrolled-back window look like a normal scroll.
    """
    best = 0.0
    for rep in reps:
        if rep == norm:
            return WEIGHT_EXACT
        if not _numbers_agree(rep, norm):
            continue
        if best < WEIGHT_CLOSE and _at_least(rep, norm, AGREE_SIMILARITY):
            best = WEIGHT_CLOSE
        elif best < WEIGHT_LOOSE and _at_least(rep, norm, threshold):
            best = WEIGHT_LOOSE
    return best


def _alnum_count(text: str) -> int:
    return sum(1 for c in text if c.isalnum())


def _digit_count(text: str) -> int:
    return sum(1 for c in text if c.isdigit())


def is_terminated(text: str) -> bool:
    """True when ``text`` ends with terminal punctuation (a complete message).

    A closing parenthesis also counts: the only parenthesised text the game prints is
    a trailing mitigation / critical marker such as ``(Block 6)`` after the period.
    """
    stripped = text.rstrip().rstrip("-").rstrip()  # loot lines are framed: "--X loots [Y] from Z's corpse.--"
    return bool(stripped) and (stripped[-1] in TERMINAL_PUNCTUATION or stripped[-1] == ")")


# --------------------------------------------------------------------------------------
# Internal state
# --------------------------------------------------------------------------------------


@dataclass
class _Row:
    """One visual row of the window in the current frame."""

    x: int
    y: int
    h: int
    text: str
    norm: str
    fragments: int


@dataclass
class _Vote:
    """A text variant observed for a pending row, at a given quality tier."""

    text: str
    norm: str
    tier: int
    count: int = 1


@dataclass
class _PendingLine:
    """A logical row of the window that has been seen but not yet emitted (or that
    was emitted and is kept only to anchor the alignment of later frames)."""

    first_seen: float
    y: float
    votes: list[_Vote] = field(default_factory=list)
    frames_seen: int = 0
    good_frames: int = 0  #: observations at tier >= TIER_WEAK
    emitted: bool = False
    seq: int | None = None  #: position in the emitted history (set when emitted or recognised as old)
    behind: bool = False  #: recognised as an old row that newer emitted rows followed (scrolled back)
    backlog: bool = False  #: on screen in the first frame of a cold start
    estimated: bool = False  #: ``first_seen`` was spread over an out-of-sync gap
    created: float = field(init=False)  #: frame time the line was created (the staleness clock)

    def __post_init__(self) -> None:
        self.created = self.first_seen

    def observe(self, text: str, norm: str, tier: int) -> None:
        """Record one observation of this row."""
        self.frames_seen += 1
        if tier >= TIER_WEAK:
            self.good_frames += 1
        for vote in self.votes:
            if vote.tier == tier and vote.text == text:
                vote.count += 1
                return
        self.votes.append(_Vote(text, norm, tier))

    def absorb(self, other: _PendingLine) -> None:
        """Take over the observations of ``other``, an earlier sighting of this row."""
        for theirs in other.votes:
            mine = next((v for v in self.votes if v.tier == theirs.tier and v.text == theirs.text), None)
            if mine is not None:
                mine.count += theirs.count
            else:
                self.votes.append(_Vote(theirs.text, theirs.norm, theirs.tier, theirs.count))
        self.frames_seen += other.frames_seen
        self.good_frames += other.good_frames
        if other.first_seen < self.first_seen:
            self.first_seen, self.estimated = other.first_seen, other.estimated
        self.backlog = self.backlog or other.backlog

    def has_tier(self, tier: int) -> bool:
        return any(v.tier == tier for v in self.votes)

    def representative_norms(self) -> list[str]:
        """The most frequent normalized variant of each tier (for alignment matching)."""
        reps: list[str] = []
        for tier in (TIER_GOOD, TIER_WEAK, TIER_TOP):
            best: _Vote | None = None
            for v in self.votes:
                if v.tier == tier and (best is None or v.count > best.count):
                    best = v
            if best is not None and best.norm not in reps:
                reps.append(best.norm)
        return reps

    def matches(self, norm: str, threshold: float) -> bool:
        """True when ``norm`` is similar to (and has the same numbers as) a representative
        variant of this row."""
        return any(
            _numbers_agree(rep, norm) and _at_least(rep, norm, threshold) for rep in self.representative_norms()
        )

    def match_weight(self, norm: str, threshold: float) -> float:
        """Alignment vote weight of ``norm`` against this row (0 when not similar)."""
        return _match_weight(self.representative_norms(), norm, threshold)

    def _support(self, candidate: _Vote) -> float:
        """How well ``candidate`` agrees with all non-top observations (fuzzy count)."""
        pool = [v for v in self.votes if v.tier >= TIER_WEAK] or self.votes
        return sum(v.count * _ratio(candidate.norm, v.norm) for v in pool)

    def best_vote(self, min_tier: int) -> _Vote | None:
        """The variant to emit: from the best available tier at or above ``min_tier``,
        the variant with the most (fuzzy) agreeing observations; ties go to the variant
        with the most alphanumeric characters, then the most digits, then the earliest
        seen."""
        for tier in (TIER_GOOD, TIER_WEAK, TIER_TOP):
            if tier < min_tier:
                break
            cands = [v for v in self.votes if v.tier == tier]
            if not cands:
                continue
            scored = [
                (self._support(v), _alnum_count(v.text), _digit_count(v.text), -i, v)
                for i, v in enumerate(cands)
            ]
            scored.sort(key=lambda s: s[:4], reverse=True)
            return scored[0][4]
        return None

    def has_conflict(self, vote: _Vote) -> bool:
        """True when a clean reading of this row differs from ``vote`` (OCR jitter)."""
        return any(v.tier == TIER_GOOD and v.norm != vote.norm for v in self.votes)

    def agreeing_good_frames(self, vote: _Vote) -> int:
        """Number of TIER_GOOD observations that agree with ``vote``."""
        return sum(
            v.count
            for v in self.votes
            if v.tier == TIER_GOOD and _ratio(v.norm, vote.norm) >= AGREE_SIMILARITY
        )


# --------------------------------------------------------------------------------------
# Tracker
# --------------------------------------------------------------------------------------


class Tracker:
    """De-duplicates the scrolling combat window across frames (see module docstring).

    :param similarity: minimum :func:`similar` ratio for two readings to be the same row.
    :param min_frames: trustworthy, agreeing observations needed before a row is emitted.
    :param join_timeout_s: how long an unterminated first half waits for its second half.
    :param ignore_top_line: treat the first visual row as clipped/unreliable.
    :param row_pitch: vertical distance between rows in pixels; measured per frame when
        ``None``.
    :param margin_x: left text margin in pixels; the most common row ``x`` when ``None``.
    :param margin_tolerance_px: how far a row's ``x`` may deviate from the text margin
        before the reading is considered left-clipped (or junk-prefixed).
    :param stale_s: a row that is still not ready after this long stops blocking the rows
        below it (it is emitted with its best text if seen often enough, else dropped).
        Keep it larger than ``join_timeout_s``.
    """

    def __init__(
        self,
        similarity: float = 0.80,
        min_frames: int = 2,
        join_timeout_s: float = 5.0,
        ignore_top_line: bool = True,
        *,
        row_pitch: float | None = None,
        margin_x: int | None = None,
        margin_tolerance_px: int = 3,
        stale_s: float = 5.0,
    ) -> None:
        self.similarity = similarity
        self.min_frames = max(1, min_frames)
        self.join_timeout_s = join_timeout_s
        self.ignore_top_line = ignore_top_line
        self.margin_tolerance_px = margin_tolerance_px
        self.stale_s = stale_s

        self._fixed_pitch = row_pitch
        self._fixed_margin = margin_x
        self._pitch_samples: deque[float] = deque(maxlen=30)
        self._height_samples: deque[int] = deque(maxlen=120)
        self._x_counts: Counter[int] = Counter()
        self._complete_len_samples: deque[int] = deque(maxlen=200)
        self.occluded_frames: int = 0  #: frames skipped by the occlusion guard

        self.prev: list[str] = []  #: visual row texts of the last frame
        self.pending: list[_PendingLine] = []  #: logical rows, top to bottom
        #: ``(seq, normalized text)`` of the last emitted visual rows, oldest first.
        self.history: deque[tuple[int, str]] = deque(maxlen=HISTORY_ROWS)
        self._next_seq = 0
        self.replays_suppressed = 0  #: rows recognised as re-shown old content (not emitted again)
        self.held: Message | None = None  #: unterminated first half awaiting its tail
        self._held_since: float = 0.0
        self._held_parts: int = 1  #: visual lines already merged into ``held``
        self._frames: int = 0

        #: No history and no imported state yet: the first frame's rows are backlog.
        self._cold = True
        #: Rows seen in a single frame before a jump (and the rows of the jump frames),
        #: held back until a frame aligns again, oldest first.
        self._limbo: list[_PendingLine] = []
        #: The pending rows as they were before the first jump of the current run, to
        #: return to when the jump frames turn out to be a passing glitch.
        self._before_jump: list[_PendingLine] | None = None
        self._jump_frames = 0  #: consecutive frames that could not be aligned
        #: Window state: "live", "jumped", "scrolled_back" or "occluded".
        self._state = "live"
        self._state_since: float | None = None
        self._last_live: float | None = None  #: ``now`` of the last frame in sync with the chat's bottom
        self._spread_from: float | None = None  #: where the next spread of estimated times starts
        self._estimated_rows = 0  #: rows given estimated times since the window was last live
        self._state_logged_at: dict[str, float] = {}
        self._state_unlogged: Counter[str] = Counter()

    @property
    def emitted_tail(self) -> list[str]:
        """Normalized texts of the remembered emitted rows, oldest first."""
        return [norm for _seq, norm in self.history]

    @property
    def scrolled_back(self) -> bool:
        """True while the window is out of sync with the bottom of the chat.

        That is: the chat is scrolled up (the window's bottom row re-shows an old row),
        an overlay covers it, or it jumped and no frame has aligned since.  New lines
        that arrive meanwhile are not visible; they show up (with estimated times) once
        the window is back in sync, which turns this False again.
        """
        return self._state != "live"

    # ---- saved state ----------------------------------------------------------------

    def export_state(self, now: float | None = None) -> dict:
        """JSON-serialisable state for :meth:`import_state` after a restart.

        Holds the emitted-row history (seq and normalized text), the next seq, the rows
        of the last frame, the measured geometry and the ``saved`` time (``now``, or the
        wall clock when ``None``; use the same clock as :meth:`update`).  Rows that were
        never emitted are not included: if they are still on screen after the restart
        they are new to the next run.
        """
        return {
            "version": STATE_VERSION,
            "saved": time.time() if now is None else float(now),
            "next_seq": self._next_seq,
            "history": [[seq, norm] for seq, norm in self.history],
            "prev": list(self.prev),
            "geometry": {
                "pitch": self.pitch,
                "line_height": self.line_height,
                "margin": self.margin,
                "complete_len": self.complete_len,
            },
        }

    def import_state(self, state: dict, *, now: float, max_age_s: float = 600.0) -> bool:
        """Load a state saved by :meth:`export_state`; call it before the first frame.

        The state is refused (``False``, nothing changed) when it is malformed, holds no
        history, or is older than ``max_age_s`` at ``now``.  Once loaded, the first frame
        is re-synchronized with the history: rows still on screen are recognised as
        already emitted, rows that arrived while the app was down are emitted as new.
        """
        try:
            if int(state["version"]) != STATE_VERSION:
                return False
            saved = float(state["saved"])
            next_seq = int(state["next_seq"])
            history = [(int(seq), str(norm)) for seq, norm in state["history"]]
            prev = [str(text) for text in state.get("prev", [])]
            geometry = state.get("geometry") or {}
            pitch = geometry.get("pitch")
            line_height = geometry.get("line_height")
            margin = geometry.get("margin")
            complete_len = geometry.get("complete_len")
        except (KeyError, TypeError, ValueError, AttributeError):
            log.info("tracker state ignored: malformed")
            return False
        age = now - saved
        if not -5.0 <= age <= max_age_s:
            log.info("tracker state ignored: saved %.0f s ago (limit %.0f s)", age, max_age_s)
            return False
        history = history[-HISTORY_ROWS:]
        seqs = [seq for seq, _norm in history]
        if (
            not history
            or any(b <= a for a, b in zip(seqs, seqs[1:]))
            or seqs[0] < 0
            or seqs[-1] >= next_seq
            or not all(norm for _seq, norm in history)
        ):
            log.info("tracker state ignored: no usable history")
            return False
        self.history = deque(history, maxlen=HISTORY_ROWS)
        self._next_seq = next_seq
        self.prev = prev
        self.pending = []
        self._limbo = []
        self._before_jump = None
        self._jump_frames = 0
        self._cold = False
        for value, samples in ((pitch, self._pitch_samples), (line_height, self._height_samples)):
            if isinstance(value, (int, float)) and value > 0:
                samples.append(value)
        if isinstance(margin, int) and margin >= 0 and not self._x_counts:
            self._x_counts[margin] += 1
        if isinstance(complete_len, (int, float)) and complete_len > 0:
            self._complete_len_samples.append(int(complete_len))
        log.info("loaded tracker state: %d remembered rows, saved %.0f s ago", len(history), age)
        return True

    # ---- geometry -------------------------------------------------------------------

    @property
    def pitch(self) -> float:
        """Current estimate of the vertical distance between rows (pixels)."""
        if self._fixed_pitch:
            return self._fixed_pitch
        if self._pitch_samples:
            return statistics.median(self._pitch_samples)
        return DEFAULT_ROW_PITCH

    @property
    def line_height(self) -> float:
        """Current estimate of the text height of a row (pixels)."""
        if self._height_samples:
            return float(statistics.median(self._height_samples))
        return DEFAULT_LINE_HEIGHT

    @property
    def complete_len(self) -> float:
        """Typical character count of a complete (terminated) row."""
        if self._complete_len_samples:
            return float(statistics.median(self._complete_len_samples))
        return DEFAULT_COMPLETE_LEN

    def _is_fragment(self, text: str) -> bool:
        """An unterminated row much shorter than a complete row (clipped by an overlay)."""
        return not is_terminated(text) and len(text.strip()) < FRAGMENT_RATIO * self.complete_len

    @property
    def margin(self) -> int | None:
        """Left text margin: the most common row ``x`` since the tracker started.

        Readings with a clipped first glyph sit one character to the right of the
        margin and readings with junk prepended a few pixels to the left, so the mode
        over the whole run is used rather than a sliding window: during a stretch of
        bad OCR the clipped readings can outnumber the clean ones.
        """
        if self._fixed_margin is not None:
            return self._fixed_margin
        if not self._x_counts:
            return None
        return self._x_counts.most_common(1)[0][0]

    def _update_geometry(self, rows: list[_Row]) -> None:
        for row in rows:
            self._height_samples.append(row.h)
            self._x_counts[row.x] += 1
            if is_terminated(row.text) and len(row.text) >= 20:
                self._complete_len_samples.append(len(row.text))
        if self._fixed_pitch or len(rows) < 2:
            return
        h_med = self.line_height
        gaps = [b.y - a.y for a, b in zip(rows, rows[1:]) if b.y - a.y >= 0.8 * h_med]
        if not gaps:
            return
        smallest = min(gaps)
        self._pitch_samples.append(statistics.median(g for g in gaps if g <= 1.5 * smallest))

    def _group_rows(self, lines: Sequence[LineLike]) -> list[_Row]:
        """Merge OCR lines that share a vertical band into visual rows, top to bottom.

        Lines with fewer than ``MIN_ROW_ALNUM`` alphanumeric characters (a stray glyph
        at the window edge, a scrollbar) are discarded before merging so they neither
        become rows of their own nor pollute a real row.
        """
        items = sorted(
            (l for l in lines if l.text and _alnum_count(l.text) >= MIN_ROW_ALNUM),
            key=lambda l: (l.y, l.x),
        )
        for l in lines:
            if l.text and l.text.strip() and _alnum_count(l.text) < MIN_ROW_ALNUM:
                log.debug("frame %d: ignoring junk line %r", self._frames, l.text)
        groups: list[list[LineLike]] = []
        for line in items:
            if groups:
                ref = groups[-1][0]
                overlap = min(ref.y + ref.h, line.y + line.h) - max(ref.y, line.y)
                if overlap >= 0.5 * min(ref.h, line.h):
                    groups[-1].append(line)
                    continue
            groups.append([line])
        rows: list[_Row] = []
        margin = self.margin
        char_w = CHAR_W_PER_H * self.line_height
        before, after = CONTINUATION_SLACK_PX
        for group in groups:
            group.sort(key=lambda l: l.x)
            # Foreign text (another UI window drawn over the chat) shares the row band
            # but does not continue the chat text: keep only fragments that start where
            # the previous fragment ends, and drop rows that start far from the margin.
            kept: list[LineLike] = [group[0]]
            for line in group[1:]:
                prev = kept[-1]
                prev_right = prev.x + char_w * len(prev.text.strip())
                if margin is not None and abs(line.x - margin) <= self.margin_tolerance_px * 3:
                    # A second line starting at the text margin is its own row (two rows
                    # whose boxes overlap because the font is small), never a continuation.
                    rows.append(self._make_row(kept))
                    kept = [line]
                    continue
                if prev_right - before <= line.x <= prev_right + after:
                    kept.append(line)
                else:
                    log.debug("frame %d: dropping foreign fragment at x=%d: %r", self._frames, line.x, line.text)
            if margin is not None and kept[0].x > margin + FOREIGN_X_PX:
                log.debug("frame %d: dropping foreign row at x=%d: %r", self._frames, kept[0].x, kept[0].text)
                continue
            rows.append(self._make_row(kept))
        rows.sort(key=lambda r: r.y)
        return rows

    @staticmethod
    def _make_row(group: list[LineLike]) -> _Row:
        text = " ".join(l.text.strip() for l in group)
        main = max(group, key=lambda l: _alnum_count(l.text))
        return _Row(x=group[0].x, y=main.y, h=main.h, text=text, norm=normalize(text), fragments=len(group))

    def _is_occluded(self, rows: list[_Row]) -> bool:
        """True when most rows are clipped fragments (an overlay covers the window)."""
        if len(rows) < 3:
            return False
        fragments = sum(1 for r in rows if self._is_fragment(r.text))
        return fragments >= OCCLUSION_FRACTION * len(rows)

    def _tier(self, row: _Row, index: int, n_rows: int) -> int:
        """Quality tier of a reading at visual row ``index`` of ``n_rows``."""
        if index == 0 and self.ignore_top_line and (n_rows >= 2 or row.y < 0.6 * row.h):
            return TIER_TOP
        if row.fragments > 1:
            return TIER_WEAK
        margin = self.margin
        if margin is not None and abs(row.x - margin) > self.margin_tolerance_px:
            return TIER_WEAK
        h_med = self.line_height
        if row.h > 1.3 * h_med or row.h < 0.6 * h_med:
            return TIER_WEAK
        return TIER_GOOD

    # ---- public API -----------------------------------------------------------------

    def update(self, lines: Sequence[LineLike], now: float) -> list[Message]:
        """Feed one frame's OCR lines; return the messages completed by this frame."""
        self._frames += 1
        rows = self._group_rows(lines)
        if self._is_occluded(rows):
            # Leave every piece of state untouched; alignment resumes when the overlay
            # closes.  Only the join timeout keeps ticking.
            self.occluded_frames += 1
            log.debug("frame %d: occluded (%d rows mostly fragments); skipped", self._frames, len(rows))
            self._set_state("occluded", now, "%d rows mostly fragments", len(rows))
            return self._join_timeout(now)
        self._update_geometry(rows)
        completed: list[Message] = []

        if rows:
            if not self.pending:
                completed.extend(self._start(rows, now))
            else:
                dy = self._align(rows)
                if dy is None and self._before_jump is not None:
                    dy = self._recover(rows)
                if dy is None:
                    completed.extend(self._jump(rows, now))
                else:
                    completed.extend(self._aligned(rows, dy, now))
            self.prev = [r.text for r in rows]

        if not self._limbo:
            # While rows are held over a jump nothing is emitted: the rows below them
            # must not overtake them.
            completed.extend(self._emit_ready(now))

        out: list[Message] = []
        for msg in completed:
            out.extend(self._join(msg, now))
        out.extend(self._join_timeout(now))
        return out

    def flush(self, now: float) -> list[Message]:
        """Emit everything still pending (best available text) plus any held half."""
        completed = self._release_held()
        self._before_jump = None
        completed.extend(self._finalize_all())
        out: list[Message] = []
        for msg in completed:
            out.extend(self._join(msg, now))
        if self.held is not None:
            out.append(self.held)
            self.held = None
        return out

    # ---- alignment ------------------------------------------------------------------

    def _align(self, rows: list[_Row]) -> float | None:
        """Scroll distance (pixels, >= ~0) between the pending rows and ``rows``.

        Every (pending, row) pair with similar text votes for ``dy = pending.y - row.y``;
        the best-supported cluster wins (smallest ``dy`` on ties).  ``None`` means the
        frame could not be aligned (window cleared, resized or scrolled by more than a
        window).
        """
        pitch = self.pitch
        n = len(rows)
        pairs: list[tuple[float, float]] = []
        for line in self.pending:
            for j, row in enumerate(rows):
                dy = line.y - row.y
                if dy < -0.45 * pitch:
                    continue
                weight = line.match_weight(row.norm, self.similarity)
                if weight > 0.0:
                    if j == 0 and n >= 2 and self.ignore_top_line:
                        weight *= TOP_ROW_WEIGHT
                    pairs.append((dy, weight))
        if not pairs:
            return None
        best_dy: float | None = None
        best_score = 0.0
        for dy_c, _ in pairs:
            score = sum(w for dy, w in pairs if abs(dy - dy_c) <= 0.3 * pitch)
            if score > best_score or (
                score == best_score and best_dy is not None and dy_c < best_dy
            ):
                best_dy, best_score = dy_c, score
        if best_dy is None:
            return None
        # Rows of the current frame that would fall inside the previous window.
        top = min(l.y for l in self.pending) - best_dy - 0.5 * pitch
        bottom = max(l.y for l in self.pending) - best_dy + 0.5 * pitch
        overlap = sum(1 for r in rows if top <= r.y <= bottom)
        needed = max(MIN_ALIGN_SCORE, MIN_ALIGN_FRACTION * overlap)
        if best_score < needed:
            log.debug(
                "frame %d: best alignment dy=%.0f scores %.1f < %.1f needed",
                self._frames,
                best_dy,
                best_score,
                needed,
            )
            return None
        members = [dy for dy, _ in pairs if abs(dy - best_dy) <= 0.3 * pitch]
        dy = statistics.fmean(members)
        return max(dy, 0.0)

    def _apply(self, rows: list[_Row], dy: float, now: float, *, spread: bool = False) -> list[Message]:
        """Shift pending rows by ``dy``, retire the ones that scrolled off, and attach
        the current rows to pending rows (or create new pending rows).

        With ``spread`` (the window was out of sync before this frame) the new rows
        get times spread over the gap (see :meth:`_spread`).
        """
        pitch = self.pitch
        tol = 0.5 * pitch
        completed: list[Message] = []
        kept: list[_PendingLine] = []
        for line in self.pending:
            line.y -= dy
            if line.y < -0.25 * pitch:
                completed.extend(self._finalize(line, "scrolled off"))
            else:
                kept.append(line)
        self.pending = kept

        n = len(rows)
        max_y = max((l.y for l in self.pending), default=float("-inf"))
        taken: set[int] = set()
        new_lines: list[_PendingLine] = []  #: new rows under all known rows
        gap_lines: list[_PendingLine] = []  #: new rows above or between known rows
        above: _PendingLine | None = None  #: pending line of the row directly above, this frame
        for j, row in enumerate(rows):
            tier = self._tier(row, j, n)
            # Candidates: untaken pending rows within one pitch, nearest first.
            cands = sorted(
                (
                    (abs(line.y - row.y), idx)
                    for idx, line in enumerate(self.pending)
                    if idx not in taken and abs(line.y - row.y) <= pitch
                ),
                key=lambda c: c[0],
            )
            match = next(
                (idx for d, idx in cands if self.pending[idx].matches(row.norm, self.similarity)),
                None,
            )
            if match is not None:
                taken.add(match)
                line = self.pending[match]
                line.observe(row.text, row.norm, tier)
                line.y = row.y
                above = line
            elif cands and cands[0][0] <= tol:
                log.debug("frame %d: noise at y=%d: %r", self._frames, row.y, row.text)
            elif row.y > max_y + tol:
                line = _PendingLine(first_seen=now, y=row.y)
                line.observe(row.text, row.norm, tier)
                old = self._chain_replay(above, row.norm)
                if old is not None:
                    line.emitted, line.seq = True, old
                    line.behind = old < self._next_seq - 1
                    self.replays_suppressed += 1
                    log.debug("frame %d: row re-shows emitted row %d: %r", self._frames, old, row.text)
                self.pending.append(line)
                taken.add(len(self.pending) - 1)
                new_lines.append(line)
                above = line
                log.debug("frame %d: new row y=%d tier=%d: %r", self._frames, row.y, tier, row.text)
            else:
                line = _PendingLine(first_seen=now, y=row.y)
                line.observe(row.text, row.norm, tier)
                pos = next(
                    (i for i, l in enumerate(self.pending) if l.y > row.y), len(self.pending)
                )
                self.pending.insert(pos, line)
                taken = {i + 1 if i >= pos else i for i in taken}
                taken.add(pos)
                gap_lines.append(line)
                above = line
                log.debug("frame %d: gap row y=%d tier=%d: %r", self._frames, row.y, tier, row.text)
        self._check_replayed_gap(gap_lines)
        self._check_replayed_block(new_lines)
        if spread:
            self._spread([l for l in gap_lines + new_lines if not l.emitted], now)
        return completed

    # ---- replay detection -------------------------------------------------------------

    def _record(self, line: _PendingLine, norm: str) -> None:
        """Mark ``line`` emitted and append it to the history with the next seq."""
        line.emitted = True
        line.seq = self._next_seq
        self._next_seq += 1
        self.history.append((line.seq, norm))

    def _history_norm(self, seq: int) -> str | None:
        if not self.history:
            return None
        index = seq - self.history[0][0]
        if 0 <= index < len(self.history):
            return self.history[index][1]
        return None

    def _history_positions(self, norms: Sequence[str], *, top_row: bool = False) -> tuple[list[int | None], int]:
        """Line ``norms`` (top to bottom) up with the emitted history, in order.

        Every (row, remembered row) pair with similar text votes for the offset between
        them; the best offset must explain enough rows (the same rule as the frame
        alignment).  Returns the seq each row re-shows (``None`` for rows outside the
        history at that offset) and how many rows actually matched their position.
        """
        n = len(norms)
        hist = list(self.history)
        if not hist or not n:
            return [None] * n, 0
        votes: Counter[int] = Counter()
        threshold = self.similarity
        for j, norm in enumerate(norms):
            sm = difflib.SequenceMatcher(None, "", norm, autojunk=False)
            for seq, old in hist:
                if old == norm:
                    weight = WEIGHT_EXACT
                else:
                    if not _numbers_agree(old, norm):
                        continue
                    sm.set_seq1(old)
                    if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                        continue
                    ratio = sm.ratio()
                    if ratio >= AGREE_SIMILARITY:
                        weight = WEIGHT_CLOSE
                    elif ratio >= threshold:
                        weight = WEIGHT_LOOSE
                    else:
                        continue
                if top_row and j == 0 and n >= 2 and self.ignore_top_line:
                    weight *= TOP_ROW_WEIGHT
                votes[seq - j] += weight
        if not votes:
            return [None] * n, 0
        offset, score = max(votes.items(), key=lambda kv: (kv[1], kv[0]))
        first, last = hist[0][0], hist[-1][0]
        inside = [first <= j + offset <= last for j in range(n)]
        if score < max(MIN_ALIGN_SCORE, MIN_ALIGN_FRACTION * sum(inside)):
            return [None] * n, 0
        positions: list[int | None] = []
        matching = 0
        for j, norm in enumerate(norms):
            if not inside[j]:
                positions.append(None)
                continue
            positions.append(j + offset)
            old = self._history_norm(j + offset)
            if old is not None and _match_weight((old,), norm, threshold) > 0.0:
                matching += 1
        return positions, matching

    def _replay_anchor(self, above: _PendingLine | None) -> bool:
        """True when new rows directly under ``above`` can only be re-shown old content.

        In a live window new messages only ever appear under the newest emitted row.
        Under an emitted row that is several emissions old, whose successors are no
        longer in the window, the window is showing old content again (scrolled back
        down).  Under the newest emission, or under a row not emitted yet, rows are new.
        """
        if above is None or above.seq is None:
            return False
        if above.seq >= self._next_seq - 1:
            return False  # the row above is the newest emission: these are new messages
        # Rows emitted after it are still in the window: a normal scroll.
        return not any(l.seq is not None and l.seq > above.seq for l in self.pending if l is not above)

    def _chain_replay(self, above: _PendingLine | None, norm: str) -> int | None:
        """The seq a new bottom row re-shows, judged from the row directly above it.

        Under an old row (see :meth:`_replay_anchor`) the row is compared with the rows
        that followed that row originally.
        """
        if not self._replay_anchor(above):
            return None
        assert above is not None and above.seq is not None
        last = self._next_seq - 1
        digits = _digits(norm)
        for seq in range(above.seq + 1, min(last, above.seq + 3) + 1):
            old = self._history_norm(seq)
            if old is None or _digits(old) != digits:  # strict here: a replay keeps its numbers
                continue
            if _at_least(old, norm, REPLAY_CHAIN_SIMILARITY):
                return seq
        return None

    def _check_replayed_block(self, new_lines: list[_PendingLine]) -> None:
        """A burst of new rows under an old row that lines up with the rows that followed
        that row originally is old content.

        Group spells print the same block of lines on every cast ("Your Restorative Smite
        heals X for 10 Health." for each party member), so a burst that merely resembles
        an earlier one is not enough: it must sit directly under an old row (see
        :meth:`_replay_anchor`) and continue the history right after that row.  Only the
        rows that match their own history row are suppressed.
        """
        fresh = [l for l in new_lines if not l.emitted]
        if len(fresh) < REPLAY_BLOCK_ROWS:
            return
        idx = self.pending.index(fresh[0])
        above = self.pending[idx - 1] if idx > 0 else None
        if not self._replay_anchor(above):
            return
        assert above is not None and above.seq is not None
        norms = [l.representative_norms()[0] for l in fresh]
        positions, matching = self._history_positions(norms)
        if matching < REPLAY_BLOCK_MATCHES or positions[0] != above.seq + 1:
            return
        self._suppress_replayed(fresh, norms, positions)

    def _check_replayed_gap(self, gap_lines: list[_PendingLine]) -> None:
        """New rows that appear above or between known rows are old content when they
        line up with the history (the chat's faded-out top rows showing again, or a
        window that grew).

        New messages only ever arrive at the bottom, so these are checked without an
        anchor, each run of adjacent rows on its own, and every row of a run that lines
        up is old, also one whose reading differs from its history row.
        """
        fresh = [l for l in gap_lines if not l.emitted]
        if len(fresh) < REPLAY_BLOCK_MATCHES:
            return
        index = {id(l): i for i, l in enumerate(self.pending)}
        runs: list[list[_PendingLine]] = []
        for line in fresh:
            if runs and index[id(line)] == index[id(runs[-1][-1])] + 1:
                runs[-1].append(line)
            else:
                runs.append([line])
        for run in runs:
            if len(run) < REPLAY_BLOCK_MATCHES:
                continue
            norms = [l.representative_norms()[0] for l in run]
            positions, matching = self._history_positions(norms)
            if matching >= REPLAY_BLOCK_MATCHES:
                self._suppress_replayed(run, norms, positions, matching_only=False)

    def _suppress_replayed(
        self,
        lines: list[_PendingLine],
        norms: list[str],
        positions: list[int | None],
        *,
        matching_only: bool = True,
    ) -> None:
        """Mark the rows at ``positions`` as old: only the ones that match their history
        row, or (``matching_only=False``) every row inside the history."""
        newest = self._next_seq - 1
        suppressed: list[str] = []
        for line, norm, seq in zip(lines, norms, positions):
            old = self._history_norm(seq) if seq is not None else None
            if seq is None or old is None:
                continue
            if matching_only and _match_weight((old,), norm, self.similarity) == 0.0:
                continue
            line.emitted, line.seq = True, seq
            line.behind = seq < newest
            best = line.best_vote(TIER_TOP)
            suppressed.append(best.text if best is not None else norm)
        if not suppressed:
            return
        self.replays_suppressed += len(suppressed)
        log.info(
            "frame %d: %d new rows re-show old content; not emitted again: %s",
            self._frames,
            len(suppressed),
            " / ".join(repr(t) for t in suppressed[:2]) + (" / ..." if len(suppressed) > 2 else ""),
        )

    def _resync(self, rows: list[_Row], now: float) -> list[_PendingLine]:
        """Start a fresh pending list from ``rows``; return the rows that are new.

        Rows that line up with the tail of recently emitted messages (the window content
        after a spurious "jump") are marked as already emitted so they are not repeated.
        """
        n = len(rows)
        positions, _matching = self._history_positions([r.norm for r in rows], top_row=True)
        newest = self._next_seq - 1
        fresh: list[_PendingLine] = []
        for j, row in enumerate(rows):
            line = _PendingLine(first_seen=now, y=row.y)
            line.observe(row.text, row.norm, self._tier(row, j, n))
            seq = positions[j]
            if seq is not None:
                line.emitted, line.seq = True, seq
                line.behind = seq < newest
            else:
                fresh.append(line)
            self.pending.append(line)
        old = n - len(fresh)
        if old:
            log.info("frame %d: re-synchronized with %d previously emitted rows", self._frames, old)
        return fresh

    # ---- window state: first frame, jumps, scroll-back --------------------------------

    def _start(self, rows: list[_Row], now: float) -> list[Message]:
        """First frame (or first frame after :meth:`import_state`): re-sync with the history.

        On a cold start (no history) every row was already on screen: flagged as backlog.
        """
        cold = self._cold and not self.history
        fresh = self._resync(rows, now)
        self._cold = False
        if cold:
            for line in fresh:
                line.backlog = True
            log.info(
                "frame %d: first frame; %d rows already on screen are flagged as backlog", self._frames, len(fresh)
            )
        out = self._release_held()
        self._settle(now, len(rows))
        return out

    def _jump(self, rows: list[_Row], now: float) -> list[Message]:
        """The frame does not align with the previous one: hold back, re-sync, wait.

        Rows seen often enough are emitted; rows seen in a single frame are held back
        (see :meth:`_hold_back`).  The frame is re-synchronized with the history, takes
        over the held rows it shows again, and its new rows get spread times.
        """
        if self._jump_frames == 0:
            self._before_jump = self.pending
        self._jump_frames += 1
        out = self._hold_back()
        self.pending = []
        fresh = self._resync(rows, now)
        taken = self._absorb_held()
        self._spread([l for l in fresh if not l.emitted and id(l) not in taken], now)
        log.debug(
            "frame %d: window jumped (no alignment with the previous frame): %d rows, %d new, %d held back",
            self._frames,
            len(rows),
            len(fresh),
            len(self._limbo),
        )
        self._set_state(
            "jumped", now, "no alignment with the previous frame; %d unconfirmed rows held back", len(self._limbo)
        )
        if self._jump_frames >= JUMP_HOLD_MAX_FRAMES:
            out.extend(self._release_held())
            self._before_jump = None
        return out

    def _recover(self, rows: list[_Row]) -> float | None:
        """Align ``rows`` with the window as it was before the current run of jumps.

        When that works the jump frames were a passing glitch (an overlay, a blurred
        frame): the pending rows from before the jump are restored and the rows of the
        jump frames, never seen again, are dropped.
        """
        assert self._before_jump is not None
        jump_lines = self.pending
        self.pending = self._before_jump
        dy = self._align(rows)
        if dy is None:
            self.pending = jump_lines
            return None
        restored = {id(l) for l in self.pending}
        dropped = [l for l in jump_lines + self._limbo if id(l) not in restored and not l.emitted]
        for line in dropped:
            line.emitted = True
        log.debug(
            "frame %d: aligned with the window before the jump; %d rows of the jump frames dropped",
            self._frames,
            len(dropped),
        )
        self._limbo = []
        return dy

    def _aligned(self, rows: list[_Row], dy: float, now: float) -> list[Message]:
        """A frame that aligns: apply it and end any run of jumps."""
        spread = self._state in ("scrolled_back", "occluded")
        applied = self._apply(rows, dy, now, spread=spread)
        out = self._release_held()  # older than anything that scrolled off in this frame
        out.extend(applied)
        self._before_jump = None
        self._jump_frames = 0
        self._settle(now, len(rows))
        return out

    def _hold_back(self) -> list[Message]:
        """Window jumped: emit the rows seen often enough, hold back the rest.

        From the first row seen in fewer than ``min_frames`` frames on, unemitted rows go
        to ``_limbo`` (all of them when rows are held already, to keep the order).
        """
        out: list[Message] = []
        hold = bool(self._limbo)
        for line in self.pending:
            if line.emitted:
                continue
            if not hold and 0 < line.good_frames < self.min_frames:
                hold = True
            if hold:
                self._limbo.append(line)
            else:
                out.extend(self._finalize(line, "window jumped"))
        return out

    def _absorb_held(self) -> set[int]:
        """Match the held rows with the rows now in the window, in order.

        A held row that shows again is the same row: the window row takes over its
        observations and first sighting.  That includes a window row the re-sync took for
        an old one: the held row was new when it was seen, and a short window of common
        lines lines up with older history all too easily.  Returns the ``id`` of every
        window row that took over a held row.
        """
        held, window = self._limbo, self.pending
        if not held or not window:
            return set()
        reps = [w.representative_norms()[0] for w in window]
        weight = [[_match_weight(h.representative_norms(), r, self.similarity) for r in reps] for h in held]
        m, n = len(held), len(window)
        # Heaviest order-preserving matching (weighted longest common subsequence).
        best = [[0.0] * (n + 1) for _ in range(m + 1)]
        for i in range(m - 1, -1, -1):
            for j in range(n - 1, -1, -1):
                b = max(best[i + 1][j], best[i][j + 1])
                if weight[i][j] > 0.0:
                    b = max(b, weight[i][j] + best[i + 1][j + 1])
                best[i][j] = b
        pairs: list[tuple[int, int]] = []
        i = j = 0
        while i < m and j < n:
            if weight[i][j] > 0.0 and best[i][j] == weight[i][j] + best[i + 1][j + 1]:
                pairs.append((i, j))
                i += 1
                j += 1
            elif best[i + 1][j] >= best[i][j + 1]:
                i += 1
            else:
                j += 1
        # A short generic row ("damage.", "Stopped attacking.") matching on its own says
        # nothing; it counts next to another matched pair, or when it is distinctive.
        paired = set(pairs)
        taken: set[int] = set()
        matched: set[int] = set()
        for i, j in pairs:
            line = window[j]
            if (line.emitted and line.seq is None) or not (
                (i - 1, j - 1) in paired
                or (i + 1, j + 1) in paired
                or (weight[i][j] >= WEIGHT_CLOSE and _alnum_count(reps[j]) >= DISTINCT_ROW_ALNUM)
            ):
                continue
            if line.emitted:
                # Taken for an old row by the re-sync or the replay checks, but it is the
                # held row, which was new when it was seen: it was never emitted.
                line.emitted, line.seq, line.behind = False, None, False
            line.absorb(held[i])
            taken.add(id(line))
            matched.add(i)
        if matched:
            log.debug("frame %d: %d held rows are in the window again", self._frames, len(matched))
            self._limbo = [h for k, h in enumerate(held) if k not in matched]
        return taken

    def _release_held(self) -> list[Message]:
        """Emit the held rows that are not in the window (best available text)."""
        if not self._limbo:
            return []
        self._absorb_held()
        out: list[Message] = []
        for line in self._limbo:
            out.extend(self._finalize(line, "held over a jump"))
        self._limbo = []
        return out

    def _spread(self, lines: list[_PendingLine], now: float) -> None:
        """Spread the first sightings of rows revealed together over the gap.

        The rows arrived at unknown times since the window was last in sync with the
        bottom of the chat (or since the previous spread), so they get evenly spaced
        times up to ``now``, in window order, and are flagged as estimated.
        """
        if not lines:
            return
        start = self._spread_from if self._spread_from is not None else self._last_live
        if start is None or start > now:
            start = now
        n = len(lines)
        for i, line in enumerate(lines):
            line.first_seen = start + (now - start) * (i + 1) / n
            line.estimated = True
        self._spread_from = now
        self._estimated_rows += n

    def _window_behind(self, n_rows: int) -> bool:
        """True when the bottom row of the window re-shows an old row (scrolled back).

        Only a window of at least ``REPLAY_BLOCK_ROWS`` rows counts: the chat fades out
        to its last few lines when idle, and two or three common lines line up with older
        history too easily to tell.
        """
        if n_rows < REPLAY_BLOCK_ROWS:
            return False
        for line in reversed(self.pending):
            if line.emitted and line.seq is None:
                continue  # a dropped reading
            return line.behind
        return False

    def _settle(self, now: float, n_rows: int) -> None:
        """Window state after a frame of ``n_rows`` rows that aligned (or re-synced on start)."""
        if self._window_behind(n_rows):
            self._set_state("scrolled_back", now, "the window's bottom row is an old row")
            return
        if self._state != "live":
            self._set_state(
                "live",
                now,
                "back in sync after %.1f s; %d rows got estimated times",
                now - (self._state_since if self._state_since is not None else now),
                self._estimated_rows,
            )
        self._last_live = now
        self._spread_from = None
        self._estimated_rows = 0

    def _set_state(self, state: str, now: float, detail: str = "", *args: object) -> None:
        """Change the window state; log the change at INFO, at most once per
        ``STATE_LOG_INTERVAL_S`` per kind of state."""
        if state == self._state:
            return
        previous, self._state = self._state, state
        if previous == "live":
            self._state_since = now  # out of sync since
        elif state == "live":
            self._state_since = None
        msg = "frame %d: chat window %s (was %s)" + ("; " + detail if detail else "")
        margs: tuple[object, ...] = (self._frames, state.replace("_", " "), previous.replace("_", " "), *args)
        last = self._state_logged_at.get(state)
        if last is not None and 0.0 <= now - last < STATE_LOG_INTERVAL_S:
            self._state_unlogged[state] += 1
            log.debug(msg, *margs)
            return
        skipped = self._state_unlogged.pop(state, 0)
        if skipped:
            msg += " (%d more such changes not logged)"
            margs = (*margs, skipped)
        self._state_logged_at[state] = now
        log.info(msg, *margs)

    # ---- emission -------------------------------------------------------------------

    def _is_ready(self, line: _PendingLine) -> bool:
        if line.emitted or line.good_frames < self.min_frames:
            return False
        best = line.best_vote(TIER_GOOD)
        if best is None:
            return False
        if line.agreeing_good_frames(best) < self.min_frames:
            return False
        # The clean readings disagree ("Bone Ohips" / "Bone Chips"): wait until one variant
        # was read identically twice, so a one-frame misread cannot win a tie.  Bounded so
        # a row that keeps jittering does not hold up the rows below it.
        if (
            self.min_frames >= 2
            and best.count < 2
            and line.has_conflict(best)
            and line.good_frames < CONSENSUS_MAX_FRAMES
        ):
            return False
        return True

    def _can_emit_degraded(self, line: _PendingLine) -> bool:
        """A row that is not ready may still be emitted (scroll-off, flush, stale) when
        it was seen cleanly at least once, or at least ``min_frames`` times anywhere but
        the top row.  A single degraded sighting is dropped."""
        return line.has_tier(TIER_GOOD) or line.good_frames >= self.min_frames

    def _emit(self, line: _PendingLine, vote: _Vote) -> list[Message]:
        self._record(line, vote.norm)
        text = vote.text
        # A "(Block 6)"-style marker wrapped onto the row below belongs to this message.
        marker = self._marker_row_after(line)
        if marker is not None:
            mvote = marker.best_vote(TIER_TOP)
            if mvote is not None:
                text = text.rstrip() + " " + mvote.text.strip()
                self._record(marker, mvote.norm)
        log.debug(
            "emit (%d frames, first %.3f): %r", line.frames_seen, line.first_seen, text
        )
        return [
            Message(text, line.first_seen, line.frames_seen, backlog=line.backlog, estimated_ts=line.estimated)
        ]

    def _marker_row_after(self, line: _PendingLine) -> _PendingLine | None:
        """The pending row directly below ``line`` if it is a marker-only row."""
        try:
            idx = self.pending.index(line)
        except ValueError:
            return None
        if idx + 1 >= len(self.pending):
            return None
        nxt = self.pending[idx + 1]
        if nxt.emitted:
            return None
        vote = nxt.best_vote(TIER_TOP)
        if vote is None or not MARKER_ONLY_RX.match(vote.text.strip()):
            return None
        return nxt

    def _finalize(self, line: _PendingLine, reason: str) -> list[Message]:
        """Emit ``line`` with its best available text, or drop it."""
        if line.emitted:
            return []
        vote = line.best_vote(TIER_WEAK)
        if vote is None or not self._can_emit_degraded(line):
            line.emitted = True
            log.debug("dropped (%s, %d frames): %r", reason, line.frames_seen, line.votes)
            return []
        return self._emit(line, vote)

    def _finalize_all(self) -> list[Message]:
        out: list[Message] = []
        for line in self.pending:
            out.extend(self._finalize(line, "finalize"))
        return out

    def _emit_ready(self, now: float) -> list[Message]:
        """Emit ready rows in window order; resolve stale blockers."""
        out: list[Message] = []
        for idx, line in enumerate(self.pending):
            if line.emitted:
                continue
            if self._is_ready(line):
                vote = line.best_vote(TIER_GOOD)
                assert vote is not None
                out.extend(self._emit(line, vote))
                continue
            if line.good_frames == 0:
                # Only ever seen as the clipped top row: it can never become ready while
                # it stays there, so it must not hold back the rows below it.  It is
                # dropped when it scrolls off.
                continue
            stale = now - line.created >= self.stale_s
            if stale and any(self._is_ready(l) for l in self.pending[idx + 1 :]):
                best = line.best_vote(TIER_WEAK)
                log.debug(
                    "stale blocker after %.1fs (%d frames): %r",
                    now - line.first_seen,
                    line.frames_seen,
                    best.text if best else None,
                )
                out.extend(self._finalize(line, "stale"))
                continue
            break
        return out

    # ---- wrapped-line joining -------------------------------------------------------

    def _join(self, msg: Message, now: float) -> list[Message]:
        if self.held is not None:
            held, self.held = self.held, None
            parts = self._held_parts
            tail = msg.text.strip()
            if self._is_continuation(held.text, tail):
                joined = Message(
                    held.text.rstrip() + " " + tail,
                    held.first_seen,
                    min(held.frames_seen, msg.frames_seen),
                    backlog=held.backlog,
                    estimated_ts=held.estimated_ts,
                )
                if is_terminated(joined.text) or parts + 1 >= MAX_JOIN_PARTS:
                    return [joined]
                # Three-line messages (coin splits): keep accumulating until a terminator.
                self.held = joined
                self._held_parts = parts + 1
                return []
            # Not a continuation: the held half merely lost its period to OCR.  Release it
            # unjoined and process this message normally.
            held.fragment = True
            log.debug("held half released unjoined (next line is a new message): %r", held.text)
            return [held, *self._join(msg, now)]
        if is_terminated(msg.text):
            return [msg]
        if MARKER_ONLY_RX.match(msg.text.strip()):
            # Its hit line was already returned to the caller; hand it over on its own.
            msg.fragment = True
            return [msg]
        if len(msg.text.strip()) < JOIN_MIN_RATIO * self.complete_len:
            # Wrapped first halves are always near full width; a short unterminated row
            # is a clipped reading and must not swallow the next message.
            msg.fragment = True
            log.debug("fragment emitted unjoined: %r", msg.text)
            return [msg]
        self.held = msg
        self._held_since = now
        self._held_parts = 1
        return []

    def _is_continuation(self, head: str, tail: str) -> bool:
        """Decide whether visual line ``tail`` continues the unterminated ``head``.

        The game wraps at word boundaries, so a continuation is recognised by either side:
        the head stops on a connector (``of``, ``for 57``, ``but``, ``warrior's``, a damage
        type), or the tail starts in lower case without being a new NPC sentence (an article
        followed by a name).  A capitalised full-width tail, or an article-led one after a
        head that does not stop mid-phrase, is a new message whose predecessor lost its period.
        """
        if not tail:
            return False
        if MARKER_ONLY_RX.match(tail):
            return True
        head_words = head.strip().split()
        if head_words and (head_words[-1].endswith("'s") or head_words[-1].endswith("s'")):
            return True  # "a skeletal cleric's" + "Shock hits ...": an ability name follows
        if starts_message(tail):
            # "a skeletal cleric begins casting Stun." after a line that lost its end
            # ("... 3 points of Holy"): two messages, not a wrap.
            return False
        if _ends_with_connector(head):
            return True
        starts_lower = tail[:1].islower()
        starts_article = _ARTICLE_START_RX.match(tail) is not None
        if starts_lower and not starts_article:
            return True
        if starts_article and _ends_with_connector(head):
            return True
        return False

    def _join_timeout(self, now: float) -> list[Message]:
        if self.held is not None and now - self._held_since >= self.join_timeout_s:
            msg, self.held = self.held, None
            log.debug("held half timed out: %r", msg.text)
            return [msg]
        return []


# --------------------------------------------------------------------------------------
# Fixture replay (dev tool; used by ``cli.replay`` and the tests)
# --------------------------------------------------------------------------------------


@dataclass
class FixtureLine:
    """Minimal line object with the attributes the tracker needs."""

    x: int
    y: int
    h: int
    text: str


def load_fixture(path: str | Path) -> list[tuple[float, list[FixtureLine]]]:
    """Load a ``burst_ocr.json`` fixture as ``[(now_seconds, lines), ...]``."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    frames: list[tuple[float, list[FixtureLine]]] = []
    for frame in data["frames"]:
        lines = [FixtureLine(l["x"], l["y"], l["h"], l["text"]) for l in frame["lines"]]
        frames.append((frame["t_ms"] / 1000.0, lines))
    return frames


def replay_fixture(path: str | Path, **tracker_kwargs: object) -> list[Message]:
    """Feed a fixture through a :class:`Tracker` and return every emitted message."""
    tracker = Tracker(**tracker_kwargs)  # type: ignore[arg-type]
    out: list[Message] = []
    now = 0.0
    for now, lines in load_fixture(path):
        out.extend(tracker.update(lines, now))
    out.extend(tracker.flush(now))
    return out
