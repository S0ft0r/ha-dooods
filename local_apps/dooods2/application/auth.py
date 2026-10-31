"""Проверка API-ключа DOOODS2 Overhaul."""

from __future__ import annotations

import hmac
from typing import Any


_INGRESS_PROXY_HOST = "172.30.32.2"


def is_authenticated_ingress_request(remote_host: Any, remote_user_id: Any) -> bool:
    """Доверять только аутентифицированному HA-пользователю от Supervisor Ingress."""
    return (
        remote_host == _INGRESS_PROXY_HOST
        and isinstance(remote_user_id, str)
        and bool(remote_user_id.strip())
    )


def api_key_matches(expected: Any, provided: Any) -> bool:
    """Сравнить заголовок API-ключа с настройкой сервера без утечки по времени."""
    if not isinstance(expected, str) or not expected:
        return False
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(expected, provided)
