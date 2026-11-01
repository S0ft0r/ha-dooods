#!/usr/bin/env python3
"""Синхронизировать контекст Home Assistant App с исходниками DOOODS2."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "analysis" / "application"
DESTINATION = ROOT / "local_apps" / "dooods2" / "application"
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


def _ignore(_directory: str, names: list[str]) -> set[str]:
    """Не копировать кэши, модели и средства сборки в контекст App."""
    return {name for name in names if name in _IGNORED_NAMES}


def sync_local_app(root: Path = ROOT) -> Path:
    """Пересобрать локальный build context из backend-кода в analysis/application."""
    source = root / "analysis" / "application"
    destination = root / "local_apps" / "dooods2" / "application"
    if not source.is_dir():
        raise SystemExit(f"Каталог исходников не найден: {source}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=_ignore)
    return destination


def main() -> None:
    """Синхронизировать контекст и сообщить путь к нему."""
    destination = sync_local_app()
    print(f"Синхронизирован DOOODS2 App: {destination.parent}")


if __name__ == "__main__":
    main()
