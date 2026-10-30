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
    DooodsAuthError,
    DooodsConnectionError,
    DooodsInvalidResponseError,
    async_get_detectors,
)
from .archive import normalize_archive_directory, normalize_archive_limit
from .camera_scan_intervals import normalize_camera_scan_intervals
from .camera_switches import normalize_camera_switches
from .const import (
    CONF_AREA,
    CONF_ARCHIVE_MAX_AGE_DAYS,
    CONF_ARCHIVE_MAX_COUNT,
    CONF_AUTH_KEY,
    CONF_CAMERA_DETECTION_ENABLED,
    CONF_CAMERA_PROFILES,
    CONF_CAMERA_SCAN_INTERVALS,
    CONF_CAMERA_STREAM_ENABLED,
    CONF_BOTTOM,
    CONF_CONFIDENCE,
    CONF_COVERS,
    CONF_DETECTOR,
    CONF_FILE_OUT,
    CONF_LABELS,
    CONF_LEFT,
    CONF_MOTION_SENSORS,
    CONF_RIGHT,
    CONF_SOURCE,
    CONF_TIMEOUT,
    CONF_TOP,
    CONF_URL,
    DEFAULT_ARCHIVE_MAX_AGE_DAYS,
    DEFAULT_ARCHIVE_MAX_COUNT,
    DEFAULT_AREA,
    DEFAULT_CONFIDENCE,
    DEFAULT_TIMEOUT,
    DOMAIN,
)
from .motion import resolve_motion_sensors
from .profiles import normalize_camera_profiles

_LOGGER = logging.getLogger(__name__)

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

CAMERA_SELECTION_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SOURCE, default=[]): EntitySelector(
            EntitySelectorConfig(domain="camera", multiple=True)
        ),
    }
)

AREA_OPTIONS_FIELDS = {
    probatio.Required(CONF_AREA, default=dict(DEFAULT_AREA)): section(
        AREA_SCHEMA,
        {"collapsed": True},
    ),
}
AREA_OPTIONS_SCHEMA = probatio.Schema(AREA_OPTIONS_FIELDS)

OUTPUT_FIELDS = {
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
OUTPUT_SCHEMA = probatio.Schema(OUTPUT_FIELDS)


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


def _detector_fields(
    detectors: list[dict[str, Any]],
    default_detector: str,
) -> dict[Any, Any]:
    """Создать поле выбора детектора, возвращённого сервером DOODS2."""
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
    return {
        probatio.Required(CONF_DETECTOR, default=default_detector): SelectSelector(
            SelectSelectorConfig(
                options=options,
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
    }


def _manual_detection_fields(default_detector: str) -> dict[Any, Any]:
    """Оставить возможность изменить имя детектора при недоступности сервера."""
    return {
        probatio.Required(CONF_DETECTOR, default=default_detector): TextSelector(
            TextSelectorConfig()
        )
    }


def _options_settings_schema(detector_fields: Mapping[Any, Any]) -> probatio.Schema:
    """Объединить детектор, область распознавания и настройки архива в одну форму."""
    fields = dict(detector_fields)
    fields.update(AREA_OPTIONS_FIELDS)
    fields.update(OUTPUT_FIELDS)
    return probatio.Schema(fields)


def _lines(value: str) -> list[str]:
    """Преобразовать многострочное поле формы в список непустых строк."""
    return [line.strip() for line in value.splitlines() if line.strip()]


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


def _first_person_detector(
    detectors: list[dict[str, Any]],
) -> tuple[dict[str, Any], str] | None:
    """Выбрать первый детектор с меткой person и вернуть точное имя этой метки."""
    for detector in detectors:
        labels = detector.get("labels", [])
        person_label = next(
            (
                label
                for label in labels
                if isinstance(label, str) and label.strip().casefold() == "person"
            ),
            None,
        )
        if person_label is not None:
            return detector, person_label
    return None


def _options_for_form(options: Mapping[str, Any]) -> dict[str, Any]:
    """Подготовить сохранённые параметры и значения по умолчанию для мастеров."""
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
        CONF_CAMERA_SCAN_INTERVALS: normalize_camera_scan_intervals(
            options.get(CONF_CAMERA_SCAN_INTERVALS, {}), cameras
        ),
        CONF_CAMERA_DETECTION_ENABLED: normalize_camera_switches(
            options.get(CONF_CAMERA_DETECTION_ENABLED, {}),
            cameras,
            default=None,
        ),
        CONF_CAMERA_STREAM_ENABLED: normalize_camera_switches(
            options.get(CONF_CAMERA_STREAM_ENABLED, {}),
            cameras,
            default=None,
        ),
        CONF_DETECTOR: options.get(CONF_DETECTOR, ""),
        CONF_TIMEOUT: options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
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
        CONF_CAMERA_SCAN_INTERVALS: normalize_camera_scan_intervals(
            options.get(CONF_CAMERA_SCAN_INTERVALS, {}), cameras
        ),
        CONF_CAMERA_DETECTION_ENABLED: normalize_camera_switches(
            options.get(CONF_CAMERA_DETECTION_ENABLED, {}),
            cameras,
            default=None,
        ),
        CONF_CAMERA_STREAM_ENABLED: normalize_camera_switches(
            options.get(CONF_CAMERA_STREAM_ENABLED, {}),
            cameras,
            default=None,
        ),
        CONF_DETECTOR: options.get(CONF_DETECTOR, ""),
        CONF_TIMEOUT: options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT),
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
                elif (
                    person_detector := _first_person_detector(self._available_detectors)
                ) is None:
                    errors["base"] = "no_person_detector"
                else:
                    detector, person_label = person_detector
                    self._server_data = {
                        CONF_URL: server_url,
                        CONF_AUTH_KEY: auth_key,
                    }
                    self._draft_options = _options_for_form(
                        {
                            CONF_DETECTOR: detector["name"],
                            CONF_CONFIDENCE: 50,
                            CONF_LABELS: [person_label],
                            CONF_AREA: dict(DEFAULT_AREA),
                        }
                    )
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
        """Получить список камер для текущей записи интеграции."""
        if user_input is not None:
            if not user_input.get(CONF_SOURCE):
                return self.async_show_form(
                    step_id="cameras",
                    data_schema=self.add_suggested_values_to_schema(
                        CAMERA_SELECTION_SCHEMA,
                        user_input,
                    ),
                    errors={"base": "no_cameras"},
                )
            self._draft_options.update(user_input)
            cameras = self._draft_options[CONF_SOURCE]
            self._draft_options[CONF_MOTION_SENSORS] = resolve_motion_sensors(
                self.hass,
                cameras,
                _normalize_motion_sensors(
                    self._draft_options.get(CONF_MOTION_SENSORS, {}), cameras
                ),
            )
            self._draft_options[CONF_CAMERA_PROFILES] = normalize_camera_profiles(
                self._draft_options.get(CONF_CAMERA_PROFILES, {}),
                cameras,
            )
            return await self.async_step_output()

        return self.async_show_form(
            step_id="cameras",
            data_schema=self.add_suggested_values_to_schema(
                CAMERA_SELECTION_SCHEMA,
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
    """Изменять общие параметры и сохранять профильные настройки камер."""

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

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить камеры и перейти к общим настройкам детектора и архива."""
        draft = self._get_draft_options()
        if user_input is not None:
            if not user_input.get(CONF_SOURCE):
                return self.async_show_form(
                    step_id="init",
                    data_schema=self.add_suggested_values_to_schema(
                        CAMERA_SELECTION_SCHEMA,
                        user_input,
                    ),
                    errors={"base": "no_cameras"},
                )
            draft.update(user_input)
            cameras = draft[CONF_SOURCE]
            draft[CONF_MOTION_SENSORS] = resolve_motion_sensors(
                self.hass,
                cameras,
                _normalize_motion_sensors(
                    draft.get(CONF_MOTION_SENSORS, {}), cameras
                ),
            )
            draft[CONF_CAMERA_PROFILES] = normalize_camera_profiles(
                draft.get(CONF_CAMERA_PROFILES, {}),
                cameras,
            )
            return await self.async_step_settings()

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                CAMERA_SELECTION_SCHEMA,
                draft,
            ),
        )

    @override
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Изменить детектор и настройки архива на одной форме."""
        draft = self._get_draft_options()
        detectors, detector_error = await self._async_get_available_detectors()
        if user_input is not None:
            previous_detector = draft.get(CONF_DETECTOR, "")
            draft.update(user_input)
            if draft.get(CONF_DETECTOR) != previous_detector:
                # Старый список меток может не относиться к новому детектору.
                draft[CONF_LABELS] = []
            return self.async_create_entry(
                title="",
                data=_normalize_options(draft),
            )

        errors: dict[str, str] = {}
        selected_detector = draft.get(CONF_DETECTOR, "")
        if detectors:
            detector_names = [detector["name"] for detector in detectors]
            if selected_detector not in detector_names:
                if selected_detector:
                    errors["base"] = "detector_not_found"
                selected_detector = detector_names[0]
                draft[CONF_DETECTOR] = selected_detector
                draft[CONF_LABELS] = []
            detector_fields = _detector_fields(detectors, selected_detector)
        else:
            detector_fields = _manual_detection_fields(selected_detector)
            if detector_error is not None:
                errors["base"] = detector_error

        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(
                _options_settings_schema(detector_fields),
                draft,
            ),
            errors=errors,
        )
