#!/usr/bin/env python3
"""Build a focused release ZIP: current Integration + App + GitHub tooling.

This is deliberately not a full repository/source archive. It includes the
installable component trees, GitHub Actions workflows and scripts, validator
requirements, the current release-state snapshot, and the standalone package
validator. It never synchronizes the App mirror or updates release state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from validate_components import validate_components
from validate_release_archive import validate_path

ROOT = Path(__file__).resolve().parents[2]
_DIRS = (
    ".github/scripts",
    ".github/workflows",
    "custom_components/dooods",
    "local_apps/dooods2",
)
_FILES = (
    ".github/requirements-validator.txt",
)
_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "out",
    "target",
}
_EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
_APP_VERSION_RE = re.compile(r"(?m)^\s*version:\s*[\"']?([^\"'\s#]+)")


def _safe_member(name: str) -> bool:
    """Accept only normalized relative POSIX archive paths."""
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == name


def _read_versions() -> dict[str, str]:
    """Read current component versions without invoking a release builder."""
    manifest_path = ROOT / "custom_components/dooods/manifest.json"
    app_config_path = ROOT / "local_apps/dooods2/config.yaml"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        app_config = app_config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        raise SystemExit(f"Cannot read component version metadata: {err}") from err

    integration_version = manifest.get("version")
    match = _APP_VERSION_RE.search(app_config)
    if not isinstance(integration_version, str) or not _VERSION_RE.fullmatch(integration_version):
        raise SystemExit(f"Invalid integration version in {manifest_path}")
    if match is None or not _VERSION_RE.fullmatch(match.group(1)):
        raise SystemExit(f"Invalid App version in {app_config_path}")
    return {"integration": integration_version, "app": match.group(1)}


def _collect_sources() -> dict[str, Path]:
    """Collect only the components and GitHub release/validation tooling."""
    result: dict[str, Path] = {}
    for relative in _FILES:
        source = ROOT / relative
        if not source.is_file():
            raise SystemExit(f"Required bundle file is missing: {relative}")
        result[PurePosixPath(relative).as_posix()] = source

    for relative in _DIRS:
        source = ROOT / relative
        if not source.is_dir():
            raise SystemExit(f"Required bundle directory is missing: {relative}")
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise SystemExit(f"Symlinks are not allowed: {path.relative_to(ROOT)}")
            if not path.is_file():
                continue
            local = path.relative_to(source)
            if any(part in _EXCLUDED_DIRS for part in local.parts):
                continue
            if path.name in _EXCLUDED_FILES or path.suffix.lower() in _EXCLUDED_SUFFIXES:
                continue
            member = PurePosixPath(relative, *local.parts).as_posix()
            if not _safe_member(member):
                raise SystemExit(f"Unsafe bundle path: {member!r}")
            if member in result:
                raise SystemExit(f"Duplicate bundle path: {member}")
            result[member] = path

    validator = ROOT / ".github/scripts/validate_release_archive.py"
    if not validator.is_file():
        raise SystemExit(".github/scripts/validate_release_archive.py is missing")
    result["DOOODS-VALIDATE.py"] = validator
    return dict(sorted(result.items()))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_notes(versions: dict[str, str], member_count: int) -> str:
    return "\n".join(
        (
            "# DOOODS components and GitHub Actions tooling",
            "",
            f"- Integration `dooods`: `{versions['integration']}`",
            f"- Home Assistant App `dooods2`: `{versions['app']}`",
            f"- Members excluding this note and the bundle manifest: `{member_count}`",
            "",
            "This focused release ZIP contains the complete installable Integration and App trees, `.github/scripts/`, GitHub Actions workflows, validator requirements, and the standalone `DOOODS-VALIDATE.py` entry point. The internal `releases/last_release.json` incremental-build baseline is deliberately not bundled.",
            "",
            "It is **not** a full DOOODS repository archive. It omits `analysis/tests/`, the optional developer source mirror at `analysis/application/`, upstream snapshots, root-level project documents, asset experiments, and historical release ZIPs. The GitHub component workflows run against the included `local_apps/dooods2/` tree; where `analysis/application/` is present in a developer monorepo, validators also compare it with the checked-in App build context. The broader `validate_repository.py` is intended for the full developer monorepo.",
            "",
            "Building this ZIP does not publish anything by itself. The manually dispatched `create-github-release.yml` workflow attaches the focused ZIP and checksum to a GitHub prerelease. The separate component-artifact workflow still creates independent Integration and App Actions artifacts; neither workflow publishes to HACS or the Home Assistant Apps Store.",
            "",
            "## Validate the downloaded ZIP",
            "",
            "The standalone validator is included at the ZIP root. After extracting it (or the bundle), run:",
            "",
            "```bash",
            "python3 DOOODS-VALIDATE.py path/to/dooods_release_<integration>__<app>_components_with_github_actions.zip",
            "```",
            "",
            "The App mirror and backend source were not synchronized. The builder does not include or modify `releases/last_release.json`.",
            "",
        )
    )


def _write_zip(destination: Path, payloads: dict[str, bytes]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for member, data in sorted(payloads.items()):
                if not _safe_member(member):
                    raise ValueError(f"Unsafe archive member: {member!r}")
                info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(
                    info,
                    data,
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=9,
                )
        with zipfile.ZipFile(temporary) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise zipfile.BadZipFile("duplicate ZIP member names")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise zipfile.BadZipFile(f"CRC failure: {bad_member}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_archive(output: Path | None = None, *, overwrite: bool = False) -> Path:
    """Build and validate a focused components + GitHub tooling archive."""
    errors = validate_components(ROOT, ("integration", "app"))
    if errors:
        raise SystemExit("Component validation failed:\n  - " + "\n  - ".join(errors))

    versions = _read_versions()
    if output is None:
        output = ROOT / "releases" / (
            f"dooods_release_{versions['integration']}__{versions['app']}"
            "_components_with_github_actions.zip"
        )
    elif not output.is_absolute():
        output = ROOT / output
    output = output.resolve()
    releases_dir = (ROOT / "releases").resolve()
    if output.parent != releases_dir:
        raise SystemExit("The archive must be written directly into releases/.")
    if output.exists() and not overwrite:
        raise SystemExit(f"Refusing to overwrite existing release: {output.name}")

    payloads = {name: path.read_bytes() for name, path in _collect_sources().items()}
    notes = _bundle_notes(versions, len(payloads))
    payloads["DOOODS-RELEASE-BUNDLE.md"] = notes.encode("utf-8")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "bundle_type": "components-and-github-actions-tooling",
        "versions": versions,
        "member_count_excluding_manifest": len(payloads),
        "members": {name: _sha256(data) for name, data in sorted(payloads.items())},
        "contains": [
            "custom_components/dooods",
            "local_apps/dooods2",
            ".github/scripts",
            ".github/workflows",
            "DOOODS-VALIDATE.py",
        ],
        "does_not_contain_full_repository": True,
        "does_not_modify_release_state": True,
    }
    payloads["DOOODS-RELEASE-BUNDLE.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    _write_zip(output, payloads)
    validation_errors = validate_path(output)
    if validation_errors:
        output.unlink(missing_ok=True)
        raise SystemExit(
            "Standalone release validation failed:\n  - "
            + "\n  - ".join(validation_errors)
        )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional output path under releases/.")
    parser.add_argument("--overwrite", action="store_true", help="Replace only the named archive.")
    args = parser.parse_args()

    archive_path = build_archive(args.output, overwrite=args.overwrite)
    with zipfile.ZipFile(archive_path) as archive:
        member_count = len(archive.namelist())
    data = archive_path.read_bytes()
    print(f"Created and validated: {archive_path.relative_to(ROOT)}")
    print(f"Members: {member_count}; size: {len(data)} bytes")
    print(f"SHA-256: {_sha256(data)}")
    print("No App synchronization or release-state update was run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
