"""Render ``tests/fixtures/ocr_sample.png``: a synthetic stand-in for a crop of the game's
Combat chat window (680x530), used by ``tests/test_ocr.py``.

Real captures of the window are not published because they show other players' names, so
this draws the same kind of picture: chat lines in the game's colours, in a serif font, on a
translucent brown panel over a busy background, including wrapped lines.

    .venv\\Scripts\\python tests\\fixtures\\make_ocr_sample.py [OUT.png]
"""

from __future__ import annotations

import sys
from pathlib import Path

HEALER = "Tamsin"
TANK = "Brannoc"
ROGUE = "Wenna"

#: (text, colour) top to bottom; long lines wrap like the game's chat does
LINES = [
    (f"{HEALER}'s Righteous Strike hits a stumbling zombie for 51 points of Holy Damage.", "#9a9a9a"),
    (f"{HEALER}'s devotion is rewarded!", "#9a9a9a"),
    (f"{HEALER}'s Righteous Strike hits a stumbling zombie for 170 points of Holy Damage.", "#9a9a9a"),
    (f"Your party member {HEALER} has slain a stumbling zombie!", "#c81e1e"),
    ("Stopped attacking.", "#e02020"),
    ("The warmth of the campfire leaves you.", "#2e8b2e"),
    ("The warmth of the campfire leaves you.", "#2e8b2e"),
    (f"{HEALER} begins casting Lesser Heal.", "#6f6af0"),
    (f"{HEALER}'s Lesser Heal heals {TANK} for 27 Health.", "#28a8e8"),
    (f"{ROGUE} begins to sneak.", "#40e080"),
]

EXPECTED = (
    "Stopped attacking.",
    f"{HEALER} begins casting Lesser Heal.",
    f"Your party member {HEALER} has slain a stumbling zombie!",
)


def render(path: Path) -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPen
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])  # noqa: F841 - fonts need it
    w, h = 680, 530
    img = QImage(w, h, QImage.Format.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    # the world behind the chat: warm, uneven
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0.0, QColor("#4a3a30"))
    g.setColorAt(0.55, QColor("#6b5242"))
    g.setColorAt(1.0, QColor("#3a2c24"))
    p.fillRect(0, 0, w, h, g)
    p.fillRect(QRectF(w * 0.7, 0, w * 0.3, h * 0.35), QColor(200, 170, 140, 90))
    # the chat panel and its frame
    p.fillRect(QRectF(6, 6, w - 12, h - 12), QColor(40, 30, 26, 150))
    p.setPen(QPen(QColor("#2a2a2a"), 6))
    p.drawRect(QRectF(3, 3, w - 6, h - 6))
    font = QFont("Cambria")
    font.setPixelSize(21)
    p.setFont(font)
    fm = QFontMetricsF(font)
    left, right, y, step = 22, w - 30, 16.0, 37.0
    for text, colour in LINES:
        p.setPen(QColor(colour))
        words, line = text.split(), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if fm.horizontalAdvance(trial) > right - left and line:
                p.drawText(QRectF(left, y, right - left, step), int(Qt.AlignmentFlag.AlignLeft), line)
                y += step
                line = word
            else:
                line = trial
        p.drawText(QRectF(left, y, right - left, step), int(Qt.AlignmentFlag.AlignLeft), line)
        y += step + 2
    p.end()
    img.save(str(path))


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("ocr_sample.png")
    render(out)
    print(f"wrote {out}")
