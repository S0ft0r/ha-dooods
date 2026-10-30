"""Индивидуальные интервалы периодического сканирования камер DOOODS."""

from __future__ import annotations

import math
from typing import Any, override

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .camera_scan_intervals import (
    camera_scan_interval,
    normalize_camera_scan_intervals,
)
from .const import (
    CONF_CAMERA_SCAN_INTERVALS,
    CONF_SCAN_INTERVAL,
    MAX_CAMERA_SCAN_INTERVAL,
    MIN_CAMERA_SCAN_INTERVAL,
)
from .device import camera_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать настройку интервала в устройстве каждой выбранной камеры."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            DooodsCameraScanIntervalNumber(
                hass,
                entry,
                coordinator,
                camera_entity_id,
            )
            for camera_entity_id in coordinator.cameras
        ]
    )


class DooodsCameraScanIntervalNumber(NumberEntity):
    """Редактировать интервал резервного сканирования одной камеры."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera_scan_interval"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:timer-cog-outline"
    _attr_native_min_value = MIN_CAMERA_SCAN_INTERVAL
    _attr_native_max_value = MAX_CAMERA_SCAN_INTERVAL
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: Any,
        camera_entity_id: str,
    ) -> None:
        """Связать настройку с отдельным устройством-камерой."""
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{entry.entry_id}_{camera_entity_id}_scan_interval"
        )
        self._attr_device_info = camera_device_info(
            hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def native_value(self) -> float:
        """Вернуть сохранённый интервал или 30 секунд для новой камеры."""
        intervals = normalize_camera_scan_intervals(
            self._entry.options.get(CONF_CAMERA_SCAN_INTERVALS, {}),
            self._coordinator.cameras,
        )
        return float(camera_scan_interval(self._camera_entity_id, intervals))

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Сохранить целое число секунд только для этой камеры и перезагрузить запись."""
        if isinstance(value, bool):
            raise ValueError("Интервал сканирования должен быть целым числом секунд")
        try:
            numeric_value = float(value)
        except (TypeError, ValueError, OverflowError) as err:
            raise ValueError(
                "Интервал сканирования должен быть целым числом секунд"
            ) from err
        if not math.isfinite(numeric_value) or not numeric_value.is_integer():
            raise ValueError("Интервал сканирования должен быть целым числом секунд")

        interval = int(numeric_value)
        if not MIN_CAMERA_SCAN_INTERVAL <= interval <= MAX_CAMERA_SCAN_INTERVAL:
            raise ValueError(
                f"Интервал должен быть от {MIN_CAMERA_SCAN_INTERVAL} "
                f"до {MAX_CAMERA_SCAN_INTERVAL} секунд"
            )
        if self._camera_entity_id not in self._coordinator.cameras:
            raise ValueError("Камера больше не выбрана в DOOODS")

        options = dict(self._entry.options)
        intervals = normalize_camera_scan_intervals(
            options.get(CONF_CAMERA_SCAN_INTERVALS, {}),
            self._coordinator.cameras,
        )
        intervals[self._camera_entity_id] = interval
        options.pop(CONF_SCAN_INTERVAL, None)
        options[CONF_CAMERA_SCAN_INTERVALS] = intervals

        self.hass.config_entries.async_update_entry(
            self._entry,
            options=options,
        )
        await self.hass.config_entries.async_reload(self._entry.entry_id)
