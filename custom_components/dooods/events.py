"""Build safe aggregated Home Assistant events for accepted DOOODS detections."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

_BOUNDING_BOX_FIELDS = ("left", "top", "right", "bottom")


def detection_event_data(
    *,
    entry_id: str,
    camera_entity_id: str,
    checked_at: datetime | None,
    detections: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return one serializable, privacy-safe event payload for a positive scan.

    The payload intentionally contains only accepted detection metadata. It does
    not contain the source frame, filesystem paths, or server credentials.
    """
    accepted: list[dict[str, Any]] = []
    labels: set[str] = set()
    for detection in detections:
        label = detection.get("label")
        if not isinstance(label, str) or not label.strip():
            continue

        confidence = detection.get("confidence")
        try:
            normalized_confidence = float(confidence)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(normalized_confidence):
            continue

        raw_box = detection.get("bounding_box")
        if not isinstance(raw_box, Mapping):
            continue
        box: dict[str, float] = {}
        for field in _BOUNDING_BOX_FIELDS:
            try:
                coordinate = float(raw_box[field])
            except (KeyError, TypeError, ValueError, OverflowError):
                break
            if not math.isfinite(coordinate):
                break
            box[field] = coordinate
        if len(box) != len(_BOUNDING_BOX_FIELDS):
            continue

        event_detection: dict[str, Any] = {
            "label": label,
            "confidence": normalized_confidence,
            "bounding_box": box,
        }
        region_id = detection.get("region_id")
        if isinstance(region_id, (str, int)) and not isinstance(region_id, bool):
            event_detection["region_id"] = region_id
        accepted.append(event_detection)
        labels.add(label)

    timestamp = checked_at.isoformat() if checked_at is not None else None
    return {
        "entry_id": entry_id,
        "camera_entity_id": camera_entity_id,
        "timestamp": timestamp,
        "labels": sorted(labels),
        "detections": accepted,
    }
