"""Unique atomic writes and conservative retention of application-owned folders."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time

OWNERSHIP_FILE = ".pnut-owned.json"
_replace_lock = threading.Lock()


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".pnut-write-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        with _replace_lock:
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value: dict) -> None:
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def mark_owned(directory: Path, kind: str, *, pinned: bool = False) -> None:
    atomic_json(Path(directory) / OWNERSHIP_FILE,
                {"schema": 1, "kind": kind, "created": time.time(), "pinned": pinned,
                 "lease_until": time.time() + 3600 if pinned else 0})


def prune_owned(root: Path, kind: str, *, keep: int = 3, max_age_days: int = 30,
                protected: tuple[Path, ...] = ()) -> list[Path]:
    """Delete only direct owned children, refusing links anywhere in their trees."""
    root = Path(root).absolute()
    if not root.is_dir() or any(p.is_symlink() or p.is_junction() for p in (root, *root.parents)):
        return []
    protected_paths = {Path(p).absolute() for p in protected}
    candidates = []
    for child in root.iterdir():
        try:
            if child in protected_paths or not child.is_dir() or child.is_symlink() or child.is_junction():
                continue
            marker = child / OWNERSHIP_FILE
            if marker.is_symlink() or marker.is_junction() or marker.stat().st_size > 4096:
                continue
            record = json.loads(marker.read_text(encoding="utf-8-sig"))
            if (record.get("schema") != 1 or record.get("kind") != kind
                    or type(record.get("created")) not in (int, float) or not math.isfinite(record["created"])):
                continue
            if record.get("pinned"):
                lease = record.get("lease_until", float("inf"))
                if type(lease) not in (int, float) or lease > time.time():
                    continue
            candidates.append((record["created"], child))
        except (OSError, ValueError, AttributeError, OverflowError):
            continue
    removed = []
    cutoff = time.time() - max_age_days * 86400
    for index, (created, child) in enumerate(sorted(candidates, reverse=True)):
        if index < keep and created >= cutoff:
            continue
        try:
            if not child.resolve().is_relative_to(root.resolve()):
                continue
            if any(p.is_symlink() or p.is_junction() for p in child.rglob("*")):
                continue
            shutil.rmtree(child)
            removed.append(child)
        except OSError:
            continue
    return removed
