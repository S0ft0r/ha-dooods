"""Автогенерация ключа и сохранение в настройках Home Assistant App."""

from __future__ import annotations

import copy
import hmac
import json
import secrets
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_SUPERVISOR_BASE_URL = "http://supervisor"
_APP_INFO_PATH = "/addons/self/info"
_APP_OPTIONS_PATH = "/addons/self/options"
_REQUEST_TIMEOUT_SECONDS = 10


class SupervisorOptionsError(RuntimeError):
    """Ошибка получения или сохранения настроек App через Supervisor."""


def _request_supervisor(
    method: str,
    path: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Выполнить запрос к Supervisor, не включая токен или тело в ошибки."""
    request_data = None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if payload is not None:
        request_data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        f"{_SUPERVISOR_BASE_URL}{path}",
        data=request_data,
        headers=headers,
        method=method,
    )
    open_url = opener or urlopen
    try:
        with open_url(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            response_body = response.read()
    except HTTPError as err:
        raise SupervisorOptionsError(
            f"Supervisor API вернул HTTP {err.code}."
        ) from None
    except (URLError, TimeoutError, OSError):
        raise SupervisorOptionsError(
            "Не удалось связаться с API Home Assistant Supervisor."
        ) from None

    try:
        envelope = json.loads(response_body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        raise SupervisorOptionsError(
            "Supervisor API вернул некорректный ответ."
        ) from None

    if not isinstance(envelope, dict) or envelope.get("result") != "ok":
        raise SupervisorOptionsError(
            "Supervisor API не подтвердил операцию с настройками App."
        )
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise SupervisorOptionsError(
            "Supervisor API вернул некорректные настройки App."
        )
    return data


def _get_app_info(token: str, opener: Callable[..., Any] | None) -> dict[str, Any]:
    """Получить сведения о собственном App и проверить его настройки."""
    app_info = _request_supervisor(
        "GET",
        _APP_INFO_PATH,
        token,
        opener=opener,
    )
    if not isinstance(app_info.get("options"), dict):
        raise SupervisorOptionsError(
            "Supervisor API не вернул настройки этого App."
        )
    return app_info


def _get_app_options(token: str, opener: Callable[..., Any] | None) -> dict[str, Any]:
    """Получить собственные сохранённые настройки App, включая парольные поля."""
    return copy.deepcopy(_get_app_info(token, opener)["options"])


def ensure_api_key(
    configured_key: Any,
    supervisor_token: Any,
    *,
    opener: Callable[..., Any] | None = None,
) -> str:
    """Вернуть имеющийся API-ключ или создать и сохранить новый через Supervisor.

    Существующий ключ не меняется при перезапуске или обновлении App. Если ключ
    пуст, он создаётся только после того, как Supervisor подтвердит сохранение
    нового значения в постоянных настройках App.
    """
    if isinstance(configured_key, str) and configured_key.strip():
        return configured_key

    if not isinstance(supervisor_token, str) or not supervisor_token.strip():
        raise SupervisorOptionsError(
            "SUPERVISOR_TOKEN недоступен; задайте server.auth_key вручную "
            "в конфигурации вне Home Assistant Supervisor."
        )
    token = supervisor_token.strip()

    options = _get_app_options(token, opener)
    server_options = options.get("server")
    if server_options is None:
        server_options = {}
        options["server"] = server_options
    if not isinstance(server_options, dict):
        raise SupervisorOptionsError(
            "В настройках App поле server имеет неверный формат."
        )

    existing_key = server_options.get("auth_key")
    if isinstance(existing_key, str) and existing_key.strip():
        return existing_key

    generated_key = secrets.token_urlsafe(32)
    server_options["auth_key"] = generated_key
    _request_supervisor(
        "POST",
        _APP_OPTIONS_PATH,
        token,
        payload={"options": options},
        opener=opener,
    )

    verified_options = _get_app_options(token, opener)
    verified_server = verified_options.get("server")
    verified_key = verified_server.get("auth_key") if isinstance(verified_server, dict) else None
    if not isinstance(verified_key, str) or not hmac.compare_digest(
        verified_key,
        generated_key,
    ):
        raise SupervisorOptionsError(
            "Supervisor не подтвердил сохранение нового API-ключа."
        )

    return generated_key
