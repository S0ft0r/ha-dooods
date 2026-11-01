#!/usr/bin/env python3
"""Собрать единый инкрементальный архив изменённых файлов DOOODS."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Mapping

from package_local_app import sync_local_app

ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = ROOT / "releases"
STATE_PATH = RELEASE_DIR / "last_release.json"
UPDATE_MANIFEST_NAME = "DOOODS-UPDATE.json"
_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_APP_VERSION_PATTERN = re.compile(r"(?m)^\s*version:\s*[\"']?([^\"'\s#]+)")
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
_EXCLUDED_FILES = {".DS_Store"}
_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
_PACKAGE_DIRS = (
    ("custom_components/dooods", "custom_components/dooods"),
    ("local_apps/dooods2", "dooods2"),
)
_FULL_PACKAGE_DIRS = (
    ("custom_components/dooods", "custom_components/dooods"),
    ("local_apps/dooods2", "local_apps/dooods2"),
)


def sha256(data: bytes) -> str:
    """Вернуть SHA-256 содержимого файла."""
    return hashlib.sha256(data).hexdigest()


def _collect_package_files(
    root: Path,
    package_dirs: tuple[tuple[str, str], ...],
) -> dict[str, Path]:
    """Собрать файлы компонентов под переданными архивными путями."""
    result: dict[str, Path] = {}
    for source_name, archive_prefix in package_dirs:
        source_dir = root / source_name
        if not source_dir.is_dir():
            raise SystemExit(f"Каталог компонента не найден: {source_dir}")
        for path in sorted(source_dir.rglob("*")):
            relative = path.relative_to(source_dir)
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in _EXCLUDED_DIRS for part in relative.parts):
                continue
            if path.name in _EXCLUDED_FILES or path.suffix in _EXCLUDED_SUFFIXES:
                continue
            archive_path = PurePosixPath(archive_prefix, *relative.parts).as_posix()
            result[archive_path] = path
    return result


def collect_package_files(root: Path = ROOT) -> dict[str, Path]:
    """Собрать incremental payload под путями установки компонентов."""
    return _collect_package_files(root, _PACKAGE_DIRS)


def collect_full_package_files(root: Path = ROOT) -> dict[str, Path]:
    """Собрать полный пакет проекта, сохранив App в `local_apps/dooods2/`."""
    return _collect_package_files(root, _FULL_PACKAGE_DIRS)


def read_component_versions(root: Path = ROOT) -> dict[str, str]:
    """Прочитать независимые версии интеграции и локального App."""
    integration_manifest = root / "custom_components" / "dooods" / "manifest.json"
    app_config = root / "local_apps" / "dooods2" / "config.yaml"
    try:
        manifest = json.loads(integration_manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"Не удалось прочитать {integration_manifest}: {err}") from err
    integration_version = manifest.get("version")
    if not isinstance(integration_version, str) or not integration_version:
        raise SystemExit("В manifest.json не найдена строковая версия интеграции.")
    try:
        app_text = app_config.read_text(encoding="utf-8")
    except OSError as err:
        raise SystemExit(f"Не удалось прочитать {app_config}: {err}") from err
    match = _APP_VERSION_PATTERN.search(app_text)
    if match is None:
        raise SystemExit(f"В {app_config} не найдена версия App.")
    return {"integration": integration_version, "app": match.group(1)}


def calculate_delta(
    previous_hashes: Mapping[str, str], current_payloads: Mapping[str, bytes]
) -> tuple[dict[str, bytes], list[str]]:
    """Вернуть новые/изменённые файлы и пути, удалённые с прошлого релиза."""
    changed = {
        path: data
        for path, data in current_payloads.items()
        if previous_hashes.get(path) != sha256(data)
    }
    deleted = sorted(set(previous_hashes) - set(current_payloads))
    return dict(sorted(changed.items())), deleted


def load_release_state(path: Path = STATE_PATH) -> dict[str, object]:
    """Прочитать сохранённые версии и хеши предыдущего выпуска."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise SystemExit(
            f"Нет базового состояния {path}. Восстановите last_release.json "
            "из полного архива последнего выпуска перед сборкой патча."
        ) from err
    except (OSError, json.JSONDecodeError) as err:
        raise SystemExit(f"Не удалось прочитать {path}: {err}") from err
    if not isinstance(state, dict):
        raise SystemExit(f"Некорректная структура файла {path}.")
    if state.get("schema_version") != 1:
        raise SystemExit(f"Неподдерживаемая схема файла {path}.")
    versions = state.get("versions")
    files = state.get("files")
    if not isinstance(versions, dict) or not isinstance(files, dict):
        raise SystemExit(f"Некорректная структура файла {path}.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in versions.items()):
        raise SystemExit(f"Некорректные версии в {path}.")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in files.items()):
        raise SystemExit(f"Некорректные хеши в {path}.")
    return state


def _validate_archive_path(path: str) -> None:
    """Запретить абсолютные пути и обход каталогов при распаковке ZIP."""
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or ".." in parsed.parts or "\\" in path:
        raise ValueError(f"Небезопасный путь в архиве: {path!r}")


def write_delta_archive(
    archive_path: Path,
    changed_files: Mapping[str, bytes],
    deleted_files: list[str],
    base_versions: Mapping[str, str],
    target_versions: Mapping[str, str],
    support_files: Mapping[str, bytes] | None = None,
) -> None:
    """Записать изменённые файлы, вспомогательные файлы и инструкцию по удалению."""
    support_files = dict(support_files or {})
    for path in (*changed_files.keys(), *deleted_files, *support_files.keys()):
        _validate_archive_path(path)
    if set(changed_files) & set(support_files):
        raise ValueError("Путь не может быть одновременно payload и support file.")
    if set(deleted_files) & (set(changed_files) | set(support_files)):
        raise ValueError("Путь не может одновременно добавляться и удаляться.")

    metadata = {
        "schema_version": 1,
        "archive_type": "incremental",
        "base_versions": dict(base_versions),
        "target_versions": dict(target_versions),
        "changed_files": [
            {"path": path, "sha256": sha256(data)}
            for path, data in sorted(changed_files.items())
        ],
        "deleted_files": sorted(deleted_files),
        "instructions": (
            "Распаковать поверх указанной базовой версии. После распаковки "
            "удалить пути из deleted_files. Архив не является полным установочным пакетом."
        ),
    }
    if support_files:
        metadata["support_files"] = [
            {"path": path, "sha256": sha256(data)}
            for path, data in sorted(support_files.items())
        ]

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in sorted(changed_files.items()):
            archive.writestr(path, data)
        for path, data in sorted(support_files.items()):
            archive.writestr(path, data)
        archive.writestr(
            UPDATE_MANIFEST_NAME,
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )


def _write_state(path: Path, versions: Mapping[str, str], payloads: Mapping[str, bytes]) -> None:
    """Атомарно сохранить хеши файлов нового базового релиза."""
    state = {
        "schema_version": 1,
        "versions": dict(versions),
        "files": {name: sha256(data) for name, data in sorted(payloads.items())},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _release_filename(versions: Mapping[str, str]) -> str:
    """Сформировать имя единого архива из версий обоих компонентов."""
    integration = versions["integration"]
    app = versions["app"]
    if not _VERSION_PATTERN.fullmatch(integration) or not _VERSION_PATTERN.fullmatch(app):
        raise SystemExit("Версия компонента содержит неподдерживаемые символы.")
    return f"dooods_update_{integration}__{app}.zip"


def main() -> None:
    """Синхронизировать App и собрать дельту относительно предыдущего релиза."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать состав дельты, не создавать ZIP и не обновлять базу релиза.",
    )
    args = parser.parse_args()

    app_context = sync_local_app(ROOT)
    print(f"Синхронизирован build context App: {app_context}")

    state = load_release_state()
    previous_versions = state["versions"]
    previous_hashes = state["files"]
    assert isinstance(previous_versions, dict)
    assert isinstance(previous_hashes, dict)

    file_paths = collect_package_files()
    current_payloads = {name: path.read_bytes() for name, path in file_paths.items()}
    current_versions = read_component_versions()
    changed, deleted = calculate_delta(previous_hashes, current_payloads)

    if not changed and not deleted:
        print("Изменений относительно предыдущего релиза нет; архив не создавался.")
        return

    print(f"Новых или изменённых файлов: {len(changed)}")
    for name in changed:
        print(f"  + {name}")
    print(f"Удалённых файлов: {len(deleted)}")
    for name in deleted:
        print(f"  - {name}")
    print(f"Базовые версии: {previous_versions}")
    print(f"Новые версии:   {current_versions}")

    if args.dry_run:
        print("Режим dry-run: ZIP и состояние предыдущего релиза не менялись.")
        return

    archive_path = RELEASE_DIR / _release_filename(current_versions)
    if archive_path.exists():
        raise SystemExit(
            f"Архив {archive_path.name} уже существует. Увеличьте версию компонента "
            "или переместите прежний архив перед повторной сборкой."
        )

    temporary_archive = archive_path.with_suffix(".zip.tmp")
    try:
        write_delta_archive(
            temporary_archive,
            changed,
            deleted,
            previous_versions,
            current_versions,
            support_files={
                "DOOODS-VALIDATE.py": (
                    ROOT / ".github" / "scripts" / "validate_release_archive.py"
                ).read_bytes()
            },
        )
        temporary_archive.replace(archive_path)
        _write_state(STATE_PATH, current_versions, current_payloads)
    except Exception:
        temporary_archive.unlink(missing_ok=True)
        archive_path.unlink(missing_ok=True)
        raise

    print(f"Создан единый инкрементальный архив: {archive_path.relative_to(ROOT)}")
    print(
        f"В архиве: {len(changed)} изменённых файлов, "
        f"валидатор DOOODS-VALIDATE.py и {UPDATE_MANIFEST_NAME}."
    )
    print(f"Состояние предыдущего релиза обновлено: {STATE_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
