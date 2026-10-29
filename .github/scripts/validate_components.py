#!/usr/bin/env python3
"""Validate only the Home Assistant integration and App in the GitHub repo."""

from __future__ import annotations

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
_IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}


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


def _app_version_constant(path: Path, errors: list[str]) -> str | None:
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
        if any(isinstance(target, ast.Name) and target.id == "APP_VERSION" for target in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            errors.append(f"{_relative(path)}: APP_VERSION must be a string constant")
            return None
    errors.append(f"{_relative(path)}: missing APP_VERSION string constant")
    return None


def _walk_files(base: Path):
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_DIRS for part in path.relative_to(base).parts):
            continue
        yield path


def validate_components(root: Path = ROOT) -> list[str]:
    """Check metadata, serialised files and Python syntax in the two components."""
    global ROOT, INTEGRATION, APP
    ROOT = root.resolve()
    INTEGRATION = ROOT / "custom_components" / "dooods"
    APP = ROOT / "local_apps" / "dooods2"
    errors: list[str] = []

    for directory in (INTEGRATION, APP):
        if not directory.is_dir():
            errors.append(f"required component directory is missing: {_relative(directory)}")

    integration_required = (INTEGRATION / "manifest.json", INTEGRATION / "__init__.py")
    app_required = (
        APP / "config.yaml",
        APP / "Dockerfile",
        APP / "application" / "main.py",
        APP / "application" / "capabilities.py",
    )
    for path in (*integration_required, *app_required):
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
                capabilities_version = _app_version_constant(capabilities_path, errors)
                if capabilities_version is not None and capabilities_version != app_version:
                    errors.append(
                        "local_apps/dooods2/application/capabilities.py APP_VERSION "
                        "must match local_apps/dooods2/config.yaml version"
                    )

    # Parse only JSON/YAML files beneath the two requested component trees.
    for base in (INTEGRATION, APP):
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
    errors = validate_components()
    if errors:
        print("Component validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("Component validation passed: custom_components/dooods and local_apps/dooods2.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
