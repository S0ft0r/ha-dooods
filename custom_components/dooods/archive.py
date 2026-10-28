"""Локальный архив размеченных кадров и очистка по квоте хранения."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

_ARCHIVE_DIRNAME = "dooods"
_ARCHIVE_FILENAME_PREFIX = "dooods_"
_SECONDS_PER_DAY = 24 * 60 * 60


def has_accepted_annotated_frame(
    detections: Sequence[Any],
    image: bytes | None,
    checked_at: datetime | None,
) -> bool:
    """Проверить, что кадр содержит принятую детекцию и размеченный JPEG."""
    return bool(detections) and bool(image) and checked_at is not None


def normalize_archive_limit(value: Any, default: int) -> int:
    """Нормализовать целочисленный лимит архива; ноль означает отсутствие лимита."""
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_archive_directory(value: Any) -> str:
    """Получить один каталог архива и не выбирать наугад из старых шаблонов путей."""
    if isinstance(value, str):
        directories = [line.strip() for line in value.splitlines() if line.strip()]
    elif isinstance(value, list):
        directories = [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    else:
        directories = []
    return directories[0] if len(directories) == 1 else ""


def _safe_component(value: str) -> str:
    """Преобразовать идентификатор в безопасный и устойчивый компонент пути."""
    readable = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_.-").lower()
    readable = readable[:48] or "item"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    return f"{readable}_{digest}"


def _archive_entry_root(directory: Path, entry_id: str) -> Path:
    """Вернуть подкаталог, принадлежащий только этой записи интеграции."""
    return directory / _ARCHIVE_DIRNAME / _safe_component(entry_id)


def _as_utc(value: datetime) -> datetime:
    """Нормализовать время кадра в UTC, считая naive-значение временем UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def save_annotated_frame_sync(
    directory: str | Path,
    entry_id: str,
    camera_entity_id: str,
    image: bytes,
    checked_at: datetime,
    *,
    max_count: int,
    max_age_days: int,
    now: datetime | None = None,
) -> Path:
    """Атомарно сохранить JPEG камеры и применить квоты только к её архиву."""
    root_directory = Path(directory).expanduser()
    entry_root = _archive_entry_root(root_directory, entry_id)
    camera_root = entry_root / _safe_component(camera_entity_id)
    namespace_root = root_directory / _ARCHIVE_DIRNAME
    for managed_directory in (namespace_root, entry_root, camera_root):
        if managed_directory.is_symlink():
            raise OSError("Refusing to write through a symlink in the archive path")
        managed_directory.mkdir(parents=True, exist_ok=True)

    frame_time = _as_utc(checked_at)
    date_directory = camera_root / frame_time.strftime("%Y-%m-%d")
    if date_directory.is_symlink():
        raise OSError("Refusing to write through a symlink in the archive path")
    date_directory.mkdir(parents=True, exist_ok=True)

    timestamp = frame_time.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = date_directory / (
        f"{_ARCHIVE_FILENAME_PREFIX}{timestamp}_{uuid.uuid4().hex[:8]}.jpg"
    )
    temporary = date_directory / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as image_file:
            image_file.write(image)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    _prune_camera_archive_sync(
        camera_root,
        max_count=max_count,
        max_age_days=max_age_days,
        now=now,
    )
    return destination


def prune_archive_sync(
    directory: str | Path,
    entry_id: str,
    *,
    max_count: int,
    max_age_days: int,
    now: datetime | None = None,
) -> int:
    """Удалить архивные JPEG по квотам во всех камерах этой записи."""
    root_directory = Path(directory).expanduser()
    namespace_root = root_directory / _ARCHIVE_DIRNAME
    entry_root = _archive_entry_root(root_directory, entry_id)
    if (
        namespace_root.is_symlink()
        or entry_root.is_symlink()
        or not entry_root.is_dir()
    ):
        return 0

    deleted = 0
    try:
        camera_directories = tuple(entry_root.iterdir())
    except OSError:
        return 0

    for camera_root in camera_directories:
        if not camera_root.is_dir() or camera_root.is_symlink():
            continue
        deleted += _prune_camera_archive_sync(
            camera_root,
            max_count=max_count,
            max_age_days=max_age_days,
            now=now,
        )
    return deleted


def _prune_camera_archive_sync(
    camera_root: Path,
    *,
    max_count: int,
    max_age_days: int,
    now: datetime | None = None,
) -> int:
    """Оставить самые новые кадры камеры, применяя лимит количества и срока через OR."""
    if max_count <= 0 and max_age_days <= 0:
        return 0

    now_timestamp = (now or datetime.now(UTC)).timestamp()
    max_age_seconds = max_age_days * _SECONDS_PER_DAY
    files: list[tuple[int, Path]] = []
    try:
        candidates = camera_root.rglob(f"{_ARCHIVE_FILENAME_PREFIX}*.jpg")
        for path in candidates:
            if path.is_symlink() or not path.is_file():
                continue
            try:
                files.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    except OSError:
        return 0

    files.sort(key=lambda item: item[0], reverse=True)
    kept = 0
    deleted = 0
    for mtime_ns, path in files:
        too_old = max_age_seconds > 0 and (
            now_timestamp - mtime_ns / 1_000_000_000 > max_age_seconds
        )
        over_count = max_count > 0 and kept >= max_count
        if too_old or over_count:
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            deleted += 1
            _remove_empty_date_directories(path.parent, camera_root)
        else:
            kept += 1
    return deleted


def _remove_empty_date_directories(directory: Path, camera_root: Path) -> None:
    """Удалить опустевшие каталог-даты, не выходя из каталога камеры."""
    current = directory
    while current != camera_root:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _resolve_archive_directory(hass: Any, directory: str) -> Path:
    """Разрешить относительный каталог относительно конфигурационного каталога HA."""
    path = Path(directory).expanduser()
    if path.is_absolute():
        return path
    return Path(hass.config.path(str(path)))


async def async_save_annotated_frame(
    hass: Any,
    directory: str,
    entry_id: str,
    camera_entity_id: str,
    image: bytes,
    checked_at: datetime,
    *,
    max_count: int,
    max_age_days: int,
) -> Path:
    """Сохранить принятый JPEG в executor, не блокируя event loop Home Assistant."""
    archive_directory = _resolve_archive_directory(hass, directory)
    save_frame = partial(
        save_annotated_frame_sync,
        archive_directory,
        entry_id,
        camera_entity_id,
        image,
        checked_at,
        max_count=max_count,
        max_age_days=max_age_days,
    )
    return await hass.async_add_executor_job(save_frame)


async def async_prune_archive(
    hass: Any,
    directory: str,
    entry_id: str,
    *,
    max_count: int,
    max_age_days: int,
) -> int:
    """Применить архивные квоты в executor, не блокируя event loop Home Assistant."""
    archive_directory = _resolve_archive_directory(hass, directory)
    prune = partial(
        prune_archive_sync,
        archive_directory,
        entry_id,
        max_count=max_count,
        max_age_days=max_age_days,
    )
    return await hass.async_add_executor_job(prune)
