"""API key, Supervisor Ingress and anti-CSRF helpers for DOODS2."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import re
from collections.abc import Mapping
from typing import Any

_SUPERVISOR_PEER = "172.30.32.2"
_MIN_SUPERVISOR = (2026, 10, 2)
_VERSION_RE = re.compile(r"^(\d{4})\.(\d{1,2})\.(\d+)$")
_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def supervisor_version_supported(version: Any) -> bool:
    """Accept only final, well-formed Supervisor versions at or above minimum."""
    if not isinstance(version, str):
        return False
    match = _VERSION_RE.fullmatch(version)
    if not match:
        return False
    year, month, patch = (int(part) for part in match.groups())
    return 1 <= month <= 12 and (year, month, patch) >= _MIN_SUPERVISOR


def check_supervisor_version(token: str) -> bool:
    """Check /info off the event loop; no proxies, redirects or response logging."""
    if not isinstance(token, str) or not token:
        return False
    connection = None
    try:
        connection = http.client.HTTPConnection(_SUPERVISOR_PEER, timeout=3)
        connection.request(
            "GET",
            "/info",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            return False
        data = response.read(65537)
        if len(data) > 65536:
            return False
        envelope = json.loads(data)
        if not isinstance(envelope, dict) or envelope.get("result") != "ok":
            return False
        info = envelope.get("data")
        return isinstance(info, dict) and supervisor_version_supported(info.get("supervisor"))
    except (OSError, ValueError, RecursionError, http.client.HTTPException, UnicodeError):
        return False
    finally:
        if connection is not None:
            connection.close()


def ingress_user_id(remote_host: Any, headers: Any) -> str | None:
    """Return the identity asserted by Supervisor, only from its actual TCP peer.

    This is trustworthy only under Supervisor's documented isolated Ingress
    network and with Uvicorn proxy-header rewriting disabled. It does not by
    itself prove administrator status; that requires panel_admin and a
    sufficiently recent Supervisor.
    """
    if remote_host != _SUPERVISOR_PEER or not isinstance(headers, Mapping):
        return None
    identity = headers.get("x-remote-user-id")
    if not isinstance(identity, str) or not _USER_ID_RE.fullmatch(identity):
        return None
    return identity


def is_authenticated_ingress_request(
    remote_host: Any, headers: Any, *, supervisor_verified: bool = False
) -> bool:
    """Require trusted peer, trusted identity and an independently verified version."""
    return supervisor_verified and ingress_user_id(remote_host, headers) is not None


def api_key_matches(expected: Any, provided: Any) -> bool:
    """Compare server-side API keys without revealing length-dependent content."""
    if not isinstance(expected, str) or not expected or not isinstance(provided, str):
        return False
    try:
        return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8"))
    except (UnicodeError, ValueError):
        return False


def csrf_token(secret: bytes, identity: str) -> str:
    """Derive a per-user anti-CSRF token from an ephemeral server secret."""
    return hmac.new(
        secret, b"dooods-faces-csrf\0" + identity.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def csrf_token_matches(secret: bytes, identity: str, provided: Any) -> bool:
    if not isinstance(provided, str) or not re.fullmatch(r"[0-9a-f]{64}", provided):
        return False
    return hmac.compare_digest(csrf_token(secret, identity), provided)
