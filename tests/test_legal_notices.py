"""Release notices must be readable offline and match the shipped dependency audit."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from scripts import generate_legal_notices as notices


class LegalNoticesTests(unittest.TestCase):
    def test_index_local_links_are_real_bundled_notices(self):
        page = (notices.ROOT / "legal" / "open-source.md").read_text(encoding="utf-8")
        links = re.findall(r"\]\((licenses/[^)]+)\)", page)
        self.assertGreaterEqual(len(set(links)), 19)
        for relative in links:
            with self.subTest(relative=relative):
                target = notices.ROOT / "legal" / relative
                self.assertTrue(target.is_file())
                self.assertGreater(target.stat().st_size, 500)

    def test_upstream_collections_match_provenance_hashes(self):
        notices.verify_upstream()
        manifest = json.loads(notices.MANIFEST.read_text(encoding="utf-8"))
        for provenance in manifest.values():
            self.assertTrue(provenance["source"].startswith("https://"))
            self.assertRegex(provenance["source_sha256"], r"^[a-f0-9]{64}$")

    def test_changed_upstream_text_blocks_build(self):
        with tempfile.TemporaryDirectory(prefix="pnut-notice-test-") as directory:
            destination = Path(directory)
            manifest = json.loads(notices.MANIFEST.read_text(encoding="utf-8"))
            for filename in manifest:
                (destination / filename).write_bytes((notices.DEST / filename).read_bytes())
            changed = next(iter(manifest))
            (destination / changed).write_text("truncated license", encoding="utf-8")
            manifest_file = destination / "manifest.json"
            manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(notices, "DEST", destination), patch.object(notices, "MANIFEST", manifest_file):
                with self.assertRaisesRegex(RuntimeError, "Missing or changed upstream notice"):
                    notices.verify_upstream()

    def test_dependency_update_requires_notice_review(self):
        with patch.object(notices.metadata, "version", return_value="99.0.0"):
            with self.assertRaisesRegex(RuntimeError, "Update and review notices before shipping"):
                notices.generated_notices()

    def test_pnut_mit_copy_is_exact(self):
        self.assertEqual((notices.DEST / "PNUT-MIT.txt").read_bytes(), (notices.ROOT / "LICENSE").read_bytes())

    def test_lgpl_and_companion_gpl_texts_are_bundled_in_full(self):
        qt = (notices.DEST / "Qt-6.11.2-qtbase.txt").read_text(encoding="utf-8")
        ffmpeg = (notices.DEST / "FFmpeg-7.1.5.txt").read_text(encoding="utf-8")
        self.assertIn("===== LICENSES/LGPL-3.0-only.txt =====", qt)
        self.assertIn("===== LICENSES/GPL-3.0-only.txt =====", qt)
        self.assertIn("GNU LESSER GENERAL PUBLIC LICENSE", ffmpeg)
        self.assertIn("Version 2.1, February 1999", ffmpeg)
        self.assertIn("END OF TERMS AND CONDITIONS", qt)
        self.assertIn("END OF TERMS AND CONDITIONS", ffmpeg)

    def test_normal_release_verification_never_downloads(self):
        with patch.object(notices, "download", side_effect=AssertionError("Build attempted network access")):
            self.assertEqual(notices.main(["--check"]), 0)

    def test_cryptography_and_transitive_versions_are_pinned_and_noticed(self):
        requirements = (notices.ROOT / "requirements.txt").read_text(encoding="utf-8")
        for name, version in (("cryptography", "50.0.2"), ("cffi", "2.1.1"), ("pycparser", "3.11")):
            with self.subTest(name=name):
                self.assertIn(f"{name}=={version}", requirements)
                self.assertEqual(notices.VERSIONS[name], version)
                wheel_notice = (notices.DEST / f"{name}-{version}.txt").read_text(encoding="utf-8")
                self.assertIn("installed wheel notices", wheel_notice)
                self.assertIn("LICENSE", wheel_notice)
        crypto = (notices.DEST / "cryptography-50.0.2.txt").read_text(encoding="utf-8")
        self.assertIn("===== licenses/LICENSE.APACHE =====", crypto)
        self.assertIn("===== licenses/LICENSE.BSD =====", crypto)

    def test_cryptography_rust_notices_cover_entire_verified_lockfile(self):
        text = (notices.DEST / "cryptography-50.0.2-Rust-dependencies.txt").read_text(encoding="utf-8")
        lock_text = text.split("===== Cargo.lock =====\n", 1)[1].split("\n===== ", 1)[0]
        packages = [package for package in tomllib.loads(lock_text)["package"]
                    if package.get("source", "").startswith("registry+")]
        self.assertGreaterEqual(len(packages), 30)
        for package in packages:
            name, version = package["name"], package["version"]
            with self.subTest(name=name, version=version):
                self.assertIn(f"===== {name}-{version}/PROVENANCE.txt =====", text)
                self.assertIn(f"SHA-256: {package['checksum']}", text)
                self.assertRegex(text, rf"===== {re.escape(name)}-{re.escape(version)}/(?:[^\n/]*/)*(?:LICENSE|COPYING|NOTICE)")

    def test_distinct_openssl_binaries_and_cffi_native_notices_are_bundled(self):
        build = (notices.DEST / "cryptography-OpenSSL-build.txt").read_text(encoding="utf-8")
        self.assertIn(notices.CRYPTOGRAPHY_OPENSSL_VERSION, build)
        self.assertIn("separate from CPython's OpenSSL 3.0.18", build)
        openssl = (notices.DEST / "OpenSSL-4.0.3.txt").read_text(encoding="utf-8")
        self.assertIn("===== AUTHORS.md =====", openssl)
        self.assertIn("Apache License", openssl)
        cffi = (notices.DEST / "cffi-2.1.1-native-notices.txt").read_text(encoding="utf-8")
        self.assertIn("===== src/c/libffi_x86_x64/ffi.c =====", cffi)
        self.assertIn("Permission is hereby granted", cffi)
        self.assertIn("===== setup.py =====", cffi)

    def test_cryptography_openssl_update_requires_native_notice_review(self):
        with patch.object(notices, "cryptography_openssl_version", return_value="OpenSSL changed"):
            with self.assertRaisesRegex(RuntimeError, "cryptography OpenSSL version changed"):
                notices.generated_notices()


if __name__ == "__main__":
    unittest.main()
