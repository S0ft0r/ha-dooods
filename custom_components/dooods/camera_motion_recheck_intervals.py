"""Персональные паузы повторной RTSP-проверки DOOODS."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    DEFAULT_CAMERA_MOTION_RECHECK_INTERVAL,
    MAX_CAMERA_MOTION_RECHECK_INTERVAL,
    MIN_CAMERA_MOTION_RECHECK_INTERVAL,
)


def normalize_camera_motion_recheck_intervals(
    value: Any,
    camera_entity_ids: Iterable[str],
) -> dict[str, int]:
    """Нормализовать задержки повторной проверки выбранных камер."""
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
        if (
            MIN_CAMERA_MOTION_RECHECK_INTERVAL
            <= interval
            <= MAX_CAMERA_MOTION_RECHECK_INTERVAL
        ):
            normalized[camera_entity_id] = interval

    return normalized


def camera_motion_recheck_interval(
    camera_entity_id: str,
    configured_intervals: Mapping[str, int],
) -> int:
    """Вернуть задержку или default 5 секунд; 0 отключает cooldown."""
    interval = configured_intervals.get(camera_entity_id)
    if (
        isinstance(interval, int)
        and not isinstance(interval, bool)
        and MIN_CAMERA_MOTION_RECHECK_INTERVAL
        <= interval
        <= MAX_CAMERA_MOTION_RECHECK_INTERVAL
    ):
        return interval
    return DEFAULT_CAMERA_MOTION_RECHECK_INTERVAL
