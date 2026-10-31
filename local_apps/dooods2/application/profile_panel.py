"""Встроенная панель Home Assistant для редактирования зон камер DOOODS2."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import probatio
from aiohttp import web
try:
    from homeassistant.components.http.const import KEY_HASS_USER
except ImportError:
    from homeassistant.auth.const import KEY_HASS_USER
from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.camera import async_get_image
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import (
    DooodsApiError,
    DooodsAuthError,
    DooodsConnectionError,
    DooodsInvalidResponseError,
    async_get_capabilities,
    async_get_detectors,
    async_get_profile,
    async_get_profiles,
    async_put_profile,
)
from .archive import (
    async_list_archived_frames,
    async_read_archived_frame,
    normalize_archive_directory,
)
from .const import (
    CONF_AUTH_KEY,
    CONF_CAMERA_PROFILES,
    CONF_FILE_OUT,
    CONF_DETECTOR,
    CONF_LABELS,
    CONF_SOURCE,
    CONF_URL,
    DOMAIN,
)
from .panel_url import PROFILE_MANAGER_PANEL_PATH
from .profiles import (
    camera_profile_id,
    default_camera_profile,
    normalize_camera_profiles,
)

_PROFILE_FEATURES = {"profiles", "polygon_zones"}
_PANEL_PATH = PROFILE_MANAGER_PANEL_PATH
_PANEL_ELEMENT = "dooods-profile-manager"
_PANEL_TITLE = "DOOODS"
_PANEL_ICON = "mdi:cctv"
_STATIC_URL = "/dooods_profile_panel"
_STATIC_DIR = Path(__file__).parent / "frontend"
_PANEL_STATE_KEY = f"_{DOMAIN}_profile_panel"
_COMMANDS_REGISTERED_KEY = f"_{DOMAIN}_profile_panel_commands_registered"
_PREVIEW_VIEW_REGISTERED_KEY = f"_{DOMAIN}_profile_preview_view_registered"
_ARCHIVE_VIEW_REGISTERED_KEY = f"_{DOMAIN}_profile_archive_view_registered"
_PROFILE_SETUP_LOCK_KEY = f"_{DOMAIN}_profile_setup_lock"
_MAX_ZONES_PER_PROFILE = 128


def _entry_for_id(hass: HomeAssistant, entry_id: str) -> Any:
    """Найти запись DOOODS и не допустить обращение к другой интеграции."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise HomeAssistantError("Запись интеграции DOOODS не найдена.")
    return entry


def _cameras_for_entry(entry: Any) -> list[str]:
    """Вернуть только выбранные в этой записи камеры."""
    options = entry.options if isinstance(entry.options, Mapping) else {}
    cameras = options.get(CONF_SOURCE, [])
    if not isinstance(cameras, list):
        return []
    return list(
        dict.fromkeys(
            camera for camera in cameras if isinstance(camera, str) and camera
        )
    )


def _camera_name(hass: HomeAssistant, camera_entity_id: str) -> str:
    """Вернуть понятное имя камеры, не меняя её entity ID."""
    state = hass.states.get(camera_entity_id)
    if state is not None:
        name = state.attributes.get("friendly_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return camera_entity_id


def _profiles_error_code(error: Exception) -> str:
    """Сопоставить ответ backend с коротким безопасным состоянием панели."""
    if isinstance(error, DooodsAuthError):
        return "invalid_auth"
    return "unavailable"


async def _async_profile_api(
    hass: HomeAssistant,
    entry: Any,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Проверить возможности сервера и получить профили без передачи ключа в UI."""
    base_url = entry.data.get(CONF_URL, "")
    auth_key = entry.data.get(CONF_AUTH_KEY, "")
    if not base_url or not auth_key:
        raise HomeAssistantError("Для профильных функций нужен API-ключ DOOODS2.")

    features = await async_get_capabilities(hass, base_url, auth_key)
    if not _PROFILE_FEATURES.issubset(features):
        raise HomeAssistantError("Сервер не объявляет поддержку профилей и зон.")
    try:
        profiles = await async_get_profiles(hass, base_url, auth_key)
    except DooodsApiError as err:
        raise HomeAssistantError("Не удалось получить профили DOOODS2.") from err
    return features, profiles


async def async_ensure_camera_profiles(
    hass: HomeAssistant,
    entry: Any,
    camera_entity_ids: list[str],
    configured_profiles: Mapping[str, str] | None = None,
    *,
    person_label: str = "person",
) -> dict[str, str]:
    """Обеспечить отдельный стабильный профиль для каждой выбранной камеры."""
    camera_entity_ids = list(dict.fromkeys(camera_entity_ids))
    lock = hass.data.setdefault(_PROFILE_SETUP_LOCK_KEY, asyncio.Lock())
    async with lock:
        _, available_profiles = await _async_profile_api(hass, entry)
        profiles_by_id = {
            profile["id"]: profile
            for profile in available_profiles
            if isinstance(profile.get("id"), str)
        }
        current_map = normalize_camera_profiles(
            configured_profiles or {},
            camera_entity_ids,
        )
        result: dict[str, str] = {}
        claimed_ids: set[str] = set()
        base_url = entry.data[CONF_URL]
        auth_key = entry.data[CONF_AUTH_KEY]

        async def repair_empty_profile(
            profile_id: str,
            camera_entity_id: str,
            summary: Mapping[str, Any],
        ) -> None:
            """Восстановить единственную стартовую зону, не меняя ID или название."""
            if summary.get("zone_count") != 0:
                return
            profile = default_camera_profile(
                camera_entity_id,
                _camera_name(hass, camera_entity_id),
                person_label=person_label,
            )
            profile["id"] = profile_id
            if isinstance(summary.get("name"), str) and summary["name"].strip():
                profile["name"] = summary["name"]
            try:
                await async_put_profile(hass, base_url, auth_key, profile)
            except DooodsApiError as err:
                raise HomeAssistantError(
                    "Не удалось восстановить обязательную зону профиля камеры."
                ) from err
            profiles_by_id[profile_id] = {
                "id": profile_id,
                "name": profile["name"],
                "zone_count": 1,
            }

        for camera_entity_id in camera_entity_ids:
            current_id = current_map.get(camera_entity_id)
            if current_id in profiles_by_id and current_id not in claimed_ids:
                await repair_empty_profile(
                    current_id,
                    camera_entity_id,
                    profiles_by_id[current_id],
                )
                result[camera_entity_id] = current_id
                claimed_ids.add(current_id)
                continue

            target_id = camera_profile_id(camera_entity_id)
            if target_id in claimed_ids:
                raise HomeAssistantError(
                    "Не удалось создать уникальный профиль для выбранной камеры."
                )
            if target_id in profiles_by_id:
                await repair_empty_profile(
                    target_id,
                    camera_entity_id,
                    profiles_by_id[target_id],
                )
                result[camera_entity_id] = target_id
                claimed_ids.add(target_id)
                continue

            profile: dict[str, Any]
            if current_id in profiles_by_id:
                try:
                    source_profile = await async_get_profile(
                        hass,
                        base_url,
                        auth_key,
                        current_id,
                    )
                except DooodsApiError as err:
                    raise HomeAssistantError(
                        "Не удалось прочитать профиль камеры для создания отдельной копии."
                    ) from err
                source_zones = source_profile.get("zones")
                if isinstance(source_zones, list) and source_zones:
                    profile = {
                        "id": target_id,
                        "name": _camera_name(hass, camera_entity_id)[:128],
                        "zones": deepcopy(source_zones),
                    }
                else:
                    profile = default_camera_profile(
                        camera_entity_id,
                        _camera_name(hass, camera_entity_id),
                        person_label=person_label,
                    )
            else:
                profile = default_camera_profile(
                    camera_entity_id,
                    _camera_name(hass, camera_entity_id),
                    person_label=person_label,
                )

            try:
                await async_put_profile(hass, base_url, auth_key, profile)
            except DooodsApiError as err:
                raise HomeAssistantError(
                    f"Не удалось создать профиль для камеры «{_camera_name(hass, camera_entity_id)}»."
                ) from err
            profiles_by_id[target_id] = {
                "id": target_id,
                "name": profile["name"],
            }
            result[camera_entity_id] = target_id
            claimed_ids.add(target_id)

        return result


def _archive_labels_for_camera(entry: Any, camera_entity_id: str) -> list[str]:
    """Вернуть настроенные метки камеры для локального фильтра архива."""
    options = entry.options if isinstance(entry.options, Mapping) else {}
    coordinator = getattr(entry, "runtime_data", None)
    labels_for_camera = getattr(coordinator, "labels_for_camera", None)
    if callable(labels_for_camera):
        labels = labels_for_camera(camera_entity_id)
    elif isinstance(getattr(coordinator, "camera_labels", None), Mapping):
        labels = coordinator.camera_labels.get(camera_entity_id, [])
    else:
        labels = options.get(CONF_LABELS, [])
    if not isinstance(labels, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            label.strip()
            for label in labels
            if isinstance(label, str) and label.strip()
        )
    )


async def async_get_profile_panel_data(hass: HomeAssistant) -> dict[str, Any]:
    """Собрать список записей и выбранных камер для встроенного редактора."""
    result: list[dict[str, Any]] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        cameras = _cameras_for_entry(entry)
        options = entry.options if isinstance(entry.options, Mapping) else {}
        profile_map = normalize_camera_profiles(
            options.get(CONF_CAMERA_PROFILES, {}),
            cameras,
        )
        profiles_error: str | None = None
        available_profile_ids: set[str] = set()
        if cameras:
            try:
                _, available_profiles = await _async_profile_api(hass, entry)
                available_profile_ids = {
                    profile["id"]
                    for profile in available_profiles
                    if isinstance(profile.get("id"), str)
                }
            except HomeAssistantError as err:
                if "API-ключ" in str(err):
                    profiles_error = "auth_missing"
                elif "не объявляет" in str(err):
                    profiles_error = "unsupported"
                else:
                    profiles_error = _profiles_error_code(err.__cause__ or err)

        result.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title or entry.entry_id,
                "profiles_error": profiles_error,
                "cameras": [
                    {
                        "entity_id": camera_entity_id,
                        "name": _camera_name(hass, camera_entity_id),
                        "archive_labels": _archive_labels_for_camera(
                            entry,
                            camera_entity_id,
                        ),
                        "profile_ready": profile_map.get(camera_entity_id)
                        in available_profile_ids,
                    }
                    for camera_entity_id in cameras
                ],
            }
        )

    return {"entries": result}


async def async_get_camera_profile_editor_data(
    hass: HomeAssistant,
    *,
    entry_id: str,
    camera_entity_id: str,
) -> dict[str, Any]:
    """Получить зоны и доступные метки для редактора одной выбранной камеры."""
    entry = _entry_for_id(hass, entry_id)
    cameras = _cameras_for_entry(entry)
    if camera_entity_id not in cameras:
        raise HomeAssistantError("Эта камера не выбрана в указанной записи DOOODS.")
    options = entry.options if isinstance(entry.options, Mapping) else {}
    profile_map = normalize_camera_profiles(
        options.get(CONF_CAMERA_PROFILES, {}),
        cameras,
    )
    profile_id = profile_map.get(camera_entity_id)
    if not profile_id:
        raise HomeAssistantError("Для камеры ещё не создан профиль.")

    _, available_profiles = await _async_profile_api(hass, entry)
    if not any(profile.get("id") == profile_id for profile in available_profiles):
        raise HomeAssistantError("Профиль камеры не найден на сервере DOOODS2.")
    base_url = entry.data[CONF_URL]
    auth_key = entry.data[CONF_AUTH_KEY]
    try:
        profile = await async_get_profile(hass, base_url, auth_key, profile_id)
        detectors = await async_get_detectors(hass, base_url, auth_key)
    except DooodsApiError as err:
        raise HomeAssistantError(
            "Не удалось загрузить зоны или метки DOOODS2."
        ) from err

    detector_name = options.get(CONF_DETECTOR)
    selected_detector = next(
        (detector for detector in detectors if detector.get("name") == detector_name),
        None,
    )
    if selected_detector is None:
        raise HomeAssistantError(
            "Выбранный детектор не найден. Исправьте настройки детектора в Options Flow."
        )
    labels = selected_detector.get("labels", [])
    if not labels:
        raise HomeAssistantError(
            "Выбранный детектор не вернул метки. Проверьте конфигурацию DOODS2."
        )
    available_labels = list(
        dict.fromkeys(
            label.strip()
            for label in labels
            if isinstance(label, str) and label.strip()
        )
    )
    return {
        "zones": profile["zones"],
        "available_labels": available_labels,
    }


async def async_get_camera_archive_data(
    hass: HomeAssistant,
    *,
    entry_id: str,
    camera_entity_id: str,
    cursor: str = "",
    limit: int = 40,
    label: str = "",
) -> dict[str, Any]:
    """Вернуть безопасную страницу локального архива выбранной камеры."""
    entry = _entry_for_id(hass, entry_id)
    if camera_entity_id not in _cameras_for_entry(entry):
        raise HomeAssistantError("Эта камера не выбрана в указанной записи DOOODS.")
    if (
        not isinstance(cursor, str)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not isinstance(label, str)
        or len(label) > 128
    ):
        raise HomeAssistantError("Параметры страницы архива некорректны.")
    label = label.strip()
    if label and label not in _archive_labels_for_camera(entry, camera_entity_id):
        raise HomeAssistantError("Эта метка не настроена для выбранной камеры.")
    options = entry.options if isinstance(entry.options, Mapping) else {}
    archive_directory = normalize_archive_directory(options.get(CONF_FILE_OUT))
    if not archive_directory:
        return {"enabled": False, "frames": [], "next_cursor": ""}
    try:
        result = await async_list_archived_frames(
            hass,
            archive_directory,
            entry_id,
            camera_entity_id,
            cursor=cursor,
            limit=limit,
            label=label,
        )
    except (OSError, TypeError, ValueError) as err:
        raise HomeAssistantError("Не удалось прочитать локальный архив DOOODS.") from err
    return {"enabled": True, **result}


async def async_get_camera_archive_image(
    hass: HomeAssistant,
    *,
    entry_id: str,
    camera_entity_id: str,
    frame_id: str,
) -> bytes | None:
    """Прочитать JPEG, повторно проверив доступ записи и выбранной камеры."""
    entry = _entry_for_id(hass, entry_id)
    if camera_entity_id not in _cameras_for_entry(entry):
        raise HomeAssistantError("Эта камера не выбрана в указанной записи DOOODS.")
    options = entry.options if isinstance(entry.options, Mapping) else {}
    archive_directory = normalize_archive_directory(options.get(CONF_FILE_OUT))
    if not archive_directory:
        return None
    return await async_read_archived_frame(
        hass,
        archive_directory,
        entry_id,
        camera_entity_id,
        frame_id,
    )


async def async_save_camera_profile_zones(
    hass: HomeAssistant,
    *,
    entry_id: str,
    camera_entity_id: str,
    zones: list[dict[str, Any]],
) -> dict[str, Any]:
    """Сохранить зоны камеры в её серверном профиле, не изменяя связь камер."""
    entry = _entry_for_id(hass, entry_id)
    cameras = _cameras_for_entry(entry)
    if camera_entity_id not in cameras:
        raise HomeAssistantError("Эта камера не выбрана в указанной записи DOOODS.")
    if not isinstance(zones, list) or not zones or len(zones) > _MAX_ZONES_PER_PROFILE:
        raise HomeAssistantError("В профиле должна остаться хотя бы одна зона.")
    if any(not isinstance(zone, dict) for zone in zones):
        raise HomeAssistantError("Не удалось проверить список зон профиля.")

    options = entry.options if isinstance(entry.options, Mapping) else {}
    profile_map = normalize_camera_profiles(
        options.get(CONF_CAMERA_PROFILES, {}),
        cameras,
    )
    profile_id = profile_map.get(camera_entity_id)
    if not profile_id:
        raise HomeAssistantError("Для камеры ещё не создан профиль.")
    base_url = entry.data.get(CONF_URL, "")
    auth_key = entry.data.get(CONF_AUTH_KEY, "")
    if not base_url or not auth_key:
        raise HomeAssistantError("Профиль недоступен: проверьте подключение к DOOODS2.")

    try:
        current_profile = await async_get_profile(
            hass,
            base_url,
            auth_key,
            profile_id,
        )
        saved_profile = await async_put_profile(
            hass,
            base_url,
            auth_key,
            {
                "id": profile_id,
                "name": current_profile["name"],
                "zones": zones,
            },
        )
    except DooodsAuthError as err:
        raise HomeAssistantError(
            "Сервер DOOODS2 отклонил API-ключ; изменения зон не сохранены."
        ) from err
    except DooodsInvalidResponseError as err:
        if getattr(err, "translation_key", None) == "profile_not_found":
            raise HomeAssistantError(
                "Профиль камеры не найден. Перезагрузите интеграцию DOOODS и повторите попытку."
            ) from err
        raise HomeAssistantError(
            "DOOODS2 отклонил зоны профиля. Проверьте геометрию, координаты, "
            "метки и пороги."
        ) from err
    except DooodsConnectionError as err:
        raise HomeAssistantError(
            "Не удалось связаться с DOOODS2; изменения зон не сохранены."
        ) from err
    except DooodsApiError as err:
        raise HomeAssistantError(
            "Ответ DOOODS2 не удалось проверить; изменения зон не сохранены."
        ) from err

    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is not None and getattr(coordinator, "camera_profiles", None):
        coordinator._schedule_profile_labels_refresh()
        coordinator.request_camera_scans([camera_entity_id])

    return {"zones": saved_profile["zones"]}


class DooodsCameraPreviewView(HomeAssistantView):
    """Защищённо получить свежий кадр выбранной камеры для встроенного редактора."""

    url = "/api/dooods/profile_manager/preview"
    name = "api:dooods:profile_manager:preview"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Сохранить ссылку на Home Assistant для проверки записи и камеры."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Вернуть текущий кадр только администратору и только для выбранной камеры."""
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            raise web.HTTPForbidden()
        entry_id = request.query.get("entry_id", "")
        camera_entity_id = request.query.get("camera_entity_id", "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or camera_entity_id not in _cameras_for_entry(entry)
        ):
            raise web.HTTPNotFound()
        try:
            image = await async_get_image(self.hass, camera_entity_id, timeout=5)
        except Exception as err:  # Camera providers can raise integration-specific errors.
            raise web.HTTPBadGateway(text="Не удалось получить кадр камеры.") from err
        content = getattr(image, "content", None)
        content_type = str(getattr(image, "content_type", "")).lower()
        if not isinstance(content, bytes) or not content:
            raise web.HTTPBadGateway(text="Камера не вернула изображение.")
        if content_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise web.HTTPUnsupportedMediaType(
                text="Формат кадра камеры не поддерживается."
            )
        return web.Response(
            body=content,
            content_type=content_type,
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )


class DooodsCameraArchiveImageView(HomeAssistantView):
    """Защищённо отдавать JPEG из архива выбранной камеры."""

    url = "/api/dooods/profile_manager/archive_image"
    name = "api:dooods:profile_manager:archive_image"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Сохранить Home Assistant для проверки доступа к записи DOOODS."""
        self.hass = hass

    async def get(self, request: web.Request) -> web.Response:
        """Выдать JPEG только администратору и только из выбранной камеры."""
        user = request.get(KEY_HASS_USER)
        if user is None or not getattr(user, "is_admin", False):
            raise web.HTTPForbidden()
        entry_id = request.query.get("entry_id", "")
        camera_entity_id = request.query.get("camera_entity_id", "")
        frame_id = request.query.get("frame_id", "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if (
            entry is None
            or entry.domain != DOMAIN
            or camera_entity_id not in _cameras_for_entry(entry)
        ):
            raise web.HTTPNotFound()
        try:
            content = await async_get_camera_archive_image(
                self.hass,
                entry_id=entry_id,
                camera_entity_id=camera_entity_id,
                frame_id=frame_id,
            )
        except HomeAssistantError as err:
            raise web.HTTPNotFound() from err
        except (OSError, TypeError, ValueError) as err:
            raise web.HTTPBadGateway(
                text="Не удалось прочитать JPEG из локального архива."
            ) from err
        if not isinstance(content, bytes) or not content:
            raise web.HTTPNotFound()
        return web.Response(
            body=content,
            content_type="image/jpeg",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
            },
        )


async def async_register_profile_camera_preview_view(hass: HomeAssistant) -> None:
    """Однократно зарегистрировать защищённый HTTP endpoint снимка камеры."""
    if hass.data.get(_PREVIEW_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(DooodsCameraPreviewView(hass))
    hass.data[_PREVIEW_VIEW_REGISTERED_KEY] = True


async def async_register_profile_camera_archive_view(hass: HomeAssistant) -> None:
    """Однократно зарегистрировать защищённый endpoint JPEG-архива."""
    if hass.data.get(_ARCHIVE_VIEW_REGISTERED_KEY):
        return
    hass.http.register_view(DooodsCameraArchiveImageView(hass))
    hass.data[_ARCHIVE_VIEW_REGISTERED_KEY] = True


@websocket_api.websocket_command(
    {probatio.Required("type"): f"{DOMAIN}/profile_manager/data"}
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_profile_manager_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Вернуть список выбранных камер без назначения или раскрытия данных авторизации."""
    connection.send_result(msg["id"], await async_get_profile_panel_data(hass))


@websocket_api.websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/profile_manager/editor_data",
        probatio.Required("entry_id"): str,
        probatio.Required("camera_entity_id"): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_profile_editor_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Загрузить зоны и метки выбранной камеры через серверные API-запросы."""
    result = await async_get_camera_profile_editor_data(
        hass,
        entry_id=msg["entry_id"],
        camera_entity_id=msg["camera_entity_id"],
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/profile_manager/archive_data",
        probatio.Required("entry_id"): str,
        probatio.Required("camera_entity_id"): str,
        probatio.Required("cursor"): str,
        probatio.Required("limit"): int,
        probatio.Optional("label", default=""): str,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_profile_archive_data(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Вернуть одну страницу метаданных локального архива для администраторов."""
    result = await async_get_camera_archive_data(
        hass,
        entry_id=msg["entry_id"],
        camera_entity_id=msg["camera_entity_id"],
        cursor=msg["cursor"],
        limit=msg["limit"],
        label=msg.get("label", ""),
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        probatio.Required("type"): f"{DOMAIN}/profile_manager/save_zones",
        probatio.Required("entry_id"): str,
        probatio.Required("camera_entity_id"): str,
        probatio.Required("zones"): list,
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def websocket_save_camera_profile_zones(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Сохранить изменения зон выбранной камеры."""
    result = await async_save_camera_profile_zones(
        hass,
        entry_id=msg["entry_id"],
        camera_entity_id=msg["camera_entity_id"],
        zones=msg["zones"],
    )
    connection.send_result(msg["id"], result)


def async_register_profile_panel_commands(hass: HomeAssistant) -> None:
    """Однократно зарегистрировать защищённые WebSocket-команды встроенной панели."""
    if hass.data.get(_COMMANDS_REGISTERED_KEY):
        return
    websocket_api.async_register_command(hass, websocket_profile_manager_data)
    websocket_api.async_register_command(hass, websocket_profile_editor_data)
    websocket_api.async_register_command(hass, websocket_profile_archive_data)
    websocket_api.async_register_command(hass, websocket_save_camera_profile_zones)
    hass.data[_COMMANDS_REGISTERED_KEY] = True


async def async_register_profile_panel(hass: HomeAssistant) -> None:
    """Подать встроенный модуль редактора и зарегистрировать sidebar-панель HA."""
    state = hass.data.setdefault(_PANEL_STATE_KEY, {})
    lock = state.setdefault("registration_lock", asyncio.Lock())
    async with lock:
        if frontend.async_panel_exists(hass, _PANEL_PATH):
            return
        if not state.get("static_path_registered"):
            await hass.http.async_register_static_paths(
                [
                    StaticPathConfig(
                        f"{_STATIC_URL}/profile-manager.js",
                        str(_STATIC_DIR / "profile-manager.js"),
                        cache_headers=False,
                    )
                ]
            )
            state["static_path_registered"] = True
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=_PANEL_PATH,
            webcomponent_name=_PANEL_ELEMENT,
            sidebar_title=_PANEL_TITLE,
            sidebar_icon=_PANEL_ICON,
            module_url=f"{_STATIC_URL}/profile-manager.js",
            embed_iframe=False,
            require_admin=True,
        )


async def async_unregister_profile_panel_if_unused(
    hass: HomeAssistant,
    *,
    unloading_entry_id: str,
) -> None:
    """Скрыть панель после выгрузки последней записи интеграции."""
    state = hass.data.setdefault(_PANEL_STATE_KEY, {})
    lock = state.setdefault("registration_lock", asyncio.Lock())
    async with lock:
        remaining_entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if entry.entry_id != unloading_entry_id
        ]
        if remaining_entries:
            return
        frontend.async_remove_panel(hass, _PANEL_PATH, warn_if_unknown=False)
