"""Результат проверки камеры и сохранение последнего подходящего JPEG."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class CameraScanResult:
    """Последние счётчики сканирования и кадр с прошедшими фильтры детекциями."""

    counts: dict[str, int]
    confidences: dict[str, float]
    detections: tuple[dict[str, Any], ...]
    annotated_image: bytes | None
    checked_at: datetime | None
    image_checked_at: datetime | None = None
    error: str | None = None
    source_frame_signature: bytes | None = None


def retain_last_accepted_image(
    previous: CameraScanResult | None,
    current: CameraScanResult,
) -> CameraScanResult:
    """Менять JPEG и его timestamp только при новом принятом кадре с детекциями."""
    source_frame_signature = current.source_frame_signature
    if source_frame_signature is None and previous is not None:
        source_frame_signature = previous.source_frame_signature

    if current.detections and current.annotated_image is not None:
        return replace(
            current,
            image_checked_at=current.checked_at,
            source_frame_signature=source_frame_signature,
        )
    return replace(
        current,
        annotated_image=previous.annotated_image if previous is not None else None,
        image_checked_at=(
            previous.image_checked_at if previous is not None else None
        ),
        source_frame_signature=source_frame_signature,
    )
