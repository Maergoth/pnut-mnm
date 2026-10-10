"""Bundle auditable dependency notices; normal builds never access the network.

Run with the build venv. --refresh-upstream is a deliberate maintenance operation
for the pinned upstream source archives. Review its diff when updating versions.
--check verifies both the installed versions and the committed notice contents.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import importlib.metadata as metadata
import io
import json
from pathlib import Path, PurePosixPath
import platform
import ssl
import sys
import tarfile
import tomllib
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "legal" / "licenses"
MANIFEST = DEST / "upstream-manifest.json"
VERSIONS = {
    "PySide6": "6.11.2", "PySide6-Essentials": "6.11.2",
    "PySide6-Addons": "6.11.2", "shiboken6": "6.11.2",
    "numpy": "2.5.3", "opencv-python": "5.0.0.93", "pillow": "12.3.0",
    "mss": "10.2.0", "windows-capture": "2.0.1", "winrt-runtime": "3.2.1",
    "winrt-Windows.Foundation": "3.2.1",
    "winrt-Windows.Foundation.Collections": "3.2.1",
    "winrt-Windows.Globalization": "3.2.1", "winrt-Windows.Graphics.Imaging": "3.2.1",
    "winrt-Windows.Media.Ocr": "3.2.1", "winrt-Windows.Storage.Streams": "3.2.1",
    "typing_extensions": "4.16.0", "pyinstaller": "6.22.3",
}
PYTHON_VERSION = "3.14.3"
OPENSSL_VERSION = "OpenSSL 3.0.18 30 Sep 2025"
FFMPEG_VERSION = "7.1.5"
QT_MODULES = ("qtbase", "qtsvg", "qtmultimedia", "qtspeech", "pyside-setup")
STATIC_FILES = tuple(f"Qt-6.11.2-{m}.txt" for m in QT_MODULES) + (
    "FFmpeg-7.1.5.txt", "OpenSSL-3.0.18.txt", "PyWinRT-3.2.1.txt",
    "windows-capture-2.0.1-Rust-dependencies.txt",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PNUT-license-notices"})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def source_files(data: bytes) -> dict[str, bytes]:
    """Read an archive in memory; never extract untrusted archive paths to disk."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        return {
            "/".join(PurePosixPath(member.name).parts[1:]): archive.extractfile(member).read()
            for member in archive.getmembers() if member.isfile()
        }


def is_notice(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    if PurePosixPath(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp"):
        return False
    return name.startswith(("license", "licence", "copying", "copyright", "notice"))


def combine(title: str, sections: dict[str, bytes]) -> bytes:
    if not sections:
        raise RuntimeError(f"No upstream notices found for {title}")
    # Each upstream text is preserved verbatim between labeled boundaries.
    content = [title.encode() + b"\n\n"]
    for name, data in sorted(sections.items()):
        content.extend((f"===== {name} =====\n".encode(), data, b"\n\n"))
    return b"".join(content)


def qt_notices(module: str) -> tuple[str, bytes, str, str]:
    owner = "pyside" if module == "pyside-setup" else "qt"
    url = f"https://codeload.github.com/{owner}/{module}/tar.gz/refs/tags/v6.11.2"
    data = download(url)
    files = source_files(data)
    selected = {
        name: contents for name, contents in files.items()
        if name.startswith("LICENSES/") or name == "REUSE.toml"
        or (name.startswith(("src/", "sources/")) and is_notice(name))
        or (name.startswith(("src/", "sources/")) and name.endswith("qt_attribution.json"))
    }
    # Qt attribution records sometimes identify a copyright/license header in a
    # source file instead of a stand-alone LICENSE file. Preserve that file too.
    for name, contents in tuple(selected.items()):
        if not name.endswith("qt_attribution.json"):
            continue
        # Qt's attribution tooling accepts literal newlines within descriptions.
        records = json.loads(contents, strict=False)
        if isinstance(records, dict):
            records = [records]
        for record in records:
            referenced_files = []
            for field in ("LicenseFile", "CopyrightFile"):
                value = record.get(field, [])
                referenced_files.extend([value] if isinstance(value, str) else value)
            for relative in referenced_files:
                candidate = str(PurePosixPath(name).parent / relative)
                # Resolve ./ and ../ within the archive, without using the host FS.
                parts = []
                for part in PurePosixPath(candidate).parts:
                    if part == "..":
                        if parts:
                            parts.pop()
                    elif part != ".":
                        parts.append(part)
                candidate = "/".join(parts)
                if candidate in files:
                    selected[candidate] = files[candidate]
                else:
                    raise RuntimeError(f"Missing attribution reference in {module}: {name}: {relative}")
    return f"Qt-6.11.2-{module}.txt", combine(f"{module} 6.11.2 upstream licenses and attributions", selected), url, digest(data)


def archive_notices(name: str, version: str, url: str) -> tuple[str, bytes, str, str]:
    data = download(url)
    files = source_files(data)
    selected = {p: b for p, b in files.items() if is_notice(p)}
    if name == "OpenSSL":
        selected["AUTHORS.md"] = files["AUTHORS.md"]
    return f"{name}-{version}.txt", combine(f"{name} {version} upstream notices", selected), url, digest(data)


def rust_notices() -> tuple[str, bytes, str, str]:
    index_url = "https://pypi.org/pypi/windows-capture/2.0.1/json"
    index = json.loads(download(index_url))
    sdist = next(item for item in index["urls"] if item["packagetype"] == "sdist")
    data = download(sdist["url"])
    if digest(data) != sdist["digests"]["sha256"]:
        raise RuntimeError("windows-capture sdist checksum mismatch")
    files = source_files(data)
    lock_name = next(name for name in files if name.endswith("Cargo.lock"))
    lock = tomllib.loads(files[lock_name].decode())
    packages = [p for p in lock["package"] if p.get("source", "").startswith("registry+")]

    def crate_notices(package: dict) -> dict[str, bytes]:
        name, version = package["name"], package["version"]
        url = f"https://static.crates.io/crates/{name}/{name}-{version}.crate"
        crate = download(url)
        if digest(crate) != package["checksum"]:
            raise RuntimeError(f"Crate checksum mismatch: {name} {version}")
        contents = source_files(crate)
        notices = {f"{name}-{version}/{p}": b for p, b in contents.items() if is_notice(p)}
        cargo = tomllib.loads(contents["Cargo.toml"].decode())
        expression = cargo.get("package", {}).get("license", "")
        if not notices:
            # Some monorepo crates omit the root license from the crate archive.
            # Their Cargo VCS record identifies the exact upstream commit.
            info = json.loads(contents[".cargo_vcs_info.json"])
            repository = cargo["package"]["repository"].removesuffix(".git").removeprefix("https://github.com/")
            commit = info["git"]["sha1"]
            for filename in ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE-MIT", "LICENSE-APACHE"):
                source = f"https://raw.githubusercontent.com/{repository}/{commit}/{filename}"
                try:
                    license_data = download(source)
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise
                else:
                    notices[f"{name}-{version}/{filename}"] = license_data
                    notices[f"{name}-{version}/{filename}.source.txt"] = f"Source: {source}\nSHA-256: {digest(license_data)}\n".encode()
            if not notices:
                raise RuntimeError(f"No notice files in Rust dependency {name} {version}: {expression}")
        notices[f"{name}-{version}/PROVENANCE.txt"] = (
            f"Source: {url}\nSHA-256: {digest(crate)}\nLicense expression: {expression}\n".encode()
        )
        return notices

    selected = {lock_name: files[lock_name]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for notices in pool.map(crate_notices, packages):
            selected.update(notices)
    return "windows-capture-2.0.1-Rust-dependencies.txt", combine(
        "windows-capture 2.0.1 Rust dependency notices (entire upstream lockfile; includes build-only dependencies)", selected
    ), sdist["url"], digest(data)


def refresh_upstream() -> None:
    jobs = [lambda module=m: qt_notices(module) for m in QT_MODULES]
    jobs.extend((
        lambda: archive_notices("FFmpeg", "7.1.5", "https://codeload.github.com/FFmpeg/FFmpeg/tar.gz/refs/tags/n7.1.5"),
        lambda: archive_notices("OpenSSL", "3.0.18", "https://codeload.github.com/openssl/openssl/tar.gz/refs/tags/openssl-3.0.18"),
        rust_notices,
    ))
    results = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda job: job(), jobs):
            results.append(result)
    url = "https://raw.githubusercontent.com/pywinrt/pywinrt/v3.2.1/LICENSE"
    data = download(url)
    results.append(("PyWinRT-3.2.1.txt", data, url, digest(data)))
    DEST.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for filename, content, source, source_hash in results:
        (DEST / filename).write_bytes(content)
        manifest[filename] = {"source": source, "source_sha256": source_hash, "notice_sha256": digest(content)}
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify_upstream() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if set(manifest) != set(STATIC_FILES):
        raise RuntimeError("Upstream notice manifest does not match the pinned dependencies")
    for filename, provenance in manifest.items():
        if digest((DEST / filename).read_bytes()) != provenance["notice_sha256"]:
            raise RuntimeError(f"Missing or changed upstream notice: {filename}")


def generated_notices() -> dict[str, bytes]:
    for name, expected in VERSIONS.items():
        installed = metadata.version(name)
        if installed != expected:
            raise RuntimeError(f"Notice audit requires {name}=={expected}; installed {installed}. Update and review notices before shipping.")
    if platform.python_version() != PYTHON_VERSION or ssl.OPENSSL_VERSION != OPENSSL_VERSION:
        raise RuntimeError("Python/OpenSSL version changed; update and review the native notices before shipping")
    outputs = {"PNUT-MIT.txt": (ROOT / "LICENSE").read_bytes()}
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    outputs[f"CPython-{PYTHON_VERSION}.txt"] = python_license.read_bytes()
    if b"Python Software Foundation" not in outputs[f"CPython-{PYTHON_VERSION}.txt"]:
        raise RuntimeError("CPython license file is incomplete")
    for name in ("numpy", "opencv-python", "pillow", "mss", "windows-capture", "typing_extensions", "pyinstaller"):
        distribution = metadata.distribution(name)
        notices = {}
        for file in distribution.files or []:
            relative = str(file).replace("\\", "/")
            if ".dist-info/" in relative and is_notice(relative):
                notices[relative.split(".dist-info/", 1)[1]] = distribution.locate_file(file).read_bytes()
        if not notices:
            raise RuntimeError(f"No installed full license text for {name}")
        if name == "opencv-python" and not any("3RD-PARTY" in p for p in notices):
            raise RuntimeError("OpenCV native third-party notices are missing")
        filename = f"{name}-{VERSIONS[name]}.txt"
        outputs[filename] = combine(f"{name} {VERSIONS[name]} installed wheel notices", notices)
    distribution = metadata.distribution("PySide6")
    lib_path = Path(distribution.locate_file("PySide6/avutil-59.dll"))
    ffmpeg = ctypes.CDLL(str(lib_path.resolve()))
    for function in ("av_version_info", "avutil_configuration", "avutil_license"):
        getattr(ffmpeg, function).restype = ctypes.c_char_p
    version = ffmpeg.av_version_info().decode()
    license_name = ffmpeg.avutil_license().decode()
    configuration = ffmpeg.avutil_configuration().decode()
    if version != FFMPEG_VERSION or license_name != "LGPL version 2.1 or later" or "--enable-gpl" in configuration:
        raise RuntimeError("Qt FFmpeg changed; review the exact version/configuration and native notices")
    outputs["Qt-FFmpeg-build.txt"] = (
        "Qt's unmodified shared FFmpeg libraries\n"
        "Copyright (c) 2000-2023 the FFmpeg developers (as recorded by Qt's upstream attribution)\n"
        f"Version: {version}\nLicense reported by binary: {license_name}\nConfiguration: {configuration}\n"
        f"Corresponding upstream source: https://ffmpeg.org/releases/ffmpeg-{version}.tar.xz\n"
        "Qt build scripts: https://code.qt.io/cgit/qt/qtmultimedia.git/tree/src/3rdparty/ffmpeg?h=v6.11.2\n"
    ).encode()
    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-upstream", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh_upstream and args.check:
        parser.error("--refresh-upstream and --check are mutually exclusive")
    if args.refresh_upstream:
        refresh_upstream()
    verify_upstream()
    outputs = generated_notices()
    for name, content in outputs.items():
        target = DEST / name
        if args.check:
            if not target.is_file() or target.read_bytes() != content:
                raise RuntimeError(f"Notice is missing or stale: {name}. Run this script and review its diff.")
        else:
            target.write_bytes(content)
    print(f"{'Verified' if args.check else 'Generated'} {len(outputs)} installed notices and {len(STATIC_FILES)} upstream notice collections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
