"""Постоянные настройки face-пайплайна внутри App.

Параметры App (config.yaml / env) в Home Assistant только для чтения, поэтому
список камер, выбранных в интеграции, хранится рядом с галереей в /data.
Файл пишется атомарно: временный файл в том же каталоге и os.replace, иначе
оборванная запись оставила бы пустой настройку, которую нельзя восстановить.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

_LOGGER = logging.getLogger("doods.face_settings")

_DEFAULT_PATH = "/data/face_settings.json"
_MAX_CAMERAS = 64
_MAX_CAMERA_LENGTH = 128


class FaceSettingsError(RuntimeError):
    """Не удалось прочитать или сохранить настройки face-пайплайна."""


def normalize_cameras(value: Any) -> list[str]:
    """Оставить непустые строки без дублей, сохранив порядок."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        camera = item.strip()
        if not camera or len(camera) > _MAX_CAMERA_LENGTH or camera in seen:
            continue
        seen.add(camera)
        normalized.append(camera)
        if len(normalized) >= _MAX_CAMERAS:
            break
    return normalized


class FaceSettingsStore:
    """Хранилище выбранных камер для face detection в постоянном /data."""

    def __init__(self, path: str | Path = _DEFAULT_PATH) -> None:
        self.path = Path(path)

    def load_cameras(self) -> list[str] | None:
        """Вернуть сохранённый список или None, если настройка ещё не писалась."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise FaceSettingsError(str(err)) from err
        try:
            payload = json.loads(raw)
        except ValueError as err:
            raise FaceSettingsError("invalid json") from err
        if not isinstance(payload, dict):
            raise FaceSettingsError("payload is not an object")
        cameras = payload.get("face_detection_cameras")
        if cameras is None:
            return None
        return normalize_cameras(cameras)

    def save_cameras(self, cameras: Iterable[str]) -> list[str]:
        """Сохранить список камер и вернуть его в нормализованном виде."""
        normalized = normalize_cameras(cameras)
        payload = {
            "version": 1,
            "face_detection_cameras": normalized,
        }
        directory = self.path.parent
        temp_path: str | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            handle, temp_path = tempfile.mkstemp(
                dir=str(directory),
                prefix=".face_settings.",
                suffix=".tmp",
            )
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, self.path)
            temp_path = None
        except OSError as err:
            raise FaceSettingsError(str(err)) from err
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
        _LOGGER.debug(
            "Face detection cameras saved: %s",
            ", ".join(normalized) if normalized else "(none)",
        )
        return normalized
