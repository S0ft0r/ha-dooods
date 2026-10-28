"""Действия Home Assistant для интеграции DOOODS."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_SCAN

SCAN_SERVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(ATTR_ENTITY_ID): cv.entity_ids,
    }
)


def async_register_services(hass: HomeAssistant) -> None:
    """Зарегистрировать действия до загрузки записей интеграции."""
    if hass.services.has_service(DOMAIN, SERVICE_SCAN):
        return

    async def async_scan(call: ServiceCall) -> None:
        """Поставить выбранные камеры в очередь внеочередного сканирования."""
        raw_camera_entity_ids = call.data.get(ATTR_ENTITY_ID, [])
        if isinstance(raw_camera_entity_ids, str):
            camera_entity_ids = [raw_camera_entity_ids]
        elif isinstance(raw_camera_entity_ids, list):
            camera_entity_ids = raw_camera_entity_ids
        else:
            camera_entity_ids = []

        if not camera_entity_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="scan_no_camera_selected",
            )

        invalid_entity_ids = [
            entity_id
            for entity_id in camera_entity_ids
            if not isinstance(entity_id, str) or not entity_id.startswith("camera.")
        ]
        if invalid_entity_ids:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="scan_invalid_camera_target",
                translation_placeholders={
                    "entity_ids": ", ".join(map(str, invalid_entity_ids)),
                },
            )

        camera_entity_ids = list(dict.fromkeys(camera_entity_ids))
        selected_cameras = set(camera_entity_ids)
        coordinator_targets: list[tuple[Any, set[str]]] = []
        configured_cameras: set[str] = set()

        for entry in hass.config_entries.async_loaded_entries(DOMAIN):
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is None:
                continue
            entry_cameras = set(getattr(coordinator, "cameras", ()))
            matching_cameras = selected_cameras.intersection(entry_cameras)
            if matching_cameras:
                coordinator_targets.append((coordinator, matching_cameras))
                configured_cameras.update(matching_cameras)

        missing_cameras = selected_cameras - configured_cameras
        if missing_cameras:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="scan_camera_not_configured",
                translation_placeholders={
                    "camera_entity_ids": ", ".join(sorted(missing_cameras)),
                },
            )

        for coordinator, matching_cameras in coordinator_targets:
            coordinator.request_camera_scans(matching_cameras)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SCAN,
        async_scan,
        schema=SCAN_SERVICE_SCHEMA,
    )
