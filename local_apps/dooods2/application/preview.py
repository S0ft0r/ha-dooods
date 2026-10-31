"""Временное in-memory хранилище кадров для редактора зон."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

_SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DEFAULT_TTL_SECONDS = 300.0
_DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_SOURCES = 12
_MAX_SOURCE_NAME_LENGTH = 128


class PreviewValidationError(ValueError):
    """Ошибка идентификатора источника, имени или JPEG-кадра."""


class PreviewTooLargeError(PreviewValidationError):
    """Размер кадра превышает установленный лимит памяти."""


@dataclass(frozen=True, slots=True)
class PreviewFrame:
    """Последний JPEG-кадр одного источника, хранящийся только в RAM."""

    source_id: str
    source_name: str
    image: bytes
    stored_at: float


class PreviewStore:
    """Ограниченное хранилище последних временных кадров без записи на диск."""

    def __init__(
        self,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
        max_sources: int = _DEFAULT_MAX_SOURCES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("TTL предпросмотра должен быть больше нуля")
        if max_frame_bytes <= 0 or max_sources <= 0:
            raise ValueError("Лимиты предпросмотра должны быть больше нуля")
        self.ttl_seconds = float(ttl_seconds)
        self.max_frame_bytes = max_frame_bytes
        self.max_sources = max_sources
        self._clock = clock or time.monotonic
        self._frames: OrderedDict[str, PreviewFrame] = OrderedDict()
        self._lock = threading.RLock()

    def _validate_source_id(self, source_id: str) -> None:
        """Отклонить пустой или небезопасный идентификатор источника."""
        if not isinstance(source_id, str) or not _SOURCE_ID_PATTERN.fullmatch(source_id):
            raise PreviewValidationError("Некорректный source_id")

    def _expire(self, now: float) -> None:
        """Удалить все кадры, срок хранения которых истёк."""
        expired = [
            source_id
            for source_id, frame in self._frames.items()
            if now - frame.stored_at >= self.ttl_seconds
        ]
        for source_id in expired:
            self._frames.pop(source_id, None)

    def put(
        self,
        source_id: str,
        source_name: str,
        image: bytes,
    ) -> PreviewFrame:
        """Сохранить новый JPEG-кадр в памяти, заменив предыдущий кадр источника."""
        self._validate_source_id(source_id)
        if not isinstance(source_name, str):
            raise PreviewValidationError("source_name должен быть строкой")
        normalized_name = source_name.strip() or source_id
        if len(normalized_name) > _MAX_SOURCE_NAME_LENGTH:
            raise PreviewValidationError(
                f"source_name не должен быть длиннее {_MAX_SOURCE_NAME_LENGTH} символов"
            )
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized_name):
            raise PreviewValidationError("source_name содержит управляющие символы")
        if not isinstance(image, bytes) or not image:
            raise PreviewValidationError("Кадр должен быть непустым JPEG в bytes")
        if len(image) > self.max_frame_bytes:
            raise PreviewTooLargeError("Размер JPEG превышает лимит предпросмотра")
        if not image.startswith(b"\xff\xd8\xff"):
            raise PreviewValidationError("Кадр не распознан как JPEG")

        now = self._clock()
        frame = PreviewFrame(
            source_id=source_id,
            source_name=normalized_name,
            image=image,
            stored_at=now,
        )
        with self._lock:
            self._expire(now)
            self._frames.pop(source_id, None)
            self._frames[source_id] = frame
            while len(self._frames) > self.max_sources:
                self._frames.popitem(last=False)
        return frame

    def purge_expired(self) -> int:
        """Удалить просроченные кадры и вернуть число освобождённых слотов."""
        now = self._clock()
        with self._lock:
            previous_count = len(self._frames)
            self._expire(now)
            return previous_count - len(self._frames)

    def get(self, source_id: str) -> PreviewFrame | None:
        """Получить кадр, удалив просроченное значение перед чтением."""
        self._validate_source_id(source_id)
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._frames.get(source_id)

    def list_frames(self) -> list[PreviewFrame]:
        """Вернуть активные источники от самого нового к самому старому."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            return list(self._frames.values())[::-1]

    def delete(self, source_id: str) -> bool:
        """Удалить кадр одного источника; вернуть false, если его нет."""
        self._validate_source_id(source_id)
        with self._lock:
            self._expire(self._clock())
            return self._frames.pop(source_id, None) is not None

    def remaining_seconds(self, frame: PreviewFrame) -> float:
        """Вернуть оставшееся время жизни кадра с округлением до десятых."""
        remaining = self.ttl_seconds - (self._clock() - frame.stored_at)
        return round(max(0.0, remaining), 1)
