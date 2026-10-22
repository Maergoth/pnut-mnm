"""Read the player's anchored mini-stat panel from OCR and its original pixels.

The XP values are percentages inferred from screen pixels, never raw XP points.
This module does not mutate OCR lines or send panel text to the chat tracker.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import re
from statistics import median
import time
from typing import TYPE_CHECKING, Sequence

import cv2
import numpy as np

if TYPE_CHECKING:
    from .ocr import OcrEngine, OcrLine

_STATUS_OCR_TARGET_SCALE = 1.5
_STATUS_OCR_MAX_PIXELS = 1_000_000
_FOCUSED_OCR_SCALE = 2.0
_FOCUSED_OCR_MAX_CALLS = 2
_FOCUSED_OCR_MAX_PIXELS = 200_000
_FOCUSED_OCR_BUDGET_S = .15

Rect = tuple[int, int, int, int]
_LABEL = re.compile(r"^(HEALTH|MANA|ENDURANCE|WEIGHT)(?:\s*:?\s*(\d[\d,]*\s*/\s*\d[\d,]*))?\s*$", re.I)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_RATIO = re.compile(rf"^({_NUMBER})\s*/\s*({_NUMBER})$")
_INTEGER = re.compile(rf"^[+\-−]?{_NUMBER}$")
_STAT_NAMES = {
    "STR": "strength", "STRENGTH": "strength",
    "STA": "stamina", "STAMINA": "stamina",
    "AGI": "agility", "AGILITY": "agility",
    "DEX": "dexterity", "DEXTERITY": "dexterity",
    "INT": "intelligence", "INTELLIGENCE": "intelligence",
    "WIS": "wisdom", "WISDOM": "wisdom",
    "CHA": "charisma", "CHARISMA": "charisma",
    "AC": "armor_class",
    "COLD": "resist_cold", "CORR": "resist_corruption", "CORRUPTION": "resist_corruption",
    "DIS": "resist_disease", "DISEASE": "resist_disease",
    "ELEC": "resist_electricity", "ELECTRICITY": "resist_electricity",
    "FIRE": "resist_fire", "HOLY": "resist_holy",
    "MAG": "resist_magic", "MAGIC": "resist_magic",
    "POI": "resist_poison", "POISON": "resist_poison",
}
_STAT_LABEL = re.compile(rf"^({'|'.join(_STAT_NAMES)})\s*:?(?:\s*([+\-−]?{_NUMBER}))?\s*$", re.I)
_STAT_GROUPS = (
    ("strength", "stamina", "agility", "dexterity"),
    ("intelligence", "wisdom", "charisma"),
    ("resist_cold", "resist_corruption", "resist_disease", "resist_electricity"),
    ("resist_fire", "resist_holy", "resist_magic", "resist_poison"),
)
_STAT_FIELDS = (*dict.fromkeys(_STAT_NAMES.values()),)


@dataclass(frozen=True, slots=True)
class PlayerStatusReading:
    hp_current: int | None
    hp_max: int | None
    panel_rect: Rect
    panel_line_indices: tuple[int, ...]
    xp_percent: float | None = None
    xp_bar_rect: Rect | None = None
    xp_resolution_points: float | None = None
    weight_current: int | None = None
    weight_max: int | None = None
    mana_max: int | None = None
    strength: int | None = None
    stamina: int | None = None
    agility: int | None = None
    dexterity: int | None = None
    intelligence: int | None = None
    wisdom: int | None = None
    charisma: int | None = None
    armor_class: int | None = None
    resist_cold: int | None = None
    resist_corruption: int | None = None
    resist_disease: int | None = None
    resist_electricity: int | None = None
    resist_fire: int | None = None
    resist_holy: int | None = None
    resist_magic: int | None = None
    resist_poison: int | None = None

    @property
    def hp_percent(self) -> float | None:
        if self.hp_current is None or self.hp_max is None or self.hp_max <= 0:
            return None
        return 100.0 * self.hp_current / self.hp_max

    @property
    def overburdened(self) -> bool | None:
        if (type(self.weight_current) is not int or type(self.weight_max) is not int
                or not 0 <= self.weight_current <= 1_000_000_000
                or not 0 < self.weight_max <= 1_000_000_000):
            return None
        return self.weight_current > self.weight_max


def _ratio(text: str, *, allow_overflow: bool = False) -> tuple[int, int] | None:
    match = _RATIO.fullmatch(text.strip())
    if match is None:
        return None
    if any(len(value.replace(",", "")) > 10 for value in match.groups()):
        return None
    current, maximum = (int(value.replace(",", "")) for value in match.groups())
    if maximum <= 0 or maximum > 1_000_000_000 or current > 1_000_000_000:
        return None
    if not allow_overflow and current > maximum:
        return None
    return current, maximum


def _label_ratio(index: int, lines: Sequence[OcrLine], label: str,
                 inline: str | None) -> tuple[tuple[int, int], tuple[int, ...]] | None:
    if inline is not None:
        value = _ratio(inline, allow_overflow=label == "WEIGHT")
        return (value, (index,)) if value is not None else None
    anchor = lines[index]
    height = max(1, anchor.h)
    candidates = []
    for other_index, line in enumerate(lines):
        value = _ratio(line.text, allow_overflow=label == "WEIGHT")
        if value is None:
            continue
        dy, dx = line.y - anchor.y, line.x - anchor.x
        beside = abs(dy) <= height * 0.65 and height * 1.5 <= dx <= height * 20
        below = height * 0.45 <= dy <= height * 2.3 and abs(dx) <= height * 4
        if beside or below:
            candidates.append((value, (index, other_index)))
    return candidates[0] if len(candidates) == 1 else None


def _integer(text: str) -> int | None:
    if _INTEGER.fullmatch(text.strip()) is None:
        return None
    digits = text.strip().replace(",", "").replace("−", "-")
    if len(digits.lstrip("+-")) > 10:
        return None
    value = int(digits)
    return value if -1_000_000_000 <= value <= 1_000_000_000 else None


def _inside(line: OcrLine, rect: Rect) -> bool:
    return line.h > 0 and rect[0] <= line.x < rect[2] and rect[1] <= line.y < line.y + line.h <= rect[3]


def _stat_labels(lines: Sequence[OcrLine], rect: Rect) -> list[tuple[int, str, str | None]]:
    result = []
    for index, line in enumerate(lines):
        if _inside(line, rect) and (match := _STAT_LABEL.fullmatch(line.text.strip())) is not None:
            result.append((index, _STAT_NAMES[match[1].upper()], match[2]))
    return result


def _stat_values(lines: Sequence[OcrLine], rect: Rect) -> dict[str, int | None]:
    """Accept explicit labels and unique local numbers, never a guessed grid cell."""
    labels = _stat_labels(lines, rect)
    proposals: dict[str, list[tuple[int, int]]] = {}
    for index, field, inline in labels:
        candidates = []
        if inline is not None:
            value = _integer(inline)
            if value is not None:
                candidates.append((index, value))
        else:
            anchor = lines[index]
            height = anchor.h
            for other_index, line in enumerate(lines):
                if not _inside(line, rect) or not .5 * height <= line.h <= 1.8 * height:
                    continue
                value = _integer(line.text)
                if value is None:
                    continue
                if any(_inside(sign, rect) and sign.text.strip() in ("+", "-", "−")
                       and abs(sign.y - line.y) <= height * .45
                       and 0 < line.x - sign.x <= height * 1.5 for sign in lines):
                    # A separately OCRed sign is not permission to silently
                    # turn an uncertain negative value into a positive record.
                    continue
                dx, dy = line.x - anchor.x, line.y - anchor.y
                beside = abs(dy) <= height * .45 and height * 1.5 <= dx <= height * 10
                below = (field == "armor_class" and height * .6 <= dy <= height * 2
                         and abs(dx) <= height * 2)
                if beside or below:
                    candidates.append((other_index, value))
        # A repeated label is ambiguous even when one copy lacks a readable value.
        proposals.setdefault(field, []).extend(candidates if len(candidates) == 1 else [(-1, 0)])
    result = dict.fromkeys(_STAT_FIELDS)
    used = {}
    for field, candidates in proposals.items():
        if len(candidates) == 1 and candidates[0][0] >= 0:
            index, value = candidates[0]
            used.setdefault(index, []).append(field)
            result[field] = value
    for fields in used.values():
        if len(fields) > 1:
            for field in fields:
                result[field] = None
    return result


def _focus_rect(lines: Sequence[OcrLine], reading: PlayerStatusReading,
                group: tuple[str, ...], image: np.ndarray | None) -> Rect | None:
    """Bound a side group using its observed labels, row spacing and number column."""
    panel = reading.panel_rect
    health = [line for line in lines if _inside(line, panel)
              and (match := _LABEL.fullmatch(line.text.strip())) is not None and match[1].upper() == "HEALTH"]
    if len(health) != 1:
        return None
    anchor = health[0]
    # Attribute boxes can start above the resource panel's old padded top in
    # shared chat crops. Only coherent side groups may use that extra space.
    scope = (panel[0], max(0, min(panel[1], anchor.y - anchor.h * 10)), panel[2], panel[3])
    left_group = group in (_STAT_GROUPS[0], _STAT_GROUPS[2])
    labels = [(index, field, inline) for index, field, inline in _stat_labels(lines, scope)
              if field in group and (lines[index].x <= anchor.x - anchor.h * 2 if left_group
                                     else lines[index].x >= anchor.x + anchor.h * 4)]
    if len(labels) < 2 or len({field for _index, field, _inline in labels}) != len(labels):
        return None
    ordered = sorted(labels, key=lambda item: group.index(item[1]))
    height = float(median(lines[index].h for index, _field, _inline in ordered))
    xs = [lines[index].x for index, _field, _inline in ordered]
    if max(xs) - min(xs) > height * 1.5:
        return None
    steps = [(lines[right[0]].y - lines[left[0]].y) / (group.index(right[1]) - group.index(left[1]))
             for left, right in zip(ordered, ordered[1:])]
    step = float(median(steps))
    if not height * .75 <= step <= height * 2 or any(abs(value - step) > height * .4 for value in steps):
        return None
    top = float(median(lines[index].y - group.index(field) * step for index, field, _inline in ordered))
    # Only already matched rows establish the number column; a bare number never
    # establishes a stat's identity. OcrLine has no width, so allow bounded glyph padding.
    ends = []
    for index, _field, inline in ordered:
        anchor = lines[index]
        if inline is not None:
            ends.append(anchor.x + anchor.h * (len(anchor.text) * .8 + .75))
        else:
            for line in lines:
                if (_inside(line, scope) and _integer(line.text) is not None
                        and abs(line.y - anchor.y) <= height * .45
                        and height * 1.5 <= line.x - anchor.x <= height * 10):
                    ends.append(line.x + line.h * (len(line.text) * .8 + .75))
    if not ends:
        return None
    rect = (max(scope[0], math.floor(min(xs) - height * .75)),
            max(scope[1], math.floor(top - height)),
            min(panel[2], image.shape[1] if image is not None else panel[2], math.ceil(max(ends))),
            min(panel[3], image.shape[0] if image is not None else panel[3],
                math.ceil(top + (len(group) - 1) * step + height * 1.7)))
    return rect if rect[0] < rect[2] and rect[1] < rect[3] and rect[2] - rect[0] <= height * 14 else None


def _upper_attribute_groups(lines: Sequence[OcrLine], reading: PlayerStatusReading,
                            image: np.ndarray | None) -> PlayerStatusReading:
    """Read/exclude verified attribute boxes above the resource padding only."""
    changes = {}
    excluded = set(reading.panel_line_indices)
    for group in _STAT_GROUPS[:2]:
        rect = _focus_rect(lines, reading, group, image)
        if rect is None or rect[1] >= reading.panel_rect[1]:
            continue
        values = _stat_values(lines, rect)
        if sum(values[field] is not None for field in group) < 2:
            continue
        for field in group:
            if values[field] is not None and getattr(reading, field) is None:
                changes[field] = values[field]
        # These are verified side boxes, not a full-width extension that could
        # swallow nearby chat. Unreadable/missing labels' numbers stay private.
        excluded.update(index for index, line in enumerate(lines) if _inside(line, rect))
    if changes or excluded != set(reading.panel_line_indices):
        return replace(reading, panel_line_indices=tuple(sorted(excluded)), **changes)
    return reading


def _focus_missing_stats(ocr: OcrEngine, raw: np.ndarray, mode: str, scale: float,
                         lines: Sequence[OcrLine], reading: PlayerStatusReading) -> PlayerStatusReading:
    from .ocr import OcrLine, preprocess

    deadline = time.monotonic() + _FOCUSED_OCR_BUDGET_S
    calls = pixels = 0
    for group in _STAT_GROUPS:
        if calls >= _FOCUSED_OCR_MAX_CALLS or time.monotonic() >= deadline:
            break
        if all(getattr(reading, field) is not None for field in group):
            continue
        rect = _focus_rect(lines, reading, group, raw)
        if rect is None:
            continue
        left, top, right, bottom = rect
        cost = round((right - left) * _FOCUSED_OCR_SCALE) * round((bottom - top) * _FOCUSED_OCR_SCALE)
        if pixels + cost > _FOCUSED_OCR_MAX_PIXELS:
            continue
        calls += 1
        pixels += cost
        try:
            focused = ocr.read(preprocess(raw[top:bottom, left:right], mode, _FOCUSED_OCR_SCALE))
        except Exception:
            # An optional retry must not discard valid Health, Weight or XP.
            continue
        undo = _FOCUSED_OCR_SCALE / scale
        mapped = [OcrLine(left + round(line.x / undo), top + round(line.y / undo),
                          max(1, round(line.h / undo)), line.text) for line in focused]
        values = _stat_values(mapped, rect)
        known = [field for field in group if getattr(reading, field) is not None]
        if (len(known) < 2 or any(values[field] is not None and values[field] != getattr(reading, field)
                                  for field in known)
                or sum(values[field] == getattr(reading, field) for field in known) < 2):
            continue
        changes = {field: values[field] for field in group
                   if getattr(reading, field) is None and values[field] is not None}
        if changes:
            reading = replace(reading, **changes)
    return reading


def _image(bgr: np.ndarray | None) -> np.ndarray | None:
    if not isinstance(bgr, np.ndarray) or bgr.dtype != np.uint8 or bgr.ndim != 3:
        return None
    if bgr.shape[2] != 3 or min(bgr.shape[:2]) <= 0:
        return None
    return bgr


def _colors(bgr: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    hsv = cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2HSV)
    hue, saturation, value = cv2.split(hsv)
    # The live bar animates through orange and shaded gold. Treat the whole
    # fill as one track: restricting this to bright yellow fragments both the
    # framed-bar detector and the prefix measurement as the texture moves.
    gold = (hue >= 4) & (hue <= 42) & (saturation >= 100) & (value >= 45)
    blue = (hue >= 85) & (hue <= 130) & (saturation >= 80) & (value >= 55)
    dark = value <= 35
    return gold, blue, dark


def _valid_rect(rect: Rect, image: np.ndarray) -> bool:
    if not isinstance(rect, (tuple, list)) or len(rect) != 4 or any(type(value) is not int for value in rect):
        return False
    left, top, right, bottom = rect
    return (0 <= left < right <= image.shape[1] and 0 <= top < bottom <= image.shape[0]
            and right - left >= 40 and bottom - top >= 3
            and right - left >= (bottom - top) * 5)


def _find_bar(image: np.ndarray, panel: Rect, weight_bottom: int, height: int) -> Rect | None:
    left, _top, right, bottom = panel
    top = max(0, weight_bottom)
    bottom = min(bottom, image.shape[0], top + height * 5)
    if right <= left or bottom <= top:
        return None
    roi = image[top:bottom, left:right]
    gold, blue, dark = _colors(roi)
    gap = max(1, min(5, round(height * 0.3)))
    mask = cv2.morphologyEx((gold | blue | dark).astype(np.uint8), cv2.MORPH_CLOSE,
                            np.ones((1, gap), dtype=np.uint8))
    _count, _labels, stats, _centres = cv2.connectedComponentsWithStats(mask)
    candidates = []
    for x, y, width, bar_height, pixels in stats[1:]:
        x, y, width, bar_height, pixels = map(int, (x, y, width, bar_height, pixels))
        if (width < height * 8 or width < bar_height * 5 or not height * 0.6 <= bar_height <= height * 3
                or pixels / (width * bar_height) < 0.75 or y == 0 or y + bar_height >= roi.shape[0]):
            continue
        # A filled rectangle must have neutral rails above and below. Gold chat
        # glyphs and unbounded black background do not form this track.
        rails = roi[[y - 1, y + bar_height], x:x + width]
        rail_hsv = cv2.cvtColor(rails, cv2.COLOR_BGR2HSV)
        neutral = (rail_hsv[:, :, 1] <= 45) & (rail_hsv[:, :, 2] >= 35)
        if float(neutral.mean()) < 0.7:
            continue
        candidate = (left + x, top + y, left + x + width, top + y + bar_height)
        if _valid_rect(candidate, image):
            candidates.append(candidate)
    return candidates[0] if len(candidates) == 1 else None


def _prefix_fraction(mask: np.ndarray) -> float | None:
    """Measure a dense, left-to-right fill while bridging thin segment marks."""
    if mask.size == 0:
        return None
    profile = mask.mean(axis=0) >= 0.5
    indices = np.flatnonzero(profile)
    if not len(indices):
        return 0.0
    width = profile.size
    first, last = int(indices[0]), int(indices[-1])
    if first > max(2, round(width * 0.015)):
        return None
    gap = max(2, round(width * 0.015))
    if any(stop - start > gap for start, stop in zip(indices, indices[1:])):
        return None
    if float(mask[:, first:last + 1].mean()) < 0.7:
        return None
    return (last + 1) / width


def _read_xp(image: np.ndarray, rect: Rect) -> tuple[float, float] | None:
    left, top, right, bottom = rect
    gold, blue, dark = _colors(image[top:bottom, left:right])
    height, width = gold.shape
    blue_rows = np.flatnonzero(blue.sum(axis=1) > 0)
    if len(blue_rows):
        # MnM draws the 20%-wide refinement as a thin blue sub-bar along
        # the bottom; it is not additional XP added to the yellow fill.
        if (int(blue_rows[0]) < height * 0.55 or len(blue_rows) > max(3, height * 0.4)
                or int(blue_rows[-1]) - int(blue_rows[0]) + 1 != len(blue_rows)):
            return None
        main = gold[max(1, height // 5):max(2, int(blue_rows[0]))]
        fine = blue[blue_rows]
        gold_fraction, blue_fraction = _prefix_fraction(main), _prefix_fraction(fine)
        if gold_fraction is None or blue_fraction is None:
            return None
        coarse_estimate = gold_fraction * 100.0
        remainder = blue_fraction * 20.0
        choices = [bucket * 20.0 + remainder for bucket in range(6) if bucket * 20.0 + remainder <= 100.0]
        result = min(choices, key=lambda value: abs(value - coarse_estimate))
        if abs(result - coarse_estimate) > max(1.5, 200.0 / width):
            return None
        return result, 20.0 / width
    main = gold[max(0, height // 5):max(1, height * 4 // 5)]
    fraction = _prefix_fraction(main)
    if fraction is None:
        return None
    if fraction == 0 and float(dark.mean()) < 0.8:
        return None
    return fraction * 100.0, 100.0 / width


def read_player_status(lines: Sequence[OcrLine], bgr: np.ndarray | None = None, *,
                       xp_bar_confirmed: bool = False, xp_bar_rect: Rect | None = None) -> PlayerStatusReading | None:
    """Read one verified stat panel; rectangles use crop-relative L,T,R,B.

    The panel needs Health, matching Mana/Endurance and Weight anchors. An
    unreadable health ratio does not prevent independently valid weight or XP.
    XP is read only after the known bar is confirmed or its rectangle calibrated.
    ``panel_line_indices`` identifies spatial panel lines to omit from chat OCR.
    """
    image = _image(bgr)
    labels = []
    for index, line in enumerate(lines):
        if line.h <= 0:
            continue
        match = _LABEL.fullmatch(line.text.strip())
        if match is not None:
            name, inline = match.groups()
            name = name.upper()
            value = _label_ratio(index, lines, name, inline)
            if value is not None or name == "HEALTH":
                labels.append((index, name, value))
    panels = []
    for index, name, value in labels:
        if name != "HEALTH":
            continue
        anchor = lines[index]
        height = max(1, anchor.h)
        local = [(other_index, kind, ratio) for other_index, kind, ratio in labels
                 if kind != "HEALTH" and 0 <= lines[other_index].y - anchor.y <= height * 20
                 and abs(lines[other_index].x - anchor.x) <= height * 7]
        weights = [item for item in local if item[1] == "WEIGHT"]
        resource = [item for item in local if item[1] in ("MANA", "ENDURANCE")]
        if len(weights) != 1 or not resource:
            continue
        weight_index, _kind, weight_ratio = weights[0]
        weight_bottom = max(lines[item].y + lines[item].h for item in weight_ratio[1])
        known = [index, weight_index, *(item[0] for item in resource)]
        left = max(0, min(lines[item].x for item in known) - height * 7)
        top = max(0, anchor.y - height * 4)
        right = max(lines[item].x for item in known) + height * 17
        bottom = weight_bottom + height * 5
        if image is not None:
            right, bottom = min(right, image.shape[1]), min(bottom, image.shape[0])
        panel = (left, top, right, bottom)
        if right <= left or bottom <= top:
            continue
        panel_indices = tuple(item for item, line in enumerate(lines)
                              if left <= line.x < right and top <= line.y and line.y + line.h <= bottom)
        calibrated = image is not None and xp_bar_rect is not None and _valid_rect(xp_bar_rect, image)
        bar = xp_bar_rect if calibrated else None
        if image is not None and bar is None:
            bar = _find_bar(image, panel, weight_bottom, height)
        measured = _read_xp(image, bar) if image is not None and bar is not None and (xp_bar_confirmed or calibrated) else None
        hp = value[0] if value is not None else (None, None)
        mana = [ratio[0][1] for _index, kind, ratio in resource if kind == "MANA" and ratio is not None]
        reading = PlayerStatusReading(hp[0], hp[1], panel, panel_indices,
                                      measured[0] if measured else None, bar,
                                      measured[1] if measured else None,
                                      weight_ratio[0][0], weight_ratio[0][1],
                                      mana[0] if len(mana) == 1 else None,
                                      **_stat_values(lines, panel))
        panels.append(_upper_attribute_groups(lines, reading, image))
    return panels[0] if len(panels) == 1 else None


def read_status_crop(ocr: OcrEngine, raw: np.ndarray, mode: str, scale: float
                     ) -> tuple[PlayerStatusReading | None, list[OcrLine]]:
    """Read a separate mini-stat crop, first at least 1.5x enlarged.

    The caller's OCR engine already undoes ``scale``, so coordinates are divided
    by only the additional enlargement here. XP always uses original color
    pixels. Accidentally large regions are not additionally enlarged. Missing
    side-group labels may use at most two small 2x passes within a shared pixel
    and between-call time budget. Like the initial synchronous OCR call, an
    individual engine read runs to completion. Returned lines remain the first
    pass's rows so exclusion indices and chat processing never see retry rows.
    """
    from .ocr import OcrLine, preprocess

    effective = max(scale, _STATUS_OCR_TARGET_SCALE)
    if scale < effective and raw.shape[0] * raw.shape[1] * effective ** 2 > _STATUS_OCR_MAX_PIXELS:
        effective = scale
    lines = ocr.read(preprocess(raw, mode, effective))
    extra = effective / scale
    if extra > 1.0:
        lines = [OcrLine(round(line.x / extra), round(line.y / extra),
                         round(line.h / extra), line.text) for line in lines]
    reading = read_player_status(lines, raw, xp_bar_confirmed=True)
    if reading is not None:
        reading = _focus_missing_stats(ocr, raw, mode, scale, lines, reading)
    return reading, lines


__all__ = ["PlayerStatusReading", "read_player_status", "read_status_crop"]
