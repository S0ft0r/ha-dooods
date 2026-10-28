"""Вспомогательные функции безопасной передачи временных кадров камер."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

MAX_PREVIEW_FRAME_BYTES = 4 * 1024 * 1024
PREVIEW_UPLOAD_INTERVAL_SECONDS = 30.0


def preview_source_id(entry_id: str, camera_entity_id: str) -> str:
    """Сформировать стабильный непрозрачный ID без публикации camera_entity_id."""
    identity = f"{entry_id}\0{camera_entity_id}".encode()
    digest = hashlib.sha256(identity).hexdigest()[:32]
    return f"camera-{digest}"


def is_preview_jpeg(image: bytes) -> bool:
    """Проверить JPEG-сигнатуру и лимит размера, заданный API предпросмотра."""
    return (
        isinstance(image, bytes)
        and image.startswith(b"\xff\xd8\xff")
        and len(image) <= MAX_PREVIEW_FRAME_BYTES
    )


def preview_source_name(
    friendly_name: object,
    camera_entity_id: str,
    source_id: str,
) -> str:
    """Выбрать читаемое имя источника, не используя ID HA как подпись."""
    if isinstance(friendly_name, str):
        normalized_name = "".join(
            character for character in friendly_name.strip() if character.isprintable()
        ).strip()
        if (
            normalized_name
            and camera_entity_id.casefold() not in normalized_name.casefold()
        ):
            return normalized_name[:128]
    return f"Камера {source_id[-6:]}"


class PreviewUploadLimiter:
    """Ограничить повторную передачу кадров одной камеры по монотонным часам."""

    def __init__(
        self,
        interval_seconds: float = PREVIEW_UPLOAD_INTERVAL_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("Интервал передачи preview должен быть больше нуля")
        self.interval_seconds = float(interval_seconds)
        self._clock = clock or time.monotonic
        self._last_upload: dict[str, float] = {}

    def should_upload(self, camera_entity_id: str) -> bool:
        """Разрешить первое сканирование и последующие попытки после интервала."""
        now = self._clock()
        last_upload = self._last_upload.get(camera_entity_id)
        if last_upload is not None and now - last_upload < self.interval_seconds:
            return False
        self._last_upload[camera_entity_id] = now
        return True
