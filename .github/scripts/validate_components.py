#!/usr/bin/env python3
"""Read-only validation of the monorepo's integration and DOOODS2 App sources."""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tokenize
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = ROOT / "custom_components" / "dooods"
APP = ROOT / "local_apps" / "dooods2"
_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")
_IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
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


def _relative(path: Path) -> str:
    """Return a stable path relative to the repository root."""
    return path.relative_to(ROOT).as_posix()


def _load_json(path: Path, errors: list[str]) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        errors.append(f"{_relative(path)}: invalid JSON ({err})")
        return None


def _load_yaml(path: Path, errors: list[str]) -> Any | None:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as err:
        errors.append(f"{_relative(path)}: invalid YAML ({err})")
        return None


def _module_string_constant(path: Path, name: str, errors: list[str]) -> str | None:
    try:
        with tokenize.open(path) as source_file:
            tree = ast.parse(source_file.read(), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as err:
        errors.append(f"{_relative(path)}: cannot parse Python source ({err})")
        return None

    for node in tree.body:
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
            errors.append(f"{_relative(path)}: {name} must be a string constant")
            return None
    errors.append(f"{_relative(path)}: missing {name} string constant")
    return None


def _walk_files(base: Path):
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.relative_to(base).parts):
            continue
        yield path


def _app_context_files(base: Path) -> dict[str, Path]:
    """List files copied into the App build context, without modifying it."""
    result: dict[str, Path] = {}
    if not base.is_dir():
        return result
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if any(part in _APP_CONTEXT_IGNORED_NAMES for part in relative.parts):
            continue
        result[relative.as_posix()] = path
    return result


def _validate_app_source_mirror(errors: list[str]) -> None:
    """Compare an optional authoritative source with the checked-in App context."""
    source = ROOT / "analysis" / "application"
    staged = APP / "application"
    if not staged.is_dir():
        errors.append("required App build context is missing: local_apps/dooods2/application")
        return
    # Some GitHub distribution checkouts contain only the installable App tree.
    # Validate that tree directly; compare it with the source mirror only when
    # the optional developer-workspace directory is present.
    if not source.is_dir():
        return

    source_files = _app_context_files(source)
    staged_files = _app_context_files(staged)
    for relative in sorted(set(source_files) - set(staged_files)):
        errors.append(f"local App build context is missing source file: {relative}")
    for relative in sorted(set(staged_files) - set(source_files)):
        errors.append(f"local App build context has stale/extra file: {relative}")
    for relative in sorted(set(source_files) & set(staged_files)):
        if source_files[relative].read_bytes() != staged_files[relative].read_bytes():
            errors.append(f"local App build context differs from source: {relative}")


def validate_components(
    root: Path = ROOT,
    components: tuple[str, ...] = ("integration", "app"),
) -> list[str]:
    """Validate only the requested component(s), defaulting to both."""
    global ROOT, INTEGRATION, APP
    ROOT = root.resolve()
    INTEGRATION = ROOT / "custom_components" / "dooods"
    APP = ROOT / "local_apps" / "dooods2"
    errors: list[str] = []

    if "integration" in components:
        if not INTEGRATION.is_dir():
            errors.append("required component directory is missing: custom_components/dooods")
        integration_required = (INTEGRATION / "manifest.json", INTEGRATION / "__init__.py")
        for path in integration_required:
            if not path.is_file():
                errors.append(f"required component file is missing: {_relative(path)}")

        manifest_path = INTEGRATION / "manifest.json"
        manifest = _load_json(manifest_path, errors) if manifest_path.is_file() else None
        if manifest_path.is_file():
            if not isinstance(manifest, dict):
                errors.append(f"{_relative(manifest_path)} must contain a JSON object")
            else:
                if manifest.get("domain") != "dooods":
                    errors.append(f"{_relative(manifest_path)} domain must be 'dooods'")
                version = manifest.get("version")
                if not isinstance(version, str) or not _VERSION.fullmatch(version):
                    errors.append(f"{_relative(manifest_path)} must contain a valid version")

    if "app" in components:
        if not APP.is_dir():
            errors.append("required component directory is missing: local_apps/dooods2")
        app_required = (
            APP / "config.yaml",
            APP / "Dockerfile",
            APP / "application" / "main.py",
            APP / "application" / "capabilities.py",
        )
        for path in app_required:
            if not path.is_file():
                errors.append(f"required component file is missing: {_relative(path)}")

        app_config_path = APP / "config.yaml"
        app_config = _load_yaml(app_config_path, errors) if app_config_path.is_file() else None
        if app_config_path.is_file():
            if not isinstance(app_config, dict):
                errors.append(f"{_relative(app_config_path)} must contain a YAML mapping")
            else:
                if app_config.get("slug") != "dooods2":
                    errors.append(f"{_relative(app_config_path)} slug must be 'dooods2'")
                app_version = app_config.get("version")
                if not isinstance(app_version, str) or not _VERSION.fullmatch(app_version):
                    errors.append(f"{_relative(app_config_path)} must contain a valid version")
                arch = app_config.get("arch")
                if not isinstance(arch, list) or not arch or not all(isinstance(item, str) for item in arch):
                    errors.append(f"{_relative(app_config_path)} arch must be a non-empty list of strings")

                capabilities_path = APP / "application" / "capabilities.py"
                if capabilities_path.is_file():
                    capabilities_version = _module_string_constant(
                        capabilities_path, "APP_VERSION", errors
                    )
                    if capabilities_version is not None and capabilities_version != app_version:
                        errors.append(
                            "local_apps/dooods2/application/capabilities.py APP_VERSION "
                            "must match local_apps/dooods2/config.yaml version"
                        )

        _validate_app_source_mirror(errors)

    selected_dirs = []
    if "integration" in components:
        selected_dirs.append(INTEGRATION)
    if "app" in components:
        selected_dirs.append(APP)
    for base in selected_dirs:
        if not base.is_dir():
            continue
        for path in _walk_files(base):
            suffix = path.suffix.lower()
            if suffix == ".json":
                _load_json(path, errors)
            elif suffix in {".yaml", ".yml"}:
                _load_yaml(path, errors)
            elif suffix == ".py":
                try:
                    with tokenize.open(path) as source_file:
                        compile(source_file.read(), str(path), "exec")
                except (OSError, UnicodeError, SyntaxError) as err:
                    errors.append(f"{_relative(path)}: invalid Python syntax ({err})")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("integration", "app", "all"), default="all")
    args = parser.parse_args()
    components = ("integration", "app") if args.component == "all" else (args.component,)

    errors = validate_components(components=components)
    if errors:
        print("Component validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    label = " and ".join(components)
    print(f"Component validation passed for {label}.")
    if "app" in components:
        if (ROOT / "analysis" / "application").is_dir():
            print("The App source/build-context comparison was read-only; no synchronization was run.")
        else:
            print("analysis/application is absent; the checked-in App build context was validated directly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
