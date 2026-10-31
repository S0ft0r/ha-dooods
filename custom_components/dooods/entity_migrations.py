"""Entity-registry migrations for deprecated per-camera controls."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN

_MOTION_GATE_DIAGNOSTIC_SUFFIXES = (
    "motion_gate_state",
    "motion_gate_last_score",
    "motion_gate_max_score",
    "motion_gate_sampled_frames",
    "motion_gate_candidate_frames",
    "motion_gate_last_trigger",
)


def migrate_camera_entity_registry(
    hass: Any,
    entry_id: str,
    camera_entity_ids: Iterable[str],
) -> tuple[int, int]:
    """Disable existing RTSP diagnostics and remove the replaced stream switch."""
    registry = er.async_get(hass)
    diagnostics_disabled = 0
    stream_switches_removed = 0

    for camera_entity_id in camera_entity_ids:
        if not isinstance(camera_entity_id, str) or not camera_entity_id:
            continue
        for suffix in _MOTION_GATE_DIAGNOSTIC_SUFFIXES:
            unique_id = f"{entry_id}_{camera_entity_id}_{suffix}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            if entity_id is None:
                continue
            entity = registry.async_get(entity_id)
            if entity is None or entity.disabled_by is not None:
                continue
            registry.async_update_entity(
                entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
            diagnostics_disabled += 1

        switch_unique_id = f"{entry_id}_{camera_entity_id}_stream_processing"
        switch_entity_id = registry.async_get_entity_id(
            "switch",
            DOMAIN,
            switch_unique_id,
        )
        if switch_entity_id is not None:
            registry.async_remove(switch_entity_id)
            stream_switches_removed += 1

    return diagnostics_disabled, stream_switches_removed
