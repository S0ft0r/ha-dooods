"""Отдельное хранилище связей профиля с источником временного preview."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ProfileSourceValidationError(ValueError):
    """Ошибка формата ID профиля или непрозрачного source_id."""


class ProfileSourceStoreError(RuntimeError):
    """Ошибка чтения или записи файла связей preview-источников."""


class ProfileSourceStore:
    """Хранит только profile_id → opaque source_id, отдельно от профилей и JPEG."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._sources = self._read_file()

    @staticmethod
    def _validate_profile_id(profile_id: Any) -> str:
        """Проверить внутренний стабильный ID профиля."""
        if not isinstance(profile_id, str) or not _PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ProfileSourceValidationError("Некорректный ID профиля")
        return profile_id

    @staticmethod
    def _validate_source_id(source_id: Any) -> str:
        """Проверить source_id как непрозрачную строку, не интерпретируя её."""
        if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise ProfileSourceValidationError("Некорректный source_id")
        return source_id

    def _read_file(self) -> dict[str, str]:
        """Загрузить связи; отсутствие файла означает пустое хранилище."""
        try:
            with self.path.open("r", encoding="utf-8") as source_file:
                payload = json.load(source_file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as err:
            raise ProfileSourceStoreError(
                f"Не удалось прочитать связи preview-источников: {err}"
            ) from err

        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "profile_sources",
        }:
            raise ProfileSourceStoreError("Неверная структура файла связей preview-источников")
        if type(payload.get("schema_version")) is not int or payload["schema_version"] != _SCHEMA_VERSION:
            raise ProfileSourceStoreError("Неподдерживаемая схема файла связей preview-источников")
        raw_sources = payload.get("profile_sources")
        if not isinstance(raw_sources, Mapping):
            raise ProfileSourceStoreError("profile_sources должен быть объектом")

        try:
            return {
                self._validate_profile_id(profile_id): self._validate_source_id(source_id)
                for profile_id, source_id in raw_sources.items()
            }
        except ProfileSourceValidationError as err:
            raise ProfileSourceStoreError(
                f"Некорректная связь preview-источника: {err}"
            ) from err

    def _write_file(self, sources: Mapping[str, str]) -> None:
        """Атомарно сохранить только связи ID; изображений или entity ID здесь нет."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "profile_sources": {
                profile_id: sources[profile_id]
                for profile_id in sorted(sources)
            },
        }
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                json.dump(payload, temporary_file, ensure_ascii=False, indent=2)
                temporary_file.write("\n")
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
        except OSError as err:
            raise ProfileSourceStoreError(
                f"Не удалось сохранить связи preview-источников: {err}"
            ) from err
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def get(self, profile_id: str) -> str | None:
        """Вернуть запомненный непрозрачный source_id или None."""
        profile_id = self._validate_profile_id(profile_id)
        with self._lock:
            return self._sources.get(profile_id)

    def put(self, profile_id: str, source_id: str) -> None:
        """Сохранить или заменить preview-источник для одного профиля."""
        profile_id = self._validate_profile_id(profile_id)
        source_id = self._validate_source_id(source_id)
        with self._lock:
            updated_sources = dict(self._sources)
            updated_sources[profile_id] = source_id
            self._write_file(updated_sources)
            self._sources = updated_sources

    def delete(self, profile_id: str) -> bool:
        """Удалить связь профиля; вернуть false, если её не было."""
        profile_id = self._validate_profile_id(profile_id)
        with self._lock:
            if profile_id not in self._sources:
                return False
            updated_sources = dict(self._sources)
            del updated_sources[profile_id]
            self._write_file(updated_sources)
            self._sources = updated_sources
        return True
