"""Сведения об устройствах-камерах, общие для сущностей DOOODS."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .coordinator import DooodsCoordinator


def _translated_device_name(
    info_type: type[Any],
    *,
    fallback_name: str,
    translation_key: str,
    translation_placeholders: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Подготовить переведённое имя или совместимый вариант для старой HA."""
    fields = getattr(info_type, "__annotations__", {})
    if "translation_key" not in fields:
        return {"name": fallback_name}

    values: dict[str, Any] = {"translation_key": translation_key}
    if translation_placeholders and "translation_placeholders" in fields:
        values["translation_placeholders"] = translation_placeholders
    return values


def camera_device_info(
    hass: HomeAssistant,
    coordinator: DooodsCoordinator,
    camera_entity_id: str,
) -> Any:
    """Вернуть современное описание дочернего устройства с совместимостью со старой HA."""
    identifiers = {(DOMAIN, f"{coordinator.entry_id}:camera:{camera_entity_id}")}
    name = camera_name(hass, camera_entity_id)
    camera_name_values = _translated_device_name(
        getattr(dr, "ChildDeviceInfo", dr.DeviceInfo),
        fallback_name=name,
        translation_key="camera_child",
        translation_placeholders={"camera_name": name},
    )

    if child_device_info := getattr(dr, "ChildDeviceInfo", None):
        return child_device_info(
            identifiers=identifiers,
            parent_device_id=coordinator.parent_device_id,
            **camera_name_values,
        )

    device_info_fields = getattr(dr.DeviceInfo, "__annotations__", {})
    if "via_device_id" in device_info_fields:
        return dr.DeviceInfo(
            identifiers=identifiers,
            manufacturer="DOOODS",
            via_device_id=coordinator.parent_device_id,
            **_translated_device_name(
                dr.DeviceInfo,
                fallback_name=name,
                translation_key="camera_child",
                translation_placeholders={"camera_name": name},
            ),
        )

    return dr.DeviceInfo(
        identifiers=identifiers,
        manufacturer="DOOODS",
        via_device=(DOMAIN, coordinator.parent_device_identifier),
        **_translated_device_name(
            dr.DeviceInfo,
            fallback_name=name,
            translation_key="camera_child",
            translation_placeholders={"camera_name": name},
        ),
    )


def camera_name(hass: HomeAssistant, camera_entity_id: str) -> str:
    """Вернуть отображаемое имя исходной камеры."""
    state = hass.states.get(camera_entity_id)
    if state is not None:
        friendly_name = state.attributes.get("friendly_name")
        if isinstance(friendly_name, str) and friendly_name.strip():
            return friendly_name.strip()
    return camera_entity_id
