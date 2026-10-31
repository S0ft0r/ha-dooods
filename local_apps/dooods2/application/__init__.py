"""Настроить опрос DOOODS2 и сущности для каждой камеры."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
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
from .camera_motion_recheck_intervals import (
    normalize_camera_motion_recheck_intervals,
)
from .camera_scan_intervals import normalize_camera_scan_intervals
from .camera_switches import normalize_camera_switches
from .const import (
    CONF_AREA,
    CONF_ARCHIVE_MAX_AGE_DAYS,
    CONF_ARCHIVE_MAX_COUNT,
    CONF_AUTH_KEY,
    CONF_CAMERA_DETECTION_ENABLED,
    CONF_CAMERA_MOTION_RECHECK_INTERVALS,
    CONF_CAMERA_PROFILES,
    CONF_CAMERA_SCAN_INTERVALS,
    CONF_CAMERA_STREAM_ENABLED,
    CONF_CONFIDENCE,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_MOTION_SENSORS,
    CONF_SOURCE,
    CONF_TIMEOUT,
    CONF_URL,
    DEFAULT_ARCHIVE_MAX_AGE_DAYS,
    DEFAULT_ARCHIVE_MAX_COUNT,
    DEFAULT_AREA,
    DEFAULT_CONFIDENCE,
    DEFAULT_TIMEOUT,
    DOMAIN,
    MOTION_SENSOR_NONE,
)
from .coordinator import DooodsCoordinator
from .entity_migrations import migrate_camera_entity_registry
from .panel_url import build_profile_manager_url
from .motion import motion_sensor_is_usable, resolve_motion_sensors
from .profile_panel import (
    async_ensure_camera_profiles,
    async_register_profile_camera_archive_view,
    async_register_profile_camera_preview_view,
    async_register_profile_panel,
    async_register_profile_panel_commands,
    async_unregister_profile_panel_if_unused,
)
from .profiles import normalize_camera_profiles, profile_labels_for_detector
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
_PROFILE_FEATURES = {"profiles", "polygon_zones"}
PLATFORMS = (
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.IMAGE,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


type DooodsConfigEntry = ConfigEntry[DooodsCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Зарегистрировать сервис, панель и защищённые endpoint просмотра камеры и архива."""
    async_register_services(hass)
    async_register_profile_panel_commands(hass)
    try:
        await async_register_profile_camera_preview_view(hass)
    except Exception:
        _LOGGER.exception("Не удалось зарегистрировать preview камеры DOOODS")
    try:
        await async_register_profile_camera_archive_view(hass)
    except Exception:
        _LOGGER.exception("Не удалось зарегистрировать endpoint архива DOOODS")
    try:
        await async_register_profile_panel(hass)
    except Exception:  # Camera detection can still run if sidebar setup fails.
        _LOGGER.exception("Не удалось зарегистрировать панель профилей DOOODS")
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate per-camera diagnostic entities and the old stream switch once."""
    current_version = getattr(entry, "version", 1)
    if current_version > 2:
        _LOGGER.error(
            "Config entry %s has unsupported version %s",
            entry.entry_id,
            current_version,
        )
        return False
    if current_version < 2:
        options = entry.options if isinstance(entry.options, Mapping) else {}
        cameras = options.get(CONF_SOURCE, [])
        if not isinstance(cameras, list):
            cameras = []
        disabled, removed = migrate_camera_entity_registry(
            hass,
            entry.entry_id,
            cameras,
        )
        hass.config_entries.async_update_entry(entry, version=2)
        _LOGGER.info(
            "Migrated DOOODS camera entities for %s: disabled %d RTSP diagnostics; "
            "removed %d stream switches",
            entry.entry_id,
            disabled,
            removed,
        )
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
) -> tuple[str, str]:
    """Зарегистрировать виртуальное устройство-службу DOOODS2 и вернуть его идентификатор и ключ."""
    identifier = f"server:{entry.entry_id}"
    device_info_fields = getattr(DeviceInfo, "__annotations__", {})
    device_info_values: dict[str, Any] = {
        "identifiers": {(DOMAIN, identifier)},
        "manufacturer": "DOOODS",
        "model": "DOOODS2",
        "entry_type": dr.DeviceEntryType.SERVICE,
    }
    if panel_url := build_profile_manager_url(hass):
        device_info_values["configuration_url"] = panel_url
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
    """Использовать HA motion-сенсор только пока RTSP gate камеры не активен."""
    cameras_by_motion_sensor: dict[str, set[str]] = {}
    for camera_entity_id, motion_entity_id in motion_sensors.items():
        if (
            camera_entity_id in coordinator.cameras
            and isinstance(motion_entity_id, str)
            and motion_entity_id.startswith("binary_sensor.")
            and motion_entity_id != MOTION_SENSOR_NONE
        ):
            cameras_by_motion_sensor.setdefault(motion_entity_id, set()).add(
                camera_entity_id
            )

    if not cameras_by_motion_sensor:
        return

    @callback
    def _handle_motion_state_change(event: Event) -> None:
        """Запросить сканирование при переходе выбранного датчика движения в on."""
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        camera_entity_ids = cameras_by_motion_sensor.get(event.data["entity_id"])
        if not camera_entity_ids:
            return
        sensor_state = new_state.state if new_state is not None else None
        for camera_entity_id in camera_entity_ids:
            coordinator.motion_sensor_state_changed(camera_entity_id, sensor_state)
        if (
            new_state is not None
            and new_state.state == "on"
            and (old_state is None or old_state.state != "on")
        ):
            scan_camera_ids = coordinator.camera_ids_for_ha_motion_trigger(
                camera_entity_ids
            )
            coordinator.request_camera_scans(scan_camera_ids)

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
    motion_stream_enabled = bool(auth_key) and "rtsp_motion_stream" in capabilities
    if preview_enabled:
        _LOGGER.debug("Сервер DOOODS2 объявил поддержку временного preview API")
    if annotation_localization_enabled:
        _LOGGER.debug("Сервер DOOODS2 объявил поддержку локализованных JPEG-аннотаций")
    if motion_stream_enabled:
        _LOGGER.debug("Сервер DOOODS2 поддерживает непрерывный RTSP motion stream")

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
    cameras = list(
        dict.fromkeys(
            camera for camera in cameras if isinstance(camera, str) and camera
        )
    )
    motion_sensors = resolve_motion_sensors(
        hass,
        cameras,
        options.get(CONF_MOTION_SENSORS, {}),
    )
    camera_scan_intervals = normalize_camera_scan_intervals(
        options.get(CONF_CAMERA_SCAN_INTERVALS, {}),
        cameras,
    )
    camera_motion_recheck_intervals = normalize_camera_motion_recheck_intervals(
        options.get(CONF_CAMERA_MOTION_RECHECK_INTERVALS, {}),
        cameras,
    )
    camera_detection_enabled = normalize_camera_switches(
        options.get(CONF_CAMERA_DETECTION_ENABLED, {}),
        cameras,
    )
    default_stream_sources = {
        camera_entity_id: bool(
            motion_stream_enabled
            and not motion_sensor_is_usable(
                hass,
                motion_sensors.get(camera_entity_id),
            )
        )
        for camera_entity_id in cameras
    }
    camera_stream_enabled = normalize_camera_switches(
        options.get(CONF_CAMERA_STREAM_ENABLED, {}),
        cameras,
        default=default_stream_sources,
    )
    # Persist the initial source so later sensor availability changes do not silently
    # switch a camera from HA motion to RTSP after the user has chosen a source.
    if camera_stream_enabled != options.get(CONF_CAMERA_STREAM_ENABLED, {}):
        options = dict(options)
        options[CONF_CAMERA_STREAM_ENABLED] = camera_stream_enabled
        hass.config_entries.async_update_entry(entry, options=options)
    camera_profiles = normalize_camera_profiles(
        options.get(CONF_CAMERA_PROFILES, {}),
        cameras,
    )
    camera_labels: dict[str, list[str]] = {}
    profiles_by_id: dict[str, dict[str, Any]] = {}
    profiles_supported = bool(auth_key) and _PROFILE_FEATURES.issubset(capabilities)
    if cameras and profiles_supported:
        try:
            profile_person_label = next(
                (
                    label
                    for label in detector["labels"]
                    if label.casefold() == "person"
                ),
                "person",
            )
            camera_profiles = await async_ensure_camera_profiles(
                hass,
                entry,
                cameras,
                camera_profiles,
                person_label=profile_person_label,
            )
        except DooodsAuthError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_auth_failed",
            ) from err
        except HomeAssistantError as err:
            raise ConfigEntryNotReady(
                translation_domain=DOMAIN,
                translation_key="setup_profiles_failed",
            ) from err
        if camera_profiles != options.get(CONF_CAMERA_PROFILES, {}):
            options = dict(options)
            options[CONF_CAMERA_PROFILES] = camera_profiles
            hass.config_entries.async_update_entry(entry, options=options)
    elif camera_profiles:
        profile_person_label = next(
            (
                label
                for label in detector["labels"]
                if label.casefold() == "person"
            ),
            "person",
        )
        _LOGGER.warning(
            "Profiles API недоступен; для камер записи %s используются legacy-defaults",
            entry.entry_id,
        )
        camera_profiles = {}
        options = dict(options)
        options[CONF_CAMERA_PROFILES] = {}
        options[CONF_LABELS] = [profile_person_label]
        options[CONF_CONFIDENCE] = 50
        options[CONF_AREA] = dict(DEFAULT_AREA)
        hass.config_entries.async_update_entry(entry, options=options)
        labels = [profile_person_label]
        normalized_area = dict(DEFAULT_AREA)

    if camera_profiles:
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
                "Профили камер DOOODS не найдены после проверки сервера: %s",
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

    parent_device_id, parent_device_identifier = _register_server_device(hass, entry)
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
        camera_scan_intervals=camera_scan_intervals,
        camera_motion_recheck_intervals=camera_motion_recheck_intervals,
        parent_device_id=parent_device_id,
        parent_device_identifier=parent_device_identifier,
        preview_enabled=preview_enabled,
        annotation_localization_enabled=annotation_localization_enabled,
        motion_stream_enabled=motion_stream_enabled,
        motion_sensors=motion_sensors,
        camera_detection_enabled=camera_detection_enabled,
        camera_stream_enabled=camera_stream_enabled,
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
    try:
        await async_register_profile_panel(hass)
    except Exception:  # Keep camera detection available if panel registration fails.
        _LOGGER.exception("Не удалось зарегистрировать панель профилей DOOODS")
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
    await async_unregister_profile_panel_if_unused(
        hass,
        unloading_entry_id=entry.entry_id,
    )
    return True
