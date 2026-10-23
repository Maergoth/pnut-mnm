"""Build the small Windows Atlas adapter; cache and verify its pinned SDK.

Only the SDK headers and statically linked loader are used. No browser runtime
is downloaded or bundled. A cached SDK package makes ordinary rebuilds offline.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT / "native" / "atlas_webview" / "sdk.json"
BUILD = ROOT / "build" / "atlas_webview"
MAX_PACKAGE = 32 * 1024 * 1024
SDK_FILES = ("build/native/include/WebView2.h", "build/native/include/WebView2EnvironmentOptions.h",
             "build/native/x64/WebView2LoaderStatic.lib")


def sdk_pin() -> dict[str, str]:
    pin = json.loads(PIN.read_text(encoding="utf-8"))
    version = pin.get("version", "")
    expected = f"https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/{version}/microsoft.web.webview2.{version}.nupkg"
    if (pin.get("package") != "Microsoft.Web.WebView2" or not re.fullmatch(r"\d+(?:\.\d+){3}", version)
            or pin.get("url") != expected or not re.fullmatch(r"[0-9a-f]{64}", pin.get("sha256", ""))):
        raise ValueError("Invalid pinned WebView2 SDK metadata")
    return pin


def verified_package(pin: dict[str, str], cache: Path) -> bytes:
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        if cache.stat().st_size > MAX_PACKAGE:
            raise ValueError("Cached WebView2 SDK exceeds the size limit")
        data = cache.read_bytes()
    else:
        request = Request(pin["url"], headers={"User-Agent": "PNUT-native-build"})
        with urlopen(request, timeout=45) as response:
            destination = urlsplit(response.geturl())
            if destination.scheme != "https" or destination.hostname != "api.nuget.org":
                raise ValueError("Unexpected SDK download redirect")
            data = response.read(MAX_PACKAGE + 1)
        if len(data) > MAX_PACKAGE:
            raise ValueError("WebView2 SDK exceeds the size limit")
    if hashlib.sha256(data).hexdigest() != pin["sha256"]:
        raise ValueError("WebView2 SDK checksum does not match the pinned package")
    if not cache.exists():
        with tempfile.NamedTemporaryFile(dir=cache.parent, delete=False) as stream:
            pending = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.replace(pending, cache)
        finally:
            pending.unlink(missing_ok=True)
    return data


def prepare_sdk() -> tuple[dict[str, str], Path]:
    if not BUILD.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("Native build directory must remain inside the workspace")
    pin = sdk_pin()
    cache = BUILD / "sdk-cache" / f"microsoft.web.webview2.{pin['version']}.nupkg"
    data = verified_package(pin, cache)
    sdk = BUILD / "sdk" / pin["version"]
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in SDK_FILES:
            entry = archive.getinfo(name)
            if entry.file_size > MAX_PACKAGE:
                raise ValueError("SDK file exceeds the size limit")
            target = sdk / name
            if not target.resolve().is_relative_to(BUILD.resolve()):
                raise ValueError("SDK file escapes the build directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(entry))
    return pin, sdk


def msbuild() -> tuple[Path, str]:
    installer = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    if not installer.is_file():
        raise RuntimeError("Visual Studio C++ Build Tools and Windows SDK are required to build the Atlas adapter")
    result = subprocess.run([str(installer), "-latest", "-products", "*", "-requires",
                             "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-format", "json", "-utf8"],
                            capture_output=True, text=True, encoding="utf-8", check=True, timeout=20)
    installations = json.loads(result.stdout)
    if not installations:
        raise RuntimeError("Visual Studio C++ x64 build tools were not found")
    install = Path(installations[0]["installationPath"])
    compiler = install / "MSBuild/Current/Bin/MSBuild.exe"
    toolset = "v145" if int(installations[0]["installationVersion"].split(".")[0]) >= 18 else "v143"
    if not compiler.is_file():
        raise RuntimeError("MSBuild is missing from the C++ build tools installation")
    return compiler, toolset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-only", action="store_true", help="Verify and prepare the pinned SDK without compiling")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("The Atlas adapter targets Windows x64")
    pin, sdk = prepare_sdk()
    print(f"Verified Microsoft.Web.WebView2 {pin['version']} SDK", flush=True)
    if args.sdk_only:
        return 0
    compiler, toolset = msbuild()
    project = ROOT / "native/atlas_webview/atlas_webview.vcxproj"
    subprocess.run([str(compiler), str(project), "/nologo", "/m", "/t:Rebuild", "/v:minimal",
                    "/p:Configuration=Release", "/p:Platform=x64", f"/p:PlatformToolset={toolset}",
                    f"/p:WebView2SdkRoot={sdk}", f"/p:PnutOutputDir={BUILD}"],
                   cwd=ROOT, check=True, timeout=180)
    output = BUILD / "pnut_atlas_webview.dll"
    if not output.is_file() or not output.stat().st_size:
        raise RuntimeError("Native Atlas build produced no adapter DLL")
    print(f"Built Atlas adapter: {output.stat().st_size:,} bytes", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
