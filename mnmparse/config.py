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
import math
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from mnmparse.export import DEFAULT_PRESET, PRESETS, SORTS
from mnmparse.triggers import BUILTIN_SOUNDS

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
        overlay_locked: Prevent moving/resizing when ``True``; starts unlocked by default.
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
    revenge_alert_enabled: bool = True
    revenge_alert_flash: bool = True
    revenge_alert_sound: str = "Dink"  #: empty disables the alert tone
    revenge_alert_volume: int = 25  #: percent of the trigger master volume
    revenge_alert_cooldown: float = 30.0  #: seconds without a sighting before alerting again
    revenge_alert_duration: float = 4.0  #: fading notification duration, in seconds
    capture_profiles: dict[str, Any] = dataclasses.field(default_factory=dict)
    active_profile: str = "Default"
    history_recent_fights: int = 100
    history_retention_days: int = 30
    encounter_timeout_s: float = 12.0
    stats_interval_s: float = 5.0
    overlay_enabled: bool = False
    overlay_opacity: float = 0.85
    overlay_locked: bool = False
    overlay_font_scale: float = 1.0
    overlay_tab: str = "damage"
    start_capture_on_launch: bool = True
    minimize_to_tray: bool = True
    feed_max_lines: int = 500
    include_personal: bool = False  #: count lines only the viewer gets (skill-ups, faction, XP) in the Session tab
    player_status_enabled: bool = True  #: read the mini stat panel
    player_status_separate: bool = True  #: read a separate region after it is selected
    player_status_crop: tuple[int, int, int, int] | None = None  #: None means no separate region selected
    player_status_xp: bool = True  #: estimate experience from its yellow/blue bar
    low_health_enabled: bool = True  #: alert once when confirmed health falls below the threshold
    low_health_threshold: int = 10  #: health percentage; two fresh readings below it confirm an alert
    low_health_flash: bool = True  #: briefly color the screen edges red
    low_health_sound: str = "Alert"
    low_health_volume: int = 80  #: percent of the trigger master volume
    show_other_groups: bool = False  #: show nearby outsider rows and fights without group participation
    dummy_fix: bool = False  #: unreadable hit / heal numbers count as the zone's average (estimates)
    attack_bar: bool = True  #: show the auto-attack timer bar under the overlay
    npc_enabled: bool = True  #: considered NPC details, detachable under the overlay
    npc_level: bool = True
    npc_common_drops: bool = False
    npc_unique_drops: bool = True
    npc_quests: bool = False
    npc_reputation: bool = False
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
        if self.player_status_crop is not None and not _valid_crop(self.player_status_crop):
            issues.append("player_status_crop must be null or four integers with positive size and nonnegative origin")
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
        for name in ("revenge_alert_enabled", "revenge_alert_flash", "player_status_enabled", "player_status_separate", "player_status_xp",
                     "low_health_enabled", "low_health_flash"):
            if type(getattr(self, name)) is not bool:
                issues.append(f"{name} must be true or false")
        if self.revenge_alert_sound not in ("", *BUILTIN_SOUNDS):
            issues.append("revenge_alert_sound must be a built-in sound or empty (silent)")
        if type(self.revenge_alert_volume) is not int or not 0 <= self.revenge_alert_volume <= 100:
            issues.append("revenge_alert_volume must be an integer between 0 and 100")
        if self.low_health_sound not in ("", *BUILTIN_SOUNDS):
            issues.append("low_health_sound must be a built-in sound or empty (silent)")
        if type(self.low_health_volume) is not int or not 0 <= self.low_health_volume <= 100:
            issues.append("low_health_volume must be an integer between 0 and 100")
        if type(self.low_health_threshold) is not int or not 1 <= self.low_health_threshold <= 100:
            issues.append("low_health_threshold must be an integer between 1 and 100")
        for name, minimum, maximum in (("revenge_alert_cooldown", 0, 3600), ("revenge_alert_duration", 1, 60)):
            if not _bounded_number(getattr(self, name), minimum, maximum):
                issues.append(f"{name} must be a finite number between {minimum} and {maximum}")
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


def _bounded_number(value: Any, minimum: float, maximum: float) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and minimum <= value <= maximum
    except OverflowError:
        return False


def _valid_crop(value: Any) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(n) is not int for n in value):
        return False
    left, top, right, bottom = value
    return 0 <= left < right and 0 <= top < bottom


def _coerce(name: str, value: Any, default: Any) -> Any:
    """Coerce a JSON value to the type of the dataclass field ``name``.

    Raises:
        ValueError: if the value cannot be converted.
    """
    if name == "capture_profiles":
        from mnmparse.profiles import validate_profiles
        return validate_profiles(value)
    if name in {"revenge_days", "revenge_entries", "revenge_alert_volume", "low_health_volume", "low_health_threshold"}:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("expected a whole number")
        number = int(value)
        maximum = {"revenge_days": 3650, "revenge_entries": 1000, "revenge_alert_volume": 100,
                   "low_health_volume": 100, "low_health_threshold": 100}[name]
        minimum = 1 if name == "low_health_threshold" else 0
        if not minimum <= number <= maximum:
            if minimum:
                raise ValueError(f"expected {minimum} through {maximum}")
            label = "0" if name in {"revenge_alert_volume", "low_health_volume"} else "0 (all)"
            raise ValueError(f"expected {label} through {maximum}")
        return number
    if name in {"revenge_alert_cooldown", "revenge_alert_duration"}:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("expected a number")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError("expected a finite number") from exc
        minimum, maximum = (0, 3600) if name == "revenge_alert_cooldown" else (1, 60)
        if not _bounded_number(number, minimum, maximum):
            raise ValueError(f"expected {minimum} through {maximum}")
        return number
    if name in {"revenge_alert_sound", "low_health_sound"}:
        if value not in ("", *BUILTIN_SOUNDS):
            raise ValueError("expected a built-in sound or empty (silent)")
        return value
    if name in {"revenge_alert_enabled", "revenge_alert_flash", "player_status_enabled", "player_status_separate", "player_status_xp",
                "low_health_enabled", "low_health_flash"} and not isinstance(value, (bool, str)):
        if type(value) is not int or value not in (0, 1):
            raise ValueError("expected true or false")
    if name == "crop":
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            raise ValueError("crop must be a list of four integers [left, top, right, bottom]")
        return tuple(int(v) for v in value)
    if name == "player_status_crop":
        if value is None:
            return None
        if (not isinstance(value, (list, tuple)) or len(value) != 4
                or any(type(n) not in (int, str) for n in value)):
            raise ValueError("expected null or four integer coordinates")
        crop = tuple(int(n) for n in value)
        if not _valid_crop(crop):
            raise ValueError("expected a positive rectangle with a nonnegative origin")
        return crop
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
    # Old profiles and imported preferences never silently opt out of Carebear Mode.
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
    return _migrate_saved_config(loaded, data)


def _migrate_saved_config(loaded: Config, data: dict[str, Any]) -> Config:
    """Preserve prior capture choices before materializing new defaults."""
    # The setup wizard was introduced after existing users had already saved
    # their character and crop. Do not force them through first-run calibration.
    if "setup_complete" not in data and loaded.player_name.strip() and loaded.window_title.strip():
        try:
            left, top, right, bottom = _coerce("crop", data.get("crop"), loaded.crop)
            calibrated = (0 <= left < right and 0 <= top < bottom
                          and loaded.capture_backend in CAPTURE_BACKENDS
                          and loaded.ocr_engine in OCR_ENGINES)
        except (TypeError, ValueError):
            calibrated = False
        if calibrated:
            loaded = dataclasses.replace(loaded, setup_complete=True)
    # Preserve the mode of installed, calibrated versions before separate capture
    # had its own flag. New first-run configurations keep separate capture pending.
    if "player_status_separate" not in data and loaded.setup_complete:
        loaded = dataclasses.replace(loaded, player_status_separate=loaded.player_status_crop is not None)
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
    data["player_status_crop"] = list(cfg.player_status_crop) if cfg.player_status_crop is not None else None
    from mnmparse.storage import atomic_json
    atomic_json(cfg_path, data)
    log.info("Saved config to %s", cfg_path)
