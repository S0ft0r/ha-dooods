"""Проверка API-ключа DOOODS2 Overhaul."""

from __future__ import annotations

import hmac
from ipaddress import IPv4Address, IPv4Network
from typing import Any

_SUPERVISOR_NETWORK = IPv4Network("172.30.0.0/16")


def _is_supervisor_host(remote_host: Any) -> bool:
    """Проверяет, что запрос пришёл из внутренней сети Supervisor (Ingress)."""
    if not isinstance(remote_host, str):
        return False
    try:
        return IPv4Address(remote_host) in _SUPERVISOR_NETWORK
    except ValueError:
        return False


def is_authenticated_ingress_request(remote_host: Any, headers: Any) -> bool:
    """Доверять только аутентифицированному HA-пользователю от Supervisor Ingress.

    HA Ingress проксирует запросы из внутренней сети Supervisor и добавляет
    заголовок Authorization для аутентифицированных пользователей.
    """
    if not _is_supervisor_host(remote_host):
        return False
    if not isinstance(headers, dict):
        return False
    auth = headers.get("authorization") or headers.get("Authorization")
    return isinstance(auth, str) and bool(auth.strip())


def api_key_matches(expected: Any, provided: Any) -> bool:
    """Сравнить заголовок API-ключа с настройкой сервера без утечки по времени."""
    if not isinstance(expected, str) or not expected:
        return False
    if not isinstance(provided, str):
        return False
    return hmac.compare_digest(expected, provided)
