"""Design tokens, palette, fonts and style sheets for the PNUT M&M desktop app.

Everything visual that more than one module needs lives here (APP_SPEC section 4):

* the colour tokens (``BG0`` ... ``PET`` and the party palette),
* :func:`actor_color` (stable per-name colours for the meter rows),
* :data:`KIND_COLORS` and :func:`feed_color` (message-feed colours by event kind),
* font helpers (:func:`app_font`, :func:`tabular_font`),
* :func:`apply_theme` (Fusion style + QPalette + the global style sheet) and
  :func:`overlay_qss` (the style sheet for the translucent overlay).

Colours are plain ``#rrggbb`` strings so that the pure-Python model code can carry them
without importing Qt; :func:`qcolor` converts them when painting.
"""

from __future__ import annotations

import logging
import zlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PySide6.QtGui import QColor, QFont
    from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokens (exact values from APP_SPEC section 4)
# ---------------------------------------------------------------------------

BG0 = "#0f1117"
"""Window background (darkest)."""
BG1 = "#161a23"
"""Panels and input fields."""
BG2 = "#1e2430"
"""Raised surfaces: buttons, menus, chips."""
LINE = "rgba(255,255,255,0.08)"
"""1 px hairline borders."""
TEXT = "#e8e6e3"
MUTED = "#9aa3b2"
ACCENT = "#f0b35b"
"""Gold, echoing the game's UI trim."""
ACCENT2 = "#5bc0eb"
DANGER = "#e5484d"
SUCCESS = "#46c37b"
YOU = "#ffd166"
NPC = "#d9655b"
PET = "#c084fc"

PARTY: tuple[str, ...] = (
    "#5bc0eb",
    "#7ee787",
    "#ff9f68",
    "#c792ea",
    "#f78fb3",
    "#4dd0e1",
    "#ffd54f",
    "#a5d6a7",
)
"""Colours for other players, chosen by a stable hash of the name."""

FONT_FAMILY = "Segoe UI Variable Display"
FONT_FALLBACK = "Segoe UI"
FONT_FAMILIES: tuple[str, ...] = (FONT_FAMILY, FONT_FALLBACK)

OVERLAY_FONT_PX = 13
"""Overlay base font size in logical pixels (multiplied by the user's font scale)."""
MAIN_FONT_PX = 14
"""Main window base font size in logical pixels."""

RADIUS_PANEL = 12
RADIUS_CHIP = 8
BAR_ALPHA_LEFT = 0.45
"""Meter bar fill alpha at the left edge."""
BAR_ALPHA_RIGHT = 0.20
"""Meter bar fill alpha at the right edge."""
HOVER_LIGHTEN = 0.06
"""Rows lighten by this much (0..1 white overlay) on hover."""

STATUS_COLORS: dict[str, str] = {
    "ok": SUCCESS,
    "warn": ACCENT,
    "bad": DANGER,
    "idle": MUTED,
}
"""Colours for ``StatusChip.set_state`` states."""

KIND_COLORS: dict[str, str] = {
    "melee_hit": TEXT,  # other players' hits; the player's own hits use YOU (see feed_color)
    "ability_hit": TEXT,
    "ability_partial": MUTED,
    "damage_effect": TEXT,
    "env_damage": DANGER,
    "melee_miss": MUTED,
    "ability_miss": MUTED,
    "resist": MUTED,
    "fizzle": MUTED,
    "heal": SUCCESS,
    "kill": ACCENT,
    "cast": ACCENT2,
    "interrupt": ACCENT2,
    "status": "#b8c0cc",
    "cannot_attack": "#d08770",
    "experience": "#c3e88d",
    "loot": ACCENT,
    "coin": "#e0c060",
    "coin_split": "#e0c060",  # the wrapped end of a coin line: the same colour as its start
    "reward": ACCENT,
    "vendor": "#c8b88a",
    "craft": "#a5d6a7",
    "cc": "#c792ea",
    "cc_fade": "#8f80ad",
    "personal": "#7a8290",
    "consider": "#8fa3bf",
    "zone": "#7fc8c8",
    "debuff": "#b48ead",
    "aggro": "#e0a070",
    "awaken": "#9fb3c8",
    "chat": "#a8b4c4",
    "level_up": "#c3e88d",
    "unknown": "#6b7280",
    "marker": "#6b7280",
}
"""Base feed colour per ``Event.kind`` (every kind in ``grammar.KINDS`` has an entry)."""

DAMAGE_KINDS: frozenset[str] = frozenset({"melee_hit", "ability_hit"})


# ---------------------------------------------------------------------------
# Colour helpers (no Qt needed)
# ---------------------------------------------------------------------------


def _parse_hex(color: str) -> tuple[int, int, int]:
    """``"#rrggbb"`` -> ``(r, g, b)``.

    Raises:
        ValueError: the string is not a 6-digit hex colour.
    """
    text = color.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #rrggbb, got {color!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def rgba(color: str, alpha: float) -> str:
    """Return a CSS ``rgba(r,g,b,a)`` string for a hex colour at ``alpha`` (0..1)."""
    r, g, b = _parse_hex(color)
    a = min(1.0, max(0.0, float(alpha)))
    return f"rgba({r},{g},{b},{a:.3f})"


def mix(color: str, other: str, amount: float) -> str:
    """Blend ``color`` towards ``other`` by ``amount`` (0 = color, 1 = other); returns hex."""
    r1, g1, b1 = _parse_hex(color)
    r2, g2, b2 = _parse_hex(other)
    t = min(1.0, max(0.0, float(amount)))
    r = round(r1 + (r2 - r1) * t)
    g = round(g1 + (g2 - g1) * t)
    b = round(b1 + (b2 - b1) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def lighten(color: str, amount: float = HOVER_LIGHTEN) -> str:
    """Lighten a hex colour by overlaying white at ``amount`` (the hover effect)."""
    return mix(color, "#ffffff", amount)


def relative_luminance(color: str) -> float:
    """WCAG relative luminance (0 = black, 1 = white) of a hex colour."""

    def channel(v: int) -> float:
        c = v / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = _parse_hex(color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours (1 .. 21)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _name_hash(name: str) -> int:
    """Stable (process-independent) hash of an actor name, case-insensitive."""
    return zlib.crc32(name.strip().lower().encode("utf-8")) & 0xFFFFFFFF


def actor_color(name: str, *, is_you: bool = False, is_npc: bool = False, is_pet: bool = False) -> str:
    """Return the hex colour for an actor row.

    Precedence: the player (``YOU``), then pets (``PET``), then NPCs (``NPC``); every other
    name gets a party colour chosen by a stable hash so the same player always gets the
    same colour across encounters and restarts.
    """
    if is_you:
        return YOU
    if is_pet:
        return PET
    if is_npc:
        return NPC
    return PARTY[_name_hash(name) % len(PARTY)]


def feed_color(kind: str, *, is_player_action: bool = False, is_player_target: bool = False) -> str:
    """Return the feed colour for a message of ``kind``.

    Damage dealt by the player is ``YOU``; damage dealt *to* the player is ``DANGER``;
    everything else comes from :data:`KIND_COLORS` (unknown kinds use the ``unknown`` colour).
    """
    if kind in DAMAGE_KINDS:
        if is_player_target and not is_player_action:
            return DANGER
        if is_player_action:
            return YOU
    return KIND_COLORS.get(kind, KIND_COLORS["unknown"])


def status_color(state: str) -> str:
    """Colour for a status chip state (``ok``/``warn``/``bad``/``idle``)."""
    return STATUS_COLORS.get(state, MUTED)


# ---------------------------------------------------------------------------
# Qt helpers
# ---------------------------------------------------------------------------


def qcolor(color: str, alpha: float = 1.0) -> QColor:
    """Build a ``QColor`` from a hex token and an alpha in 0..1."""
    from PySide6.QtGui import QColor

    r, g, b = _parse_hex(color)
    return QColor(r, g, b, round(min(1.0, max(0.0, alpha)) * 255))


def tabular_font(font: QFont) -> QFont:
    """Enable tabular (fixed-width) digits on ``font`` when the Qt build supports it.

    Uses ``QFont.setFeature("tnum", 1)`` (Qt 6.7+); on older builds the font is returned
    unchanged.  Numbers in the meter columns line up either way because they are
    right-aligned, tabular digits just stop them from jittering while they change.
    """
    from PySide6.QtGui import QFont

    if not hasattr(font, "setFeature") or not hasattr(QFont, "Tag"):
        return font
    try:
        font.setFeature(QFont.Tag("tnum"), 1)
    except Exception as exc:  # noqa: BLE001 - depends on the exact Qt build
        log.debug("tabular digits unavailable: %s", exc)
    return font


def app_font(size_px: float = MAIN_FONT_PX, weight: int | None = None, *, tabular: bool = False) -> QFont:
    """Return the app font at ``size_px`` logical pixels.

    Args:
        size_px: Pixel size (Qt scales it for the screen's DPR).
        weight: A ``QFont.Weight`` value (``QFont.Weight.Normal`` when ``None``).
        tabular: Also enable tabular digits (see :func:`tabular_font`).
    """
    from PySide6.QtGui import QFont

    font = QFont()
    font.setFamilies(list(FONT_FAMILIES))
    font.setPixelSize(max(1, round(size_px)))
    font.setWeight(QFont.Weight.Normal if weight is None else QFont.Weight(weight))
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return tabular_font(font) if tabular else font


def number_font(size_px: float = MAIN_FONT_PX, weight: int | None = None) -> QFont:
    """Shortcut for :func:`app_font` with tabular digits enabled (meter numbers)."""
    return app_font(size_px, weight, tabular=True)


def _build_palette() -> object:
    """The QPalette matching the tokens (Fusion reads it for everything QSS leaves alone)."""
    from PySide6.QtGui import QPalette

    pal = QPalette()
    cg = QPalette.ColorGroup
    role = QPalette.ColorRole
    base = {
        role.Window: qcolor(BG0),
        role.WindowText: qcolor(TEXT),
        role.Base: qcolor(BG1),
        role.AlternateBase: qcolor(BG2),
        role.Text: qcolor(TEXT),
        role.Button: qcolor(BG2),
        role.ButtonText: qcolor(TEXT),
        role.BrightText: qcolor("#ffffff"),
        role.Highlight: qcolor(ACCENT, 0.35),
        role.HighlightedText: qcolor(TEXT),
        role.ToolTipBase: qcolor(BG2),
        role.ToolTipText: qcolor(TEXT),
        role.PlaceholderText: qcolor(MUTED),
        role.Link: qcolor(ACCENT2),
        role.LinkVisited: qcolor(ACCENT2),
        role.Light: qcolor(lighten(BG2, 0.12)),
        role.Midlight: qcolor(lighten(BG2, 0.06)),
        role.Mid: qcolor(BG1),
        role.Dark: qcolor(BG0),
        role.Shadow: qcolor("#000000"),
    }
    for r, c in base.items():
        pal.setColor(r, c)
    for r in (role.WindowText, role.Text, role.ButtonText):
        pal.setColor(cg.Disabled, r, qcolor(MUTED, 0.7))
    pal.setColor(cg.Disabled, role.Button, qcolor(BG1))
    pal.setColor(cg.Disabled, role.Base, qcolor(BG1))
    pal.setColor(cg.Disabled, role.Highlight, qcolor(BG2))
    return pal


def _indicator_pixmap_path(name: str) -> str:
    """Render a small indicator (check mark, down arrow) to a PNG and return its path.

    QSS cannot embed images, so the few indicators that Fusion cannot draw on a styled
    control are rasterised into ``<project>/assets/ui/`` (next to the generated icon, so
    everything the app writes stays under the project folder) at every launch, so a
    theme change never leaves stale images behind.  Returns ``""`` on failure (the style
    sheet then simply omits the ``image:`` rule), never raising.
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainter, QPen, QPixmap, QPolygonF

    from mnmparse.config import project_path

    directory = project_path("assets") / "ui"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.debug("cannot create %s: %s", directory, exc)
        return ""
    path = (directory / f"{name}.png").as_posix()
    size = 32  # drawn at 2x and scaled down by Qt -> crisp on DPR 1.5/2 screens
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if name == "check":
            pen = QPen(qcolor("#1a1406"))
            pen.setWidthF(4.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([QPointF(8, 17), QPointF(14, 23), QPointF(24, 10)]))
        elif name == "arrow_down":
            pen = QPen(qcolor(MUTED))
            pen.setWidthF(3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([QPointF(9, 13), QPointF(16, 20), QPointF(23, 13)]))
        elif name == "arrow_up":
            pen = QPen(qcolor(MUTED))
            pen.setWidthF(3.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPolyline(QPolygonF([QPointF(9, 19), QPointF(16, 12), QPointF(23, 19)]))
    finally:
        painter.end()
    if not pm.save(path, "PNG"):
        log.debug("could not write indicator image %s", path)
        return ""
    return path


def _image_rule(path: str) -> str:
    """``image: url("...")`` rule for a rasterised indicator, or ``""`` when unavailable.

    The path is quoted: the project folder may contain characters such as ``&`` that
    break an unquoted QSS ``url()``.
    """
    return f'image: url("{path}");' if path else ""


def main_qss() -> str:
    """The global style sheet for the main window and dialogs (dark glass)."""
    check = _indicator_pixmap_path("check")
    down = _indicator_pixmap_path("arrow_down")
    up = _indicator_pixmap_path("arrow_up")
    families = ", ".join(f'"{f}"' for f in FONT_FAMILIES)
    return f"""
    QMainWindow, QDialog {{ background: {BG0}; }}
    QWidget {{
        color: {TEXT};
        font-family: {families};
        font-size: {MAIN_FONT_PX}px;
        selection-background-color: {rgba(ACCENT, 0.35)};
        selection-color: {TEXT};
    }}
    QWidget:disabled {{ color: {rgba(MUTED, 0.7)}; }}
    QFrame[class="panel"], QWidget[class="panel"] {{
        background: {BG1};
        border: 1px solid {LINE};
        border-radius: {RADIUS_PANEL}px;
    }}
    QFrame[class="raised"] {{
        background: {BG2};
        border: 1px solid {LINE};
        border-radius: {RADIUS_PANEL}px;
    }}
    QLabel {{ background: transparent; border: none; }}
    QLabel[class="muted"] {{ color: {MUTED}; }}
    QLabel[class="title"] {{ font-size: 22px; font-weight: 600; }}
    QLabel[class="h2"] {{ font-size: 16px; font-weight: 600; }}
    QLabel[class="accent"] {{ color: {ACCENT}; font-weight: 600; }}
    QLabel[class="link"] {{ color: {ACCENT2}; text-decoration: underline; }}
    QLabel[class="error"] {{ color: {DANGER}; }}

    QPushButton {{
        background: {BG2};
        color: {TEXT};
        border: 1px solid {LINE};
        border-radius: {RADIUS_CHIP}px;
        padding: 6px 14px;
        min-height: 18px;
    }}
    QPushButton:hover {{ background: {lighten(BG2, 0.06)}; border-color: rgba(255,255,255,0.14); }}
    QPushButton:pressed {{ background: {BG1}; }}
    QPushButton:disabled {{ color: {rgba(MUTED, 0.6)}; background: {BG1}; }}
    QPushButton:checked {{ background: {rgba(ACCENT, 0.18)}; border-color: {rgba(ACCENT, 0.5)}; }}
    QPushButton[class="primary"] {{
        background: {ACCENT};
        color: #1a1406;
        border: 1px solid {ACCENT};
        font-weight: 600;
        padding: 6px 18px;
    }}
    QPushButton[class="primary"]:hover {{ background: {lighten(ACCENT, 0.12)}; }}
    QPushButton[class="primary"]:pressed {{ background: {mix(ACCENT, BG0, 0.2)}; }}
    QPushButton[class="primary"]:disabled {{ background: {mix(ACCENT, BG0, 0.6)}; color: {rgba("#1a1406", 0.6)}; }}
    QPushButton[class="danger"] {{
        background: {rgba(DANGER, 0.15)};
        color: {DANGER};
        border: 1px solid {rgba(DANGER, 0.45)};
    }}
    QPushButton[class="danger"]:hover {{ background: {rgba(DANGER, 0.25)}; }}
    QPushButton[class="flat"], QToolButton {{
        background: transparent;
        border: 1px solid transparent;
        border-radius: {RADIUS_CHIP}px;
        padding: 4px 8px;
        color: {TEXT};
    }}
    QToolButton:hover, QPushButton[class="flat"]:hover {{ background: rgba(255,255,255,0.06); }}
    QToolButton:pressed {{ background: rgba(255,255,255,0.10); }}
    QToolButton:checked {{ background: {rgba(ACCENT, 0.16)}; color: {ACCENT}; }}
    QToolButton::menu-indicator {{ image: none; }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
        background: {BG1};
        color: {TEXT};
        border: 1px solid {LINE};
        border-radius: {RADIUS_CHIP}px;
        padding: 5px 8px;
        selection-background-color: {rgba(ACCENT, 0.45)};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QTextEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {rgba(ACCENT, 0.7)}; }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{
        color: {rgba(MUTED, 0.6)};
    }}
    QLineEdit[class="error"] {{ border: 1px solid {rgba(DANGER, 0.8)}; }}
    QComboBox {{ padding-right: 26px; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 24px;
        border: none;
        border-left: 1px solid {LINE};
        border-top-right-radius: {RADIUS_CHIP}px;
        border-bottom-right-radius: {RADIUS_CHIP}px;
    }}
    QComboBox::down-arrow {{ {_image_rule(down)} width: 14px; height: 14px; }}
    QComboBox QAbstractItemView {{
        background: {BG2};
        color: {TEXT};
        border: 1px solid {LINE};
        border-radius: {RADIUS_CHIP}px;
        padding: 4px;
        selection-background-color: {rgba(ACCENT, 0.25)};
        selection-color: {TEXT};
        outline: 0;
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{
        subcontrol-origin: border; subcontrol-position: top right;
        width: 20px; border: none; border-left: 1px solid {LINE};
        border-top-right-radius: {RADIUS_CHIP}px;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: 20px; border: none; border-left: 1px solid {LINE};
        border-bottom-right-radius: {RADIUS_CHIP}px;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: rgba(255,255,255,0.06); }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ {_image_rule(up)} width: 10px; height: 10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ {_image_rule(down)} width: 10px; height: 10px; }}

    QSlider {{ background: transparent; min-height: 22px; }}
    QSlider::groove:horizontal {{ height: 4px; background: {BG2}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
    QSlider::add-page:horizontal {{ background: {BG2}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        width: 14px; height: 14px; margin: -5px 0;
        border-radius: 7px; background: {TEXT}; border: 1px solid rgba(0,0,0,0.4);
    }}
    QSlider::handle:horizontal:hover {{ background: #ffffff; }}
    QSlider::groove:vertical {{ width: 4px; background: {BG2}; border-radius: 2px; }}
    QSlider::handle:vertical {{
        width: 14px; height: 14px; margin: 0 -5px;
        border-radius: 7px; background: {TEXT}; border: 1px solid rgba(0,0,0,0.4);
    }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: rgba(255,255,255,0.16); border-radius: 3px; min-height: 24px;
    }}
    QScrollBar::handle:vertical:hover {{ background: rgba(255,255,255,0.28); }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: none; border: none; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{
        background: rgba(255,255,255,0.16); border-radius: 3px; min-width: 24px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: rgba(255,255,255,0.28); }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; background: none; border: none; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

    QAbstractScrollArea {{ background: transparent; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QTableView, QTreeView {{
        background: transparent;
        alternate-background-color: transparent;
        gridline-color: transparent;
        border: none;
        selection-background-color: rgba(255,255,255,0.06);
        selection-color: {TEXT};
        outline: 0;
    }}
    QTableView::item, QTreeView::item {{ padding: 2px 6px; border: none; }}
    QTableView::item:selected, QTreeView::item:selected {{ background: rgba(255,255,255,0.06); color: {TEXT}; }}
    QTableView::item:hover, QTreeView::item:hover {{ background: rgba(255,255,255,0.06); }}
    QTableCornerButton::section {{ background: transparent; border: none; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{
        background: transparent;
        color: {MUTED};
        border: none;
        border-bottom: 1px solid {LINE};
        padding: 4px 6px;
        font-size: 12px;
        font-weight: 600;
    }}
    QHeaderView::section:hover {{ color: {TEXT}; }}
    QHeaderView::down-arrow {{ {_image_rule(down)} width: 10px; height: 10px; subcontrol-position: center right; }}
    QHeaderView::up-arrow {{ {_image_rule(up)} width: 10px; height: 10px; subcontrol-position: center right; }}

    QListView, QListWidget {{
        background: {BG1};
        border: 1px solid {LINE};
        border-radius: {RADIUS_PANEL}px;
        padding: 4px;
        outline: 0;
    }}
    QListView::item, QListWidget::item {{ padding: 6px 8px; border-radius: {RADIUS_CHIP}px; }}
    QListView::item:hover, QListWidget::item:hover {{ background: rgba(255,255,255,0.06); }}
    QListView::item:selected, QListWidget::item:selected {{ background: {rgba(ACCENT, 0.18)}; color: {TEXT}; }}

    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar {{ background: transparent; }}
    QTabBar::tab {{
        background: transparent;
        color: {MUTED};
        padding: 6px 14px;
        margin-right: 2px;
        border: none;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:hover {{ color: {TEXT}; }}
    QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}

    QToolTip {{
        background: {BG2};
        color: {TEXT};
        border: 1px solid rgba(255,255,255,0.14);
        padding: 4px 8px;
    }}
    QMenu {{
        background: {BG2};
        color: {TEXT};
        border: 1px solid rgba(255,255,255,0.12);
        padding: 6px;
    }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 6px; }}
    QMenu::item:selected {{ background: rgba(255,255,255,0.08); }}
    QMenu::item:disabled {{ color: {rgba(MUTED, 0.6)}; }}
    QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}
    QMenu::indicator {{ width: 14px; height: 14px; margin-left: 6px; }}

    QCheckBox, QRadioButton {{ spacing: 8px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1px solid rgba(255,255,255,0.25);
        background: {BG1};
    }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {rgba(ACCENT, 0.6)}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; {_image_rule(check)} }}
    QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {LINE}; }}

    QGroupBox {{
        border: 1px solid {LINE};
        border-radius: {RADIUS_PANEL}px;
        margin-top: 16px;
        padding: 12px 10px 8px 10px;
        background: {BG1};
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 12px;
        padding: 0 6px;
        color: {ACCENT};
    }}
    QStatusBar {{ background: {BG1}; border-top: 1px solid {LINE}; color: {MUTED}; }}
    QStatusBar::item {{ border: none; }}
    QSplitter::handle {{ background: transparent; }}
    QSplitter::handle:horizontal {{ width: 6px; }}
    QSplitter::handle:vertical {{ height: 6px; }}
    QProgressBar {{
        background: {BG2}; border: none; border-radius: 4px; height: 8px; text-align: center;
    }}
    QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
    QFrame[class="hline"] {{ background: {LINE}; max-height: 1px; border: none; }}
    """


def overlay_qss(opacity: float) -> str:
    """Style sheet for the overlay window.

    The overlay paints its own rounded, ``opacity``-alpha ``BG0`` background in
    ``paintEvent`` so that the text stays fully opaque; this sheet therefore makes every
    child transparent and only styles the header, tab strip, toolbar and meter.
    ``opacity`` is also exposed for widgets that want a matching panel colour via the
    ``QWidget[class="overlayPanel"]`` selector.
    """
    a = min(1.0, max(0.0, float(opacity)))
    families = ", ".join(f'"{f}"' for f in FONT_FAMILIES)
    return f"""
    QWidget {{
        background: transparent;
        color: {TEXT};
        font-family: {families};
        font-size: {OVERLAY_FONT_PX}px;
        border: none;
    }}
    QWidget[class="overlayPanel"] {{
        background: {rgba(BG0, a)};
        border: 1px solid {LINE};
        border-radius: {RADIUS_PANEL}px;
    }}
    QLabel {{ background: transparent; }}
    QLabel[class="muted"] {{ color: {MUTED}; }}
    QLabel[class="title"] {{ font-weight: 600; }}
    QLabel[class="accent"] {{ color: {ACCENT}; font-weight: 600; }}
    QLabel[class="ended"] {{ color: {MUTED}; font-style: italic; }}
    QToolButton {{
        background: transparent;
        color: {MUTED};
        border: 1px solid transparent;
        border-radius: 6px;
        padding: 2px 8px;
    }}
    QToolButton:hover {{ background: rgba(255,255,255,0.08); color: {TEXT}; }}
    QToolButton:pressed {{ background: rgba(255,255,255,0.14); }}
    QToolButton:checked {{ color: {ACCENT}; border-bottom: 2px solid {ACCENT}; border-radius: 0; }}
    QToolButton[class="tab"] {{ padding: 2px 10px; }}
    QWidget[class="overlayToolbar"] {{
        background: {rgba(BG2, 0.95)};
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: {RADIUS_CHIP}px;
    }}
    QSlider {{ background: transparent; min-height: 16px; }}
    QSlider::groove:horizontal {{ height: 3px; background: rgba(255,255,255,0.18); border-radius: 1px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 1px; }}
    QSlider::handle:horizontal {{
        width: 10px; height: 10px; margin: -4px 0; border-radius: 5px; background: {TEXT};
    }}
    QTableView {{
        background: transparent;
        alternate-background-color: transparent;
        gridline-color: transparent;
        border: none;
        selection-background-color: rgba(255,255,255,0.06);
        selection-color: {TEXT};
        outline: 0;
    }}
    QTableView::item {{ padding: 1px 4px; border: none; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{
        background: transparent;
        color: {MUTED};
        border: none;
        border-bottom: 1px solid {LINE};
        padding: 2px 4px;
        font-size: {OVERLAY_FONT_PX - 2}px;
        font-weight: 600;
    }}
    QListView, QListWidget, QTextEdit, QPlainTextEdit, QTextBrowser {{
        background: transparent; border: none; outline: 0;
    }}
    QScrollBar:vertical {{ background: transparent; width: 6px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 3px; min-height: 16px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; background: none; border: none; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
    QScrollBar:horizontal {{ height: 0; }}
    QSizeGrip {{ background: transparent; width: 14px; height: 14px; }}
    QToolTip {{
        background: {BG2}; color: {TEXT}; border: 1px solid rgba(255,255,255,0.14); padding: 3px 6px;
    }}
    """


def apply_theme(app: QApplication) -> None:
    """Apply the dark-glass theme to ``app``: Fusion style, palette, font and style sheet.

    Call once right after the ``QApplication`` is created, before any window is shown.
    """
    from PySide6.QtWidgets import QStyleFactory

    if "Fusion" in QStyleFactory.keys():
        app.setStyle("Fusion")
    else:  # pragma: no cover - Fusion ships with every Qt build
        log.warning("Fusion style unavailable; using %s", app.style().objectName())
    app.setPalette(_build_palette())
    app.setFont(app_font(MAIN_FONT_PX))
    app.setStyleSheet(main_qss())
    log.debug("theme applied (style=%s)", app.style().objectName())


__all__ = [
    "ACCENT",
    "ACCENT2",
    "BAR_ALPHA_LEFT",
    "BAR_ALPHA_RIGHT",
    "BG0",
    "BG1",
    "BG2",
    "DANGER",
    "FONT_FALLBACK",
    "FONT_FAMILIES",
    "FONT_FAMILY",
    "HOVER_LIGHTEN",
    "KIND_COLORS",
    "LINE",
    "MAIN_FONT_PX",
    "MUTED",
    "NPC",
    "OVERLAY_FONT_PX",
    "PARTY",
    "PET",
    "RADIUS_CHIP",
    "RADIUS_PANEL",
    "STATUS_COLORS",
    "SUCCESS",
    "TEXT",
    "YOU",
    "actor_color",
    "app_font",
    "apply_theme",
    "contrast_ratio",
    "feed_color",
    "lighten",
    "main_qss",
    "mix",
    "number_font",
    "overlay_qss",
    "qcolor",
    "relative_luminance",
    "rgba",
    "status_color",
    "tabular_font",
]
