"""Pinned portable build runtime: no network or real install during unit checks."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts import prepare_build_python as runtime


def package(extra=()):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in sorted(runtime.REQUIRED):
            archive.writestr(name, b"synthetic runtime")
        for name, body in extra:
            archive.writestr(name, body)
    return stream.getvalue()


class PrepareBuildPythonTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def test_validated_package_is_prepared_atomically_and_reused_without_download(self):
        data = package()
        output = self.root / "runtime"
        download = Mock(return_value=data)
        probe = Mock(return_value={})
        with patch.object(runtime, "PACKAGE_SHA256", hashlib.sha256(data).hexdigest()):
            executable = runtime.prepare_python(output, download=download, probe=probe)
            self.assertEqual(executable, output / "tools/python.exe")
            self.assertTrue(executable.is_file())
            self.assertEqual(runtime.prepare_python(output, download=download, probe=probe), executable)
        download.assert_called_once()
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(list(self.root.glob(".pnut-python-*")), [])

    def test_hash_failure_precedes_extraction_and_execution(self):
        probe = Mock()
        with self.assertRaisesRegex(ValueError, "checksum"):
            runtime.prepare_python(self.root / "runtime", download=lambda: package(), probe=probe)
        probe.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())

    def test_oversized_download_is_rejected(self):
        with patch.object(runtime, "MAX_PACKAGE_BYTES", 3):
            with self.assertRaises(ValueError):
                runtime.verified_package(download=lambda: b"four")

    def test_invalid_existing_output_is_preserved(self):
        output = self.root / "existing"
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(ValueError, "Nothing was overwritten"):
            runtime.prepare_python(output, download=Mock(side_effect=AssertionError("downloaded")))
        self.assertEqual(sentinel.read_text(), "keep")

    def test_failed_runtime_validation_never_publishes_partial_destination(self):
        data = package()
        with patch.object(runtime, "PACKAGE_SHA256", hashlib.sha256(data).hexdigest()):
            with self.assertRaisesRegex(ValueError, "probe failed"):
                runtime.prepare_python(self.root / "runtime", download=lambda: data,
                                       probe=Mock(side_effect=ValueError("probe failed")))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_unsafe_paths_and_duplicate_windows_names_are_rejected_before_writes(self):
        for name in ("../escape", "/absolute", "tools/../../escape", "tools\\..\\escape", "C:/escape",
                     "tools/file:stream", "tools/CON.txt", "tools/trailing.", "tools/dot/./file"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "Unsafe"):
                    runtime.safe_extract(package([(name, b"bad")]), self.root)
                self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            runtime.safe_extract(package([("TOOLS/PYTHON.EXE", b"bad")]), self.root)

    def test_symlinks_and_extraction_bounds_are_rejected(self):
        link = zipfile.ZipInfo("tools/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            runtime.safe_extract(package([(link, b"../outside")]), self.root)
        for limit in ("MAX_FILES", "MAX_EXTRACT_BYTES", "MAX_FILE_BYTES"):
            with self.subTest(limit=limit), patch.object(runtime, limit, 1):
                with self.assertRaises(ValueError):
                    runtime.safe_extract(package(), self.root)

    def test_missing_required_runtime_files_are_rejected(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("tools/python.exe", b"incomplete")
        with self.assertRaisesRegex(ValueError, "missing required"):
            runtime.safe_extract(stream.getvalue(), self.root)

    def test_download_redirects_stay_on_official_https_hosts(self):
        for url in ("http://www.nuget.org/package", "https://example.com/package", "https://user@api.nuget.org/package"):
            self.assertFalse(runtime.official_url(url))
        self.assertTrue(runtime.official_url("https://globalcdn.nuget.org/packages/python.3.14.10.nupkg"))

    def test_real_probe_rejects_wrong_version_ssl_and_architecture(self):
        good = {"python": "3.14.10", "implementation": "CPython", "openssl": "OpenSSL 3.5.9 29 Sep 2026", "bits": 64}
        with patch.object(runtime, "verify_signatures"):
            for changes in ({"python": "3.14.3"}, {"openssl": "OpenSSL 3.0.0"}, {"bits": 32}):
                with self.subTest(changes=changes), patch.object(runtime.subprocess, "run",
                    return_value=Mock(stdout=json.dumps({**good, **changes}))):
                    with self.assertRaises(ValueError):
                        runtime.validate_runtime(self.root / "python.exe")
            with patch.object(runtime.subprocess, "run", return_value=Mock(stdout=json.dumps(good))):
                self.assertEqual(runtime.validate_runtime(self.root / "python.exe"), good)
