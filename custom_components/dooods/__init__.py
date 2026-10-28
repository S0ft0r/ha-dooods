"""Настроить опрос DOOODS2 и сущности для каждой камеры."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType

from .api import (
    DooodsApiError,
    DooodsAuthError,
    DooodsConnectionError,
    DooodsInvalidResponseError,
    async_get_capabilities,
    async_get_detectors,
    async_get_profiles,
)
from .archive import normalize_archive_directory, normalize_archive_limit
from .const import (
    CONF_AREA,
    CONF_ARCHIVE_MAX_AGE_DAYS,
    CONF_ARCHIVE_MAX_COUNT,
    CONF_AUTH_KEY,
    CONF_CAMERA_PROFILES,
    CONF_CONFIDENCE,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_MOTION_SENSORS,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_TIMEOUT,
    CONF_URL,
    DEFAULT_ARCHIVE_MAX_AGE_DAYS,
    DEFAULT_ARCHIVE_MAX_COUNT,
    DEFAULT_AREA,
    DEFAULT_CONFIDENCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .coordinator import DooodsCoordinator
from .motion import resolve_motion_sensors
from .profiles import normalize_camera_profiles, profile_labels_for_detector
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
_PROFILE_FEATURES = {"profiles", "polygon_zones"}
PLATFORMS = (Platform.BINARY_SENSOR, Platform.SENSOR, Platform.IMAGE)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


type DooodsConfigEntry = ConfigEntry[DooodsCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Зарегистрировать действия интеграции до загрузки её записей."""
    async_register_services(hass)
    return True


def _label_list(value: Any) -> list[str]:
    """Привести метки из текущих параметров или старых сохранённых текстовых значений к единому виду."""
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, list):
        values = value
    else:
        return []
    return list(
        dict.fromkeys(
            item.strip() for item in values if isinstance(item, str) and item.strip()
        )
    )


def _register_server_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    base_url: str,
) -> tuple[str, str]:
    """Зарегистрировать виртуальное устройство-службу DOOODS2 и вернуть его идентификатор и ключ."""
    identifier = f"server:{entry.entry_id}"
    device_info_fields = getattr(DeviceInfo, "__annotations__", {})
    device_info_values: dict[str, Any] = {
        "identifiers": {(DOMAIN, identifier)},
        "manufacturer": "DOOODS",
        "model": "DOOODS2",
        "configuration_url": base_url,
        "entry_type": dr.DeviceEntryType.SERVICE,
    }
    if "translation_key" in device_info_fields:
        device_info_values["translation_key"] = "doods_server"
    else:
        device_info_values["name"] = "DOOODS2"
    device_info = DeviceInfo(**device_info_values)
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_or_create_device"):
        device = registry.async_get_or_create_device(
            config_entry_id=entry.entry_id,
            device_info=device_info,
        )
    else:
        info = (
            device_info.as_dict()
            if hasattr(device_info, "as_dict")
            else dict(device_info)
        )
        device = registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            **info,
        )
    return device.id, identifier


def _remove_stale_camera_devices(
    hass: HomeAssistant,
    entry: ConfigEntry,
    parent_device_id: str,
    cameras: list[str],
) -> None:
    """Удалить дочерние устройства камер, которые больше не выбраны в записи интеграции."""
    registry = dr.async_get(hass)
    current_identifiers = {
        (DOMAIN, f"{entry.entry_id}:camera:{camera_entity_id}")
        for camera_entity_id in cameras
    }
    identifier_prefix = f"{entry.entry_id}:camera:"
    devices: dict[str, Any] = {}

    if get_children := getattr(dr, "async_entries_for_parent_device", None):
        devices.update(
            {device.id: device for device in get_children(registry, parent_device_id)}
        )
    if get_entry_devices := getattr(dr, "async_entries_for_config_entry", None):
        devices.update(
            {
                device.id: device
                for device in get_entry_devices(registry, entry.entry_id)
            }
        )
    if not devices:
        devices.update({device.id: device for device in registry.devices.values()})

    for device in devices.values():
        camera_identifiers = {
            identifier
            for identifier in device.identifiers
            if identifier[0] == DOMAIN and identifier[1].startswith(identifier_prefix)
        }
        if camera_identifiers and not camera_identifiers & current_identifiers:
            _LOGGER.info(
                "Удаляется устаревшее устройство камеры DOOODS %s (%s)",
                device.name,
                device.id,
            )
            registry.async_remove_device(device.id)


def _setup_motion_triggers(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: DooodsCoordinator,
    motion_sensors: Mapping[str, Any],
) -> None:
    """Отслеживать внешние датчики движения и запрашивать сканирование связанных камер."""
    cameras_by_motion_sensor: dict[str, set[str]] = {}
    for camera_entity_id, motion_entity_id in motion_sensors.items():
        if (
            camera_entity_id in coordinator.cameras
            and isinstance(motion_entity_id, str)
            and motion_entity_id
        ):
            cameras_by_motion_sensor.setdefault(motion_entity_id, set()).add(
                camera_entity_id
            )

    if not cameras_by_motion_sensor:
        return

    @callback
    def _handle_motion_state_change(event: Event) -> None:
        """Запросить сканирование только при переходе выбранного датчика движения из off в on."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if (
            old_state is None
            or old_state.state != "off"
            or new_state is None
            or new_state.state != "on"
        ):
            return
        camera_entity_ids = cameras_by_motion_sensor.get(event.data["entity_id"])
        if camera_entity_ids:
            coordinator.request_camera_scans(camera_entity_ids)

    entry.async_on_unload(
        async_track_state_change_event(
            hass,
            list(cameras_by_motion_sensor),
            _handle_motion_state_change,
        )
    )


async def async_setup_entry(hass: HomeAssistant, entry: DooodsConfigEntry) -> bool:
    """Проверить DOODS2, настроить родительское устройство и запустить сканирование камер."""
    base_url = entry.data[CONF_URL]
    auth_key = entry.data.get(CONF_AUTH_KEY, "")
    try:
        detectors = await async_get_detectors(hass, base_url, auth_key)
    except DooodsAuthError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_auth_failed",
        ) from err
    except (DooodsConnectionError, DooodsInvalidResponseError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_failed",
        ) from err
    except DooodsApiError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_failed",
        ) from err

    capabilities = await async_get_capabilities(hass, base_url, auth_key)
    preview_enabled = bool(auth_key) and "camera_preview" in capabilities
    annotation_localization_enabled = "localized_annotations" in capabilities
    if preview_enabled:
        _LOGGER.debug("Сервер DOOODS2 объявил поддержку временного preview API")
    if annotation_localization_enabled:
        _LOGGER.debug("Сервер DOOODS2 объявил поддержку локализованных JPEG-аннотаций")

    if not detectors:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="no_detectors",
        )

    options = entry.options
    detector_name = options.get(CONF_DETECTOR)
    detector = next(
        (item for item in detectors if item["name"] == detector_name),
        None,
    )
    if detector is None:
        detector = detectors[0]
        _LOGGER.warning(
            "Выбранный детектор %r недоступен; используется %r до обновления параметров",
            detector_name,
            detector["name"],
        )

    selected_labels = _label_list(options.get(CONF_LABELS, []))
    labels = selected_labels or detector["labels"]
    area = options.get(CONF_AREA, {})
    if not isinstance(area, Mapping):
        area = {}
    normalized_area = {**DEFAULT_AREA, **area}
    cameras = options.get(CONF_SOURCE, [])
    if not isinstance(cameras, list):
        cameras = []
    camera_profiles = normalize_camera_profiles(
        options.get(CONF_CAMERA_PROFILES, {}),
        cameras,
    )
    camera_labels: dict[str, list[str]] = {}
    profiles_by_id: dict[str, dict[str, Any]] = {}
    if camera_profiles:
        if not auth_key or not _PROFILE_FEATURES.issubset(capabilities):
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_profiles_unsupported",
            )
        try:
            available_profiles = await async_get_profiles(
                hass,
                base_url,
                auth_key,
            )
        except DooodsAuthError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_auth_failed",
            ) from err
        except DooodsApiError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_profiles_failed",
            ) from err

        profiles_by_id = {
            profile["id"]: profile for profile in available_profiles
        }
        missing_profile_ids = set(camera_profiles.values()) - set(profiles_by_id)
        if missing_profile_ids:
            _LOGGER.error(
                "В настройках DOOODS выбраны отсутствующие профили: %s",
                ", ".join(sorted(missing_profile_ids)),
            )
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_profile_not_found",
            )
        for camera_entity_id, profile_id in camera_profiles.items():
            camera_labels[camera_entity_id] = profile_labels_for_detector(
                profiles_by_id[profile_id],
                detector["labels"],
            )

    parent_device_id, parent_device_identifier = _register_server_device(
        hass, entry, base_url
    )
    coordinator = DooodsCoordinator(
        hass,
        entry,
        base_url=base_url,
        auth_key=auth_key,
        detector_name=detector["name"],
        cameras=cameras,
        labels=labels,
        detector_labels=detector["labels"],
        confidence=float(options.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE)),
        area=normalized_area,
        timeout=int(options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)),
        scan_interval=int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        parent_device_id=parent_device_id,
        parent_device_identifier=parent_device_identifier,
        preview_enabled=preview_enabled,
        annotation_localization_enabled=annotation_localization_enabled,
        camera_profiles=camera_profiles,
        camera_labels=camera_labels,
        archive_directory=normalize_archive_directory(
            options.get(CONF_FILE_OUT, "")
        ),
        archive_max_count=normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_COUNT, DEFAULT_ARCHIVE_MAX_COUNT),
            DEFAULT_ARCHIVE_MAX_COUNT,
        ),
        archive_max_age_days=normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_AGE_DAYS, DEFAULT_ARCHIVE_MAX_AGE_DAYS),
            DEFAULT_ARCHIVE_MAX_AGE_DAYS,
        ),
    )
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_stale_camera_devices(hass, entry, parent_device_id, cameras)
    motion_sensors = resolve_motion_sensors(
        hass,
        cameras,
        options.get(CONF_MOTION_SENSORS, {}),
    )
    if motion_sensors:
        _setup_motion_triggers(hass, entry, coordinator, motion_sensors)
    start_listener_pending = False
    remove_start_listener: Callable[[], None] | None = None

    @callback
    def _start_scanning(_event: Event | None = None) -> None:
        """Запустить таймер и первый скан после готовности Home Assistant."""
        nonlocal start_listener_pending
        start_listener_pending = False
        coordinator.start_periodic_scanning()
        initial_scan_task = entry.async_create_background_task(
            hass,
            coordinator.async_refresh(),
            name=f"{DOMAIN}_initial_camera_scan",
        )
        coordinator.set_initial_scan_task(initial_scan_task)

    if hass.is_running:
        _start_scanning()
    else:
        start_listener_pending = True
        remove_start_listener = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            _start_scanning,
        )

        @callback
        def _remove_start_listener_on_unload() -> None:
            """Снять listener только пока он не был автоматически вызван."""
            nonlocal start_listener_pending
            if start_listener_pending and remove_start_listener is not None:
                start_listener_pending = False
                remove_start_listener()

        entry.async_on_unload(_remove_start_listener_on_unload)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DooodsConfigEntry) -> bool:
    """Выгрузить платформы сущностей и остановить слушатели и таймеры координатора."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    if coordinator := getattr(entry, "runtime_data", None):
        await coordinator.async_shutdown()
    return True
