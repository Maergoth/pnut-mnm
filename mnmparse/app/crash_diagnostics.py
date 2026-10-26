"""Retain native-failure stacks and uncaught exceptions in a local log.

The descriptor is separate from rotating application logs and deliberately
stays open until process exit. Initialization uses only the standard library so
the entry point can enable it before importing Qt or the application. This
cannot capture failures before this initializer runs, guarantee a report after every
native failure, or replace a native crash dump. C frames are requested; the
official Windows Python build currently reports that C unwinding is unavailable.
"""
from __future__ import annotations

from datetime import datetime, timezone
import faulthandler
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
import traceback
from typing import Sequence


NATIVE_LOG_BYTES = 1_000_000
NATIVE_LOG_NAME = "native-crash.log"
_lock = threading.Lock()
_fd: int | None = None
_path: Path | None = None
_enabled = False
_startup_directory: Path | None = None
_hooks_installed = False
_previous_excepthook = None
_previous_thread_excepthook = None
log = logging.getLogger(__name__)


def startup_log_directory() -> Path | None:
    """Return the entry point's destination without creating a second profile."""
    return _startup_directory


def _argument_value(args: Sequence[str], option: str) -> str | None:
    for index, value in enumerate(args):
        if value.startswith(option + "="):
            return value[len(option) + 1:]
        if value == option and index + 1 < len(args):
            return args[index + 1]
    return None


def enable_startup_diagnostics(args: Sequence[str], *, version: str) -> Path | None:
    """Arm diagnostics before third-party imports; checks never use app logs."""
    global _startup_directory
    try:
        if _startup_directory is None:
            smoke = "--smoke-test" in args or "--atlas-smoke" in args
            selftest = any(value == "--selftest-seconds" or value.startswith("--selftest-seconds=")
                           for value in args)
            report = _argument_value(args, "--report") if smoke else None
            if smoke and report:
                target = Path(report).resolve()
                _startup_directory = target.with_name(target.name + ".crash-logs")
            elif smoke or selftest:
                _startup_directory = Path(tempfile.mkdtemp(prefix="pnut-check-crash-logs-"))
            else:
                base = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
                        else Path(__file__).resolve().parents[2])
                _startup_directory = base / "logs"
        result = enable_crash_diagnostics(_startup_directory, version=version)
        install_exception_hooks()
        return result
    except Exception:
        log.warning("Early crash diagnostics are unavailable; continuing startup", exc_info=True)
        return None


def _record_exception(exc_type: type[BaseException], exc: BaseException, tb, *, thread: str | None = None) -> None:
    """Ordinary Python exceptions can safely use formatting; native handlers cannot."""
    label = "Uncaught exception" + (f" in thread {thread}" if thread is not None else "")
    try:
        if _fd is not None:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            text = f"\n{stamp} | PID {os.getpid()} | {label}\n"
            text += "".join(traceback.format_exception(exc_type, exc, tb))
            os.write(_fd, text.encode("utf-8", errors="backslashreplace"))
    except Exception:
        # Logging a failure must never replace the original exception.
        pass
    try:
        if log.hasHandlers():
            log.critical(label, exc_info=(exc_type, exc, tb))
    except Exception:
        pass


def _excepthook(exc_type, exc, tb) -> None:
    _record_exception(exc_type, exc, tb)
    if _previous_excepthook is not None:
        _previous_excepthook(exc_type, exc, tb)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not SystemExit:
        _record_exception(args.exc_type, args.exc_value, args.exc_traceback,
                          thread=args.thread.name if args.thread is not None else "?")
    if _previous_thread_excepthook is not None:
        _previous_thread_excepthook(args)


def install_exception_hooks() -> None:
    """Install once and preserve the host's original exception handlers."""
    global _hooks_installed, _previous_excepthook, _previous_thread_excepthook
    with _lock:
        if _hooks_installed:
            return
        _previous_excepthook = sys.excepthook
        _previous_thread_excepthook = threading.excepthook
        sys.excepthook = _excepthook
        threading.excepthook = _thread_excepthook
        _hooks_installed = True


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
                header += (f"Runtime: {sys.version.replace(chr(10), ' ')} | {sys.platform} | "
                           f"frozen={bool(getattr(sys, 'frozen', False))} | "
                           f"executable={Path(sys.executable).name} | all_threads=True c_stack=True\n")
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
            faulthandler.enable(file=_fd, all_threads=True, c_stack=True)
        except Exception:
            log.warning("Native crash thread reporting could not be enabled; continuing startup", exc_info=True)
            return None
        _enabled = True
    install_exception_hooks()
    return _path
