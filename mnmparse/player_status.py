"""Read the player's anchored mini-stat panel from OCR and its original pixels.

The XP values are percentages inferred from screen pixels, never raw XP points.
This module does not mutate OCR lines or send panel text to the chat tracker.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Sequence

import cv2
import numpy as np

if TYPE_CHECKING:
    from .ocr import OcrEngine, OcrLine

_STATUS_OCR_TARGET_SCALE = 1.5
_STATUS_OCR_MAX_PIXELS = 1_000_000

Rect = tuple[int, int, int, int]
_LABEL = re.compile(r"^(HEALTH|MANA|ENDURANCE|WEIGHT)(?:\s*:?\s*(\d[\d,]*\s*/\s*\d[\d,]*))?\s*$", re.I)
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
_RATIO = re.compile(rf"^({_NUMBER})\s*/\s*({_NUMBER})$")


@dataclass(frozen=True, slots=True)
class PlayerStatusReading:
    hp_current: int | None
    hp_max: int | None
    panel_rect: Rect
    panel_line_indices: tuple[int, ...]
    xp_percent: float | None = None
    xp_bar_rect: Rect | None = None
    xp_resolution_points: float | None = None

    @property
    def hp_percent(self) -> float | None:
        if self.hp_current is None or self.hp_max is None or self.hp_max <= 0:
            return None
        return 100.0 * self.hp_current / self.hp_max


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
    unreadable health ratio does not prevent an independently valid XP reading.
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
        if value is None and measured is None:
            continue
        hp = value[0] if value is not None else (None, None)
        panels.append(PlayerStatusReading(hp[0], hp[1], panel, panel_indices,
                                         measured[0] if measured else None, bar,
                                         measured[1] if measured else None))
    return panels[0] if len(panels) == 1 else None


def read_status_crop(ocr: OcrEngine, raw: np.ndarray, mode: str, scale: float
                     ) -> tuple[PlayerStatusReading | None, list[OcrLine]]:
    """Read a separate mini-stat crop in one OCR pass at least 1.5x enlarged.

    The caller's OCR engine already undoes ``scale``, so coordinates are divided
    by only the additional enlargement here. XP always uses original color
    pixels. Accidentally large regions are not additionally enlarged.
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
    return read_player_status(lines, raw, xp_bar_confirmed=True), lines


__all__ = ["PlayerStatusReading", "read_player_status", "read_status_crop"]
