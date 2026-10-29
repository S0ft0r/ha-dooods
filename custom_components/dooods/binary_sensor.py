"""Бинарные сенсоры DOOODS для каждой пары «камера — метка»."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, override

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera_result import CameraScanResult
from .const import DOMAIN
from .coordinator import DooodsCoordinator
from .device import camera_device_info
from .label_icons import label_translation_key

_LOGGER = logging.getLogger(__name__)


def _label_entity_unique_id(entry_id: str, camera_entity_id: str, label: str) -> str:
    """Построить стабильный ID управляемого сенсора профильной метки."""
    return f"{entry_id}_{camera_entity_id}_{label}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Создать и обновлять сенсоры по текущим меткам назначенных профилей."""
    coordinator: DooodsCoordinator = entry.runtime_data
    manager = _DooodsLabelEntityManager(coordinator, async_add_entities)
    manager.async_sync_entities()
    entry.async_on_unload(
        coordinator.async_add_listener(manager.async_sync_entities)
    )
    entry.async_on_unload(manager.async_stop)


class _DooodsLabelEntityManager:
    """Сверять набор бинарных сенсоров с текущими метками профилей."""

    def __init__(
        self,
        coordinator: DooodsCoordinator,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        """Сохранить координатор, callback платформы и список активных сущностей."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._entities: dict[tuple[str, str], DooodsLabelBinarySensor] = {}
        self._removal_task: asyncio.Task[Any] | None = None
        self._stopped = False

    @callback
    def async_sync_entities(self) -> None:
        """Добавить новые сенсоры и запланировать удаление больше не нужных."""
        if self._stopped:
            return

        desired = self._desired_entity_keys()
        new_entities: list[DooodsLabelBinarySensor] = []
        for camera_entity_id, label in sorted(desired - self._entities.keys()):
            entity = DooodsLabelBinarySensor(
                self._coordinator,
                camera_entity_id,
                label,
            )
            self._entities[(camera_entity_id, label)] = entity
            new_entities.append(entity)
        if new_entities:
            self._async_add_entities(new_entities)

        if self._entities.keys() - desired or self._stale_registry_entities(desired):
            self._schedule_stale_entity_removal()

    def _desired_entity_keys(self) -> set[tuple[str, str]]:
        """Вернуть все актуальные пары «камера — метка»."""
        return {
            (camera_entity_id, label)
            for camera_entity_id in self._coordinator.cameras
            for label in self._coordinator.labels_for_camera(camera_entity_id)
        }

    def _desired_unique_ids(self, desired: set[tuple[str, str]]) -> set[str]:
        """Вернуть unique_id всех сущностей, которые нужны текущим профилям."""
        return {
            _label_entity_unique_id(
                self._coordinator.entry_id,
                camera_entity_id,
                label,
            )
            for camera_entity_id, label in desired
        }

    def _stale_registry_entities(
        self,
        desired: set[tuple[str, str]],
    ) -> dict[str, str]:
        """Найти сенсоры в реестре, которых больше нет в активном профиле."""
        registry = er.async_get(self._coordinator.hass)
        desired_unique_ids = self._desired_unique_ids(desired)
        integration_prefix = f"{self._coordinator.entry_id}_"
        return {
            registry_entry.unique_id: registry_entry.entity_id
            for registry_entry in er.async_entries_for_config_entry(
                registry,
                self._coordinator.entry_id,
            )
            if registry_entry.platform == DOMAIN
            and registry_entry.entity_id.startswith("binary_sensor.")
            and registry_entry.unique_id.startswith(integration_prefix)
            and registry_entry.unique_id not in desired_unique_ids
        }

    @callback
    def _schedule_stale_entity_removal(self) -> None:
        """Запустить одну последовательную задачу удаления устаревших сенсоров."""
        if self._removal_task is not None and not self._removal_task.done():
            return
        self._removal_task = (
            self._coordinator._config_entry.async_create_background_task(
                self._coordinator.hass,
                self._async_remove_stale_entities(),
                name=f"{self._coordinator.entry_id}_remove_stale_label_entities",
            )
        )

    async def _async_remove_stale_entities(self) -> None:
        """Удалить активные и сохранённые сенсоры, которых больше нет в профиле."""
        failed = False
        registry = er.async_get(self._coordinator.hass)
        try:
            while not self._stopped:
                desired = self._desired_entity_keys()
                stale_keys = self._entities.keys() - desired
                stale_registry_entities = self._stale_registry_entities(desired)
                tracked_by_unique_id = {
                    entity.unique_id: (key, entity)
                    for key, entity in self._entities.items()
                }
                stale_unique_ids = set(stale_registry_entities)
                stale_unique_ids.update(
                    entity.unique_id
                    for key, entity in self._entities.items()
                    if key in stale_keys
                )
                if not stale_unique_ids:
                    break

                for unique_id in sorted(stale_unique_ids):
                    desired_unique_ids = self._desired_unique_ids(
                        self._desired_entity_keys()
                    )
                    if unique_id in desired_unique_ids:
                        continue

                    tracked = tracked_by_unique_id.get(unique_id)
                    entity = tracked[1] if tracked is not None else None
                    entity_id = registry.async_get_entity_id(
                        "binary_sensor",
                        DOMAIN,
                        unique_id,
                    ) or stale_registry_entities.get(unique_id)
                    if entity_id is None and entity is not None:
                        entity_id = entity.entity_id

                    try:
                        if entity is not None and entity.hass is not None:
                            await entity.async_remove(force_remove=True)
                        elif entity_id is not None:
                            self._coordinator.hass.states.async_remove(entity_id)
                        if entity_id is not None:
                            registry.async_remove(entity_id)
                    except asyncio.CancelledError:
                        failed = True
                        raise
                    except Exception:
                        failed = True
                        _LOGGER.exception(
                            "Не удалось удалить бинарный сенсор DOOODS "
                            "с unique_id %s",
                            unique_id,
                        )
                        break

                    if tracked is not None:
                        self._entities.pop(tracked[0], None)
                if failed:
                    break
        finally:
            self._removal_task = None
            if not failed and not self._stopped:
                self.async_sync_entities()

    @callback
    def async_stop(self) -> None:
        """Остановить задачу синхронизации при выгрузке записи интеграции."""
        self._stopped = True
        if self._removal_task is not None and not self._removal_task.done():
            self._removal_task.cancel()


class DooodsLabelBinarySensor(CoordinatorEntity[DooodsCoordinator], BinarySensorEntity):
    """Показывать, была ли метка обнаружена при последнем успешном сканировании камеры."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DooodsCoordinator,
        camera_entity_id: str,
        label: str,
    ) -> None:
        """Инициализировать сенсор метки детектора для камеры."""
        super().__init__(coordinator)
        self._camera_entity_id = camera_entity_id
        self._label = label
        self._attr_translation_key = label_translation_key(label)
        if self._attr_translation_key == "object":
            self._attr_name = label
        self._attr_unique_id = _label_entity_unique_id(
            coordinator.entry_id,
            camera_entity_id,
            label,
        )
        self._attr_device_info = camera_device_info(
            coordinator.hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Считать доступными только сенсоры камеры, сканирование которой завершилось успешно."""
        result = self._camera_result
        return (
            super().available
            and self._label in self.coordinator.labels_for_camera(self._camera_entity_id)
            and result is not None
            and result.error is None
        )

    @property
    @override
    def is_on(self) -> bool | None:
        """Вернуть, присутствовала ли метка при последнем успешном сканировании."""
        result = self._camera_result
        if (
            self._label not in self.coordinator.labels_for_camera(self._camera_entity_id)
            or result is None
            or result.error is not None
        ):
            return None
        return result.counts.get(self._label, 0) > 0

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Предоставить идентификатор камеры и число объектов с максимальной уверенностью для последней успешной проверки."""
        attributes: dict[str, Any] = {"camera_entity_id": self._camera_entity_id}
        result = self._camera_result
        if (
            self._label in self.coordinator.labels_for_camera(self._camera_entity_id)
            and result is not None
            and result.error is None
        ):
            attributes["count"] = result.counts.get(self._label, 0)
            if (confidence := result.confidences.get(self._label)) is not None:
                attributes["confidence"] = confidence
        return attributes

    @property
    def _camera_result(self) -> CameraScanResult | None:
        """Вернуть последний результат сканирования этой камеры, если он есть."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._camera_entity_id)
