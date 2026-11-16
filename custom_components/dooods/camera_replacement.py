"""Pure helpers for explicitly replacing one configured camera source."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .camera_selection import (
    SELECTION_VERSION,
    SELECTION_VERSION_KEY,
    resolve_camera_selection,
)

ARCHIVE_SOURCES = "camera_archive_sources"
CAMERA_MAPS = (
    "camera_profiles",
    "motion_sensors",
    "camera_scan_intervals",
    "camera_motion_recheck_intervals",
    "camera_detection_enabled",
    "camera_stream_enabled",
)


def archive_source(options: Mapping[str, Any], camera_id: str) -> str:
    """Resolve one camera's private archive namespace without following chains."""
    mapping = options.get(ARCHIVE_SOURCES)
    value = mapping.get(camera_id) if isinstance(mapping, Mapping) else None
    return value if isinstance(value, str) and value.startswith("camera.") else camera_id


def replacement_options(options: Mapping[str, Any], old: str, new: str) -> dict[str, Any]:
    """Replace an active camera and transfer its per-camera configuration.

    This helper never touches files or the entity registry. The caller validates
    profiles, target archive, ownership and access before persisting the result.
    """
    cameras, objects, faces = resolve_camera_selection(options)
    if old not in cameras:
        raise ValueError("Старая камера больше не выбрана в этой записи.")
    if not new.startswith("camera.") or new == old:
        raise ValueError("Выберите другую корректную камеру.")
    if new in cameras:
        raise ValueError("Новая камера уже используется в этой записи.")
    for key in CAMERA_MAPS:
        value = options.get(key)
        if isinstance(value, Mapping) and new in value:
            raise ValueError("Для новой камеры уже существуют настройки; замена остановлена.")
    archives = options.get(ARCHIVE_SOURCES)
    if archives is not None and not isinstance(archives, Mapping):
        raise ValueError("Некорректная конфигурация архива; замена остановлена.")
    archives = dict(archives or {})
    old_root = archive_source(options, old)
    if new in archives:
        raise ValueError("Для новой камеры уже существует привязка архива.")
    for camera in set(cameras) | set(archives):
        if camera != old and archive_source(options, camera) in {old_root, new}:
            raise ValueError("Архив уже связан с другой камерой.")
    result = deepcopy(dict(options))
    result[SELECTION_VERSION_KEY] = SELECTION_VERSION
    result["object_detection_cameras"] = [new if camera == old else camera for camera in objects]
    result["face_detection_cameras"] = [new if camera == old else camera for camera in faces]
    result["source"] = list(dict.fromkeys(result["object_detection_cameras"] + result["face_detection_cameras"]))
    for key in CAMERA_MAPS:
        value = result.get(key)
        if isinstance(value, Mapping):
            remapped = dict(value)
            if old in remapped:
                remapped[new] = remapped.pop(old)
            result[key] = remapped
    archives.pop(old, None)
    if old_root != new:
        archives[new] = old_root
    result[ARCHIVE_SOURCES] = archives
    return result
