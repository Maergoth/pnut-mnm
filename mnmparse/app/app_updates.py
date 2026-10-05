"""Verified GitHub release downloads and a deferred Windows application swap.

Checking for an update never executes the download or changes the installation.
The caller explicitly invokes ``prepare_restart`` before quitting the application.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import logging
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import zipfile

from PySide6.QtCore import QObject, Signal, Slot

from mnmparse.app import APP_VERSION

log = logging.getLogger(__name__)
REPOSITORY = "Maergoth/pnut-mnm"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
EXECUTABLE = "PNUT M&M.exe"
MAX_DOWNLOAD = 512 * 1024 * 1024
MAX_EXPANDED = 1536 * 1024 * 1024
MAX_FILES = 20000
DOWNLOAD_SECONDS = 600
_HOSTS = {"api.github.com", "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_RESERVED = re.compile(r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)", re.I)


class UpdateError(ValueError):
    """An update could not safely be downloaded or installed."""


class _Cancelled(UpdateError):
    pass


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value):
        raise UpdateError("The release has an unsupported version number.")
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def _trusted_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in _HOSTS
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (TypeError, ValueError):
        return False


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_url(newurl):
            raise UpdateError("GitHub redirected the download to an unexpected address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, limit: int, cancelled: threading.Event,
           destination: Path | None = None, progress: Callable[[str], None] | None = None) -> bytes:
    if cancelled.is_set():
        raise _Cancelled("Update cancelled.")
    if not _trusted_url(url):
        raise UpdateError("The update download address is not trusted.")
    started = time.monotonic()
    accept = "application/vnd.github+json" if urlsplit(url).hostname == "api.github.com" else "application/octet-stream"
    request = Request(url, headers={"User-Agent": f"PNUT-MnM/{APP_VERSION}", "Accept": accept})
    output = destination.open("wb") if destination else io.BytesIO()
    try:
        with build_opener(_Redirects()).open(request, timeout=20) as response:
            if not _trusted_url(response.geturl()):
                raise UpdateError("The update download address is not trusted.")
            length = response.headers.get("Content-Length")
            if length and (not length.isdigit() or int(length) > limit):
                raise UpdateError("The update download exceeds the allowed size.")
            received = 0
            last_notice = -1
            while True:
                if cancelled.is_set():
                    raise _Cancelled("Update cancelled.")
                if time.monotonic() - started > DOWNLOAD_SECONDS:
                    raise UpdateError("The update download timed out. Try again later.")
                data = response.read(min(256 * 1024, limit + 1 - received))
                if not data:
                    break
                received += len(data)
                if received > limit:
                    raise UpdateError("The update download exceeds the allowed size.")
                output.write(data)
                megabytes = received // (1024 * 1024)
                if progress and megabytes != last_notice:
                    progress(f"Downloading application update: {megabytes} MB…")
                    last_notice = megabytes
            if length and received != int(length):
                raise UpdateError("The update download was incomplete. Try again.")
        return output.getvalue() if destination is None else b""
    finally:
        output.close()


@dataclass(frozen=True)
class Release:
    version: str
    name: str
    archive_url: str
    checksum_url: str


def select_release(payload: dict, current: str = APP_VERSION) -> Release | None:
    """Only the exact Windows package/checksum pair from the stable release."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("GitHub did not return a stable public release.")
    tag = payload.get("tag_name", "")
    parsed = version_tuple(tag)
    if parsed <= version_tuple(current):
        return None
    version = tag.removeprefix("v")
    name = f"PNUT-MnM-{version}-windows.zip"
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise UpdateError("The GitHub release has no Windows download.")
    urls = []
    for wanted in (name, name + ".sha256"):
        matching = [item for item in assets if isinstance(item, dict) and item.get("name") == wanted]
        if len(matching) != 1:
            raise UpdateError("The latest release is missing its Windows package or checksum.")
        item = matching[0]
        url = item.get("browser_download_url", "")
        expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{wanted}"
        if url != expected:
            raise UpdateError("The release asset points to an unexpected download.")
        size = item.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= (MAX_DOWNLOAD if wanted == name else 4096):
            raise UpdateError("The release asset has an invalid size.")
        urls.append(url)
    return Release(version, name, urls[0], urls[1])


def verify_archive(archive: Path, checksum: bytes, expected_name: str) -> None:
    try:
        text = checksum.decode("ascii").strip()
    except UnicodeError as exc:
        raise UpdateError("The release checksum is invalid.") from exc
    match = re.fullmatch(r"([0-9a-fA-F]{64})[ \t]+\*?([^\r\n]+)", text)
    if match is None or match[2] != expected_name:
        raise UpdateError("The release checksum does not describe this package.")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest.lower() != match[1].lower():
        raise UpdateError("The downloaded update failed its checksum. Try downloading again.")


def extract_archive(archive: Path, destination: Path, cancelled: threading.Event) -> None:
    """Validate every Windows path before extracting a single package file."""
    destination.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise UpdateError("The update expands beyond the allowed size.")
        seen: set[str] = set()
        for entry in entries:
            name = entry.filename
            parts = name.rstrip("/").split("/")
            mode = entry.external_attr >> 16
            if (not name or entry.orig_filename != name or "\\" in name or "\x00" in name or entry.flag_bits & 1
                    or any(not part or part in (".", "..") or part[-1:] in (" ", ".")
                           or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
                           or _RESERVED.match(part) for part in parts)
                    or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))
                    or entry.external_attr & 0x400):
                raise UpdateError("The update contains an unsafe file path.")
            if not (name in (EXECUTABLE, "START HERE.txt")
                    or (parts[0] == "_internal" and (len(parts) > 1 or entry.is_dir()))):
                raise UpdateError("The update contains unexpected files.")
            normalized = str(PurePosixPath(name)).casefold()
            if normalized in seen:
                raise UpdateError("The update contains duplicate file paths.")
            seen.add(normalized)
        if EXECUTABLE.casefold() not in seen or not any(name.startswith("_internal/") for name in seen):
            raise UpdateError("The update is missing its executable or support files.")
        root = destination.resolve()
        for entry in entries:
            if cancelled.is_set():
                raise _Cancelled("Update cancelled.")
            target = destination.joinpath(*entry.filename.rstrip("/").split("/"))
            if not target.resolve().is_relative_to(root):
                raise UpdateError("The update contains a path outside its staging directory.")
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, target.open("xb") as output:
                    while data := source.read(256 * 1024):
                        if cancelled.is_set():
                            raise _Cancelled("Update cancelled.")
                        output.write(data)


def _stage_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "PNUT" / "updates"


def download_update(cancelled: threading.Event, progress: Callable[[str], None]) -> tuple[str, Path] | None:
    progress("Checking GitHub for application updates…")
    payload = json.loads(_fetch(LATEST_URL, 1024 * 1024, cancelled))
    release = select_release(payload)
    if release is None:
        return None
    root = _stage_root()
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{release.version}-", dir=root))
    archive = staging / release.name
    checksum = _fetch(release.checksum_url, 4096, cancelled)
    _fetch(release.archive_url, MAX_DOWNLOAD, cancelled, archive, progress)
    progress("Verifying application update…")
    verify_archive(archive, checksum, release.name)
    payload_dir = staging / "package"
    extract_archive(archive, payload_dir, cancelled)
    return release.version, payload_dir


def _quote_ps(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def installed_directory() -> Path:
    if not getattr(sys, "frozen", False) or sys.platform != "win32":
        raise UpdateError("Application updates are available in the installed Windows build. Update development source with Git.")
    executable = Path(sys.executable).absolute()
    install = executable.parent
    if (executable.name != EXECUTABLE or not executable.is_file()
            or not (install / "_internal").is_dir() or _is_link(executable)):
        raise UpdateError("This installation does not have the expected PNUT application files.")
    for path in (install, *install.parents, install / "_internal", install / "START HERE.txt"):
        if path.exists() and _is_link(path):
            raise UpdateError("Automatic updates cannot replace a linked application directory.")
    return install.resolve()


def make_restart_script(install: Path, package: Path, process_id: int, token: str) -> str:
    """Paths are literal data; all moves are bounded to three application entries."""
    if not install.is_absolute() or not package.is_absolute() or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise UpdateError("The update helper paths are invalid.")
    if not isinstance(process_id, int) or process_id < 1:
        raise UpdateError("The update helper process is invalid.")
    script = r'''$ErrorActionPreference = 'Stop'
$install = __INSTALL__
$package = __PACKAGE__
$ownerPid = __PID__
$token = __TOKEN__
$names = @('PNUT M&M.exe', '_internal', 'START HERE.txt')
$incoming = Join-Path $install ('.pnut-incoming-' + $token)
$backup = Join-Path $install ('.pnut-backup-' + $token)
$failed = Join-Path $install ('.pnut-failed-' + $token)
$result = Join-Path (Split-Path -Parent $package) 'install-result.json'
$movedOld = @()
$movedNew = @()
$swapStarted = $false
# The new executable is an independent PyInstaller application, not a worker of
# the old frozen process (which passed its bootloader environment to this helper).
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
function Assert-PlainPath([string]$path, [string]$root) {
    $full = [IO.Path]::GetFullPath($path)
    $base = [IO.Path]::GetFullPath($root).TrimEnd('\')
    if (($full -ne $base) -and -not $full.StartsWith($base + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'An update path escapes the application directory.'
    }
    $check = $full
    while ($check) {
        if (Test-Path -LiteralPath $check) {
            $item = Get-Item -LiteralPath $check -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked update paths are not supported.' }
        }
        $parent = Split-Path -Parent $check
        if ($parent -eq $check) { break }
        $check = $parent
    }
}
function Move-Owned([string]$source, [string]$destination) {
    Assert-PlainPath $source $install
    Assert-PlainPath $destination $install
    Move-Item -LiteralPath $source -Destination $destination
}
try {
    Assert-PlainPath $install $install
    Assert-PlainPath $package $package
    if (-not (Test-Path -LiteralPath (Join-Path $package 'PNUT M&M.exe') -PathType Leaf)) { throw 'Staged application is missing.' }
    if (-not (Test-Path -LiteralPath (Join-Path $package '_internal') -PathType Container)) { throw 'Staged support files are missing.' }
    foreach ($path in @($incoming, $backup, $failed)) {
        Assert-PlainPath $path $install
        if (Test-Path -LiteralPath $path) { throw 'An update work directory already exists.' }
    }
    # Copy across volumes before stopping or touching the installed application.
    New-Item -ItemType Directory -Path $incoming | Out-Null
    foreach ($name in $names) {
        $source = Join-Path $package $name
        Assert-PlainPath $source $package
        if (Test-Path -LiteralPath $source) {
            foreach ($item in @(Get-ChildItem -LiteralPath $source -Recurse -Force)) {
                if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked package files are not supported.' }
            }
            Copy-Item -LiteralPath $source -Destination (Join-Path $incoming $name) -Recurse
        }
    }
    $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if ($owner -and -not $owner.WaitForExit(120000)) { throw 'PNUT did not exit in time; no application files were changed.' }
    New-Item -ItemType Directory -Path $backup | Out-Null
    $swapStarted = $true
    foreach ($name in $names) {
        $live = Join-Path $install $name
        if (Test-Path -LiteralPath $live) {
            Move-Owned $live (Join-Path $backup $name)
            $movedOld += $name
        }
        $next = Join-Path $incoming $name
        if (Test-Path -LiteralPath $next) {
            Move-Owned $next $live
            $movedNew += $name
        }
    }
    @{ok=$true; backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
    Start-Process -FilePath (Join-Path $install 'PNUT M&M.exe') -WorkingDirectory $install -WindowStyle Hidden
} catch {
    $failure = $_.Exception.Message
    try {
        if ($swapStarted) {
            New-Item -ItemType Directory -Path $failed | Out-Null
            foreach ($name in $movedNew) { Move-Owned (Join-Path $install $name) (Join-Path $failed $name) }
            foreach ($name in $movedOld) { Move-Owned (Join-Path $backup $name) (Join-Path $install $name) }
        }
    } catch { $failure += ' Rollback needs attention: ' + $_.Exception.Message }
    @{ok=$false; error=$failure; backup=$backup} | ConvertTo-Json | Set-Content -LiteralPath $result -Encoding UTF8
    $owner = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    if (-not $owner -and (Test-Path -LiteralPath (Join-Path $install 'PNUT M&M.exe'))) {
        Start-Process -FilePath (Join-Path $install 'PNUT M&M.exe') -WorkingDirectory $install -WindowStyle Hidden
    }
    exit 1
}
'''
    return (script.replace("__INSTALL__", _quote_ps(install))
            .replace("__PACKAGE__", _quote_ps(package))
            .replace("__PID__", str(process_id)).replace("__TOKEN__", _quote_ps(token)))


class _Signals(QObject):
    progress = Signal(str)
    completed = Signal(object, str)


class _Worker:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.signals = _Signals()

    def _emit(self, signal, *args) -> None:
        if not self.cancelled.is_set():
            try:
                signal.emit(*args)
            except RuntimeError:
                pass

    def run(self) -> None:
        try:
            result = download_update(self.cancelled, lambda message: self._emit(self.signals.progress, message))
            message = f"PNUT {result[0]} is ready. Choose Restart to update to apply it." if result else f"PNUT {APP_VERSION} is up to date."
            self._emit(self.signals.completed, result, message)
        except Exception as exc:
            log.warning("Application update failed: %s", exc)
            self._emit(self.signals.completed, None, f"Could not update PNUT: {exc}")


class AppUpdateController(QObject):
    started = Signal()
    progress = Signal(str)
    finished = Signal(str)
    ready = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._worker: _Worker | None = None
        self._closed = False
        self._ready_package: Path | None = None
        self._ready_version = ""
        self._restart_prepared = False

    @property
    def is_running(self) -> bool:
        return self._worker is not None

    @property
    def ready_package(self) -> Path | None:
        return self._ready_package

    @Slot()
    def start(self) -> bool:
        if self._closed or self.is_running or self._restart_prepared:
            return False
        try:
            installed_directory()
        except UpdateError as exc:
            self.finished.emit(str(exc))
            return False
        if self._ready_package is not None:
            self.finished.emit(f"PNUT {self._ready_version} is ready. Choose Restart to update to apply it.")
            self.ready.emit(self._ready_version)
            return False
        self._worker = worker = _Worker()
        worker.signals.progress.connect(self._on_progress)
        worker.signals.completed.connect(self._on_completed)
        self.started.emit()
        threading.Thread(target=worker.run, name="app-update", daemon=True).start()
        return True

    @Slot(str)
    def _on_progress(self, message: str) -> None:
        if not self._closed:
            self.progress.emit(message)

    @Slot(object, str)
    def _on_completed(self, result, message: str) -> None:
        self._worker = None
        if self._closed:
            return
        if result is not None:
            self._ready_version, self._ready_package = result
        self.finished.emit(message)
        if result is not None:
            self.ready.emit(self._ready_version)

    def prepare_restart(self) -> bool:
        if self._closed or self._ready_package is None or self._restart_prepared:
            return False
        try:
            install = installed_directory()
            package = self._ready_package.resolve()
            if (not package.is_relative_to(_stage_root().resolve())
                    or not (package / EXECUTABLE).is_file() or not (package / "_internal").is_dir()):
                raise UpdateError("The staged update is unavailable. Download it again.")
            # Fail before quitting if this install cannot create its incoming files.
            with tempfile.NamedTemporaryFile(prefix=".pnut-write-check-", dir=install):
                pass
            token = uuid.uuid4().hex
            script = package.parent / "install.ps1"
            script.write_text(make_restart_script(install, package, os.getpid(), token), encoding="utf-8-sig")
            powershell = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            subprocess.Popen([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                             cwd=package.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            self._restart_prepared = True
            return True
        except Exception as exc:
            log.exception("Could not prepare application update")
            self.finished.emit(f"Could not install the update: {exc}. Check that the application folder is writable.")
            return False

    def shutdown(self) -> None:
        self._closed = True
        if self._worker:
            self._worker.cancelled.set()
            self._worker = None
