"""Координатор периодического и запускаемого движением сканирования камер через DOOODS2."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, override
from urllib.parse import urlsplit

from homeassistant.components import camera as camera_component
from homeassistant.components.camera import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.translation import async_get_translations
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import (
    DooodsApiError,
    DooodsAuthError,
    DooodsDetectionError,
    async_detect,
    async_get_profiles,
    async_iter_motion_stream_events,
    async_put_preview,
)
from .archive import (
    async_prune_archive,
    async_save_annotated_frame,
    has_accepted_annotated_frame,
    normalize_archive_directory,
)
from .camera_motion_recheck_intervals import (
    camera_motion_recheck_interval,
    normalize_camera_motion_recheck_intervals,
)
from .camera_result import CameraScanResult, retain_last_accepted_image
from .camera_scan_intervals import camera_scan_interval
from .const import (
    CONF_BOTTOM,
    CONF_CAMERA_DETECTION_ENABLED,
    CONF_COVERS,
    CONF_LEFT,
    CONF_RIGHT,
    CONF_TOP,
    DEFAULT_FACE_MATCH_EVENT_COOLDOWN,
    DOMAIN,
    EVENT_DETECTION,
    EVENT_FACE_MATCH,
)
from .events import (
    annotated_image_unique_id,
    detection_event_data,
    face_match_event_cooldown_allows,
    face_match_event_data,
    face_match_event_key,
)
from .face_diagnostics import normalize_face_diagnostics
from .frame_comparison import (
    source_frame_signature,
    source_frames_are_similar,
)
from .label_icons import label_translation_key
from .motion import motion_sensor_is_usable
from .preview import (
    PreviewUploadLimiter,
    is_preview_jpeg,
    preview_source_id,
    preview_source_name,
)
from .profiles import profile_labels_for_detector

_LOGGER = logging.getLogger(__name__)
_INITIAL_CAMERA_RETRY_DELAY = 5
_MAX_CAMERA_RETRY_DELAY = 300
_PROFILE_LABEL_REFRESH_INTERVAL = timedelta(minutes=1)
_MOTION_GATE_DEFAULT_THRESHOLD = 0.004


class DooodsCoordinator(DataUpdateCoordinator[dict[str, CameraScanResult]]):
    """Сканировать выбранные камеры по расписанию и при событии связанного датчика движения."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        *,
        base_url: str,
        auth_key: str,
        detector_name: str,
        cameras: list[str],
        labels: list[str],
        confidence: float,
        area: Mapping[str, Any],
        timeout: int,
        camera_scan_intervals: Mapping[str, int] | None,
        parent_device_id: str,
        parent_device_identifier: str,
        object_detection_cameras: list[str] | None = None,
        camera_motion_recheck_intervals: Mapping[str, int] | None = None,
        preview_enabled: bool = False,
        annotation_localization_enabled: bool = False,
        motion_stream_enabled: bool = False,
        face_diagnostics_enabled: bool = False,
        motion_sensors: Mapping[str, str] | None = None,
        camera_detection_enabled: Mapping[str, bool] | None = None,
        camera_stream_enabled: Mapping[str, bool] | None = None,
        detector_labels: list[str] | None = None,
        camera_profiles: Mapping[str, str] | None = None,
        camera_labels: Mapping[str, list[str]] | None = None,
        archive_directory: str = "",
        archive_max_count: int = 10,
        archive_max_age_days: int = 0,
        emit_detection_events: bool = False,
        emit_face_match_events: bool = False,
        face_match_event_cooldown: int = DEFAULT_FACE_MATCH_EVENT_COOLDOWN,
    ) -> None:
        """Инициализировать координатор и неизменяемые параметры сканирования."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"{DOMAIN}_{config_entry.entry_id}",
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=1,
                immediate=True,
            ),
        )
        self.base_url = base_url
        self.auth_key = auth_key
        self.detector_name = detector_name
        self.cameras = cameras
        # None preserves the legacy constructor default; an explicit empty
        # selection disables object detection on all cameras.
        self.object_detection_cameras = (
            frozenset(
                camera for camera in object_detection_cameras if isinstance(camera, str)
            )
            if isinstance(object_detection_cameras, list)
            else None
        )
        self.labels = labels
        self.detector_labels = list(
            detector_labels if detector_labels is not None else labels
        )
        self.annotation_localization_enabled = annotation_localization_enabled
        self.motion_stream_enabled = bool(motion_stream_enabled and auth_key)
        self.face_diagnostics_enabled = bool(
            face_diagnostics_enabled and self.motion_stream_enabled
        )
        self.motion_sensors = dict(motion_sensors or {})
        self.camera_detection_enabled = dict(camera_detection_enabled or {})
        self.camera_stream_enabled = dict(camera_stream_enabled or {})
        self._motion_stream_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_motion_candidates: dict[str, bytes] = {}
        self.camera_labels = {
            camera_entity_id: list(camera_label_values)
            for camera_entity_id, camera_label_values in (camera_labels or {}).items()
        }
        self.camera_profiles = dict(camera_profiles or {})
        self.confidence = confidence
        self.area = dict(area)
        self.timeout = timeout
        configured_intervals = (
            camera_scan_intervals
            if isinstance(camera_scan_intervals, Mapping)
            else {}
        )
        self.camera_scan_intervals = {
            camera_entity_id: camera_scan_interval(
                camera_entity_id,
                configured_intervals,
            )
            for camera_entity_id in cameras
        }
        self.camera_motion_recheck_intervals = (
            normalize_camera_motion_recheck_intervals(
                camera_motion_recheck_intervals or {},
                cameras,
            )
        )
        self.parent_device_id = parent_device_id
        self.parent_device_identifier = parent_device_identifier
        self.preview_enabled = preview_enabled and bool(auth_key)
        self.entry_id = config_entry.entry_id
        self._config_entry = config_entry
        self._preview_upload_limiter = PreviewUploadLimiter()
        self.archive_directory = normalize_archive_directory(archive_directory)
        self.archive_max_count = max(0, archive_max_count)
        self.archive_max_age_days = max(0, archive_max_age_days)
        self._last_successful_frame_signatures: dict[str, bytes] = {}
        self._archive_retry_frame_signatures: dict[str, bytes] = {}
        self.emit_detection_events = bool(emit_detection_events)
        self._label_translation_language: str | None = None
        self._label_translations: dict[str, str] = {}
        self._pending_detection_events: list[
            tuple[str, CameraScanResult, CameraScanResult]
        ] = []
        self.emit_face_match_events = bool(emit_face_match_events)
        self.face_match_event_cooldown = max(0, face_match_event_cooldown)
        self._last_face_match_event_keys: dict[str, tuple[Any, ...]] = {}
        self._last_face_match_events: dict[tuple[str, str], tuple[float, str, float | None]] = {}
        self._archive_cleanup_unsubscribe: Callable[[], None] | None = None
        self._archive_cleanup_task: asyncio.Task[Any] | None = None
        self._profile_labels_unsubscribe: Callable[[], None] | None = None
        self._profile_labels_task: asyncio.Task[Any] | None = None
        self._pending_camera_scans: set[str] = set()
        self._pending_periodic_camera_scans: set[str] = set()
        self._pending_motion_camera_scans: set[str] = set()
        self._motion_candidate_cooldowns: dict[str, float] = {}
        self._force_full_scan = False
        self._active_full_scan = False
        self._periodic_scanning_started = False
        self._cancel_periodic_scans: dict[str, Callable[[], None]] = {}
        self._camera_retry_unsubscribers: dict[str, Callable[[], None]] = {}
        self._camera_retry_delays: dict[str, int] = {}
        self._initial_scan_task: asyncio.Task[Any] | None = None
        self._is_shutting_down = False
        self._motion_gate_listeners: set[Callable[[str], None]] = set()
        self._motion_gate_diagnostics = {
            camera_entity_id: {
                "state": (
                    "connecting"
                    if self.camera_uses_rtsp_motion_gate(camera_entity_id)
                    else "disabled"
                ),
                "connection_state": (
                    "connecting"
                    if self.camera_uses_rtsp_motion_gate(camera_entity_id)
                    else "disabled"
                ),
                "gate_active": False,
                "active_source": self._motion_source_for_camera(camera_entity_id),
                "last_score": None,
                "max_score_interval": None,
                "threshold": _MOTION_GATE_DEFAULT_THRESHOLD,
                "sampled_frames_total": 0,
                "candidate_frames_total": 0,
                "last_motion_at": None,
                "last_candidate_at": None,
                "last_error": None,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            for camera_entity_id in self.cameras
        }
        self._face_diagnostics_listeners: set[Callable[[str], None]] = set()
        self._face_diagnostics_by_camera = {
            camera_entity_id: {
                "state": (
                    "loading"
                    if self.face_diagnostics_enabled
                    and self.camera_uses_rtsp_motion_gate(camera_entity_id)
                    else "disabled"
                ),
                "frames_processed_total": 0,
                "faces_detected": None,
                "largest_face_width_px": None,
                "largest_face_height_px": None,
                "embedding_ready": None,
                "inference_ms": None,
                "last_processed_at": None,
                "last_error": None,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            for camera_entity_id in self.cameras
        }

    @callback
    def set_initial_scan_task(self, task: asyncio.Task[Any]) -> None:
        """Сохранить задачу первого сканирования для явной отмены при выгрузке."""
        if self._is_shutting_down:
            task.cancel()
            return
        if self._initial_scan_task is not None and not self._initial_scan_task.done():
            self._initial_scan_task.cancel()
        if task.done():
            self._initial_scan_task = None
            return
        self._initial_scan_task = task
        task.add_done_callback(self._clear_initial_scan_task)

    @callback
    def _clear_initial_scan_task(self, task: asyncio.Task[Any]) -> None:
        """Удалить завершённую задачу первого сканирования из координатора."""
        if self._initial_scan_task is task:
            self._initial_scan_task = None

    async def _cancel_initial_scan_task(self) -> None:
        """Отменить и дождаться активного первого сканирования при выгрузке."""
        task = self._initial_scan_task
        self._initial_scan_task = None
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def labels_for_camera(self, camera_entity_id: str) -> list[str]:
        """Вернуть метки профиля камеры или глобальный набор legacy-режима."""
        if camera_entity_id in self.camera_labels:
            return self.camera_labels[camera_entity_id]
        return self.labels

    @callback
    def camera_recognition_is_enabled(self, camera_entity_id: str) -> bool:
        """Считать распознавание включённым для старых записей, где нет переключателя."""
        enabled = getattr(self, "camera_detection_enabled", {}).get(camera_entity_id)
        return enabled if isinstance(enabled, bool) else True

    @callback
    def camera_object_detection_is_enabled(self, camera_entity_id: str) -> bool:
        """Check the object-only camera filter without affecting RTSP or faces."""
        selected = getattr(self, "object_detection_cameras", None)
        return camera_entity_id in self.cameras and (
            selected is None or camera_entity_id in selected
        )

    @callback
    def camera_stream_processing_is_enabled(self, camera_entity_id: str) -> bool:
        """Return whether the direct source selector chose RTSP for this camera."""
        enabled = getattr(self, "camera_stream_enabled", {}).get(camera_entity_id)
        return enabled is True

    @callback
    def camera_uses_rtsp_motion_gate(self, camera_entity_id: str) -> bool:
        """Return whether RTSP is this camera’s active, supported motion source."""
        return bool(
            getattr(self, "motion_stream_enabled", False)
            and self.camera_stream_processing_is_enabled(camera_entity_id)
        )

    @callback
    def camera_uses_ha_motion_trigger(self, camera_entity_id: str) -> bool:
        """Use a selected HA motion trigger only when RTSP is not the active source."""
        return not self.camera_uses_rtsp_motion_gate(camera_entity_id)

    @callback
    def camera_ids_for_ha_motion_trigger(
        self,
        camera_entity_ids: Iterable[str],
    ) -> set[str]:
        """Filter HA motion-trigger scans so active RTSP gates remain exclusive."""
        return {
            camera_entity_id
            for camera_entity_id in camera_entity_ids
            if self.camera_uses_ha_motion_trigger(camera_entity_id)
        }

    @callback
    def async_add_motion_gate_listener(
        self,
        listener: Callable[[str], None],
    ) -> Callable[[], None]:
        """Subscribe to per-camera RTSP diagnostic updates and return an unsubscribe."""
        self._motion_gate_listeners.add(listener)

        @callback
        def _unsubscribe() -> None:
            self._motion_gate_listeners.discard(listener)

        return _unsubscribe

    @callback
    def motion_gate_diagnostics_for_camera(
        self,
        camera_entity_id: str,
    ) -> dict[str, Any]:
        """Return a snapshot of the RTSP diagnostics for one camera."""
        return dict(self._motion_gate_diagnostics.get(camera_entity_id, {}))

    @callback
    def async_add_face_diagnostics_listener(
        self,
        listener: Callable[[str], None],
    ) -> Callable[[], None]:
        """Subscribe to per-camera experimental face-diagnostic updates."""
        self._face_diagnostics_listeners.add(listener)

        @callback
        def _unsubscribe() -> None:
            self._face_diagnostics_listeners.discard(listener)

        return _unsubscribe

    @callback
    def face_diagnostics_for_camera(self, camera_entity_id: str) -> dict[str, Any]:
        """Return a safe in-memory diagnostic snapshot for one camera."""
        diagnostics_by_camera = getattr(self, "_face_diagnostics_by_camera", {})
        return dict(diagnostics_by_camera.get(camera_entity_id, {}))

    @callback
    def _notify_face_diagnostics_listeners(self, camera_entity_id: str) -> None:
        """Notify only face diagnostic entities attached to this camera."""
        for listener in tuple(getattr(self, "_face_diagnostics_listeners", ())):
            try:
                listener(camera_entity_id)
            except Exception:
                _LOGGER.exception("Face diagnostic listener failed")

    @callback
    def _update_face_diagnostics(
        self,
        camera_entity_id: str,
        payload: Any,
    ) -> None:
        """Keep only allowlisted scalar telemetry; never retain images/embeddings."""
        if (
            not getattr(self, "face_diagnostics_enabled", False)
            or camera_entity_id not in self.cameras
        ):
            return
        normalized = normalize_face_diagnostics(payload)
        if normalized is None:
            return
        diagnostics = self._face_diagnostics_by_camera.setdefault(
            camera_entity_id,
            {},
        )
        diagnostics.update(normalized)
        diagnostics["updated_at"] = datetime.now(UTC).isoformat()
        self._notify_face_diagnostics_listeners(camera_entity_id)
        match = normalized.get("match")
        if match:
            self._fire_face_match_event(
                camera_entity_id,
                normalized.get("last_processed_at"),
                match,
            )

    @callback
    def _set_face_diagnostics_state(
        self,
        camera_entity_id: str,
        state: str,
    ) -> None:
        """Change the diagnostic lifecycle state without clearing last metrics."""
        if (
            not getattr(self, "face_diagnostics_enabled", False)
            or camera_entity_id not in self.cameras
            or state not in {"disabled", "loading"}
        ):
            return
        diagnostics = self._face_diagnostics_by_camera.setdefault(
            camera_entity_id,
            {},
        )
        diagnostics["state"] = state
        diagnostics["updated_at"] = datetime.now(UTC).isoformat()
        self._notify_face_diagnostics_listeners(camera_entity_id)

    @callback
    def _motion_source_for_camera(self, camera_entity_id: str) -> str:
        """Return the currently selected trigger source for diagnostics."""
        if self.camera_uses_rtsp_motion_gate(camera_entity_id):
            return "rtsp_gate"
        motion_entity_id = getattr(self, "motion_sensors", {}).get(camera_entity_id)
        if motion_sensor_is_usable(self.hass, motion_entity_id):
            return "ha_motion"
        return "none"

    @callback
    def _update_motion_gate_diagnostics(
        self,
        camera_entity_id: str,
        *,
        connection_state: str | None = None,
        gate_active: bool | None = None,
        last_score: Any = None,
        max_score_interval: Any = None,
        threshold: Any = None,
        sampled_frames_total: Any = None,
        candidate_frames_total: Any = None,
        last_motion_at: Any = None,
        last_candidate_at: Any = None,
        last_error: Any = None,
        increment_candidates: bool = False,
        reset: bool = False,
        active_source: str | None = None,
    ) -> None:
        """Update one camera's gate telemetry and notify diagnostic sensor entities."""
        if camera_entity_id not in self.cameras:
            return
        diagnostics_by_camera = getattr(self, "_motion_gate_diagnostics", None)
        if not isinstance(diagnostics_by_camera, dict):
            return
        diagnostics = diagnostics_by_camera.setdefault(camera_entity_id, {})
        if reset:
            diagnostics.update(
                {
                    "state": "connecting",
                    "connection_state": "connecting",
                    "gate_active": False,
                    "last_score": None,
                    "max_score_interval": None,
                    "threshold": _MOTION_GATE_DEFAULT_THRESHOLD,
                    "sampled_frames_total": 0,
                    "candidate_frames_total": 0,
                    "last_motion_at": None,
                    "last_candidate_at": None,
                    "last_error": None,
                }
            )
        if active_source is not None:
            diagnostics["active_source"] = active_source
        else:
            diagnostics["active_source"] = self._motion_source_for_camera(
                camera_entity_id
            )

        if connection_state in {
            "disabled",
            "connecting",
            "connected",
            "reconnecting",
            "error",
        }:
            diagnostics["connection_state"] = connection_state
        if isinstance(gate_active, bool):
            diagnostics["gate_active"] = gate_active

        current_connection_state = diagnostics.get("connection_state")
        if (
            current_connection_state
            in {"disabled", "connecting", "reconnecting", "error"}
            and not isinstance(gate_active, bool)
        ):
            diagnostics["gate_active"] = False
        if current_connection_state == "connected":
            diagnostics["state"] = (
                "active" if diagnostics.get("gate_active") else "idle"
            )
        elif current_connection_state in {
            "disabled",
            "connecting",
            "reconnecting",
        }:
            diagnostics["state"] = current_connection_state
        elif current_connection_state == "error":
            diagnostics["state"] = "reconnecting"

        for key, value in (
            ("last_score", last_score),
            ("max_score_interval", max_score_interval),
            ("threshold", threshold),
        ):
            if (
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(value)
                and 0 <= value <= 1
            ):
                diagnostics[key] = float(value)

        for key, value in (
            ("sampled_frames_total", sampled_frames_total),
            ("candidate_frames_total", candidate_frames_total),
        ):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                old_value = diagnostics.get(key, 0)
                diagnostics[key] = (
                    value if reset else max(old_value, value)
                )
        if increment_candidates:
            diagnostics["candidate_frames_total"] = (
                diagnostics.get("candidate_frames_total", 0) + 1
            )

        for key, value in (
            ("last_motion_at", last_motion_at),
            ("last_candidate_at", last_candidate_at),
        ):
            parsed = None
            if isinstance(value, datetime):
                parsed = value
            elif isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                diagnostics[key] = parsed.astimezone(UTC).isoformat()
        if last_error is not None:
            diagnostics["last_error"] = (
                str(last_error)[:64] if isinstance(last_error, str) else "error"
            )
        elif current_connection_state == "connected":
            diagnostics["last_error"] = None

        diagnostics["updated_at"] = datetime.now(UTC).isoformat()
        for listener in tuple(getattr(self, "_motion_gate_listeners", ())):
            try:
                listener(camera_entity_id)
            except Exception:
                _LOGGER.exception(
                    "Could not update RTSP diagnostics for camera %s",
                    camera_entity_id,
                )

    @callback
    def start_periodic_scanning(self) -> None:
        """Запустить опрос камер и независимую очистку архива по сроку хранения."""
        if self._is_shutting_down:
            return
        self._reconcile_motion_streams()
        if self.archive_directory:
            self._schedule_archive_cleanup()
            if (
                self.archive_max_age_days > 0
                and self._archive_cleanup_unsubscribe is None
            ):
                self._archive_cleanup_unsubscribe = async_track_time_interval(
                    self.hass,
                    self._async_handle_archive_cleanup,
                    timedelta(days=1),
                )
        if self.camera_profiles and self._profile_labels_unsubscribe is None:
            self._profile_labels_unsubscribe = async_track_time_interval(
                self.hass,
                self._async_handle_profile_labels_refresh,
                _PROFILE_LABEL_REFRESH_INTERVAL,
            )
        if self._periodic_scanning_started:
            return
        if self._config_entry.pref_disable_polling:
            _LOGGER.debug(
                "Периодическое сканирование DOOODS отключено: %s", self.entry_id
            )
            return
        self._periodic_scanning_started = True
        for camera_entity_id in self.cameras:
            self._start_periodic_scan_for_camera(camera_entity_id)

    @callback
    def _start_periodic_scan_for_camera(self, camera_entity_id: str) -> None:
        """Install the per-camera timer only while recognition and polling are enabled."""
        if (
            self._is_shutting_down
            or not self._periodic_scanning_started
            or self._config_entry.pref_disable_polling
            or camera_entity_id not in self.cameras
            or not self.camera_recognition_is_enabled(camera_entity_id)
            or camera_entity_id in self._cancel_periodic_scans
        ):
            return
        interval = camera_scan_interval(
            camera_entity_id,
            self.camera_scan_intervals,
        )

        @callback
        def _handle_camera_scan(
            now: datetime,
            camera_id: str = camera_entity_id,
        ) -> None:
            self._async_handle_periodic_camera_scan(camera_id, now)

        self._cancel_periodic_scans[camera_entity_id] = async_track_time_interval(
            self.hass,
            _handle_camera_scan,
            timedelta(seconds=interval),
        )

    @callback
    def _cancel_periodic_scan_for_camera(self, camera_entity_id: str) -> None:
        """Remove one camera timer after recognition is disabled."""
        if cancel_scan := self._cancel_periodic_scans.pop(camera_entity_id, None):
            cancel_scan()

    @callback
    def set_camera_switch_state(
        self,
        camera_entity_id: str,
        option_key: str,
        enabled: bool,
    ) -> None:
        """Apply a per-camera switch immediately without disturbing other cameras."""
        if camera_entity_id not in self.cameras or not isinstance(enabled, bool):
            return
        if option_key == CONF_CAMERA_DETECTION_ENABLED:
            self.camera_detection_enabled[camera_entity_id] = enabled
            if enabled:
                self._start_periodic_scan_for_camera(camera_entity_id)
                self._reconcile_motion_streams()
                self.request_camera_scans([camera_entity_id])
            else:
                self._cancel_periodic_scan_for_camera(camera_entity_id)
                self._clear_camera_retry(camera_entity_id)
                self._pending_camera_scans.discard(camera_entity_id)
                self._pending_periodic_camera_scans.discard(camera_entity_id)
                self._pending_motion_candidates.pop(camera_entity_id, None)
                self._stop_motion_stream(camera_entity_id)
                results = dict(self.data or {})
                results[camera_entity_id] = CameraScanResult(
                    counts={},
                    confidences={},
                    detections=(),
                    annotated_image=None,
                    checked_at=None,
                )
                self.async_set_updated_data(results)
    @callback
    def _reconcile_motion_streams(self) -> None:
        """Make an enabled RTSP gate the exclusive motion trigger for its camera."""
        if self._is_shutting_down:
            return
        for camera_entity_id in self.cameras:
            if (
                not self.camera_recognition_is_enabled(camera_entity_id)
                or not self.camera_uses_rtsp_motion_gate(camera_entity_id)
            ):
                self._stop_motion_stream(camera_entity_id)
            else:
                self._ensure_motion_stream(camera_entity_id)

    @callback
    def motion_sensor_state_changed(
        self,
        camera_entity_id: str,
        _state: str | None,
    ) -> None:
        """Keep RTSP as the sole motion trigger when stream processing is enabled."""
        if self._is_shutting_down:
            return
        if (
            self.camera_recognition_is_enabled(camera_entity_id)
            and self.camera_uses_rtsp_motion_gate(camera_entity_id)
        ):
            self._ensure_motion_stream(camera_entity_id)
        else:
            self._stop_motion_stream(camera_entity_id)

    @callback
    def _ensure_motion_stream(self, camera_entity_id: str) -> None:
        """Create one background RTSP reader task for an eligible camera."""
        task = self._motion_stream_tasks.get(camera_entity_id)
        if task is not None and not task.done():
            return
        if (
            self._is_shutting_down
            or camera_entity_id not in self.cameras
            or not self.camera_recognition_is_enabled(camera_entity_id)
            or not self.camera_uses_rtsp_motion_gate(camera_entity_id)
        ):
            return
        self._update_motion_gate_diagnostics(
            camera_entity_id,
            reset=True,
            connection_state="connecting",
            active_source="rtsp_gate",
        )
        self._set_face_diagnostics_state(camera_entity_id, "loading")
        task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_motion_stream_loop(camera_entity_id),
            name=f"{DOMAIN}_motion_stream_{camera_entity_id}",
        )
        self._motion_stream_tasks[camera_entity_id] = task
        task.add_done_callback(
            lambda done, camera_id=camera_entity_id: self._clear_motion_stream_task(
                camera_id,
                done,
            )
        )

    @callback
    def _clear_motion_stream_task(
        self,
        camera_entity_id: str,
        task: asyncio.Task[Any],
    ) -> None:
        """Forget a finished RTSP reader without erasing a replacement task."""
        if self._motion_stream_tasks.get(camera_entity_id) is task:
            self._motion_stream_tasks.pop(camera_entity_id, None)

    @callback
    def _stop_motion_stream(self, camera_entity_id: str) -> None:
        """Cancel the RTSP reader when stream processing or recognition is disabled."""
        task = getattr(self, "_motion_stream_tasks", {}).pop(camera_entity_id, None)
        if task is not None and not task.done():
            task.cancel()
        getattr(self, "_motion_candidate_cooldowns", {}).pop(
            camera_entity_id,
            None,
        )
        pending_motion_scans = getattr(self, "_pending_motion_camera_scans", None)
        pending_candidates = getattr(self, "_pending_motion_candidates", None)
        if isinstance(pending_candidates, dict):
            pending_candidates.pop(camera_entity_id, None)
        if (
            isinstance(pending_motion_scans, set)
            and camera_entity_id in pending_motion_scans
        ):
            pending_motion_scans.discard(camera_entity_id)
            getattr(self, "_pending_camera_scans", set()).discard(camera_entity_id)
        self._update_motion_gate_diagnostics(
            camera_entity_id,
            connection_state="disabled",
            gate_active=False,
            active_source=self._motion_source_for_camera(camera_entity_id),
        )
        self._set_face_diagnostics_state(camera_entity_id, "disabled")

    async def _async_motion_stream_loop(self, camera_entity_id: str) -> None:
        """Resolve a camera RTSP URL and forward only motion-candidate JPEGs to YOLO."""
        stream_source_getter = getattr(
            camera_component,
            "async_get_stream_source",
            None,
        )
        if not callable(stream_source_getter):
            _LOGGER.warning(
                "Home Assistant has no async_get_stream_source API; "
                "RTSP motion gate disabled for %s",
                camera_entity_id,
            )
            self._update_motion_gate_diagnostics(
                camera_entity_id,
                connection_state="error",
                gate_active=False,
                last_error="unsupported_stream_source_api",
            )
            return

        retry_delay = _INITIAL_CAMERA_RETRY_DELAY
        while (
            not self._is_shutting_down
            and self.camera_recognition_is_enabled(camera_entity_id)
            and self.camera_uses_rtsp_motion_gate(camera_entity_id)
        ):
            try:
                stream_source = await stream_source_getter(
                    self.hass,
                    camera_entity_id,
                )
            except (
                HomeAssistantError,
                OSError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as err:
                _LOGGER.debug(
                    "Could not resolve an RTSP source for camera %s (%s)",
                    camera_entity_id,
                    type(err).__name__,
                )
                stream_source = None

            try:
                parsed_source = (
                    urlsplit(stream_source)
                    if isinstance(stream_source, str) and len(stream_source) <= 2048
                    else None
                )
            except ValueError:
                parsed_source = None
            if (
                parsed_source is None
                or parsed_source.scheme.lower() not in {"rtsp", "rtsps"}
                or not parsed_source.hostname
            ):
                self._update_motion_gate_diagnostics(
                    camera_entity_id,
                    connection_state="reconnecting",
                    gate_active=False,
                    last_error="invalid_stream_source",
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, _MAX_CAMERA_RETRY_DELAY)
                continue

            try:
                async for event in async_iter_motion_stream_events(
                    self.hass,
                    self.base_url,
                    self.auth_key,
                    source_id=camera_entity_id,
                    stream_source=stream_source,
                    roi_polygons=[],
                ):
                    if self._is_shutting_down:
                        return
                    if (
                        not self.camera_recognition_is_enabled(camera_entity_id)
                        or not self.camera_uses_rtsp_motion_gate(camera_entity_id)
                    ):
                        return
                    if event.get("type") == "status":
                        self._update_face_diagnostics(
                            camera_entity_id,
                            event.get("face_diagnostics"),
                        )
                        status_state = event.get("state")
                        connection_state = status_state
                        if status_state == "stopped":
                            connection_state = (
                                "reconnecting"
                                if self.camera_uses_rtsp_motion_gate(camera_entity_id)
                                else "disabled"
                            )
                        if status_state == "connected":
                            retry_delay = _INITIAL_CAMERA_RETRY_DELAY
                        if connection_state in {
                            "disabled",
                            "connecting",
                            "connected",
                            "reconnecting",
                            "error",
                        }:
                            self._update_motion_gate_diagnostics(
                                camera_entity_id,
                                connection_state=connection_state,
                                gate_active=event.get("gate_active")
                                if isinstance(event.get("gate_active"), bool)
                                else None,
                                last_score=event.get("last_score"),
                                max_score_interval=event.get("max_score_interval"),
                                threshold=event.get("threshold"),
                                sampled_frames_total=event.get(
                                    "sampled_frames_total"
                                ),
                                candidate_frames_total=event.get(
                                    "candidate_frames_total"
                                ),
                                last_motion_at=event.get("last_motion_at"),
                                last_candidate_at=event.get("last_candidate_at"),
                            )
                        elif status_state == "frame_encode_failed":
                            self._update_motion_gate_diagnostics(
                                camera_entity_id,
                                last_error=status_state,
                            )
                        _LOGGER.debug(
                            "RTSP motion stream for %s: %s",
                            camera_entity_id,
                            status_state or "unknown",
                        )
                    elif event.get("type") == "motion":
                        motion_state = event.get("state")
                        if motion_state in {"active", "idle"}:
                            self._update_motion_gate_diagnostics(
                                camera_entity_id,
                                connection_state="connected",
                                gate_active=motion_state == "active",
                                last_score=event.get("score"),
                                last_motion_at=event.get("occurred_at")
                                if motion_state == "active"
                                else None,
                            )
                    elif event.get("type") == "candidate":
                        self._update_face_diagnostics(
                            camera_entity_id,
                            event.get("face_diagnostics"),
                        )
                        candidate = event.get("image")
                        self._update_motion_gate_diagnostics(
                            camera_entity_id,
                            connection_state="connected",
                            gate_active=True,
                            last_score=event.get("motion_score"),
                            last_candidate_at=event.get("captured_at"),
                            increment_candidates=True,
                        )
                        if isinstance(candidate, bytes):
                            self.request_camera_candidate(
                                camera_entity_id,
                                candidate,
                            )
                    elif event.get("type") == "error":
                        self._update_motion_gate_diagnostics(
                            camera_entity_id,
                            connection_state="error",
                            last_error=event.get("error", "motion_stream_error"),
                        )
                        _LOGGER.debug(
                            "RTSP motion stream reported an error for %s",
                            camera_entity_id,
                        )
            except asyncio.CancelledError:
                raise
            except DooodsAuthError as err:
                self._update_motion_gate_diagnostics(
                    camera_entity_id,
                    connection_state="error",
                    gate_active=False,
                    last_error="authentication_failed",
                )
                _LOGGER.error(
                    "Authentication rejected the RTSP motion stream for %s: %s",
                    camera_entity_id,
                    err,
                )
                return
            except DooodsApiError as err:
                self._update_motion_gate_diagnostics(
                    camera_entity_id,
                    connection_state="reconnecting",
                    gate_active=False,
                    last_error="api_disconnected",
                )
                _LOGGER.debug(
                    "RTSP motion stream disconnected for %s: %s",
                    camera_entity_id,
                    err,
                )
            except (HomeAssistantError, OSError, TimeoutError) as err:
                self._update_motion_gate_diagnostics(
                    camera_entity_id,
                    connection_state="reconnecting",
                    gate_active=False,
                    last_error=type(err).__name__,
                )
                _LOGGER.debug(
                    "Could not start RTSP motion stream for %s (%s)",
                    camera_entity_id,
                    type(err).__name__,
                )

            if (
                not self._is_shutting_down
                and self.camera_recognition_is_enabled(camera_entity_id)
                and self.camera_uses_rtsp_motion_gate(camera_entity_id)
            ):
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, _MAX_CAMERA_RETRY_DELAY)

    @callback
    def _motion_candidate_cooldown_is_active(self, camera_entity_id: str) -> bool:
        """Return whether positive detection currently throttles RTSP candidates."""
        cooldowns = getattr(self, "_motion_candidate_cooldowns", None)
        if not isinstance(cooldowns, dict):
            return False
        deadline = cooldowns.get(camera_entity_id)
        if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
            cooldowns.pop(camera_entity_id, None)
            return False
        if time.monotonic() < deadline:
            return True
        cooldowns.pop(camera_entity_id, None)
        return False

    @callback
    def _start_motion_candidate_cooldown(self, camera_entity_id: str) -> None:
        """Delay another RTSP candidate scan after a successful positive result."""
        if not self.camera_uses_rtsp_motion_gate(camera_entity_id):
            return
        delay = camera_motion_recheck_interval(
            camera_entity_id,
            getattr(self, "camera_motion_recheck_intervals", {}),
        )
        if delay <= 0:
            return
        cooldowns = getattr(self, "_motion_candidate_cooldowns", None)
        if not isinstance(cooldowns, dict):
            return
        cooldowns[camera_entity_id] = time.monotonic() + delay

        pending_motion_scans = getattr(self, "_pending_motion_camera_scans", set())
        if camera_entity_id in pending_motion_scans:
            pending_motion_scans.discard(camera_entity_id)
            getattr(self, "_pending_camera_scans", set()).discard(camera_entity_id)
            getattr(self, "_pending_motion_candidates", {}).pop(
                camera_entity_id,
                None,
            )

    @callback
    def request_camera_candidate(
        self,
        camera_entity_id: str,
        image: bytes,
    ) -> None:
        """Queue the exact JPEG frame that crossed the RTSP motion gate."""
        if (
            self._is_shutting_down
            or camera_entity_id not in self.cameras
            or not self.camera_recognition_is_enabled(camera_entity_id)
            or not self.camera_object_detection_is_enabled(camera_entity_id)
            or not self.camera_uses_rtsp_motion_gate(camera_entity_id)
            or not isinstance(image, bytes)
            or not image
        ):
            return
        getattr(self, "_pending_periodic_camera_scans", set()).discard(
            camera_entity_id
        )
        self._pending_motion_candidates[camera_entity_id] = image
        if self._motion_candidate_cooldown_is_active(camera_entity_id):
            return
        if camera_entity_id in self._pending_camera_scans:
            return
        self._pending_camera_scans.add(camera_entity_id)
        pending_motion_scans = getattr(self, "_pending_motion_camera_scans", None)
        if isinstance(pending_motion_scans, set):
            pending_motion_scans.add(camera_entity_id)
        self._config_entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            name=f"{DOMAIN}_motion_candidate_scan",
        )

    @callback
    def _schedule_archive_cleanup(self) -> None:
        """Запустить очистку архива, не создавая параллельные задания."""
        if (
            self._is_shutting_down
            or not self.archive_directory
            or (
                self._archive_cleanup_task is not None
                and not self._archive_cleanup_task.done()
            )
        ):
            return
        task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_cleanup_archive(),
            name=f"{DOMAIN}_archive_cleanup",
        )
        self._archive_cleanup_task = task
        task.add_done_callback(self._clear_archive_cleanup_task)

    @callback
    def _clear_archive_cleanup_task(self, task: asyncio.Task[Any]) -> None:
        """Освободить ссылку на завершившуюся задачу очистки архива."""
        if self._archive_cleanup_task is task:
            self._archive_cleanup_task = None

    @callback
    def _async_handle_archive_cleanup(self, _now: datetime) -> None:
        """Запланировать ежедневное удаление файлов, превысивших срок хранения."""
        self._schedule_archive_cleanup()

    async def _async_cleanup_archive(self) -> None:
        """Применить лимиты хранения к существующим файлам всех камер записи."""
        try:
            await async_prune_archive(
                self.hass,
                self.archive_directory,
                self.entry_id,
                max_count=self.archive_max_count,
                max_age_days=self.archive_max_age_days,
            )
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Не удалось выполнить очистку архива DOOODS: %s",
                err,
            )

    @callback
    def _async_handle_profile_labels_refresh(self, _now: datetime) -> None:
        """Запланировать фоновую сверку меток назначенных профилей."""
        self._schedule_profile_labels_refresh()

    @callback
    def _schedule_profile_labels_refresh(self) -> None:
        """Не допускать параллельных запросов списка профилей."""
        if (
            self._is_shutting_down
            or not self.camera_profiles
            or (
                self._profile_labels_task is not None
                and not self._profile_labels_task.done()
            )
        ):
            return
        task = self._config_entry.async_create_background_task(
            self.hass,
            self._async_refresh_profile_labels(),
            name=f"{DOMAIN}_profile_label_refresh",
        )
        self._profile_labels_task = task
        task.add_done_callback(self._clear_profile_labels_task)

    @callback
    def _clear_profile_labels_task(self, task: asyncio.Task[Any]) -> None:
        """Освободить ссылку на завершившуюся сверку меток профилей."""
        if self._profile_labels_task is task:
            self._profile_labels_task = None

    async def _async_refresh_profile_labels(self) -> None:
        """Обновить выбранные метки и сенсоры без перезагрузки записи интеграции."""
        try:
            profiles = await async_get_profiles(
                self.hass,
                self.base_url,
                self.auth_key,
            )
        except DooodsApiError as err:
            _LOGGER.debug(
                "Не удалось обновить метки профилей DOOODS; оставлен последний список: %s",
                err,
            )
            return

        profiles_by_id = {profile["id"]: profile for profile in profiles}
        changed_cameras: list[str] = []
        for camera_entity_id, profile_id in self.camera_profiles.items():
            profile = profiles_by_id.get(profile_id)
            if profile is None:
                # Не удалять сенсоры из-за кратковременной ошибки или удалённого профиля.
                continue
            current_labels = self.camera_labels.get(camera_entity_id, [])
            updated_labels = profile_labels_for_detector(
                profile,
                self.detector_labels,
            )
            if current_labels != updated_labels:
                self.camera_labels[camera_entity_id] = updated_labels
                changed_cameras.append(camera_entity_id)

        if changed_cameras:
            _LOGGER.info(
                "Обновлены метки профилей DOOODS для камер: %s",
                ", ".join(changed_cameras),
            )
            self.async_update_listeners()
            self.request_camera_scans(changed_cameras)

    @callback
    def _schedule_camera_retry(self, camera_entity_id: str) -> None:
        """Запланировать повтор при временной недоступности камеры или DOODS2."""
        if (
            self._is_shutting_down
            or camera_entity_id not in self.cameras
            or not self.camera_recognition_is_enabled(camera_entity_id)
            or camera_entity_id in self._camera_retry_unsubscribers
        ):
            return

        delay = self._camera_retry_delays.get(
            camera_entity_id,
            _INITIAL_CAMERA_RETRY_DELAY,
        )
        self._camera_retry_delays[camera_entity_id] = min(
            delay * 2,
            _MAX_CAMERA_RETRY_DELAY,
        )

        @callback
        def _retry_scan(_now: datetime) -> None:
            self._camera_retry_unsubscribers.pop(camera_entity_id, None)
            if not self._is_shutting_down:
                self.request_camera_scans([camera_entity_id])

        _LOGGER.debug(
            "Повторное сканирование камеры %s через %s секунд",
            camera_entity_id,
            delay,
        )
        self._camera_retry_unsubscribers[camera_entity_id] = async_call_later(
            self.hass,
            delay,
            _retry_scan,
        )

    @callback
    def _clear_camera_retry(self, camera_entity_id: str) -> None:
        """Отменить повтор камеры после успешного сканирования и сбросить задержку."""
        if cancel_retry := self._camera_retry_unsubscribers.pop(
            camera_entity_id,
            None,
        ):
            cancel_retry()
        self._camera_retry_delays.pop(camera_entity_id, None)

    @callback
    def _async_handle_periodic_camera_scan(
        self,
        camera_entity_id: str,
        _now: datetime,
    ) -> None:
        """Добавить одну камеру в очередь по её индивидуальному интервалу."""
        if not self._is_shutting_down and camera_entity_id in self.cameras:
            self.request_camera_scans([camera_entity_id], periodic=True)

    @callback
    def request_camera_scans(
        self,
        camera_entity_ids: Iterable[str],
        *,
        full_scan: bool = False,
        periodic: bool = False,
    ) -> None:
        """Добавить выбранные камеры в очередь и поручить координатору объединить запросы обновления."""
        camera_ids = set(camera_entity_ids).intersection(self.cameras)
        if full_scan:
            camera_ids = set(self.cameras)
            self._force_full_scan = True
        camera_ids = {
            camera_entity_id
            for camera_entity_id in camera_ids
            if self.camera_recognition_is_enabled(camera_entity_id)
            and self.camera_object_detection_is_enabled(camera_entity_id)
        }
        if not camera_ids:
            if full_scan:
                self._force_full_scan = False
            return
        new_camera_ids = camera_ids - self._pending_camera_scans
        periodic_camera_scans = getattr(
            self,
            "_pending_periodic_camera_scans",
            None,
        )
        if isinstance(periodic_camera_scans, set):
            if periodic and not full_scan:
                periodic_camera_scans.update(new_camera_ids)
            else:
                periodic_camera_scans.difference_update(camera_ids)
        pending_candidates = getattr(self, "_pending_motion_candidates", None)
        if isinstance(pending_candidates, dict):
            for camera_entity_id in camera_ids:
                pending_candidates.pop(camera_entity_id, None)
        pending_motion_scans = getattr(self, "_pending_motion_camera_scans", None)
        if isinstance(pending_motion_scans, set):
            pending_motion_scans.difference_update(camera_ids)
        self._pending_camera_scans.update(camera_ids)
        if not new_camera_ids and not full_scan:
            return
        self._config_entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            name=f"{DOMAIN}_camera_scan_request",
        )

    @override
    async def async_shutdown(self) -> None:
        """Остановить таймеры и завершить начальный скан при выгрузке записи."""
        self._is_shutting_down = True
        self._pending_detection_events.clear()
        for cancel_scan in self._cancel_periodic_scans.values():
            cancel_scan()
        self._cancel_periodic_scans.clear()
        if self._archive_cleanup_unsubscribe is not None:
            self._archive_cleanup_unsubscribe()
            self._archive_cleanup_unsubscribe = None
        if self._profile_labels_unsubscribe is not None:
            self._profile_labels_unsubscribe()
            self._profile_labels_unsubscribe = None
        profile_labels_task = self._profile_labels_task
        self._profile_labels_task = None
        if profile_labels_task is not None and not profile_labels_task.done():
            profile_labels_task.cancel()
            await asyncio.gather(profile_labels_task, return_exceptions=True)
        archive_cleanup_task = self._archive_cleanup_task
        self._archive_cleanup_task = None
        if archive_cleanup_task is not None and not archive_cleanup_task.done():
            archive_cleanup_task.cancel()
            await asyncio.gather(archive_cleanup_task, return_exceptions=True)
        for cancel_retry in self._camera_retry_unsubscribers.values():
            cancel_retry()
        self._camera_retry_unsubscribers.clear()
        self._camera_retry_delays.clear()
        motion_stream_task_map = getattr(self, "_motion_stream_tasks", {})
        motion_stream_tasks = list(motion_stream_task_map.values())
        motion_stream_task_map.clear()
        for task in motion_stream_tasks:
            if not task.done():
                task.cancel()
        if motion_stream_tasks:
            await asyncio.gather(*motion_stream_tasks, return_exceptions=True)
        getattr(self, "_pending_motion_candidates", {}).clear()
        getattr(self, "_motion_candidate_cooldowns", {}).clear()
        getattr(self, "_pending_periodic_camera_scans", set()).clear()
        getattr(self, "_pending_motion_camera_scans", set()).clear()
        await self._cancel_initial_scan_task()
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, CameraScanResult]:
        """Сканировать только запрошенные камеры, сохраняя последние результаты для остальных."""
        self._pending_detection_events.clear()
        pending_events: list[tuple[str, CameraScanResult, CameraScanResult]] = []
        self._active_full_scan = self.data is None or self._force_full_scan
        try:
            if self._active_full_scan:
                requested_camera_ids = set(self.cameras)
            else:
                requested_camera_ids = self._pending_camera_scans.copy()
            pending_periodic_scans = getattr(
                self,
                "_pending_periodic_camera_scans",
                None,
            )
            periodic_camera_ids: set[str] = set()
            if isinstance(pending_periodic_scans, set):
                if not self._active_full_scan:
                    periodic_camera_ids = requested_camera_ids.intersection(
                        pending_periodic_scans
                    )
                pending_periodic_scans.difference_update(requested_camera_ids)
            self._pending_camera_scans.difference_update(requested_camera_ids)
            pending_motion_scans = getattr(self, "_pending_motion_camera_scans", None)
            if isinstance(pending_motion_scans, set):
                pending_motion_scans.difference_update(requested_camera_ids)
            pending_candidates = getattr(self, "_pending_motion_candidates", {})
            candidate_images = {
                camera_entity_id: pending_candidates.pop(camera_entity_id)
                for camera_entity_id in requested_camera_ids
                if camera_entity_id in pending_candidates
            }
            camera_ids = {
                camera_entity_id
                for camera_entity_id in requested_camera_ids
                if self.camera_recognition_is_enabled(camera_entity_id)
                and self.camera_object_detection_is_enabled(camera_entity_id)
            }
            self._force_full_scan = False
            results = dict(self.data or {})
            for camera_entity_id in self.cameras:
                if not self.camera_object_detection_is_enabled(camera_entity_id):
                    # No stale object results while the independent face stream runs.
                    results.pop(camera_entity_id, None)
                elif not self.camera_recognition_is_enabled(camera_entity_id):
                    results[camera_entity_id] = CameraScanResult(
                        counts={},
                        confidences={},
                        detections=(),
                        annotated_image=None,
                        checked_at=None,
                    )
            if not camera_ids:
                return results

            if getattr(self, "emit_detection_events", False):
                await self._async_load_label_translations()

            for camera_entity_id in self.cameras:
                if camera_entity_id in camera_ids:
                    previous_result = results.get(camera_entity_id)
                    scan_result = await self._async_scan_camera(
                        camera_entity_id,
                        candidate_images.get(camera_entity_id),
                    )
                    if not self.camera_recognition_is_enabled(camera_entity_id):
                        results[camera_entity_id] = CameraScanResult(
                            counts={},
                            confidences={},
                            detections=(),
                            annotated_image=None,
                            checked_at=None,
                        )
                        continue
                    if scan_result.error is None:
                        self._clear_camera_retry(camera_entity_id)
                        if scan_result.detections:
                            self._start_motion_candidate_cooldown(camera_entity_id)
                        archive_candidate = (
                            self.camera_recognition_is_enabled(camera_entity_id)
                            and has_accepted_annotated_frame(
                                scan_result.detections,
                                scan_result.annotated_image,
                                scan_result.checked_at,
                            )
                        )
                        current_signature = scan_result.source_frame_signature
                        if archive_candidate:
                            previous_signature = (
                                self._last_successful_frame_signatures.get(
                                    camera_entity_id
                                )
                            )
                            retry_signature = self._archive_retry_frame_signatures.get(
                                camera_entity_id
                            )
                            retry_is_pending = (
                                current_signature is not None
                                and retry_signature is not None
                                and source_frames_are_similar(
                                    retry_signature,
                                    current_signature,
                                )
                            )
                            if (
                                camera_entity_id in periodic_camera_ids
                                and current_signature is not None
                                and previous_signature is not None
                                and source_frames_are_similar(
                                    previous_signature,
                                    current_signature,
                                )
                                and not retry_is_pending
                            ):
                                _LOGGER.debug(
                                    "Не сохраняю повторный кадр камеры %s: изображение не изменилось",
                                    camera_entity_id,
                                )
                            else:
                                archived = await self._async_archive_camera_frame(
                                    camera_entity_id,
                                    scan_result.annotated_image,
                                    scan_result.checked_at,
                                    scan_result.detections,
                                )
                                if archived:
                                    self._archive_retry_frame_signatures.pop(
                                        camera_entity_id,
                                        None,
                                    )
                                elif (
                                    self.archive_directory
                                    and current_signature is not None
                                ):
                                    self._archive_retry_frame_signatures[
                                        camera_entity_id
                                    ] = current_signature

                        if current_signature is not None and (
                            not scan_result.detections or archive_candidate
                        ):
                            self._last_successful_frame_signatures[
                                camera_entity_id
                            ] = current_signature
                    else:
                        self._schedule_camera_retry(camera_entity_id)
                    if not self.camera_recognition_is_enabled(camera_entity_id):
                        results[camera_entity_id] = CameraScanResult(
                            counts={},
                            confidences={},
                            detections=(),
                            annotated_image=None,
                            checked_at=None,
                        )
                        continue
                    results[camera_entity_id] = retain_last_accepted_image(
                        previous_result,
                        scan_result,
                    )
                    if scan_result.error is None and scan_result.detections:
                        pending_events.append(
                            (camera_entity_id, scan_result, results[camera_entity_id])
                        )
            self._pending_detection_events = pending_events
            return results
        finally:
            self._active_full_scan = False

    @callback
    @override
    def _async_refresh_finished(self) -> None:
        """Запланировать повторное сканирование, если движение обнаружено во время текущей проверки."""
        if not self._pending_camera_scans and not self._force_full_scan:
            return
        self._config_entry.async_create_background_task(
            self.hass,
            self.async_request_refresh(),
            name=f"{DOMAIN}_coalesced_camera_scan",
        )

    @callback
    @override
    def async_update_listeners(self) -> None:
        """Publish scan events only after coordinator data and entity states update."""
        pending, self._pending_detection_events = self._pending_detection_events, []
        super().async_update_listeners()
        if self._is_shutting_down or not self.last_update_success:
            return
        for camera_entity_id, scan_result, published_result in pending:
            if (
                self.data.get(camera_entity_id) is published_result
                and self.camera_recognition_is_enabled(camera_entity_id)
                and self.camera_object_detection_is_enabled(camera_entity_id)
            ):
                self._fire_detection_event(camera_entity_id, scan_result)

    @callback
    def _annotated_image_entity_id(
        self, camera_entity_id: str, scan_result: CameraScanResult
    ) -> str | None:
        """Resolve the live image entity only if it published this scan's JPEG."""
        if scan_result.annotated_image is None or scan_result.checked_at is None:
            return None
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "image", DOMAIN, annotated_image_unique_id(self.entry_id, camera_entity_id)
        )
        if entity_id is None:
            return None
        entry = registry.async_get(entity_id)
        if entry is None or entry.disabled_by is not None:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            updated_at = datetime.fromisoformat(state.state)
        except (TypeError, ValueError):
            return None
        if updated_at != scan_result.checked_at:
            return None
        return entity_id

    async def _async_load_label_translations(self) -> None:
        """Cache entity-name translations for the current Home Assistant language."""
        language = self.hass.config.language or "en"
        if self._label_translation_language == language:
            return
        try:
            translations = await async_get_translations(
                self.hass, language, "entity", integrations={DOMAIN}
            )
        except Exception:  # Translation failures must never interrupt detection.
            _LOGGER.exception("Could not load detection label translations")
            self._label_translation_language = None
            self._label_translations = {}
            return
        self._label_translations = translations
        self._label_translation_language = language

    def _fire_detection_event(
        self,
        camera_entity_id: str,
        scan_result: CameraScanResult,
    ) -> None:
        """Emit one opt-in Home Assistant event for accepted detections in a scan."""
        if not getattr(self, "emit_detection_events", False) or not scan_result.detections:
            return
        event_data = detection_event_data(
            entry_id=self.entry_id,
            camera_entity_id=camera_entity_id,
            checked_at=scan_result.checked_at,
            detections=scan_result.detections,
        )
        if event_data["detections"]:
            translations = getattr(self, "_label_translations", {})

            def display_label(label: str) -> str:
                key = label_translation_key(label)
                if key == "object":
                    return label
                return translations.get(
                    f"component.{DOMAIN}.entity.binary_sensor.{key}.name", label
                )

            event_data["labels_translated"] = [
                display_label(label) for label in event_data["labels"]
            ]
            for detection in event_data["detections"]:
                detection["label_translated"] = display_label(detection["label"])
            event_data["image_entity_id"] = self._annotated_image_entity_id(
                camera_entity_id, scan_result
            )
            self.hass.bus.async_fire(EVENT_DETECTION, event_data)

    @callback
    def _fire_face_match_event(
        self,
        camera_entity_id: str,
        checked_at: str | None,
        match: dict[str, Any] | None,
    ) -> None:
        """Emit face decisions with per-frame deduplication and cooldown."""
        if not getattr(self, "emit_face_match_events", False) or not match:
            return
        event_key = face_match_event_key(checked_at=checked_at, match=match)
        if event_key is None:
            return
        if self._last_face_match_event_keys.get(camera_entity_id) == event_key:
            return
        state = str(match.get("state"))
        person_key = str(match.get("person_id") or match.get("person_name") or "")
        cooldown_key = (camera_entity_id, person_key)
        previous_event = self._last_face_match_events.get(cooldown_key)
        now = time.monotonic()
        elapsed = now - previous_event[0] if previous_event is not None else None
        previous_state = previous_event[1] if previous_event is not None else None
        previous_similarity = previous_event[2] if previous_event is not None else None
        similarity = match.get("similarity")
        if not face_match_event_cooldown_allows(
            state=state,
            previous_state=previous_state,
            elapsed=elapsed,
            cooldown=self.face_match_event_cooldown,
            similarity=similarity,
            previous_similarity=previous_similarity,
        ):
            # Do not publish this suppressed frame later
            # from a repeated status snapshot.
            self._last_face_match_event_keys[camera_entity_id] = event_key
            return
        event_data = face_match_event_data(
            entry_id=self.entry_id,
            camera_entity_id=camera_entity_id,
            checked_at=checked_at,
            person_id=match.get("person_id"),
            person_name=match.get("person_name"),
            similarity=match.get("similarity"),
            state=state,
        )
        self.hass.bus.async_fire(EVENT_FACE_MATCH, event_data)
        self._last_face_match_event_keys[camera_entity_id] = event_key
        self._last_face_match_events[cooldown_key] = (now, state, similarity)

    async def _async_archive_camera_frame(
        self,
        camera_entity_id: str,
        image: bytes,
        checked_at: datetime,
        detections: Iterable[Mapping[str, Any]],
    ) -> bool:
        """Сохранить JPEG и метаданные прошедших фильтры детекций."""
        if not self.archive_directory:
            return False
        try:
            await async_save_annotated_frame(
                self.hass,
                self.archive_directory,
                self.entry_id,
                camera_entity_id,
                image,
                checked_at,
                detections=tuple(detections),
                max_count=self.archive_max_count,
                max_age_days=self.archive_max_age_days,
            )
        except (OSError, ValueError) as err:
            _LOGGER.warning(
                "Не удалось сохранить принятый кадр камеры %s в архив DOOODS: %s",
                camera_entity_id,
                err,
            )
            return False
        return True

    async def _async_scan_camera(
        self,
        camera_entity_id: str,
        image_data: bytes | None = None,
    ) -> CameraScanResult:
        """Run YOLO on a motion-candidate JPEG or a regular HA camera snapshot."""
        try:
            if not self.camera_object_detection_is_enabled(camera_entity_id):
                return CameraScanResult({}, {}, (), None, None)
            if not self.camera_recognition_is_enabled(camera_entity_id):
                return CameraScanResult({}, {}, (), None, None)
            if image_data is None:
                image = await async_get_image(
                    self.hass,
                    camera_entity_id,
                    timeout=self.timeout,
                )
                image_data = image.content if image is not None else None
            if not image_data:
                raise DooodsDetectionError(
                    "camera_no_image",
                    {"camera_entity_id": camera_entity_id},
                )
            if not self.camera_recognition_is_enabled(camera_entity_id):
                return CameraScanResult({}, {}, (), None, None)
            frame_signature = None
            if self.archive_directory:
                try:
                    frame_signature = await self.hass.async_add_executor_job(
                        source_frame_signature,
                        image_data,
                    )
                except (HomeAssistantError, OSError, RuntimeError, ValueError) as err:
                    _LOGGER.debug(
                        "Не удалось вычислить отпечаток кадра камеры %s: %s",
                        camera_entity_id,
                        err,
                    )
            if self.preview_enabled and is_preview_jpeg(image_data):
                self._schedule_preview_upload(camera_entity_id, image_data)
            profile_id = self.camera_profiles.get(camera_entity_id)
            detection_result = await async_detect(
                self.hass,
                self.base_url,
                self.auth_key,
                detector_name=self.detector_name,
                image=image_data,
                labels=self.labels,
                confidence=self.confidence,
                area=self.area,
                timeout=self.timeout,
                profile_id=profile_id,
                annotation_locale=(
                    getattr(getattr(self.hass, "config", None), "language", None)
                    if self.annotation_localization_enabled
                    else None
                ),
            )
            if not self.camera_recognition_is_enabled(camera_entity_id):
                return CameraScanResult({}, {}, (), None, None)
            if detection_result.annotated_image is None:
                _LOGGER.debug(
                    "DOODS2 вернул обнаружения без размеченного изображения: %s",
                    camera_entity_id,
                )
            return self._summarize(
                detection_result.detections,
                detection_result.annotated_image,
                profile_active=profile_id is not None,
                source_frame_signature=frame_signature,
            )
        except (HomeAssistantError, TimeoutError, OSError) as err:
            _LOGGER.warning(
                "Не удалось просканировать камеру %s через DOOODS2: %s",
                camera_entity_id,
                err,
            )
            return CameraScanResult(
                counts={},
                confidences={},
                detections=(),
                annotated_image=None,
                checked_at=None,
                error=str(err),
            )

    def _schedule_preview_upload(
        self,
        camera_entity_id: str,
        image: bytes,
    ) -> None:
        """Передать исходный JPEG отдельно от результата распознавания и без ожидания API."""
        if not self.preview_enabled or not self._preview_upload_limiter.should_upload(
            camera_entity_id
        ):
            return

        source_id = preview_source_id(self.entry_id, camera_entity_id)
        camera_state = self.hass.states.get(camera_entity_id)
        friendly_name = (
            camera_state.attributes.get("friendly_name")
            if camera_state is not None
            else None
        )
        source_name = preview_source_name(
            friendly_name,
            camera_entity_id,
            source_id,
        )
        self._config_entry.async_create_background_task(
            self.hass,
            self._async_upload_preview(source_id, source_name, image),
            name=f"{DOMAIN}_preview_upload_{source_id[-8:]}",
        )

    async def _async_upload_preview(
        self,
        source_id: str,
        source_name: str,
        image: bytes,
    ) -> None:
        """Отправить кадр в Overhaul; ошибка предпросмотра не меняет результат сканирования."""
        try:
            await async_put_preview(
                self.hass,
                self.base_url,
                self.auth_key,
                source_id=source_id,
                source_name=source_name,
                image=image,
                timeout=5,
            )
        except DooodsApiError as err:
            _LOGGER.debug(
                "Не удалось передать временный кадр в DOOODS2 Overhaul: %s", err
            )

    def _summarize(
        self,
        detections: list[dict[str, Any]],
        annotated_image: bytes | None,
        *,
        profile_active: bool = False,
        source_frame_signature: bytes | None = None,
    ) -> CameraScanResult:
        """Подсчитать метки и рамки, прошедшие профильный либо legacy-фильтр."""
        counts: Counter[str] = Counter()
        confidences: dict[str, float] = {}
        accepted_detections: list[dict[str, Any]] = []
        for detection in detections:
            label = detection["label"]
            confidence = float(detection["confidence"])
            if not profile_active:
                if self.labels and label not in self.labels:
                    continue
                if confidence < self.confidence:
                    continue
                if not self._is_in_area(detection):
                    continue

            counts[label] += 1
            confidences[label] = max(
                confidences.get(label, float("-inf")),
                confidence,
            )
            accepted_detection = {
                "label": label,
                "confidence": confidence,
                "bounding_box": {
                    "top": float(detection["top"]),
                    "left": float(detection["left"]),
                    "bottom": float(detection["bottom"]),
                    "right": float(detection["right"]),
                },
            }
            if region_id := detection.get("region_id"):
                accepted_detection["region_id"] = region_id
            accepted_detections.append(accepted_detection)

        return CameraScanResult(
            counts=dict(counts),
            confidences=confidences,
            detections=tuple(accepted_detections),
            annotated_image=annotated_image,
            checked_at=datetime.now(UTC),
            source_frame_signature=source_frame_signature,
        )

    def _is_in_area(self, detection: Mapping[str, Any]) -> bool:
        """Дополнительно локально проверить обнаружения по заданной общей области."""
        top = float(self.area.get(CONF_TOP, 0.0))
        left = float(self.area.get(CONF_LEFT, 0.0))
        bottom = float(self.area.get(CONF_BOTTOM, 1.0))
        right = float(self.area.get(CONF_RIGHT, 1.0))
        detection_top = detection["top"]
        detection_left = detection["left"]
        detection_bottom = detection["bottom"]
        detection_right = detection["right"]

        if self.area.get(CONF_COVERS, True):
            return (
                top <= detection_top
                and left <= detection_left
                and bottom >= detection_bottom
                and right >= detection_right
            )
        return (
            detection_top <= bottom
            and detection_left <= right
            and detection_bottom >= top
            and detection_right >= left
        )
