"""Isolated output selection never replaces a candidate's personal files."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from mnmparse import __version__


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pnut_release_paths", ROOT / "scripts" / "package_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def quote_ps(value):
    return "'" + str(value).replace("'", "''") + "'"


def directory_link(link: Path, target: Path):
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise unittest.SkipTest("Directory links unavailable")
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command",
            "New-Item -ItemType Junction -Path " + quote_ps(link) + " -Value " + quote_ps(target)],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise unittest.SkipTest("Directory links unavailable")


class ReleaseOutputPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.package = self.root / "dist/candidates/new/PNUT M&M"
        (self.package / "_internal").mkdir(parents=True)
        (self.package / "PNUT M&M.exe").write_bytes(b"test executable")
        self.info = {"schema": 1, "version": __version__, "commit": "a" * 40, "dirty": True,
                     "built_at": "2026-10-10T12:00:00+00:00"}
        (self.package / "_internal/build-info.json").write_text(json.dumps(self.info))
        self.output = self.root / "dist/candidates/new/releases"
        self.original = self.root / "dist/PNUT M&M"
        self.original.mkdir()
        (self.original / "config.json").write_bytes(b"personal preferences")

    def invoke(self, create):
        arguments = ["package_release.py", "--allow-dirty", "--package-dir", "dist/candidates/new/PNUT M&M",
                     "--output-dir", "dist/candidates/new/releases"]
        with patch.object(release, "ROOT", self.root), patch.object(sys, "argv", arguments), \
                patch.object(release.subprocess, "check_output", side_effect=["a" * 40, " M file"]), \
                patch.object(release, "_create_verified_archive", side_effect=create) as verify:
            result = release.main()
        return result, verify

    def test_custom_candidate_and_artifact_paths_preserve_original_preferences(self):
        def verified(package, archive, report, info):
            self.assertEqual(package, self.package)
            self.assertEqual(report.parent, self.output)
            self.assertEqual(info, self.info)
            archive.write_bytes(b"verified ZIP")
            return "b" * 64
        result, verify = self.invoke(verified)
        self.assertEqual(result, 0)
        self.assertEqual(verify.call_count, 1)
        archive = self.output / f"PNUT-MnM-{__version__}-windows.zip"
        self.assertEqual(archive.read_bytes(), b"verified ZIP")
        self.assertIn("b" * 64, archive.with_suffix(".zip.sha256").read_text())
        self.assertEqual((self.original / "config.json").read_bytes(), b"personal preferences")

    def test_failed_custom_smoke_preserves_previous_verified_artifact(self):
        self.output.mkdir(parents=True)
        archive = self.output / f"PNUT-MnM-{__version__}-windows.zip"
        archive.write_bytes(b"previous verified ZIP")
        def failed(package, working, report, info):
            working.write_bytes(b"partial new ZIP")
            raise RuntimeError("smoke failed")
        with self.assertRaisesRegex(RuntimeError, "smoke failed"):
            self.invoke(failed)
        self.assertEqual(archive.read_bytes(), b"previous verified ZIP")
        self.assertEqual(list(self.output.glob(".pnut-package-*")), [])

    def test_output_paths_reject_workspace_escape_and_linked_ancestors(self):
        directory_link(self.root / "linked", self.package)
        with patch.object(release, "ROOT", self.root):
            for candidate in ("..", self.root, self.root / "linked/artifacts"):
                with self.subTest(candidate=candidate), self.assertRaises(RuntimeError):
                    release._workspace_directory(candidate)
            self.assertEqual(release._workspace_directory("dist/candidates/new"), self.package.parent)

    def test_custom_bundle_rejects_nested_links_before_packaging(self):
        directory_link(self.package / "_internal/linked", self.original)
        with self.assertRaisesRegex(RuntimeError, "linked"):
            release._validate_package(self.package)


@unittest.skipUnless(sys.platform == "win32", "PowerShell build-path validation is Windows-only")
class BuildOutputPathTests(unittest.TestCase):
    def test_actual_build_resolver_accepts_new_output_and_rejects_escape_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            directory_link(root / "linked", real)
            command = (
                "$tokens=$null; $errors=$null; "
                "$ast=[Management.Automation.Language.Parser]::ParseFile(" + quote_ps(ROOT / "build_exe.ps1") + ",[ref]$tokens,[ref]$errors); "
                "if ($errors.Count) { throw $errors[0] }; "
                "$function=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resolve-WorkspaceDirectory'},$true); "
                "Invoke-Expression $function.Extent.Text; $root=" + quote_ps(root) + "; "
                "$resolved=Resolve-WorkspaceDirectory 'dist/candidates/setup-fix-20261010'; "
                "$rejected=@(); foreach($path in @('..','linked/new')) { "
                "try { $null=Resolve-WorkspaceDirectory $path } catch { $rejected+=$path } }; "
                "@{resolved=$resolved;rejected=$rejected} | ConvertTo-Json -Compress"
            )
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr)
            values = json.loads(result.stdout)
            # PowerShell expands 8.3 TEMP aliases while tempfile may retain them.
            self.assertEqual(Path(values["resolved"]).resolve(),
                             (root / "dist/candidates/setup-fix-20261010").resolve())
            self.assertEqual(values["rejected"], ["..", "linked/new"])


if __name__ == "__main__":
    unittest.main()
