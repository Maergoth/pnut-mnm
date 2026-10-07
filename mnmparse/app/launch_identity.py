"""Give each packaged Windows session its own executable filename.

The stable executable remains the shortcut/update target. Its short-lived launcher
copies only itself beside the existing support directory, then starts that copy.
This changes names, not the executable contents or other identifying properties.
Keep this module stdlib-only so launch failures can be reported before Qt loads.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Sequence

EXECUTABLE = "PNUT M&M.exe"
_ALIAS = re.compile(r"[0-9a-f]{24}\.exe\Z")
_MARKER = re.compile(r"\.pnut-runtime-([0-9a-f]{24})\.json\Z")
_MIN_CLEANUP_AGE = 24 * 60 * 60
_HANDOFF = "_PNUT_SESSION_HANDOFF"


class LaunchError(RuntimeError):
    """The packaged session could not be started under its generated name."""


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _plain_directory(directory: Path) -> bool:
    return directory.is_absolute() and directory.is_dir() and not any(
        _linked(part) for part in (directory, *directory.parents)
    )


def _marker(executable: Path) -> Path:
    return executable.with_name(f".pnut-runtime-{executable.stem}.json")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def is_session_executable(path: Path) -> bool:
    """Recognize only an intact generated copy with its matching ownership record.

    Callers additionally validate the canonical executable and support directory.
    Old-version records remain recognizable for conservative cleanup after updates.
    """
    executable = Path(path).absolute()
    marker = _marker(executable)
    try:
        if (not _ALIAS.fullmatch(executable.name)
                or not _plain_directory(executable.parent)
                or not executable.is_file() or _linked(executable)
                or not marker.is_file() or _linked(marker)
                or marker.stat().st_size > 4096):
            return False
        record = json.loads(marker.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or set(record) != {"schema", "executable", "sha256"}
                or record["schema"] != 1 or record["executable"] != executable.name
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            return False
        return _digest(executable) == record["sha256"]
    except (OSError, ValueError, TypeError):
        return False


def cleanup_sessions(directory: Path, *, now: float | None = None) -> None:
    """Remove only verified, old, unlocked copies; never recurse or follow links.

    A full day of grace avoids racing another launch between copying and spawning.
    Windows refuses deletion while an executable is running, which leaves its
    ownership record intact for a later launch to retry.
    """
    directory = Path(directory).absolute()
    if not _plain_directory(directory):
        return
    cutoff = (time.time() if now is None else now) - _MIN_CLEANUP_AGE
    for marker in directory.glob(".pnut-runtime-*.json"):
        match = _MARKER.fullmatch(marker.name)
        if not match:
            continue
        executable = directory / f"{match[1]}.exe"
        try:
            if (marker.stat().st_mtime >= cutoff or executable.stat().st_mtime >= cutoff
                    or not is_session_executable(executable)):
                continue
            executable.unlink()
            marker.unlink()
        except OSError:
            # Locked, missing, or otherwise inaccessible entries are left alone.
            continue


def _copy_session(canonical: Path) -> Path:
    for _ in range(8):
        executable = canonical.with_name(f"{secrets.token_hex(12)}.exe")
        marker = _marker(executable)
        if marker.exists() or marker.is_symlink():
            continue
        try:
            destination = executable.open("xb")
        except FileExistsError:
            continue
        marker_created = False
        try:
            with destination, canonical.open("rb") as source:
                shutil.copyfileobj(source, destination)
            # Exclusive creation protects against a second launcher or existing file.
            with marker.open("x", encoding="utf-8") as stream:
                marker_created = True
                json.dump({"schema": 1, "executable": executable.name,
                           "sha256": _digest(executable)}, stream)
            return executable
        except BaseException:
            executable.unlink(missing_ok=True)
            if marker_created:
                marker.unlink(missing_ok=True)
            raise
    raise LaunchError("Could not allocate a unique session executable. Please try again.")


def launch_if_needed(args: Sequence[str], *, wait: bool = False) -> int | None:
    """Return None in the app process, or the stable launcher's exit status.

    Diagnostics wait for their child so existing smoke-test callers still receive
    its exit status and completed report. Interactive launches exit immediately.
    """
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        return None
    handoff = os.environ.pop(_HANDOFF, None)
    executable = Path(sys.executable).absolute()
    canonical = executable.with_name(EXECUTABLE)
    internal = executable.parent / "_internal"
    if (not _plain_directory(executable.parent) or not canonical.is_file()
            or _linked(canonical) or not internal.is_dir() or _linked(internal)):
        raise LaunchError("The application files are missing or linked. Extract the entire release into a writable folder.")
    if executable.name.casefold() != EXECUTABLE.casefold():
        if (handoff == str(executable) and is_session_executable(executable)
                and _digest(executable) == _digest(canonical)):
            return None
        raise LaunchError("This session executable could not be verified. Start the application using PNUT M&M.exe.")
    alias: Path | None = None
    child = None
    try:
        cleanup_sessions(executable.parent)
        alias = _copy_session(canonical)
        environment = os.environ.copy()
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment[_HANDOFF] = str(alias)
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 1  # Win32 SW_SHOWNORMAL; subprocess exports only SW_HIDE.
        child = subprocess.Popen(
            [str(alias), *args], env=environment, startupinfo=startup,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if wait:
            try:
                return child.wait(timeout=25)
            except subprocess.TimeoutExpired as exc:
                child.kill()
                child.wait(timeout=5)
                raise LaunchError("The application startup check timed out.") from exc
        return 0
    except (OSError, ValueError) as exc:
        raise LaunchError(f"Could not start the session executable: {exc}. Check that the application folder is writable.") from exc
    finally:
        # An interactive child owns these files until a later launch cleans them.
        if (alias is not None and (child is None or (wait and child.poll() is not None))
                and is_session_executable(alias)):
            try:
                alias.unlink(missing_ok=True)
                _marker(alias).unlink(missing_ok=True)
            except OSError:
                pass


def report_launch_error(error: LaunchError, args: Sequence[str]) -> None:
    """Report diagnostics without Qt; normal launches receive a visible error."""
    if "--smoke-test" in args:
        try:
            target = Path(args[args.index("--report") + 1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"ok": False, "stage": "launcher_error", "frozen": True,
                                          "error": str(error)}), encoding="utf-8")
        except (ValueError, IndexError, OSError):
            pass
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(error), "Application launch error", 0x10)
    elif sys.stderr is not None:
        print(str(error), file=sys.stderr)
