"""Нормализация переключателей распознавания и RTSP motion gate по камерам."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def normalize_camera_switches(
    value: Any,
    camera_entity_ids: Iterable[str],
    *,
    default: bool | Mapping[str, bool] | None = True,
) -> dict[str, bool]:
    """Normalize saved camera states and optionally fill per-camera defaults."""
    configured = value if isinstance(value, Mapping) else {}
    normalized: dict[str, bool] = {}
    for camera_entity_id in camera_entity_ids:
        if not isinstance(camera_entity_id, str) or not camera_entity_id:
            continue
        switch_state = configured.get(camera_entity_id)
        if isinstance(switch_state, bool):
            normalized[camera_entity_id] = switch_state
            continue
        if default is None:
            continue
        fallback = (
            default.get(camera_entity_id, True)
            if isinstance(default, Mapping)
            else default
        )
        normalized[camera_entity_id] = fallback if isinstance(fallback, bool) else True
    return normalized
