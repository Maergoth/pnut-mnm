"""Build provenance embedded in portable releases and reported by startup gates."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
import re
import sys

from mnmparse import __version__


def validate_build_info(info: dict, *, version: str | None = None) -> dict:
    if (not isinstance(info, dict) or type(info.get("schema")) is not int or info.get("schema") != 1
            or not isinstance(info.get("version"), str) or info.get("version") != (version or __version__)
            or not isinstance(info.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", info["commit"])
            or not isinstance(info.get("dirty"), bool) or not isinstance(info.get("built_at"), str)):
        raise ValueError("Build provenance does not match the application's version.")
    try:
        if datetime.fromisoformat(info["built_at"]).tzinfo is None:
            raise ValueError("missing UTC offset")
    except ValueError as exc:
        raise ValueError("Build provenance timestamp is invalid.") from exc
    return info


def build_info() -> dict:
    if not getattr(sys, "frozen", False):
        return {"schema": 1, "version": __version__, "commit": "source", "dirty": True, "built_at": "source"}
    path = Path(sys._MEIPASS) / "build-info.json"
    return validate_build_info(json.loads(path.read_text(encoding="utf-8")))
