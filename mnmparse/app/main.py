"""Application entry point, ``QApplication`` setup and the main window shell.

``run(argv)`` builds everything (APP_SPEC sections 3, 9):

* :class:`App` - the ``QApplication``: names, theme, icon, ``QSettings``, logging, and the
  wiring between the :class:`~mnmparse.app.engine.Engine`, the :class:`MainWindow`, the
  :class:`~mnmparse.app.overlay.OverlayWindow` and the :class:`~mnmparse.app.tray.TrayIcon`.
* :class:`MainWindow` - a normal taskbar window (never topmost) with the top bar
  (Start/Stop, status chips, overlay switches), the 56 px navigation rail and the pages.

Safety posture: nothing in here touches the game.  The only windows created are our own;
the overlay is never activated or raised after its initial ``show()``; the engine drives
the same read-only capture and OCR modules as the command-line tool.

Modules written by other parts of the app (pages, overlay, engine, widgets) are imported
with a fallback: when one is missing or broken the shell still starts and shows a
"module missing" placeholder so the rest can be exercised.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Sequence

from PySide6.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRect,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTimer,
    QtMsgType,
    Signal,
    qInstallMessageHandler,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QGuiApplication,
    QHideEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QShortcut,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from mnmparse import grammar
from mnmparse.app import APP_NAME, APP_TAGLINE, APP_VERSION, ORGANIZATION, SETTINGS_APP_NAME, theme
from mnmparse.app import icon as app_icon
from mnmparse.app.tray import TrayIcon
from mnmparse.app.widgets import WarningLatch, capture_warning
from mnmparse.config import Config, load_config, project_path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional modules (written concurrently; the shell must start without them)
# ---------------------------------------------------------------------------

_IMPORT_ERRORS: dict[str, str] = {}
"""Module name -> error text for every optional module that failed to import."""


def _optional_import(module: str, names: Sequence[str]) -> dict[str, Any]:
    """Import ``names`` from ``module``; on failure record the error and return ``{}``."""
    import importlib

    try:
        mod = importlib.import_module(module)
        return {name: getattr(mod, name) for name in names}
    except Exception as exc:  # noqa: BLE001 - ImportError or a bug inside the module
        _IMPORT_ERRORS[module] = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning("%s unavailable: %s", module, _IMPORT_ERRORS[module])
        return {}


_engine_mod = _optional_import("mnmparse.app.engine", ["Engine"])
_overlay_mod = _optional_import("mnmparse.app.overlay", ["OverlayWindow"])
_widgets_mod = _optional_import("mnmparse.app.widgets", ["StatusChip", "ToggleSwitch"])
_pages_mod = _optional_import(
    "mnmparse.app.pages", ["LivePage", "SessionPage", "FeedPage", "TriggersPage", "SettingsPage", "AboutPage"]
)

LOG_FILE_BYTES = 1_000_000
LOG_FILE_BACKUPS = 3
DEFAULT_WINDOW_SIZE = QSize(1180, 760)
MIN_WINDOW_SIZE = QSize(980, 640)
"""Floor of the window's minimum size; the real minimum also covers the pages' content
(see :meth:`MainWindow._sync_minimum_size`), so no page is ever squeezed into clipped text."""
NAV_RAIL_WIDTH = 56
TOP_BAR_HEIGHT = 60
STATUS_MESSAGE_MS = 8000
GIL_SWITCH_INTERVAL_S = 0.001  #: see run()
OCR_SLOW_MS = 150.0
"""OCR times above this show the OCR chip in the warning colour."""

PAGES: tuple[tuple[str, str], ...] = (
    ("live", "Live"),
    ("session", "Session"),
    ("feed", "Feed"),
    ("triggers", "Triggers"),
    ("settings", "Settings"),
    ("about", "About"),
)
"""Navigation rail entries: ``(key, label)`` in display order."""

RUNNING_STATES: frozenset[str] = frozenset({"starting", "no_window", "running", "paused"})
"""Engine states in which the Start/Stop button reads "Stop"."""

STATE_TEXT: dict[str, str] = {
    "stopped": "Capture stopped",
    "starting": "Starting capture...",
    "no_window": "Game window not found - retrying every 2 s",
    "running": "Capturing",
    "paused": "Paused",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _qt_message_handler(mode: QtMsgType, _context: Any, message: str) -> None:
    """Route Qt's own warnings into the log file (there is no console to see them)."""
    qt_log = logging.getLogger("qt")
    if mode == QtMsgType.QtDebugMsg:
        qt_log.debug("%s", message)
    elif mode == QtMsgType.QtInfoMsg:
        qt_log.info("%s", message)
    elif mode == QtMsgType.QtWarningMsg:
        qt_log.warning("%s", message)
    else:
        qt_log.error("%s", message)


def setup_logging(verbose: bool, log_dir: Path | None = None) -> Path | None:
    """Configure ``logs/app.log`` (rotating, 1 MB x 3) and uncaught-exception hooks.

    Args:
        verbose: DEBUG for the ``mnmparse`` loggers (INFO otherwise); other libraries
            stay at WARNING.
        log_dir: Where ``app.log`` goes; defaults to ``<project>/logs``.

    Returns:
        The log file path, or ``None`` when the directory could not be created (the app
        then logs to stderr only, if there is one).
    """
    root = logging.getLogger()
    root.setLevel(logging.WARNING)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    log_path: Path | None = None
    directory = log_dir or project_path("logs")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / "app.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=LOG_FILE_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        log_path = None
        print(f"Cannot open log file in {directory}: {exc}", file=sys.stderr)
    # No console window is ever opened; but when one is attached (python.exe rather than
    # pythonw.exe) mirror the log there so `--selftest-seconds` runs are easy to follow.
    if sys.stderr is not None and getattr(sys.stderr, "isatty", lambda: False)():
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(stream)
    logging.getLogger("mnmparse").setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("qt").setLevel(logging.DEBUG if verbose else logging.WARNING)

    def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        logging.getLogger("mnmparse.app").critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("mnmparse.app").critical(
            "Uncaught exception in thread %s", args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    qInstallMessageHandler(_qt_message_handler)
    return log_path


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------


def is_player_action(ev: Any, player_name: str) -> bool:
    """True when the event's actor is the local player (or the player's pet)."""
    if getattr(ev, "is_pet", False):
        return True
    if grammar.is_you(getattr(ev, "raw_actor", None)):
        return True
    actor = getattr(ev, "actor", None)
    return bool(player_name) and actor == player_name


def is_player_target(ev: Any, player_name: str) -> bool:
    """True when the event's target is the local player."""
    target = getattr(ev, "target", None)
    return grammar.is_you(target) or (bool(player_name) and target == player_name)


def _append_feed(
    append: Callable[..., Any],
    text: str,
    kind: str,
    player_action: bool,
    ts: float | None,
    player_target: bool,
) -> None:
    """Call a feed ``append`` with the parsed target flag, tolerating the older signature.

    ``is_player_target`` lets the feed colour a hit on the player phrased with the
    configured name (``a rat hits Maergoth ...``) as DANGER, not only lines with YOU.
    """
    args = (text, kind, player_action) if ts is None else (text, kind, player_action, ts)
    try:
        append(*args, is_player_target=player_target)
    except TypeError:  # a feed without the keyword: fall back to its own word scan
        append(*args)


# ---------------------------------------------------------------------------
# Fallback widgets (used only when mnmparse.app.widgets is missing)
# ---------------------------------------------------------------------------


class _FallbackChip(QLabel):
    """Minimal stand-in for ``widgets.StatusChip``: a coloured dot and a label."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setStyleSheet(
            f"QLabel {{ color: {theme.TEXT}; background: {theme.BG2}; border: 1px solid {theme.LINE};"
            f" border-radius: {theme.RADIUS_CHIP}px; padding: 3px 10px; }}"
        )
        self.set_state("idle", text)

    def set_state(self, state: str, text: str) -> None:
        """Set the dot colour (``ok``/``warn``/``bad``/``idle``) and the text."""
        self.setText(f'<span style="color:{theme.status_color(state)}">&#9679;</span>&nbsp;{text}')


class _FallbackToggle(QCheckBox):
    """Minimal stand-in for ``widgets.ToggleSwitch`` (a plain check box)."""


def _make_chip(text: str) -> QWidget:
    """Create a StatusChip (or the fallback) already showing ``text`` in the idle state."""
    cls = _widgets_mod.get("StatusChip")
    if cls is not None:
        for args in ((), (text,)):
            try:
                chip = cls(*args)
                chip.set_state("idle", text)
                return chip
            except TypeError:
                continue
        log.warning("StatusChip could not be constructed; using the fallback chip")
    return _FallbackChip(text)


def _make_toggle() -> QWidget:
    """Create a ToggleSwitch (or the fallback check box)."""
    cls = _widgets_mod.get("ToggleSwitch")
    if cls is not None:
        try:
            return cls()
        except TypeError:
            log.warning("ToggleSwitch() could not be constructed; using the fallback toggle")
    return _FallbackToggle()


class _MissingPage(QWidget):
    """Placeholder shown in place of a page whose module is missing or broken."""

    def __init__(self, title: str, reason: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head = QLabel(f"{title}: module missing")
        head.setProperty("class", "h2")
        head.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(reason)
        body.setProperty("class", "muted")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(head)
        layout.addWidget(body)


class _MissingEngine(QObject):
    """Stand-in with the Engine's signals when ``mnmparse.app.engine`` is unavailable."""

    message = Signal(object, object)
    snapshot = Signal(object)
    encounter_closed = Signal(object)
    status = Signal(dict)
    state_changed = Signal(str)
    error = Signal(str)

    def __init__(self, cfg: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.state = "stopped"
        self.is_running = False

    def start(self) -> None:
        """Report the missing module instead of capturing."""
        self.error.emit("Engine module missing: " + _IMPORT_ERRORS.get("mnmparse.app.engine", "?"))
        self.state_changed.emit("stopped")

    def stop(self) -> None:
        self.state_changed.emit("stopped")

    def set_paused(self, paused: bool) -> None:  # noqa: D102 - trivial stand-ins
        pass

    def reset_encounter(self) -> None:
        pass

    def update_config(self, cfg: Config) -> None:
        self._cfg = cfg

    def grab_frame(self) -> Any:
        return None

    def test_ocr(self, frame: Any, crop: Any, cfg: Config) -> list[Any]:
        return []

    def history(self) -> list[Any]:
        return []


# ---------------------------------------------------------------------------
# Navigation rail icons (QPainter)
# ---------------------------------------------------------------------------


def _paint_nav_glyph(painter: QPainter, name: str, size: float, color: QColor) -> None:
    """Draw one of the rail glyphs (live, session, feed, settings, about) into ``size`` px."""
    pen = QPen(color)
    pen.setWidthF(max(1.5, size * 0.09))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    m = size * 0.12
    inner = size - 2 * m
    centre = QPointF(size / 2, size / 2)
    if name == "live":
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        bar_w = inner / 3 * 0.68
        gap = (inner - 3 * bar_w) / 2
        for i, frac in enumerate((0.45, 0.75, 1.0)):
            h = inner * frac
            rect = QRectF(m + i * (bar_w + gap), m + (inner - h), bar_w, h)
            painter.drawRoundedRect(rect, bar_w * 0.25, bar_w * 0.25)
    elif name == "history":
        rect = QRectF(m, m, inner, inner)
        painter.drawEllipse(rect)
        painter.drawLine(centre, QPointF(centre.x(), centre.y() - inner * 0.32))
        painter.drawLine(centre, QPointF(centre.x() + inner * 0.24, centre.y()))
    elif name == "feed":
        for i, frac in enumerate((1.0, 0.7, 0.88)):
            y = m + inner * (0.15 + 0.35 * i)
            painter.drawLine(QPointF(m, y), QPointF(m + inner * frac, y))
    elif name == "session":
        # a loot bag: rounded body with a tied neck
        body = QRectF(m + inner * 0.1, m + inner * 0.3, inner * 0.8, inner * 0.68)
        painter.drawRoundedRect(body, inner * 0.22, inner * 0.22)
        painter.drawLine(QPointF(m + inner * 0.32, m + inner * 0.3), QPointF(m + inner * 0.42, m + inner * 0.06))
        painter.drawLine(QPointF(m + inner * 0.68, m + inner * 0.3), QPointF(m + inner * 0.58, m + inner * 0.06))
        painter.drawLine(QPointF(m + inner * 0.36, m + inner * 0.3), QPointF(m + inner * 0.64, m + inner * 0.3))
    elif name == "settings":
        radius = inner / 2
        painter.save()
        painter.translate(centre)
        for i in range(8):
            painter.save()
            painter.rotate(i * 45)
            painter.drawLine(QPointF(0, -radius * 0.6), QPointF(0, -radius))
            painter.restore()
        painter.restore()
        painter.drawEllipse(centre, radius * 0.55, radius * 0.55)
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, radius * 0.18, radius * 0.18)
    elif name == "triggers":
        # a bell: dome, rim and clapper
        w = inner * 0.62
        left, top = centre.x() - w / 2, m + inner * 0.08
        path = QPainterPath()
        path.moveTo(left, m + inner * 0.72)
        path.cubicTo(left, top, left + w, top, left + w, m + inner * 0.72)
        painter.drawPath(path)
        painter.drawLine(QPointF(left - inner * 0.1, m + inner * 0.74), QPointF(left + w + inner * 0.1, m + inner * 0.74))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(centre.x(), m + inner * 0.88), inner * 0.08, inner * 0.08)
    elif name == "about":
        painter.drawEllipse(QRectF(m, m, inner, inner))
        painter.drawLine(QPointF(centre.x(), centre.y() - size * 0.02), QPointF(centre.x(), centre.y() + size * 0.2))
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(centre.x(), centre.y() - size * 0.18), size * 0.055, size * 0.055)
    else:  # unknown page: a plain dot
        painter.setBrush(color)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, inner * 0.2, inner * 0.2)


def _nav_pixmap(name: str, color: str, size: int, dpr: float) -> QPixmap:
    """Render a rail glyph at ``size`` logical px for a screen with ``dpr``."""
    px = max(1, round(size * dpr))
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        _paint_nav_glyph(painter, name, float(px), theme.qcolor(color))
    finally:
        painter.end()
    pm.setDevicePixelRatio(dpr)
    return pm


def nav_icon(name: str, size: int = 22) -> QIcon:
    """A rail icon: muted when unchecked, gold when checked (``QIcon.State.On``)."""
    screen = QGuiApplication.primaryScreen()
    dpr = max(1.0, screen.devicePixelRatio() if screen is not None else 1.0)
    dpr = max(dpr, 2.0)  # draw at least 2x so it stays crisp on every monitor
    icon = QIcon()
    icon.addPixmap(_nav_pixmap(name, theme.MUTED, size, dpr), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_nav_pixmap(name, theme.TEXT, size, dpr), QIcon.Mode.Active, QIcon.State.Off)
    icon.addPixmap(_nav_pixmap(name, theme.ACCENT, size, dpr), QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(_nav_pixmap(name, theme.ACCENT, size, dpr), QIcon.Mode.Active, QIcon.State.On)
    return icon


class _NavButton(QToolButton):
    """One entry of the navigation rail: glyph above a small label, checkable."""

    def __init__(self, key: str, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self.setText(label)
        self.setIcon(nav_icon(key))
        self.setIconSize(QSize(22, 22))
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(NAV_RAIL_WIDTH, NAV_RAIL_WIDTH)
        self.setToolTip(label)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """The normal (taskbar, never topmost) main window.

    Layout: a top bar (brand, Start/Stop, status chips, overlay switches), a 56 px navigation
    rail on the left and a ``QStackedWidget`` with the pages.  The window never talks to the
    engine directly for control; it emits request signals that :class:`App` wires up, and it
    receives engine updates through the ``on_*`` slots.

    Signals:
        capture_toggled: Start/Stop pressed.
        overlay_toggled(bool): the Overlay switch changed (by the user).
        lock_toggled(bool): the Lock overlay switch changed (by the user).
        quit_requested: the window was closed and minimise-to-tray is off.
        minimised_to_tray: the window was hidden instead of closed.
        visibility_changed(bool): shown / hidden (for the tray menu label).
    """

    capture_toggled = Signal()
    overlay_toggled = Signal(bool)
    map_toggled = Signal(bool)
    lock_toggled = Signal(bool)
    quit_requested = Signal()
    minimised_to_tray = Signal()
    visibility_changed = Signal(bool)

    def __init__(
        self,
        engine: Any,
        overlay: Any | None,
        cfg: Config,
        settings: QSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._overlay = overlay
        self._cfg = cfg
        self._settings = settings
        self._quitting = False
        self._state = "stopped"
        self._scrolled_back = False  #: the chat does not show its newest line (engine status)
        self._last_occluded = 0
        self._pages: dict[str, QWidget] = {}
        self._nav_buttons: dict[str, _NavButton] = {}
        self._fitting_top_bar = False

        self.setWindowTitle(APP_NAME)
        self.resize(DEFAULT_WINDOW_SIZE)
        self.setMinimumSize(MIN_WINDOW_SIZE)

        central = QWidget(self)
        central.setObjectName("central")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top_bar())
        outer.addWidget(self._build_warning_banner())
        outer.addWidget(self._build_timer_share_banner())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav_rail())
        self._stack = QStackedWidget(central)
        body.addWidget(self._stack, 1)
        outer.addLayout(body, 1)
        self.setCentralWidget(central)

        self._status_bar = QStatusBar(self)
        self._state_label = QLabel(STATE_TEXT["stopped"], self._status_bar)
        self._state_label.setProperty("class", "muted")
        self._status_bar.addPermanentWidget(self._state_label)
        self.setStatusBar(self._status_bar)

        self.setStyleSheet(self._shell_qss())
        self._build_pages()
        self._install_shortcuts()
        self._restore_geometry()
        self.set_overlay_available(overlay is not None)
        self.on_state("stopped")

    # -- construction ------------------------------------------------------------------

    @staticmethod
    def _shell_qss() -> str:
        return f"""
        QFrame#topBar {{
            background: {theme.BG1};
            border-bottom: 1px solid {theme.LINE};
        }}
        QFrame#navRail {{
            background: {theme.BG1};
            border-right: 1px solid {theme.LINE};
        }}
        QFrame#navRail QToolButton {{
            border: none;
            border-radius: 10px;
            color: {theme.MUTED};
            font-size: 10px;
            padding: 4px 0 2px 0;
            margin: 2px 4px;
        }}
        QFrame#navRail QToolButton:hover {{ background: rgba(255,255,255,0.06); color: {theme.TEXT}; }}
        QFrame#navRail QToolButton:checked {{ background: {theme.rgba(theme.ACCENT, 0.14)}; color: {theme.ACCENT}; }}
        QFrame#warningBanner {{
            background: {theme.rgba(theme.ACCENT, 0.13)};
            border-bottom: 1px solid {theme.rgba(theme.ACCENT, 0.45)};
        }}
        QLabel#warningText {{ color: {theme.TEXT}; }}
        QLabel#warningIcon {{ color: {theme.ACCENT}; font-size: 15px; font-weight: 700; }}
        QPushButton#warningDismiss {{
            background: transparent; color: {theme.MUTED}; border: 1px solid {theme.LINE};
            border-radius: 8px; padding: 3px 12px;
        }}
        QPushButton#warningDismiss:hover {{ color: {theme.TEXT}; background: rgba(255,255,255,0.06); }}
        QLabel#brand {{ font-size: 17px; font-weight: 600; letter-spacing: 0.5px; }}
        QLabel#brandSub {{ color: {theme.MUTED}; font-size: 11px; }}
        QLabel#switchLabel {{ color: {theme.MUTED}; }}
        """

    def _build_top_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setObjectName("topBar")
        bar.setFixedHeight(TOP_BAR_HEIGHT)
        self._top_bar = bar
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 0, 16, 0)
        layout.setSpacing(10)
        self._top_layout = layout

        logo = QLabel(bar)
        logo_px = app_icon.render_icon_pixmap(56)
        logo_px.setDevicePixelRatio(2.0)
        logo.setPixmap(logo_px)
        logo.setFixedSize(28, 28)
        logo.setScaledContents(True)
        logo.setToolTip(APP_NAME)
        layout.addWidget(logo)
        # The brand text sits in its own widget so a narrow window can drop it (the logo
        # and the window title keep the name visible).
        self._brand_widget = QWidget(bar)
        brand_box = QVBoxLayout(self._brand_widget)
        brand_box.setContentsMargins(0, 0, 0, 0)
        brand_box.setSpacing(0)
        brand = QLabel(APP_NAME, self._brand_widget)
        brand.setObjectName("brand")
        self._brand_sub = QLabel(APP_TAGLINE, self._brand_widget)
        self._brand_sub.setObjectName("brandSub")
        brand_box.addWidget(brand)
        brand_box.addWidget(self._brand_sub)
        layout.addWidget(self._brand_widget)
        layout.addSpacing(8)

        self._start_button = QPushButton("Start capture", bar)
        self._start_button.setProperty("class", "primary")
        self._start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._start_button.setMinimumWidth(130)
        self._start_button.setToolTip("Start or stop reading the game's Combat window (F5)")
        self._start_button.clicked.connect(self.capture_toggled)
        layout.addWidget(self._start_button)
        layout.addSpacing(4)

        # Capture diagnostics (window found, rate, OCR time, messages, occlusion) live on the
        # Settings page; the top bar only warns when something needs the player's attention.
        layout.addStretch(1)

        self._overlay_switch = _make_toggle()
        self._overlay_switch.setToolTip("Show the always-on-top meter over the game (our own window)")
        self._lock_switch = _make_toggle()
        self._lock_switch.setToolTip("Locked = cannot be moved or resized; tabs, sorting and hover breakdowns still work")
        self._switch_labels: list[tuple[QLabel, str, str]] = []
        for switch, text, short in (
            (self._overlay_switch, "Overlay", "Overlay"),
            (self._lock_switch, "Lock overlay", "Lock"),
        ):
            label = QLabel(text, bar)
            label.setObjectName("switchLabel")
            label.setToolTip(switch.toolTip())
            layout.addWidget(label)
            layout.addWidget(switch)
            layout.addSpacing(6)
            self._switch_labels.append((label, text, short))
        self._overlay_switch.toggled.connect(self._on_overlay_switch)
        self._lock_switch.toggled.connect(self._on_lock_switch)
        self._map_button = QPushButton("Map", bar)
        self._map_button.setCheckable(True)
        self._map_button.setToolTip("Show the resizable zone map (automatically follows zone-entry messages)")
        self._map_button.toggled.connect(self.map_toggled)
        layout.addWidget(self._map_button)
        return bar

    def _top_bar_needed_width(self) -> int:
        """Minimum width the top bar's visible items need right now."""
        self._top_layout.invalidate()
        return self._top_layout.minimumSize().width()

    def _fit_top_bar(self) -> None:
        """Drop the least important top-bar items until the bar fits the window width.

        Order: the brand subtitle, the brand text, then the long switch labels.  Everything
        comes back as soon as the window is wide enough.
        """
        if self._fitting_top_bar or getattr(self, "_state_label", None) is None:
            return  # re-entrant call, or a resize event before the shell is fully built
        self._fitting_top_bar = True
        try:
            available = self._top_bar.width()
            self._brand_sub.setVisible(True)
            self._brand_widget.setVisible(True)
            for label, full, _short in self._switch_labels:
                label.setText(full)
            steps: list[Callable[[], None]] = [
                lambda: self._brand_sub.setVisible(False),
                lambda: self._brand_widget.setVisible(False),
                lambda: [label.setText(short) for label, _full, short in self._switch_labels],
            ]
            for step in steps:
                if self._top_bar_needed_width() <= available:
                    break
                step()
        finally:
            self._fitting_top_bar = False
        self._update_state_label()

    def _update_state_label(self) -> None:
        """Status bar: the engine state (and whether the chat is scrolled up)."""
        text = STATE_TEXT.get(self._state, self._state)
        if getattr(self, "_scrolled_back", False) and self._state in RUNNING_STATES:
            text += "  ·  chat scrolled up (lines wait)"
        self._state_label.setText(text)

    def _build_warning_banner(self) -> QWidget:
        """A thin bar under the top bar for capture problems; hidden while all is well."""
        banner = QFrame(self)
        banner.setObjectName("warningBanner")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 6, 12, 6)
        layout.setSpacing(10)
        icon = QLabel("!", banner)
        icon.setObjectName("warningIcon")
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        self._warning_text = QLabel("", banner)
        self._warning_text.setObjectName("warningText")
        self._warning_text.setWordWrap(True)
        layout.addWidget(self._warning_text, 1)
        dismiss = QPushButton("Dismiss", banner)
        dismiss.setObjectName("warningDismiss")
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setToolTip("Hide this until the problem has cleared and comes back")
        dismiss.clicked.connect(self._dismiss_warning)
        layout.addWidget(dismiss, 0, Qt.AlignmentFlag.AlignVCenter)
        banner.hide()
        self._warning_banner = banner
        self._warning_latch = WarningLatch()
        return banner

    def _dismiss_warning(self) -> None:
        self._warning_latch.dismiss()
        self._warning_banner.hide()

    def _build_timer_share_banner(self) -> QWidget:
        banner = QFrame(self)
        banner.setObjectName("timerShareBanner")
        banner.setStyleSheet(f"QFrame#timerShareBanner {{ background: {theme.BG2}; border-bottom: 1px solid {theme.LINE}; }}")
        layout = QHBoxLayout(banner)
        layout.setContentsMargins(16, 6, 12, 6)
        self._timer_share_text = QLabel(banner)
        self._timer_share_text.setTextFormat(Qt.TextFormat.PlainText)
        self._timer_share_text.setWordWrap(True)
        layout.addWidget(self._timer_share_text, 1)
        review = QPushButton("Review timer…", banner)
        review.setObjectName("Chip")
        review.clicked.connect(self._review_shared_timer)
        layout.addWidget(review)
        banner.hide()
        self._timer_share_banner = banner
        return banner

    def set_timer_share_notice(self, message: str) -> None:
        """Keep an incoming share reviewable without opening or focusing a window."""
        self._timer_share_text.setText(message)
        self._timer_share_banner.setVisible(bool(message))

    def _review_shared_timer(self) -> None:
        page = self.page("triggers")
        review = getattr(page, "review_chat_shares", None)
        if callable(review):
            self.show_page("triggers")
            review()

    def warning_visible(self) -> bool:
        """True while the capture warning banner is shown."""
        return not self._warning_banner.isHidden()

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit_top_bar()
        # The child bar can still have its old width until Qt completes this layout.
        QTimer.singleShot(0, self, self._fit_top_bar)

    def event(self, event: QEvent) -> bool:  # noqa: D102 - Qt override
        result = super().event(event)
        if event.type() == QEvent.Type.LayoutRequest:
            self._sync_minimum_size()
        return result

    def _sync_minimum_size(self) -> None:
        """Minimum size = what the content needs (never below :data:`MIN_WINDOW_SIZE`).

        The content's minimum changes at run time (the encounter header wraps to two rows,
        the details chips wrap), so it is re-read on every layout request; Qt then grows
        the window if it is smaller than the new minimum.
        """
        layout = self.layout()
        if layout is None:
            return
        want = layout.minimumSize().expandedTo(MIN_WINDOW_SIZE)
        if want != self.minimumSize():
            self.setMinimumSize(want)

    def _build_nav_rail(self) -> QWidget:
        rail = QFrame(self)
        rail.setObjectName("navRail")
        rail.setFixedWidth(NAV_RAIL_WIDTH)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(2)
        self._nav_group = QButtonGroup(rail)
        self._nav_group.setExclusive(True)
        for index, (key, label) in enumerate(PAGES):
            button = _NavButton(key, label, rail)
            self._nav_group.addButton(button, index)
            self._nav_buttons[key] = button
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)
            if key == "settings":
                layout.insertStretch(layout.count() - 1, 1)  # push Settings/About to the bottom
        self._nav_group.idClicked.connect(self._on_nav_clicked)
        return rail

    def _build_pages(self) -> None:
        for key, label in PAGES:
            page = self._make_page(key, label)
            self._pages[key] = page
            self._stack.addWidget(page)
        wanted = str(self._settings.value("main/page", "live"))
        self.show_page(wanted if wanted in self._pages else "live")
        # Live > Import log also hands the file's session to the Session page.
        live, session = self._pages.get("live"), self._pages.get("session")
        imported = getattr(live, "imported", None)
        adder = getattr(session, "add_imported_session", None)
        if imported is not None and callable(adder):
            try:
                imported.connect(lambda result: adder(result.name, result.session))
            except Exception:  # noqa: BLE001
                log.debug("could not wire the import signal", exc_info=True)

    def _make_page(self, key: str, label: str) -> QWidget:
        """Construct ``<Label>Page(engine, cfg, settings)`` or a placeholder."""
        cls_name = f"{label}Page"
        cls = _pages_mod.get(cls_name)
        if cls is None:
            reason = _IMPORT_ERRORS.get("mnmparse.app.pages", f"{cls_name} not found")
            return _MissingPage(label, reason)
        try:
            return cls(self._engine, self._cfg, self._settings)
        except Exception as exc:  # noqa: BLE001 - a broken page must not take the shell down
            log.exception("%s could not be created", cls_name)
            return _MissingPage(label, f"{type(exc).__name__}: {exc}")

    def _install_shortcuts(self) -> None:
        for index, (key, _label) in enumerate(PAGES, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda k=key: self.show_page(k))
        QShortcut(QKeySequence("F5"), self).activated.connect(self.capture_toggled)
        QShortcut(QKeySequence("Ctrl+Q"), self).activated.connect(self.request_quit)

    # -- pages ---------------------------------------------------------------------------

    def page(self, key: str) -> QWidget | None:
        """Return the page widget for ``key`` (``"live"`` ... ``"about"``) or ``None``."""
        return self._pages.get(key)

    def show_page(self, key: str) -> None:
        """Switch the stack to ``key`` and highlight its rail button."""
        page = self._pages.get(key)
        if page is None:
            return
        self._stack.setCurrentWidget(page)
        button = self._nav_buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        self._settings.setValue("main/page", key)

    def _on_nav_clicked(self, index: int) -> None:
        if 0 <= index < len(PAGES):
            self.show_page(PAGES[index][0])

    # -- engine slots (always called in the GUI thread) ---------------------------------

    def on_state(self, state: str) -> None:
        """Reflect the engine state in the Start/Stop button, chips and status bar."""
        self._state = state
        running = state in RUNNING_STATES
        self._start_button.setText("Stop capture" if running else "Start capture")
        if state == "stopped":
            self._warning_latch.update(False)
            self._warning_banner.hide()
        settings_page = self._pages.get("settings")
        setter = getattr(settings_page, "set_engine_state", None)
        if callable(setter):
            setter(state)
        self.setWindowTitle(f"{APP_NAME} - {STATE_TEXT.get(state, state)}" if running else APP_NAME)
        self._update_state_label()
        self._fit_top_bar()

    def on_status(self, status: dict[str, Any]) -> None:
        """Route the engine's ~1 Hz status: details to Settings, problems to the banner, and
        the chat being scrolled up (a note, not a problem) to the status bar's state text."""
        settings_page = self._pages.get("settings")
        setter = getattr(settings_page, "set_status", None)
        if callable(setter):
            setter(status)
        state = str(status.get("state", self._state))
        scrolled = bool(status.get("scrolled_back")) and state in RUNNING_STATES
        if scrolled != self._scrolled_back:
            self._scrolled_back = scrolled
            self._update_state_label()
        problems = {k: v for k, v in status.items() if k != "scrolled_back"}
        warning = capture_warning(problems) if state in RUNNING_STATES else None
        if self._warning_latch.update(warning is not None) and warning is not None:
            self._warning_text.setText(warning[1])
            self._warning_banner.show()
        else:
            self._warning_banner.hide()

    def on_error(self, text: str) -> None:
        """Show an engine error in the status bar (it is also in the log)."""
        self._status_bar.showMessage(text, STATUS_MESSAGE_MS)

    def show_message(self, text: str) -> None:
        """A short note in the status bar (e.g. what was copied to the clipboard)."""
        self._status_bar.showMessage(text, STATUS_MESSAGE_MS)

    def on_snapshot(self, snap: Any) -> None:
        """Forward the live encounter snapshot to the Live page."""
        page = self._pages.get("live")
        setter = getattr(page, "set_snapshot", None)
        if callable(setter):
            setter(snap)

    def on_encounter_closed(self, snap: Any) -> None:
        """Forward a closed encounter to the Live page's encounter list."""
        page = self._pages.get("live")
        adder = getattr(page, "add_encounter", None)
        if callable(adder):
            adder(snap)

    def on_encounter_updated(self, snap: Any) -> None:
        """A listed fight was counted again (the party roster learned a member): refresh its row."""
        page = self._pages.get("live")
        updater = getattr(page, "update_encounter", None)
        if callable(updater):
            updater(snap)

    def on_session(self, snap: Any) -> None:
        """Forward the session (loot / kills / CC) snapshot to the Session page."""
        page = self._pages.get("session")
        setter = getattr(page, "set_session", None)
        if callable(setter):
            setter(snap)

    def on_message(self, msg: Any, ev: Any) -> None:
        """Forward a logged message to the Feed page."""
        page = self._pages.get("feed")
        append = getattr(page, "append", None)
        if not callable(append):
            return
        player = getattr(self._cfg, "player_name", "") or ""
        _append_feed(append, msg.text, ev.kind, is_player_action(ev, player), msg.first_seen,
                     is_player_target(ev, player))

    def set_config(self, cfg: Config) -> None:
        """Adopt a freshly saved configuration (minimise-to-tray, player name, fps target)."""
        self._cfg = cfg
        for key, page in self._pages.items():
            if key == "settings":
                continue
            setter = getattr(page, "set_config", None)
            if callable(setter):
                try:
                    setter(cfg)
                except Exception:  # noqa: BLE001 - a page must not break the save
                    log.exception("%s page could not adopt the configuration", key)

    # -- overlay switches ----------------------------------------------------------------

    def set_overlay_available(self, available: bool, *, lock_available: bool | None = None) -> None:
        """Enable available controls; a map window can still use the shared lock."""
        self._overlay_switch.setEnabled(available)
        self._lock_switch.setEnabled(available if lock_available is None else lock_available)
        if not available:
            self._overlay_switch.setToolTip(
                "Overlay unavailable: " + _IMPORT_ERRORS.get("mnmparse.app.overlay", "module missing")
            )

    def set_overlay_state(self, visible: bool) -> None:
        """Mirror the overlay's visibility in the switch without emitting ``overlay_toggled``."""
        self._overlay_switch.blockSignals(True)
        self._overlay_switch.setChecked(bool(visible))
        self._overlay_switch.blockSignals(False)

    def set_lock_state(self, locked: bool) -> None:
        """Mirror the overlay's lock state in the switch without emitting ``lock_toggled``."""
        self._lock_switch.blockSignals(True)
        self._lock_switch.setChecked(bool(locked))
        self._lock_switch.blockSignals(False)

    def _on_overlay_switch(self, checked: bool) -> None:
        self.overlay_toggled.emit(bool(checked))

    def _on_lock_switch(self, checked: bool) -> None:
        self.lock_toggled.emit(bool(checked))

    # -- window lifecycle ------------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Restore the logical geometry saved by :meth:`save_state`, or centre the default.

        The logical ``QRect`` plus the screen name are stored instead of
        ``QWidget.saveGeometry``: the opaque blob shrinks the window by the frame
        margins each time it is restored across screens with different device pixel
        ratios (the primary is DPR 1.5, the secondary DPR 1.0).  The rect is applied
        only when that screen still exists and still contains it; otherwise the window
        gets its default size centred on the primary screen.
        """
        rect = self._settings.value("main/rect")
        screen_name = str(self._settings.value("main/screen", "") or "")
        maximized = self._settings.value("main/maximized", False, type=bool)
        screen = next((s for s in QGuiApplication.screens() if s.name() == screen_name), None)
        if isinstance(rect, QRect) and rect.isValid() and screen is not None and screen.availableGeometry().contains(rect):
            self.setGeometry(rect)
        else:
            if rect is not None:
                log.debug("stored window geometry %r on %r not usable; using defaults", rect, screen_name)
            primary = QGuiApplication.primaryScreen()
            self.resize(DEFAULT_WINDOW_SIZE)
            if primary is not None:
                self.move(primary.availableGeometry().center() - self.rect().center())
        if maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def save_state(self) -> None:
        """Persist the window geometry (logical rect + screen) and current page to QSettings."""
        maximized = bool(self.windowState() & Qt.WindowState.WindowMaximized)
        self._settings.setValue("main/maximized", maximized)
        if not maximized and not (self.windowState() & Qt.WindowState.WindowMinimized):
            screen = self.screen()
            self._settings.setValue("main/rect", QRect(self.geometry()))
            self._settings.setValue("main/screen", screen.name() if screen is not None else "")
        self._settings.remove("main/geometry")  # the pre-1.1 saveGeometry() blob
        current = self._stack.currentWidget()
        for key, page in self._pages.items():
            if page is current:
                self._settings.setValue("main/page", key)
                break

    def prepare_quit(self) -> None:
        """Make the next ``close()`` quit instead of minimising to the tray."""
        self._quitting = True

    def request_quit(self) -> None:
        """Quit the application (Ctrl+Q)."""
        self.prepare_quit()
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self.save_state()
        tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        if not self._quitting and getattr(self._cfg, "minimize_to_tray", True) and tray_ok:
            event.ignore()
            self.hide()
            self.minimised_to_tray.emit()
            return
        event.accept()
        if not self._quitting:
            self._quitting = True
            self.quit_requested.emit()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        QTimer.singleShot(0, self, self._fit_top_bar)
        QTimer.singleShot(0, self, lambda: _ensure_native_visible(self))
        self.visibility_changed.emit(True)

    def reveal(self) -> None:
        """Restore our main window from the tray without discarding maximization."""
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        _ensure_native_visible(self)
        self.raise_()
        self.activateWindow()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt override
        super().hideEvent(event)
        self.visibility_changed.emit(False)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and not self.isMinimized():
            # restored from the taskbar: pages that skipped updates while minimised catch up
            refresh = getattr(self._pages.get("live"), "refresh_if_stale", None)
            if callable(refresh):
                refresh()


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _call_first(obj: Any, names: Sequence[str], *args: Any) -> bool:
    """Call the first method of ``obj`` named in ``names``; return whether one existed."""
    for name in names:
        fn = getattr(obj, name, None)
        if callable(fn):
            fn(*args)
            return True
    return False


def _overlay_locked(overlay: Any, default: bool) -> bool:
    """Read the overlay's lock state through whichever accessor it offers."""
    for name in ("is_locked", "locked"):
        attr = getattr(overlay, name, None)
        if attr is None:
            continue
        try:
            return bool(attr() if callable(attr) else attr)
        except Exception:  # noqa: BLE001
            log.debug("overlay.%s failed", name, exc_info=True)
    return default


def _ensure_native_visible(window: QWidget) -> None:
    """Repair SW_HIDE launch state for our main window, including old updaters.

    Windows can consume a launcher's hidden flag on Qt's first ShowWindow call.
    Qt then considers the window shown, so another QWidget.show() is a no-op.
    Check after the show event and only repair an unintended native mismatch.
    """
    if (QGuiApplication.platformName() != "windows" or not window.isVisible()
            or window.isMinimized() or window.windowHandle() is None):
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        hwnd = int(window.winId())
        if not user32.IsWindowVisible(hwnd):
            # Preserve maximization; a normal window need not steal focus at startup.
            user32.ShowWindow(hwnd, 3 if window.isMaximized() else 8)
            log.info("Restored main window hidden by its launcher")
    except Exception:
        log.debug("Could not reconcile main window visibility", exc_info=True)


class App(QApplication):
    """The ``QApplication``: owns the engine, windows, tray and settings, and wires them.

    Build it with ``App(argv)`` and then call :meth:`bootstrap`.  All engine signals are
    connected to methods of QObjects living in the GUI thread, so Qt queues them across the
    worker thread automatically.
    """

    def __init__(self, argv: Sequence[str]) -> None:
        super().__init__(list(argv))
        self.setApplicationName(APP_NAME)
        self.setOrganizationName(ORGANIZATION)
        self.setApplicationVersion(APP_VERSION)
        self.setQuitOnLastWindowClosed(False)  # the tray keeps us alive when the window hides
        theme.apply_theme(self)
        self.icon: QIcon = app_icon.make_icon()
        self.setWindowIcon(self.icon)
        self.settings = QSettings(ORGANIZATION, SETTINGS_APP_NAME)
        self.cfg: Config = Config()
        self.engine: Any = None
        self.window: MainWindow | None = None
        self.overlay: Any | None = None
        self.map_overlay: Any | None = None
        self.map_downloads: Any | None = None
        self.app_updates: Any | None = None
        self.triggers: Any | None = None
        self.tray: TrayIcon | None = None
        self._last_status: dict[str, Any] = {}
        self._selftest = False
        self._shut_down = False
        self.aboutToQuit.connect(self.shutdown)

    # -- startup -----------------------------------------------------------------------

    def bootstrap(self, cfg: Config, *, selftest_seconds: float | None = None) -> None:
        """Create the engine, windows and tray for ``cfg`` and show the main window.

        Args:
            cfg: The loaded configuration.
            selftest_seconds: When set, start capture, show the overlay and quit after
                that many seconds (the integrator's smoke test).
        """
        self.cfg = cfg
        from mnmparse.trigger_chat import ChatShareAssembler
        self._chat_shares = ChatShareAssembler()
        app_icon.ensure_icon_file(project_path("assets") / "icon.ico")
        self.engine = self._make_engine(cfg)
        self.overlay = self._make_overlay(cfg)
        from mnmparse.app.map_overlay import MapOverlay
        from mnmparse.app.map_downloads import MapDownloadController
        from mnmparse.app.app_updates import AppUpdateController
        self.map_overlay = MapOverlay(self.settings)
        self.map_downloads = MapDownloadController(self.map_overlay.repo, self)
        self.app_updates = AppUpdateController(self)
        self.triggers = self._make_trigger_runner()
        self.window = MainWindow(self.engine, self.overlay, cfg, self.settings)
        self.tray = TrayIcon(self.icon, self)
        self._wire()

        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        else:
            log.warning("No system tray available; closing the window quits the app")
        self.window.show()

        locked = self._current_overlay_lock()
        self._on_overlay_locked(locked)
        self._sync_map_appearance()
        lock_available = self.overlay is not None or self.map_overlay is not None
        self.window.set_overlay_available(self.overlay is not None, lock_available=lock_available)
        self.tray.set_overlay_available(self.overlay is not None, lock_available=lock_available)

        show_overlay = self.settings.value("main/overlay_visible", bool(getattr(cfg, "overlay_enabled", False)), type=bool)
        if selftest_seconds is not None:
            self._selftest = True
            show_overlay = True
        self.set_overlay_visible(bool(show_overlay))
        self._sync_overlay_settings()  # Settings > Overlay shows what the overlay really uses
        self.set_map_visible(self.settings.value("map/visible", True, type=bool))
        if selftest_seconds is None and self.settings.value("map/download_on_startup", False, type=bool):
            QTimer.singleShot(0, self.map_downloads.start)
        if selftest_seconds is None and self.settings.value("app/update_on_startup", False, type=bool):
            QTimer.singleShot(0, self.app_updates.start)

        if selftest_seconds is not None:
            log.info("Self-test: capturing for %.0f s", selftest_seconds)
            QTimer.singleShot(0, self.start_capture)
            QTimer.singleShot(int(max(0.0, selftest_seconds) * 1000), self.request_quit)
        elif bool(getattr(cfg, "start_capture_on_launch", False)):
            QTimer.singleShot(0, self.start_capture)

    def _make_engine(self, cfg: Config) -> Any:
        cls = _engine_mod.get("Engine")
        if cls is None:
            return _MissingEngine(cfg, self)
        try:
            return cls(cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("Engine could not be created")
            _IMPORT_ERRORS["mnmparse.app.engine"] = f"{type(exc).__name__}: {exc}"
            return _MissingEngine(cfg, self)

    def _make_overlay(self, cfg: Config) -> Any | None:
        cls = _overlay_mod.get("OverlayWindow")
        if cls is None:
            return None
        try:
            return cls(self.settings, cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("OverlayWindow could not be created")
            _IMPORT_ERRORS["mnmparse.app.overlay"] = f"{type(exc).__name__}: {exc}"
            return None

    def _wire(self) -> None:
        """Connect engine -> window/overlay/pages and window/tray -> app actions."""
        assert self.window is not None and self.tray is not None
        engine, window, tray, overlay = self.engine, self.window, self.tray, self.overlay

        engine.state_changed.connect(window.on_state)
        engine.state_changed.connect(self._on_engine_state)
        engine.status.connect(window.on_status)
        engine.status.connect(self._on_engine_status)
        engine.error.connect(window.on_error)
        engine.error.connect(self._on_engine_error)
        engine.snapshot.connect(window.on_snapshot)
        engine.encounter_closed.connect(window.on_encounter_closed)
        engine.encounter_closed.connect(self._on_encounter_closed_export)
        # A fight counted again is not a new one: it never reaches the auto-copy (or its sound).
        self._connect_optional(engine, "encounter_updated", window.on_encounter_updated)
        self._connect_optional(engine, "notice", window.show_message)
        engine.message.connect(window.on_message)
        engine.message.connect(self._on_chat_share)
        if self.map_overlay is not None:
            engine.message.connect(self.map_overlay.on_message)
            self.map_overlay.visibility_changed.connect(self._on_map_visibility)
        window.map_toggled.connect(self.set_map_visible)
        live_page = window.page("live")
        if live_page is not None:
            self._connect_optional(live_page, "copy_requested", self.copy_snapshot)
            self._connect_optional(live_page, "group_override_requested", self.set_group_override)
            self._connect_optional(live_page, "pet_owner_requested", self.set_pet_owner)
        if self.triggers is not None:
            engine.message.connect(self._on_message_for_triggers)
            page = window.page("triggers")
            if page is not None and callable(getattr(page, "set_runner", None)):
                page.set_runner(self.triggers)
                self._connect_optional(page, "chat_share_pending", self._on_chat_share_pending)
            if overlay is not None and callable(getattr(overlay, "set_trigger_runner", None)):
                overlay.set_trigger_runner(self.triggers)
        self._connect_optional(engine, "session", window.on_session)
        if overlay is not None:
            engine.snapshot.connect(overlay.set_snapshot)
            if hasattr(overlay, "update_encounter"):
                self._connect_optional(engine, "encounter_updated", overlay.update_encounter)
            if hasattr(overlay, "set_session"):
                self._connect_optional(engine, "session", overlay.set_session)
            if hasattr(overlay, "set_status"):
                engine.status.connect(overlay.set_status)
            engine.message.connect(self._on_message_for_overlay)
            self._connect_optional(overlay, "copy_requested", self.copy_snapshot)
            self._connect_optional(overlay, "group_override_requested", self.set_group_override)
            self._connect_optional(overlay, "pet_owner_requested", self.set_pet_owner)
            self._connect_optional(overlay, "locked_changed", self._on_overlay_locked)
            self._connect_optional(overlay, "appearance_changed", self._sync_map_appearance)
            self._connect_optional(overlay, "click_through_changed", self._on_overlay_click_through)
            self._connect_optional(overlay, "visibility_changed", self._on_overlay_visibility)

        window.capture_toggled.connect(self.toggle_capture)
        window.overlay_toggled.connect(self.set_overlay_visible)
        window.lock_toggled.connect(self.set_overlay_locked)
        window.quit_requested.connect(self.request_quit)
        window.minimised_to_tray.connect(self._on_minimised_to_tray)
        window.visibility_changed.connect(tray.set_window_visible)

        settings_page = window.page("settings")
        if settings_page is not None:
            self._connect_optional(settings_page, "config_changed", self.on_config_changed)
            self._connect_optional(settings_page, "sound_preview_requested", self.play_sound)
            self._connect_optional(settings_page, "overlay_reset_requested", self.reset_overlay_position)
            self._connect_optional(settings_page, "overlay_setting_changed", self._on_overlay_setting)
            if self.app_updates is not None:
                self._connect_optional(settings_page, "app_update_requested", self.app_updates.start)
                self._connect_optional(settings_page, "app_restart_requested", self.restart_for_update)
                self.app_updates.started.connect(
                    lambda: settings_page.set_app_update_status("Checking GitHub for updates…", True))
                self.app_updates.progress.connect(
                    lambda message: settings_page.set_app_update_status(message, True))
                self.app_updates.finished.connect(self._on_app_update_status)
                self.app_updates.ready.connect(
                    lambda version: self._on_app_update_status(f"Version {version} is ready. Restart to install."))
            if self.map_downloads is not None:
                self._connect_optional(settings_page, "map_download_requested", self.map_downloads.start)
                self.map_downloads.started.connect(
                    lambda: settings_page.set_map_download_status("Downloading latest maps…", True))
                self.map_downloads.progress.connect(
                    lambda message: settings_page.set_map_download_status(message, True))
                self.map_downloads.finished.connect(self._on_map_download_finished)
            if overlay is not None:
                for name in ("locked_changed", "click_through_changed", "tab_changed", "visibility_changed",
                             "appearance_changed", "attack_bar_changed"):
                    self._connect_optional(overlay, name, lambda *_: self._sync_overlay_settings())
        if overlay is not None and callable(getattr(overlay, "set_history", None)):
            try:
                overlay.set_history(engine.history())
            except Exception:  # noqa: BLE001
                log.debug("overlay history could not be primed", exc_info=True)
        live_page = window.page("live")
        if live_page is not None and callable(getattr(live_page, "set_history", None)):
            try:
                live_page.set_history(engine.history())
            except Exception:  # noqa: BLE001
                log.debug("history page could not be primed", exc_info=True)

        tray.show_window_requested.connect(self.show_window)
        tray.hide_window_requested.connect(self.hide_window)
        tray.toggle_capture_requested.connect(self.toggle_capture)
        tray.toggle_overlay_requested.connect(self.toggle_overlay)
        tray.toggle_lock_requested.connect(self.toggle_overlay_lock)
        self._connect_optional(tray, "toggle_click_through_requested", self.toggle_overlay_click_through)
        if self.overlay is not None and hasattr(self.overlay, "click_through"):
            tray.set_overlay_click_through(bool(self.overlay.click_through))
        tray.reset_requested.connect(self.reset_encounter)
        tray.quit_requested.connect(self.request_quit)

    def _on_chat_share(self, msg: Any, event: Any) -> None:
        if getattr(msg, "backlog", False) or self.window is None:
            return
        page = self.window.page("triggers")
        offer = getattr(page, "offer_chat_share", None)
        if not callable(offer) or self.triggers is None:
            return
        text = getattr(msg, "text", "") or ""
        sender = str(getattr(event, "actor", "") or "") if getattr(event, "kind", "") == "chat" else ""
        try:
            for share in self._chat_shares.feed(text, sender=sender):
                if offer(share) is False:
                    self._chat_shares.forget(share.share_id)
        except Exception:  # Bad OCR or a malformed share must not interrupt capture.
            log.debug("Could not read a chat timer share", exc_info=True)

    def _on_chat_share_pending(self, message: str) -> None:
        if self.window is not None:
            self.window.set_timer_share_notice(message)
            if message and not self.window.isVisible() and self.tray is not None:
                self.tray.notify("Shared PNUT timer", message + " Open PNUT to review it.")

    def _on_app_update_status(self, message: str) -> None:
        if self.window is not None:
            page = self.window.page("settings")
            if page is not None:
                ready = self.app_updates is not None and self.app_updates.ready_package is not None
                page.set_app_update_status(message, False, ready)

    def restart_for_update(self) -> None:
        if self.app_updates is not None and self.app_updates.prepare_restart():
            self.request_quit()

    def _on_map_download_finished(self, message: str) -> None:
        if self.window is not None:
            page = self.window.page("settings")
            if page is not None:
                page.set_map_download_status(message, False)
        if self.map_overlay is not None:
            self.map_overlay.reload_cached_map()

    @staticmethod
    def _connect_optional(obj: Any, signal_name: str, slot: Callable[..., Any]) -> bool:
        """Connect ``obj.<signal_name>`` to ``slot`` if the signal exists."""
        signal = getattr(obj, signal_name, None)
        if signal is None or not hasattr(signal, "connect"):
            log.debug("%s has no %s signal", type(obj).__name__, signal_name)
            return False
        signal.connect(slot)
        return True

    # -- capture -----------------------------------------------------------------------

    def start_capture(self) -> None:
        """Start the engine worker (returns immediately)."""
        if getattr(self.cfg, "capture_backend", "wgc") == "mss" and self.overlay is not None and self.overlay.isVisible():
            # Only the WGC backend captures the game window alone; mss grabs the desktop
            # rectangle, so an overlay sitting over the Combat chat would be OCR'd.
            text = "Backend \"mss\" captures the desktop: keep the overlay away from the Combat chat crop."
            log.warning(text)
            if self.window is not None:
                self.window.on_error(text)
        try:
            self.engine.start()
        except Exception as exc:  # noqa: BLE001 - surface instead of crashing the GUI
            log.exception("engine.start failed")
            if self.window is not None:
                self.window.on_error(f"Could not start capture: {exc}")

    def stop_capture(self) -> None:
        """Stop the engine worker (flushes the tracker and closes the logs)."""
        try:
            self.engine.stop()
        except Exception:  # noqa: BLE001
            log.exception("engine.stop failed")

    def toggle_capture(self) -> None:
        """Start/Stop from the top bar, F5 or the tray."""
        running = bool(getattr(self.engine, "is_running", False))
        if not running:
            running = getattr(self.engine, "state", "stopped") in RUNNING_STATES
        if running:
            self.stop_capture()
        else:
            self.start_capture()

    def reset_encounter(self) -> None:
        """Close the open encounter now."""
        _call_first(self.engine, ("reset_encounter",))

    def _on_engine_state(self, state: str) -> None:
        if self.tray is not None:
            self.tray.set_capture_running(state in RUNNING_STATES)
            self.tray.set_status_text(STATE_TEXT.get(state, state))

    def _on_engine_status(self, status: dict[str, Any]) -> None:
        self._last_status = dict(status)

    def _on_engine_error(self, text: str) -> None:
        log.error("engine: %s", text)
        if self.tray is not None and self.window is not None and not self.window.isVisible():
            self.tray.notify(APP_NAME, text, warning=True)

    def _make_trigger_runner(self) -> Any:
        try:
            from mnmparse.app.triggers_runtime import TriggerRunner, default_store

            return TriggerRunner(default_store(), self)
        except Exception:  # noqa: BLE001 - triggers must never stop the app
            log.exception("triggers unavailable")
            return None

    def _on_message_for_triggers(self, msg: Any, ev: Any) -> None:
        text = getattr(msg, "text", "") or ""
        try:
            self.triggers.observe(text)
        except Exception:  # noqa: BLE001
            log.exception("trigger evaluation failed on %r", text)
        page = self.window.page("triggers") if self.window is not None else None
        adder = getattr(page, "add_recent_line", None)
        if callable(adder):
            adder(text)

    # -- clipboard export ------------------------------------------------------------
    def copy_snapshot(self, snap: Any, *, automatic: bool = False) -> str:
        """Copy the one-line summary of ``snap`` (Settings > Export format) to the clipboard,
        play the export sound and confirm it on the overlay and in the status bar."""
        from mnmparse.export import format_from_config, format_snapshot

        try:
            text = format_snapshot(snap, format_from_config(self.cfg))
        except Exception:  # noqa: BLE001 - a bad template must never break the app
            log.exception("export failed")
            text = ""
        if not text:
            return ""
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return ""
        clipboard.setText(text)
        sound = str(getattr(self.cfg, "export_sound", "") or "")
        if sound:
            self.play_sound(sound)
        if self.overlay is not None and callable(getattr(self.overlay, "flash_copied", None)):
            self.overlay.flash_copied()
        if self.window is not None:
            self.window.show_message(f"{'Fight ended, copied' if automatic else 'Copied'}: {text}")
        log.info("copied to the clipboard: %s", text)
        return text

    def _on_encounter_closed_export(self, snap: Any) -> None:
        """A fight ended: copy it when Settings > Export says so (only fights of our group)."""
        from mnmparse.export import format_from_config, has_people

        if snap is None or not bool(getattr(self.cfg, "export_auto", False)):
            return
        if not getattr(snap, "ours", True) or not has_people(snap, format_from_config(self.cfg)):
            return  # another group's fight, or nobody to list (the group dealt no damage, say)
        self.copy_snapshot(snap, automatic=True)

    def set_group_override(self, name: str, in_group: Any) -> None:
        """Right-click > count a person in or out of the group (the engine's party roster)."""
        setter = getattr(self.engine, "set_group_override", None)
        if not callable(setter):
            return
        state = None if in_group is None else bool(in_group)
        try:
            setter(name, state)
        except Exception:  # noqa: BLE001
            log.exception("group override failed")
            return
        if self.window is not None:
            text = {True: f"{name} now counts as your group", False: f"{name} no longer counts as your group",
                    None: f"The chat decides again whether {name} is in your group"}[state]
            running = bool(getattr(self.engine, "is_running", False))
            self.window.show_message(text + (" (the open fight and the recent fights of this zone are counted again)"
                                             if running else " (from the next capture)"))

    def set_pet_owner(self, pet: str, owner: Any) -> None:
        setter = getattr(self.engine, "set_pet_owner", None)
        if callable(setter):
            setter(pet, owner)
            if self.window is not None:
                page = self.window.page("live")
                if page is not None and callable(getattr(page, "set_pet_owner", None)):
                    page.set_pet_owner(pet, owner)
                self.window.show_message(f"{pet} attributed to {owner}" if owner else f"Pet assignment cleared for {pet}")

    def set_map_visible(self, visible: bool) -> None:
        if self.map_overlay is not None:
            self.map_overlay.setVisible(bool(visible))
        self._on_map_visibility(bool(visible))

    def _on_map_visibility(self, visible: bool) -> None:
        if not self._shut_down:
            self.settings.setValue("map/visible", visible)
        if self.window is not None:
            button = self.window._map_button
            button.blockSignals(True)
            button.setChecked(visible)
            button.blockSignals(False)

    def play_sound(self, name: str) -> None:
        """A built-in sound through the trigger audio (its volume and output device)."""
        if self.triggers is None or not name:
            return
        try:
            self.triggers.audio.play_builtin(name)
        except Exception:  # noqa: BLE001 - sound is a nicety
            log.debug("could not play %s", name, exc_info=True)

    def _on_message_for_overlay(self, msg: Any, ev: Any) -> None:
        if self.overlay is None:
            return
        player = getattr(self.cfg, "player_name", "") or ""
        try:
            _append_feed(self.overlay.append_feed, msg.text, ev.kind, is_player_action(ev, player),
                         None, is_player_target(ev, player))
        except Exception:  # noqa: BLE001
            log.debug("overlay.append_feed failed", exc_info=True)
        observe = getattr(self.overlay, "observe_event", None)
        if callable(observe):
            try:
                observe(ev)
            except Exception:  # noqa: BLE001
                log.debug("overlay.observe_event failed", exc_info=True)

    # -- overlay ----------------------------------------------------------------------

    def set_overlay_visible(self, visible: bool) -> None:
        """Show or hide the overlay (our own window; shown without activation)."""
        if self.overlay is None:
            if self.window is not None:
                self.window.set_overlay_state(False)
            return
        self.overlay.setVisible(bool(visible))
        self._on_overlay_visibility(bool(self.overlay.isVisible()))

    def toggle_overlay(self) -> None:
        """Tray: flip the overlay visibility."""
        current = self.overlay is not None and self.overlay.isVisible()
        self.set_overlay_visible(not current)

    def set_overlay_locked(self, locked: bool) -> None:
        """Lock (no move/resize) or unlock both overlay windows."""
        if self.overlay is None:
            self._on_overlay_locked(bool(locked))
            return
        try:
            self.overlay.set_locked(bool(locked))
        except Exception:  # noqa: BLE001
            log.exception("overlay.set_locked failed")
            return
        self._on_overlay_locked(_overlay_locked(self.overlay, bool(locked)))

    def toggle_overlay_lock(self) -> None:
        """Tray: flip the overlay lock."""
        self.set_overlay_locked(not self._current_overlay_lock())

    def _current_overlay_lock(self) -> bool:
        """Use the combat overlay's effective lock, or its saved preference if unavailable."""
        default = self.settings.value("overlay/locked", bool(getattr(self.cfg, "overlay_locked", True)), type=bool)
        return _overlay_locked(self.overlay, default)

    def _sync_map_appearance(self, *_args: Any) -> None:
        """Keep the map frame and header consistent with the combat overlay."""
        if self.map_overlay is None:
            return
        opacity = getattr(self.overlay, "opacity", None)
        font_scale = getattr(self.overlay, "font_scale", None)
        if opacity is None:
            opacity = self.settings.value("overlay/opacity", float(getattr(self.cfg, "overlay_opacity", 0.85)), type=float)
        if font_scale is None:
            font_scale = self.settings.value("overlay/font_scale", float(getattr(self.cfg, "overlay_font_scale", 1.0)), type=float)
        self.map_overlay.set_appearance(float(opacity), float(font_scale))

    def reset_overlay_position(self) -> None:
        """Settings page "Reset position": put the overlay back at its default geometry."""
        if self.overlay is None:
            return
        if _call_first(self.overlay, ("reset_position", "reset_geometry", "reset_layout")):
            return
        # Fallback when the overlay offers no reset method: the spec's default geometry.
        self.settings.remove("overlay/geometry")
        self.overlay.setGeometry(1880, 60, 560, 330)

    def toggle_overlay_click_through(self) -> None:
        """Tray: flip the overlay between accepting the mouse and ignoring it."""
        overlay = self.overlay
        setter = getattr(overlay, "set_click_through", None)
        if overlay is None or not callable(setter):
            return
        setter(not bool(getattr(overlay, "click_through", False)))

    def _on_overlay_setting(self, key: str, value: Any) -> None:
        """A Settings > Overlay control changed: apply it to the overlay right away."""
        overlay = self.overlay
        if key == "locked":
            self.set_overlay_locked(bool(value))
            return
        if overlay is None:
            if key in ("opacity", "font_scale"):
                self.settings.setValue(f"overlay/{key}", float(value))
                self._sync_map_appearance()
                self._sync_overlay_settings()
            return
        try:
            if key == "visible":
                self.set_overlay_visible(bool(value))
            elif key == "click_through":
                overlay.set_click_through(bool(value))
            elif key == "opacity":
                overlay.set_opacity(float(value))
            elif key == "font_scale":
                overlay.set_font_scale(float(value))
            elif key == "tab":
                overlay.set_tab(str(value))
            elif key == "attack_bar":
                overlay.set_attack_bar_enabled(bool(value))
        except Exception:  # noqa: BLE001
            log.exception("applying overlay setting %s failed", key)

    def _sync_overlay_settings(self) -> None:
        """Mirror the overlay's current state into Settings > Overlay."""
        overlay = self.overlay
        page = self.window.page("settings") if self.window is not None else None
        sync = getattr(page, "sync_overlay", None)
        if not callable(sync):
            return
        if overlay is None:
            sync(locked=self._current_overlay_lock(),
                 opacity=self.settings.value("overlay/opacity", self.cfg.overlay_opacity, type=float),
                 font_scale=self.settings.value("overlay/font_scale", self.cfg.overlay_font_scale, type=float))
            return
        try:
            sync(
                visible=overlay.isVisible(),
                locked=overlay.locked,
                click_through=overlay.click_through,
                opacity=overlay.opacity,
                font_scale=overlay.font_scale,
                tab=overlay.tab,
                attack_bar=overlay.attack_bar.enabled,
            )
        except Exception:  # noqa: BLE001
            log.debug("settings sync failed", exc_info=True)

    def _on_overlay_click_through(self, enabled: bool) -> None:
        if self.tray is not None:
            self.tray.set_overlay_click_through(bool(enabled))

    def _on_overlay_locked(self, locked: bool) -> None:
        self.settings.setValue("overlay/locked", bool(locked))
        if self.map_overlay is not None:
            self.map_overlay.set_locked(bool(locked))
        if self.window is not None:
            self.window.set_lock_state(locked)
        if self.tray is not None:
            self.tray.set_overlay_locked(locked)
        self._sync_overlay_settings()

    def _on_overlay_visibility(self, visible: bool) -> None:
        if self.window is not None:
            self.window.set_overlay_state(visible)
        if self.tray is not None:
            self.tray.set_overlay_visible(visible)
        # The preference is the user's choice, not the teardown: shutdown() closes the
        # overlay (hideEvent -> visibility_changed(False)) and must not turn it off for
        # the next launch; the self-test forces it on and must not persist that either.
        if not self._selftest and not self._shut_down:
            self.settings.setValue("main/overlay_visible", bool(visible))

    # -- window / tray -------------------------------------------------------------------

    def show_window(self) -> None:
        """Bring the main window back from the tray (it is our own window)."""
        if self.window is None:
            return
        self.window.reveal()  # our main window only; never the overlay or the game

    def hide_window(self) -> None:
        """Hide the main window to the tray."""
        if self.window is not None:
            self.window.hide()

    def _on_minimised_to_tray(self) -> None:
        if self.tray is None:
            return
        self.tray.set_window_visible(False)
        if not self.settings.value("tray/balloon_shown", False, type=bool):
            self.settings.setValue("tray/balloon_shown", True)
            self.tray.show_minimised_balloon()

    def on_config_changed(self, cfg: Config) -> None:
        """The Settings page saved ``cfg``: adopt it in the shell and the overlay."""
        self.cfg = cfg
        if self.window is not None:
            self.window.set_config(cfg)
        if self.overlay is not None:
            try:
                _call_first(self.overlay, ("set_opacity",), float(getattr(cfg, "overlay_opacity", 0.85)))
                _call_first(self.overlay, ("set_font_scale",), float(getattr(cfg, "overlay_font_scale", 1.0)))
                _call_first(self.overlay, ("set_tab",), str(getattr(cfg, "overlay_tab", "damage")))
                _call_first(self.overlay, ("set_show_other_groups",), bool(getattr(cfg, "show_other_groups", False)))
                _call_first(self.overlay, ("set_config",), cfg)
            except Exception:  # noqa: BLE001
                log.exception("applying overlay settings failed")
            enabled = bool(getattr(cfg, "overlay_enabled", False))
            self.settings.setValue("main/overlay_visible", enabled)
            self.set_overlay_visible(enabled)
        else:
            self.settings.setValue("overlay/opacity", float(getattr(cfg, "overlay_opacity", 0.85)))
            self.settings.setValue("overlay/font_scale", float(getattr(cfg, "overlay_font_scale", 1.0)))
        self.set_overlay_locked(bool(getattr(cfg, "overlay_locked", True)))
        self._sync_map_appearance()
        log.info("Configuration updated from the Settings page")

    # -- shutdown ------------------------------------------------------------------------

    def request_quit(self) -> None:
        """Quit cleanly from anywhere (tray, Ctrl+Q, self-test timer)."""
        if self.window is not None:
            self.window.prepare_quit()
            self.window.save_state()
        # QApplication.quit() closes windows before emitting aboutToQuit. Freeze
        # preferences first so those hide events cannot turn an open map into a
        # saved "closed" map on the next launch (including an update restart).
        self.shutdown()
        self.quit()

    def shutdown(self) -> None:
        """``aboutToQuit``: stop the engine, hide the windows, flush settings (idempotent)."""
        if self._shut_down:
            return
        self._shut_down = True
        log.info("Shutting down")
        if self._selftest and self._last_status:
            log.info("Self-test status at exit: %s", self._last_status)
        self.stop_capture()
        if self.app_updates is not None:
            self.app_updates.shutdown()
        if self.map_downloads is not None:
            self.map_downloads.shutdown()
        if self.map_overlay is not None:
            self.map_overlay.shutdown()
        if self.overlay is not None:
            try:
                self.overlay.close()
            except Exception:  # noqa: BLE001
                log.debug("overlay.close failed", exc_info=True)
        if self.window is not None:
            self.window.save_state()
        if self.tray is not None:
            self.tray.hide()
        self.settings.sync()
        log.info("Bye")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    """``python -m mnmparse.app [-v] [--selftest-seconds N] [--config PATH]``."""
    parser = argparse.ArgumentParser(
        prog="python -m mnmparse.app",
        description=f"{APP_NAME}: desktop app for the Monsters & Memories screen-read combat parser.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG logging in logs/app.log")
    parser.add_argument(
        "--console",
        action="store_true",
        help="keep an attached console window open (by default the app detaches from it; the Feed page shows the messages)",
    )
    parser.add_argument(
        "--selftest-seconds",
        type=float,
        default=None,
        metavar="N",
        help="start capture, show the overlay and quit after N seconds (exit code 0)",
    )
    parser.add_argument("--config", metavar="PATH", default=None, help="config JSON (default: config.json)")
    parser.add_argument(
        "--probe-stalls",
        action="store_true",
        help="log slow GUI events, slow garbage collections and the attack bar's frame timing (logs/app.log)",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    return parser


def _ensure_std_streams() -> None:
    """Under ``pythonw.exe`` stdout/stderr are ``None``; give them a sink so nothing crashes."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except OSError:
                pass


def _detach_console() -> None:
    """Close the console window this process may have been started from (Windows).

    The app has no use for a console: messages live on the Feed page and diagnostics in
    logs/app.log.  Only our own process is affected (``FreeConsole`` detaches it); the
    standard streams are re-pointed at the null device so later prints cannot fail.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleWindow():
            kernel32.FreeConsole()
            sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - lives for the process
            sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    except Exception:  # noqa: BLE001 - never let this stop the app
        pass


def run(argv: Sequence[str] | None = None) -> int:
    """Run the desktop app; returns the process exit code."""
    _ensure_std_streams()
    # The capture and engine threads keep Python's interpreter lock busy, and every Qt call a
    # GUI callback makes can hand the lock over and wait up to the switch interval to get it
    # back (5 ms by default): a paint of a few dozen calls then took 20-40 ms and the overlay
    # animations stuttered.  1 ms bounds each of those waits.
    sys.setswitchinterval(GIL_SWITCH_INTERVAL_S)
    args = build_arg_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if not (args.console or args.verbose or args.selftest_seconds is not None):
        _detach_console()
    log_path = setup_logging(args.verbose)
    log.info("%s %s starting (python %s, log %s)", APP_NAME, APP_VERSION, sys.version.split()[0], log_path)
    for module, error in _IMPORT_ERRORS.items():  # recorded at import time, before the log existed
        log.warning("%s unavailable: %s", module, error)
    cfg = load_config(args.config)
    for problem in cfg.problems():
        log.warning("config: %s", problem)

    if args.probe_stalls:
        from mnmparse.app import probe

        probe.enable()
        probe.instrument()
        app: App = type("StallProbeApp", (probe.StallProbeMixin, App), {})([sys.argv[0]])
        report = QTimer(app)
        report.setInterval(int(probe.REPORT_S * 1000))
        report.timeout.connect(probe.report)
        report.start()
    else:
        app = App([sys.argv[0]])
    try:
        app.bootstrap(cfg, selftest_seconds=args.selftest_seconds)
    except Exception:
        log.exception("Startup failed")
        app.shutdown()
        return 1
    code = app.exec()
    app.shutdown()
    log.info("Exit code %d", code)
    return int(code)


__all__ = [
    "App",
    "MainWindow",
    "PAGES",
    "build_arg_parser",
    "is_player_action",
    "is_player_target",
    "nav_icon",
    "run",
    "setup_logging",
]
