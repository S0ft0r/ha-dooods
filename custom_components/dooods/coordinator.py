"""Координатор периодического и запускаемого движением сканирования камер через DOOODS2."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, override

from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    DooodsApiError,
    DooodsDetectionError,
    async_detect,
    async_get_profiles,
    async_put_preview,
)
from .archive import (
    async_prune_archive,
    async_save_annotated_frame,
    has_accepted_annotated_frame,
    normalize_archive_directory,
)
from .camera_result import CameraScanResult, retain_last_accepted_image
from .const import (
    CONF_BOTTOM,
    CONF_COVERS,
    CONF_LEFT,
    CONF_RIGHT,
    CONF_TOP,
    DOMAIN,
)
from .preview import (
    PreviewUploadLimiter,
    is_preview_jpeg,
    preview_source_id,
    preview_source_name,
)
from .profiles import profile_labels_for_detector

_LOGGER = logging.getLogger(__name__)
_INITIAL_CAMERA_RETRY_DELAY = 5
_MAX_CAMERA_RETRY_DELAY = 300
_PROFILE_LABEL_REFRESH_INTERVAL = timedelta(minutes=1)


class DooodsCoordinator(DataUpdateCoordinator[dict[str, CameraScanResult]]):
    """Сканировать выбранные камеры по расписанию и при событии связанного датчика движения."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        *,
        base_url: str,
        auth_key: str,
        detector_name: str,
        cameras: list[str],
        labels: list[str],
        confidence: float,
        area: Mapping[str, Any],
        timeout: int,
        scan_interval: int,
        parent_device_id: str,
        parent_device_identifier: str,
        preview_enabled: bool = False,
        annotation_localization_enabled: bool = False,
        detector_labels: list[str] | None = None,
        camera_profiles: Mapping[str, str] | None = None,
        camera_labels: Mapping[str, list[str]] | None = None,
        archive_directory: str = "",
        archive_max_count: int = 10,
        archive_max_age_days: int = 0,
    ) -> None:
        """Инициализировать координатор и неизменяемые параметры сканирования."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=1,
                immediate=True,
            ),
        )
        self.base_url = base_url
        self.auth_key = auth_key
        self.detector_name = detector_name
        self.cameras = cameras
        self.labels = labels
        self.detector_labels = list(
            detector_labels if detector_labels is not None else labels
        )
        self.annotation_localization_enabled = annotation_localization_enabled
        self.camera_labels = {
            camera_entity_id: list(camera_label_values)
            for camera_entity_id, camera_label_values in (camera_labels or {}).items()
        }
        self.camera_profiles = dict(camera_profiles or {})
        self.confidence = confidence
        self.area = dict(area)
        self.timeout = timeout
        self.scan_interval = scan_interval
        self.parent_device_id = parent_device_id
        self.parent_device_identifier = parent_device_identifier
        self.preview_enabled = preview_enabled and bool(auth_key)
        self.entry_id = config_entry.entry_id
        self._config_entry = config_entry
        self._preview_upload_limiter = PreviewUploadLimiter()
        self.archive_directory = normalize_archive_directory(archive_directory)
        self.archive_max_count = max(0, archive_max_count)
        self.archive_max_age_days = max(0, archive_max_age_days)
        self._archive_cleanup_unsubscribe: Callable[[], None] | None = None
        self._archive_cleanup_task: asyncio.Task[Any] | None = None
        self._profile_labels_unsubscribe: Callable[[], None] | None = None
        self._profile_labels_task: asyncio.Task[Any] | None = None
        self._pending_camera_scans: set[str] = set()
        self._force_full_scan = False
        self._active_full_scan = False
        self._cancel_periodic_scan: Callable[[], None] | None = None
        self._camera_retry_unsubscribers: dict[str, Callable[[], None]] = {}
        self._camera_retry_delays: dict[str, int] = {}
        self._initial_scan_task: asyncio.Task[Any] | None = None
        self._is_shutting_down = False

    @callback
    def set_initial_scan_task(self, task: asyncio.Task[Any]) -> None:
        """Сохранить задачу первого сканирования для явной отмены при выгрузке."""
        if self._is_shutting_down:
            task.cancel()
            return
        if self._initial_scan_task is not None and not self._initial_scan_task.done():
            self._initial_scan_task.cancel()
        if task.done():
            self._initial_scan_task = None
            return
        self._initial_scan_task = task
        task.add_done_callback(self._clear_initial_scan_task)

    @callback
    def _clear_initial_scan_task(self, task: asyncio.Task[Any]) -> None:
        """Удалить завершённую задачу первого сканирования из координатора."""
        if self._initial_scan_task is task:
            self._initial_scan_task = None

    async def _cancel_initial_scan_task(self) -> None:
        """Отменить и дождаться активного первого сканирования при выгрузке."""
        task = self._initial_scan_task
        self._initial_scan_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def labels_for_camera(self, camera_entity_id: str) -> list[str]:
        """Вернуть метки профиля камеры или глобальный набор legacy-режима."""
        if camera_entity_id in self.camera_labels:
            return self.camera_labels[camera_entity_id]
        return self.labels

    @callback
    def start_periodic_scanning(self) -> None:
        """Запустить опрос камер и независимую очистку архива по сроку хранения."""
        if self._is_shutting_down:
            return
        if self.archive_directory:
            self._schedule_archive_cleanup()
            if (
                self.archive_max_age_days > 0
                and self._archive_cleanup_unsubscribe is None
            ):
                self._archive_cleanup_unsubscribe = async_track_time_interval(
                    self.hass,
                    self._async_handle_archive_cleanup,
                    timedelta(days=1),
                )
        if self.camera_profiles and self._profile_labels_unsubscribe is None:
            self._profile_labels_unsubscribe = async_track_time_interval(
                self.hass,
                self._async_handle_profile_labels_refresh,
                _PROFILE_LABEL_REFRESH_INTERVAL,
            )
        if self._cancel_periodic_scan is not None:
            return
        if self._config_entry.pref_disable_polling:
            _LOGGER.debug(
                "Периодическое сканирование DOOODS отключено: %s", self.entry_id
            )
            return
        self._cancel_periodic_scan = async_track_time_interval(
            self.hass,
            self._async_handle_periodic_scan,
            timedelta(seconds=self.scan_interval),
        )

    @callback
    def _schedule_archive_cleanup(self) -> None:
        """Запустить очистку архива, не создавая параллельные задания."""
        if (
            self._is_shutting_down
            or not self.archive_directory
            or (
                self._archive_cleanup_task is not None
                and not self._archive_cleanup_task.done()
            )
        ):
            return
        task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_cleanup_archive(),
            name=f"{DOMAIN}_archive_cleanup",
        )
        self._archive_cleanup_task = task
        task.add_done_callback(self._clear_archive_cleanup_task)

    @callback
    def _clear_archive_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        """Освободить ссылку на завершившуюся задачу очистки архива."""
        if self._archive_cleanup_task is task:
            self._archive_cleanup_task = None

    @callback
    def _async_handle_archive_cleanup(self, _now: datetime) -> None:
        """Запланировать ежедневное удаление файлов, превысивших срок хранения."""
        self._schedule_archive_cleanup()

    async def _async_cleanup_archive(self) -> None:
        """Применить лимиты хранения к существующим файлам всех камер записи."""
        try:
            await async_prune_archive(
                self.hass,
                self.archive_directory,
                self.entry_id,
                max_count=self.archive_max_count,
                max_age_days=self.archive_max_age_days,
            )
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Не удалось выполнить очистку архива DOOODS: %s",
                err,
            )

    @callback
    def _async_handle_profile_labels_refresh(self, _now: datetime) -> None:
        """Запланировать фоновую сверку меток назначенных профилей."""
        self._schedule_profile_labels_refresh()

    @callback
    def _schedule_profile_labels_refresh(self) -> None:
        """Не допускать параллельных запросов списка профилей."""
        if (
            self._is_shutting_down
            or not self.camera_profiles
            or (
                self._profile_labels_task is not None
                and not self._profile_labels_task.done()
            )
        ):
            return
        task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_profile_labels(),
            name=f"{DOMAIN}_profile_label_refresh",
        )
        self._profile_labels_task = task
        task.add_done_callback(self._clear_profile_labels_task)

    @callback
    def _clear_profile_labels_task(self, task: asyncio.Task[Any]) -> None:
        """Освободить ссылку на завершившуюся сверку меток профилей."""
        if self._profile_labels_task is task:
            self._profile_labels_task = None

    async def _async_refresh_profile_labels(self) -> None:
        """Обновить выбранные метки и сенсоры без перезагрузки записи интеграции."""
        try:
            profiles = await async_get_profiles(
                self.hass,
                self.base_url,
                self.auth_key,
            )
        except DooodsApiError as err:
            _LOGGER.debug(
                "Не удалось обновить метки профилей DOOODS; оставлен последний список: %s",
                err,
            )
            return

        profiles_by_id = {profile["id"]: profile for profile in profiles}
        changed_cameras: list[str] = []
        for camera_entity_id, profile_id in self.camera_profiles.items():
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                # Не удалять сенсоры из-за кратковременной ошибки или удалённого профиля.
                continue
            current_labels = self.camera_labels.get(camera_entity_id, [])
            updated_labels = profile_labels_for_detector(
                profile,
                self.detector_labels,
            )
            if current_labels != updated_labels:
                self.camera_labels[camera_entity_id] = updated_labels
                changed_cameras.append(camera_entity_id)

        if changed_cameras:
            _LOGGER.info(
                "Обновлены метки профилей DOOODS для камер: %s",
                ", ".join(changed_cameras),
            )
            self.async_update_listeners()
            self.request_camera_scans(changed_cameras)

    @callback
    def _schedule_camera_retry(self, camera_entity_id: str) -> None:
        """Запланировать повтор при временной недоступности камеры или DOODS2."""
        if (
            self._is_shutting_down
            or camera_entity_id not in self.cameras
            or camera_entity_id in self._camera_retry_unsubscribers
        ):
            return

        delay = self._camera_retry_delays.get(
            camera_entity_id,
            _INITIAL_CAMERA_RETRY_DELAY,
        )
        self._camera_retry_delays[camera_entity_id] = min(
            delay * 2,
            _MAX_CAMERA_RETRY_DELAY,
        )

        @callback
        def _retry_scan(_now: datetime) -> None:
            self._camera_retry_unsubscribers.pop(camera_entity_id, None)
            if not self._is_shutting_down:
                self.request_camera_scans([camera_entity_id])

        _LOGGER.debug(
            "Повторное сканирование камеры %s через %s секунд",
            camera_entity_id,
            delay,
        )
        self._camera_retry_unsubscribers[camera_entity_id] = async_call_later(
            self.hass,
            delay,
            _retry_scan,
        )

    @callback
    def _clear_camera_retry(self, camera_entity_id: str) -> None:
        """Отменить повтор камеры после успешного сканирования и сбросить задержку."""
        if cancel_retry := self._camera_retry_unsubscribers.pop(
            camera_entity_id,
            None,
        ):
            cancel_retry()
        self._camera_retry_delays.pop(camera_entity_id, None)

    @callback
    def _async_handle_periodic_scan(self, now: datetime) -> None:
        """Добавить все камеры в очередь по истечении интервала резервного сканирования."""
        if self._active_full_scan:
            return
        self.request_camera_scans(self.cameras, full_scan=True)

    @callback
    def request_camera_scans(
        self,
        camera_entity_ids: Iterable[str],
        *,
        full_scan: bool = False,
    ) -> None:
        """Добавить выбранные камеры в очередь и поручить координатору объединить запросы обновления."""
        camera_ids = set(camera_entity_ids).intersection(self.cameras)
        if full_scan:
            camera_ids = set(self.cameras)
            self._force_full_scan = True
        if not camera_ids:
            return
        self._pending_camera_scans.update(camera_ids)
        self._config_entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            name=f"{DOMAIN}_camera_scan_request",
        )

    @override
    async def async_shutdown(self) -> None:
        """Остановить таймеры и завершить начальный скан при выгрузке записи."""
        self._is_shutting_down = True
        if self._cancel_periodic_scan is not None:
            self._cancel_periodic_scan()
            self._cancel_periodic_scan = None
        if self._archive_cleanup_unsubscribe is not None:
            self._archive_cleanup_unsubscribe()
            self._archive_cleanup_unsubscribe = None
        if self._profile_labels_unsubscribe is not None:
            self._profile_labels_unsubscribe()
            self._profile_labels_unsubscribe = None
        profile_labels_task = self._profile_labels_task
        self._profile_labels_task = None
        if profile_labels_task is not None and not profile_labels_task.done():
            profile_labels_task.cancel()
            await asyncio.gather(profile_labels_task, return_exceptions=True)
        archive_cleanup_task = self._archive_cleanup_task
        self._archive_cleanup_task = None
        if archive_cleanup_task is not None and not archive_cleanup_task.done():
            archive_cleanup_task.cancel()
            await asyncio.gather(archive_cleanup_task, return_exceptions=True)
        for cancel_retry in self._camera_retry_unsubscribers.values():
            cancel_retry()
        self._camera_retry_unsubscribers.clear()
        self._camera_retry_delays.clear()
        await self._cancel_initial_scan_task()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, CameraScanResult]:
        """Сканировать только запрошенные камеры, сохраняя последние результаты для остальных."""
        self._active_full_scan = self.data is None or self._force_full_scan
        try:
            if self._active_full_scan:
                camera_ids = set(self.cameras)
            else:
                camera_ids = self._pending_camera_scans.copy()
            self._pending_camera_scans.clear()
            self._force_full_scan = False
            if not camera_ids:
                return self.data or {}

            results = dict(self.data or {})
            for camera_entity_id in self.cameras:
                if camera_entity_id in camera_ids:
                    previous_result = results.get(camera_entity_id)
                    scan_result = await self._async_scan_camera(camera_entity_id)
                    if scan_result.error is None:
                        self._clear_camera_retry(camera_entity_id)
                        if has_accepted_annotated_frame(
                            scan_result.detections,
                            scan_result.annotated_image,
                            scan_result.checked_at,
                        ):
                            await self._async_archive_camera_frame(
                                camera_entity_id,
                                scan_result.annotated_image,
                                scan_result.checked_at,
                            )
                    else:
                        self._schedule_camera_retry(camera_entity_id)
                    results[camera_entity_id] = retain_last_accepted_image(
                        previous_result,
                        scan_result,
                    )
            return results
        finally:
            self._active_full_scan = False

    @callback
    @override
    def _async_refresh_finished(self) -> None:
        """Запланировать повторное сканирование, если движение обнаружено во время текущей проверки."""
        if not self._pending_camera_scans and not self._force_full_scan:
            return
        self._config_entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            name=f"{DOMAIN}_coalesced_camera_scan",
        )

    async def _async_archive_camera_frame(
        self,
        camera_entity_id: str,
        image: bytes,
        checked_at: datetime,
    ) -> None:
        """Сохранить JPEG только при детекциях, прошедших активные фильтры камеры."""
        if not self.archive_directory:
            return
        try:
            await async_save_annotated_frame(
                self.hass,
                self.archive_directory,
                self.entry_id,
                camera_entity_id,
                image,
                checked_at,
                max_count=self.archive_max_count,
                max_age_days=self.archive_max_age_days,
            )
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Не удалось сохранить принятый кадр камеры %s в архив DOOODS: %s",
                camera_entity_id,
                err,
            )

    async def _async_scan_camera(self, camera_entity_id: str) -> CameraScanResult:
        """Получить снимок и распознать объекты одной камеры, не влияя на обработку остальных камер при ошибке."""
        try:
            image = await async_get_image(
                self.hass,
                camera_entity_id,
                timeout=self.timeout,
            )
            if image is None or not image.content:
                raise DooodsDetectionError(
                    "camera_no_image",
                    {"camera_entity_id": camera_entity_id},
                )
            if self.preview_enabled and is_preview_jpeg(image.content):
                self._schedule_preview_upload(camera_entity_id, image.content)
            profile_id = self.camera_profiles.get(camera_entity_id)
            detection_result = await async_detect(
                self.hass,
                self.base_url,
                self.auth_key,
                detector_name=self.detector_name,
                image=image.content,
                labels=self.labels,
                confidence=self.confidence,
                area=self.area,
                timeout=self.timeout,
                profile_id=profile_id,
                annotation_locale=(
                    getattr(getattr(self.hass, "config", None), "language", None)
                    if self.annotation_localization_enabled
                    else None
                ),
            )
            if detection_result.annotated_image is None:
                _LOGGER.debug(
                    "DOODS2 вернул обнаружения без размеченного изображения: %s",
                    camera_entity_id,
                )
            return self._summarize(
                detection_result.detections,
                detection_result.annotated_image,
                profile_active=profile_id is not None,
            )
        except (HomeAssistantError, TimeoutError, OSError) as err:
            _LOGGER.warning(
                "Не удалось просканировать камеру %s через DOOODS2: %s",
                camera_entity_id,
                err,
            )
            return CameraScanResult(
                counts={},
                confidences={},
                detections=(),
                annotated_image=None,
                checked_at=None,
                error=str(err),
            )

    def _schedule_preview_upload(
        self,
        camera_entity_id: str,
        image: bytes,
    ) -> None:
        """Передать исходный JPEG отдельно от результата распознавания и без ожидания API."""
        if not self.preview_enabled or not self._preview_upload_limiter.should_upload(
            camera_entity_id
        ):
            return

        source_id = preview_source_id(self.entry_id, camera_entity_id)
        camera_state = self.hass.states.get(camera_entity_id)
        friendly_name = (
            camera_state.attributes.get("friendly_name")
            if camera_state is not None
            else None
        )
        source_name = preview_source_name(
            friendly_name,
            camera_entity_id,
            source_id,
        )
        self._config_entry.async_create_background_task(
            self.hass,
            self._async_upload_preview(source_id, source_name, image),
            name=f"{DOMAIN}_preview_upload_{source_id[-8:]}",
        )

    async def _async_upload_preview(
        self,
        source_id: str,
        source_name: str,
        image: bytes,
    ) -> None:
        """Отправить кадр в Overhaul; ошибка предпросмотра не меняет результат сканирования."""
        try:
            await async_put_preview(
                self.hass,
                self.base_url,
                self.auth_key,
                source_id=source_id,
                source_name=source_name,
                image=image,
                timeout=5,
            )
        except DooodsApiError as err:
            _LOGGER.debug(
                "Не удалось передать временный кадр в DOOODS2 Overhaul: %s", err
            )

    def _summarize(
        self,
        detections: list[dict[str, Any]],
        annotated_image: bytes | None,
        *,
        profile_active: bool = False,
    ) -> CameraScanResult:
        """Подсчитать метки и рамки, прошедшие профильный либо legacy-фильтр."""
        counts: Counter[str] = Counter()
        confidences: dict[str, float] = {}
        accepted_detections: list[dict[str, Any]] = []
        for detection in detections:
            label = detection["label"]
            confidence = float(detection["confidence"])
            if not profile_active:
                if self.labels and label not in self.labels:
                    continue
                if confidence < self.confidence:
                    continue
                if not self._is_in_area(detection):
                    continue

            counts[label] += 1
            confidences[label] = max(
                confidences.get(label, float("-inf")),
                confidence,
            )
            accepted_detection = {
                "label": label,
                "confidence": confidence,
                "bounding_box": {
                    "top": float(detection["top"]),
                    "left": float(detection["left"]),
                    "bottom": float(detection["bottom"]),
                    "right": float(detection["right"]),
                },
            }
            if region_id := detection.get("region_id"):
                accepted_detection["region_id"] = region_id
            accepted_detections.append(accepted_detection)

        return CameraScanResult(
            counts=dict(counts),
            confidences=confidences,
            detections=tuple(accepted_detections),
            annotated_image=annotated_image,
            checked_at=datetime.now(UTC),
        )

    def _is_in_area(self, detection: Mapping[str, Any]) -> bool:
        """Дополнительно локально проверить обнаружения по заданной общей области."""
        top = float(self.area.get(CONF_TOP, 0.0))
        left = float(self.area.get(CONF_LEFT, 0.0))
        bottom = float(self.area.get(CONF_BOTTOM, 1.0))
        right = float(self.area.get(CONF_RIGHT, 1.0))
        detection_top = detection["top"]
        detection_left = detection["left"]
        detection_bottom = detection["bottom"]
        detection_right = detection["right"]

        if self.area.get(CONF_COVERS, True):
            return (
                top <= detection_top
                and left <= detection_left
                and bottom >= detection_bottom
                and right >= detection_right
            )
        return (
            detection_top <= bottom
            and detection_left <= right
            and detection_bottom >= top
            and detection_right >= left
        )
