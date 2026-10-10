"""Runtime configuration: the :class:`Config` dataclass plus JSON load/save.

The configuration file lives next to the package by default
(``<project>/config.json``).  Loading never raises on a bad or missing file:
defaults are used and problems are logged, so a typo in the JSON cannot stop
the tool from starting.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from mnmparse.export import DEFAULT_PRESET, PRESETS, SORTS

log = logging.getLogger(__name__)


def _find_project_root() -> Path:
    """Locate the folder that holds ``config.json`` and ``logs/``.

    From source this is the directory that contains the ``mnmparse`` package.
    Inside a PyInstaller build (``sys.frozen``) the package lives in the
    bundle's internal folder, so the root is the folder of the executable
    instead: ``config.json`` and ``logs`` then sit next to ``PNUT M&M.exe``.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


#: Project root: the directory that contains the ``mnmparse`` package (or the
#: executable's folder when running as a frozen PyInstaller build).
PROJECT_ROOT: Path = _find_project_root()
#: Default location of the configuration file ("next to the package").
DEFAULT_CONFIG_PATH: Path = PROJECT_ROOT / "config.json"

CAPTURE_BACKENDS: tuple[str, ...] = ("wgc", "mss")
OCR_ENGINES: tuple[str, ...] = ("windows", "rapid")
PREPROCESS_MODES: tuple[str, ...] = ("none", "gray", "maxchannel")
OVERLAY_TABS: tuple[str, ...] = ("overview", "damage", "healing", "taken", "session", "feed", "revenge")


@dataclass
class Config:
    """All user-tunable settings.

    Attributes:
        window_title: Title of the game window to capture.
        capture_backend: ``"wgc"`` (Windows Graphics Capture of the window) or
            ``"mss"`` (desktop region grab of the window rectangle).
        crop: ``(left, top, right, bottom)`` of the chat text area in
            game-window pixels.
        fps: How many frames per second the live loop captures and OCRs.
        ocr_engine: ``"windows"`` (Windows.Media.Ocr) or ``"rapid"`` (RapidOCR).
        ocr_scale: Upscale factor applied before OCR (1.0 to 2.0 is sensible).
        preprocess: ``"none"``, ``"gray"`` or ``"maxchannel"``.
        log_dir: Directory for the log files; relative paths are resolved
            against the project root.
        player_name: Optional character name so "You"/"YOU" can be attributed.
        encounter_timeout_s: Seconds without damage before an encounter closes.
        stats_interval_s: Seconds between console stats tables.
        overlay_enabled: Show the in-game overlay window when the app starts.
        overlay_opacity: Background alpha of the overlay panel, 0.0 to 1.0.
        overlay_locked: Overlay starts click-through (locked) when ``True``.
        overlay_font_scale: Multiplier applied to the overlay's base font size.
        overlay_tab: Overlay tab shown at start: ``"damage"``, ``"healing"``,
            ``"taken"`` or ``"feed"``.
        start_capture_on_launch: Start the capture pipeline as soon as the
            desktop app opens.
        minimize_to_tray: Closing the main window hides it to the tray instead
            of quitting.
        feed_max_lines: Maximum number of lines the message feed keeps.
    """

    window_title: str = "Monsters and Memories"
    capture_backend: str = "wgc"
    crop: tuple[int, int, int, int] = (0, 60, 700, 600)
    fps: float = 4.0
    ocr_engine: str = "windows"
    ocr_scale: float = 1.0
    preprocess: str = "maxchannel"
    log_dir: str = "logs"
    player_name: str = ""
    config_schema: int = 1
    casual_mode: bool = True
    casual_mode_confirmed: bool = False
    reduced_motion: bool = False
    setup_complete: bool = False
    revenge_enabled: bool = False
    revenge_days: int = 30  #: display recent activity only; 0 shows all saved dates
    revenge_entries: int = 100  #: newest entries to display; 0 shows all saved players
    capture_profiles: dict[str, Any] = dataclasses.field(default_factory=dict)
    active_profile: str = "Default"
    history_recent_fights: int = 100
    history_retention_days: int = 30
    encounter_timeout_s: float = 12.0
    stats_interval_s: float = 5.0
    overlay_enabled: bool = False
    overlay_opacity: float = 0.85
    overlay_locked: bool = True
    overlay_font_scale: float = 1.0
    overlay_tab: str = "damage"
    start_capture_on_launch: bool = True
    minimize_to_tray: bool = True
    feed_max_lines: int = 500
    include_personal: bool = False  #: count lines only the viewer gets (skill-ups, faction, XP) in the Session tab
    show_other_groups: bool = False  #: list fights nobody in the party took part in (other groups nearby)
    dummy_fix: bool = False  #: unreadable hit / heal numbers count as the zone's average (estimates)
    attack_bar: bool = True  #: show the auto-attack timer bar under the overlay
    log_break_minutes: float = 60.0  #: start a new log file after this long without messages
    log_max_mb: float = 5.0  #: start a new log file when the raw log reaches this size
    overlay_click_through: bool = False  #: overlay ignores the mouse entirely (no tabs, no tooltips)
    # Clipboard export (mnmparse.export): one line per fight, from these templates.
    export_auto: bool = False  #: explicitly opt in to automatic clipboard changes
    export_sound: str = "Dink"  #: built-in sound when a fight is copied ("" = silent)
    export_line: str = PRESETS[DEFAULT_PRESET].line
    export_actor: str = PRESETS[DEFAULT_PRESET].actor
    export_separator: str = PRESETS[DEFAULT_PRESET].separator
    export_sort: str = PRESETS[DEFAULT_PRESET].sort  #: damage / healing / taken / utility
    export_max_actors: int = PRESETS[DEFAULT_PRESET].max_actors

    def problems(self) -> list[str]:
        """Return human-readable validation problems (empty when valid)."""
        issues: list[str] = []
        if self.capture_backend not in CAPTURE_BACKENDS:
            issues.append(
                f"capture_backend must be one of {CAPTURE_BACKENDS}, got {self.capture_backend!r}"
            )
        if self.ocr_engine not in OCR_ENGINES:
            issues.append(f"ocr_engine must be one of {OCR_ENGINES}, got {self.ocr_engine!r}")
        if self.preprocess not in PREPROCESS_MODES:
            issues.append(f"preprocess must be one of {PREPROCESS_MODES}, got {self.preprocess!r}")
        try:
            left, top, right, bottom = self.crop
        except (TypeError, ValueError):
            issues.append(f"crop must be (left, top, right, bottom), got {self.crop!r}")
        else:
            if right <= left or bottom <= top or left < 0 or top < 0:
                issues.append(
                    f"crop must be (left, top, right, bottom) with positive size, got {self.crop!r}"
                )
        if self.fps <= 0:
            issues.append(f"fps must be > 0, got {self.fps!r}")
        if not 0.25 <= self.ocr_scale <= 4.0:
            issues.append(f"ocr_scale must be between 0.25 and 4.0, got {self.ocr_scale!r}")
        if self.encounter_timeout_s <= 0:
            issues.append(f"encounter_timeout_s must be > 0, got {self.encounter_timeout_s!r}")
        if self.stats_interval_s <= 0:
            issues.append(f"stats_interval_s must be > 0, got {self.stats_interval_s!r}")
        if not 0.0 <= self.overlay_opacity <= 1.0:
            issues.append(f"overlay_opacity must be between 0.0 and 1.0, got {self.overlay_opacity!r}")
        if not 0.5 <= self.overlay_font_scale <= 3.0:
            issues.append(
                f"overlay_font_scale must be between 0.5 and 3.0, got {self.overlay_font_scale!r}"
            )
        if self.overlay_tab not in OVERLAY_TABS:
            issues.append(f"overlay_tab must be one of {OVERLAY_TABS}, got {self.overlay_tab!r}")
        if self.feed_max_lines <= 0:
            issues.append(f"feed_max_lines must be > 0, got {self.feed_max_lines!r}")
        if not 1 <= self.history_recent_fights <= 1000:
            issues.append("history_recent_fights must be between 1 and 1000")
        if not 1 <= self.history_retention_days <= 3650:
            issues.append("history_retention_days must be between 1 and 3650")
        if type(self.revenge_days) is not int or not 0 <= self.revenge_days <= 3650:
            issues.append("revenge_days must be an integer between 0 (all) and 3650")
        if type(self.revenge_entries) is not int or not 0 <= self.revenge_entries <= 1000:
            issues.append("revenge_entries must be an integer between 0 (all) and 1000")
        if self.config_schema != 1:
            issues.append("config_schema must be 1")
        if self.capture_profiles:
            from mnmparse.profiles import validate_profiles
            try:
                validate_profiles(self.capture_profiles)
            except (TypeError, ValueError) as exc:
                issues.append(str(exc))
        if self.log_break_minutes <= 0:
            issues.append(f"log_break_minutes must be > 0, got {self.log_break_minutes!r}")
        if self.log_max_mb <= 0:
            issues.append(f"log_max_mb must be > 0, got {self.log_max_mb!r}")
        if self.export_sort not in SORTS:
            issues.append(f"export_sort must be one of {SORTS}, got {self.export_sort!r}")
        if not 1 <= int(self.export_max_actors) <= 40:
            issues.append(f"export_max_actors must be between 1 and 40, got {self.export_max_actors!r}")
        if not str(self.export_line or "").strip():
            issues.append("the export Line must not be empty")
        if not str(self.export_actor or "").strip():
            issues.append("the export Each person template must not be empty")
        if any("\n" in text for text in (self.export_line, self.export_actor, self.export_separator)):
            issues.append("export templates must be one line")
        return issues


def project_path(path: str | Path) -> Path:
    """Resolve ``path`` against the project root unless it is already absolute."""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Coerce a JSON value to the type of the dataclass field ``name``.

    Raises:
        ValueError: if the value cannot be converted.
    """
    if name == "capture_profiles":
        from mnmparse.profiles import validate_profiles
        return validate_profiles(value)
    if name in {"revenge_days", "revenge_entries"}:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("expected a whole number")
        number = int(value)
        maximum = 3650 if name == "revenge_days" else 1000
        if not 0 <= number <= maximum:
            raise ValueError(f"expected 0 (all) through {maximum}")
        return number
    if name == "crop":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("crop must be a list of four integers [left, top, right, bottom]")
        return tuple(int(v) for v in value)
    if isinstance(default, bool):  # bool is a subclass of int: check it first
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1", "yes", "on"):
                return True
            if lowered in ("false", "0", "no", "off", ""):
                return False
            raise ValueError("expected true or false")
        return bool(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("expected a number")
        return float(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, str):
        return "" if value is None else str(value)
    return value


def _apply_overrides(cfg: Config, data: dict[str, Any], source: str) -> Config:
    """Return a copy of ``cfg`` with the recognised keys of ``data`` applied."""
    known = {f.name for f in fields(Config)}
    updates: dict[str, Any] = {}
    for key, value in data.items():
        if key not in known:
            log.warning("%s: ignoring unknown setting %r", source, key)
            continue
        default = getattr(cfg, key)
        try:
            updates[key] = _coerce(key, value, default)
        except (TypeError, ValueError) as exc:
            log.warning("%s: bad value for %r (%s); keeping default %r", source, key, exc, default)
    merged = dataclasses.replace(cfg, **updates)
    # Old profiles and imported preferences never silently opt out of Casual Mode.
    if not merged.casual_mode and not merged.casual_mode_confirmed:
        merged.casual_mode = True
    if merged.casual_mode:
        merged.casual_mode_confirmed = False
    for issue in merged.problems():
        log.warning("%s: %s", source, issue)
    return merged


def load_config(path: str | None = None) -> Config:
    """Load the configuration: built-in defaults overridden by a JSON file.

    Args:
        path: Path of the JSON file; ``None`` means :data:`DEFAULT_CONFIG_PATH`.

    Returns:
        A :class:`Config`.  A missing or unreadable file yields the defaults
        (with a log message); unknown keys and bad values are skipped with a
        warning rather than raising.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = Config()
    if not cfg_path.is_file():
        log.info("No config file at %s; using defaults", cfg_path)
        return cfg
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log.warning("Could not read config %s (%s); using defaults", cfg_path, exc)
        return cfg
    if not isinstance(data, dict):
        log.warning("Config %s is not a JSON object; using defaults", cfg_path)
        return cfg
    log.debug("Loaded config from %s", cfg_path)
    loaded = _apply_overrides(cfg, data, source=str(cfg_path))
    # The setup wizard was introduced after existing users had already saved
    # their character and crop. Do not force them through first-run calibration.
    if "setup_complete" not in data and loaded.player_name.strip() and loaded.window_title.strip():
        try:
            left, top, right, bottom = _coerce("crop", data.get("crop"), cfg.crop)
            calibrated = (0 <= left < right and 0 <= top < bottom
                          and loaded.capture_backend in CAPTURE_BACKENDS
                          and loaded.ocr_engine in OCR_ENGINES)
        except (TypeError, ValueError):
            calibrated = False
        if calibrated:
            loaded = dataclasses.replace(loaded, setup_complete=True)
    return loaded


def save_config(cfg: Config, path: str | None = None) -> None:
    """Write ``cfg`` as pretty-printed JSON (atomically, via a temp file).

    Args:
        cfg: The configuration to persist.
        path: Destination; ``None`` means :data:`DEFAULT_CONFIG_PATH`.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = dataclasses.asdict(cfg)
    data["crop"] = [int(v) for v in cfg.crop]
    from mnmparse.storage import atomic_json
    atomic_json(cfg_path, data)
    log.info("Saved config to %s", cfg_path)
