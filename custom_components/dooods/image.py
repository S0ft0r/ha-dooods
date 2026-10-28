"""Изображения для каждой камеры с последними рамками, размеченными DOOODS2."""

from __future__ import annotations

from datetime import datetime
from typing import override

from homeassistant.components.image import ImageEntity
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
    """Создать сущность последнего размеченного изображения для каждой выбранной камеры."""
    coordinator: DooodsCoordinator = entry.runtime_data
    async_add_entities(
        [
            DooodsAnnotatedImage(coordinator, camera_entity_id)
            for camera_entity_id in coordinator.cameras
        ]
    )


class DooodsAnnotatedImage(CoordinatorEntity[DooodsCoordinator], ImageEntity):
    """Предоставить последний кадр камеры с рамками выбранных объектов."""

    _attr_has_entity_name = True
    _attr_translation_key = "annotated_detections"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Инициализировать сущность размеченного изображения камеры."""
        super().__init__(coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{camera_entity_id}_annotated_detections"
        )
        self._attr_device_info = camera_device_info(
            coordinator.hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Сделать сущность доступной только после получения размеченного кадра этой камеры."""
        result = self._camera_result
        return (
            super().available
            and result is not None
            and result.error is None
            and result.annotated_image is not None
            and result.image_checked_at is not None
        )

    @property
    @override
    def image_last_updated(self) -> datetime | None:
        """Вернуть время кадра с последней детекцией, прошедшей фильтры."""
        result = self._camera_result
        if result is None or result.error is not None:
            return None
        return result.image_checked_at

    @override
    async def async_image(self) -> bytes | None:
        """Вернуть последний размеченный JPEG, в котором есть принятая детекция."""
        result = self._camera_result
        if result is None or result.error is not None:
            return None
        return result.annotated_image

    @property
    def _camera_result(self) -> CameraScanResult | None:
        """Вернуть последний результат сканирования этой камеры, если он есть."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._camera_entity_id)
