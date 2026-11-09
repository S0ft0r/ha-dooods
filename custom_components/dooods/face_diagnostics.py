"""Validation for sanitized, in-memory face-pipeline diagnostics from DOOODS2F."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

FACE_DIAGNOSTIC_STATES = {
    "disabled",
    "loading",
    "ready",
    "no_face",
    "face_detected",
    "error",
}

FACE_DIAGNOSTIC_ERROR_CODES = {
    "embedding_invalid",
    "feature_failed",
    "inference_failed",
    "invalid_frame",
    "model_load_failed",
    "unsupported_opencv",
}

FACE_MATCH_STATES = {"matched", "unknown", "ambiguous"}


def _normalize_match_result(payload: Any) -> dict[str, Any] | None:
    """Validate optional face match result; never forward biometric embeddings."""
    if not isinstance(payload, dict):
        return None
    match_state = payload.get("state")
    if match_state is not None and match_state not in FACE_MATCH_STATES:
        return None
    person_id = payload.get("person_id")
    if person_id is not None and (
        not isinstance(person_id, str) or not person_id.strip()
    ):
        return None
    person_name = payload.get("person_name")
    if person_name is not None and (
        not isinstance(person_name, str) or not person_name.strip()
    ):
        return None
    similarity = payload.get("similarity")
    if similarity is not None and (
        isinstance(similarity, bool)
        or not isinstance(similarity, (int, float))
        or not math.isfinite(similarity)
        or not 0.0 <= similarity <= 1.0
    ):
        return None
    return {
        "state": match_state,
        "person_id": person_id.strip() if isinstance(person_id, str) else None,
        "person_name": person_name.strip() if isinstance(person_name, str) else None,
        "similarity": round(float(similarity), 4)
        if isinstance(similarity, (int, float))
        else None,
    }


def normalize_face_diagnostics(payload: Any) -> dict[str, Any] | None:
    """Whitelist numeric/status fields; never forward image or embedding data."""
    if not isinstance(payload, Mapping):
        return None

    state = payload.get("state")
    frames = payload.get("frames_processed_total")
    faces = payload.get("faces_detected")
    width = payload.get("largest_face_width_px")
    height = payload.get("largest_face_height_px")
    embedding_ready = payload.get("embedding_ready")
    inference_ms = payload.get("inference_ms")
    timestamp = payload.get("last_processed_at")
    error = payload.get("last_error")
    match = payload.get("match")

    if not isinstance(state, str) or state not in FACE_DIAGNOSTIC_STATES:
        return None
    if (
        isinstance(frames, bool)
        or not isinstance(frames, int)
        or not 0 <= frames <= 2**63 - 1
    ):
        return None
    if faces is not None and (
        isinstance(faces, bool)
        or not isinstance(faces, int)
        or not 0 <= faces <= 10_000
    ):
        return None
    for dimension in (width, height):
        if dimension is not None and (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or not 1 <= dimension <= 16_384
        ):
            return None
    if (width is None) != (height is None):
        return None
    if faces == 0 and (
        state != "no_face"
        or width is not None
        or embedding_ready is not False
    ):
        return None
    if state == "no_face" and faces != 0:
        return None
    if faces is not None and faces > 0 and (width is None or embedding_ready is None):
        return None
    if state == "face_detected" and (faces is None or faces == 0):
        return None
    if embedding_ready is not None and not isinstance(embedding_ready, bool):
        return None
    if inference_ms is not None and (
        isinstance(inference_ms, bool)
        or not isinstance(inference_ms, (int, float))
        or not math.isfinite(inference_ms)
        or not 0 <= inference_ms <= 60_000
    ):
        return None

    normalized_timestamp: str | None = None
    if timestamp is not None:
        if not isinstance(timestamp, str):
            return None
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed_timestamp.tzinfo is None:
            return None
        normalized_timestamp = parsed_timestamp.astimezone(UTC).isoformat()

    if error is not None and (
        not isinstance(error, str)
        or error not in FACE_DIAGNOSTIC_ERROR_CODES
    ):
        error = None

    normalized_match = _normalize_match_result(match)

    return {
        "state": state,
        "frames_processed_total": frames,
        "faces_detected": faces,
        "largest_face_width_px": width,
        "largest_face_height_px": height,
        "embedding_ready": embedding_ready,
        "match": normalized_match,
        "inference_ms": float(inference_ms) if inference_ms is not None else None,
        "last_processed_at": normalized_timestamp,
        "last_error": error,
    }
