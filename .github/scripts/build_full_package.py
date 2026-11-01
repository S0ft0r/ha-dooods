#!/usr/bin/env python3
"""Собрать полный архив DOOODS с App в `local_apps/dooods2/`."""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile

from build_release import (
    RELEASE_DIR,
    ROOT,
    collect_full_package_files,
    read_component_versions,
    sha256,
)
from package_local_app import sync_local_app

VALIDATOR_PATH = ROOT / ".github" / "scripts" / "validate_release_archive.py"
VALIDATOR_MEMBER = "DOOODS-VALIDATE.py"


def main() -> None:
    """Собрать установочные файлы интеграции и App, сохранив структуру проекта."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Заменить одноимённый полный архив после проверки нового ZIP.",
    )
    parser.add_argument(
        "--without-validator",
        action="store_true",
        help="Не добавлять DOOODS-VALIDATE.py в полный пакет.",
    )
    args = parser.parse_args()

    app_context = sync_local_app(ROOT)
    print(f"Синхронизирован build context App: {app_context}")
    versions = read_component_versions(ROOT)
    suffix = "" if args.without_validator else "_with_validator"
    archive_path = RELEASE_DIR / (
        f"dooods_full_{versions['integration']}__{versions['app']}{suffix}.zip"
    )
    if archive_path.exists() and not args.overwrite:
        raise SystemExit(
            f"Архив {archive_path.name} уже существует. "
            "Используйте --overwrite только для его намеренной замены."
        )

    files = collect_full_package_files(ROOT)
    if not args.without_validator:
        if not VALIDATOR_PATH.is_file():
            raise SystemExit(f"Standalone validator not found: {VALIDATOR_PATH}")
        files[VALIDATOR_MEMBER] = VALIDATOR_PATH
    if "local_apps/dooods2/config.yaml" not in files:
        raise SystemExit("В полный пакет не вошёл App local_apps/dooods2/config.yaml")
    if "custom_components/dooods/manifest.json" not in files:
        raise SystemExit("В полный пакет не вошла интеграция dooods")

    temporary_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
    try:
        with zipfile.ZipFile(
            temporary_path,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name, path in sorted(files.items()):
                archive.write(path, arcname=name)

        with zipfile.ZipFile(temporary_path) as archive:
            if archive.testzip() is not None:
                raise zipfile.BadZipFile("CRC/содержимое архива не прошло проверку")
            names = set(archive.namelist())
        if any(name.startswith("dooods2/") for name in names):
            raise zipfile.BadZipFile("App не должен лежать в корне архива")
        temporary_path.replace(archive_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    payload = archive_path.read_bytes()
    print(f"Создан полный пакет: {archive_path.relative_to(ROOT)}")
    print(f"Файлов: {len(files)}; размер: {len(payload)} байт")
    print(f"SHA-256: {sha256(payload)}")
    print("App расположен в local_apps/dooods2/; внутренние документы и тесты не включены.")
    if not args.without_validator:
        print(f"В архив включён standalone-валидатор {VALIDATOR_MEMBER}.")


if __name__ == "__main__":
    main()
