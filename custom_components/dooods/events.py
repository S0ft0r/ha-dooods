"""Build safe aggregated Home Assistant events for accepted DOOODS detections."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

_BOUNDING_BOX_FIELDS = ("left", "top", "right", "bottom")
FACE_MATCH_EVENT_STATES = frozenset({"matched", "ambiguous"})


def face_match_event_key(
    *,
    checked_at: str | None,
    match: Mapping[str, Any] | None,
) -> tuple[Any, ...] | None:
    """Return a stable per-frame key for a publishable face match decision."""
    if not match:
        return None
    state = match.get("state")
    if state not in FACE_MATCH_EVENT_STATES:
        return None
    return (
        checked_at,
        match.get("person_id"),
        match.get("person_name"),
        match.get("similarity"),
        state,
    )


def face_match_event_cooldown_allows(
    *,
    state: str,
    previous_state: str | None,
    elapsed: float | None,
    cooldown: int,
) -> bool:
    """Allow repeats after cooldown and an immediate ambiguous-to-match upgrade."""
    if (
        cooldown <= 0
        or previous_state is None
        or elapsed is None
        or elapsed >= cooldown
    ):
        return True
    return state == "matched" and previous_state != "matched"


def face_match_event_data(
    *,
    entry_id: str,
    camera_entity_id: str,
    checked_at: str | None,
    person_id: str | None,
    person_name: str | None,
    similarity: float | None,
    state: str,
) -> dict[str, Any]:
    """Return one serializable, privacy-safe event payload for a face decision.

    The payload intentionally contains only match metadata. It does not contain
    the source frame, filesystem paths, embeddings, or server credentials.
    """
    return {
        "entry_id": entry_id,
        "camera_entity_id": camera_entity_id,
        "timestamp": checked_at,
        "person_id": person_id,
        "person_name": person_name,
        "similarity": round(float(similarity), 4)
        if isinstance(similarity, (int, float))
        else None,
        "state": state,
    }


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
