"""Мастера первоначальной настройки и параметров независимой интеграции DOOODS."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override
from urllib.parse import urlsplit, urlunsplit

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

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
    CONF_PROFILE_ID,
    CONF_BOTTOM,
    CONF_CONFIDENCE,
    CONF_COVERS,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_LEFT,
    CONF_MOTION_SENSOR,
    CONF_MOTION_SENSORS,
    CONF_RIGHT,
    CONF_SCAN_INTERVAL,
    CONF_SOURCE,
    CONF_TIMEOUT,
    CONF_TOP,
    CONF_URL,
    DEFAULT_ARCHIVE_MAX_AGE_DAYS,
    DEFAULT_ARCHIVE_MAX_COUNT,
    DEFAULT_AREA,
    DEFAULT_CONFIDENCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .motion import camera_motion_sensors
from .profiles import normalize_camera_profiles

_LOGGER = logging.getLogger(__name__)
NO_PROFILE_VALUE = "__no_profile__"
_PROFILE_FEATURES = {"profiles", "polygon_zones"}

AREA_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_TOP, default=DEFAULT_AREA[CONF_TOP]): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=1)
        ),
        probatio.Required(CONF_LEFT, default=DEFAULT_AREA[CONF_LEFT]): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=1)
        ),
        probatio.Required(CONF_BOTTOM, default=DEFAULT_AREA[CONF_BOTTOM]): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=1)
        ),
        probatio.Required(CONF_RIGHT, default=DEFAULT_AREA[CONF_RIGHT]): probatio.All(
            probatio.Coerce(float), probatio.Range(min=0, max=1)
        ),
        probatio.Required(CONF_COVERS, default=DEFAULT_AREA[CONF_COVERS]): bool,
    }
)

USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        probatio.Optional(probatio.Secret(CONF_AUTH_KEY), default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

RECONFIGURE_SCHEMA = USER_SCHEMA

CAMERA_SCHEDULE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SOURCE, default=[]): EntitySelector(
            EntitySelectorConfig(domain="camera", multiple=True)
        ),
        probatio.Required(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): probatio.All(probatio.Coerce(int), probatio.Range(min=1, max=86400)),
    }
)

MOTION_SENSOR_SCHEMA = probatio.Schema(
    {
        probatio.Optional(CONF_MOTION_SENSOR): EntitySelector(
            EntitySelectorConfig(domain="binary_sensor")
        ),
    }
)

AREA_OPTIONS_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_AREA, default=dict(DEFAULT_AREA)): section(
            AREA_SCHEMA,
            {"collapsed": True},
        ),
    }
)

OUTPUT_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_FILE_OUT, default=""): TextSelector(
            TextSelectorConfig(multiline=False)
        ),
        probatio.Required(
            CONF_ARCHIVE_MAX_COUNT,
            default=DEFAULT_ARCHIVE_MAX_COUNT,
        ): probatio.All(probatio.Coerce(int), probatio.Range(min=0, max=100000)),
        probatio.Required(
            CONF_ARCHIVE_MAX_AGE_DAYS,
            default=DEFAULT_ARCHIVE_MAX_AGE_DAYS,
        ): probatio.All(probatio.Coerce(int), probatio.Range(min=0, max=36500)),
    }
)


def _normalize_server_url(value: str) -> str | None:
    """Проверить и нормализовать базовый URL DOODS2 без обращения к серверу."""
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return None

    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        return None

    hostname = hostname.lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


async def _async_get_detectors(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Получить список детекторов и сопоставить исключения API с ключами ошибок мастера."""
    try:
        return await async_get_detectors(hass, base_url, auth_key), None
    except DooodsAuthError:
        return None, "invalid_auth"
    except DooodsInvalidResponseError as err:
        _LOGGER.debug("Некорректный ответ /detectors от %s: %s", base_url, err)
        return None, "invalid_response"
    except DooodsConnectionError as err:
        _LOGGER.debug(
            "Не удалось подключиться к DOODS2 по адресу %s: %s", base_url, err
        )
        return None, "cannot_connect"


def _detector_schema(
    detectors: list[dict[str, Any]],
    default_detector: str,
) -> probatio.Schema:
    """Создать список детекторов и общие параметры распознавания."""
    options = [
        SelectOptionDict(
            value=detector["name"],
            label=(
                f"{detector['name']} ({detector['type']})"
                if detector["type"]
                else detector["name"]
            ),
        )
        for detector in detectors
    ]
    return probatio.Schema(
        {
            probatio.Required(CONF_DETECTOR, default=default_detector): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            probatio.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=3600)
            ),
            probatio.Required(
                CONF_CONFIDENCE, default=DEFAULT_CONFIDENCE
            ): probatio.All(probatio.Coerce(float), probatio.Range(min=0, max=100)),
        }
    )


def _manual_detection_schema(default_detector: str) -> probatio.Schema:
    """Разрешить редактирование сохранённых параметров детектора при временной недоступности сервера."""
    return probatio.Schema(
        {
            probatio.Required(CONF_DETECTOR, default=default_detector): TextSelector(
                TextSelectorConfig()
            ),
            probatio.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): probatio.All(
                probatio.Coerce(int), probatio.Range(min=1, max=3600)
            ),
            probatio.Required(
                CONF_CONFIDENCE, default=DEFAULT_CONFIDENCE
            ): probatio.All(probatio.Coerce(float), probatio.Range(min=0, max=100)),
        }
    )


def _labels_schema(labels: list[str] | None) -> probatio.Schema:
    """Создать список выбора известных меток или текстовое поле для ручного ввода."""
    if labels:
        selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=label, label=label) for label in labels
                ],
                multiple=True,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        default: list[str] | str = []
    else:
        selector = TextSelector(TextSelectorConfig(multiline=True))
        default = ""
    return probatio.Schema({probatio.Required(CONF_LABELS, default=default): selector})


def _profile_schema(
    profiles: list[dict[str, Any]],
    current_profile_id: str | None,
) -> probatio.Schema:
    """Создать список профилей с безопасным вариантом оставить legacy-фильтр."""
    options = [SelectOptionDict(value=NO_PROFILE_VALUE, label="Без профиля")]
    profile_ids: set[str] = set()
    for profile in profiles:
        profile_id = profile["id"]
        profile_ids.add(profile_id)
        options.append(
            SelectOptionDict(
                value=profile_id,
                label=f"{profile['name']} ({profile_id})",
            )
        )
    if current_profile_id and current_profile_id not in profile_ids:
        options.append(
            SelectOptionDict(
                value=current_profile_id,
                label=f"Сохранённый профиль недоступен ({current_profile_id})",
            )
        )
    default = current_profile_id or NO_PROFILE_VALUE
    return probatio.Schema(
        {
            probatio.Required(CONF_PROFILE_ID, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _motion_sensor_schema(
    candidates: list[dict[str, str]],
    current_entity_id: str | None,
) -> probatio.Schema:
    """Ограничить выбор сенсорами, зарегистрированными на устройстве камеры."""
    candidate_ids = {candidate["entity_id"] for candidate in candidates}
    if not candidates or (
        current_entity_id and current_entity_id not in candidate_ids
    ):
        return MOTION_SENSOR_SCHEMA

    selector = SelectSelector(
        SelectSelectorConfig(
            options=[
                SelectOptionDict(
                    value=candidate["entity_id"],
                    label=f"{candidate['name']} ({candidate['entity_id']})",
                )
                for candidate in candidates
            ],
            mode=SelectSelectorMode.DROPDOWN,
        )
    )
    if len(candidates) == 1:
        default = current_entity_id or candidates[0]["entity_id"]
        return probatio.Schema(
            {probatio.Required(CONF_MOTION_SENSOR, default=default): selector}
        )
    return probatio.Schema({probatio.Optional(CONF_MOTION_SENSOR): selector})


async def _async_get_available_profiles(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
) -> list[dict[str, Any]]:
    """Загрузить профили только при объявленной поддержке и не ломать legacy-серверы."""
    if not auth_key:
        return []
    features = await async_get_capabilities(hass, base_url, auth_key)
    if not _PROFILE_FEATURES.issubset(features):
        return []
    try:
        return await async_get_profiles(hass, base_url, auth_key)
    except DooodsApiError as err:
        _LOGGER.debug("Не удалось получить профили DOOODS2: %s", err)
        return []


def _lines(value: str) -> list[str]:
    """Преобразовать многострочное поле формы в список непустых строк."""
    return [line.strip() for line in value.splitlines() if line.strip()]


def _multiline(value: Any) -> str:
    """Подготовить сохранённый список или строку для многострочного текстового поля."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return ""


def _label_values(value: Any) -> list[str]:
    """Привести метки из списка выбора или старого текстового поля к единому виду."""
    if isinstance(value, str):
        return _lines(value)
    if isinstance(value, list):
        return list(
            dict.fromkeys(
                item.strip() for item in value if isinstance(item, str) and item.strip()
            )
        )
    return []


def _normalize_motion_sensors(
    value: Any,
    camera_entity_ids: list[str],
) -> dict[str, str]:
    """Оставить только настройки датчиков движения для выбранных камер."""
    if not isinstance(value, Mapping):
        return {}
    return {
        camera_entity_id: motion_entity_id.strip()
        for camera_entity_id in camera_entity_ids
        if isinstance(camera_entity_id, str)
        and isinstance(
            motion_entity_id := value.get(camera_entity_id),
            str,
        )
        and motion_entity_id.strip()
    }


def _camera_display_name(hass: HomeAssistant, camera_entity_id: str) -> str:
    """Получить отображаемое имя камеры для подсказки по настройке датчика движения."""
    state = hass.states.get(camera_entity_id)
    if state is not None:
        friendly_name = state.attributes.get("friendly_name")
        if isinstance(friendly_name, str) and friendly_name.strip():
            return friendly_name.strip()
    return camera_entity_id


def _labels_for_detector(
    detectors: list[dict[str, Any]] | None,
    detector_name: str,
) -> list[str] | None:
    """Вернуть известные метки детектора либо None, если список недоступен."""
    if detectors is None:
        return None
    for detector in detectors:
        if detector["name"] == detector_name:
            return detector["labels"]
    return None


def _options_for_form(options: Mapping[str, Any]) -> dict[str, Any]:
    """Подготовить сохранённые параметры и значения по умолчанию для экранов мастера."""
    area = options.get(CONF_AREA, {})
    cameras = options.get(CONF_SOURCE, [])
    if not isinstance(cameras, list):
        cameras = []
    return {
        CONF_SOURCE: cameras,
        CONF_MOTION_SENSORS: _normalize_motion_sensors(
            options.get(CONF_MOTION_SENSORS, {}), cameras
        ),
        CONF_CAMERA_PROFILES: normalize_camera_profiles(
            options.get(CONF_CAMERA_PROFILES, {}), cameras
        ),
        CONF_DETECTOR: options.get(CONF_DETECTOR, ""),
        CONF_TIMEOUT: options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        CONF_SCAN_INTERVAL: options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        CONF_CONFIDENCE: options.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE),
        CONF_LABELS: _label_values(options.get(CONF_LABELS, [])),
        CONF_AREA: {**DEFAULT_AREA, **area}
        if isinstance(area, Mapping)
        else dict(DEFAULT_AREA),
        CONF_FILE_OUT: normalize_archive_directory(options.get(CONF_FILE_OUT, "")),
        CONF_ARCHIVE_MAX_COUNT: normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_COUNT, DEFAULT_ARCHIVE_MAX_COUNT),
            DEFAULT_ARCHIVE_MAX_COUNT,
        ),
        CONF_ARCHIVE_MAX_AGE_DAYS: normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_AGE_DAYS, DEFAULT_ARCHIVE_MAX_AGE_DAYS),
            DEFAULT_ARCHIVE_MAX_AGE_DAYS,
        ),
    }


def _normalize_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Преобразовать значения мастера в типы, сохраняемые в параметрах записи интеграции."""
    area = options.get(CONF_AREA, {})
    cameras = options.get(CONF_SOURCE, [])
    if not isinstance(cameras, list):
        cameras = []
    normalized_area = (
        {**DEFAULT_AREA, **area} if isinstance(area, Mapping) else dict(DEFAULT_AREA)
    )
    return {
        CONF_SOURCE: cameras,
        CONF_MOTION_SENSORS: _normalize_motion_sensors(
            options.get(CONF_MOTION_SENSORS, {}), cameras
        ),
        CONF_CAMERA_PROFILES: normalize_camera_profiles(
            options.get(CONF_CAMERA_PROFILES, {}), cameras
        ),
        CONF_DETECTOR: options.get(CONF_DETECTOR, ""),
        CONF_TIMEOUT: options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
        CONF_SCAN_INTERVAL: options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        CONF_CONFIDENCE: options.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE),
        CONF_LABELS: _label_values(options.get(CONF_LABELS, [])),
        CONF_AREA: normalized_area,
        CONF_FILE_OUT: normalize_archive_directory(options.get(CONF_FILE_OUT, "")),
        CONF_ARCHIVE_MAX_COUNT: normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_COUNT, DEFAULT_ARCHIVE_MAX_COUNT),
            DEFAULT_ARCHIVE_MAX_COUNT,
        ),
        CONF_ARCHIVE_MAX_AGE_DAYS: normalize_archive_limit(
            options.get(CONF_ARCHIVE_MAX_AGE_DAYS, DEFAULT_ARCHIVE_MAX_AGE_DAYS),
            DEFAULT_ARCHIVE_MAX_AGE_DAYS,
        ),
    }


class DooodsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Управлять первоначальной настройкой и перенастройкой записи DOOODS."""

    VERSION = 1

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Получить параметры сервера и продолжить мастер настройки."""
        errors: dict[str, str] = {}
        if user_input is not None:
            server_url = _normalize_server_url(user_input[CONF_URL])
            if server_url is None:
                errors["base"] = "invalid_url"
            else:
                await self.async_set_unique_id(server_url)
                self._abort_if_unique_id_configured()
                auth_key = user_input.get(CONF_AUTH_KEY, "").strip()
                (
                    self._available_detectors,
                    connection_error,
                ) = await _async_get_detectors(
                    self.hass,
                    server_url,
                    auth_key,
                )
                if connection_error is not None:
                    errors["base"] = connection_error
                elif not self._available_detectors:
                    errors["base"] = "no_detectors"
                else:
                    self._server_data = {
                        CONF_URL: server_url,
                        CONF_AUTH_KEY: auth_key,
                    }
                    self._draft_options = _options_for_form({})
                    self._draft_options[CONF_DETECTOR] = self._available_detectors[0][
                        "name"
                    ]
                    return await self.async_step_cameras()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    @override
    async def async_step_cameras(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Получить список камер и интервал периодического сканирования."""
        if user_input is not None:
            if not user_input.get(CONF_SOURCE):
                return self.async_show_form(
                    step_id="cameras",
                    data_schema=self.add_suggested_values_to_schema(
                        CAMERA_SCHEDULE_SCHEMA,
                        user_input,
                    ),
                    errors={"base": "no_cameras"},
                )
            self._draft_options.update(user_input)
            self._draft_options[CONF_MOTION_SENSORS] = _normalize_motion_sensors(
                self._draft_options.get(CONF_MOTION_SENSORS, {}),
                self._draft_options[CONF_SOURCE],
            )
            self._motion_camera_index = 0
            return await self.async_step_motion()

        return self.async_show_form(
            step_id="cameras",
            data_schema=self.add_suggested_values_to_schema(
                CAMERA_SCHEDULE_SCHEMA,
                self._draft_options,
            ),
        )

    @override
    async def async_step_motion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Найти сенсор движения камеры или предложить выбрать внешний."""
        cameras = self._draft_options.get(CONF_SOURCE, [])
        if not cameras:
            return await self.async_step_detection()

        camera_index = getattr(self, "_motion_camera_index", 0)
        motion_sensors = dict(self._draft_options.get(CONF_MOTION_SENSORS, {}))
        if user_input is not None:
            camera_entity_id = cameras[camera_index]
            motion_entity_id = user_input.get(CONF_MOTION_SENSOR)
            if isinstance(motion_entity_id, str) and motion_entity_id.strip():
                motion_sensors[camera_entity_id] = motion_entity_id.strip()
            else:
                motion_sensors.pop(camera_entity_id, None)
            self._draft_options[CONF_MOTION_SENSORS] = motion_sensors
            camera_index += 1
            if camera_index >= len(cameras):
                self._motion_camera_index = 0
                self._profile_camera_index = 0
                self._available_profiles = await _async_get_available_profiles(
                    self.hass,
                    self._server_data[CONF_URL],
                    self._server_data.get(CONF_AUTH_KEY, ""),
                )
                return await self.async_step_profiles()
            self._motion_camera_index = camera_index

        camera_entity_id = cameras[camera_index]
        motion_entity_id = motion_sensors.get(camera_entity_id)
        motion_candidates = camera_motion_sensors(self.hass, camera_entity_id)
        candidate_ids = {candidate["entity_id"] for candidate in motion_candidates}
        if motion_candidates and motion_entity_id not in candidate_ids:
            motion_entity_id = (
                motion_candidates[0]["entity_id"]
                if len(motion_candidates) == 1
                else None
            )
        suggested_values = (
            {CONF_MOTION_SENSOR: motion_entity_id} if motion_entity_id else {}
        )
        camera_name = _camera_display_name(self.hass, camera_entity_id)
        return self.async_show_form(
            step_id="motion",
            data_schema=self.add_suggested_values_to_schema(
                _motion_sensor_schema(motion_candidates, motion_entity_id),
                suggested_values,
            ),
            description_placeholders={
                "camera_name": camera_name,
                "camera_entity_id": camera_entity_id,
                "camera_index": str(camera_index + 1),
                "camera_count": str(len(cameras)),
            },
        )

    @override
    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Назначить каждой камере один профиль полигональной фильтрации."""
        cameras = self._draft_options.get(CONF_SOURCE, [])
        if not cameras:
            return await self.async_step_detection()

        profiles = getattr(self, "_available_profiles", [])
        profile_map = normalize_camera_profiles(
            self._draft_options.get(CONF_CAMERA_PROFILES, {}),
            cameras,
        )
        if not profiles and not profile_map:
            self._draft_options[CONF_CAMERA_PROFILES] = {}
            return await self.async_step_detection()

        camera_index = getattr(self, "_profile_camera_index", 0)
        if user_input is not None:
            camera_entity_id = cameras[camera_index]
            selected_profile_id = user_input.get(CONF_PROFILE_ID, NO_PROFILE_VALUE)
            if selected_profile_id == NO_PROFILE_VALUE:
                profile_map.pop(camera_entity_id, None)
            else:
                profile_map[camera_entity_id] = selected_profile_id
            self._draft_options[CONF_CAMERA_PROFILES] = profile_map
            camera_index += 1
            if camera_index >= len(cameras):
                self._profile_camera_index = 0
                return await self.async_step_detection()
            self._profile_camera_index = camera_index

        camera_entity_id = cameras[camera_index]
        current_profile_id = profile_map.get(camera_entity_id)
        return self.async_show_form(
            step_id="profiles",
            data_schema=self.add_suggested_values_to_schema(
                _profile_schema(profiles, current_profile_id),
                {
                    CONF_PROFILE_ID: current_profile_id or NO_PROFILE_VALUE,
                },
            ),
            description_placeholders={
                "camera_name": _camera_display_name(self.hass, camera_entity_id),
                "camera_entity_id": camera_entity_id,
                "camera_index": str(camera_index + 1),
                "camera_count": str(len(cameras)),
            },
        )

    @override
    async def async_step_detection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Выбрать детектор и сохранить значения порога уверенности и тайм-аута по умолчанию."""
        detector_names = [detector["name"] for detector in self._available_detectors]
        if user_input is not None:
            previous_detector = self._draft_options.get(CONF_DETECTOR)
            self._draft_options.update(user_input)
            if self._draft_options[CONF_DETECTOR] != previous_detector:
                self._draft_options[CONF_LABELS] = []
            return await self.async_step_labels()

        selected_detector = self._draft_options.get(CONF_DETECTOR)
        if selected_detector not in detector_names:
            selected_detector = detector_names[0]
            self._draft_options[CONF_DETECTOR] = selected_detector
            self._draft_options[CONF_LABELS] = []

        return self.async_show_form(
            step_id="detection",
            data_schema=self.add_suggested_values_to_schema(
                _detector_schema(self._available_detectors, selected_detector),
                self._draft_options,
            ),
        )

    @override
    async def async_step_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Выбрать метки, поддерживаемые выбранным детектором."""
        if user_input is not None:
            self._draft_options.update(user_input)
            self._draft_options[CONF_LABELS] = _label_values(
                self._draft_options.get(CONF_LABELS, [])
            )
            return await self.async_step_area()

        selected_detector = self._draft_options[CONF_DETECTOR]
        available_labels = _labels_for_detector(
            self._available_detectors,
            selected_detector,
        )
        selected_labels = _label_values(self._draft_options.get(CONF_LABELS, []))
        if available_labels:
            selected_labels = [
                label for label in selected_labels if label in available_labels
            ]
            suggested_values = {**self._draft_options, CONF_LABELS: selected_labels}
        else:
            suggested_values = {
                **self._draft_options,
                CONF_LABELS: _multiline(selected_labels),
            }

        return self.async_show_form(
            step_id="labels",
            data_schema=self.add_suggested_values_to_schema(
                _labels_schema(available_labels),
                suggested_values,
            ),
        )

    @override
    async def async_step_area(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Получить общую область распознавания."""
        if user_input is not None:
            self._draft_options.update(user_input)
            return await self.async_step_output()

        return self.async_show_form(
            step_id="area",
            data_schema=self.add_suggested_values_to_schema(
                AREA_OPTIONS_SCHEMA,
                self._draft_options,
            ),
        )

    @override
    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Получить пути вывода и создать запись без обращения к сети."""
        if user_input is not None:
            self._draft_options.update(user_input)
            return self.async_create_entry(
                title=urlsplit(self._server_data[CONF_URL]).netloc,
                data=self._server_data,
                options=_normalize_options(self._draft_options),
            )

        return self.async_show_form(
            step_id="output",
            data_schema=self.add_suggested_values_to_schema(
                OUTPUT_SCHEMA,
                self._draft_options,
            ),
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить URL сервера или сохранённый ключ после проверки списка детекторов."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            server_url = _normalize_server_url(user_input[CONF_URL])
            if server_url is None:
                errors["base"] = "invalid_url"
            elif any(
                other.entry_id != entry.entry_id
                and _normalize_server_url(other.data.get(CONF_URL, "")) == server_url
                for other in self.hass.config_entries.async_entries(DOMAIN)
            ):
                errors["base"] = "already_configured"
            else:
                auth_key = user_input.get(CONF_AUTH_KEY, "").strip()
                detectors, connection_error = await _async_get_detectors(
                    self.hass,
                    server_url,
                    auth_key,
                )
                if connection_error is not None:
                    errors["base"] = connection_error
                elif not detectors:
                    errors["base"] = "no_detectors"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=server_url,
                        title=urlsplit(server_url).netloc,
                        data_updates={CONF_URL: server_url, CONF_AUTH_KEY: auth_key},
                    )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                RECONFIGURE_SCHEMA,
                user_input or entry.data,
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DooodsOptionsFlow:
        """Создать мастер параметров для записи интеграции."""
        return DooodsOptionsFlow()


class DooodsOptionsFlow(OptionsFlowWithReload):
    """Сохранять параметры настройки, введённые на нескольких экранах мастера."""

    def _get_draft_options(self) -> dict[str, Any]:
        """Вернуть текущий черновик, заполненный сохранёнными параметрами интеграции."""
        if not hasattr(self, "_draft_options"):
            self._draft_options = _options_for_form(self.config_entry.options)
        return self._draft_options

    async def _async_get_available_detectors(
        self,
    ) -> tuple[list[dict[str, Any]] | None, str | None]:
        """Получить метаданные детекторов один раз за цикл мастера параметров."""
        if not hasattr(self, "_available_detectors"):
            (
                self._available_detectors,
                self._detector_error,
            ) = await _async_get_detectors(
                self.hass,
                self.config_entry.data[CONF_URL],
                self.config_entry.data.get(CONF_AUTH_KEY, ""),
            )
            if not self._available_detectors and self._detector_error is None:
                self._detector_error = "no_detectors"
        return self._available_detectors, self._detector_error

    async def _async_get_available_profiles(self) -> list[dict[str, Any]]:
        """Получить поддерживаемые профили один раз за цикл мастера параметров."""
        if not hasattr(self, "_available_profiles"):
            self._available_profiles = await _async_get_available_profiles(
                self.hass,
                self.config_entry.data[CONF_URL],
                self.config_entry.data.get(CONF_AUTH_KEY, ""),
            )
        return self._available_profiles

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить список камер и расписание сканирования."""
        draft = self._get_draft_options()
        if user_input is not None:
            if not user_input.get(CONF_SOURCE):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self.add_suggested_values_to_schema(
                        CAMERA_SCHEDULE_SCHEMA,
                        user_input,
                    ),
                    errors={"base": "no_cameras"},
                )
            draft.update(user_input)
            draft[CONF_MOTION_SENSORS] = _normalize_motion_sensors(
                draft.get(CONF_MOTION_SENSORS, {}),
                draft[CONF_SOURCE],
            )
            self._motion_camera_index = 0
            return await self.async_step_motion()

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                CAMERA_SCHEDULE_SCHEMA,
                draft,
            ),
        )

    @override
    async def async_step_motion(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Найти сенсор движения камеры или предложить выбрать внешний."""
        draft = self._get_draft_options()
        cameras = draft.get(CONF_SOURCE, [])
        if not cameras:
            return await self.async_step_detection()

        camera_index = getattr(self, "_motion_camera_index", 0)
        motion_sensors = dict(draft.get(CONF_MOTION_SENSORS, {}))
        if user_input is not None:
            camera_entity_id = cameras[camera_index]
            motion_entity_id = user_input.get(CONF_MOTION_SENSOR)
            if isinstance(motion_entity_id, str) and motion_entity_id.strip():
                motion_sensors[camera_entity_id] = motion_entity_id.strip()
            else:
                motion_sensors.pop(camera_entity_id, None)
            draft[CONF_MOTION_SENSORS] = motion_sensors
            camera_index += 1
            if camera_index >= len(cameras):
                self._motion_camera_index = 0
                self._profile_camera_index = 0
                await self._async_get_available_profiles()
                return await self.async_step_profiles()
            self._motion_camera_index = camera_index

        camera_entity_id = cameras[camera_index]
        motion_entity_id = motion_sensors.get(camera_entity_id)
        motion_candidates = camera_motion_sensors(self.hass, camera_entity_id)
        candidate_ids = {candidate["entity_id"] for candidate in motion_candidates}
        if motion_candidates and motion_entity_id not in candidate_ids:
            motion_entity_id = (
                motion_candidates[0]["entity_id"]
                if len(motion_candidates) == 1
                else None
            )
        suggested_values = (
            {CONF_MOTION_SENSOR: motion_entity_id} if motion_entity_id else {}
        )
        return self.async_show_form(
            step_id="motion",
            data_schema=self.add_suggested_values_to_schema(
                _motion_sensor_schema(motion_candidates, motion_entity_id),
                suggested_values,
            ),
            description_placeholders={
                "camera_name": _camera_display_name(self.hass, camera_entity_id),
                "camera_entity_id": camera_entity_id,
                "camera_index": str(camera_index + 1),
                "camera_count": str(len(cameras)),
            },
        )

    @override
    async def async_step_profiles(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Назначить выбранный профиль полигональной фильтрации каждой камере."""
        draft = self._get_draft_options()
        cameras = draft.get(CONF_SOURCE, [])
        if not cameras:
            return await self.async_step_detection()

        profiles = await self._async_get_available_profiles()
        profile_map = normalize_camera_profiles(
            draft.get(CONF_CAMERA_PROFILES, {}),
            cameras,
        )
        if not profiles and not profile_map:
            draft[CONF_CAMERA_PROFILES] = {}
            return await self.async_step_detection()

        camera_index = getattr(self, "_profile_camera_index", 0)
        if user_input is not None:
            camera_entity_id = cameras[camera_index]
            selected_profile_id = user_input.get(CONF_PROFILE_ID, NO_PROFILE_VALUE)
            if selected_profile_id == NO_PROFILE_VALUE:
                profile_map.pop(camera_entity_id, None)
            else:
                profile_map[camera_entity_id] = selected_profile_id
            draft[CONF_CAMERA_PROFILES] = profile_map
            camera_index += 1
            if camera_index >= len(cameras):
                self._profile_camera_index = 0
                return await self.async_step_detection()
            self._profile_camera_index = camera_index

        camera_entity_id = cameras[camera_index]
        current_profile_id = profile_map.get(camera_entity_id)
        return self.async_show_form(
            step_id="profiles",
            data_schema=self.add_suggested_values_to_schema(
                _profile_schema(profiles, current_profile_id),
                {
                    CONF_PROFILE_ID: current_profile_id or NO_PROFILE_VALUE,
                },
            ),
            description_placeholders={
                "camera_name": _camera_display_name(self.hass, camera_entity_id),
                "camera_entity_id": camera_entity_id,
                "camera_index": str(camera_index + 1),
                "camera_count": str(len(cameras)),
            },
        )

    @override
    async def async_step_detection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить детектор, тайм-аут и порог уверенности."""
        draft = self._get_draft_options()
        detectors, detector_error = await self._async_get_available_detectors()
        if user_input is not None:
            previous_detector = draft.get(CONF_DETECTOR, "")
            draft.update(user_input)
            if draft[CONF_DETECTOR] != previous_detector:
                draft[CONF_LABELS] = []
            return await self.async_step_labels()

        errors: dict[str, str] = {}
        if detectors:
            detector_names = [detector["name"] for detector in detectors]
            selected_detector = draft.get(CONF_DETECTOR, "")
            if selected_detector not in detector_names:
                if selected_detector:
                    errors["base"] = "detector_not_found"
                selected_detector = detector_names[0]
                draft[CONF_DETECTOR] = selected_detector
                draft[CONF_LABELS] = []
            schema = _detector_schema(detectors, selected_detector)
        else:
            schema = _manual_detection_schema(draft.get(CONF_DETECTOR, ""))
            if detector_error is not None:
                errors["base"] = detector_error

        return self.async_show_form(
            step_id="detection",
            data_schema=self.add_suggested_values_to_schema(schema, draft),
            errors=errors,
        )

    @override
    async def async_step_labels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить метки, поддерживаемые выбранным детектором."""
        draft = self._get_draft_options()
        detectors, detector_error = await self._async_get_available_detectors()
        if user_input is not None:
            draft.update(user_input)
            draft[CONF_LABELS] = _label_values(draft.get(CONF_LABELS, []))
            return await self.async_step_area()

        available_labels = _labels_for_detector(
            detectors,
            draft.get(CONF_DETECTOR, ""),
        )
        selected_labels = _label_values(draft.get(CONF_LABELS, []))
        if available_labels:
            selected_labels = [
                label for label in selected_labels if label in available_labels
            ]
            suggested_values = {**draft, CONF_LABELS: selected_labels}
        else:
            suggested_values = {**draft, CONF_LABELS: _multiline(selected_labels)}

        errors = {"base": detector_error} if detector_error else {}
        return self.async_show_form(
            step_id="labels",
            data_schema=self.add_suggested_values_to_schema(
                _labels_schema(available_labels),
                suggested_values,
            ),
            errors=errors,
        )

    @override
    async def async_step_area(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить общую область распознавания."""
        draft = self._get_draft_options()
        if user_input is not None:
            draft.update(user_input)
            return await self.async_step_output()

        return self.async_show_form(
            step_id="area",
            data_schema=self.add_suggested_values_to_schema(
                AREA_OPTIONS_SCHEMA,
                draft,
            ),
        )

    @override
    async def async_step_output(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить пути вывода и сохранить параметры интеграции."""
        draft = self._get_draft_options()
        if user_input is not None:
            draft.update(user_input)
            return self.async_create_entry(
                title="",
                data=_normalize_options(draft),
            )

        return self.async_show_form(
            step_id="output",
            data_schema=self.add_suggested_values_to_schema(
                OUTPUT_SCHEMA,
                draft,
            ),
        )
