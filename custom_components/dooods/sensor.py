"""Сводные сенсоры каждой камеры с рамками объектов в атрибутах."""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera_result import CameraScanResult
from .coordinator import DooodsCoordinator
from .device import camera_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать сводный сенсор распознавания для каждой выбранной камеры."""
    coordinator: DooodsCoordinator = entry.runtime_data
    async_add_entities(
        [
            DooodsCameraSensor(coordinator, camera_entity_id)
            for camera_entity_id in coordinator.cameras
        ]
    )


class DooodsCameraSensor(CoordinatorEntity[DooodsCoordinator], SensorEntity):
    """Предоставить число обнаруженных объектов и нормализованные рамки для камеры."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera_detections"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Инициализировать сводный сенсор камеры."""
        super().__init__(coordinator)
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{camera_entity_id}_camera_detections"
        )
        self._attr_device_info = camera_device_info(
            coordinator.hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Ожидать успешного сканирования этой камеры."""
        result = self._camera_result
        return super().available and result is not None and result.error is None

    @property
    @override
    def native_value(self) -> int | None:
        """Вернуть число выбранных объектов, обнаруженных при последнем успешном сканировании."""
        result = self._camera_result
        if result is None or result.error is not None:
            return None
        return len(result.detections)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Предоставить число объектов по меткам, максимальные значения уверенности и нормализованные рамки."""
        attributes: dict[str, Any] = {
            "camera_entity_id": self._camera_entity_id,
            "selected_labels": list(
                self.coordinator.labels_for_camera(self._camera_entity_id)
            ),
        }
        result = self._camera_result
        if result is not None and result.error is None:
            attributes.update(
                {
                    "counts_by_label": result.counts,
                    "max_confidence_by_label": result.confidences,
                    "detections": list(result.detections),
                    "last_checked": (
                        result.checked_at.isoformat() if result.checked_at else None
                    ),
                }
            )
        return attributes

    @property
    def _camera_result(self) -> CameraScanResult | None:
        """Вернуть последний результат сканирования этой камеры, если он есть."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._camera_entity_id)
