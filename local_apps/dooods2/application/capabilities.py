"""Описание версии протокола и возможностей DOOODS2 Overhaul."""

from __future__ import annotations

API_VERSION = 1
APP_VERSION = "0.1.17-dev"
PROFILE_FEATURES: tuple[str, ...] = ("profiles", "polygon_zones")
PREVIEW_FEATURES: tuple[str, ...] = ("camera_preview",)
ANNOTATION_FEATURES: tuple[str, ...] = ("localized_annotations",)
MOTION_STREAM_FEATURES: tuple[str, ...] = ("rtsp_motion_stream",)


def capabilities_payload(
    *,
    profiles_enabled: bool = False,
    preview_enabled: bool = False,
    localized_annotations_enabled: bool = False,
    motion_stream_enabled: bool = False,
) -> dict[str, object]:
    """Сформировать ответ с возможностями, реально доступными через API."""
    features = []
    if profiles_enabled:
        features.extend(PROFILE_FEATURES)
    if preview_enabled:
        features.extend(PREVIEW_FEATURES)
    if localized_annotations_enabled:
        features.extend(ANNOTATION_FEATURES)
    if motion_stream_enabled:
        features.extend(MOTION_STREAM_FEATURES)
    return {
        "api_version": API_VERSION,
        "app_version": APP_VERSION,
        "features": sorted(features),
    }
