"""Постоянное локальное хранилище вручную подтверждённых face embeddings."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
import uuid

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA_VERSION = 1
_BACKUP_FORMAT = "dooods2f_face_gallery"
_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_PEOPLE = 256
_MAX_SAMPLES_PER_PERSON = 128
_MAX_TOTAL_SAMPLES = 4096
_MAX_EMBEDDING_DIMENSION = 4096
_MAX_NAME_LENGTH = 128
_MAX_MODEL_TOKEN_LENGTH = 64
_MAX_EMBEDDING_ABS_VALUE = 1_000_000.0
_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_MODEL_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

logger = logging.getLogger(__name__)
_STORE_FIELDS = {"schema_version", "people"}
_BACKUP_FIELDS = {"format", "schema_version", "exported_at", "people"}
_PERSON_FIELDS = {"id", "name", "created_at", "updated_at", "samples"}
_SAMPLE_FIELDS = {
    "id",
    "model_id",
    "model_version",
    "embedding",
    "confirmed_at",
    "enrollment_method",
    "capture_id",
    "face_index",
}


class FaceGalleryValidationError(ValueError):
    """Некорректные данные человека, образца или резервной копии."""


class FaceGalleryStoreError(RuntimeError):
    """Ошибка чтения или атомарной записи хранилища галереи."""


class FaceGalleryNotFoundError(LookupError):
    """Человек, к которому добавляется образец, отсутствует."""


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], field: str) -> None:
    extra = set(value) - allowed
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise FaceGalleryValidationError(f"Неизвестные поля {field}: {names}")


def _validate_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise FaceGalleryValidationError(f"Некорректный {field}")
    return value


def _validate_model_token(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > _MAX_MODEL_TOKEN_LENGTH
        or not _MODEL_TOKEN_PATTERN.fullmatch(value)
    ):
        raise FaceGalleryValidationError(f"Некорректный {field}")
    return value


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FaceGalleryValidationError("Имя человека не должно быть пустым")
    normalized = value.strip()
    if len(normalized) > _MAX_NAME_LENGTH:
        raise FaceGalleryValidationError(
            f"Имя человека не должно быть длиннее {_MAX_NAME_LENGTH} символов"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise FaceGalleryValidationError("Имя человека содержит управляющие символы")
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as err:
        raise FaceGalleryValidationError("Имя человека содержит недопустимый Unicode") from err
    return normalized


def _validate_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 40:
        raise FaceGalleryValidationError(f"Некорректная дата {field}")
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as err:
        raise FaceGalleryValidationError(f"Некорректная дата {field}") from err
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FaceGalleryValidationError(f"Дата {field} должна содержать часовой пояс")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_embedding(value: Any) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise FaceGalleryValidationError("embedding должен быть числовым списком")
    if not value or len(value) > _MAX_EMBEDDING_DIMENSION:
        raise FaceGalleryValidationError(
            f"Размер embedding должен быть от 1 до {_MAX_EMBEDDING_DIMENSION}"
        )

    embedding: list[float] = []
    norm_squared = 0.0
    for index, raw_value in enumerate(value):
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            raise FaceGalleryValidationError(
                f"Элемент embedding {index} должен быть числом"
            )
        try:
            number = float(raw_value)
        except (OverflowError, ValueError) as err:
            raise FaceGalleryValidationError(
                f"Элемент embedding {index} вне допустимого диапазона"
            ) from err
        if not math.isfinite(number) or abs(number) > _MAX_EMBEDDING_ABS_VALUE:
            raise FaceGalleryValidationError(
                f"Элемент embedding {index} вне допустимого диапазона"
            )
        embedding.append(number)
        norm_squared += number * number
    if norm_squared <= 1e-24:
        raise FaceGalleryValidationError("Нулевой embedding сохранить нельзя")
    return tuple(embedding)


@dataclass(frozen=True, slots=True)
class FaceSample:
    """Образец-признак; изображение лица в этой версии не сохраняется."""

    id: str
    model_id: str
    model_version: str
    embedding: tuple[float, ...]
    confirmed_at: str
    enrollment_method: str = "manual"
    capture_id: str | None = None
    face_index: int | None = None

    def summary(self) -> dict[str, Any]:
        """Метаданные образца без раскрытия самого biometric embedding."""
        return {
            "id": self.id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "embedding_dimension": len(self.embedding),
            "confirmed_at": self.confirmed_at,
            "enrollment_method": self.enrollment_method,
            "capture_id": self.capture_id,
            "face_index": self.face_index,
        }

    def as_dict(self) -> dict[str, Any]:
        """Полное представление только для локального файла/защищённой копии."""
        data: dict[str, Any] = {
            "id": self.id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "embedding": list(self.embedding),
            "confirmed_at": self.confirmed_at,
            "enrollment_method": self.enrollment_method,
        }
        if self.capture_id is not None:
            data["capture_id"] = self.capture_id
        if self.face_index is not None:
            data["face_index"] = self.face_index
        return data


@dataclass(frozen=True, slots=True)
class FacePerson:
    """Запись человека и подтверждённые образцы лица."""

    id: str
    name: str
    created_at: str
    updated_at: str
    samples: tuple[FaceSample, ...]

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "sample_count": len(self.samples),
        }

    def detail(self) -> dict[str, Any]:
        """Детали человека без embedding-векторов."""
        return {
            **self.summary(),
            "samples": [sample.summary() for sample in self.samples],
        }

    def as_dict(self) -> dict[str, Any]:
        """Полное представление для локального файла/резервной копии."""
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "samples": [sample.as_dict() for sample in self.samples],
        }


def _sample_from_dict(value: Any) -> FaceSample:
    if not isinstance(value, Mapping):
        raise FaceGalleryValidationError("Образец должен быть JSON-объектом")
    _reject_unknown_fields(value, _SAMPLE_FIELDS, "образца")
    sample_id = _validate_id(value.get("id"), "ID образца")
    model_id = _validate_model_token(value.get("model_id"), "model_id")
    model_version = _validate_model_token(value.get("model_version"), "model_version")
    embedding = _validate_embedding(value.get("embedding"))
    confirmed_at = _validate_timestamp(value.get("confirmed_at"), "подтверждения")
    if value.get("enrollment_method") != "manual":
        raise FaceGalleryValidationError(
            "Галерея принимает только вручную подтверждённые образцы"
        )
    capture_id = value.get("capture_id")
    if capture_id is not None and not isinstance(capture_id, str):
        raise FaceGalleryValidationError("capture_id образца должен быть строкой")
    face_index = value.get("face_index")
    if face_index is not None and (
        isinstance(face_index, bool) or not isinstance(face_index, int) or face_index < 0
    ):
        raise FaceGalleryValidationError("face_index образца должен быть неотрицательным целым числом")
    return FaceSample(
        id=sample_id,
        model_id=model_id,
        model_version=model_version,
        embedding=embedding,
        confirmed_at=confirmed_at,
        capture_id=capture_id,
        face_index=face_index,
    )


def _person_from_dict(value: Any) -> FacePerson:
    if not isinstance(value, Mapping):
        raise FaceGalleryValidationError("Человек должен быть JSON-объектом")
    _reject_unknown_fields(value, _PERSON_FIELDS, "человека")
    person_id = _validate_id(value.get("id"), "ID человека")
    name = _validate_name(value.get("name"))
    created_at = _validate_timestamp(value.get("created_at"), "создания человека")
    updated_at = _validate_timestamp(value.get("updated_at"), "обновления человека")
    if updated_at < created_at:
        raise FaceGalleryValidationError(
            "Дата обновления человека не может предшествовать дате создания"
        )

    raw_samples = value.get("samples")
    if not isinstance(raw_samples, list):
        raise FaceGalleryValidationError("samples человека должны быть списком")
    if len(raw_samples) > _MAX_SAMPLES_PER_PERSON:
        raise FaceGalleryValidationError(
            f"У человека не может быть больше {_MAX_SAMPLES_PER_PERSON} образцов"
        )

    samples: list[FaceSample] = []
    seen_sample_ids: set[str] = set()
    for raw_sample in raw_samples:
        sample = _sample_from_dict(raw_sample)
        if sample.id in seen_sample_ids:
            raise FaceGalleryValidationError(
                f"ID образца {sample.id} повторяется у одного человека"
            )
        if sample.confirmed_at < created_at or sample.confirmed_at > updated_at:
            raise FaceGalleryValidationError(
                f"Дата подтверждения образца {sample.id} вне диапазона жизни записи человека"
            )
        seen_sample_ids.add(sample.id)
        samples.append(sample)

    return FacePerson(
        id=person_id,
        name=name,
        created_at=created_at,
        updated_at=updated_at,
        samples=tuple(samples),
    )


class FaceGalleryStore:
    """Потокобезопасная JSON-галерея с атомарной записью и версией схемы."""

    def __init__(self, path: str | Path, face_debug_store: Any = None) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.face_debug_store = face_debug_store
        self._people = self._read_file()

    @staticmethod
    def _validate_people(raw_people: Any) -> dict[str, FacePerson]:
        if not isinstance(raw_people, list):
            raise FaceGalleryValidationError("people в галерее должны быть списком")
        if len(raw_people) > _MAX_PEOPLE:
            raise FaceGalleryValidationError(
                f"В галерее не может быть больше {_MAX_PEOPLE} человек"
            )

        people: dict[str, FacePerson] = {}
        total_samples = 0
        for raw_person in raw_people:
            person = _person_from_dict(raw_person)
            if person.id in people:
                raise FaceGalleryValidationError(
                    f"ID человека {person.id} повторяется"
                )
            people[person.id] = person
            total_samples += len(person.samples)
            if total_samples > _MAX_TOTAL_SAMPLES:
                raise FaceGalleryValidationError(
                    f"В галерее не может быть больше {_MAX_TOTAL_SAMPLES} образцов"
                )
        return people

    def _read_file(self) -> dict[str, FacePerson]:
        """Загрузить схему v1; при повреждении не заменять файл пустой галереей."""
        try:
            size = self.path.stat().st_size
            if size > _MAX_FILE_BYTES:
                raise FaceGalleryStoreError("Файл галереи превышает допустимый размер")
            with self.path.open("r", encoding="utf-8") as gallery_file:
                payload = json.load(gallery_file)
        except FileNotFoundError:
            return {}
        except FaceGalleryStoreError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as err:
            raise FaceGalleryStoreError(
                "Не удалось прочитать файл галереи"
            ) from err

        if not isinstance(payload, Mapping) or set(payload) != _STORE_FIELDS:
            raise FaceGalleryStoreError("Неверная структура файла галереи")
        schema_version = payload.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise FaceGalleryStoreError(
                f"Неподдерживаемая версия файла галереи: {schema_version}"
            )
        try:
            return self._validate_people(payload.get("people"))
        except FaceGalleryValidationError as err:
            raise FaceGalleryStoreError(
                f"Некорректный файл галереи: {err}"
            ) from err

    @staticmethod
    def _file_payload(people: Mapping[str, FacePerson]) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "people": [people[person_id].as_dict() for person_id in sorted(people)],
        }

    def _write_file(self, people: Mapping[str, FacePerson]) -> None:
        """Атомарно сохранить новый снимок; временный файл создаётся с mode 0600."""
        payload = self._file_payload(people)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise FaceGalleryStoreError("Не удалось подготовить каталог галереи") from err
        try:
            content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            )
        except (TypeError, ValueError, UnicodeEncodeError) as err:
            raise FaceGalleryStoreError("Не удалось сериализовать галерею") from err
        if len(content) > _MAX_FILE_BYTES:
            raise FaceGalleryValidationError("Галерея превышает допустимый размер файла")

        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = temporary_file.name
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except OSError as err:
            raise FaceGalleryStoreError("Не удалось сохранить файл галереи") from err
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass

    @staticmethod
    def _total_samples(people: Mapping[str, FacePerson]) -> int:
        return sum(len(person.samples) for person in people.values())

    def list_people(self) -> list[dict[str, Any]]:
        """Вернуть метаданные людей без embedding-векторов."""
        with self._lock:
            return [self._people[key].summary() for key in sorted(self._people)]

    def get_people_with_embeddings(self) -> list[dict[str, Any]]:
        """Вернуть людей с embedding-векторами для сопоставления (внутреннее использование)."""
        with self._lock:
            return [
                {
                    "id": person.id,
                    "name": person.name,
                    "samples": [
                        {
                            "id": sample.id,
                            "model_id": sample.model_id,
                            "model_version": sample.model_version,
                            "embedding": list(sample.embedding),
                            "confirmed_at": sample.confirmed_at,
                        }
                        for sample in person.samples
                    ],
                }
                for person in self._people.values()
            ]

    def get_person(self, person_id: str) -> dict[str, Any] | None:
        """Вернуть запись и метаданные образцов без embedding-векторов."""
        person_id = _validate_id(person_id, "ID человека")
        with self._lock:
            person = self._people.get(person_id)
            return person.detail() if person is not None else None

    def get_person_face_reference(self, person_id: str) -> dict[str, Any] | None:
        """Вернуть (capture_id, face_index) первого образца с привязкой к кадру.

        Используется для получения изображения лица персоны из приватного буфера.
        """
        person_id = _validate_id(person_id, "ID человека")
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return None
            for sample in person.samples:
                if sample.capture_id is not None and sample.face_index is not None:
                    return {
                        "capture_id": sample.capture_id,
                        "face_index": sample.face_index,
                        "sample_id": sample.id,
                    }
        return None

    def has_person_face_image(self, person_id: str, sample_id: str) -> bool:
        """Есть ли эталонное изображение образца в постоянном буфере App."""
        try:
            person_id = _validate_id(person_id, "ID человека")
            sample_id = _validate_id(sample_id, "ID образца")
        except FaceGalleryValidationError:
            return False
        if self._person_face_image_path(person_id, sample_id).is_file():
            return True
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return False
            sample = next((s for s in person.samples if s.id == sample_id), None)
        return bool(
            sample is not None
            and sample.capture_id is not None
            and sample.face_index is not None
            and self.face_debug_store is not None
        )

    def get_reference_face_image(self, person_id: str) -> bytes | None:
        """Вернуть JPEG эталона из постоянного буфера, без обращения к кадрам.

        Используется страницей совпадений: кадр-источник может быть вытеснен
        из FIFO-буфера, а копия эталона остаётся навсегда.
        """
        person_id = _validate_id(person_id, "ID человека")
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return None
            sample_ids = [sample.id for sample in person.samples]
        for sample_id in sample_ids:
            path = self._person_face_image_path(person_id, sample_id)
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if content.startswith(b"\xff\xd8\xff"):
                return content
        return None

    def _person_face_image_path(self, person_id: str, sample_id: str) -> Path:
        return self.path.parent / "face_debug" / "person_faces" / person_id / f"face-{sample_id}.jpg"

    def get_person_face_image(self, person_id: str, sample_id: str) -> bytes | None:
        """Вернуть байты JPEG эталонного лица образца или None, если недоступно.

        Сначала читает сохранённый файл из приватного буфера App;
        при отсутствии — пытается достать кроп из буфера событий FaceDebugStore
        по capture_id/face_index образца.
        """
        person_id = _validate_id(person_id, "ID человека")
        sample_id = _validate_id(sample_id, "ID образца")
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return None
            sample = next((s for s in person.samples if s.id == sample_id), None)
            if sample is None:
                return None
        path = self._person_face_image_path(person_id, sample_id)
        try:
            return path.read_bytes()
        except FileNotFoundError:
            pass
        except OSError:
            return None
        if sample.capture_id is not None and sample.face_index is not None and self.face_debug_store is not None:
            try:
                from face_debug_store import (
                    FaceDebugNotFoundError,
                    FaceDebugStoreError,
                    FaceDebugValidationError,
                )
                return self.face_debug_store.get_capture_image(
                    sample.capture_id, kind="face", face_index=sample.face_index
                )
            except (FaceDebugNotFoundError, FaceDebugValidationError, FaceDebugStoreError):
                return None
        return None

    def create_person(self, name: Any) -> dict[str, Any]:
        """Создать человека с серверным стабильным ID и пустой галереей образцов."""
        normalized_name = _validate_name(name)
        with self._lock:
            if len(self._people) >= _MAX_PEOPLE:
                raise FaceGalleryValidationError(
                    f"В галерее не может быть больше {_MAX_PEOPLE} человек"
                )
            now = _utc_now()
            person = FacePerson(
                id=uuid.uuid4().hex,
                name=normalized_name,
                created_at=now,
                updated_at=now,
                samples=(),
            )
            updated_people = dict(self._people)
            updated_people[person.id] = person
            self._write_file(updated_people)
            self._people = updated_people
        return person.detail()

    def rename_person(self, person_id: str, name: Any) -> dict[str, Any] | None:
        """Переименовать человека, сохранив его ID и все образцы."""
        person_id = _validate_id(person_id, "ID человека")
        normalized_name = _validate_name(name)
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return None
            updated_person = replace(
                person,
                name=normalized_name,
                updated_at=_utc_now(),
            )
            updated_people = dict(self._people)
            updated_people[person_id] = updated_person
            self._write_file(updated_people)
            self._people = updated_people
        return updated_person.detail()

    def delete_person(self, person_id: str) -> bool:
        """Удалить человека вместе со всеми embeddings и метаданными образцов."""
        person_id = _validate_id(person_id, "ID человека")
        with self._lock:
            if person_id not in self._people:
                return False
            updated_people = dict(self._people)
            del updated_people[person_id]
            self._write_file(updated_people)
            self._people = updated_people
        return True

    def add_sample(
        self,
        person_id: str,
        *,
        model_id: Any,
        model_version: Any,
        embedding: Any,
        manual_confirmation: Any,
        capture_id: Any = None,
        face_index: Any = None,
    ) -> dict[str, Any]:
        """Сохранить embedding только при явном подтверждении администратора."""
        person_id = _validate_id(person_id, "ID человека")
        if manual_confirmation is not True:
            raise FaceGalleryValidationError(
                "Для добавления образца требуется manual_confirmation=true"
            )
        normalized_model_id = _validate_model_token(model_id, "model_id")
        normalized_model_version = _validate_model_token(
            model_version,
            "model_version",
        )
        normalized_embedding = _validate_embedding(embedding)
        if capture_id is not None and not isinstance(capture_id, str):
            raise FaceGalleryValidationError("capture_id должен быть строкой")
        if face_index is not None and (
            isinstance(face_index, bool) or not isinstance(face_index, int) or face_index < 0
        ):
            raise FaceGalleryValidationError("face_index должен быть неотрицательным целым числом")
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                raise FaceGalleryNotFoundError("Человек не найден")
            if len(person.samples) >= _MAX_SAMPLES_PER_PERSON:
                raise FaceGalleryValidationError(
                    f"У человека не может быть больше {_MAX_SAMPLES_PER_PERSON} образцов"
                )
            if self._total_samples(self._people) >= _MAX_TOTAL_SAMPLES:
                raise FaceGalleryValidationError(
                    f"В галерее не может быть больше {_MAX_TOTAL_SAMPLES} образцов"
                )

            sample = FaceSample(
                id=uuid.uuid4().hex,
                model_id=normalized_model_id,
                model_version=normalized_model_version,
                embedding=normalized_embedding,
                confirmed_at=_utc_now(),
                capture_id=capture_id,
                face_index=face_index,
            )
            updated_person = replace(
                person,
                updated_at=sample.confirmed_at,
                samples=person.samples + (sample,),
            )
            updated_people = dict(self._people)
            updated_people[person_id] = updated_person
            self._write_file(updated_people)
            self._people = updated_people

            if capture_id is not None and face_index is not None:
                self._save_person_face_image(
                    person_id, sample.id, capture_id, face_index
                )
        return sample.summary()

    def _save_person_face_image(
        self,
        person_id: str,
        sample_id: str,
        capture_id: str,
        face_index: int,
    ) -> None:
        """Сохранить кадр лица персоны в приватном буфере App.

        Путь: /data/face_debug/person_faces/{person_id}/face-{sample_id}.jpg.
        Ошибка чтения из буфера не откатывает запись образца — только логируется.
        """
        if self.face_debug_store is None:
            return
        from face_debug_store import (
            FaceDebugNotFoundError,
            FaceDebugStoreError,
            FaceDebugValidationError,
        )
        try:
            jpeg_bytes = self.face_debug_store.get_capture_image(
                capture_id, kind="face", face_index=face_index
            )
        except (FaceDebugNotFoundError, FaceDebugValidationError) as err:
            logger.warning(
                "Не удалось получить изображение лица %s из кадра %s: %s",
                sample_id,
                capture_id,
                err,
            )
            return
        except FaceDebugStoreError as err:
            logger.warning(
                "Ошибка чтения изображения лица %s из кадра %s: %s",
                sample_id,
                capture_id,
                err,
            )
            return
        try:
            target_dir = self.path.parent / "face_debug" / "person_faces" / person_id
            target_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(target_dir, 0o700)
            except OSError:
                pass
            target_path = target_dir / f"face-{sample_id}.jpg"
            target_path.write_bytes(jpeg_bytes)
            try:
                os.chmod(target_path, 0o600)
            except OSError:
                pass
        except OSError as err:
            logger.warning(
                "Не удалось сохранить изображение лица %s в приватный буфер: %s",
                sample_id,
                err,
            )

    def delete_sample(self, person_id: str, sample_id: str) -> bool:
        """Удалить один образец, не удаляя запись человека."""
        person_id = _validate_id(person_id, "ID человека")
        sample_id = _validate_id(sample_id, "ID образца")
        with self._lock:
            person = self._people.get(person_id)
            if person is None:
                return False
            remaining_samples = tuple(
                sample for sample in person.samples if sample.id != sample_id
            )
            if len(remaining_samples) == len(person.samples):
                return False
            sample_removed = next((s for s in person.samples if s.id == sample_id), None)
            updated_person = replace(
                person,
                updated_at=_utc_now(),
                samples=remaining_samples,
            )
            updated_people = dict(self._people)
            updated_people[person_id] = updated_person
            self._write_file(updated_people)
            self._people = updated_people
        # Удалить сохранённый файл эталона из приватного буфера App (если есть).
        if sample_removed is not None:
            try:
                self._person_face_image_path(person_id, sample_id).unlink(missing_ok=True)
            except OSError:
                pass
        return True

    def export_backup(self) -> dict[str, Any]:
        """Сформировать полный экспорт; включает embeddings и не включает изображения."""
        with self._lock:
            return {
                "format": _BACKUP_FORMAT,
                "schema_version": _SCHEMA_VERSION,
                "exported_at": _utc_now(),
                "people": [
                    self._people[key].as_dict() for key in sorted(self._people)
                ],
            }

    def replace_from_backup(self, backup: Any) -> dict[str, int]:
        """Проверить полную резервную копию до атомарной замены текущей галереи."""
        if not isinstance(backup, Mapping):
            raise FaceGalleryValidationError("Резервная копия должна быть JSON-объектом")
        _reject_unknown_fields(backup, _BACKUP_FIELDS, "резервной копии")
        if set(backup) != _BACKUP_FIELDS:
            raise FaceGalleryValidationError("Неполная структура резервной копии")
        if backup.get("format") != _BACKUP_FORMAT:
            raise FaceGalleryValidationError("Неизвестный формат резервной копии")
        schema_version = backup.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise FaceGalleryValidationError(
                f"Неподдерживаемая версия резервной копии: {schema_version}"
            )
        _validate_timestamp(backup.get("exported_at"), "экспорта")
        try:
            replacement = self._validate_people(backup.get("people"))
        except FaceGalleryValidationError:
            raise

        with self._lock:
            self._write_file(replacement)
            self._people = replacement
        return {
            "people": len(replacement),
            "samples": self._total_samples(replacement),
        }
