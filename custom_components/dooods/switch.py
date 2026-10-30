"""Переключатели распознавания и RTSP motion gate для каждой камеры."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .camera_switches import normalize_camera_switches
from .const import CONF_CAMERA_DETECTION_ENABLED, CONF_CAMERA_STREAM_ENABLED
from .device import camera_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать оба переключателя в настройках устройства каждой камеры."""
    coordinator = entry.runtime_data
    entities: list[DooodsCameraSwitch] = []
    for camera_entity_id in coordinator.cameras:
        entities.extend(
            (
                DooodsCameraSwitch(
                    hass,
                    entry,
                    coordinator,
                    camera_entity_id,
                    option_key=CONF_CAMERA_DETECTION_ENABLED,
                    switch_id="recognition",
                    translation_key="camera_recognition",
                    icon="mdi:eye",
                ),
                DooodsCameraSwitch(
                    hass,
                    entry,
                    coordinator,
                    camera_entity_id,
                    option_key=CONF_CAMERA_STREAM_ENABLED,
                    switch_id="stream_processing",
                    translation_key="camera_stream_processing",
                    icon="mdi:video-wireless",
                ),
            )
        )
    async_add_entities(entities)


class DooodsCameraSwitch(SwitchEntity):
    """Сохранить настройку одной камеры и применить её перезагрузкой записи."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: Any,
        camera_entity_id: str,
        *,
        option_key: str,
        switch_id: str,
        translation_key: str,
        icon: str,
    ) -> None:
        """Инициализировать переключатель как конфигурационную сущность камеры."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._camera_entity_id = camera_entity_id
        self._option_key = option_key
        self._switch_id = switch_id
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{camera_entity_id}_{switch_id}"
        self._attr_device_info = camera_device_info(
            hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def is_on(self) -> bool:
        """Вернуть сохранённое состояние или вычисленный per-camera default."""
        if self._option_key == CONF_CAMERA_STREAM_ENABLED:
            return self._coordinator.camera_stream_processing_is_enabled(
                self._camera_entity_id
            )
        return normalize_camera_switches(
            self._entry.options.get(self._option_key, {}),
            [self._camera_entity_id],
        )[self._camera_entity_id]

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Включить распознавание или обработку RTSP-потока только для этой камеры."""
        await self._async_set_enabled(True)

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Отключить соответствующую обработку только для этой камеры."""
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Сохранить переключатель и сразу применить его к координатору."""
        if self.is_on is enabled:
            return
        options = dict(self._entry.options)
        switches = normalize_camera_switches(
            options.get(self._option_key, {}),
            self._coordinator.cameras,
            default=None,
        )
        switches[self._camera_entity_id] = enabled
        options[self._option_key] = switches
        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options,
        )
        self._coordinator.set_camera_switch_state(
            self._camera_entity_id,
            self._option_key,
            enabled,
        )
        self.async_write_ha_state()
