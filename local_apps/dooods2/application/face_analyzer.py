"""Experimental YuNet/SFace processing for RTSP motion-candidate frames.

The HA telemetry remains aggregate-only. When explicitly configured, a private
bounded App store keeps recent face-positive frames/crops and an event log; no
embedding or identity is persisted by this analyzer.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import urllib.request
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger("doods.face_analyzer")

_MAX_ANALYSIS_SIDE = 1280
_MAX_CAMERA_STATES = 128
_EMBEDDING_DIMENSION = 128
_DEFAULT_YUNET_SCORE_THRESHOLD = 0.9
_MIN_YUNET_SCORE_THRESHOLD = 0.1
_MAX_YUNET_SCORE_THRESHOLD = 1.0


class FaceEmbeddingError(RuntimeError):
    """Safe status code for an explicitly requested gallery enrollment."""

    def __init__(self, code: str) -> None:
        allowed = {"models_not_ready", "invalid_frame", "face_not_found", "face_changed", "embedding_invalid", "feature_failed"}
        self.code = code if code in allowed else "feature_failed"
        super().__init__(self.code)


_MODEL_SPECS: dict[str, dict[str, Any]] = {
    "yunet": {
        "filename": "face_detection_yunet_2023mar.onnx",
        "url": (
            "https://huggingface.co/opencv/face_detection_yunet/resolve/main/"
            "face_detection_yunet_2023mar.onnx?download=true"
        ),
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "max_bytes": 2 * 1024 * 1024,
    },
    "sface": {
        "filename": "face_recognition_sface_2021dec.onnx",
        "url": (
            "https://huggingface.co/opencv/face_recognition_sface/resolve/main/"
            "face_recognition_sface_2021dec.onnx?download=true"
        ),
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "max_bytes": 64 * 1024 * 1024,
    },
}

_ERROR_CODES = {
    "embedding_invalid",
    "feature_failed",
    "inference_failed",
    "invalid_frame",
    "model_load_failed",
    "unsupported_opencv",
}


class FaceAnalyzer:
    """Run YuNet + SFace on motion candidates and retain bounded alpha diagnostics."""

    def __init__(
        self,
        model_directory: str | Path = "/data/face_models",
        *,
        yunet_score_threshold: float = _DEFAULT_YUNET_SCORE_THRESHOLD,
        face_match_threshold: float = 0.7,
        face_detection_cameras: list[str] | None = None,
        face_gallery_store: Any = None,
        debug_store: Any = None,
    ) -> None:
        """Initialize analyzer and validate the configurable alpha YuNet threshold."""
        self.model_directory = Path(model_directory)
        self.yunet_score_threshold = self._normalize_yunet_score_threshold(
            yunet_score_threshold
        )
        self.face_match_threshold = self._normalize_match_threshold(
            face_match_threshold
        )
        self.face_detection_cameras = set(
            camera for camera in (face_detection_cameras or []) if isinstance(camera, str)
        )
        self.face_gallery_store = face_gallery_store
        self.debug_store = debug_store
        self._state_lock = threading.RLock()
        self._initialize_lock = threading.Lock()
        self._inference_lock = threading.Lock()
        self._model_state = "loading"
        self._model_error: str | None = None
        self._cv2: Any = None
        self._detector: Any = None
        self._recognizer: Any = None
        self._camera_diagnostics: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def initialize(self) -> None:
        """Download pinned models if needed, verify hashes, and load OpenCV models."""
        with self._initialize_lock:
            with self._state_lock:
                if self._model_state == "ready":
                    return
                self._model_state = "loading"
                self._model_error = None

            try:
                model_paths = {
                    name: self._ensure_model_file(spec)
                    for name, spec in _MODEL_SPECS.items()
                }
                import cv2

                detector_api = getattr(cv2, "FaceDetectorYN", None)
                recognizer_api = getattr(cv2, "FaceRecognizerSF", None)
                detector_factory = getattr(detector_api, "create", None)
                recognizer_factory = getattr(recognizer_api, "create", None)
                if not callable(detector_factory) or not callable(recognizer_factory):
                    raise _FaceModelError("unsupported_opencv")

                detector = detector_factory(
                    str(model_paths["yunet"]),
                    "",
                    (320, 240),
                    self.yunet_score_threshold,
                    0.3,
                    5000,
                )
                recognizer = recognizer_factory(str(model_paths["sface"]), "")
                if detector is None or recognizer is None:
                    raise _FaceModelError("model_load_failed")
            except _FaceModelError as err:
                error_code = err.code
                with self._state_lock:
                    self._model_state = "error"
                    self._model_error = error_code
                _LOGGER.warning(
                    "Experimental face model initialization failed (%s)",
                    error_code,
                )
                return
            except Exception as err:  # Keep the main object-detection API available.
                with self._state_lock:
                    self._model_state = "error"
                    self._model_error = "model_load_failed"
                _LOGGER.warning(
                    "Experimental face model initialization failed (%s)",
                    type(err).__name__,
                )
                return

            with self._state_lock:
                self._cv2 = cv2
                self._detector = detector
                self._recognizer = recognizer
                self._model_state = "ready"
                self._model_error = None
            _LOGGER.info("Experimental YuNet/SFace diagnostics are ready")

    def model_status(self) -> dict[str, Any]:
        """Return model readiness and configuration without biometric material."""
        with self._state_lock:
            return {
                "state": self._model_state,
                "yunet_score_threshold": self.yunet_score_threshold,
                "last_error": self._model_error,
            }

    def snapshot(self, source_id: str) -> dict[str, Any]:
        """Return a camera's latest in-memory diagnostic summary only."""
        with self._state_lock:
            existing = self._camera_diagnostics.get(source_id)
            if existing is not None:
                self._camera_diagnostics.move_to_end(source_id)
                result = dict(existing)
            else:
                result = {
                    "state": self._model_state,
                    "frames_processed_total": 0,
                    "faces_detected": None,
                    "largest_face_width_px": None,
                    "largest_face_height_px": None,
                    "embedding_ready": None,
                    "inference_ms": None,
                    "last_processed_at": None,
                    "last_error": self._model_error,
                }
            return result

    def process(self, frame: Any, source_id: str) -> dict[str, Any]:
        """Analyze one motion-candidate frame and return no biometric payload."""
        # Если список камер для face detection задан, анализировать только их.
        # Остальные камеры получают диагностику без распознавания лиц.
        if (
            self.face_detection_cameras
            and source_id not in self.face_detection_cameras
        ):
            with self._state_lock:
                previous = self._camera_diagnostics.get(source_id, {})
                frame_count = int(previous.get("frames_processed_total", 0)) + 1
            diagnostics = {
                "state": "disabled",
                "frames_processed_total": frame_count,
                "faces_detected": None,
                "largest_face_width_px": None,
                "largest_face_height_px": None,
                "embedding_ready": None,
                "inference_ms": None,
                "last_processed_at": datetime.now(UTC).isoformat(),
                "last_error": None,
            }
            with self._state_lock:
                self._camera_diagnostics[source_id] = diagnostics
                self._camera_diagnostics.move_to_end(source_id)
                while len(self._camera_diagnostics) > _MAX_CAMERA_STATES:
                    self._camera_diagnostics.popitem(last=False)
            return diagnostics

        with self._state_lock:
            if self._model_state != "ready" or self._cv2 is None:
                return self.snapshot(source_id)
            cv2 = self._cv2
            detector = self._detector
            recognizer = self._recognizer
            previous = self._camera_diagnostics.get(source_id, {})
            frame_count = int(previous.get("frames_processed_total", 0)) + 1

        started = time.perf_counter()
        faces_detected: int | None = None
        face_width: int | None = None
        face_height: int | None = None
        embedding_ready: bool | None = None
        face_detections: list[dict[str, Any]] = []
        match_result: dict[str, Any] | None = None
        state = "error"
        error_code: str | None = None

        try:
            height, width = self._frame_size(frame)
            if width <= 0 or height <= 0:
                raise _FaceInferenceError("invalid_frame")

            scale = min(1.0, _MAX_ANALYSIS_SIDE / float(max(width, height)))
            if scale < 1.0:
                analysis_width = max(1, round(width * scale))
                analysis_height = max(1, round(height * scale))
                analysis_frame = cv2.resize(
                    frame,
                    (analysis_width, analysis_height),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                analysis_frame = frame
                analysis_width = width
                analysis_height = height

            with self._inference_lock:
                detector.setInputSize((analysis_width, analysis_height))
                _result, detected_faces = detector.detect(analysis_frame)
                rows = list(detected_faces) if detected_faces is not None else []
                faces_detected = len(rows)
                for face_index, face in enumerate(rows):
                    x = round(float(face[0]) * width / analysis_width)
                    y = round(float(face[1]) * height / analysis_height)
                    box_width = max(1, round(float(face[2]) * width / analysis_width))
                    box_height = max(1, round(float(face[3]) * height / analysis_height))
                    raw_score = float(face[-1]) if len(face) >= 15 else None
                    score = (
                        min(1.0, max(0.0, raw_score))
                        if raw_score is not None and math.isfinite(raw_score)
                        else None
                    )
                    face_detections.append(
                        {
                            "face_index": face_index,
                            "bbox": {
                                "x": max(0, x),
                                "y": max(0, y),
                                "width": box_width,
                                "height": box_height,
                            },
                            "score": score,
                        }
                    )
                if not rows:
                    state = "no_face"
                    embedding_ready = False
                else:
                    largest_face = max(
                        rows,
                        key=lambda face: float(face[2]) * float(face[3]),
                    )
                    face_width = max(
                        1,
                        round(float(largest_face[2]) * width / analysis_width),
                    )
                    face_height = max(
                        1,
                        round(float(largest_face[3]) * height / analysis_height),
                    )
                    try:
                        aligned_face = recognizer.alignCrop(
                            analysis_frame,
                            largest_face,
                        )
                        feature = recognizer.feature(aligned_face)
                        self._validate_feature(feature)
                    except _FaceInferenceError:
                        raise
                    except Exception as err:
                        raise _FaceInferenceError("feature_failed") from err
                    state = "face_detected"
                    embedding_ready = True

                    # Сопоставление с галереей людей
                    if self.face_gallery_store is not None:
                        try:
                            people = self.face_gallery_store.get_people_with_embeddings()
                            if people:
                                feature_list = [float(v) for v in feature.flatten().tolist()]
                                best_match = None
                                best_similarity = 0.0
                                second_best_similarity = 0.0
                                for person in people:
                                    for sample in person.get("samples", []):
                                        sample_embedding = sample.get("embedding")
                                        if not sample_embedding:
                                            continue
                                        similarity = self._cosine_similarity(
                                            feature_list,
                                            [float(v) for v in sample_embedding],
                                        )
                                        if similarity > best_similarity:
                                            second_best_similarity = best_similarity
                                            best_similarity = similarity
                                            best_match = person
                                        elif similarity > second_best_similarity:
                                            second_best_similarity = similarity
                                if best_match is not None:
                                    if best_similarity >= self.face_match_threshold:
                                        gap = best_similarity - second_best_similarity
                                        if gap >= 0.1:
                                            match_result = {
                                                "state": "matched",
                                                "person_id": best_match["id"],
                                                "person_name": best_match["name"],
                                                "similarity": round(best_similarity, 4),
                                            }
                                        else:
                                            match_result = {
                                                "state": "ambiguous",
                                                "person_id": best_match["id"],
                                                "person_name": best_match["name"],
                                                "similarity": round(best_similarity, 4),
                                            }
                                    else:
                                        match_result = {
                                            "state": "unknown",
                                        }
                        except Exception as err:
                            _LOGGER.debug(
                                "Face gallery matching failed (%s)",
                                type(err).__name__,
                            )
        except _FaceInferenceError as err:
            error_code = err.code
            state = "error"
            if faces_detected is None:
                embedding_ready = False
        except Exception as err:
            error_code = "inference_failed"
            state = "error"
            if faces_detected is None:
                embedding_ready = False
            _LOGGER.debug(
                "Experimental face inference failed (%s)",
                type(err).__name__,
            )

        elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
        diagnostics = {
            "state": state,
            "frames_processed_total": frame_count,
            "faces_detected": faces_detected,
            "largest_face_width_px": face_width,
            "largest_face_height_px": face_height,
            "embedding_ready": embedding_ready,
            "match": match_result,
            "inference_ms": round(elapsed_ms, 2),
            "last_processed_at": datetime.now(UTC).isoformat(),
            "last_error": error_code if error_code in _ERROR_CODES else None,
        }
        with self._state_lock:
            self._camera_diagnostics[source_id] = diagnostics
            self._camera_diagnostics.move_to_end(source_id)
            while len(self._camera_diagnostics) > _MAX_CAMERA_STATES:
                self._camera_diagnostics.popitem(last=False)

        if self.debug_store is not None:
            full_frame_jpeg: bytes | None = None
            face_crops_jpeg: list[bytes] = []
            if face_detections:
                try:
                    full_frame_jpeg, face_crops_jpeg = self._encode_debug_capture(
                        cv2,
                        frame,
                        face_detections,
                    )
                except Exception as err:
                    _LOGGER.debug(
                        "Experimental face capture encoding failed (%s)",
                        type(err).__name__,
                    )
            try:
                self.debug_store.record_result(
                    source_id=source_id,
                    diagnostics=diagnostics,
                    face_detections=face_detections,
                    yunet_score_threshold=self.yunet_score_threshold,
                    full_frame_jpeg=full_frame_jpeg,
                    face_crops_jpeg=face_crops_jpeg,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Experimental face-debug event was not queued (%s)",
                    type(err).__name__,
                )
        return dict(diagnostics)

    def match_frame(
        self,
        frame: Any,
        source_id: str,
    ) -> dict[str, Any] | None:
        """Run face detection + SFace embedding + gallery matching on a frame.

        Returns the match result dict (state, person_id, person_name, similarity)
        or None when no match is possible. Does not write to the debug store.
        """
        with self._state_lock:
            if self._model_state != "ready" or self._cv2 is None:
                return None
            cv2 = self._cv2
            detector = self._detector
            recognizer = self._recognizer

        height, width = self._frame_size(frame)
        if width <= 0 or height <= 0:
            return None

        scale = min(1.0, _MAX_ANALYSIS_SIDE / float(max(width, height)))
        if scale < 1.0:
            analysis_width = max(1, round(width * scale))
            analysis_height = max(1, round(height * scale))
            analysis_frame = cv2.resize(
                frame,
                (analysis_width, analysis_height),
                interpolation=cv2.INTER_AREA,
            )
        else:
            analysis_frame = frame
            analysis_width = width
            analysis_height = height

        with self._inference_lock:
            try:
                detector.setInputSize((analysis_width, analysis_height))
                _result, detected_faces = detector.detect(analysis_frame)
                rows = list(detected_faces) if detected_faces is not None else []
                if not rows:
                    return None
                largest_face = max(
                    rows,
                    key=lambda face: float(face[2]) * float(face[3]),
                )
                aligned_face = recognizer.alignCrop(analysis_frame, largest_face)
                feature = recognizer.feature(aligned_face)
                self._validate_feature(feature)
            except _FaceInferenceError:
                return None
            except Exception:
                return None

        # Сопоставление с галереей людей
        if self.face_gallery_store is None:
            return None
        try:
            people = self.face_gallery_store.get_people_with_embeddings()
            if not people:
                return None
            feature_list = [float(v) for v in feature.flatten().tolist()]
            best_match = None
            best_similarity = 0.0
            second_best_similarity = 0.0
            for person in people:
                for sample in person.get("samples", []):
                    sample_embedding = sample.get("embedding")
                    if not sample_embedding:
                        continue
                    similarity = self._cosine_similarity(
                        feature_list,
                        [float(v) for v in sample_embedding],
                    )
                    if similarity > best_similarity:
                        second_best_similarity = best_similarity
                        best_similarity = similarity
                        best_match = person
                    elif similarity > second_best_similarity:
                        second_best_similarity = similarity
            if best_match is None:
                return None
            if best_similarity < self.face_match_threshold:
                return None
            gap = best_similarity - second_best_similarity
            if gap < 0.1:
                return {
                    "state": "ambiguous",
                    "person_id": best_match["id"],
                    "person_name": best_match["name"],
                    "similarity": round(best_similarity, 4),
                }
            return {
                "state": "matched",
                "person_id": best_match["id"],
                "person_name": best_match["name"],
                "similarity": round(best_similarity, 4),
            }
        except Exception:
            return None

    @staticmethod
    def _encode_debug_capture(
        cv2: Any,
        frame: Any,
        face_detections: list[dict[str, Any]],
    ) -> tuple[bytes, list[bytes]]:
        """Encode one full frame and padded face crops for the private ring buffer."""
        quality = int(getattr(cv2, "IMWRITE_JPEG_QUALITY", 1))
        encode_parameters = [quality, 88]
        success, encoded_frame = cv2.imencode(".jpg", frame, encode_parameters)
        if not success:
            raise ValueError("Could not encode full frame")
        crop_images: list[bytes] = []
        frame_height, frame_width = frame.shape[:2]
        for face in face_detections:
            bbox = face["bbox"]
            x = int(bbox["x"])
            y = int(bbox["y"])
            width = int(bbox["width"])
            height = int(bbox["height"])
            padding_x = max(8, round(width * 0.18))
            padding_y = max(8, round(height * 0.18))
            left = max(0, x - padding_x)
            top = max(0, y - padding_y)
            right = min(frame_width, x + width + padding_x)
            bottom = min(frame_height, y + height + padding_y)
            crop = frame[top:bottom, left:right]
            if getattr(crop, "size", 0) <= 0:
                raise ValueError("Face crop is empty")
            success, encoded_crop = cv2.imencode(".jpg", crop, encode_parameters)
            if not success:
                raise ValueError("Could not encode face crop")
            crop_images.append(encoded_crop.tobytes())
        return encoded_frame.tobytes(), crop_images

    def embedding_from_capture(
        self,
        jpeg_bytes: bytes,
        *,
        expected_bbox: dict[str, int],
    ) -> list[float]:
        """Recompute an embedding from a retained frame after explicit user choice."""
        with self._state_lock:
            if self._model_state != "ready" or self._cv2 is None:
                raise FaceEmbeddingError("models_not_ready")
            cv2 = self._cv2
            detector = self._detector
            recognizer = self._recognizer
        try:
            import numpy as np

            if not isinstance(jpeg_bytes, bytes) or not jpeg_bytes.startswith(b"\xff\xd8\xff"):
                raise FaceEmbeddingError("invalid_frame")
            encoded = np.frombuffer(jpeg_bytes, dtype=np.uint8)
            frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            height, width = self._frame_size(frame)
        except FaceEmbeddingError:
            raise
        except Exception as err:
            raise FaceEmbeddingError("invalid_frame") from err
        if width <= 0 or height <= 0:
            raise FaceEmbeddingError("invalid_frame")

        scale = min(1.0, _MAX_ANALYSIS_SIDE / float(max(width, height)))
        if scale < 1.0:
            analysis_width = max(1, round(width * scale))
            analysis_height = max(1, round(height * scale))
            analysis_frame = cv2.resize(
                frame,
                (analysis_width, analysis_height),
                interpolation=cv2.INTER_AREA,
            )
        else:
            analysis_frame = frame
            analysis_width = width
            analysis_height = height

        with self._inference_lock:
            try:
                detector.setInputSize((analysis_width, analysis_height))
                _result, detected_faces = detector.detect(analysis_frame)
                rows = list(detected_faces) if detected_faces is not None else []
                if not rows:
                    raise FaceEmbeddingError("face_not_found")
                expected = {
                    key: float(expected_bbox[key])
                    for key in ("x", "y", "width", "height")
                }
                best_row = None
                best_iou = 0.0
                for row in rows:
                    current = {
                        "x": float(row[0]) * width / analysis_width,
                        "y": float(row[1]) * height / analysis_height,
                        "width": float(row[2]) * width / analysis_width,
                        "height": float(row[3]) * height / analysis_height,
                    }
                    iou = self._bbox_iou(expected, current)
                    if iou > best_iou:
                        best_iou = iou
                        best_row = row
                if best_row is None or best_iou < 0.2:
                    raise FaceEmbeddingError("face_changed")
                aligned_face = recognizer.alignCrop(analysis_frame, best_row)
                feature = recognizer.feature(aligned_face)
                self._validate_feature(feature)
                return [float(value) for value in feature.flatten().tolist()]
            except FaceEmbeddingError:
                raise
            except _FaceInferenceError as err:
                raise FaceEmbeddingError(err.code) from err
            except Exception as err:
                raise FaceEmbeddingError("feature_failed") from err

    @staticmethod
    def _bbox_iou(first: dict[str, float], second: dict[str, float]) -> float:
        """Calculate overlap of two original-resolution face boxes."""
        left = max(first["x"], second["x"])
        top = max(first["y"], second["y"])
        right = min(first["x"] + first["width"], second["x"] + second["width"])
        bottom = min(first["y"] + first["height"], second["y"] + second["height"])
        intersection = max(0.0, right - left) * max(0.0, bottom - top)
        first_area = max(0.0, first["width"]) * max(0.0, first["height"])
        second_area = max(0.0, second["width"]) * max(0.0, second["height"])
        union = first_area + second_area - intersection
        return intersection / union if union > 0.0 else 0.0

    @staticmethod
    def _normalize_yunet_score_threshold(value: Any) -> float:
        """Accept only a finite threshold in the Home Assistant App schema range."""
        try:
            threshold = float(value)
        except (TypeError, ValueError, OverflowError):
            threshold = _DEFAULT_YUNET_SCORE_THRESHOLD
        if (
            isinstance(value, bool)
            or not math.isfinite(threshold)
            or not _MIN_YUNET_SCORE_THRESHOLD <= threshold <= _MAX_YUNET_SCORE_THRESHOLD
        ):
            _LOGGER.warning(
                "Invalid YuNet score threshold; using default %.1f",
                _DEFAULT_YUNET_SCORE_THRESHOLD,
            )
            return _DEFAULT_YUNET_SCORE_THRESHOLD
        return threshold

    @staticmethod
    def _normalize_match_threshold(value: Any) -> float:
        """Accept only a finite face match threshold in the 0.1–1.0 range."""
        try:
            threshold = float(value)
        except (TypeError, ValueError, OverflowError):
            return 0.7
        if (
            isinstance(value, bool)
            or not math.isfinite(threshold)
            or not 0.1 <= threshold <= 1.0
        ):
            _LOGGER.warning(
                "Invalid face match threshold; using default %.1f",
                0.7,
            )
            return 0.7
        return threshold

    @staticmethod
    def _cosine_similarity(first: list[float], second: list[float]) -> float:
        """Cosine similarity between two embedding vectors (0.0–1.0)."""
        if not first or not second or len(first) != len(second):
            return 0.0
        dot = 0.0
        norm_a = 0.0
        norm_b = 0.0
        for a, b in zip(first, second):
            dot += a * b
            norm_a += a * a
            norm_b += b * b
        if norm_a <= 1e-24 or norm_b <= 1e-24:
            return 0.0
        return dot / math.sqrt(norm_a * norm_b)

    def _ensure_model_file(self, spec: dict[str, Any]) -> Path:
        """Return a SHA-256-verified model file, downloading it atomically if absent."""
        self.model_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.model_directory.chmod(0o700)
        except OSError:
            pass

        path = self.model_directory / str(spec["filename"])
        expected_hash = str(spec["sha256"])
        if path.is_file() and self._sha256(path) == expected_hash:
            return path
        if path.exists():
            path.unlink()

        temporary_path = path.with_name(f"{path.name}.part")
        if temporary_path.exists():
            temporary_path.unlink()
        digest = hashlib.sha256()
        byte_count = 0
        max_bytes = int(spec["max_bytes"])
        request = urllib.request.Request(
            str(spec["url"]),
            headers={"User-Agent": "DOOODS2F-face-alpha/1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                with temporary_path.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        byte_count += len(chunk)
                        if byte_count > max_bytes:
                            raise _FaceModelError("model_load_failed")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != expected_hash:
                raise _FaceModelError("model_load_failed")
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            temporary_path.replace(path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return path

    @staticmethod
    def _sha256(path: Path) -> str:
        """Hash a model incrementally without loading the full model into memory."""
        digest = hashlib.sha256()
        with path.open("rb") as model_file:
            while chunk := model_file.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _frame_size(frame: Any) -> tuple[int, int]:
        """Return height and width from an OpenCV-style frame array."""
        shape = getattr(frame, "shape", None)
        if not isinstance(shape, tuple) or len(shape) < 2:
            raise _FaceInferenceError("invalid_frame")
        try:
            return int(shape[0]), int(shape[1])
        except (TypeError, ValueError, OverflowError) as err:
            raise _FaceInferenceError("invalid_frame") from err

    @staticmethod
    def _validate_feature(feature: Any) -> None:
        """Check SFace output shape/finiteness without retaining its values."""
        if feature is None:
            raise _FaceInferenceError("feature_failed")
        try:
            flat_feature = feature.flatten()
            values = flat_feature.tolist()
            if len(values) != _EMBEDDING_DIMENSION:
                raise _FaceInferenceError("embedding_invalid")
            if any(not math.isfinite(float(value)) for value in values):
                raise _FaceInferenceError("embedding_invalid")
        except _FaceInferenceError:
            raise
        except Exception as err:
            raise _FaceInferenceError("embedding_invalid") from err


class _FaceModelError(RuntimeError):
    """Internal non-sensitive model initialization status."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "model_load_failed"
        super().__init__(self.code)


class _FaceInferenceError(RuntimeError):
    """Internal non-sensitive frame-processing status."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _ERROR_CODES else "inference_failed"
        super().__init__(self.code)
