"""Схема, проверка и постоянное хранение профилей зон."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

try:
    from .zones import Polygon, PolygonZone
except ImportError:  # запуск приложения как набора модулей из каталога application
    from zones import Polygon, PolygonZone

_SCHEMA_VERSION = 1
_MAX_PROFILE_NAME_LENGTH = 128
_MAX_ZONES_PER_PROFILE = 128
_MAX_VERTICES_PER_ZONE = 256
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PROFILE_FIELDS = {"id", "name", "zones"}
_ZONE_FIELDS = {"id", "name", "points", "covers", "labels"}


class ProfileValidationError(ValueError):
    """Ошибка формата или значений профиля."""


class ProfileStoreError(RuntimeError):
    """Ошибка чтения или записи файла профилей."""


def _validate_identifier(value: Any, field: str) -> str:
    """Проверить безопасный стабильный идентификатор профиля или зоны."""
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ProfileValidationError(
            f"{field} должен содержать 1–64 латинских букв, цифр, точек, дефисов или подчёркиваний"
        )
    return value


def _validate_name(value: Any, field: str) -> str:
    """Проверить отображаемое имя профиля или зоны."""
    if not isinstance(value, str) or not value.strip():
        raise ProfileValidationError(f"{field} не должен быть пустым")
    normalized = value.strip()
    if len(normalized) > _MAX_PROFILE_NAME_LENGTH:
        raise ProfileValidationError(
            f"{field} не должен быть длиннее {_MAX_PROFILE_NAME_LENGTH} символов"
        )
    return normalized


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    """Не позволять незаметно игнорировать опечатки в JSON-контракте."""
    extra = set(value) - allowed
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ProfileValidationError(f"Неизвестные поля {field}: {names}")


@dataclass(frozen=True, slots=True)
class ProfileZone:
    """Сохраняемая зона профиля и её runtime-представление."""

    id: str
    name: str
    polygon: Polygon
    covers: bool
    labels: Mapping[str, float]

    def to_polygon_zone(self) -> PolygonZone:
        """Преобразовать зону хранения в объект геометрического фильтра."""
        return PolygonZone(
            zone_id=self.id,
            polygon=self.polygon,
            labels=self.labels,
            covers=self.covers,
        )

    def as_dict(self) -> dict[str, Any]:
        """Сформировать JSON-совместимое представление зоны."""
        return {
            "id": self.id,
            "name": self.name,
            "points": [
                {"left": left, "top": top}
                for left, top in self.polygon.points
            ],
            "covers": self.covers,
            "labels": dict(self.labels),
        }


@dataclass(frozen=True, slots=True)
class DetectionProfile:
    """Именованный профиль с упорядоченным списком зон."""

    id: str
    name: str
    zones: tuple[ProfileZone, ...]

    @property
    def polygon_zones(self) -> tuple[PolygonZone, ...]:
        """Вернуть геометрические зоны в сохранённом порядке приоритета."""
        return tuple(zone.to_polygon_zone() for zone in self.zones)

    def as_dict(self) -> dict[str, Any]:
        """Сформировать JSON-совместимое представление профиля."""
        return {
            "id": self.id,
            "name": self.name,
            "zones": [zone.as_dict() for zone in self.zones],
        }


def profile_from_dict(value: Any) -> DetectionProfile:
    """Проверить JSON-объект и создать неизменяемую модель профиля."""
    if not isinstance(value, Mapping):
        raise ProfileValidationError("Профиль должен быть JSON-объектом")
    _reject_unknown_fields(value, _PROFILE_FIELDS, "профиля")

    profile_id = _validate_identifier(value.get("id"), "id профиля")
    profile_name = _validate_name(value.get("name"), "name профиля")
    raw_zones = value.get("zones", [])
    if not isinstance(raw_zones, Sequence) or isinstance(raw_zones, (str, bytes)):
        raise ProfileValidationError("zones профиля должны быть списком")
    if len(raw_zones) > _MAX_ZONES_PER_PROFILE:
        raise ProfileValidationError(
            f"Профиль не может содержать больше {_MAX_ZONES_PER_PROFILE} зон"
        )

    zones: list[ProfileZone] = []
    seen_zone_ids: set[str] = set()
    for index, raw_zone in enumerate(raw_zones):
        if not isinstance(raw_zone, Mapping):
            raise ProfileValidationError(f"Зона {index} должна быть JSON-объектом")
        _reject_unknown_fields(raw_zone, _ZONE_FIELDS, f"зоны {index}")

        zone_id = _validate_identifier(raw_zone.get("id"), f"id зоны {index}")
        if zone_id in seen_zone_ids:
            raise ProfileValidationError(f"Идентификатор зоны {zone_id} повторяется")
        seen_zone_ids.add(zone_id)
        zone_name = _validate_name(raw_zone.get("name"), f"name зоны {index}")

        raw_points = raw_zone.get("points")
        if (
            not isinstance(raw_points, Sequence)
            or isinstance(raw_points, (str, bytes))
        ):
            raise ProfileValidationError(f"points зоны {zone_id} должны быть списком")
        if len(raw_points) > _MAX_VERTICES_PER_ZONE:
            raise ProfileValidationError(
                f"Полигон зоны {zone_id} не может содержать больше "
                f"{_MAX_VERTICES_PER_ZONE} вершин"
            )
        try:
            polygon = Polygon.from_points(raw_points)
        except ValueError as err:
            raise ProfileValidationError(
                f"Некорректный полигон зоны {zone_id}: {err}"
            ) from err

        covers = raw_zone.get("covers", False)
        if not isinstance(covers, bool):
            raise ProfileValidationError(f"covers зоны {zone_id} должен быть boolean")

        labels = raw_zone.get("labels", {})
        if not isinstance(labels, Mapping):
            raise ProfileValidationError(f"labels зоны {zone_id} должны быть объектом")
        try:
            runtime_zone = PolygonZone(
                zone_id=zone_id,
                polygon=polygon,
                labels=labels,
                covers=covers,
            )
        except ValueError as err:
            raise ProfileValidationError(
                f"Некорректные фильтры зоны {zone_id}: {err}"
            ) from err

        zones.append(
            ProfileZone(
                id=zone_id,
                name=zone_name,
                polygon=polygon,
                covers=covers,
                labels=runtime_zone.labels,
            )
        )

    return DetectionProfile(
        id=profile_id,
        name=profile_name,
        zones=tuple(zones),
    )


class ProfileStore:
    """Потокобезопасное хранилище профилей с атомарной JSON-записью."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._profiles = self._read_file()

    def _read_file(self) -> dict[str, DetectionProfile]:
        """Загрузить файл; отсутствие файла означает пустое хранилище."""
        try:
            with self.path.open("r", encoding="utf-8") as profile_file:
                payload = json.load(profile_file)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as err:
            raise ProfileStoreError(f"Не удалось прочитать файл профилей: {err}") from err

        if not isinstance(payload, Mapping) or set(payload) != {
            "schema_version",
            "profiles",
        }:
            raise ProfileStoreError("Неверная структура файла профилей")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise ProfileStoreError(
                f"Неподдерживаемая версия файла профилей: "
                f"{payload.get('schema_version')}"
            )
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise ProfileStoreError("profiles в файле должны быть списком")

        profiles: dict[str, DetectionProfile] = {}
        try:
            for raw_profile in raw_profiles:
                profile = profile_from_dict(raw_profile)
                if profile.id in profiles:
                    raise ProfileValidationError(
                        f"Идентификатор профиля {profile.id} повторяется"
                    )
                profiles[profile.id] = profile
        except ProfileValidationError as err:
            raise ProfileStoreError(f"Некорректный файл профилей: {err}") from err
        return profiles

    def _write_file(self, profiles: Mapping[str, DetectionProfile]) -> None:
        """Атомарно заменить файл полным снимком профилей."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "profiles": [
                profiles[profile_id].as_dict()
                for profile_id in sorted(profiles)
            ],
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
            raise ProfileStoreError(f"Не удалось сохранить профили: {err}") from err
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    def list_profiles(self) -> list[dict[str, Any]]:
        """Вернуть копии профилей в детерминированном порядке."""
        with self._lock:
            return [
                self._profiles[profile_id].as_dict()
                for profile_id in sorted(self._profiles)
            ]

    def get(self, profile_id: str) -> DetectionProfile | None:
        """Получить неизменяемый профиль по идентификатору."""
        with self._lock:
            return self._profiles.get(profile_id)

    def put(
        self,
        value: Any,
        *,
        expected_id: str | None = None,
    ) -> DetectionProfile:
        """Создать или полностью заменить профиль и сохранить его на диск."""
        profile = profile_from_dict(value)
        if expected_id is not None and profile.id != expected_id:
            raise ProfileValidationError(
                "id профиля в URL должен совпадать с id в JSON"
            )

        with self._lock:
            updated_profiles = dict(self._profiles)
            updated_profiles[profile.id] = profile
            self._write_file(updated_profiles)
            self._profiles = updated_profiles
        return profile

    def delete(self, profile_id: str) -> bool:
        """Удалить профиль; вернуть false, если такого профиля не было."""
        with self._lock:
            if profile_id not in self._profiles:
                return False
            updated_profiles = dict(self._profiles)
            del updated_profiles[profile_id]
            self._write_file(updated_profiles)
            self._profiles = updated_profiles
        return True
