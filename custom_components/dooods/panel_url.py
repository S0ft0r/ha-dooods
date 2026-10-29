"""Ссылки HA-панели профилей DOOODS для устройства сервера."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

PROFILE_MANAGER_PANEL_PATH = "dooods-profiles"


def build_profile_manager_url(hass: object) -> str | None:
    """Вернуть browser-ссылку на native-панель HA, не используя адрес App."""
    from homeassistant.helpers.network import get_url

    try:
        base = urlsplit(get_url(hass, prefer_external=True))
    except Exception:
        return None
    if base.scheme not in {"http", "https"} or not base.netloc:
        return None
    path = f"{base.path.rstrip('/')}/{PROFILE_MANAGER_PANEL_PATH}"
    return urlunsplit((base.scheme, base.netloc, path, "", ""))
