"""Автопоиск сенсоров движения, связанных с устройством камеры."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.helpers import entity_registry as er

_MOTION_NAME_MARKERS = ("motion", "movement", "движ")


def camera_motion_sensors(
    hass: Any,
    camera_entity_id: str,
) -> list[dict[str, str]]:
    """Найти включённые бинарные сенсоры движения на устройстве камеры."""
    registry = er.async_get(hass)
    camera_entry = registry.async_get(camera_entity_id)
    device_id = getattr(camera_entry, "device_id", None)
    if not device_id:
        return []

    candidates: list[dict[str, str]] = []
    for entry in registry.entities.values():
        entity_id = getattr(entry, "entity_id", "")
        if (
            getattr(entry, "device_id", None) != device_id
            or not entity_id.startswith("binary_sensor.")
            or getattr(entry, "disabled_by", None) is not None
        ):
            continue

        state = hass.states.get(entity_id)
        attributes = getattr(state, "attributes", {})
        if not isinstance(attributes, Mapping):
            attributes = {}
        device_class = attributes.get("device_class") or getattr(
            entry, "device_class", None
        )
        name = (
            attributes.get("friendly_name")
            or getattr(entry, "name", None)
            or entity_id
        )
        descriptor = f"{name} {entity_id}".casefold()
        is_motion = device_class == "motion" or any(
            marker in descriptor for marker in _MOTION_NAME_MARKERS
        )
        if not is_motion:
            continue

        candidates.append({"entity_id": entity_id, "name": str(name)})

    candidates.sort(
        key=lambda item: (item["name"].casefold(), item["entity_id"])
    )
    return candidates


def resolve_motion_sensors(
    hass: Any,
    camera_entity_ids: list[str],
    configured_sensors: Any,
) -> dict[str, str]:
    """Предпочесть датчик камеры, иначе использовать ручное назначение."""
    if not isinstance(configured_sensors, Mapping):
        configured_sensors = {}

    resolved: dict[str, str] = {}
    for camera_entity_id in camera_entity_ids:
        if not isinstance(camera_entity_id, str):
            continue
        configured_entity_id = configured_sensors.get(camera_entity_id)
        if isinstance(configured_entity_id, str):
            configured_entity_id = configured_entity_id.strip()
        else:
            configured_entity_id = ""

        candidates = camera_motion_sensors(hass, camera_entity_id)
        candidate_ids = {candidate["entity_id"] for candidate in candidates}
        if len(candidates) == 1:
            resolved[camera_entity_id] = candidates[0]["entity_id"]
        elif len(candidates) > 1:
            if configured_entity_id in candidate_ids:
                resolved[camera_entity_id] = configured_entity_id
        elif configured_entity_id:
            resolved[camera_entity_id] = configured_entity_id

    return resolved
