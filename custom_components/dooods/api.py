"""Асинхронные вспомогательные функции для HTTP API DOODS2."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BOTTOM,
    CONF_COVERS,
    CONF_LEFT,
    CONF_RIGHT,
    CONF_TOP,
    DOMAIN,
)


class DooodsApiError(HomeAssistantError):
    """Базовое исключение API с сообщением из файла переводов."""

    def __init__(
        self,
        translation_key: str,
        translation_placeholders: dict[str, str] | None = None,
    ) -> None:
        """Создать исключение по ключу перевода и его параметрам."""
        super().__init__(
            translation_domain=DOMAIN,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
        )


class DooodsConnectionError(DooodsApiError):
    """Исключение при недоступности сервера или ошибке HTTP."""


class DooodsAuthError(DooodsApiError):
    """Исключение при отклонении учётных данных сервером."""


class DooodsInvalidResponseError(DooodsApiError):
    """Исключение при неожиданной структуре ответа сервера."""


class DooodsDetectionError(DooodsApiError):
    """Исключение при ошибке получения снимка или распознавания."""


@dataclass(frozen=True)
class DooodsDetectionResult:
    """Результаты распознавания и необязательное размеченное сервером изображение одного кадра."""

    detections: list[dict[str, Any]]
    annotated_image: bytes | None


def _normalize_detectors(payload: Any) -> list[dict[str, Any]]:
    """Проверить метаданные детекторов и оставить поля, используемые мастерами настройки."""
    raw_detectors = payload.get("detectors") if isinstance(payload, dict) else None
    if not isinstance(raw_detectors, list):
        raise DooodsInvalidResponseError("missing_detector_list")

    detectors: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for detector in raw_detectors:
        if not isinstance(detector, dict):
            raise DooodsInvalidResponseError("invalid_detector_entry")
        name = detector.get("name")
        labels = detector.get("labels", [])
        if labels is None:
            labels = []
        if (
            not isinstance(name, str)
            or not name.strip()
            or name.strip() in seen_names
            or not isinstance(labels, list)
            or any(not isinstance(label, str) for label in labels)
        ):
            raise DooodsInvalidResponseError("invalid_detector_metadata")

        name = name.strip()
        seen_names.add(name)
        detectors.append(
            {
                "name": name,
                "type": str(detector.get("type") or "").strip(),
                "labels": list(
                    dict.fromkeys(label.strip() for label in labels if label.strip())
                ),
            }
        )
    return detectors


def _response_json_error(
    err: ValueError | aiohttp.ContentTypeError,
) -> DooodsInvalidResponseError:
    """Преобразовать некорректный JSON-ответ в исключение API интеграции."""
    return DooodsInvalidResponseError("invalid_json")


async def async_get_detectors(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
) -> list[dict[str, Any]]:
    """Получить с сервера DOODS2 названия детекторов, их типы и метки."""
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    try:
        async with async_get_clientsession(hass).get(
            f"{base_url.rstrip('/')}/detectors",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if response.status != 200:
                raise DooodsConnectionError(
                    "http_error",
                    {"endpoint": "GET /detectors", "status": str(response.status)},
                )
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise _response_json_error(err) from err
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err

    return _normalize_detectors(payload)


def _normalize_profiles(payload: Any) -> list[dict[str, Any]]:
    """Проверить список профилей и собрать метки зон для HA-сенсоров."""
    if not isinstance(payload, list):
        raise DooodsInvalidResponseError("invalid_profile_list")

    profiles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for profile in payload:
        if not isinstance(profile, dict):
            raise DooodsInvalidResponseError("invalid_profile_entry")
        profile_id = profile.get("id")
        name = profile.get("name")
        zones = profile.get("zones", [])
        if (
            not isinstance(profile_id, str)
            or not profile_id.strip()
            or profile_id.strip() in seen_ids
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(zones, list)
        ):
            raise DooodsInvalidResponseError("invalid_profile_metadata")

        labels: list[str] = []
        for zone in zones:
            if not isinstance(zone, dict):
                raise DooodsInvalidResponseError("invalid_profile_zone")
            zone_labels = zone.get("labels", {})
            if not isinstance(zone_labels, Mapping) or any(
                not isinstance(label, str) or not label.strip()
                for label in zone_labels
            ):
                raise DooodsInvalidResponseError("invalid_profile_labels")
            labels.extend(zone_labels)

        profile_id = profile_id.strip()
        seen_ids.add(profile_id)
        profiles.append(
            {
                "id": profile_id,
                "name": name.strip(),
                "labels": list(dict.fromkeys(labels)),
                "zone_count": len(zones),
            }
        )
    return profiles


async def async_get_profiles(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
) -> list[dict[str, Any]]:
    """Получить защищённый список профилей и меток, заданных в их зонах."""
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    try:
        async with async_get_clientsession(hass).get(
            f"{base_url.rstrip('/')}/profiles",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if response.status != 200:
                raise DooodsConnectionError(
                    "http_error",
                    {"endpoint": "GET /profiles", "status": str(response.status)},
                )
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise _response_json_error(err) from err
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err

    return _normalize_profiles(payload)


def _normalize_profile_details(payload: Any, expected_id: str) -> dict[str, Any]:
    """Проверить полный ответ API одного профиля, не теряя геометрию зон."""
    if not isinstance(payload, dict):
        raise DooodsInvalidResponseError("invalid_profile_entry")
    profile_id = payload.get("id")
    name = payload.get("name")
    zones = payload.get("zones")
    if (
        not isinstance(profile_id, str)
        or profile_id.strip() != expected_id
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(zones, list)
        or any(not isinstance(zone, dict) for zone in zones)
    ):
        raise DooodsInvalidResponseError("invalid_profile_metadata")
    return {"id": profile_id.strip(), "name": name.strip(), "zones": zones}


async def async_get_profile(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
    profile_id: str,
) -> dict[str, Any]:
    """Получить профиль вместе с зонами через серверную интеграцию."""
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    url = f"{base_url.rstrip('/')}/profiles/{quote(profile_id, safe='')}"
    try:
        async with async_get_clientsession(hass).get(
            url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if response.status == 404:
                raise DooodsInvalidResponseError("profile_not_found")
            if response.status != 200:
                raise DooodsConnectionError(
                    "http_error",
                    {"endpoint": "GET /profiles/{id}", "status": str(response.status)},
                )
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise _response_json_error(err) from err
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err
    return _normalize_profile_details(payload, profile_id)


async def async_put_profile(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Создать или полностью заменить профиль, не передавая ключ в браузер."""
    profile_id = profile.get("id")
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise DooodsInvalidResponseError("invalid_profile_metadata")
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    url = f"{base_url.rstrip('/')}/profiles/{quote(profile_id, safe='')}"
    try:
        async with async_get_clientsession(hass).put(
            url,
            json=dict(profile),
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if response.status == 404:
                raise DooodsInvalidResponseError("profile_not_found")
            if response.status in {400, 422}:
                raise DooodsInvalidResponseError("profile_save_rejected")
            if response.status != 200:
                raise DooodsConnectionError(
                    "http_error",
                    {"endpoint": "PUT /profiles/{id}", "status": str(response.status)},
                )
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                raise _response_json_error(err) from err
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err
    return _normalize_profile_details(payload, profile_id)


async def async_get_capabilities(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
) -> set[str]:
    """Необязательно обнаружить функции Overhaul, не ломая старый DOODS2."""
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    try:
        async with async_get_clientsession(hass).get(
            f"{base_url.rstrip('/')}/capabilities",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if response.status != 200:
                return set()
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                return set()
    except (aiohttp.ClientError, TimeoutError):
        return set()

    features = payload.get("features") if isinstance(payload, dict) else None
    if not isinstance(features, list):
        return set()
    return {feature for feature in features if isinstance(feature, str)}


async def async_put_preview(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
    *,
    source_id: str,
    source_name: str,
    image: bytes,
    timeout: float = 5,
) -> None:
    """Асинхронно отправить JPEG в защищённое временное хранилище Overhaul."""
    if not auth_key:
        raise DooodsAuthError("invalid_auth")

    source_name_b64 = base64.urlsafe_b64encode(source_name.encode("utf-8")).decode(
        "ascii"
    )
    headers = {
        "doods-auth-key": auth_key,
        "Content-Type": "image/jpeg",
        "X-Source-Name-B64": source_name_b64,
    }
    url = f"{base_url.rstrip('/')}/preview/{quote(source_id, safe='')}"
    try:
        async with async_get_clientsession(hass).put(
            url,
            data=image,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            if not 200 <= response.status < 300:
                raise DooodsConnectionError(
                    "http_error",
                    {"endpoint": "PUT /preview", "status": str(response.status)},
                )
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err


def _normalize_detections(payload: Any) -> list[dict[str, Any]]:
    """Проверить записи детекций, необходимые бинарным сенсорам по меткам."""
    if not isinstance(payload, dict):
        raise DooodsInvalidResponseError("invalid_detection_response")
    if error := payload.get("error"):
        raise DooodsDetectionError("detection_failed", {"details": str(error)})

    raw_detections = payload.get("detections")
    if not isinstance(raw_detections, list):
        raise DooodsInvalidResponseError("missing_detections_list")

    detections: list[dict[str, Any]] = []
    coordinate_names = ("top", "left", "bottom", "right")
    for detection in raw_detections:
        if not isinstance(detection, dict):
            raise DooodsInvalidResponseError("invalid_detection_entry")
        label = detection.get("label")
        confidence = detection.get("confidence")
        coordinates = {key: detection.get(key) for key in coordinate_names}
        if (
            not isinstance(label, str)
            or not label
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in coordinates.values()
            )
        ):
            raise DooodsInvalidResponseError("invalid_detection_metadata")
        region_id = detection.get("region_id")
        if region_id is not None and not isinstance(region_id, str):
            raise DooodsInvalidResponseError("invalid_detection_metadata")
        normalized_detection = {
            "label": label,
            "confidence": float(confidence),
            **{key: float(value) for key, value in coordinates.items()},
        }
        if region_id:
            normalized_detection["region_id"] = region_id
        detections.append(normalized_detection)
    return detections


def _decode_annotated_image(payload: Any) -> bytes | None:
    """Декодировать изображение Base64, если сервер DOODS2 его вернул."""
    if not isinstance(payload, str) or not payload:
        return None
    try:
        image = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    return image or None


async def async_detect(
    hass: HomeAssistant,
    base_url: str,
    auth_key: str,
    *,
    detector_name: str,
    image: bytes,
    labels: list[str],
    confidence: float,
    area: Mapping[str, Any],
    timeout: int,
    profile_id: str | None = None,
    annotation_locale: str | None = None,
) -> DooodsDetectionResult:
    """Отправить снимок камеры и запросить у DOODS2 размеченное изображение JPEG."""
    allowed_labels = labels or ["*"]
    region = {
        "id": "global",
        "top": area.get(CONF_TOP, 0.0),
        "left": area.get(CONF_LEFT, 0.0),
        "bottom": area.get(CONF_BOTTOM, 1.0),
        "right": area.get(CONF_RIGHT, 1.0),
        "covers": area.get(CONF_COVERS, True),
        "detect": {label: confidence for label in allowed_labels},
    }
    request_payload = {
        "detector_name": detector_name,
        "data": base64.b64encode(image).decode("ascii"),
        "image": ".jpg",
        "detect": {},
    }
    if annotation_locale:
        request_payload["annotation_locale"] = annotation_locale
    if profile_id:
        # Профиль целиком задаёт зоны и пороги, поэтому legacy regions не передаём.
        request_payload["profile_id"] = profile_id
    else:
        request_payload["regions"] = [region]
    headers = {"doods-auth-key": auth_key} if auth_key else {}
    try:
        async with async_get_clientsession(hass).post(
            f"{base_url.rstrip('/')}/detect",
            json=request_payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as response:
            if response.status in {401, 403}:
                raise DooodsAuthError("invalid_auth")
            try:
                payload = await response.json()
            except (ValueError, aiohttp.ContentTypeError) as err:
                if response.status != 200:
                    raise DooodsDetectionError(
                        "http_error",
                        {"endpoint": "POST /detect", "status": str(response.status)},
                    ) from err
                raise _response_json_error(err) from err
            if response.status != 200:
                detail = payload.get("error") if isinstance(payload, dict) else None
                if detail:
                    raise DooodsDetectionError(
                        "detection_failed",
                        {"details": str(detail)},
                    )
                raise DooodsDetectionError(
                    "http_error",
                    {"endpoint": "POST /detect", "status": str(response.status)},
                )
    except DooodsApiError:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise DooodsConnectionError("connection_failed") from err

    detections = _normalize_detections(payload)
    return DooodsDetectionResult(
        detections=detections,
        annotated_image=_decode_annotated_image(payload.get("image")),
    )
