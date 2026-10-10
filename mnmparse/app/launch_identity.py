"""Give each packaged Windows session its own executable filename.

The stable executable remains the shortcut/update target. Its short-lived launcher
copies only itself beside the existing support directory, then starts that copy.
This changes names, not the executable contents or other identifying properties.
Keep this module stdlib-only so launch failures can be reported before Qt loads.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
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

from mnmparse.storage import atomic_json

EXECUTABLE = "PNUT M&M.exe"
_ALIAS = re.compile(r"[0-9a-f]{24}\.exe\Z")
_MARKER = re.compile(r"\.pnut-runtime-([0-9a-f]{24})\.json\Z")
_MAX_CLEANUP_FILES = 128
_MAX_RUNTIME_BYTES = 64 * 1024 * 1024
_CLEANUP_SECONDS = 2.0
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
                or type(record["schema"]) is not int or record["schema"] != 1 or record["executable"] != executable.name
                or not isinstance(record["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            return False
        return _digest(executable) == record["sha256"]
    except (OSError, ValueError, TypeError):
        return False


def _guard_name(directory: Path) -> str:
    key = os.path.normcase(str(Path(directory).absolute()))
    return "Local\\pnut-session-files-" + hashlib.sha256(key.encode()).hexdigest()[:32]


@contextmanager
def _session_guard(directory: Path):
    """Serialize cleanup and copy/spawn, including the pre-CreateProcess gap."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, _guard_name(directory))
    if not handle:
        raise LaunchError("Could not coordinate application session files.")
    acquired = False
    try:
        # An abandoned mutex has also been acquired by this thread.
        acquired = kernel.WaitForSingleObject(handle, 2000) in (0, 0x80)
        if not acquired:
            raise LaunchError("Another PNUT launch is preparing its session. Try again shortly.")
        yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _remove_session(executable: Path) -> bool:
    """Only mapped, locked, or unverifiable runtime copies may remain."""
    try:
        if (executable == Path(sys.executable).absolute()
                or executable.stat().st_size > _MAX_RUNTIME_BYTES
                or not is_session_executable(executable)):
            return False
        # Windows refuses removal of mapped executables; retain their records too.
        executable.unlink()
        _marker(executable).unlink()
        return True
    except OSError:
        return False


def _cleanup_sessions(directory: Path) -> None:
    """Called while the directory's launch mutex is held."""
    deadline = time.monotonic() + _CLEANUP_SECONDS
    for index, marker in enumerate(directory.glob(".pnut-runtime-*.json")):
        if index >= _MAX_CLEANUP_FILES or time.monotonic() >= deadline:
            break
        match = _MARKER.fullmatch(marker.name)
        if match:
            _remove_session(directory / f"{match[1]}.exe")


def cleanup_sessions(directory: Path, *, now: float | None = None) -> None:
    """Remove verified inactive copies; leave unmarked/modified/user files alone.

    Launchers hold the same mutex until CreateProcess has mapped the next copy.
    Locked or running images remain owned and can be retried at the next startup.
    ``now`` is retained for callers of the old age-based cleanup API.
    """
    directory = Path(directory).absolute()
    if not _plain_directory(directory):
        return
    try:
        with _session_guard(directory):
            _cleanup_sessions(directory)
    except (OSError, LaunchError):
        pass


def _cleanup_script(executable: Path, digest: str, process_id: int) -> str:
    """Wait for one session to exit, then verify and delete exactly its two files."""
    executable = Path(executable).absolute()
    if (not _ALIAS.fullmatch(executable.name) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or type(process_id) is not int or process_id < 1):
        raise LaunchError("Invalid session cleanup identity.")

    def quote(value: str | Path) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = r'''$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$exe = __EXE__
$marker = __MARKER__
$expected = __HASH__
$ownerPid = __PID__
$mutex = $null
$acquired = $false
try {
    $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($owner -and -not $owner.WaitForExit(60000)) { exit 0 }
    $mutex = [Threading.Mutex]::new($false, __MUTEX__)
    try { $acquired = $mutex.WaitOne(2000) }
    catch [Threading.AbandonedMutexException] { $acquired = $true }
    if (-not $acquired) { exit 0 }
    # Never recurse, follow a reparse point, or infer ownership from a hex name.
    foreach ($path in @($exe, $marker)) {
        $part = $path
        while ($part) {
            $item = Get-Item -LiteralPath $part -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 0 }
            $part = Split-Path -Parent $part
        }
    }
    if ((Get-Item -LiteralPath $marker -Force).Length -gt 4096) { exit 0 }
    $record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ((($record.PSObject.Properties.Name | Sort-Object) -join ',') -cne 'executable,schema,sha256' -or
        ($record.schema -isnot [int] -and $record.schema -isnot [long]) -or
        $record.schema -ne 1 -or $record.executable -cne [IO.Path]::GetFileName($exe) -or
        $record.sha256 -cne $expected) { exit 0 }
    if ((Get-Item -LiteralPath $exe -Force).Length -gt 67108864) { exit 0 }
    $stream = [IO.File]::OpenRead($exe)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actual = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        $stream.Dispose()
        $hasher.Dispose()
    }
    if ($actual -cne $expected) { exit 0 }
    Remove-Item -LiteralPath $exe -Force
    Remove-Item -LiteralPath $marker -Force
} catch {
    # A running, locked, changed, or missing file is retried by startup cleanup.
} finally {
    if ($acquired) { $mutex.ReleaseMutex() }
    if ($mutex) { $mutex.Dispose() }
}
'''
    return (script.replace("__EXE__", quote(executable)).replace("__MARKER__", quote(_marker(executable)))
            .replace("__HASH__", quote(digest)).replace("__PID__", str(process_id))
            .replace("__MUTEX__", quote(_guard_name(executable.parent))))


def _schedule_session_cleanup(executable: Path) -> None:
    """Run after Python shuts down; Windows can release the mapped EXE first."""
    try:
        if not is_session_executable(executable):
            return
        script = _cleanup_script(executable, _digest(executable), os.getpid())
        powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                          base64.b64encode(script.encode("utf-16le")).decode("ascii")],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, ValueError, KeyError):
        pass


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
            register_update_boot()
            # Smoke's waiting launcher removes its child itself. A second helper
            # would race temporary extraction cleanup after the report returns.
            if "--smoke-test" not in args:
                atexit.register(_schedule_session_cleanup, executable)
            return None
        raise LaunchError("This session executable could not be verified. Start the application using PNUT M&M.exe.")
    alias: Path | None = None
    child = None
    try:
        with _session_guard(executable.parent):
            _cleanup_sessions(executable.parent)
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
        # Interactive children schedule their own verified post-exit removal.
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
            atomic_json(target, {"ok": False, "stage": "launcher_error", "frozen": True,
                                 "error": str(error)})
        except (ValueError, IndexError, OSError):
            pass
        return
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, str(error), "Application launch error", 0x10)
    elif sys.stderr is not None:
        print(str(error), file=sys.stderr)


class InstanceCoordinator:
    """One interactive process per installation; a second launch activates it.

    The installation guard protects shared application binaries; the profile guard
    also prevents separate installations from writing the same preferences. Keep
    this object alive until shutdown; construct after QApplication.
    """

    def __init__(self, installation: Path, profile: Path, parent=None) -> None:
        from PySide6.QtCore import QObject, Signal
        from PySide6.QtNetwork import QLocalServer

        class Signals(QObject):
            activated = Signal()

        self.signals = Signals(parent)
        self.activation = self.signals.activated
        self.profile = Path(profile).resolve()
        key = os.path.normcase(str(Path(installation).resolve()))
        self.name = "pnut-instance-" + hashlib.sha256(key.encode()).hexdigest()[:32]
        profile_key = os.path.normcase(str(self.profile))
        self.names = [self.name, "pnut-profile-" + hashlib.sha256(profile_key.encode()).hexdigest()[:32]]
        self.servers = [QLocalServer(parent) for _ in self.names]
        self.server = self.servers[0]
        for server in self.servers:
            server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
            server.newConnection.connect(lambda server=server: self._connection(server))
        self._handles = []
        self._locks = []
        self._acquired = False

    def acquire(self) -> bool:
        from PySide6.QtCore import QLockFile
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        import tempfile

        if self._acquired:
            return True
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel.CreateMutexW.restype = wintypes.HANDLE
            self._kernel = kernel
            ctypes.set_last_error(0)
            conflict = None
            for name in self.names:
                ctypes.set_last_error(0)
                handle = kernel.CreateMutexW(None, False, "Local\\" + name)
                if not handle:
                    self.close()
                    raise LaunchError("Could not coordinate this installation's application session.")
                self._handles.append(handle)
                if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                    conflict = name
                    break
        else:
            conflict = None
            for name in self.names:
                lock = QLockFile(str(Path(tempfile.gettempdir()) / (name + ".lock")))
                lock.setStaleLockTime(0)
                if not lock.tryLock(0):
                    conflict = name
                    break
                self._locks.append(lock)
        if conflict:
            socket = QLocalSocket()
            socket.connectToServer(conflict)
            connected = socket.waitForConnected(1500)
            if connected:
                socket.disconnectFromServer()
            self.close()
            if not connected:
                raise LaunchError("PNUT is already starting or running in this folder. Its window could not be activated; try again shortly.")
            return False
        for server, name in zip(self.servers, self.names):
            if not server.listen(name):
                # Only the mutex/lock owner can remove an abandoned server name.
                QLocalServer.removeServer(name)
                if not server.listen(name):
                    self.close()
                    raise LaunchError("Could not start this installation's activation listener.")
        self._acquired = True
        return True

    def _connection(self, server) -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            # A user-scoped pipe connection is the activation request. Reading
            # bytes after a short-lived client closes can lose that request on
            # Windows named pipes while the original process is initializing.
            self.activation.emit()
            socket.disconnectFromServer()
            socket.deleteLater()

    def close(self) -> None:
        for server in self.servers:
            server.close()
        self._acquired = False
        if self._handles:
            import ctypes
            self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            for handle in self._handles:
                self._kernel.CloseHandle(handle)
            self._handles.clear()
        for lock in self._locks:
            lock.unlock()
        self._locks.clear()


def register_update_boot() -> None:
    """Record the actual generated child PID before importing Qt on update boot."""
    token = os.environ.get("_PNUT_UPDATE_TOKEN", "")
    if not re.fullmatch(r"[0-9a-f]{32}", token) or not getattr(sys, "frozen", False):
        return
    install = Path(sys.executable).absolute().parent
    try:
        path = install / ".pnut-update.json"
        if _linked(path) or path.stat().st_size > 16384:
            return
        receipt = json.loads(path.read_text(encoding="utf-8-sig"))
        if receipt.get("schema") == 1 and receipt.get("token") == token and receipt.get("status") == "pending":
            atomic_json(install / f".pnut-boot-{token}.json",
                        {"schema": 1, "token": token, "pid": os.getpid(), "executable": sys.executable})
    except (OSError, ValueError, AttributeError):
        pass
