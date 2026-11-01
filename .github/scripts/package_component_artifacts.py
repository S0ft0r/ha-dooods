#!/usr/bin/env python3
"""Build independent integration and App ZIP artifacts without mutating sources.

The integration ZIP has the HACS ``zip_release`` asset layout: the contents of
``custom_components/dooods`` are at the ZIP root. The App ZIP contains only the
``local_apps/dooods2`` folder. These are component artifacts, not a combined
release and not a deployment to HACS or the Home Assistant Apps Store.

This script never runs the release builders, syncs the App mirror, writes under
``releases/``, or updates ``releases/last_release.json``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import tokenize
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
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
    "tests",
}
_EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_APP_CONTEXT_IGNORED_NAMES = {
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
_TAG_PREFIXES = {
    "integration": "dooods-integration-v",
    "app": "dooods2-app-v",
}


def _safe_member(name: str) -> bool:
    """Reject absolute paths, traversal, and Windows separators in ZIP names."""
    if not name or "\\" in name:
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == name


def _valid_version(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise SystemExit(f"Invalid {label} version: {value!r}")
    return value


def _read_app_version_constant(path: Path) -> str:
    try:
        with tokenize.open(path) as source_file:
            module = ast.parse(source_file.read(), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as err:
        raise SystemExit(f"Cannot parse {path.relative_to(ROOT)}: {err}") from err

    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return _valid_version(value.value, "APP_VERSION")
            break
    raise SystemExit(f"{path.relative_to(ROOT)} must define a string APP_VERSION")


def _validate_app_mirror() -> None:
    """Compare the optional App source mirror; never synchronize the build context."""
    source = ROOT / "analysis" / "application"
    staged = ROOT / "local_apps" / "dooods2" / "application"
    if not staged.is_dir():
        raise SystemExit("Checked-in App build context is missing: local_apps/dooods2/application")
    # Public/component-only checkouts may intentionally omit analysis/application.
    # In that layout, validate and package the checked-in App tree as-is.
    if not source.is_dir():
        return

    def files(base: Path) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(base)
            if any(part in _APP_CONTEXT_IGNORED_NAMES for part in relative.parts):
                continue
            result[relative.as_posix()] = path
        return result

    source_files = files(source)
    staged_files = files(staged)
    missing = sorted(set(source_files) - set(staged_files))
    extra = sorted(set(staged_files) - set(source_files))
    changed = sorted(
        relative
        for relative in set(source_files) & set(staged_files)
        if source_files[relative].read_bytes() != staged_files[relative].read_bytes()
    )
    if missing or extra or changed:
        details = []
        details.extend(f"missing from local_apps/dooods2/application: {name}" for name in missing)
        details.extend(f"extra in local_apps/dooods2/application: {name}" for name in extra)
        details.extend(f"differs from analysis/application: {name}" for name in changed)
        raise SystemExit(
            "App source/build-context mismatch (not auto-synced by this workflow):\n  - "
            + "\n  - ".join(details)
        )


def _component_versions(components: list[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    if "integration" in components:
        source = ROOT / "custom_components" / "dooods"
        manifest_path = source / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as err:
            raise SystemExit(f"Cannot read {manifest_path.relative_to(ROOT)}: {err}") from err
        if not isinstance(manifest, dict) or manifest.get("domain") != "dooods":
            raise SystemExit("custom_components/dooods/manifest.json must declare domain 'dooods'")
        if not (source / "__init__.py").is_file():
            raise SystemExit("custom_components/dooods/__init__.py is missing")
        versions["integration"] = _valid_version(manifest.get("version"), "integration")

    if "app" in components:
        source = ROOT / "local_apps" / "dooods2"
        config_path = source / "config.yaml"
        try:
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as err:
            raise SystemExit(f"Cannot read {config_path.relative_to(ROOT)}: {err}") from err
        if not isinstance(config, dict) or config.get("slug") != "dooods2":
            raise SystemExit("local_apps/dooods2/config.yaml must declare slug 'dooods2'")
        app_version = _valid_version(config.get("version"), "App")
        capabilities_path = source / "application" / "capabilities.py"
        capabilities_version = _read_app_version_constant(capabilities_path)
        if app_version != capabilities_version:
            raise SystemExit(
                "local_apps/dooods2/config.yaml version and "
                "application/capabilities.py APP_VERSION do not match"
            )
        for relative in ("Dockerfile", "application/main.py", "application/capabilities.py"):
            if not (source / relative).is_file():
                raise SystemExit(f"Required App file is missing: local_apps/dooods2/{relative}")
        _validate_app_mirror()
        versions["app"] = app_version
    return versions


def _check_expected_tag(tag: str, component: str, version: str) -> None:
    if not tag:
        return
    if component == "all":
        raise SystemExit("A component tag can build only one component; use workflow_dispatch for 'all'.")
    prefix = _TAG_PREFIXES[component]
    if not tag.startswith(prefix):
        expected_prefixes = ", ".join(_TAG_PREFIXES.values())
        raise SystemExit(f"Unexpected tag {tag!r}; expected one of: {expected_prefixes}<version>")
    tag_version = tag[len(prefix) :]
    _valid_version(tag_version, "tag")
    if tag_version != version:
        raise SystemExit(
            f"Tag version {tag_version!r} does not match {component} source version {version!r}"
        )


def _collect_files(source: Path, archive_prefix: str) -> list[tuple[str, Path]]:
    if not source.is_dir():
        raise SystemExit(f"Component source directory is missing: {source.relative_to(ROOT)}")
    files: list[tuple[str, Path]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"Symlinks are not allowed in release payloads: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in _EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.name in _EXCLUDED_FILES or path.suffix.lower() in _EXCLUDED_SUFFIXES:
            continue
        member = PurePosixPath(archive_prefix, *relative.parts).as_posix() if archive_prefix else PurePosixPath(*relative.parts).as_posix()
        if not _safe_member(member):
            raise SystemExit(f"Unsafe archive path: {member!r}")
        files.append((member, path))
    if not files:
        raise SystemExit(f"No files found in component directory: {source.relative_to(ROOT)}")
    return files


def _write_zip(
    destination: Path,
    files: list[tuple[str, Path]],
    component: str,
    overwrite: bool,
) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise SystemExit(f"Artifact already exists: {destination}; pass --overwrite to replace it")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for member, path in files:
                info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                file_mode = stat.S_IFREG | (stat.S_IMODE(path.stat().st_mode) or 0o644)
                info.external_attr = file_mode << 16
                archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

        with zipfile.ZipFile(temporary) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise zipfile.BadZipFile("duplicate member names")
            if any(not _safe_member(name) for name in names):
                raise zipfile.BadZipFile("unsafe member path")
            bad_member = archive.testzip()
            if bad_member is not None:
                raise zipfile.BadZipFile(f"CRC check failed for {bad_member}")
            required = (
                {"manifest.json", "__init__.py"}
                if component == "integration"
                else {
                    "dooods2/config.yaml",
                    "dooods2/Dockerfile",
                    "dooods2/application/main.py",
                    "dooods2/application/capabilities.py",
                }
            )
            missing = required - set(names)
            if missing:
                raise zipfile.BadZipFile(f"required members missing: {sorted(missing)}")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_github_outputs(data: dict[str, Any]) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as output:
        for component in ("integration", "app"):
            item = data["artifacts"].get(component)
            output.write(f"{component}_built={'true' if item else 'false'}\n")
            output.write(f"{component}_version={item['version'] if item else ''}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("integration", "app", "all"), default="all")
    parser.add_argument("--expected-tag", default="", help="Optional component tag to verify against the source version.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "component-artifacts")
    parser.add_argument("--overwrite", action="store_true", help="Replace artifacts in the output directory only.")
    args = parser.parse_args()

    components = ["integration", "app"] if args.component == "all" else [args.component]
    versions = _component_versions(components)
    if args.expected_tag:
        _check_expected_tag(args.expected_tag, args.component, versions[components[0]])

    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = ROOT / output_dir
    output_dir = output_dir.resolve()
    releases_dir = (ROOT / "releases").resolve()
    if output_dir == ROOT.resolve() or output_dir == releases_dir or releases_dir in output_dir.parents:
        raise SystemExit("Output directory must not be the repository root or inside releases/.")
    protected_dirs = [ROOT / "custom_components", ROOT / "local_apps", ROOT / "analysis"]
    if any(output_dir == item.resolve() or item.resolve() in output_dir.parents for item in protected_dirs):
        raise SystemExit("Output directory must not be inside a component or source directory.")

    artifacts: dict[str, dict[str, Any]] = {}
    for component in components:
        version = versions[component]
        component_dir = output_dir / component
        if component == "integration":
            source = ROOT / "custom_components" / "dooods"
            filename = "dooods.zip"
            archive_prefix = ""
            layout = "Integration files at ZIP root; suitable as a HACS zip_release asset payload."
            notes = [
                "This ZIP is only the integration release-asset payload; repository-level HACS metadata such as hacs.json is not bundled.",
                "No GitHub Release or HACS publication is created by this workflow.",
            ]
        else:
            source = ROOT / "local_apps" / "dooods2"
            filename = f"dooods2-app-{version}.zip"
            archive_prefix = "dooods2"
            layout = "The single DOOODS2 App folder is under dooods2/ in the ZIP."
            notes = [
                "This ZIP is not a complete Apps Store repository and is not consumed directly by the Store.",
                "The monorepo must be curated before direct Store use: it contains DOOODS2F and other config.yaml files; application/config.yaml is not App metadata.",
                "No GitHub Release, repository push, or Apps Store publication is created by this workflow.",
            ]
        component_files = _collect_files(source, archive_prefix)
        archive_path = component_dir / filename
        digest = _write_zip(archive_path, component_files, component, args.overwrite)
        artifact_info = {
            "schema_version": 1,
            "component": component,
            "version": version,
            "source_directory": source.relative_to(ROOT).as_posix(),
            "artifact": filename,
            "sha256": digest,
            "file_count": len(component_files),
            "archive_layout": layout,
            "store_published": False,
            "notes": notes,
        }
        _write_json(component_dir / "artifact-info.json", artifact_info)
        artifacts[component] = artifact_info
        print(f"Prepared {component} {version}: {archive_path}")
        print(f"  files: {len(component_files)}; sha256: {digest}")

    result = {
        "schema_version": 1,
        "source_repository": os.environ.get("GITHUB_REPOSITORY", "local workspace"),
        "source_revision": os.environ.get("GITHUB_SHA", ""),
        "artifacts": artifacts,
        "github_release_created": False,
        "store_publication_performed": False,
        "publication_gate": "Separate distribution repositories/layouts and the outstanding license audit are not handled by this build-only workflow.",
    }
    _write_json(output_dir / "release-manifest.json", result)
    _write_github_outputs(result)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write("## Component artifacts prepared\n\n")
            summary.write("No combined ZIP, GitHub Release, or Home Assistant Store publication was created.\n\n")
            for component, item in artifacts.items():
                summary.write(f"- **{component} {item['version']}** — `{item['artifact']}`; SHA-256 `{item['sha256']}`\n")
            summary.write("\nBefore Store distribution, prepare component-specific repository layouts and complete the license audit in `TODO.md`.\n")

    print(f"Manifest: {output_dir / 'release-manifest.json'}")
    print("Build-only mode: releases/ and releases/last_release.json were not touched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
