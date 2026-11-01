#!/usr/bin/env python3
"""Self-contained validator shipped inside DOOODS release ZIPs.

Run against a downloaded archive:
    python3 DOOODS-VALIDATE.py dooods_full_0.1.17__0.1.15.2-dev_with_validator.zip

Or against an extracted full package / patch directory:
    python3 DOOODS-VALIDATE.py /path/to/extracted-package

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable


UPDATE_MANIFEST = "DOOODS-UPDATE.json"
VALIDATOR_FILE = "DOOODS-VALIDATE.py"
UI_FILE = "custom_components/dooods/frontend/profile-manager.js"
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_path(value: object) -> bool:
    """Accept only canonical relative POSIX paths inside the bundle."""
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


def _safe_member(value: object) -> bool:
    """Permit ZIP directory records while keeping file paths traversal-safe."""
    return isinstance(value, str) and _safe_path(value.rstrip("/"))


def _json_document(read_member: Callable[[str], bytes], name: str) -> dict[str, object]:
    """Read a JSON object member or raise a clear validation error."""
    try:
        document = json.loads(read_member(name))
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as err:
        raise ValueError(f"cannot read JSON {name}: {err}") from err
    if not isinstance(document, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return document


def _profile_manager_has_fixes(source: bytes) -> bool:
    """Check all three profile-editor fixes from integration 0.1.16.1."""
    try:
        text = source.decode("utf-8")
        sidebar = text.index('<aside class="profile-sidebar">')
        settings = text.index('id="settings-card"', sidebar)
        settings_end = text.index("</section>", settings)
        status = text.index('id="status"', settings)
        sidebar_end = text.index("</aside>", sidebar)
        labels = text.index('id="label-list"', settings)
        add_label = text.index('id="add-label-button"', settings)
        save_row = text.index('class="save-row"', settings)
    except (UnicodeError, ValueError):
        return False
    keeps_order = re.search(
        r"if \(nextLabel === label\)\s*\{\s*"
        r"updated\[label\] = nextThreshold;\s*\}\s*else\s*\{",
        text,
    )
    return (
        settings_end < status < sidebar_end
        and labels < add_label < save_row
        and keeps_order is not None
    )


def _validate_full(
    names: set[str],
    read_member: Callable[[str], bytes],
    errors: list[str],
) -> None:
    """Validate a full integration + App package."""
    required = {
        "custom_components/dooods/manifest.json",
        "custom_components/dooods/frontend/profile-manager.js",
        "local_apps/dooods2/config.yaml",
        VALIDATOR_FILE,
    }
    for name in sorted(required - names):
        errors.append(f"full package is missing {name}")
    if required - names:
        return

    try:
        manifest = _json_document(read_member, "custom_components/dooods/manifest.json")
        if manifest.get("domain") != "dooods":
            errors.append("integration manifest domain must be 'dooods'")
        integration_version = manifest.get("version")
        if not isinstance(integration_version, str) or not _VERSION_RE.fullmatch(
            integration_version
        ):
            errors.append("integration manifest has an invalid version")

        app_text = read_member("local_apps/dooods2/config.yaml").decode("utf-8")
        slug_match = re.search(r"(?m)^slug:\s*[\"']?([A-Za-z0-9_-]+)", app_text)
        version_match = re.search(
            r"(?m)^version:\s*[\"']?([0-9A-Za-z][0-9A-Za-z.+_-]*)",
            app_text,
        )
        if slug_match is None or slug_match.group(1) != "dooods2":
            errors.append("App config slug must be 'dooods2'")
        if version_match is None or not _VERSION_RE.fullmatch(version_match.group(1)):
            errors.append("App config has an invalid version")

        if integration_version == "0.1.17":
            ui_source = read_member(UI_FILE)
            if not _profile_manager_has_fixes(ui_source):
                errors.append("0.1.17 full package is missing the three 0.1.16.1 UI fixes")
    except (KeyError, OSError, UnicodeError, ValueError) as err:
        errors.append(str(err))


def _validate_incremental(
    names: set[str],
    read_member: Callable[[str], bytes],
    errors: list[str],
) -> None:
    """Validate update metadata, payload hashes, and bundled validator support."""
    try:
        metadata = _json_document(read_member, UPDATE_MANIFEST)
    except ValueError as err:
        errors.append(str(err))
        return

    if metadata.get("schema_version") != 1:
        errors.append("unsupported incremental manifest schema")
    if metadata.get("archive_type") != "incremental":
        errors.append("archive_type must be 'incremental'")

    for field in ("base_versions", "target_versions"):
        versions = metadata.get(field)
        if not isinstance(versions, dict) or set(versions) != {"integration", "app"}:
            errors.append(f"{field} must contain integration and app")
        elif any(
            not isinstance(version, str) or not _VERSION_RE.fullmatch(version)
            for version in versions.values()
        ):
            errors.append(f"{field} contains an invalid version")

    changed_files = metadata.get("changed_files")
    support_files = metadata.get("support_files")
    deleted_files = metadata.get("deleted_files")
    if not isinstance(changed_files, list):
        errors.append("changed_files must be a list")
        changed_files = []
    if not isinstance(support_files, list):
        errors.append("support_files must be a list and include the bundled validator")
        support_files = []
    if not isinstance(deleted_files, list):
        errors.append("deleted_files must be a list")
        deleted_files = []

    declared_changed: dict[str, str] = {}
    declared_support: dict[str, str] = {}
    for entries, destination, label in (
        (changed_files, declared_changed, "changed_files"),
        (support_files, declared_support, "support_files"),
    ):
        for item in entries:
            if not isinstance(item, dict):
                errors.append(f"invalid {label} entry")
                continue
            path = item.get("path")
            digest = item.get("sha256")
            if not _safe_path(path):
                errors.append(f"unsafe {label} path: {path!r}")
                continue
            if path in destination:
                errors.append(f"duplicate {label} path: {path}")
                continue
            if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
                errors.append(f"invalid {label} SHA-256 for {path}")
                continue
            destination[path] = digest
            if path not in names:
                errors.append(f"{label} member is missing from archive: {path}")
                continue
            actual_digest = hashlib.sha256(read_member(path)).hexdigest()
            if actual_digest != digest:
                errors.append(f"{label} hash mismatch for {path}")

    if VALIDATOR_FILE not in declared_support:
        errors.append(f"support_files must include {VALIDATOR_FILE}")
    overlap = set(declared_changed) & set(declared_support)
    if overlap:
        errors.append(f"paths listed as both changed and support files: {sorted(overlap)}")

    deleted_paths: set[str] = set()
    for path in deleted_files:
        if not _safe_path(path):
            errors.append(f"unsafe deleted_files path: {path!r}")
            continue
        if path in deleted_paths:
            errors.append(f"duplicate deleted_files path: {path}")
        deleted_paths.add(path)
    overlap_deleted = deleted_paths & (set(declared_changed) | set(declared_support))
    if overlap_deleted:
        errors.append(f"paths listed as both payload and deleted: {sorted(overlap_deleted)}")

    expected_members = {
        UPDATE_MANIFEST,
        *declared_changed.keys(),
        *declared_support.keys(),
    }
    if names != expected_members:
        errors.append("archive members do not match changed_files and support_files")

    target = metadata.get("target_versions")
    base = metadata.get("base_versions")
    if (
        isinstance(target, dict)
        and target.get("integration") == "0.1.17"
        and isinstance(base, dict)
        and base.get("integration") == "0.1.16"
    ):
        ui_source = None
        if UI_FILE in names:
            try:
                ui_source = read_member(UI_FILE)
            except (KeyError, OSError, ValueError):
                pass
        if ui_source is None or not _profile_manager_has_fixes(ui_source):
            errors.append("cumulative 0.1.17 patch from 0.1.16 lacks the three UI fixes")


def _validate_bundle(
    names: set[str],
    read_member: Callable[[str], bytes],
) -> list[str]:
    """Validate a zip or extracted bundle using a common member reader."""
    errors: list[str] = []
    for name in names:
        if not _safe_member(name):
            errors.append(f"unsafe bundle path: {name!r}")
    if UPDATE_MANIFEST in names:
        _validate_incremental(names, read_member, errors)
    else:
        _validate_full(names, read_member, errors)
    return errors


def validate_path(path: Path) -> list[str]:
    """Validate either a DOOODS release ZIP or an extracted bundle directory."""
    path = path.expanduser().resolve()
    if path.is_dir():
        names = {
            member.relative_to(path).as_posix()
            for member in path.rglob("*")
            if member.is_file()
        }

        def read_member(name: str) -> bytes:
            if not _safe_path(name):
                raise ValueError(f"unsafe path requested: {name!r}")
            member = path.joinpath(*PurePosixPath(name).parts)
            if member.is_symlink() or not member.resolve().is_relative_to(path):
                raise ValueError(f"unsafe extracted member: {name}")
            return member.read_bytes()

        return _validate_bundle(names, read_member)

    if not path.is_file():
        return [f"bundle path does not exist: {path}"]

    try:
        with zipfile.ZipFile(path) as archive:
            member_list = archive.namelist()
            if len(member_list) != len(set(member_list)):
                return ["ZIP contains duplicate member names"]
            bad_member = archive.testzip()
            if bad_member is not None:
                return [f"ZIP CRC failure: {bad_member}"]
            names = set(member_list)
            return _validate_bundle(names, archive.read)
    except (OSError, EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile) as err:
        return [f"invalid or unreadable ZIP: {err}"]


def main() -> int:
    """Run the standalone package check and return a conventional exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="release ZIP or extracted package directory")
    args = parser.parse_args()

    errors = validate_path(args.path)
    if errors:
        print("DOOODS package validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print(f"DOOODS package validation passed: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
