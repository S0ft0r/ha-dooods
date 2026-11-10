"""Private, bounded face-analysis event log and capture ring buffer.

This alpha store keeps JPEGs inside the App's private /data directory. It never
stores face embeddings, person names, or RTSP URLs. Captures are grouped by
analysis event so a full frame and its face crops expire together.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("doods.face_debug_store")
_EVENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ALLOWED_STATES = {"face_detected", "no_face", "error"}
_DEFAULT_MAX_CAPTURES = 100
_DEFAULT_MAX_LOG_ENTRIES = 1000
_DEFAULT_MAX_MATCHES = 30
_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_DEFAULT_MIN_FACE_WIDTH = 100
_DEFAULT_MIN_FACE_HEIGHT = 100
_DEFAULT_MAX_EVENT_BYTES = 24 * 1024 * 1024
_MAX_FACES_PER_EVENT = 32


class FaceDebugValidationError(ValueError):
    """Invalid capture ID, image, or face metadata."""


class FaceDebugStoreError(RuntimeError):
    """Could not initialize or persist the private face-debug store."""


class FaceDebugNotFoundError(LookupError):
    """A retained capture or image does not exist."""


class FaceDebugStore:
    """A bounded SQLite event log plus a private FIFO of JPEG capture groups."""

    def __init__(
        self,
        directory: str | Path = "/data/face_debug",
        *,
        max_captures: int = _DEFAULT_MAX_CAPTURES,
        max_log_entries: int = _DEFAULT_MAX_LOG_ENTRIES,
        max_image_bytes: int = _DEFAULT_MAX_IMAGE_BYTES,
        max_event_bytes: int = _DEFAULT_MAX_EVENT_BYTES,
        queue_size: int = 16,
        min_face_width: int = _DEFAULT_MIN_FACE_WIDTH,
        min_face_height: int = _DEFAULT_MIN_FACE_HEIGHT,
        max_matches: int = _DEFAULT_MAX_MATCHES,
    ) -> None:
        if max_captures < 1 or max_log_entries < 1 or queue_size < 1:
            raise ValueError("Face-debug limits must be positive")
        if max_image_bytes < 3 or max_event_bytes < max_image_bytes:
            raise ValueError("Face-debug image limits are invalid")
        if min_face_width < 1:
            raise ValueError("min_face_width must be positive")
        if min_face_height < 1:
            raise ValueError("min_face_height must be positive")
        if max_matches < 1:
            raise ValueError("max_matches must be positive")
        self.directory = Path(directory)
        self.capture_directory = self.directory / "captures"
        self.match_directory = self.directory / "matches"
        self.database_path = self.directory / "events.sqlite3"
        self.max_captures = int(max_captures)
        self.max_log_entries = int(max_log_entries)
        self.max_image_bytes = int(max_image_bytes)
        self.max_event_bytes = int(max_event_bytes)
        self.min_face_width = int(min_face_width)
        self.min_face_height = int(min_face_height)
        self.max_matches = int(max_matches)
        self._has_new_frames = False
        self._has_new_matches = False
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._closed = False
        try:
            self._make_private_directory(self.directory)
            self._make_private_directory(self.capture_directory)
            self._make_private_directory(self.match_directory)
            self._connection = sqlite3.connect(
                self.database_path,
                timeout=10,
                check_same_thread=False,
                isolation_level=None,
            )
            os.chmod(self.database_path, 0o600)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS logs ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "payload_json TEXT NOT NULL, "
                "capture_id TEXT)"
            )
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS captures ("
                "capture_id TEXT PRIMARY KEY, "
                "created_at TEXT NOT NULL, "
                "metadata_json TEXT NOT NULL)"
            )
            os.chmod(self.database_path, 0o600)
            self._prune_startup_state()
        except (OSError, sqlite3.Error) as err:
            raise FaceDebugStoreError("Could not initialize private face-debug storage") from err

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    @staticmethod
    def _safe_source_id(source_id: Any) -> str:
        """Keep useful entity IDs, but never persist an arbitrary stream URL."""
        if isinstance(source_id, str) and _SOURCE_ID_PATTERN.fullmatch(source_id):
            return source_id
        raw = source_id if isinstance(source_id, str) else ""
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"source-{digest}"

    @staticmethod
    def _validate_capture_id(capture_id: Any) -> str:
        if not isinstance(capture_id, str) or not _EVENT_ID_PATTERN.fullmatch(capture_id):
            raise FaceDebugValidationError("Invalid capture ID")
        return capture_id

    @staticmethod
    def _validate_face_detections(face_detections: Any) -> list[dict[str, Any]]:
        if not isinstance(face_detections, list) or len(face_detections) > _MAX_FACES_PER_EVENT:
            raise FaceDebugValidationError("Invalid face-detection list")
        safe_faces: list[dict[str, Any]] = []
        for index, face in enumerate(face_detections):
            if not isinstance(face, dict):
                raise FaceDebugValidationError("Face detection must be an object")
            bbox = face.get("bbox")
            if not isinstance(bbox, dict):
                raise FaceDebugValidationError("Face bbox must be an object")
            safe_bbox: dict[str, int] = {}
            for key in ("x", "y", "width", "height"):
                value = bbox.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    raise FaceDebugValidationError("Face bbox coordinates must be integers")
                if value < 0 or value > 100_000:
                    raise FaceDebugValidationError("Face bbox coordinate is out of range")
                safe_bbox[key] = value
            if safe_bbox["width"] < 1 or safe_bbox["height"] < 1:
                raise FaceDebugValidationError("Face bbox must have a positive size")
            raw_score = face.get("score")
            score: float | None = None
            if raw_score is not None:
                if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                    raise FaceDebugValidationError("Face score must be numeric")
                score = float(raw_score)
                if not 0.0 <= score <= 1.0:
                    raise FaceDebugValidationError("Face score is out of range")
            safe_faces.append(
                {
                    "face_index": index,
                    "bbox": safe_bbox,
                    "score": round(score, 6) if score is not None else None,
                }
            )
        return safe_faces

    def start(self) -> None:
        """Start the bounded background disk writer."""
        with self._lock:
            if self._closed:
                raise FaceDebugStoreError("Face-debug store is closed")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="doods-face-debug-writer",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Drain queued writes and stop the background writer."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.join()
            self._queue.put(None)
            self._queue.join()
            thread.join(timeout=max(0.1, float(timeout)))
        with self._lock:
            self._thread = None
            if not self._closed:
                self._connection.close()
                self._closed = True

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait for queued events to be persisted; intended for shutdown/tests."""
        if self._thread is None or not self._thread.is_alive():
            return self._queue.unfinished_tasks == 0
        finished = threading.Event()

        def waiter() -> None:
            self._queue.join()
            finished.set()

        threading.Thread(target=waiter, daemon=True).start()
        return finished.wait(max(0.0, float(timeout)))

    def record_result(
        self,
        *,
        source_id: Any,
        diagnostics: dict[str, Any],
        face_detections: list[dict[str, Any]],
        yunet_score_threshold: float,
        full_frame_jpeg: bytes | None = None,
        face_crops_jpeg: list[bytes] | None = None,
    ) -> None:
        """Queue one analyzed candidate; images are retained only for face events."""
        if not isinstance(diagnostics, dict):
            raise FaceDebugValidationError("Diagnostics must be an object")
        state = diagnostics.get("state")
        if state not in _ALLOWED_STATES:
            return
        faces = self._validate_face_detections(face_detections)
        # Фильтруем слишком маленькие лица (люди далеко от камеры), чтобы не
        # сохранять их в буфер и не предлагать для регистрации.
        faces = [
            f for f in faces
            if f["bbox"]["width"] >= self.min_face_width and f["bbox"]["height"] >= self.min_face_height
        ]
        timestamp = diagnostics.get("last_processed_at")
        if not isinstance(timestamp, str) or not timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
        threshold = float(yunet_score_threshold)
        if not 0.0 <= threshold <= 1.0:
            raise FaceDebugValidationError("YuNet score threshold is out of range")
        inference_ms = diagnostics.get("inference_ms")
        if isinstance(inference_ms, bool) or not isinstance(inference_ms, (int, float)):
            inference_ms = None
        else:
            inference_ms = max(0.0, min(float(inference_ms), 60_000.0))
        raw_faces_count = diagnostics.get("faces_detected")
        if isinstance(raw_faces_count, bool) or not isinstance(raw_faces_count, int):
            raw_faces_count = len(faces) if state == "face_detected" else (0 if state == "no_face" else None)
        error_code = diagnostics.get("last_error")
        if not isinstance(error_code, str) or len(error_code) > 48:
            error_code = None

        match_diagnostic = diagnostics.get("match")
        if not isinstance(match_diagnostic, dict):
            match_diagnostic = None
        payload: dict[str, Any] = {
            "occurred_at": timestamp,
            "source_id": self._safe_source_id(source_id),
            "state": state,
            "faces_detected": raw_faces_count,
            "faces": faces,
            "yunet_score_threshold": round(threshold, 4),
            "inference_ms": round(inference_ms, 2) if inference_ms is not None else None,
            "last_error": error_code,
            "match": match_diagnostic,
            "capture_id": None,
            "capture_status": "not_applicable",
        }

        image_bytes: list[bytes] = []
        if faces:
            if (
                not isinstance(full_frame_jpeg, bytes)
                or not face_crops_jpeg
                or len(face_crops_jpeg) != len(faces)
            ):
                payload["capture_status"] = "image_unavailable"
            else:
                image_bytes = [full_frame_jpeg, *face_crops_jpeg]
                if any(not isinstance(item, bytes) for item in image_bytes):
                    image_bytes = []
                    payload["capture_status"] = "image_unavailable"
                elif any(
                    not item.startswith(b"\xff\xd8\xff")
                    or len(item) > self.max_image_bytes
                    for item in image_bytes
                ) or sum(map(len, image_bytes)) > self.max_event_bytes:
                    image_bytes = []
                    payload["capture_status"] = "too_large"
                else:
                    payload["capture_status"] = "queued"

        task = {
            "payload": payload,
            "image_bytes": image_bytes,
        }
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._persist(task)
            return
        try:
            self._queue.put_nowait(task)
        except queue.Full:
            payload["capture_status"] = "queue_full" if image_bytes else payload["capture_status"]
            self._write_log(payload, capture_id=None)

    def _worker(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    return
                self._persist(task)
            except Exception as err:
                _LOGGER.warning("Face-debug persistence failed (%s)", type(err).__name__)
            finally:
                self._queue.task_done()

    def _persist(self, task: dict[str, Any]) -> None:
        payload = dict(task["payload"])
        image_bytes: list[bytes] = task["image_bytes"]
        capture_id: str | None = None
        metadata: dict[str, Any] | None = None
        capture_directory: Path | None = None
        if image_bytes:
            capture_id = uuid.uuid4().hex
            payload["capture_id"] = capture_id
            payload["capture_status"] = "saving"
            capture_directory = self.capture_directory / capture_id
            try:
                metadata = self._write_capture(capture_id, payload, image_bytes)
                payload["capture_status"] = "saved"
            except Exception as err:
                if capture_directory.exists():
                    shutil.rmtree(capture_directory, ignore_errors=True)
                capture_id = None
                metadata = None
                payload["capture_id"] = None
                payload["capture_status"] = "save_failed"
                _LOGGER.warning("Face-debug image write failed (%s)", type(err).__name__)

        try:
            self._write_log(payload, capture_id=capture_id, capture_metadata=metadata)
        except Exception:
            if capture_directory is not None and capture_directory.exists():
                shutil.rmtree(capture_directory, ignore_errors=True)
            raise

        # Сохранить копию кадра в отдельном буфере совпадений (макс. 30 кадров).
        if (
            capture_directory is not None
            and metadata is not None
            and image_bytes
            and len(image_bytes) > 0
        ):
            match_diagnostic = payload.get("match")
            if isinstance(match_diagnostic, dict) and match_diagnostic.get("state") == "matched":
                try:
                    self._write_match_capture(
                        capture_id,
                        image_bytes[0],
                        metadata,
                        face_images=list(image_bytes[1:]),
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Face-debug match buffer write failed (%s)", type(err).__name__
                    )

    def _write_match_capture(
        self,
        capture_id: str,
        frame_jpeg: bytes,
        metadata: dict[str, Any],
        *,
        match_info: dict[str, Any] | None = None,
        face_images: list[bytes] | None = None,
    ) -> None:
        """Сохранить копию кадра-совпадения в отдельном приватном буфере.

        Хранит только последние max_matches кадров; старые удаляются FIFO.
        Идемпотентен: при повторном вызове для того же capture_id старые
        файлы перезаписываются, а не вызывают FileExistsError.
        """
        destination = self.match_directory / capture_id
        if destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
        self._make_private_directory(destination)
        try:
            self._write_private_file(destination / "frame.jpg", frame_jpeg)
            stored_faces = 0
            for index, image in enumerate(face_images or []):
                if not image:
                    continue
                self._write_private_file(destination / f"face-{index:02d}.jpg", image)
                stored_faces += 1
            meta = dict(metadata)
            if match_info:
                meta["person_name"] = match_info.get("person_name")
                meta["person_id"] = match_info.get("person_id")
                meta["similarity"] = match_info.get("similarity")
                meta["state"] = match_info.get("state", "matched")
                meta["match"] = {
                    key: match_info.get(key)
                    for key in (
                        "state",
                        "person_id",
                        "person_name",
                        "similarity",
                        "face_index",
                        "yunet_score_threshold",
                        "yunet_faces_detected",
                        "yunet_score",
                        "yunet_bbox",
                        "sface_threshold",
                        "sface_best_similarity",
                        "sface_second_best_similarity",
                        "sface_similarity_gap",
                        "sface_min_gap",
                        "sface_other_person",
                        "sface_sample_id",
                        "sface_sample_index",
                        "sface_samples_total",
                        "match_inference_ms",
                    )
                    if match_info.get(key) is not None
                }
            else:
                meta["match_state"] = "matched"
            meta["match_face_crops"] = stored_faces
            content = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self._write_private_file(destination / "metadata.json", content)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        self._prune_matches()
        with self._lock:
            self._has_new_matches = True

    def _prune_matches(self) -> None:
        """Оставить только последние max_matches кадров-совпадений."""
        with self._lock:
            match_dirs = sorted(
                (
                    d
                    for d in self.match_directory.iterdir()
                    if d.is_dir() and _EVENT_ID_PATTERN.fullmatch(d.name)
                ),
                key=lambda d: (
                    (d / "metadata.json").stat().st_mtime
                    if (d / "metadata.json").exists()
                    else 0
                ),
                reverse=True,
            )
        for stale in match_dirs[self.max_matches :]:
            shutil.rmtree(stale, ignore_errors=True)

    def list_matches(self, limit: int = _DEFAULT_MAX_MATCHES) -> list[dict[str, Any]]:
        """Вернуть метаданные сохранённых кадров-совпадений (новые сначала)."""
        limit = self._validate_limit(limit, self.max_matches)
        with self._lock:
            match_dirs = sorted(
                (
                    d
                    for d in self.match_directory.iterdir()
                    if d.is_dir() and _EVENT_ID_PATTERN.fullmatch(d.name)
                ),
                key=lambda d: (
                    (d / "metadata.json").stat().st_mtime
                    if (d / "metadata.json").exists()
                    else 0
                ),
                reverse=True,
            )
        result: list[dict[str, Any]] = []
        for d in match_dirs[:limit]:
            meta_path = d / "metadata.json"
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            result.append(meta)
        with self._lock:
            self._has_new_matches = False
        return result

    def get_match_image(
        self,
        capture_id: str,
        *,
        kind: str = "frame",
        face_index: int | None = None,
    ) -> bytes:
        """Вернуть JPEG кадра или кропа лица из буфера совпадений."""
        capture_id = self._validate_capture_id(capture_id)
        if kind == "frame":
            filename = "frame.jpg"
        elif kind == "face":
            if isinstance(face_index, bool) or not isinstance(face_index, int):
                raise FaceDebugValidationError("Face index must be an integer")
            if not 0 <= face_index < _MAX_FACES_PER_EVENT:
                raise FaceDebugValidationError("Face index is out of range")
            filename = f"face-{face_index:02d}.jpg"
        else:
            raise FaceDebugValidationError("Image kind must be frame or face")
        path = self.match_directory / capture_id / filename
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            # Записи, созданные до появления кропов в буфере совпадений, не
            # содержат face-NN.jpg. Пробуем исходный кадр: capture_id совпадает.
            fallback = self.capture_directory / capture_id / filename
            try:
                content = fallback.read_bytes()
            except FileNotFoundError as err:
                raise FaceDebugNotFoundError("Match image not found") from err
            except OSError as err:
                raise FaceDebugStoreError("Could not read match image") from err
        except OSError as err:
            raise FaceDebugStoreError("Could not read match image") from err
        if not content.startswith(b"\xff\xd8\xff") or len(content) > self.max_image_bytes:
            raise FaceDebugStoreError("Stored match image is invalid")
        return content

    def _write_capture(
        self,
        capture_id: str,
        payload: dict[str, Any],
        image_bytes: list[bytes],
    ) -> dict[str, Any]:
        self._validate_capture_id(capture_id)
        if len(image_bytes) != len(payload["faces"]) + 1:
            raise FaceDebugValidationError("Capture image count does not match face metadata")
        temporary = self.capture_directory / f".{capture_id}.tmp"
        destination = self.capture_directory / capture_id
        self._make_private_directory(temporary)
        try:
            self._write_private_file(temporary / "frame.jpg", image_bytes[0])
            for index, image in enumerate(image_bytes[1:]):
                self._write_private_file(temporary / f"face-{index:02d}.jpg", image)
            metadata = {
                "capture_id": capture_id,
                "occurred_at": payload["occurred_at"],
                "source_id": payload["source_id"],
                "faces_detected": payload["faces_detected"],
                "faces": payload["faces"],
                "yunet_score_threshold": payload["yunet_score_threshold"],
                "inference_ms": payload["inference_ms"],
                "match": payload.get("match"),
            }
            content = (json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
            self._write_private_file(temporary / "metadata.json", content)
            os.replace(temporary, destination)
            try:
                os.chmod(destination, 0o700)
            except OSError:
                pass
            with self._lock:
                self._has_new_frames = True
            return metadata
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    @staticmethod
    def _write_private_file(path: Path, content: bytes) -> None:
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _write_log(
        self,
        payload: dict[str, Any],
        *,
        capture_id: str | None,
        capture_metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_payload = dict(payload)
        safe_payload["capture_id"] = capture_id
        encoded = json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        with self._lock:
            if self._closed:
                return
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if capture_id is not None and capture_metadata is not None:
                    self._connection.execute(
                        "INSERT INTO captures(capture_id, created_at, metadata_json) VALUES (?, ?, ?)",
                        (
                            capture_id,
                            capture_metadata["occurred_at"],
                            json.dumps(capture_metadata, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                self._connection.execute(
                    "INSERT INTO logs(payload_json, capture_id) VALUES (?, ?)",
                    (encoded, capture_id),
                )
                self._connection.execute(
                    "DELETE FROM logs WHERE id NOT IN "
                    "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                    (self.max_log_entries,),
                )
                stale_rows = self._connection.execute(
                    "SELECT capture_id FROM captures ORDER BY rowid DESC LIMIT -1 OFFSET ?",
                    (self.max_captures,),
                ).fetchall()
                stale_ids = [str(row["capture_id"]) for row in stale_rows]
                for stale_id in stale_ids:
                    self._connection.execute(
                        "DELETE FROM captures WHERE capture_id = ?",
                        (stale_id,),
                    )
                self._connection.execute("COMMIT")
            except sqlite3.Error as err:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise FaceDebugStoreError("Could not append private face-debug event") from err
        for stale_id in stale_ids:
            shutil.rmtree(self.capture_directory / stale_id, ignore_errors=True)

    def _prune_startup_state(self) -> None:
        """Trim prior state and remove unfinished/orphaned capture directories."""
        with self._lock:
            self._connection.execute(
                "DELETE FROM logs WHERE id NOT IN "
                "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                (self.max_log_entries,),
            )
            stale_rows = self._connection.execute(
                "SELECT capture_id FROM captures ORDER BY rowid DESC LIMIT -1 OFFSET ?",
                (self.max_captures,),
            ).fetchall()
            for row in stale_rows:
                self._connection.execute(
                    "DELETE FROM captures WHERE capture_id = ?",
                    (str(row["capture_id"]),),
                )
            retained = {
                str(row["capture_id"])
                for row in self._connection.execute("SELECT capture_id FROM captures")
            }
        for child in self.capture_directory.iterdir():
            if not child.is_dir() or not _EVENT_ID_PATTERN.fullmatch(child.name) or child.name not in retained:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        pass

    def has_new_frames(self) -> bool:
        """Есть ли не прочитанные новые кадры (сбросится при list_captures)."""
        with self._lock:
            return self._has_new_frames

    def has_new_matches(self) -> bool:
        """Есть ли не прочитанные новые совпадения (сбросится при list_matches)."""
        with self._lock:
            return self._has_new_matches

    def status(self) -> dict[str, int]:
        """Return bounded store counts and configured limits."""
        with self._lock:
            captures = int(
                self._connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
            )
            logs = int(self._connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0])
            matches = len(
                [
                    d
                    for d in self.match_directory.iterdir()
                    if d.is_dir() and _EVENT_ID_PATTERN.fullmatch(d.name)
                ]
            )
        return {
            "captures": captures,
            "max_captures": self.max_captures,
            "log_entries": logs,
            "max_log_entries": self.max_log_entries,
            "matches": matches,
            "max_matches": self.max_matches,
        }

    def list_captures(self, limit: int = _DEFAULT_MAX_CAPTURES) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit, self.max_captures)
        with self._lock:
            rows = self._connection.execute(
                "SELECT metadata_json FROM captures ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            self._has_new_frames = False
        return [json.loads(row["metadata_json"]) for row in rows]

    def list_logs(self, limit: int = _DEFAULT_MAX_LOG_ENTRIES) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit, self.max_log_entries)
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload_json FROM logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def update_capture_match(
        self,
        capture_id: str,
        match: dict[str, Any],
    ) -> bool:
        """Обновить поле match в метаданных существующего кадра.

        Вызывается после rematch, чтобы на странице «Кадры» появились
        метки «Человек» и «Похожесть» для уже сохранённых кадров.
        Возвращает True при успешном обновлении.
        """
        capture_id = self._validate_capture_id(capture_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT metadata_json FROM captures WHERE capture_id = ?",
                (capture_id,),
            ).fetchone()
            if row is None:
                _LOGGER.warning(
                    "update_capture_match: capture %s not found in captures table", capture_id
                )
                return False
            meta = json.loads(row["metadata_json"])
            meta["match"] = match
            content = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "UPDATE captures SET metadata_json = ? WHERE capture_id = ?",
                    (content.decode("utf-8"), capture_id),
                )
                self._connection.execute("COMMIT")
                _LOGGER.info(
                    "update_capture_match: capture %s updated with match state=%s",
                    capture_id,
                    match.get("state"),
                )
                return True
            except sqlite3.Error as err:
                try:
                    self._connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                _LOGGER.warning(
                    "update_capture_match: capture %s update failed: %s", capture_id, err
                )
                raise FaceDebugStoreError(
                    "Could not update capture match metadata"
                )

    @staticmethod
    def _validate_limit(limit: Any, maximum: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise FaceDebugValidationError("Limit must be an integer")
        return max(1, min(limit, maximum))

    def get_capture(self, capture_id: str) -> dict[str, Any] | None:
        capture_id = self._validate_capture_id(capture_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT metadata_json FROM captures WHERE capture_id = ?",
                (capture_id,),
            ).fetchone()
        return json.loads(row["metadata_json"]) if row is not None else None

    def get_capture_image(
        self,
        capture_id: str,
        *,
        kind: str,
        face_index: int | None = None,
    ) -> bytes:
        capture_id = self._validate_capture_id(capture_id)
        if kind == "frame":
            filename = "frame.jpg"
        elif kind == "face":
            if isinstance(face_index, bool) or not isinstance(face_index, int):
                raise FaceDebugValidationError("Face index must be an integer")
            if not 0 <= face_index < _MAX_FACES_PER_EVENT:
                raise FaceDebugValidationError("Face index is out of range")
            filename = f"face-{face_index:02d}.jpg"
        else:
            raise FaceDebugValidationError("Image kind must be frame or face")
        metadata = self.get_capture(capture_id)
        if metadata is None:
            raise FaceDebugNotFoundError("Capture not found")
        if kind == "face" and face_index >= len(metadata.get("faces", [])):
            raise FaceDebugNotFoundError("Face crop not found")
        path = self.capture_directory / capture_id / filename
        try:
            content = path.read_bytes()
        except FileNotFoundError as err:
            raise FaceDebugNotFoundError("Capture image not found") from err
        except OSError as err:
            raise FaceDebugStoreError("Could not read capture image") from err
        if not content.startswith(b"\xff\xd8\xff") or len(content) > self.max_image_bytes:
            raise FaceDebugStoreError("Stored capture image is invalid")
        return content

    def get_capture_frame(self, capture_id: str) -> bytes:
        """Read a retained full frame for explicit, user-confirmed enrollment."""
        return self.get_capture_image(capture_id, kind="frame")

    def close(self) -> None:
        """Close the database when the store was never started."""
        self.stop()

