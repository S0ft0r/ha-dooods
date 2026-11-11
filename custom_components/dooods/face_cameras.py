"""Нормализация списка камер, выбранных для распознавания лиц."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_face_detection_cameras(
    value: Any,
    camera_entity_ids: Iterable[str],
) -> list[str]:
    """Оставить только подключённые камеры, сохранив порядок и убрав дубли.

    Список живёт в options записи интеграции. Камера, убранная из основного
    списка, автоматически выпадает и отсюда: иначе в App уходил бы источник,
    которого больше нет в опросе.
    """
    allowed = {
        camera
        for camera in camera_entity_ids
        if isinstance(camera, str) and camera
    }
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for camera in value:
        if not isinstance(camera, str):
            continue
        camera = camera.strip()
        if not camera or camera in seen or camera not in allowed:
            continue
        seen.add(camera)
        normalized.append(camera)
    return normalized
