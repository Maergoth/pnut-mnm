"""Create a Windows release ZIP and prove that its extracted EXE initializes.

Run after build_exe.ps1. The executable, support folder, legal notices, and public launch
instructions are shipped; settings, logs, and install-origin metadata stay local.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mnmparse import __version__
from mnmparse.provenance import validate_build_info
from mnmparse.storage import atomic_json, atomic_write


def validate_report(result: dict, expected: dict) -> None:
    if (result.get("ok") is not True or result.get("frozen") is not True or result.get("stage") != "complete"
            or result.get("app_version") != expected["version"] or result.get("build") != expected):
        raise RuntimeError("Executable startup/version/build provenance does not match the release source")


def _workspace_directory(value: str | Path) -> Path:
    """Keep custom inputs/outputs inside the workspace without following links."""
    value = Path(value)
    candidate = Path(os.path.abspath(value if value.is_absolute() else ROOT / value))
    workspace = Path(os.path.abspath(ROOT))
    if candidate == workspace or not candidate.is_relative_to(workspace):
        raise RuntimeError(f"Directory must be inside the workspace: {candidate}")
    for part in (candidate, *candidate.parents):
        if part.is_symlink() or part.is_junction() or (part.exists() and not part.is_dir()):
            raise RuntimeError(f"Directory has a file or linked ancestor: {part}")
    return candidate


def _validate_package(package: Path) -> dict:
    executable = package / "PNUT M&M.exe"
    if not executable.is_file() or not (package / "_internal").is_dir():
        raise RuntimeError("Run build_exe.ps1 before packaging a release")
    if any(path.is_symlink() or path.is_junction() for path in package.rglob("*")):
        raise RuntimeError("Application bundle contains a linked entry")
    metadata = package / "_internal" / "build-info.json"
    if not metadata.is_file() or metadata.stat().st_size > 16384:
        raise RuntimeError("Application build provenance is missing or oversized")
    return validate_build_info(json.loads(metadata.read_text(encoding="utf-8")))


def _create_verified_archive(package: Path, archive: Path, report_target: Path, expected: dict) -> str:
    executable = package / "PNUT M&M.exe"
    files = [executable] + sorted(
        p for p in (package / "_internal").rglob("*")
        if p.is_file() and p.name != "direct_url.json"
    )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in files:
            bundle.write(path, path.relative_to(package).as_posix())
        bundle.writestr("START HERE.txt", (
            "PNUT M&M - Parses, Navigation, und Timers\n\n"
            "Extract the entire ZIP into a writable folder, then run PNUT M&M.exe.\n"
            "Keep the _internal folder beside the executable. Python is not required.\n"
            "Windows 10/11 and the English (United States) OCR language pack are required.\n"
            "When replacing version 0.1.0, use a fresh folder or replace its entire _internal\n"
            "folder: merging files leaves the incompatible ICU DLL behind.\n"
            "Keep your config.json, triggers.json and logs when updating.\n\n"
            "User guide: https://github.com/Maergoth/pnut-mnm#readme\n"
            "If you have any questions, contact @Maergoth in discord\n"
        ))
    environment = os.environ.copy()
    for key in list(environment):
        if key.startswith(("PYTHON", "QT_", "QML")):
            environment.pop(key)
    windows = Path(os.environ["SystemRoot"])
    environment["PATH"] = os.pathsep.join((str(windows / "System32"), str(windows)))
    environment["QT_QPA_PLATFORM"] = "offscreen"
    with tempfile.TemporaryDirectory(prefix="pnut-release-") as temp:
        extraction = Path(temp)
        with zipfile.ZipFile(archive) as bundle:
            bad = bundle.testzip()
            if bad is not None:
                raise RuntimeError(f"ZIP integrity check failed: {bad}")
            bundle.extractall(extraction)
        report = extraction / "smoke.json"
        process = subprocess.run(
            [str(extraction / executable.name), "--smoke-test", "--report", str(report)],
            cwd=extraction, env=environment, timeout=40, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        result = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
        atomic_json(report_target, result)
        if (process.returncode != 0 or result.get("ok") is not True
                or result.get("frozen") is not True or result.get("stage") != "complete"):
            raise RuntimeError(f"Extracted release failed smoke test: exit {process.returncode}, {result}")
        validate_report(result, expected)
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-dirty", action="store_true", help="Permit an explicitly labelled development package")
    parser.add_argument("--package-dir", type=Path, default=Path("dist") / "PNUT M&M",
                        help="Application bundle inside the workspace (default: dist/PNUT M&M)")
    parser.add_argument("--output-dir", type=Path, default=Path("dist") / "releases",
                        help="Verified artifacts directory inside the workspace (default: dist/releases)")
    args = parser.parse_args()
    package = _workspace_directory(args.package_dir)
    output = _workspace_directory(args.output_dir)
    expected = _validate_package(package)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    if expected["commit"] != commit or expected["dirty"] != dirty or (dirty and not args.allow_dirty):
        raise RuntimeError("Release packaging requires a current clean build; use --allow-dirty only for development artifacts")
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"PNUT-MnM-{__version__}-windows.zip"
    report = output / f"{archive.stem}.smoke.json"
    checksum = archive.with_suffix(".zip.sha256")
    if any(path.is_symlink() or path.is_junction() or (path.exists() and not path.is_file())
           for path in (archive, report, checksum)):
        raise RuntimeError("Release artifact target is linked or is not a regular file")
    # A failed check cannot replace the last verified ZIP with a partial package.
    with tempfile.TemporaryDirectory(prefix=".pnut-package-", dir=output) as temporary:
        working = Path(temporary) / archive.name
        digest = _create_verified_archive(package, working, report, expected)
        os.replace(working, archive)
    atomic_write(checksum, f"{digest}  {archive.name}\n".encode("ascii"))
    print(f"Verified extracted EXE; release: {archive}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
