#!/usr/bin/env python3
"""Read-only structural and packaging checks for the DOOODS repository.

Run from any directory with:
    python analysis/scripts/validate_repository.py

This script never synchronizes the App or writes release artifacts.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

try:
    from package_local_app import _IGNORED_NAMES
except ModuleNotFoundError as err:
    if err.name != "package_local_app":
        raise
    # Keep the GitHub-validator overlay usable without bundling the App packager.
    _IGNORED_NAMES = {
        ".current_version",
        ".dockerignore",
        ".git",
        ".gitignore",
        ".github",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "LICENSE",
        "Makefile",
        "README.md",
        "SOURCES.md",
        "__pycache__",
        "build",
        "coverage",
        "docker",
        "dist",
        "models",
        "node_modules",
        "out",
        "requirements.pytorch.txt",
        "requirements.txt",
        "target",
        "tests",
    }


ROOT = Path(__file__).resolve().parents[2]
_VERSION_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_FILES = (
    "README.md",
    "CHANGELOG.md",
    "SOURCES.md",
    "TODO.md",
    "custom_integration/dooods/manifest.json",
    "custom_integration/dooods/services.yaml",
    "local_apps/dooods2/config.yaml",
    "local_apps/dooods2/translations/en.yaml",
    "local_apps/dooods2/translations/ru.yaml",
    "analysis/application/config.yaml",
    "analysis/application/capabilities.py",
    "local_apps/dooods2/application/capabilities.py",
    "releases/last_release.json",
)


def _relative(root: Path, path: Path) -> str:
    """Return a stable project-relative path for diagnostics."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_json(path: Path, root: Path, errors: list[str]) -> Any | None:
    """Parse JSON and append a concise diagnostic instead of raising."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        errors.append(f"invalid or unreadable JSON {_relative(root, path)}: {err}")
        return None


def _read_yaml(path: Path, root: Path, errors: list[str]) -> Any | None:
    """Parse YAML and append a concise diagnostic instead of raising."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as err:
        errors.append(f"invalid or unreadable YAML {_relative(root, path)}: {err}")
        return None


def _is_version(value: Any) -> bool:
    """Accept the project's three/four-part numeric versions and dev suffixes."""
    return isinstance(value, str) and _VERSION_RE.fullmatch(value) is not None


def _is_safe_relative_posix_path(value: Any) -> bool:
    """Reject paths that are absolute, non-canonical, or escape an archive root."""
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "." not in path.parts
        and all(":" not in part for part in path.parts)
        and path.as_posix() == value
    )


def _is_safe_archive_member(name: Any) -> bool:
    """Check a ZIP member name before treating it as an install-relative path."""
    return isinstance(name, str) and _is_safe_relative_posix_path(name.rstrip("/"))


def _module_string_constant(path: Path, name: str) -> str | None:
    """Read a top-level string constant from Python source without importing it."""
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError):
        return None

    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    return None


def _copyable_context_files(base: Path) -> dict[str, Path]:
    """List files that package_local_app.py copies, without mutating the tree."""
    files: dict[str, Path] = {}
    if not base.is_dir():
        return files
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in _IGNORED_NAMES for part in relative.parts):
            continue
        files[relative.as_posix()] = path
    return files


def _validate_release_metadata(metadata: Any, errors: list[str]) -> None:
    """Check the durable release baseline's schema and safe file records."""
    if not isinstance(metadata, dict):
        errors.append("releases/last_release.json must contain a JSON object")
        return

    if metadata.get("schema_version") != 1:
        errors.append("releases/last_release.json has an unsupported schema_version")

    versions = metadata.get("versions")
    if not isinstance(versions, dict) or set(versions) != {"integration", "app"}:
        errors.append(
            "releases/last_release.json versions must contain integration and app"
        )
    else:
        for component, version in versions.items():
            if not _is_version(version):
                errors.append(
                    f"releases/last_release.json has an invalid {component} version"
                )

    files = metadata.get("files")
    if not isinstance(files, dict) or not files:
        errors.append("releases/last_release.json files must be a non-empty object")
        return

    for path, digest in files.items():
        if not _is_safe_relative_posix_path(path):
            errors.append(f"unsafe path in releases/last_release.json: {path!r}")
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            errors.append(
                f"invalid SHA-256 in releases/last_release.json for {path!r}"
            )


def _validate_incremental_archive(
    archive: zipfile.ZipFile,
    archive_name: str,
    errors: list[str],
) -> None:
    """Validate an update manifest against the files actually stored in its ZIP."""
    manifest_name = "DOOODS-UPDATE.json"
    names = set(archive.namelist())
    if manifest_name not in names:
        return

    try:
        metadata = json.loads(archive.read(manifest_name))
    except (KeyError, UnicodeError, json.JSONDecodeError) as err:
        errors.append(f"invalid update manifest in {archive_name}: {err}")
        return

    if not isinstance(metadata, dict):
        errors.append(f"update manifest in {archive_name} must be a JSON object")
        return
    if metadata.get("schema_version") != 1:
        errors.append(f"unsupported update manifest schema in {archive_name}")
    if metadata.get("archive_type") != "incremental":
        errors.append(f"archive_type must be incremental in {archive_name}")

    for field in ("base_versions", "target_versions"):
        versions = metadata.get(field)
        if not isinstance(versions, dict) or set(versions) != {"integration", "app"}:
            errors.append(f"{field} must contain integration and app in {archive_name}")
        elif any(not _is_version(version) for version in versions.values()):
            errors.append(f"invalid {field} version in {archive_name}")

    changed_files = metadata.get("changed_files")
    support_files = metadata.get("support_files", [])
    deleted_files = metadata.get("deleted_files")
    if not isinstance(changed_files, list):
        errors.append(f"changed_files must be a list in {archive_name}")
        changed_files = []
    if not isinstance(support_files, list):
        errors.append(f"support_files must be a list in {archive_name}")
        support_files = []
    if not isinstance(deleted_files, list):
        errors.append(f"deleted_files must be a list in {archive_name}")
        deleted_files = []

    declared_files: set[str] = set()
    for item in changed_files:
        if not isinstance(item, dict):
            errors.append(f"invalid changed_files entry in {archive_name}")
            continue
        path = item.get("path")
        digest = item.get("sha256")
        if not _is_safe_archive_member(path):
            errors.append(f"unsafe changed_files path in {archive_name}: {path!r}")
            continue
        if path in declared_files:
            errors.append(f"duplicate changed_files path in {archive_name}: {path}")
            continue
        declared_files.add(path)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            errors.append(f"invalid changed_files SHA-256 in {archive_name}: {path}")
            continue
        if path not in names:
            errors.append(f"changed file is missing from {archive_name}: {path}")
            continue
        actual_digest = hashlib.sha256(archive.read(path)).hexdigest()
        if actual_digest != digest:
            errors.append(f"changed file hash mismatch in {archive_name}: {path}")

    declared_support: set[str] = set()
    for item in support_files:
        if not isinstance(item, dict):
            errors.append(f"invalid support_files entry in {archive_name}")
            continue
        path = item.get("path")
        digest = item.get("sha256")
        if not _is_safe_archive_member(path):
            errors.append(f"unsafe support_files path in {archive_name}: {path!r}")
            continue
        if path in declared_support:
            errors.append(f"duplicate support_files path in {archive_name}: {path}")
            continue
        declared_support.add(path)
        if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
            errors.append(f"invalid support_files SHA-256 in {archive_name}: {path}")
            continue
        if path not in names:
            errors.append(f"support file is missing from {archive_name}: {path}")
            continue
        actual_digest = hashlib.sha256(archive.read(path)).hexdigest()
        if actual_digest != digest:
            errors.append(f"support file hash mismatch in {archive_name}: {path}")

    support_overlap = declared_files & declared_support
    for path in sorted(support_overlap):
        errors.append(f"path is both changed and support in {archive_name}: {path}")

    deleted_paths: set[str] = set()
    for path in deleted_files:
        if not _is_safe_archive_member(path):
            errors.append(f"unsafe deleted_files path in {archive_name}: {path!r}")
            continue
        if path in deleted_paths:
            errors.append(f"duplicate deleted_files path in {archive_name}: {path}")
        deleted_paths.add(path)
    overlap = (declared_files | declared_support) & deleted_paths
    for path in sorted(overlap):
        errors.append(f"path is both payload and deleted in {archive_name}: {path}")

    actual_payload = names - {manifest_name}
    expected_payload = declared_files | declared_support
    if actual_payload != expected_payload:
        errors.append(
            f"payload does not match changed_files/support_files in {archive_name}"
        )


def _validate_release_archives(root: Path, errors: list[str]) -> None:
    """Check ZIP CRCs, safe member names, and incremental manifests."""
    releases_dir = root / "releases"
    if not releases_dir.is_dir():
        return

    for archive_path in sorted(releases_dir.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.namelist():
                    if not _is_safe_archive_member(member):
                        errors.append(
                            f"unsafe path in {_relative(root, archive_path)}: {member!r}"
                        )
                _validate_incremental_archive(
                    archive,
                    _relative(root, archive_path),
                    errors,
                )
                bad_member = archive.testzip()
                if bad_member is not None:
                    errors.append(
                        f"CRC failure in {_relative(root, archive_path)}: {bad_member}"
                    )
        except (
            OSError,
            EOFError,
            NotImplementedError,
            RuntimeError,
            zipfile.BadZipFile,
            zlib.error,
        ) as err:
            errors.append(
                f"invalid release archive {_relative(root, archive_path)}: {err}"
            )


def _read_release_member(
    root: Path,
    archive_name: str,
    member_name: str,
    errors: list[str],
) -> bytes | None:
    """Read a required member from a release ZIP with a useful error message."""
    archive_path = root / "releases" / archive_name
    if not archive_path.is_file():
        errors.append(f"required release archive is missing: releases/{archive_name}")
        return None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            if member_name not in archive.namelist():
                errors.append(
                    f"{member_name} is missing from releases/{archive_name}"
                )
                return None
            return archive.read(member_name)
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as err:
        errors.append(f"cannot read releases/{archive_name}: {err}")
        return None


def _validate_cumulative_patch_matches_full(
    root: Path,
    full_archive_name: str,
    patch_archive_name: str,
    errors: list[str],
) -> None:
    """Verify every cumulative patch payload matches its full-package target."""
    full_path = root / "releases" / full_archive_name
    patch_path = root / "releases" / patch_archive_name
    try:
        with zipfile.ZipFile(full_path) as full_archive, zipfile.ZipFile(
            patch_path
        ) as patch_archive:
            metadata = json.loads(patch_archive.read("DOOODS-UPDATE.json"))
            if not isinstance(metadata, dict):
                errors.append(f"{patch_archive_name} update manifest must be an object")
                return
            full_names = set(full_archive.namelist())
            changed_items = metadata.get("changed_files", [])
            deleted_items = metadata.get("deleted_files", [])
            if not isinstance(changed_items, list):
                changed_items = []
            if not isinstance(deleted_items, list):
                deleted_items = []

            for item in changed_items:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                update_path = item["path"]
                if update_path.startswith("custom_components/"):
                    full_member = update_path
                elif update_path.startswith("dooods2/"):
                    full_member = "local_apps/" + update_path
                else:
                    errors.append(
                        f"unsupported path in {patch_archive_name}: {update_path}"
                    )
                    continue
                if full_member not in full_names:
                    errors.append(
                        f"{full_member} is missing from {full_archive_name}"
                    )
                elif patch_archive.read(update_path) != full_archive.read(full_member):
                    errors.append(
                        f"{update_path} differs between {patch_archive_name} "
                        f"and {full_archive_name}"
                    )

            support_items = metadata.get("support_files", [])
            if not isinstance(support_items, list):
                support_items = []
            for item in support_items:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                support_path = item["path"]
                if support_path not in full_names:
                    errors.append(f"{support_path} is missing from {full_archive_name}")
                elif patch_archive.read(support_path) != full_archive.read(support_path):
                    errors.append(
                        f"{support_path} differs between {patch_archive_name} "
                        f"and {full_archive_name}"
                    )

            for update_path in deleted_items:
                if not isinstance(update_path, str):
                    continue
                if update_path.startswith("custom_components/"):
                    full_member = update_path
                elif update_path.startswith("dooods2/"):
                    full_member = "local_apps/" + update_path
                else:
                    continue
                if full_member in full_names:
                    errors.append(
                        f"deleted path {update_path} is still in {full_archive_name}"
                    )
    except (
        OSError,
        EOFError,
        KeyError,
        RuntimeError,
        UnicodeError,
        zipfile.BadZipFile,
        zlib.error,
        json.JSONDecodeError,
    ) as err:
        errors.append(
            f"cannot compare {patch_archive_name} with {full_archive_name}: {err}"
        )


def _profile_manager_has_0_1_16_1_fixes(content: bytes) -> bool:
    """Check the three profile-editor corrections carried by integration 0.1.16.1."""
    try:
        source = content.decode("utf-8")
        sidebar_start = source.index('<aside class="profile-sidebar">')
        settings_start = source.index('id="settings-card"', sidebar_start)
        settings_end = source.index("</section>", settings_start)
        status_position = source.index('id="status"', settings_start)
        sidebar_end = source.index("</aside>", sidebar_start)
        labels_position = source.index('id="label-list"', settings_start)
        add_label_position = source.index('id="add-label-button"', settings_start)
        save_row_position = source.index('class="save-row"', settings_start)
    except (UnicodeError, ValueError):
        return False

    keeps_label_order = re.search(
        r"if \(nextLabel === label\)\s*\{\s*"
        r"updated\[label\] = nextThreshold;\s*\}\s*else\s*\{",
        source,
    )
    return (
        settings_end < status_position < sidebar_end
        and labels_position < add_label_position < save_row_position
        and keeps_label_order is not None
    )


def _validate_0_1_17_artifacts(root: Path, errors: list[str]) -> None:
    """Ensure 0.1.17 full and cumulative packages retain the 0.1.16.1 fixes."""
    ui_path = "custom_components/dooods/frontend/profile-manager.js"
    base_archive = "dooods_update_0.1.16.1__0.1.15.2-dev.zip"
    full_archive = "dooods_full_0.1.17__0.1.15.2-dev.zip"
    cumulative_patch = "dooods_update_0.1.17__0.1.15.2-dev_from_0.1.16.zip"

    base_ui = _read_release_member(root, base_archive, ui_path, errors)
    full_ui = _read_release_member(root, full_archive, ui_path, errors)
    patch_ui = _read_release_member(root, cumulative_patch, ui_path, errors)

    if base_ui is not None and not _profile_manager_has_0_1_16_1_fixes(base_ui):
        errors.append(f"the {base_archive} UI payload does not contain all three fixes")
    for archive_name, payload in (
        (full_archive, full_ui),
        (cumulative_patch, patch_ui),
    ):
        if payload is not None and base_ui is not None and payload != base_ui:
            errors.append(
                f"{archive_name} does not carry forward {base_archive}'s profile-manager.js"
            )

    integration_manifest = _read_release_member(
        root,
        full_archive,
        "custom_components/dooods/manifest.json",
        errors,
    )
    if integration_manifest is not None:
        try:
            manifest = json.loads(integration_manifest)
        except (UnicodeError, json.JSONDecodeError) as err:
            errors.append(f"invalid integration manifest in releases/{full_archive}: {err}")
        else:
            if not isinstance(manifest, dict) or manifest.get("version") != "0.1.17":
                errors.append(f"{full_archive} does not target integration 0.1.17")

    app_config = _read_release_member(
        root,
        full_archive,
        "local_apps/dooods2/config.yaml",
        errors,
    )
    if app_config is not None:
        try:
            config = yaml.safe_load(app_config)
        except yaml.YAMLError as err:
            errors.append(f"invalid App config in releases/{full_archive}: {err}")
        else:
            if not isinstance(config, dict) or config.get("version") != "0.1.15.2-dev":
                errors.append(f"{full_archive} does not target App 0.1.15.2-dev")

    patch_metadata = _read_release_member(
        root,
        cumulative_patch,
        "DOOODS-UPDATE.json",
        errors,
    )
    if patch_metadata is not None:
        try:
            metadata = json.loads(patch_metadata)
        except (UnicodeError, json.JSONDecodeError) as err:
            errors.append(f"invalid update manifest in releases/{cumulative_patch}: {err}")
        else:
            expected_base = {"integration": "0.1.16", "app": "0.1.15.2-dev"}
            expected_target = {"integration": "0.1.17", "app": "0.1.15.2-dev"}
            if not isinstance(metadata, dict):
                errors.append(f"{cumulative_patch} update manifest must be a JSON object")
            else:
                if metadata.get("base_versions") != expected_base:
                    errors.append(f"{cumulative_patch} has the wrong base versions")
                if metadata.get("target_versions") != expected_target:
                    errors.append(f"{cumulative_patch} has the wrong target versions")
                changed_files = metadata.get("changed_files", [])
                if not isinstance(changed_files, list):
                    changed_files = []
                if not any(
                    isinstance(item, dict) and item.get("path") == ui_path
                    for item in changed_files
                ):
                    errors.append(f"{cumulative_patch} omits the 0.1.16.1 UI fix file")

    _validate_cumulative_patch_matches_full(
        root,
        full_archive,
        cumulative_patch,
        errors,
    )


def _validate_app_context(root: Path, errors: list[str]) -> None:
    """Ensure the local App's build context is an exact copy of its source."""
    source = root / "analysis" / "application"
    destination = root / "local_apps" / "dooods2" / "application"
    if not source.is_dir():
        errors.append("missing App source directory: analysis/application")
        return
    if not destination.is_dir():
        errors.append("missing local App build context: local_apps/dooods2/application")
        return

    source_files = _copyable_context_files(source)
    staged_files = _copyable_context_files(destination)
    source_names = set(source_files)
    staged_names = set(staged_files)

    for relative in sorted(source_names - staged_names):
        errors.append(f"local App build context is missing source file: {relative}")
    for relative in sorted(staged_names - source_names):
        errors.append(f"local App build context has stale/extra file: {relative}")
    for relative in sorted(source_names & staged_names):
        if source_files[relative].read_bytes() != staged_files[relative].read_bytes():
            errors.append(f"local App build context differs from source: {relative}")


def validate_repository(root: Path = ROOT) -> list[str]:
    """Return validation errors for a repository tree; never change any files."""
    root = root.resolve()
    errors: list[str] = []

    for relative in _REQUIRED_FILES:
        if not (root / relative).is_file():
            errors.append(f"required file is missing: {relative}")

    tests_dir = root / "analysis" / "tests"
    if not tests_dir.is_dir() or not any(tests_dir.rglob("test_*.py")):
        errors.append("analysis/tests contains no test_*.py files")

    integration_dir = root / "custom_integration" / "dooods"
    json_documents: dict[str, Any] = {}
    if integration_dir.is_dir():
        for path in sorted(integration_dir.rglob("*.json")):
            document = _read_json(path, root, errors)
            json_documents[_relative(root, path)] = document

    yaml_paths: set[Path] = set()
    for base in (integration_dir, root / "local_apps" / "dooods2"):
        if base.is_dir():
            yaml_paths.update(base.rglob("*.yaml"))
            yaml_paths.update(base.rglob("*.yml"))
    yaml_paths.add(root / "analysis" / "application" / "config.yaml")
    workflow_dir = root / ".github" / "workflows"
    if workflow_dir.is_dir():
        yaml_paths.update(workflow_dir.rglob("*.yaml"))
        yaml_paths.update(workflow_dir.rglob("*.yml"))

    yaml_documents: dict[str, Any] = {}
    for path in sorted(yaml_paths):
        if path.is_file():
            document = _read_yaml(path, root, errors)
            yaml_documents[_relative(root, path)] = document

    manifest_path = "custom_integration/dooods/manifest.json"
    manifest = json_documents.get(manifest_path)
    if not isinstance(manifest, dict):
        errors.append(f"{manifest_path} must contain a JSON object")
    else:
        if manifest.get("domain") != "dooods":
            errors.append(f"{manifest_path} domain must be 'dooods'")
        if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
            errors.append(f"{manifest_path} name must be a non-empty string")
        if manifest.get("config_flow") is not True:
            errors.append(f"{manifest_path} config_flow must be true")
        if not _is_version(manifest.get("version")):
            errors.append(f"{manifest_path} version is invalid")

    app_config_path = "local_apps/dooods2/config.yaml"
    app_config = yaml_documents.get(app_config_path)
    if not isinstance(app_config, dict):
        errors.append(f"{app_config_path} must contain a YAML mapping")
    else:
        if app_config.get("slug") != "dooods2":
            errors.append(f"{app_config_path} slug must be 'dooods2'")
        if not isinstance(app_config.get("name"), str) or not app_config["name"].strip():
            errors.append(f"{app_config_path} name must be a non-empty string")
        if not _is_version(app_config.get("version")):
            errors.append(f"{app_config_path} version is invalid")
        architectures = app_config.get("arch")
        if not isinstance(architectures, list) or not architectures:
            errors.append(f"{app_config_path} arch must be a non-empty list")
        if app_config.get("ingress") is not False:
            errors.append(f"{app_config_path} ingress must be false")

        source_version = _module_string_constant(
            root / "analysis" / "application" / "capabilities.py",
            "APP_VERSION",
        )
        staged_version = _module_string_constant(
            root / "local_apps" / "dooods2" / "application" / "capabilities.py",
            "APP_VERSION",
        )
        for label, version in (
            ("analysis/application/capabilities.py", source_version),
            ("local_apps/dooods2/application/capabilities.py", staged_version),
        ):
            if not _is_version(version):
                errors.append(f"{label} has no valid APP_VERSION string constant")
        if source_version is not None and source_version != app_config.get("version"):
            errors.append("App config version differs from analysis/application APP_VERSION")
        if staged_version is not None and staged_version != app_config.get("version"):
            errors.append("App config version differs from staged App APP_VERSION")

    release_metadata_path = root / "releases" / "last_release.json"
    if release_metadata_path.is_file():
        release_metadata = _read_json(release_metadata_path, root, errors)
        _validate_release_metadata(release_metadata, errors)

    _validate_app_context(root, errors)
    _validate_release_archives(root, errors)
    _validate_0_1_17_artifacts(root, errors)
    return errors


def main() -> int:
    """Run checks and return a conventional process exit status."""
    errors = validate_repository()
    if errors:
        print("Repository validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
