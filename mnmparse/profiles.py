"""Validated named capture settings, independent of presentation/privacy mode."""
from __future__ import annotations

import dataclasses
import math
from typing import Any

from mnmparse.config import Config

LEGACY_PROFILE_FIELDS = ("window_title", "capture_backend", "crop", "fps", "ocr_engine", "ocr_scale",
                         "preprocess", "player_name", "log_dir")
STATS_PROFILE_FIELDS = (*LEGACY_PROFILE_FIELDS, "player_status_crop")
PROFILE_FIELDS = (*STATS_PROFILE_FIELDS, "player_status_separate")


def validate_profiles(profiles: Any) -> dict:
    if not isinstance(profiles, dict) or len(profiles) > 100:
        raise ValueError("Capture profiles must be an object with at most 100 entries.")
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError("Capture profile names must be between 1 and 80 characters.")
        if (not isinstance(profile, dict) or set(profile) - {"schema", "settings", "calibration"}
                or profile.get("schema") != 1 or not isinstance(profile.get("settings"), dict)
                or set(profile["settings"]) not in (set(LEGACY_PROFILE_FIELDS), set(STATS_PROFILE_FIELDS), set(PROFILE_FIELDS))):
            raise ValueError("Capture profile schema is unsupported.")
        values = profile["settings"]
        defaults = Config()
        for key in PROFILE_FIELDS:
            value = values.get(key)
            if key == "player_status_separate":
                if key in values and type(value) is not bool:
                    raise ValueError("Capture profile separate-region setting must be true or false.")
                continue
            if key == "player_status_crop" and value is None:
                continue
            if key in ("crop", "player_status_crop"):
                if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(n) is not int for n in value):
                    raise ValueError("Capture profile crop must contain four integers.")
            elif type(getattr(defaults, key)) is float:
                try:
                    finite = type(value) in (int, float) and math.isfinite(value)
                except OverflowError:
                    finite = False
                if not finite:
                    raise ValueError("Capture profile numeric settings must be finite.")
            elif not isinstance(value, str) or len(value) > 4096:
                raise ValueError("Capture profile text settings are invalid.")
        cfg = dataclasses.replace(defaults, **values)
        if not cfg.window_title.strip() or cfg.problems():
            raise ValueError("Capture profile settings are invalid.")
        calibration = profile.get("calibration")
        if calibration is not None:
            if (not isinstance(calibration, dict) or set(calibration) != {"width", "height"}
                    or any(type(calibration[key]) is not int or not 1 <= calibration[key] <= 32768 for key in calibration)):
                raise ValueError("Capture profile calibration is invalid.")
    return profiles


def capture_profile(cfg: Config, dimensions: tuple[int, int] | None = None) -> dict:
    values = {key: getattr(cfg, key) for key in PROFILE_FIELDS}
    values["crop"] = list(cfg.crop)
    values["player_status_crop"] = list(cfg.player_status_crop) if cfg.player_status_crop is not None else None
    profile = {"schema": 1, "settings": values}
    if dimensions is not None:
        profile["calibration"] = dict(width=dimensions[0], height=dimensions[1])
    validate_profiles({"Profile": profile})
    return profile


def apply_profile(cfg: Config, name: str) -> Config:
    profiles = validate_profiles(cfg.capture_profiles)
    values = dict(profiles[name]["settings"])
    values["crop"] = tuple(values["crop"])
    stats_crop = values.get("player_status_crop")
    values["player_status_crop"] = tuple(stats_crop) if stats_crop is not None else None
    values.setdefault("player_status_separate", stats_crop is not None)
    return dataclasses.replace(cfg, **values, active_profile=name)
