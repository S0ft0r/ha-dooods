"""Camera result and RTSP motion-gate diagnostic sensors."""

from __future__ import annotations

from datetime import datetime
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .camera_result import CameraScanResult
from .coordinator import DooodsCoordinator
from .device import camera_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create camera recognition and per-camera RTSP diagnostic sensors."""
    coordinator: DooodsCoordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for camera_entity_id in coordinator.cameras:
        entities.extend(
            [
                DooodsCameraSensor(coordinator, camera_entity_id),
                DooodsMotionGateStateSensor(coordinator, camera_entity_id),
                DooodsMotionLastScoreSensor(coordinator, camera_entity_id),
                DooodsMotionMaxScoreSensor(coordinator, camera_entity_id),
                DooodsMotionSampledFramesSensor(coordinator, camera_entity_id),
                DooodsMotionCandidateFramesSensor(coordinator, camera_entity_id),
                DooodsMotionLastTriggerSensor(coordinator, camera_entity_id),
            ]
        )
    async_add_entities(entities)


class DooodsCameraSensor(CoordinatorEntity[DooodsCoordinator], SensorEntity):
    """Expose the latest detection count and normalized object boxes."""

    _attr_has_entity_name = True
    _attr_translation_key = "camera_detections"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the camera result sensor."""
        super().__init__(coordinator)
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{camera_entity_id}_camera_detections"
        )
        self._attr_device_info = camera_device_info(
            coordinator.hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Wait for a successful scan of this camera."""
        result = self._camera_result
        return super().available and result is not None and result.error is None

    @property
    @override
    def native_value(self) -> int | None:
        """Return the latest successful detection count."""
        result = self._camera_result
        if result is None or result.error is not None:
            return None
        return len(result.detections)

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose per-label counts, confidence values, and normalized boxes."""
        attributes: dict[str, Any] = {
            "camera_entity_id": self._camera_entity_id,
            "selected_labels": list(
                self.coordinator.labels_for_camera(self._camera_entity_id)
            ),
        }
        result = self._camera_result
        if result is not None and result.error is None:
            attributes.update(
                {
                    "counts_by_label": result.counts,
                    "max_confidence_by_label": result.confidences,
                    "detections": list(result.detections),
                    "last_checked": (
                        result.checked_at.isoformat() if result.checked_at else None
                    ),
                }
            )
        return attributes

    @property
    def _camera_result(self) -> CameraScanResult | None:
        """Return the latest result for this camera, if available."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._camera_entity_id)


class _DooodsMotionGateSensor(CoordinatorEntity[DooodsCoordinator], SensorEntity):
    """Common subscription and camera-device metadata for RTSP diagnostics."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: DooodsCoordinator,
        camera_entity_id: str,
        suffix: str,
        translation_key: str,
    ) -> None:
        """Initialize a diagnostic sensor associated with one camera."""
        super().__init__(coordinator)
        self._camera_entity_id = camera_entity_id
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{camera_entity_id}_{suffix}"
        )
        self._attr_translation_key = translation_key
        self._attr_device_info = camera_device_info(
            coordinator.hass,
            coordinator,
            camera_entity_id,
        )

    @property
    @override
    def available(self) -> bool:
        """Keep local stream diagnostics available when the DOOODS API is offline."""
        return True

    async def async_added_to_hass(self) -> None:
        """Subscribe this sensor to RTSP-only updates from the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_motion_gate_listener(
                self._handle_motion_gate_update
            )
        )

    @callback
    def _handle_motion_gate_update(self, camera_entity_id: str) -> None:
        """Write state only when the updated camera belongs to this entity."""
        if camera_entity_id == self._camera_entity_id:
            self.async_write_ha_state()

    @property
    def diagnostics(self) -> dict[str, Any]:
        """Return the latest diagnostic snapshot for this camera."""
        return self.coordinator.motion_gate_diagnostics_for_camera(
            self._camera_entity_id
        )

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose source selection and a short diagnostic error code."""
        diagnostics = self.diagnostics
        return {
            "camera_entity_id": self._camera_entity_id,
            "active_source": diagnostics.get("active_source", "none"),
            "last_error": diagnostics.get("last_error"),
        }


class DooodsMotionGateStateSensor(_DooodsMotionGateSensor):
    """Expose the current state of the per-camera RTSP motion gate."""

    _attr_icon = "mdi:lan-connect"

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the gate-state sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_state",
            "motion_gate_state",
        )

    @property
    @override
    def native_value(self) -> str:
        """Return one of the documented gate states."""
        state = self.diagnostics.get("state", "disabled")
        if state == "error":
            return "reconnecting"
        if state not in {
            "connecting",
            "idle",
            "active",
            "reconnecting",
            "disabled",
        }:
            return "disabled"
        return state


class DooodsMotionLastScoreSensor(_DooodsMotionGateSensor):
    """Expose the most recently sampled RTSP motion score."""

    _attr_icon = "mdi:chart-line"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 6

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the latest-score sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_last_score",
            "motion_gate_last_score",
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the latest normalized changed-pixel fraction."""
        value = self.diagnostics.get("last_score")
        return value if isinstance(value, (int, float)) else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show the motion threshold next to the current score."""
        attributes = super().extra_state_attributes
        attributes["threshold"] = self.diagnostics.get("threshold", 0.004)
        return attributes


class DooodsMotionMaxScoreSensor(_DooodsMotionGateSensor):
    """Expose the maximum motion score observed in the latest sample interval."""

    _attr_icon = "mdi:chart-bell-curve-cumulative"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 6

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the interval-maximum score sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_max_score",
            "motion_gate_max_score",
        )

    @property
    @override
    def native_value(self) -> float | None:
        """Return the latest interval's maximum normalized changed-pixel fraction."""
        value = self.diagnostics.get("max_score_interval")
        return value if isinstance(value, (int, float)) else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        """Show the motion threshold alongside the interval maximum."""
        attributes = super().extra_state_attributes
        attributes["threshold"] = self.diagnostics.get("threshold", 0.004)
        return attributes


class DooodsMotionSampledFramesSensor(_DooodsMotionGateSensor):
    """Expose the number of frames sampled by the current RTSP worker."""

    _attr_icon = "mdi:video-check-outline"
    _attr_native_unit_of_measurement = "frames"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the sampled-frame counter sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_sampled_frames",
            "motion_gate_sampled_frames",
        )

    @property
    @override
    def native_value(self) -> int | None:
        """Return the total sampled frame count since the worker started."""
        value = self.diagnostics.get("sampled_frames_total")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class DooodsMotionCandidateFramesSensor(_DooodsMotionGateSensor):
    """Expose the number of candidate frames sent to Home Assistant."""

    _attr_icon = "mdi:video-plus-outline"
    _attr_native_unit_of_measurement = "frames"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the candidate-frame counter sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_candidate_frames",
            "motion_gate_candidate_frames",
        )

    @property
    @override
    def native_value(self) -> int | None:
        """Return candidate frames sent since the worker started."""
        value = self.diagnostics.get("candidate_frames_total")
        return value if isinstance(value, int) and not isinstance(value, bool) else None


class DooodsMotionLastTriggerSensor(_DooodsMotionGateSensor):
    """Expose the most recent transition into active motion."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, coordinator: DooodsCoordinator, camera_entity_id: str) -> None:
        """Initialize the last-trigger timestamp sensor."""
        super().__init__(
            coordinator,
            camera_entity_id,
            "motion_gate_last_trigger",
            "motion_gate_last_trigger",
        )

    @property
    @override
    def native_value(self) -> datetime | None:
        """Return the last RTSP motion-gate activation time."""
        value = self.diagnostics.get("last_motion_at")
        if not isinstance(value, str):
            return None
        try:
            timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return timestamp if timestamp.tzinfo is not None else None
