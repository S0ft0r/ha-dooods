"""Tests for versioning and delta archive behavior."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile

from scripts.build_release import (
    BuildError,
    build_component_zip,
    build_delta,
    build_delta_from_git,
    verify_current_state,
)


class ReleaseBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "custom_components/domres").mkdir(parents=True)
        (self.root / "scripts").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "blueprint").mkdir(parents=True)
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / "custom_components/domres/manifest.json").write_text(
            json.dumps({"domain": "domres", "version": "0.1.0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.root / "custom_components/domres/__init__.py").write_text("# initial\n", encoding="utf-8")
        (self.root / "scripts/validate.py").write_text("# validator\n", encoding="utf-8")
        (self.root / "tests/test_smoke.py").write_text("# tests\n", encoding="utf-8")
        (self.root / "blueprint/example.yaml").write_text(
            "blueprint: {}\n", encoding="utf-8"
        )
        (self.root / ".github/workflows/validate.yml").write_text("name: validate\n", encoding="utf-8")
        (self.root / "README.md").write_text("Public documentation\n", encoding="utf-8")
        (self.root / "internal").mkdir()
        (self.root / "internal/decision.md").write_text("not for release\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_first_archive_includes_public_project_files_only(self) -> None:
        archive_path, changed, deleted = build_delta(self.root)
        self.assertEqual(deleted, [])
        self.assertIn("custom_components/domres/__init__.py", changed)
        self.assertIn("scripts/validate.py", changed)
        self.assertIn("tests/test_smoke.py", changed)
        self.assertNotIn("blueprint/example.yaml", changed)
        self.assertIn(".github/workflows/validate.yml", changed)
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
        self.assertIn("README.md", names)
        self.assertNotIn("blueprint/example.yaml", names)
        self.assertNotIn("internal/decision.md", names)
        verify_current_state(self.root)

    def test_ci_bootstrap_can_create_first_archive_at_current_patch_version(self) -> None:
        manifest_path = self.root / "custom_components/domres/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.4"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")

        with self.assertRaises(BuildError):
            build_delta(self.root)
        archive_path, changed, _ = build_delta(
            self.root,
            bootstrap_current_version=True,
        )

        self.assertEqual(archive_path.name, "domres_0.1.4_changed_files.zip")
        self.assertIn("custom_components/domres/__init__.py", changed)
        verify_current_state(self.root)

    @unittest.skipUnless(shutil.which("git"), "git is required for Git-tag delta tests")
    def test_git_tag_delta_allows_skipped_patch_versions_without_build_state(self) -> None:
        def git(*args: str) -> None:
            subprocess.run(
                ["git", *args],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q")
        git("config", "user.name", "DomRes test")
        git("config", "user.email", "domres-test@example.invalid")
        git("add", "-A")
        git("commit", "-m", "baseline")
        git("tag", "v0.1.2")

        manifest_path = self.root / "custom_components/domres/manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = "0.1.4"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        (self.root / "scripts/validate.py").write_text("# updated validator\n", encoding="utf-8")
        (self.root / "tests/test_smoke.py").unlink()
        (self.root / "internal/decision.md").write_text("still private\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-m", "skip 0.1.3 and prepare 0.1.4")

        archive_path, changed, deleted = build_delta_from_git(self.root, "v0.1.2")
        self.assertEqual(archive_path.name, "domres_0.1.4_changed_files.zip")
        self.assertIn("custom_components/domres/manifest.json", changed)
        self.assertIn("scripts/validate.py", changed)
        self.assertEqual(deleted, ["tests/test_smoke.py"])
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            deleted_names = archive.read("DELETED_FILES.txt").decode("utf-8").splitlines()
        self.assertNotIn("internal/decision.md", names)
        self.assertEqual(deleted_names, ["tests/test_smoke.py"])
        verify_current_state(self.root)

        # A retry can rebuild missing state without overwriting an identical retained ZIP.
        state_path = self.root / "release/.build_state.json"
        state_path.unlink()
        reused_path, _, _ = build_delta_from_git(self.root, "v0.1.2")
        self.assertEqual(reused_path, archive_path)
        verify_current_state(self.root)

        # A stale archive for this unpublished version is atomically regenerated.
        state_path.unlink()
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("stale.txt", "wrong baseline")
        replaced_path, _, _ = build_delta_from_git(self.root, "v0.1.2")
        self.assertEqual(replaced_path, archive_path)
        with zipfile.ZipFile(replaced_path) as archive:
            self.assertNotIn("stale.txt", archive.namelist())
        verify_current_state(self.root)

        # Once the version is tagged, the tag-based recovery path must not replace it.
        state_path.unlink()
        git("tag", "v0.1.4")
        with self.assertRaises(BuildError):
            build_delta_from_git(self.root, "v0.1.2")

    def test_next_build_bumps_patch_and_keeps_previous_archive(self) -> None:
        first_archive, _, _ = build_delta(self.root)
        (self.root / "custom_components/domres/api.py").write_text("# changed\n", encoding="utf-8")
        second_archive, changed, _ = build_delta(self.root)
        manifest = json.loads(
            (self.root / "custom_components/domres/manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "0.1.1")
        self.assertEqual(first_archive.name, "domres_0.1.0_changed_files.zip")
        self.assertTrue(first_archive.is_file())
        self.assertEqual(second_archive.name, "domres_0.1.1_changed_files.zip")
        self.assertIn("custom_components/domres/api.py", changed)
        self.assertIn("custom_components/domres/manifest.json", changed)
        verify_current_state(self.root)

    def test_component_zip_has_domres_root(self) -> None:
        target = build_component_zip(self.root, "component.zip")
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
        self.assertIn("domres/manifest.json", names)
        self.assertIn("domres/__init__.py", names)

    def test_deleted_file_is_recorded_and_old_archive_is_retained(self) -> None:
        first_archive, _, _ = build_delta(self.root)
        (self.root / "scripts/validate.py").unlink()
        second_archive, _, deleted = build_delta(self.root)
        self.assertEqual(deleted, ["scripts/validate.py"])
        self.assertTrue(first_archive.is_file())
        with zipfile.ZipFile(second_archive) as archive:
            manifest = archive.read("DELETED_FILES.txt").decode("utf-8")
        self.assertEqual(manifest.strip(), "scripts/validate.py")

    def test_no_change_does_not_create_duplicate_build(self) -> None:
        build_delta(self.root)
        with self.assertRaises(BuildError):
            build_delta(self.root)


if __name__ == "__main__":
    unittest.main()
