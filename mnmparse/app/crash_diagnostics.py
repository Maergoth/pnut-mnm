"""Retain Python thread stacks from native failures in a dedicated local log.

The descriptor is separate from rotating application logs and deliberately
stays open until process exit. This cannot capture failures before setup,
guarantee a report after every native failure, or replace a native crash dump.
"""
from __future__ import annotations

from datetime import datetime, timezone
import faulthandler
import logging
import os
from pathlib import Path
import sys
import threading


NATIVE_LOG_BYTES = 1_000_000
NATIVE_LOG_NAME = "native-crash.log"
_lock = threading.Lock()
_fd: int | None = None
_path: Path | None = None
_enabled = False
log = logging.getLogger(__name__)


def enable_crash_diagnostics(directory: Path, *, version: str = "unknown") -> Path | None:
    """Enable native-failure thread stacks without making startup depend on it.

    Reconfiguration retains the first opened descriptor, even if app.log moves
    or rotates. Only an inactive file can rotate, before the first open. One
    backup bounds normal launch history; an active process is never truncated.
    An enable failure retains the candidate descriptor for a safe later retry,
    since closing a potentially installed handler's destination is unsafe.
    """
    global _fd, _path, _enabled
    with _lock:
        if _enabled:
            return _path
        if _fd is None:
            candidate: int | None = None
            try:
                path = Path(directory).resolve() / NATIVE_LOG_NAME
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if path.stat().st_size >= NATIVE_LOG_BYTES:
                        path.replace(path.with_name(path.name + ".1"))
                except FileNotFoundError:
                    pass
                except OSError:
                    # Another running process may hold the file on Windows.
                    # Append without truncating its evidence in that case.
                    log.warning("Could not rotate the native crash log; retaining existing evidence")
                candidate = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0), 0o600)
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                header = (f"\n=== PNUT native crash diagnostics | {stamp} | version {version} | "
                          f"Python {sys.version.split()[0]} | PID {os.getpid()} ===\n")
                os.write(candidate, header.encode("utf-8", errors="backslashreplace"))
            except Exception:
                # faulthandler has not received this descriptor yet.
                if candidate is not None:
                    try:
                        os.close(candidate)
                    except OSError:
                        pass
                log.warning("Native crash diagnostics are unavailable; continuing startup", exc_info=True)
                return None
            _fd, _path = candidate, path
        try:
            faulthandler.enable(file=_fd, all_threads=True, c_stack=False)
        except Exception:
            log.warning("Native crash thread reporting could not be enabled; continuing startup", exc_info=True)
            return None
        _enabled = True
        return _path
