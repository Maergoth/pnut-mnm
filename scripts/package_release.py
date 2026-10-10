"""Create a Windows release ZIP and prove that its extracted EXE initializes.

Run after build_exe.ps1. The executable, support folder, legal notices, and public launch
instructions are shipped; settings, logs, and install-origin metadata stay local.
"""
from __future__ import annotations

import hashlib
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


def main() -> int:
    package = ROOT / "dist" / "PNUT M&M"
    executable = package / "PNUT M&M.exe"
    if not executable.is_file() or not (package / "_internal").is_dir():
        raise RuntimeError("Run build_exe.ps1 before packaging a release")
    output = ROOT / "dist" / "releases"
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"PNUT-MnM-{__version__}-windows.zip"
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
        (output / f"{archive.stem}.smoke.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if (process.returncode != 0 or result.get("ok") is not True
                or result.get("frozen") is not True or result.get("stage") != "complete"):
            raise RuntimeError(f"Extracted release failed smoke test: exit {process.returncode}, {result}")
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    archive.with_suffix(".zip.sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"Verified extracted EXE; release: {archive}")
    print(f"SHA256: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
