# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PNUT M&M desktop app (APP_SPEC section 10).

Build with ``build_exe.ps1`` or directly:
    .venv\\Scripts\\python -m PyInstaller mnmparser.spec --noconfirm

Output: ``dist\\PNUT M&M\\PNUT M&M.exe`` (one-dir, windowed, icon).  Only the Qt modules
the app imports are collected, including the lazily used Qt WebEngine browser and
its runtime dependencies. Qt3D and other unused families are excluded, as are
onnxruntime/rapidocr (the optional OCR fallback) and the old Tk crop picker.
"""

from __future__ import annotations

import glob
import os
import sys
import sysconfig
from pathlib import Path

from PyInstaller.depend.bindepend import get_imports
from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()
APP_NAME = "PNUT M&M"
ICON = ROOT / "assets" / "icon.ico"

# Resolve native dependencies against this Python installation and Windows, never
# unrelated programs in the caller's PATH. In particular, Qt uses Windows' ICU:
# a Poppler/Conda ICU with the same filename has incompatible versioned exports.
# Keep this in the spec so direct PyInstaller builds get the same isolation.
windows = Path(os.environ["SystemRoot"])
os.environ["PATH"] = os.pathsep.join(str(p) for p in (
    Path(sys.prefix) / "Scripts", Path(sys.base_prefix), windows / "System32", windows,
))

# ---------------------------------------------------------------------------
# Hidden imports: the winrt projection modules are imported lazily inside ocr.py and the
# compiled ``_winrt*`` extension modules sit at the top level of site-packages.
# ---------------------------------------------------------------------------
hiddenimports = [
    "winrt.windows.media.ocr",
    "winrt.windows.graphics.imaging",
    "winrt.windows.storage.streams",
    "winrt.windows.globalization",
    "winrt.windows.foundation",
    "winrt.windows.foundation.collections",
    "winrt.system",
    "winrt.runtime",
    "windows_capture",
    "mss",
    "mss.windows",
    "cv2",
    "numpy",
    "mnmparse.app.engine",
    "mnmparse.app.overlay",
    "mnmparse.app.pages",
    "mnmparse.app.widgets",
    "mnmparse.app.crop_picker",
    "mnmparse.app.models",
    # Triggers: sounds (QSoundEffect / QMediaPlayer) and speech (QTextToSpeech: SAPI and WinRT voices)
    "PySide6.QtMultimedia",
    "PySide6.QtTextToSpeech",
    # The Atlas widget imports these only after the user selects its provider.
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "mnmparse.app.triggers_runtime",
]

site_packages = sysconfig.get_paths().get("purelib") or ""
for pyd in glob.glob(os.path.join(site_packages, "_winrt*.pyd")):
    module = os.path.basename(pyd).split(".", 1)[0]
    if module not in hiddenimports:
        hiddenimports.append(module)

datas: list[tuple[str, str]] = []
binaries: list[tuple[str, str]] = []
for package in ("winrt", "windows_capture"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:  # package missing on this machine: the build still works without it
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += [m for m in pkg_hidden if m not in hiddenimports]

hiddenimports += [
    m
    for m in collect_submodules("mnmparse")
    if m not in hiddenimports and m != "mnmparse.calibrate"  # the Tk crop picker is excluded below
]

if ICON.is_file():
    datas.append((str(ICON), "assets"))
datas.append((str(ROOT / "assets" / "bundled_sounds"), "assets/bundled_sounds"))
example = ROOT / "config.example.json"
if example.is_file():
    datas.append((str(example), "."))
datas.append((str(ROOT / "legal"), "legal"))
provenance = ROOT / "build" / "build-info.json"
if not provenance.is_file():
    raise RuntimeError("Run build_exe.ps1 to generate current build provenance before freezing")
datas.append((str(provenance), "."))
hiddenimports += ["regex", "regex._regex"]

excludes = [
    # Optional OCR fallback (big): set "ocr_engine": "rapid" only in a source checkout.
    "onnxruntime",
    "rapidocr_onnxruntime",
    # Unused GUI toolkits / tooling.
    "tkinter",
    "_tkinter",
    "mnmparse.calibrate",
    "matplotlib",
    "IPython",
    "pytest",
    "PyQt5",
    "PyQt6",
    # PySide6 modules the app never imports.
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtDesigner",
    "PySide6.QtUiTools",
    "PySide6.QtHelp",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtXml",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects",
    "PySide6.QtScxml",
    "PySide6.QtStateMachine",
    "PySide6.QtHttpServer",
    "PySide6.QtSpatialAudio",
    "PySide6.QtAsyncio",
]

a = Analysis(
    [str(ROOT / "mnmparse" / "app" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Belt and braces: drop any Qt payload from the excluded module families that a hook pulled
# in anyway (translations, plugins, DLLs).
_DROP = (
    "qt3d",
    "multimediawidgets",
    "charts",
    "datavisualization",
    "qtgraphs",
    "designer",
    "pdf",
    "virtualkeyboard",
    "location",
    "sensors",
    "serialport",
    "bluetooth",
    "remoteobjects",
    "scxml",
    "statemachine",
    "websockets",
    "httpserver",
)


def _keep(entry: tuple) -> bool:
    name = entry[0].replace("\\", "/").lower()
    # PNUT uses OpenCV image transforms and PNG encoding, never its video backend.
    if name.startswith("cv2/opencv_videoio_ffmpeg"):
        return False
    # Release WebEngine uses the ordinary packs; debug packs add ~80 MiB.
    if "/resources/" in name and (".debug.pak" in name or ".debug.bin" in name):
        return False
    if "pyside6" not in name and "/qt6/" not in name and not name.startswith("qt6"):
        return True
    # QWebEngineWidgets builds its QQuickWidget content programmatically; PNUT
    # does not import QML modules. QtQml's generic hook otherwise collects the
    # whole installed QML tree, including unrelated GPL-only module families.
    if "/qml/" in name or "/plugins/qmltooling/" in name:
        return False
    return not any(tag in name for tag in _DROP)


def _prune_qt_dlls(entries: list[tuple]) -> list[tuple]:
    """Keep Qt DLLs reachable from retained extensions, helper EXEs and plugins.

    Removing QML files alone leaves their native dependencies in Analysis. Use
    the actual PE import graph rather than broad 'quick'/'qml' substring rules:
    the Widgets browser needs Quick/QuickWidgets/Qml, but not Quick3D/Graphs.
    Dynamically loaded plugin DLLs remain roots, including audio/TTS/TLS plugins.
    """
    def basename(entry):
        return entry[0].replace("\\", "/").rsplit("/", 1)[-1].lower()

    qt_dlls = {basename(entry): entry for entry in entries
               if basename(entry).startswith("qt6") and basename(entry).endswith(".dll")}
    pending = [entry for entry in entries if basename(entry) not in qt_dlls]
    needed = set()
    seen = set()
    while pending:
        entry = pending.pop()
        if entry[1] in seen:
            continue
        seen.add(entry[1])
        for imported, _resolved in get_imports(entry[1], search_paths=[str(Path(entry[1]).parent)]):
            name = imported.lower()
            if name in qt_dlls and name not in needed:
                needed.add(name)
                pending.append(qt_dlls[name])
    return [entry for entry in entries if basename(entry) not in qt_dlls or basename(entry) in needed]


a.binaries = [b for b in a.binaries if _keep(b)]
a.binaries = _prune_qt_dlls(a.binaries)
a.datas = [d for d in a.datas if _keep(d) and Path(d[0]).name != "direct_url.json"]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
