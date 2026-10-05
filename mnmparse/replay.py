"""Find re-read lines in an already-written log.

Two kinds of copy end up in the logs:

* **Replayed blocks** (:func:`find_replays`).  Before the tracker learned to recognise old
  content (2026-10-02 16:44), scrolling the chat back, or the window re-rendering after a
  death or a zone change, could re-emit a screenful of lines at once.  Logs written then
  contain those blocks.  A block counts as a replay only when it looks like a screen read
  in one go: a run of at least ``MIN_RUN`` consecutive lines (at least two of them
  different) that repeats an earlier run line for line, with the same numbers word for
  word; the original lies at most ``HISTORY`` lines or ``HISTORY_S`` seconds back; and the
  copy is squeezed: it spans at most ``COPY_SPAN_S`` while the original took at least
  ``SQUEEZE_S`` longer to arrive.  The game repeats itself all the time (a kill, "Stopped
  attacking.", "You gain party experience!"; a group heal printing the same amount for every
  member; a proc chain with fixed numbers), but never that fast, so those are kept.  A run
  holding a kill, loot or coin line is dropped only when those lines are exact copies.
* **The restart backlog** (:func:`find_backlog`).  Each start of the app reads the chat
  window as it is, so a new log opens with the lines the previous run already logged,
  stamped with the start time.  They are the lines at the top of the new file that repeat
  the previous file's last lines in order.

Logs written since the tracker suppresses re-shown screens live carry a format header
(:data:`mnmparse.logwriter.LOG_FORMAT`); the importer runs :func:`find_replays` only on
older ones.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from .tracker import _at_least, normalize

__all__ = [
    "find_replays",
    "find_backlog",
    "MIN_RUN",
    "BACKLOG_S",
    "RESTART_GAP_S",
    "TAIL_LINES",
]

HISTORY = 40  #: how far back (lines) an original may be ...
HISTORY_S = 60.0  #: ... or how long ago (seconds)
MIN_RUN = 6
MIN_GAP_S = 2.0
COPY_SPAN_S = 1.0  #: a re-shown screen is read in one go: the copy spans at most this ...
SQUEEZE_S = 2.0  #: ... and its original took at least this much longer to arrive
SIMILARITY = 0.85

RESTART_GAP_S = 600.0  #: a file that starts this soon after the previous one ended may open with its tail
BACKLOG_S = 2.0  #: the restart backlog: the lines stamped this soon after a file's first line
TAIL_LINES = 80  #: how many of the previous file's last lines the backlog is compared with
BACKLOG_MIN = 3  #: lines that must line up before anything is taken for backlog
_LOOKAHEAD = 3  #: a line the backlog lacks (or misread) may be skipped in the tail
_MAX_MISSES = 2  #: backlog lines in a row that match nothing before the block ends

_DIGIT_LOOKALIKES = {"i": "1", "l": "1", "o": "0"}
_NON_DIGIT_RE = re.compile(r"\D")
#: Kill, death, loot and coin lines: taken for a copy only when exact (a real one lost costs more).
_KEEP_RE = re.compile(r"\b(?:slain|loots?|coins?|split)\b")


def _numbers(norm: str) -> tuple[str, ...]:
    """The numbers of a normalized text, one per word (``"for 1 1"`` -> ``("1", "1")``).

    Kept apart, so "for 11 points" never equals "for 1 point ... (Block 1)".  A lone
    ``i``/``l``/``o`` word is the OCR's reading of a lone ``1``/``0`` ("for I point").
    """
    out = []
    for tok in norm.split():
        num = _DIGIT_LOOKALIKES.get(tok) or _NON_DIGIT_RE.sub("", tok)
        if num:
            out.append(num)
    return tuple(out)


def _same(a: str, b: str) -> bool:
    """Two normalized lines are readings of the same message."""
    if a == b:
        return True
    return _numbers(a) == _numbers(b) and _at_least(a, b, SIMILARITY)


def _span(stamps: Sequence[float]) -> float:
    return max(stamps) - min(stamps)


def find_replays(items: Sequence[tuple[float, str]]) -> set[int]:
    """Indexes of ``(ts, text)`` items that replay an earlier run of items."""
    n = len(items)
    norms = [normalize(text) for _ts, text in items]
    seen: defaultdict[str, list[int]] = defaultdict(list)
    drop: set[int] = set()
    i = 0
    while i < n:
        best: tuple[int, int] | None = None  # (length, start) of the longest valid replay
        for k in reversed(seen.get(norms[i], ())):
            if i - k > HISTORY and items[i][0] - items[k][0] > HISTORY_S:
                break
            m = 1
            while i + m < n and k + m < i and _same(norms[k + m], norms[i + m]):
                m += 1
            # A run may begin with a line misread differently than its original
            # ("fizhter" for "fighter"): extend it backwards over near-identical lines.
            b = 0
            while (
                k - b - 1 >= 0
                and i - b - 1 > k
                and (i - b - 1) not in drop
                and _same(norms[k - b - 1], norms[i - b - 1])
            ):
                b += 1
            length, start = m + b, i - b
            if length < MIN_RUN or (best is not None and length <= best[0]):
                continue
            run = range(start, start + length)
            orig = range(k - b, k - b + length)
            copy_span = _span([items[j][0] for j in run])
            if (
                len({norms[j] for j in run}) >= 2
                and copy_span <= COPY_SPAN_S
                and _span([items[j][0] for j in orig]) >= copy_span + SQUEEZE_S
                and items[start][0] - items[k - b][0] >= MIN_GAP_S
                # "slain a jackal" is not "slain a jackal pup": such lines must match exactly
                and all(norms[j] == norms[o] for j, o in zip(run, orig) if _KEEP_RE.search(norms[j]))
            ):
                best = (length, start)
        if best is not None:
            length, start = best
            for j in range(start, start + length):
                if j >= i:
                    seen[norms[j]].append(j)
                drop.add(j)
            i = start + length
            continue
        seen[norms[i]].append(i)
        i += 1
    return drop


def find_backlog(tail: Sequence[str], items: Sequence[tuple[float, str]]) -> set[int]:
    """Indexes of ``(ts, text)`` items (a new file, from its first line) that repeat ``tail``.

    ``tail`` is the previous file's last lines (up to :data:`TAIL_LINES`).  The new file's
    opening block (lines stamped within :data:`BACKLOG_S` of its first one) is lined up with
    the tail in order: a matched line repeats a tail line (same numbers word for word, the
    text fuzzy), a tail line the block lacks may be skipped, and a block line that matches
    nothing is kept (it arrived while the app was down, or the previous run never logged
    it).  Past the opening block, lines are dropped only while they go on repeating the
    tail's next lines.  Nothing is dropped unless at least :data:`BACKLOG_MIN` lines, not
    all the same text, line up and the alignment runs on to the end of the tail (the
    previous run's last lines are the newest on screen).
    """
    if not tail or not items:
        return set()
    tail_norms = [normalize(text) for text in tail[-TAIL_LINES:]]
    first = items[0][0]
    norms: list[str] = []

    def norm(j: int) -> str:
        while len(norms) <= j:
            norms.append(normalize(items[len(norms)][1]))
        return norms[j]

    def repeats(q: int, j: int) -> bool:
        # A kill, loot or coin line counts only as an exact copy (but for its final "!",
        # often read as "l"): losing a real one costs more.
        a, b = tail_norms[q], norm(j)
        return a == b or (_same(a, b) and (not _KEEP_RE.search(b) or a.rstrip("il1") == b.rstrip("il1")))

    def align(start: int, pos: int) -> tuple[set[int], int]:
        matched = {start}
        p, misses = pos + 1, 0
        for j in range(start + 1, len(items)):
            if p >= len(tail_norms):
                break  # the whole tail is accounted for: the rest is new
            in_block = items[j][0] - first <= BACKLOG_S
            hit = next((q for q in range(p, min(p + _LOOKAHEAD, len(tail_norms))) if repeats(q, j)), None)
            if hit is not None:
                matched.add(j)
                p, misses = hit + 1, 0
                continue
            misses += 1
            if not in_block or misses > _MAX_MISSES:
                break
        return matched, p

    best: set[int] = set()
    # The first rows of the opening frame may be misread: the alignment may start a little lower.
    for start in range(min(_MAX_MISSES + 1, len(items))):
        if items[start][0] - first > BACKLOG_S:
            break
        for pos in range(len(tail_norms)):
            if not repeats(pos, start):
                continue
            matched, end = align(start, pos)
            # The previous run's last lines are the newest on screen: a real backlog runs on
            # to the end of the tail.  It is also more than one line said over and over.
            if (
                len(matched) > len(best)
                and len(tail_norms) - end < _LOOKAHEAD
                and len({norms[j] for j in matched}) >= 2
            ):
                best = matched
    return best if len(best) >= BACKLOG_MIN else set()
