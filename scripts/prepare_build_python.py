"""Prepare the pinned Windows build interpreter without installing it globally."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile


ROOT = Path(__file__).resolve().parents[1]
VERSION = "3.14.10"
OPENSSL = "OpenSSL 3.5.9"
PACKAGE_URL = f"https://www.nuget.org/api/v2/package/python/{VERSION}"
PACKAGE_SHA256 = "f54ba67c9951c3d773ea47d2bb095150c037c399f0423a968bcda22809241784"
MAX_PACKAGE_BYTES = 32 * 1024 * 1024
MAX_FILES = 5000
MAX_EXTRACT_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MANIFEST = "runtime-pin.json"
REQUIRED = {"tools/python.exe", "tools/python314.dll", "tools/DLLs/_ssl.pyd",
            "tools/Lib/venv/__init__.py", "tools/Lib/ensurepip/__init__.py"}
OFFICIAL_HOSTS = {"www.nuget.org", "globalcdn.nuget.org", "api.nuget.org"}


def official_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS
            and not parsed.username and not parsed.password and parsed.port in (None, 443))


class _OfficialRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        if not official_url(url):
            raise ValueError("Python package redirected outside official HTTPS NuGet hosts")
        return super().redirect_request(request, response, code, message, headers, url)


def download_package() -> bytes:
    request = Request(PACKAGE_URL, headers={"User-Agent": "PNUT-build-runtime"})
    with build_opener(_OfficialRedirects()).open(request, timeout=45) as response:
        if not official_url(response.geturl()):
            raise ValueError("Unexpected Python package download address")
        data = response.read(MAX_PACKAGE_BYTES + 1)
    return data


def verified_package(package: Path | None = None, *, download=None) -> bytes:
    if package is None:
        data = (download or download_package)()
    else:
        if package.stat().st_size > MAX_PACKAGE_BYTES:
            raise ValueError("Python package exceeds its size limit")
        data = package.read_bytes()
    if len(data) > MAX_PACKAGE_BYTES or hashlib.sha256(data).hexdigest() != PACKAGE_SHA256:
        raise ValueError("Python package does not match the pinned size/checksum requirements")
    return data


def safe_extract(data: bytes, destination: Path) -> None:
    """Validate the whole archive before writing regular runtime files only."""
    root = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES or sum(entry.file_size for entry in entries) > MAX_EXTRACT_BYTES:
            raise ValueError("Python archive exceeds extraction limits")
        seen = set()
        for entry in entries:
            name = entry.filename
            parts = name.rstrip("/").split("/")
            mode = entry.external_attr >> 16
            if (not name or "\\" in name or "\0" in name or PurePosixPath(name).is_absolute()
                    or any(part in ("", ".", "..") or ":" in part or part.endswith((".", " "))
                           or re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
                           for part in parts)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
                    or entry.flag_bits & 1 or entry.file_size > MAX_FILE_BYTES):
                raise ValueError("Unsafe Python archive entry")
            normalized = "/".join(parts).casefold()
            if normalized in seen:
                raise ValueError("Duplicate Python archive entry")
            seen.add(normalized)
            target = root.joinpath(*parts)
            if not target.resolve().is_relative_to(root):
                raise ValueError("Python archive entry escapes its destination")
        if not REQUIRED.issubset({entry.filename for entry in entries if not entry.is_dir()}):
            raise ValueError("Python package is missing required runtime files")
        for entry in entries:
            if not entry.filename.startswith("tools/"):
                continue
            target = root.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("xb") as stream:
                    while chunk := source.read(1024 * 1024):
                        stream.write(chunk)


def verify_signatures(directory: Path) -> None:
    if os.name != "nt":
        return
    # The path travels as environment data, never PowerShell source text.
    command = """$ErrorActionPreference = 'Stop'
$rows = @('python.exe', 'python314.dll') | ForEach-Object {
  $signature = Get-AuthenticodeSignature -LiteralPath (Join-Path $env:PNUT_BUILD_PYTHON_ROOT $_)
  [PSCustomObject]@{file=$_; status=[string]$signature.Status; subject=[string]$signature.SignerCertificate.Subject}
}
$rows | ConvertTo-Json -Compress
"""
    shell = shutil.which("pwsh") or "powershell.exe"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(command.encode("utf-16-le")).decode("ascii")],
                            env=dict(os.environ, PNUT_BUILD_PYTHON_ROOT=str(directory.resolve())),
                            capture_output=True, text=True, check=True, timeout=30)
    rows = json.loads(result.stdout)
    if (not isinstance(rows, list) or {row.get("file") for row in rows} != {"python.exe", "python314.dll"}
            or any(row.get("status") != "Valid" or not re.search(
                r"(?:^|,\s*)CN=Python Software Foundation(?:,|$)", row.get("subject", "")) for row in rows)):
        raise ValueError("Python runtime does not have valid Python Software Foundation signatures")


def validate_runtime(executable: Path) -> dict:
    verify_signatures(executable.parent)
    command = ("import json,platform,ssl,struct; print(json.dumps({"
               "'python':platform.python_version(),'implementation':platform.python_implementation(),"
               "'openssl':ssl.OPENSSL_VERSION,'bits':struct.calcsize('P')*8}))")
    result = subprocess.run([str(executable), "-I", "-S", "-B", "-c", command],
                            cwd=executable.parent, capture_output=True, text=True, check=True, timeout=20)
    value = json.loads(result.stdout)
    if (value.get("python") != VERSION or value.get("implementation") != "CPython" or value.get("bits") != 64
            or not str(value.get("openssl", "")).startswith(OPENSSL + " ")):
        raise ValueError("Prepared runtime must be CPython 3.14.10 x64 with OpenSSL 3.5.9")
    return value


def prepare_python(output: Path, *, package: Path | None = None, download=None, probe=None) -> Path:
    probe = probe or validate_runtime
    if output.is_symlink():
        raise ValueError("Build runtime output must not be a symbolic link")
    output = output.resolve()
    expected = {"version": VERSION, "package_url": PACKAGE_URL, "package_sha256": PACKAGE_SHA256}
    executable = output / "tools/python.exe"
    if output.exists():
        try:
            if json.loads((output / MANIFEST).read_text(encoding="utf-8")) != expected:
                raise ValueError("runtime pin mismatch")
            probe(executable)
        except Exception as exc:
            raise ValueError("Existing build runtime is invalid; choose a new empty --output path. Nothing was overwritten.") from exc
        return executable
    data = verified_package(package, download=download)
    output.parent.mkdir(parents=True, exist_ok=True)
    # TemporaryDirectory owns only this fresh staging folder. No existing
    # destination is deleted, and Windows rename rejects a concurrent target.
    with tempfile.TemporaryDirectory(prefix=".pnut-python-", dir=output.parent) as temporary:
        staging = Path(temporary) / "runtime"
        staging.mkdir()
        safe_extract(data, staging)
        probe(staging / "tools/python.exe")
        (staging / MANIFEST).write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        if output.exists():
            raise ValueError("Build runtime destination appeared during preparation; nothing was overwritten")
        staging.rename(output)
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "private/build-python-3.14.10")
    parser.add_argument("--package", type=Path, help="Use an already downloaded package, still checking the pinned SHA256")
    args = parser.parse_args()
    try:
        executable = prepare_python(args.output, package=args.package)
    except Exception as exc:
        print(f"Build Python preparation failed: {exc}", file=sys.stderr)
        return 1
    print(executable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
