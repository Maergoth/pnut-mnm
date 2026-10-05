# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the PNUT M&M desktop app (APP_SPEC section 10).

Build with ``build_exe.ps1`` or directly:
    .venv\\Scripts\\python -m PyInstaller mnmparser.spec --noconfirm

Output: ``dist\\PNUT M&M\\PNUT M&M.exe`` (one-dir, windowed, icon).  Only the Qt modules
the app imports (QtCore/QtGui/QtWidgets, plus QtMultimedia/QtTextToSpeech for the trigger
sounds and speech) are collected; the big optional ones (WebEngine, Qt3D, Quick/QML, ...) are
excluded, as are onnxruntime/rapidocr (the optional OCR fallback) and tkinter (only the old CLI
crop picker uses it).
"""

from __future__ import annotations

import glob
import os
import sysconfig
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPECPATH).resolve()
APP_NAME = "PNUT M&M"
ICON = ROOT / "assets" / "icon.ico"

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
example = ROOT / "config.example.json"
if example.is_file():
    datas.append((str(example), "."))

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
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtWebView",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2",
    "PySide6.QtQml",
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
    "PySide6.QtPositioning",
    "PySide6.QtLocation",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtXml",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport",
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
    "webengine",
    "qt3d",
    "quick",
    "qml",
    "multimediawidgets",
    "charts",
    "datavisualization",
    "qtgraphs",
    "designer",
    "pdf",
    "virtualkeyboard",
    "location",
    "positioning",
    "sensors",
    "serialport",
    "bluetooth",
    "remoteobjects",
    "scxml",
    "statemachine",
    "webchannel",
    "websockets",
    "httpserver",
)


def _keep(entry: tuple) -> bool:
    name = entry[0].replace("\\", "/").lower()
    if "pyside6" not in name and "/qt6/" not in name and not name.startswith("qt6"):
        return True
    return not any(tag in name for tag in _DROP)


a.binaries = [b for b in a.binaries if _keep(b)]
a.datas = [d for d in a.datas if _keep(d)]

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
