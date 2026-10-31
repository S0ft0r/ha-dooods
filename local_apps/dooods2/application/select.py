"""Выбор источника движения для каждой камеры DOOODS."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, override

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_CAMERA_STREAM_ENABLED,
    CONF_MOTION_SENSORS,
    MOTION_SENSOR_NONE,
    MOTION_SOURCE_RTSP,
)
from .device import camera_device_info
from .motion import all_binary_sensors, camera_motion_sensors, resolve_motion_sensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать селектор источника движения в устройстве каждой выбранной камеры."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DooodsMotionSourceSelect(hass, entry, coordinator, camera_entity_id)
            for camera_entity_id in coordinator.cameras
        ]
    )


class DooodsMotionSourceSelect(SelectEntity):
    """Выбрать единственный источник движения одной камеры."""

    _attr_has_entity_name = True
    _attr_translation_key = "motion_source"
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
        self._coordinator = coordinator
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
        """Показать источники: датчики камеры, RTSP и отсутствие триггера."""
        options = [MOTION_SENSOR_NONE, *self._option_to_entity_id()]
        if self._rtsp_stream_option_available:
            options.append(MOTION_SOURCE_RTSP)
        return options

    @property
    @override
    def current_option(self) -> str:
        """Вернуть выбранный источник движения для этой камеры."""
        explicit_stream_state = self._explicit_stream_state()
        if explicit_stream_state is True and self._rtsp_stream_option_available:
            return MOTION_SOURCE_RTSP
        if explicit_stream_state is True:
            explicit_stream_state = False

        configured_sensor = self._configured_sensor()
        sensor_entity_id = resolve_motion_sensor(
            self.hass,
            self._camera_entity_id,
            configured_sensor,
        )
        if explicit_stream_state is False:
            if sensor_entity_id is None:
                return MOTION_SENSOR_NONE
            return self._option_for_sensor(sensor_entity_id)

        if self._coordinator.camera_stream_processing_is_enabled(
            self._camera_entity_id
        ):
            return MOTION_SOURCE_RTSP
        if sensor_entity_id is None:
            return MOTION_SENSOR_NONE
        return self._option_for_sensor(sensor_entity_id)

    @override
    async def async_select_option(self, option: str) -> None:
        """Сохранить выбранный HA/RTSP-источник и перезагрузить запись."""
        options = dict(self._entry.options)
        stream_enabled: bool
        if option == MOTION_SOURCE_RTSP:
            if not self._rtsp_stream_option_available:
                raise ValueError("Сервер DOOODS2 не поддерживает RTSP motion stream")
            stream_enabled = True
        else:
            if option == MOTION_SENSOR_NONE:
                sensor_entity_id = MOTION_SENSOR_NONE
            else:
                sensor_entity_id = self._option_to_entity_id().get(option)
                if sensor_entity_id is None:
                    raise ValueError("Выбранный датчик движения больше недоступен")

            configured_sensors = options.get(CONF_MOTION_SENSORS, {})
            motion_sensors = (
                dict(configured_sensors)
                if isinstance(configured_sensors, Mapping)
                else {}
            )
            motion_sensors[self._camera_entity_id] = sensor_entity_id
            options[CONF_MOTION_SENSORS] = motion_sensors
            stream_enabled = False

        configured_streams = options.get(CONF_CAMERA_STREAM_ENABLED, {})
        stream_states = (
            dict(configured_streams)
            if isinstance(configured_streams, Mapping)
            else {}
        )
        stream_states[self._camera_entity_id] = stream_enabled
        options[CONF_CAMERA_STREAM_ENABLED] = stream_states

        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options,
        )
        await self.hass.config_entries.async_reload(self._entry.entry_id)

    @property
    def _rtsp_stream_option_available(self) -> bool:
        """Expose RTSP only when the connected DOOODS2 server supports it."""
        return bool(getattr(self._coordinator, "motion_stream_enabled", False))

    def _explicit_stream_state(self) -> bool | None:
        """Return a saved direct-source selection, if one exists."""
        configured_streams = self._entry.options.get(CONF_CAMERA_STREAM_ENABLED, {})
        if not isinstance(configured_streams, Mapping):
            return None
        value = configured_streams.get(self._camera_entity_id)
        return value if isinstance(value, bool) else None

    def _option_for_sensor(self, sensor_entity_id: str) -> str:
        """Return the selectable display name for one binary_sensor entity."""
        return next(
            (
                option
                for option, entity_id in self._option_to_entity_id().items()
                if entity_id == sensor_entity_id
            ),
            sensor_entity_id,
        )

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
                if counts[name] > 1
                or name in {MOTION_SENSOR_NONE, MOTION_SOURCE_RTSP}
                else name
            )
            if option in options:
                option = f"{name} ({entity_id})"
            options[option] = entity_id
        return options
