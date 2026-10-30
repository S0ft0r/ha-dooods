"""Нормализация индивидуальных интервалов сканирования камер."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    DEFAULT_CAMERA_SCAN_INTERVAL,
    MAX_CAMERA_SCAN_INTERVAL,
    MIN_CAMERA_SCAN_INTERVAL,
)


def normalize_camera_scan_intervals(
    value: Any,
    camera_entity_ids: Iterable[str],
) -> dict[str, int]:
    """Оставить допустимые интервалы только для выбранных камер.

    Отсутствующее или некорректное значение не заменяется старым глобальным
    параметром `scan_interval`: такая камера использует новый default 30 секунд.
    """
    if not isinstance(value, Mapping):
        return {}

    normalized: dict[str, int] = {}
    for camera_entity_id in camera_entity_ids:
        if not isinstance(camera_entity_id, str) or not camera_entity_id:
            continue
        raw_interval = value.get(camera_entity_id)
        if isinstance(raw_interval, bool):
            continue
        if isinstance(raw_interval, int):
            interval = raw_interval
        elif isinstance(raw_interval, float) and raw_interval.is_integer():
            interval = int(raw_interval)
        elif isinstance(raw_interval, str) and raw_interval.strip().isdecimal():
            interval = int(raw_interval.strip())
        else:
            continue
        if MIN_CAMERA_SCAN_INTERVAL <= interval <= MAX_CAMERA_SCAN_INTERVAL:
            normalized[camera_entity_id] = interval

    return normalized


def camera_scan_interval(
    camera_entity_id: str,
    configured_intervals: Mapping[str, int],
) -> int:
    """Вернуть явный интервал камеры либо default 30 секунд."""
    interval = configured_intervals.get(camera_entity_id)
    if (
        isinstance(interval, int)
        and not isinstance(interval, bool)
        and MIN_CAMERA_SCAN_INTERVAL <= interval <= MAX_CAMERA_SCAN_INTERVAL
    ):
        return interval
    return DEFAULT_CAMERA_SCAN_INTERVAL
