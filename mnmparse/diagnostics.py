"""Export a local OCR diagnosis ZIP with recent evidence and a fresh Combat crop.

The report never uploads anything or includes a full game-window screenshot. Capture
failure still produces a useful JSON report; exception messages and traceback paths are
not copied into it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from contextlib import nullcontext
import platform
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mnmparse import __version__
from mnmparse.config import Config
from mnmparse.privacy import casual_enabled, safe_config_data


def _technical(value: Any) -> Any:
    """Unknown strings (including OCR evidence) have no place in a Casual report."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, dict):
        return {key: _technical(item) for key, item in value.items()
                if isinstance(key, str) and not isinstance(item, str)}
    if isinstance(value, (list, tuple)):
        return [_technical(item) for item in value if not isinstance(item, str)]
    return None


def _restrict_report(report: dict[str, Any]) -> dict[str, Any]:
    allowed = {key: report[key] for key in ("format", "format_version", "created_at_utc", "app_version", "environment", "crop") if key in report}
    for key in ("saved_settings", "effective_settings", "edited_settings"):
        allowed[key] = safe_config_data(report.get(key, {}))
    for key in ("active_ocr", "pending_ocr_settings", "runtime", "tracker", "preview_metadata"):
        allowed[key] = _technical(report.get(key, {}))
    allowed["privacy_mode"] = "casual"
    allowed["capture"] = {"attempted": False, "available": False, "reason": "Screenshots and raw OCR evidence are hidden in Casual Mode."}
    allowed["export_scope"] = "Technical counters and settings only. Raw source files remain local."
    allowed["limitations"] = ["This report cannot establish whether invisible chat lines were missed."]
    return allowed


def _config_data(cfg: Config) -> dict[str, Any]:
    data = dataclasses.asdict(cfg)
    data["crop"] = list(cfg.crop)
    return data


def _metadata(value: Any, depth: int = 0) -> Any:
    """Keep optional GUI metadata small and JSON-safe, without object representations."""
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, dict):
        return {str(key)[:100]: _metadata(item, depth + 1)
                for key, item in list(value.items())[:50] if isinstance(key, str)}
    if isinstance(value, (list, tuple)):
        return [_metadata(item, depth + 1) for item in value[:50]]
    return None


def write_ocr_diagnosis(
    path: str | Path,
    engine: Any,
    edited_config: Config | None = None,
    preview_metadata: dict[str, Any] | None = None,
) -> Path:
    """Write report.json and, when possible, combat-crop.png; return the absolute ZIP path.

    Call from a background worker: ``grab_frame`` may wait briefly for a fresh game frame.
    The image uses the effective live crop from the engine snapshot. Unsaved edits and
    previously captured preview metadata are recorded separately and supply no image.
    """
    target = Path(path).expanduser().resolve()
    snapshot = engine.ocr_diagnosis()
    saved = snapshot["saved_settings"]
    effective = snapshot["effective_settings"]
    edited = _config_data(edited_config) if edited_config is not None else dict(saved)
    differences = {key: {"saved": saved.get(key), "edited": value}
                   for key, value in edited.items() if saved.get(key) != value}
    crop = tuple(int(value) for value in effective["crop"])
    default_crop = list(Config().crop)
    report = {
        "format": "pnut-ocr-diagnosis",
        "format_version": 1,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version": __version__,
        "environment": {"os": platform.platform(), "python_version": platform.python_version(),
                        "python_implementation": platform.python_implementation()},
        **snapshot,
        "edited_settings": edited,
        "saved_vs_edited_differences": differences,
        "crop": {
            "saved": list(saved["crop"]), "effective": list(crop), "edited": list(edited["crop"]),
            "default": default_crop, "differs_from_default": list(crop) != default_crop,
            "edited_differs_from_saved": edited["crop"] != saved["crop"],
            "manual_adjustment_history": "not_tracked",
        },
        "preview_metadata": _metadata(preview_metadata or {}),
        "capture": {
            "attempted": True, "available": False, "reason": None,
            "frame_dimensions": None, "crop_valid_within_frame": None,
            "crop_clamped": None, "image_name": None, "image_dimensions": None,
        },
        "export_scope": "Local export only. The image, if available, contains only the configured Combat crop.",
    }
    report["limitations"] = list(snapshot.get("limitations", [])) + [
        "Crop history is not tracked; differing from the default does not prove it was manually adjusted.",
        "Preview metadata describes a previous UI snapshot; the attached image is captured fresh for this export.",
    ]
    capture = report["capture"]
    png: bytes | None = None
    restricted = (edited_config is not None and casual_enabled(edited_config)) or casual_enabled(getattr(engine, "config", None))
    try:
        frame = None if restricted else engine.grab_frame()
    except Exception as exc:  # noqa: BLE001 - useful JSON must survive a failed capture
        capture["reason"] = f"Fresh game capture failed ({type(exc).__name__})."
        capture["failure_type"] = type(exc).__name__
        frame = None
    if frame is None:
        if capture["reason"] is None:
            capture["reason"] = "No fresh game frame was available. The game may be closed or capture unavailable."
    else:
        try:
            from mnmparse.capture import crop_frame

            height, width = (int(value) for value in frame.shape[:2])
            capture["frame_dimensions"] = {"width": width, "height": height}
            left, top, right, bottom = crop
            within = 0 <= left < right <= width and 0 <= top < bottom <= height
            capture["crop_valid_within_frame"] = within
            capture["crop_clamped"] = not within
            image = crop_frame(frame, crop)
            if not within:
                capture["reason"] = "The configured Combat crop is outside the fresh game-frame bounds."
            elif left == top == 0 and right == width and bottom == height:
                capture["reason"] = "The configured crop covers the whole game frame; a full-window image is excluded."
            else:
                import cv2

                ok, encoded = cv2.imencode(".png", image)
                if not ok:
                    raise ValueError("PNG encoding failed")
                png = encoded.tobytes()
                capture.update({
                    "available": True, "image_name": "combat-crop.png",
                    "image_dimensions": {"width": int(image.shape[1]), "height": int(image.shape[0])},
                })
        except Exception as exc:  # noqa: BLE001 - no frame/encoder details or paths in the report
            capture["reason"] = f"The Combat crop image could not be prepared ({type(exc).__name__})."
            capture["failure_type"] = type(exc).__name__

    target.parent.mkdir(parents=True, exist_ok=True)
    if restricted or casual_enabled(getattr(engine, "config", None)):
        report = _restrict_report(report)
        png = None
    with tempfile.NamedTemporaryFile(prefix=".ocr-diagnosis-", suffix=".tmp", dir=target.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
            if png is not None:
                archive.writestr("combat-crop.png", png)
        # Serialize publication with mode changes. A report prepared while full
        # mode was active must not be published after Casual has taken effect.
        guard = getattr(engine, "_lock", None)
        with guard if guard is not None else nullcontext():
            if casual_enabled(getattr(engine, "config", None)) and not restricted:
                report = _restrict_report(report)
                with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                    archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
            os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return target
