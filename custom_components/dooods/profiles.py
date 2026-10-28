"""Нормализация назначений профилей DOOODS2 для камер Home Assistant."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_camera_profiles(
    value: Any,
    camera_entity_ids: list[str],
) -> dict[str, str]:
    """Оставить только непустые назначения профилей выбранным камерам."""
    if not isinstance(value, Mapping):
        return {}
    return {
        camera_entity_id: profile_id.strip()
        for camera_entity_id in camera_entity_ids
        if isinstance(camera_entity_id, str)
        and isinstance(profile_id := value.get(camera_entity_id), str)
        and profile_id.strip()
    }


def profile_labels(profile: Mapping[str, Any]) -> list[str]:
    """Вернуть упорядоченные метки профиля, исключив неверные и повторные значения."""
    labels = profile.get("labels", [])
    if not isinstance(labels, list):
        return []
    return list(
        dict.fromkeys(
            label.strip()
            for label in labels
            if isinstance(label, str) and label.strip()
        )
    )


def profile_labels_for_detector(
    profile: Mapping[str, Any],
    detector_labels: list[str],
) -> list[str]:
    """Раскрыть wildcard меток профиля в полный список меток выбранного детектора."""
    labels = profile_labels(profile)
    if "*" in labels:
        return list(dict.fromkeys(label for label in detector_labels if label != "*"))
    return [label for label in labels if label != "*"]
