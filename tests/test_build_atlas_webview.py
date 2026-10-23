"""The small Atlas build reuses only the verified, pinned SDK inputs."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import build_atlas_webview as build
from scripts import generate_legal_notices as notices


class AtlasBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pin = build.sdk_pin()

    def test_verified_cached_package_never_downloads(self):
        package = b"a verified fixture package"
        pin = dict(self.pin, sha256=hashlib.sha256(package).hexdigest())
        cache = self.root / "sdk.nupkg"
        cache.write_bytes(package)
        with patch.object(build, "urlopen", side_effect=AssertionError("unexpected download")):
            self.assertEqual(build.verified_package(pin, cache), package)

    def test_corrupt_cache_is_rejected_without_silent_replacement(self):
        cache = self.root / "sdk.nupkg"
        cache.write_bytes(b"changed bytes")
        with patch.object(build, "urlopen", side_effect=AssertionError("unexpected download")):
            with self.assertRaisesRegex(ValueError, "checksum"):
                build.verified_package(self.pin, cache)
        self.assertEqual(cache.read_bytes(), b"changed bytes")

    def test_pin_cannot_redirect_the_sdk_source(self):
        pin_file = self.root / "sdk.json"
        pin_file.write_text(json.dumps(dict(self.pin, url="https://example.com/sdk.zip")))
        with patch.object(build, "PIN", pin_file):
            with self.assertRaisesRegex(ValueError, "pinned"):
                build.sdk_pin()

    def test_sdk_preparation_extracts_only_headers_and_static_loader(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            for name in build.SDK_FILES:
                archive.writestr(name, name.encode())
            archive.writestr("../escape.dll", b"not extracted")
            archive.writestr("runtimes/win-x64/native/WebView2Loader.dll", b"not bundled")
        pin = dict(self.pin, sha256=hashlib.sha256(stream.getvalue()).hexdigest())
        output = self.root / "build/atlas_webview"
        with patch.object(build, "ROOT", self.root), patch.object(build, "BUILD", output), \
                patch.object(build, "sdk_pin", return_value=pin), \
                patch.object(build, "verified_package", return_value=stream.getvalue()):
            _, sdk = build.prepare_sdk()
        extracted = {str(path.relative_to(sdk)).replace("\\", "/")
                     for path in sdk.rglob("*") if path.is_file()}
        self.assertEqual(extracted, set(build.SDK_FILES))
        self.assertFalse((self.root / "escape.dll").exists())

    def test_external_build_directory_is_rejected_before_fetch(self):
        with patch.object(build, "ROOT", self.root / "repo"), patch.object(build, "BUILD", self.root / "other"), \
                patch.object(build, "verified_package", side_effect=AssertionError("unexpected SDK access")):
            with self.assertRaisesRegex(ValueError, "inside the workspace"):
                build.prepare_sdk()

    def test_sdk_notice_keeps_both_upstream_texts_in_full(self):
        stream = io.BytesIO()
        texts = {"LICENSE.txt": b"full fixture license\r\n", "NOTICE.txt": b"full fixture notices\r\n"}
        with zipfile.ZipFile(stream, "w") as archive:
            for name, content in texts.items():
                archive.writestr(name, content)
        data = stream.getvalue()
        pin = dict(self.pin, sha256=hashlib.sha256(data).hexdigest())
        with patch.object(notices, "webview2_pin", return_value=pin):
            name, content, source, source_hash = notices.webview2_notices(data)
            for text in texts.values():
                self.assertIn(text, content)
            self.assertEqual(name, notices.WEBVIEW2_NOTICE)
            self.assertEqual(source, pin["url"])
            self.assertEqual(source_hash, pin["sha256"])
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                notices.webview2_notices(data + b"changed")


if __name__ == "__main__":
    unittest.main()
