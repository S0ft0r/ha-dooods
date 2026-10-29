"""Настройки датчика движения для каждой камеры DOOODS."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, override

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_MOTION_SENSORS, MOTION_SENSOR_NONE
from .device import camera_device_info
from .motion import all_binary_sensors, camera_motion_sensors, resolve_motion_sensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать сущность настройки датчика в устройстве каждой выбранной камеры."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DooodsMotionSensorSelect(hass, entry, coordinator, camera_entity_id)
            for camera_entity_id in coordinator.cameras
        ]
    )


class DooodsMotionSensorSelect(SelectEntity):
    """Выбрать binary_sensor, запускающий сканирование одной камеры."""

    _attr_has_entity_name = True
    _attr_translation_key = "motion_sensor"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:motion-sensor"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: Any,
        camera_entity_id: str,
    ) -> None:
        """Инициализировать настройку, связанную с дочерним устройством камеры."""
        self.hass = hass
        self._entry = entry
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{entry.entry_id}_{camera_entity_id}_motion_sensor"
        )
        self._attr_device_info = camera_device_info(
            hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def options(self) -> list[str]:
        """Показать подходящие сенсоры и вариант без триггера движения."""
        return [MOTION_SENSOR_NONE, *self._option_to_entity_id()]

    @property
    @override
    def current_option(self) -> str:
        """Вернуть выбранный сенсор или состояние «не использовать»."""
        configured_sensor = self._configured_sensor()
        if configured_sensor == MOTION_SENSOR_NONE:
            return MOTION_SENSOR_NONE

        sensor_entity_id = resolve_motion_sensor(
            self.hass,
            self._camera_entity_id,
            configured_sensor,
        )
        if sensor_entity_id is None:
            return MOTION_SENSOR_NONE

        return next(
            (
                option
                for option, entity_id in self._option_to_entity_id().items()
                if entity_id == sensor_entity_id
            ),
            sensor_entity_id,
        )

    @override
    async def async_select_option(self, option: str) -> None:
        """Сохранить привязку камеры и перезагрузить запись для применения."""
        if option == MOTION_SENSOR_NONE:
            sensor_entity_id = MOTION_SENSOR_NONE
        else:
            sensor_entity_id = self._option_to_entity_id().get(option)
            if sensor_entity_id is None:
                raise ValueError("Выбранный датчик движения больше недоступен")

        options = dict(self._entry.options)
        configured_sensors = options.get(CONF_MOTION_SENSORS, {})
        motion_sensors = (
            dict(configured_sensors)
            if isinstance(configured_sensors, Mapping)
            else {}
        )
        motion_sensors[self._camera_entity_id] = sensor_entity_id
        options[CONF_MOTION_SENSORS] = motion_sensors

        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options,
        )
        await self.hass.config_entries.async_reload(self._entry.entry_id)

    def _configured_sensor(self) -> str | None:
        """Получить сохранённый выбор этой камеры."""
        configured_sensors = self._entry.options.get(CONF_MOTION_SENSORS, {})
        if not isinstance(configured_sensors, Mapping):
            return None
        value = configured_sensors.get(self._camera_entity_id)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _option_to_entity_id(self) -> dict[str, str]:
        """Сопоставить отображаемые имена вариантов с entity_id сенсоров."""
        candidates = camera_motion_sensors(self.hass, self._camera_entity_id)
        if not candidates:
            candidates = all_binary_sensors(self.hass)

        configured_sensor = self._configured_sensor()
        if configured_sensor and configured_sensor != MOTION_SENSOR_NONE:
            current_sensor = resolve_motion_sensor(
                self.hass,
                self._camera_entity_id,
                configured_sensor,
            )
            candidate_ids = {candidate["entity_id"] for candidate in candidates}
            if current_sensor and current_sensor not in candidate_ids:
                state = self.hass.states.get(current_sensor)
                attributes = getattr(state, "attributes", {})
                friendly_name = (
                    attributes.get("friendly_name")
                    if isinstance(attributes, Mapping)
                    else None
                )
                candidates.append(
                    {
                        "entity_id": current_sensor,
                        "name": str(friendly_name or current_sensor),
                    }
                )

        counts = Counter(candidate["name"].strip() for candidate in candidates)
        options: dict[str, str] = {}
        for candidate in candidates:
            entity_id = candidate["entity_id"]
            name = candidate["name"].strip() or entity_id
            option = (
                f"{name} ({entity_id})"
                if counts[name] > 1 or name == MOTION_SENSOR_NONE
                else name
            )
            if option in options:
                option = f"{name} ({entity_id})"
            options[option] = entity_id
        return options
