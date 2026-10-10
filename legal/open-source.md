# Open-source licenses and notices

Effective October 10, 2026. These notices describe the components in the official Windows build. Their full license texts and copyright notices are bundled with the app and can be read offline through the links below. Third-party components retain their own licenses; PNUT's MIT license applies to PNUT's original code.

## PNUT

Copyright (c) 2026 PNUT M&M contributors. PNUT is available under the [MIT License](licenses/PNUT-MIT.txt), including its warranty and liability disclaimer. [PNUT source and release history](https://github.com/Maergoth/pnut-mnm) are publicly available. Permission to use game names or third-party trademarks does not come from the MIT license.

## Bundled components

| Component | Version | License and full notices |
| --- | --- | --- |
| CPython and its Windows runtime components | 3.14.3 | [PSF license, historical Python licenses, and bundled native notices](licenses/CPython-3.14.3.txt) |
| PySide6, PySide6 Essentials/Addons, and Shiboken6 | 6.11.2 | LGPL-3.0-only; [PySide and Shiboken license texts and attributions](licenses/Qt-6.11.2-pyside-setup.txt) |
| Qt Core, GUI, Widgets, Network and OpenGL | 6.11.2 | LGPL-3.0-only and separate third-party licenses; [Qt Base notices](licenses/Qt-6.11.2-qtbase.txt) |
| Qt SVG | 6.11.2 | LGPL-3.0-only and separate third-party licenses; [Qt SVG notices](licenses/Qt-6.11.2-qtsvg.txt) |
| Qt Multimedia | 6.11.2 | LGPL-3.0-only and separate third-party licenses; [Qt Multimedia notices](licenses/Qt-6.11.2-qtmultimedia.txt) |
| Qt TextToSpeech | 6.11.2 | LGPL-3.0-only; [Qt Speech notices](licenses/Qt-6.11.2-qtspeech.txt) |
| FFmpeg shared libraries used by Qt Multimedia | 7.1.5 | LGPL-2.1-or-later and separate third-party licenses; [full license texts](licenses/FFmpeg-7.1.5.txt), [binary version and build configuration](licenses/Qt-FFmpeg-build.txt), and the FFmpeg attributions in the Qt Multimedia notices above |
| NumPy and its bundled native code | 2.5.3 | BSD-3-Clause and additional component licenses; [full wheel notices](licenses/numpy-2.5.3.txt), including its math libraries and runtime exceptions |
| opencv-python and its OpenCV native code | 5.0.0.93 | MIT for the Python packaging, Apache-2.0 for OpenCV, and additional component licenses; [full wheel and native third-party notices](licenses/opencv-python-5.0.0.93.txt) |
| Pillow and its bundled image libraries | 12.3.0 | MIT-CMU and additional component licenses; [full Pillow and native notices](licenses/pillow-12.3.0.txt) |
| MSS | 10.2.0 | MIT; [full license](licenses/mss-10.2.0.txt) |
| windows-capture | 2.0.1 | MIT; [full license](licenses/windows-capture-2.0.1.txt) and [native Rust dependency notices](licenses/windows-capture-2.0.1-Rust-dependencies.txt) |
| PyWinRT runtime and Windows Foundation, Foundation.Collections, Globalization, Graphics.Imaging, Media.Ocr, and Storage.Streams projections | 3.2.1 | MIT; [full license](licenses/PyWinRT-3.2.1.txt) |
| typing_extensions | 4.16.0 | PSF-2.0; [full license](licenses/typing_extensions-4.16.0.txt) |
| PyInstaller bootloader and runtime support | 6.22.3 | GPL-2.0-or-later with the Bootloader Distribution Exception; [full license and exception](licenses/pyinstaller-6.22.3.txt) |
| OpenSSL | 3.0.18 | Apache-2.0; [full license and upstream authors](licenses/OpenSSL-3.0.18.txt) |

The Qt archives, OpenCV wheel and Rust lockfile contain notices for some upstream components and build tools that are not used by PNUT. Their inclusion preserves upstream notices and does not apply their licenses to unrelated PNUT code. GPL-only Qt Virtual Keyboard and the unused Qt PDF, Quick, QML and WebEngine components are excluded from the Windows build. The unused OpenCV FFmpeg video backend DLL is also excluded; Qt's FFmpeg audio backend remains included. Optional RapidOCR/ONNX is not included in that build.

## Qt and FFmpeg: source, modification and replacement

PNUT uses unmodified Qt/PySide/Shiboken and FFmpeg libraries supplied with the pinned upstream wheels. Qt and Qt for Python are copyright The Qt Company Ltd. and contributors. FFmpeg is copyright the FFmpeg developers; individual copyright notices are preserved in the linked notices. Their use is covered by the LGPL licenses described above. The Qt notice collections include the full LGPL version 3 and GPL version 3 texts; the FFmpeg collection includes the full LGPL version 2.1 text.

You may modify these libraries and replace them with compatible versions, including for your own use. PNUT imposes no restriction on reverse engineering for debugging modifications to these libraries. The Terms and Disclaimer do not reduce rights granted by their licenses. The official Windows distribution uses separate dynamically loaded DLLs in `_internal/PySide6` and the corresponding Python extension modules, rather than incorporating these libraries into PNUT's executable. Replace compatible library files in an extracted app copy while the app is closed. Keep matching Python, Qt and binding ABIs together. The updater can replace an installation, so keep your modified copy separately or rebuild it from source.

Matching upstream source and build materials are available without charge:

- [Qt 6.11.2 source archives](https://download.qt.io/archive/qt/6.11/6.11.2/submodules/): `qtbase`, `qtsvg`, `qtmultimedia`, and `qtspeech`, each at version 6.11.2. These archives include their third-party sources, license notices, and build files.
- [Qt for Python / PySide / Shiboken 6.11.2 source archive](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/pyside-setup-everywhere-src-6.11.2.tar.xz) and [Qt for Python build instructions](https://doc.qt.io/qtforpython-6/building_from_source/index.html).
- [FFmpeg 7.1.5 source](https://github.com/FFmpeg/FFmpeg/tree/n7.1.5), [source archive](https://github.com/FFmpeg/FFmpeg/archive/refs/tags/n7.1.5.tar.gz), and [Qt's matching FFmpeg build scripts](https://code.qt.io/cgit/qt/qtmultimedia.git/tree/src/3rdparty/ffmpeg?h=v6.11.2). The exact configure options are preserved in the bundled build notice. The configured external zlib dependency is version 1.3.1; its license is in Qt's notices and its [source is available here](https://github.com/madler/zlib/tree/v1.3.1).
- [opencv-python 5.0.0.93 source archive](https://files.pythonhosted.org/packages/79/4c/a438d23e09ce2033c09f7b784ad2fbdb0adf529e434101ed28f142226f98/opencv_python-5.0.0.93.tar.gz) and [release metadata](https://pypi.org/project/opencv-python/5.0.0.93/). The archive's SHA-256 is `66aac3e5b5faa48d4025816592f3af19e4bfc2c68dec067bae2dbb4ca10aa9e2`. Its native third-party licenses are preserved in the OpenCV notices.
- [PNUT source, pinned requirements and Windows build script](https://github.com/Maergoth/pnut-mnm). Use the source tag matching your PNUT release, create a Python 3.14 virtual environment, install `requirements.txt` and PyInstaller 6.22.3, then run `build_exe.ps1 -Clean`. To use modified dependencies, update the pinned versions and review/regenerate the notices before building. The executable is not locked to a signature or an integrity check on replacement libraries.

[Qt's LGPL guidance](https://www.qt.io/development/open-source-lgpl-obligations) and [FFmpeg's licensing information](https://ffmpeg.org/legal.html) describe the upstream licenses. The supplied license texts govern your rights.

## Notice provenance

The notice generator copies installed wheel license files and the local CPython license without rewriting their text. Missing wheel licenses and native Qt attributions are copied from versioned upstream source archives. The [upstream provenance manifest](licenses/upstream-manifest.json) records source URLs and SHA-256 digests. Rust crate archives are verified against the upstream Cargo.lock checksums; their individual source URLs and digests are retained in the notice collection.

The normal build runs the generator offline and rejects unreviewed dependency, Python, OpenSSL or Qt FFmpeg version changes. Maintainers can deliberately refresh the pinned upstream material with `scripts/generate_legal_notices.py --refresh-upstream`, review the notices, and verify them with `--check`. Redistributors must preserve the relevant notices and fulfill the applicable licenses' source and replacement requirements.
