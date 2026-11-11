import uvicorn
import odrpc
import json
import base64
import logging
import asyncio
import math
import threading
from urllib.parse import urlsplit
from fastapi import (
    status,
    FastAPI,
    HTTPException,
    Request,
    Header,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from streamer import Streamer
from motion_gate import MotionGate
from motion_stream import MotionStreamWorker
from capabilities import capabilities_payload
from auth import api_key_matches, is_authenticated_ingress_request
from profiles import ProfileStoreError, ProfileValidationError
from preview import PreviewStore, PreviewTooLargeError, PreviewValidationError
from profile_sources import ProfileSourceStore, ProfileSourceStoreError, ProfileSourceValidationError
from face_gallery import (
    FaceGalleryNotFoundError,
    FaceGalleryStoreError,
    FaceGalleryValidationError,
)
from face_analyzer import FaceEmbeddingError
from face_settings import FaceSettingsError
from face_debug_store import (
    FaceDebugNotFoundError,
    FaceDebugStoreError,
    FaceDebugValidationError,
)
from concurrent.futures import ThreadPoolExecutor
from prometheus_fastapi_instrumentator import Instrumentator

import tracemalloc
from pathlib import Path

_MAX_FACE_JSON_REQUEST_BYTES = 256 * 1024
_MAX_FACE_BACKUP_REQUEST_BYTES = 17 * 1024 * 1024


class API():
    def __init__(
        self,
        config,
        doods,
        preview_store=None,
        profile_source_store=None,
        face_gallery_store=None,
        face_analyzer=None,
        face_debug_store=None,
        face_settings_store=None,
    ):
        self.config = config
        self.doods = doods
        self.api = FastAPI()
        self.profile_store = getattr(doods, "profile_store", None)
        profiles_path = getattr(self.profile_store, "path", None)
        self.profile_source_store = profile_source_store
        if self.profile_source_store is None and profiles_path is not None:
            profiles_path = Path(profiles_path)
            source_store_path = profiles_path.with_name("profile_sources.json")
            if source_store_path.resolve() == profiles_path.resolve():
                source_store_path = profiles_path.with_name("profile_preview_sources.json")
            self.profile_source_store = ProfileSourceStore(source_store_path)
        self.preview_store = preview_store or PreviewStore()
        self.face_gallery_store = face_gallery_store
        self.face_analyzer = face_analyzer
        self.face_debug_store = face_debug_store
        self.face_settings_store = face_settings_store
        self.auth_key = getattr(config, "auth_key", "") or ""
        self.logger = logging.getLogger("doods.api")
        if not self.auth_key:
            self.logger.warning(
                "API key is not configured; legacy API routes remain unauthenticated "
                "and profile features are disabled."
            )
        self._preview_cleanup_task = None
        self._face_model_task = None

        async def cleanup_preview_frames():
            """Периодически освобождать память по истечении TTL кадров."""
            interval = min(30.0, self.preview_store.ttl_seconds)
            while True:
                await asyncio.sleep(interval)
                self.preview_store.purge_expired()

        @self.api.on_event("startup")
        async def start_preview_cleanup():
            if self.auth_key and self.preview_store is not None:
                self._preview_cleanup_task = asyncio.create_task(cleanup_preview_frames())
            if self.face_debug_store is not None:
                try:
                    self.face_debug_store.start()
                except FaceDebugStoreError as err:
                    self.logger.error("Face-debug writer could not start: %s", err)
            if self.face_analyzer is not None:
                # Download/load the pinned weights off the API event loop. Failure
                # is reported to diagnostic sensors and does not stop DOOODS2.
                self._face_model_task = asyncio.create_task(
                    asyncio.to_thread(self.face_analyzer.initialize)
                )

        @self.api.on_event("shutdown")
        async def stop_preview_cleanup():
            task = self._preview_cleanup_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._preview_cleanup_task = None
            task = self._face_model_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                self._face_model_task = None
            if self.face_debug_store is not None:
                try:
                    await asyncio.to_thread(self.face_debug_store.stop, 10.0)
                except Exception as err:
                    self.logger.warning(
                        "Face-debug writer shutdown failed (%s)",
                        type(err).__name__,
                    )

        def profiles_enabled():
            return bool(self.auth_key and self.profile_store is not None)

        def preview_enabled():
            return bool(self.auth_key and self.preview_store is not None)

        def require_profiles_enabled():
            if self.profile_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile storage is not configured.",
                )
            if not self.auth_key:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Set server.auth_key to enable profile features.",
                )

        def require_preview_enabled():
            if self.preview_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Preview storage is not configured.",
                )
            if not self.auth_key:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Set server.auth_key to enable camera previews.",
                )

        def face_gallery_enabled():
            return bool(self.auth_key and self.face_gallery_store is not None)

        def require_face_gallery_enabled():
            if self.face_gallery_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face gallery storage is unavailable.",
                )

        def face_debug_enabled():
            return bool(
                self.face_debug_store is not None
                and self.face_analyzer is not None
            )

        def require_face_debug_enabled():
            if self.face_debug_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Private face-debug storage is unavailable.",
                )
            if self.face_analyzer is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face analysis is unavailable.",
                )

        async def read_limited_json(request: Request, max_bytes: int):
            """Прочитать ограниченное JSON-тело без записи данных в логи."""
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="Request body must use application/json.",
                )
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as err:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid Content-Length header.",
                    ) from err
                if declared_length > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="JSON request exceeds the configured size limit.",
                    )
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="JSON request exceeds the configured size limit.",
                    )
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid JSON request body.",
                ) from err

        def is_protected_api_path(path: str) -> bool:
            return path in {
                "/detectors",
                "/detect",
                "/image",
                "/stream",
                "/motion/stream",
                "/memory",
                "/metrics",
                "/docs",
                "/openapi.json",
                "/redoc",
                "/profiles",
                "/preview",
            } or (
                path.startswith("/profiles/")
                or path.startswith("/preview/")
            )

        @self.api.middleware("http")
        async def enforce_api_key(request: Request, call_next):
            path = request.url.path
            if self.auth_key and is_protected_api_path(path):
                # Разрешить аутентифицированным пользователям Home Assistant
                # через Supervisor Ingress без API-ключа; прямые вызовы всё
                # равно требуют doods-auth-key.
                remote_host = request.client.host if request.client else None
                if not is_authenticated_ingress_request(remote_host, request.headers):
                    supplied_key = request.headers.get("doods-auth-key")
                    has_api_key = api_key_matches(self.auth_key, supplied_key)
                    if not has_api_key:
                        response = JSONResponse(
                            status_code=status.HTTP_401_UNAUTHORIZED,
                            content={"detail": "API authentication required."},
                        )
                        response.headers["Cache-Control"] = "no-store"
                        response.headers["X-Content-Type-Options"] = "nosniff"
                        return response
            response = await call_next(request)
            if (
                path == "/faces"
                or path.startswith("/faces/")
                or path.endswith(".html")
                or path in {"/", "/index.html"}
            ):
                # HTML-страницы не кэшируем: после обновления App браузер и
                # Ingress иначе продолжают отдавать старую копию faces.html.
                response.headers["Cache-Control"] = "no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["X-Content-Type-Options"] = "nosniff"
            return response

        async def authorize_websocket(websocket: WebSocket) -> bool:
            if not self.auth_key:
                return True
            supplied_key = websocket.headers.get("doods-auth-key")
            if api_key_matches(self.auth_key, supplied_key):
                return True
            await websocket.close(code=1008, reason="Invalid API key")
            return False

        # Enable metrics
        if self.config.metrics:
            self.instrumentator = Instrumentator(
                should_ignore_untemplated=True,
                should_instrument_requests_inprogress=True,
                excluded_handlers=["/metrics"],
                inprogress_name="inprogress",
                inprogress_labels=True,
            )
            self.instrumentator.instrument(self.api).expose(self.api)

        if self.config.trace:
            tracemalloc.start()

        @self.api.get("/capabilities")
        async def capabilities():
            """Сообщить версию протокола и включённые функции приложения."""
            return capabilities_payload(
                profiles_enabled=profiles_enabled(),
                preview_enabled=preview_enabled(),
                face_gallery_enabled=face_gallery_enabled(),
                localized_annotations_enabled=True,
                motion_stream_enabled=bool(self.auth_key),
                face_pipeline_diagnostics_enabled=bool(
                    self.auth_key and self.face_analyzer is not None
                ),
                face_debug_enabled=face_debug_enabled(),
            )

        @self.api.get("/health")
        async def health():
            """Вернуть минимальный публичный ответ о доступности процесса."""
            return {"status": "ok"}

        @self.api.get("/profiles")
        async def list_profiles():
            """Получить упорядоченные по ID профили для редактора."""
            require_profiles_enabled()
            return self.profile_store.list_profiles()

        @self.api.get("/profiles/{profile_id}")
        async def get_profile(profile_id: str):
            """Получить один профиль зон."""
            require_profiles_enabled()
            profile = self.profile_store.get(profile_id)
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            return profile.as_dict()

        @self.api.get("/profiles/{profile_id}/preview-source")
        async def get_profile_preview_source(profile_id: str, response: Response):
            """Получить только непрозрачный источник preview, связанный с профилем."""
            response.headers["Cache-Control"] = "no-store"
            require_profiles_enabled()
            if self.profile_store.get(profile_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            if self.profile_source_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile preview-source storage is not configured.",
                )
            try:
                source_id = await asyncio.to_thread(
                    self.profile_source_store.get,
                    profile_id,
                )
            except ProfileSourceStoreError as err:
                self.logger.error("Profile preview-source read failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read profile preview source.",
                ) from err
            return {"source_id": source_id}

        @self.api.put("/profiles/{profile_id}/preview-source")
        async def put_profile_preview_source(
            profile_id: str,
            source_data: dict,
            response: Response,
        ):
            """Запомнить opaque source_id отдельно от JSON профиля и JPEG-кадров."""
            response.headers["Cache-Control"] = "no-store"
            require_profiles_enabled()
            if self.profile_store.get(profile_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            if self.profile_source_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile preview-source storage is not configured.",
                )
            if set(source_data) != {"source_id"}:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Expected only source_id.",
                )
            try:
                await asyncio.to_thread(
                    self.profile_source_store.put,
                    profile_id,
                    source_data.get("source_id"),
                )
            except ProfileSourceValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except ProfileSourceStoreError as err:
                self.logger.error("Profile preview-source write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save profile preview source.",
                ) from err
            return {"source_id": source_data["source_id"]}

        @self.api.delete(
            "/profiles/{profile_id}/preview-source",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_profile_preview_source(profile_id: str):
            """Убрать связь источника; сам профиль и временные кадры не меняются."""
            require_profiles_enabled()
            if self.profile_store.get(profile_id) is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            if self.profile_source_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile preview-source storage is not configured.",
                )
            try:
                await asyncio.to_thread(
                    self.profile_source_store.delete,
                    profile_id,
                )
            except ProfileSourceStoreError as err:
                self.logger.error("Profile preview-source delete failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not remove profile preview source.",
                ) from err
            return Response(
                status_code=status.HTTP_204_NO_CONTENT,
                headers={"Cache-Control": "no-store"},
            )

        @self.api.put("/profiles/{profile_id}")
        async def put_profile(profile_id: str, profile_data: dict):
            """Создать или полностью заменить профиль зон."""
            require_profiles_enabled()
            try:
                profile = await asyncio.to_thread(
                    self.profile_store.put,
                    profile_data,
                    expected_id=profile_id,
                )
            except ProfileValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except ProfileStoreError as err:
                self.logger.error("Profile write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save profile.",
                ) from err
            return profile.as_dict()

        @self.api.delete("/profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
        async def delete_profile(profile_id: str):
            """Удалить профиль; повторное удаление вернёт 404."""
            require_profiles_enabled()
            try:
                deleted = await asyncio.to_thread(
                    self.profile_store.delete,
                    profile_id,
                )
            except ProfileStoreError as err:
                self.logger.error("Profile delete failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not delete profile.",
                ) from err
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            if self.profile_source_store is not None:
                try:
                    await asyncio.to_thread(
                        self.profile_source_store.delete,
                        profile_id,
                    )
                except ProfileSourceStoreError as err:
                    self.logger.warning(
                        "Could not clean profile preview-source mapping: %s",
                        err,
                    )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @self.api.post("/faces/cameras")
        async def set_face_detection_cameras(request: Request):
            """Установить список камер, для которых выполнять face detection.

            Принимает JSON {"cameras": ["camera.entity_id", ...]}.
            Пустой список отключает face detection на всех камерах.
            """
            if self.face_analyzer is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face analysis is unavailable.",
                )
            payload = await read_limited_json(request, 8192)
            if not isinstance(payload, dict) or set(payload) != {"cameras"}:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail='Expected {"cameras": ["camera.entity_id", ...]}.',
                )
            cameras = payload.get("cameras")
            if not isinstance(cameras, list) or any(
                not isinstance(c, str) or not c.strip() for c in cameras
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="cameras must be a list of non-empty strings.",
                )
            # Убираем дубли и пустые строки, сохраняя порядок первого вхождения.
            seen: set[str] = set()
            unique_cameras: list[str] = []
            for camera in cameras:
                camera = camera.strip()
                if camera and camera not in seen:
                    seen.add(camera)
                    unique_cameras.append(camera)
            # Список переживает рестарт App: /data/face_settings.json, не память.
            if self.face_settings_store is not None:
                try:
                    unique_cameras = await asyncio.to_thread(
                        self.face_settings_store.save_cameras,
                        unique_cameras,
                    )
                except FaceSettingsError as err:
                    self.logger.error(
                        "Face detection cameras were not persisted: %s", err
                    )
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Could not persist face detection cameras.",
                    ) from err
            self.face_analyzer.face_detection_cameras = set(unique_cameras)
            self.logger.info(
                "Face detection cameras updated: %s",
                ", ".join(unique_cameras) if unique_cameras else "(none)",
            )
            return {"cameras": sorted(unique_cameras)}

        @self.api.get("/faces/cameras")
        async def get_face_detection_cameras():
            """Вернуть текущий список камер для face detection."""
            if self.face_analyzer is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face analysis is unavailable.",
                )
            return {
                "cameras": sorted(self.face_analyzer.face_detection_cameras or []),
                "filter_configured": self.face_analyzer.face_detection_cameras is not None,
            }

        @self.api.get("/faces/debug/status")
        async def face_debug_status():
            """Текущий статус модели и локального ограниченного хранилища."""
            require_face_debug_enabled()
            return {
                "model": self.face_analyzer.model_status(),
                "storage": self.face_debug_store.status(),
                "gallery_enabled": face_gallery_enabled(),
                "face_detection_cameras": sorted(
                    self.face_analyzer.face_detection_cameras or []
                ),
                "face_detection_filter_configured": (
                    self.face_analyzer.face_detection_cameras is not None
                ),
            }

        @self.api.get("/faces/debug/log")
        async def list_face_debug_log(limit: int = 200):
            """Последние записи анализа, включая no_face; без кадров и embeddings."""
            require_face_debug_enabled()
            try:
                return {
                    "entries": await asyncio.to_thread(
                        self.face_debug_store.list_logs,
                        limit,
                    ),
                    "storage": await asyncio.to_thread(self.face_debug_store.status),
                }
            except FaceDebugValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read face-debug log.",
                ) from err

        @self.api.get("/faces/debug/captures")
        async def list_face_debug_captures(limit: int = 30):
            """Последние сохранённые кадры-лица из FIFO-кольца."""
            require_face_debug_enabled()
            try:
                return {
                    "captures": await asyncio.to_thread(
                        self.face_debug_store.list_captures,
                        limit,
                    ),
                    "storage": await asyncio.to_thread(self.face_debug_store.status),
                }
            except FaceDebugValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read face-debug captures.",
                ) from err

        @self.api.get("/faces/debug/captures/{capture_id}/image")
        async def get_face_debug_image(
            capture_id: str,
            kind: str = "frame",
            face_index: int | None = None,
        ):
            """Отдать retained JPEG только после проверки face API key."""
            require_face_debug_enabled()
            try:
                image = await asyncio.to_thread(
                    self.face_debug_store.get_capture_image,
                    capture_id,
                    kind=kind,
                    face_index=face_index,
                )
            except FaceDebugNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Capture image not found.",
                ) from err
            except FaceDebugValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read capture image.",
                ) from err
            return Response(
                content=image,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @self.api.post("/faces/debug/captures/{capture_id}/faces/{face_index}/enroll")
        async def enroll_face_from_capture(
            capture_id: str,
            face_index: int,
            request: Request,
        ):
            """Сохранить embedding из выбранного кадра только после ручного подтверждения."""
            require_face_debug_enabled()
            require_face_gallery_enabled()
            payload = await read_limited_json(request, _MAX_FACE_JSON_REQUEST_BYTES)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"person_id", "manual_confirmation"}
                or payload.get("manual_confirmation") is not True
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Select a person and explicitly confirm sample enrollment.",
                )
            try:
                capture = await asyncio.to_thread(
                    self.face_debug_store.get_capture,
                    capture_id,
                )
                if capture is None:
                    raise FaceDebugNotFoundError("Capture not found")
                faces = capture.get("faces", [])
                if not 0 <= face_index < len(faces):
                    raise FaceDebugNotFoundError("Face crop not found")
                jpeg_bytes = await asyncio.to_thread(
                    self.face_debug_store.get_capture_frame,
                    capture_id,
                )
                embedding = await asyncio.to_thread(
                    self.face_analyzer.embedding_from_capture,
                    jpeg_bytes,
                    expected_bbox=faces[face_index]["bbox"],
                )
            except FaceDebugNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Capture or face crop is no longer available.",
                ) from err
            except FaceDebugValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceEmbeddingError as err:
                response_status = (
                    status.HTTP_409_CONFLICT
                    if err.code == "models_not_ready"
                    else status.HTTP_422_UNPROCESSABLE_ENTITY
                )
                raise HTTPException(
                    status_code=response_status,
                    detail=f"Enrollment could not be prepared ({err.code}).",
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read retained face frame.",
                ) from err

            try:
                return await asyncio.to_thread(
                    self.face_gallery_store.add_sample,
                    payload.get("person_id"),
                    model_id="opencv_sface",
                    model_version="2021dec",
                    embedding=embedding,
                    manual_confirmation=True,
                    capture_id=capture_id,
                    face_index=face_index,
                )
            except FaceGalleryNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                ) from err
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save confirmed face sample.",
                ) from err

        @self.api.get("/faces/people")
        async def list_face_people():
            """Список людей без embedding-векторов."""
            require_face_gallery_enabled()
            return await asyncio.to_thread(self.face_gallery_store.list_people)

        @self.api.post("/faces/people", status_code=status.HTTP_201_CREATED)
        async def create_face_person(request: Request):
            """Создать запись человека; sample лица добавляются отдельным действием."""
            require_face_gallery_enabled()
            payload = await read_limited_json(request, _MAX_FACE_JSON_REQUEST_BYTES)
            if not isinstance(payload, dict) or set(payload) != {"name"}:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Expected only the name field.",
                )
            try:
                return await asyncio.to_thread(
                    self.face_gallery_store.create_person,
                    payload.get("name"),
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save face gallery.",
                ) from err

        @self.api.get("/faces/people/{person_id}")
        async def get_face_person(person_id: str):
            """Получить человека и метаданные образцов, не сами embeddings."""
            require_face_gallery_enabled()
            try:
                person = await asyncio.to_thread(
                    self.face_gallery_store.get_person,
                    person_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if person is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                )
            return person

        @self.api.patch("/faces/people/{person_id}")
        async def rename_face_person(person_id: str, request: Request):
            """Изменить отображаемое имя, сохранив стабильный ID и образцы."""
            require_face_gallery_enabled()
            payload = await read_limited_json(request, _MAX_FACE_JSON_REQUEST_BYTES)
            if not isinstance(payload, dict) or set(payload) != {"name"}:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Expected only the name field.",
                )
            try:
                person = await asyncio.to_thread(
                    self.face_gallery_store.rename_person,
                    person_id,
                    payload.get("name"),
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save face gallery.",
                ) from err
            if person is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                )
            return person

        @self.api.delete(
            "/faces/people/{person_id}",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_face_person(person_id: str):
            """Удалить человека и все связанные embeddings."""
            require_face_gallery_enabled()
            try:
                deleted = await asyncio.to_thread(
                    self.face_gallery_store.delete_person,
                    person_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery delete failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not delete face gallery person.",
                ) from err
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @self.api.get("/faces/debug/persons/{person_id}/face")
        async def get_person_face_image(person_id: str):
            """Вернуть сохранённое изображение лица персоны для визуального сравнения."""
            require_face_debug_enabled()
            require_face_gallery_enabled()
            try:
                # Эталон хранится в постоянном буфере person_faces и не вытесняется
                # вместе с кадрами; FIFO-буфер кадров используем только как fallback.
                jpeg_bytes = await asyncio.to_thread(
                    self.face_gallery_store.get_reference_face_image,
                    person_id,
                )
                if jpeg_bytes is None:
                    reference = await asyncio.to_thread(
                        self.face_gallery_store.get_person_face_reference,
                        person_id,
                    )
                    if reference is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="No face image reference found for this person.",
                        )
                    capture_id = reference["capture_id"]
                    face_index = reference["face_index"]
                    jpeg_bytes = await asyncio.to_thread(
                        self.face_debug_store.get_capture_image,
                        capture_id,
                        kind="face",
                        face_index=face_index,
                    )
            except FaceDebugNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face image no longer available in capture buffer.",
                ) from err
            except FaceDebugValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read person face image.",
                ) from err
            return Response(
                content=jpeg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @self.api.get("/faces/debug/persons/{person_id}/faces")
        async def list_person_faces(person_id: str):
            """Список эталонных кадров персоны (образцы galerie с привязкой к кадрам)."""
            require_face_debug_enabled()
            require_face_gallery_enabled()
            try:
                person = await asyncio.to_thread(
                    self.face_gallery_store.get_person,
                    person_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if person is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                )
            samples = []
            for sample in person.get("samples", []) or []:
                sample_id = sample.get("id")
                has_image = False
                if isinstance(sample_id, str):
                    try:
                        has_image = await asyncio.to_thread(
                            self.face_gallery_store.has_person_face_image,
                            person_id,
                            sample_id,
                        )
                    except Exception:
                        has_image = False
                samples.append({
                    "id": sample_id,
                    "capture_id": sample.get("capture_id"),
                    "face_index": sample.get("face_index"),
                    "confirmed_at": sample.get("confirmed_at"),
                    "model_id": sample.get("model_id"),
                    "model_version": sample.get("model_version"),
                    "has_image": has_image,
                })
            return samples

        @self.api.get("/faces/debug/persons/{person_id}/faces/{sample_id}/image")
        async def get_person_face_sample_image(person_id: str, sample_id: str):
            """Вернуть изображение конкретного эталонного лица персоны."""
            require_face_debug_enabled()
            require_face_gallery_enabled()
            try:
                jpeg_bytes = await asyncio.to_thread(
                    self.face_gallery_store.get_person_face_image,
                    person_id,
                    sample_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if jpeg_bytes is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face sample image not found.",
                )
            return Response(
                content=jpeg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @self.api.get("/faces/debug/matches")
        async def list_face_matches():
            """Список кадров-совпадений лиц (состояние matched)."""
            require_face_debug_enabled()
            try:
                matches = await asyncio.to_thread(
                    self.face_debug_store.list_matches,
                )
            except FaceDebugStoreError as err:
                self.logger.error("Face debug store read failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read match buffer.",
                ) from err
            return matches

        @self.api.get("/faces/debug/matches/{capture_id}/image")
        async def get_match_image(
            capture_id: str,
            kind: str = "frame",
            face_index: int | None = None,
        ):
            """Вернуть изображение кадра-совпадения или кроп найденного лица."""
            require_face_debug_enabled()
            try:
                jpeg_bytes = await asyncio.to_thread(
                    self.face_debug_store.get_match_image,
                    capture_id,
                    kind=kind,
                    face_index=face_index,
                )
            except FaceDebugNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Match image not found.",
                ) from err
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read match image.",
                ) from err
            return Response(
                content=jpeg_bytes,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
            )

        @self.api.get("/faces/debug/updates")
        async def face_debug_updates():
            """Флаги новых данных для автообновления фронтенда (без самих кадров)."""
            require_face_debug_enabled()
            return {
                "frames": await asyncio.to_thread(
                    self.face_debug_store.has_new_frames
                ),
                "matches": await asyncio.to_thread(
                    self.face_debug_store.has_new_matches
                ),
            }

        @self.api.post("/faces/debug/rematch")
        async def rematch_existing_captures():
            """Перепроверить существующие кадры в буфере на совпадение с галереей.

            Для каждого кадра с лицом повторно вычисляет SFace-embedding и
            сравнивает с эталонными образцами. Новые совпадения записываются
            в приватный буфер совпадений.
            """
            require_face_debug_enabled()
            if self.face_analyzer is None or self.face_debug_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face analyzer or debug store is unavailable.",
                )
            try:
                captures = await asyncio.to_thread(
                    self.face_debug_store.list_captures,
                    100,
                )
            except FaceDebugStoreError as err:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not read face-debug captures.",
                ) from err

            processed = 0
            new_matches = 0
            errors = 0

            for capture in captures:
                capture_id = capture.get("capture_id")
                source_id = capture.get("source_id", "rematch")
                if not capture_id:
                    continue
                processed += 1
                try:
                    jpeg_bytes = await asyncio.to_thread(
                        self.face_debug_store.get_capture_image,
                        capture_id,
                        kind="frame",
                    )
                except Exception:
                    errors += 1
                    continue
                try:
                    import numpy as np

                    encoded = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    import cv2

                    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                    if frame is None:
                        errors += 1
                        continue
                    match_result = await asyncio.to_thread(
                        self.face_analyzer.match_frame,
                        frame,
                        source_id,
                    )
                    if match_result and match_result.get("state") == "matched":
                        face_images: list[bytes] = []
                        for face in capture.get("faces", []) or []:
                            index = face.get("face_index")
                            if not isinstance(index, int):
                                continue
                            try:
                                face_images.append(
                                    await asyncio.to_thread(
                                        self.face_debug_store.get_capture_image,
                                        capture_id,
                                        kind="face",
                                        face_index=index,
                                    )
                                )
                            except Exception:
                                continue
                        await asyncio.to_thread(
                            self.face_debug_store._write_match_capture,
                            capture_id,
                            jpeg_bytes,
                            capture,
                            match_info=match_result,
                            face_images=face_images,
                        )
                        try:
                            updated = await asyncio.to_thread(
                                self.face_debug_store.update_capture_match,
                                capture_id,
                                match_result,
                            )
                            if not updated:
                                self.logger.warning(
                                    "rematch: capture %s matched but metadata not updated",
                                    capture_id,
                                )
                        except Exception as err:
                            # Не прерываем rematch из-за ошибки обновления метаданных кадра
                            self.logger.warning(
                                "rematch: capture %s metadata update failed: %s",
                                capture_id,
                                err,
                            )
                        new_matches += 1
                except Exception:
                    errors += 1
                    continue

            return {
                "processed": processed,
                "new_matches": new_matches,
                "errors": errors,
            }

        @self.api.post(
            "/faces/people/{person_id}/samples",
            status_code=status.HTTP_201_CREATED,
        )
        async def add_face_sample(person_id: str, request: Request):
            """Добавить embedding только с явным ручным подтверждением."""
            require_face_gallery_enabled()
            payload = await read_limited_json(request, _MAX_FACE_JSON_REQUEST_BYTES)
            expected_fields = {
                "manual_confirmation",
                "model_id",
                "model_version",
                "embedding",
            }
            if not isinstance(payload, dict) or set(payload) != expected_fields:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Expected manual_confirmation, model_id, model_version and embedding.",
                )
            try:
                return await asyncio.to_thread(
                    self.face_gallery_store.add_sample,
                    person_id,
                    model_id=payload.get("model_id"),
                    model_version=payload.get("model_version"),
                    embedding=payload.get("embedding"),
                    manual_confirmation=payload.get("manual_confirmation"),
                )
            except FaceGalleryNotFoundError as err:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person not found.",
                ) from err
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery write failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not save face sample.",
                ) from err

        @self.api.delete(
            "/faces/people/{person_id}/samples/{sample_id}",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_face_sample(person_id: str, sample_id: str):
            """Удалить один подтверждённый embedding."""
            return await _do_delete_face_sample(person_id, sample_id)

        @self.api.delete(
            "/faces/debug/persons/{person_id}/faces/{sample_id}",
            status_code=status.HTTP_204_NO_CONTENT,
        )
        async def delete_person_face(person_id: str, sample_id: str):
            """Удалить образец персоны (путь панели эталонов на странице «Кадры»)."""
            require_face_gallery_enabled()
            require_face_debug_enabled()
            return await _do_delete_face_sample(person_id, sample_id)

        async def _do_delete_face_sample(person_id: str, sample_id: str) -> Response:
            require_face_gallery_enabled()
            try:
                deleted = await asyncio.to_thread(
                    self.face_gallery_store.delete_sample,
                    person_id,
                    sample_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery delete failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not delete face sample.",
                ) from err
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Face gallery person or sample not found.",
                )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @self.api.get("/faces/backup")
        async def export_face_gallery_backup():
            """Скачать embeddings только по защищённому API и с no-store."""
            require_face_gallery_enabled()
            backup = await asyncio.to_thread(
                self.face_gallery_store.export_backup
            )
            return JSONResponse(
                content=backup,
                headers={
                    "Content-Disposition": 'attachment; filename="dooods2f-face-gallery.json"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @self.api.post("/faces/restore")
        async def restore_face_gallery_backup(request: Request):
            """Заменить галерею только по явной команде и валидной резервной копии."""
            require_face_gallery_enabled()
            payload = await read_limited_json(
                request,
                _MAX_FACE_BACKUP_REQUEST_BYTES,
            )
            if (
                not isinstance(payload, dict)
                or set(payload) != {"confirm_replace", "backup"}
                or payload.get("confirm_replace") is not True
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Restore requires confirm_replace=true and a backup object.",
                )
            try:
                return await asyncio.to_thread(
                    self.face_gallery_store.replace_from_backup,
                    payload.get("backup"),
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryStoreError as err:
                self.logger.error("Face gallery restore failed: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not restore face gallery.",
                ) from err

        def preview_metadata(frame):
            return {
                "source_id": frame.source_id,
                "source_name": frame.source_name,
                "expires_in": self.preview_store.remaining_seconds(frame),
            }

        @self.api.put("/preview/{source_id}")
        async def put_preview(
            source_id: str,
            request: Request,
            response: Response,
            source_name_b64: str = Header(default="", alias="X-Source-Name-B64"),
        ):
            """Принять временный JPEG от интеграции без записи на диск или в лог."""
            require_preview_enabled()
            content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type != "image/jpeg":
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="Preview frame must use image/jpeg.",
                )
            content_length = request.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                except ValueError as err:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Invalid Content-Length header.",
                    ) from err
                if declared_length > self.preview_store.max_frame_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Preview frame exceeds the configured size limit.",
                    )
            image_buffer = bytearray()
            async for chunk in request.stream():
                image_buffer.extend(chunk)
                if len(image_buffer) > self.preview_store.max_frame_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Preview frame exceeds the configured size limit.",
                    )
            image_bytes = bytes(image_buffer)
            try:
                source_name = base64.b64decode(
                    source_name_b64,
                    altchars=b"-_",
                    validate=True,
                ).decode("utf-8")
            except (ValueError, UnicodeDecodeError) as err:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="X-Source-Name-B64 must be base64url-encoded UTF-8.",
                ) from err
            try:
                frame = self.preview_store.put(source_id, source_name, image_bytes)
            except PreviewTooLargeError as err:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=str(err),
                ) from err
            except PreviewValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            response.headers["Cache-Control"] = "no-store"
            return preview_metadata(frame)

        @self.api.get("/preview")
        async def list_previews(response: Response):
            """Перечислить активные временные кадры без публикации HA entity ID."""
            response.headers["Cache-Control"] = "no-store"
            require_preview_enabled()
            return [preview_metadata(frame) for frame in self.preview_store.list_frames()]

        @self.api.get("/preview/{source_id}/metadata")
        async def get_preview_metadata(source_id: str, response: Response):
            """Вернуть идентификатор и подпись временного кадра без самого JPEG."""
            response.headers["Cache-Control"] = "no-store"
            require_preview_enabled()
            try:
                frame = self.preview_store.get(source_id)
            except PreviewValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if frame is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Preview frame not found or expired.",
                )
            return preview_metadata(frame)

        @self.api.get("/preview/{source_id}")
        async def get_preview(source_id: str):
            """Получить временный JPEG с заголовком no-store."""
            require_preview_enabled()
            try:
                frame = self.preview_store.get(source_id)
            except PreviewValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if frame is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Preview frame not found or expired.",
                )
            return Response(
                content=frame.image,
                media_type="image/jpeg",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @self.api.delete("/preview/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
        async def delete_preview(source_id: str):
            """Удалить временный кадр после завершения редактирования."""
            require_preview_enabled()
            try:
                deleted = self.preview_store.delete(source_id)
            except PreviewValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            if not deleted:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Preview frame not found or expired.",
                )
            return Response(status_code=status.HTTP_204_NO_CONTENT)

        @self.api.get("/detectors", response_model=odrpc.DetectorsResponse, response_model_exclude_none=True)
        async def detectors():
            return odrpc.DetectorsResponse(detectors=self.doods.detectors())

        @self.api.post("/detect", response_model=odrpc.DetectResponse, response_model_exclude_none=True)
        async def detect(detect_request: odrpc.DetectRequest, response: Response):
            # logger.info('detect request: %s', detect_request)
            if detect_request.profile_id and not profiles_enabled():
                response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
                return odrpc.DetectResponse(
                    id=detect_request.id,
                    error="Profile features require configured API authentication.",
                )
            detect_response = self.doods.detect(detect_request)
            if detect_response.error:
                response.status_code = status.HTTP_400_BAD_REQUEST
            # If we requested an image, base64 encode it back to the user.
            if detect_request.image and detect_response.image is not None:
                detect_response.image = base64.b64encode(detect_response.image)
            return detect_response
        
        @self.api.websocket("/detect")
        async def detect_stream(websocket: WebSocket):
            if not await authorize_websocket(websocket):
                return
            await websocket.accept()
            detect_responses = asyncio.Queue()
            executor = ThreadPoolExecutor()
            async def detect_handle(detect_request: odrpc.DetectRequest):
                try:
                    detect_response = self.doods.detect(detect_request)
                    if detect_request.image and detect_response.image is not None:
                        detect_response.image = base64.b64encode(detect_response.image)
                    await detect_responses.put(detect_response)
                except asyncio.TimeoutError:
                    self.logger.error("Detector timeout error")
                except Exception as e:
                    self.logger.error("Exception({0}):{1!r}".format(type(e).__name__, e.args))

            def detect_thread(detect_request: odrpc.DetectRequest):
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(detect_handle(detect_request))
                    loop.close()
                except Exception as e:
                    self.logger.error("Exception({0}):{1!r}".format(type(e).__name__, e.args))
                    loop.close()

            async def send_detect_responses():
                try:
                    while True:
                        detect_response = await detect_responses.get()
                        await websocket.send_json(detect_response.asdict(include_none=False))
                except Exception as e:
                    self.logger.error("Exception({0}):{1!r}".format(type(e).__name__, e.args))

            send_detect_responses_task = asyncio.create_task(send_detect_responses())
            
            while True:
                try:
                    detect_config = await websocket.receive_json()
                    detect_request = odrpc.DetectRequest(**detect_config)
                    if detect_request.profile_id and not profiles_enabled():
                        await detect_responses.put(
                            odrpc.DetectResponse(
                                id=detect_request.id,
                                error="Profile features require configured API authentication.",
                            )
                        )
                        continue
                    executor.submit(detect_thread, detect_request)
                except TypeError:
                    await detect_responses.put(odrpc.DetectResponse(error='could not parse request body'))
                except WebSocketDisconnect:
                    send_detect_responses_task.cancel()
                    executor.shutdown()
                    break
                except Exception as e:
                    self.logger.error("Exception({0}):{1!r}".format(type(e).__name__, e.args))
                    send_detect_responses_task.cancel()
                    executor.shutdown()
                    break

        @self.api.post("/image")
        async def image(detect_request: odrpc.DetectRequest, response: Response):
            # logger.info('image request: %s', detect_request)
            if detect_request.profile_id and not profiles_enabled():
                return Response(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content="Profile features require configured API authentication.",
                )
            if not detect_request.image:
                detect_request.image = ".jpg"
            detect_response = self.doods.detect(detect_request)
            if detect_response.error:
                return Response(status_code=status.HTTP_400_BAD_REQUEST, content=detect_response.error)
            return Response(content=detect_response.image, media_type="image/jpeg")

        @self.api.get("/stream")
        async def stream(detect_request: str = '{}'):
            detect_request_dict = json.loads(detect_request)
            detect_request = odrpc.DetectRequest(**detect_request_dict)
            # logger.info('stream request: %s', detect_request)
            if detect_request.profile_id and not profiles_enabled():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile features require configured API authentication.",
                )
            detect_request.image = ".jpg" # Must be jpg
            return StreamingResponse(Streamer.mjpeg_streamer(Streamer(self.doods).start_stream(detect_request)), media_type="multipart/x-mixed-replace;boundary=frame")

        @self.api.websocket("/stream")
        async def websocket_stream(websocket: WebSocket):
            if not await authorize_websocket(websocket):
                return
            await websocket.accept()
            streamer = None
            try:
                # Fetch the initial request payload
                detect_config = await websocket.receive_json()
                detect_request = odrpc.DetectRequest(**detect_config)
                if detect_request.profile_id and not profiles_enabled():
                    error_response = odrpc.DetectResponse(
                        id=detect_request.id,
                        error="Profile features require configured API authentication.",
                    )
                    await websocket.send_json(error_response.asdict(include_none=False))
                    await websocket.close(code=1008)
                    return

                # Run the stream detector and return the results.
                streamer = Streamer(self.doods).start_stream(detect_request)
                for detect_response in streamer:
                    # If we requested an image, base64 encode it back to the user
                    if detect_request.image and detect_response.image is not None:
                        detect_response.image = base64.b64encode(detect_response.image).decode('utf-8')
                    await websocket.send_json(detect_response.asdict(include_none=False))            
                    # Fake poll to maintain updated connection state
                    try:
                        await asyncio.wait_for(websocket.receive_text(), 0.0001)   
                    except asyncio.TimeoutError:
                        pass

            except Exception as e:
                self.logger.info(e)
                try:
                    if streamer:
                        streamer.send(True) # Stop the streamer
                except StopIteration:
                    pass

        @self.api.websocket("/motion/stream")
        async def motion_stream(websocket: WebSocket):
            """Постоянно читать RTSP и передавать HA только кадры-кандидаты."""
            if not await authorize_websocket(websocket):
                return
            if not self.auth_key:
                await websocket.close(
                    code=1008,
                    reason="API authentication is required for RTSP motion streaming.",
                )
                return

            await websocket.accept()
            source_id = "unknown"
            stop_event = threading.Event()
            worker_thread = None
            receive_task = None
            try:
                raw_request = await websocket.receive_json()
                if not isinstance(raw_request, dict):
                    raise ValueError("invalid_request")
                request = odrpc.MotionStreamRequest(**raw_request)
                source_id = request.source_id.strip()
                if (
                    not source_id
                    or source_id != request.source_id
                    or len(source_id) > 128
                ):
                    raise ValueError("invalid_source_id")
                if (
                    not isinstance(request.stream_source, str)
                    or len(request.stream_source) > 2048
                ):
                    raise ValueError("invalid_stream_source")
                parsed_source = urlsplit(request.stream_source)
                if (
                    parsed_source.scheme.lower() not in {"rtsp", "rtsps"}
                    or not parsed_source.hostname
                ):
                    raise ValueError("unsupported_stream_source")
                if (
                    not math.isfinite(request.sample_interval)
                    or not 0.05 <= request.sample_interval <= 2.0
                    or not math.isfinite(request.candidate_interval)
                    or not 0.2 <= request.candidate_interval <= 15.0
                    or not 64 <= request.max_width <= 640
                    or not 1 <= request.pixel_threshold <= 100
                    or not math.isfinite(request.changed_fraction)
                    or not 0.0001 <= request.changed_fraction <= 0.25
                    or not 1 <= request.confirm_frames <= 10
                    or not 1 <= request.quiet_frames <= 50
                    or len(request.roi_polygons) > 128
                    or any(len(polygon) > 256 for polygon in request.roi_polygons)
                ):
                    raise ValueError("invalid_motion_settings")
                gate = MotionGate(
                    max_width=request.max_width,
                    pixel_threshold=request.pixel_threshold,
                    changed_fraction=request.changed_fraction,
                    confirm_frames=request.confirm_frames,
                    quiet_frames=request.quiet_frames,
                    roi_polygons=request.roi_polygons,
                )
            except Exception as err:
                # Do not include the payload or stream URL: it may contain credentials.
                self.logger.warning(
                    "Rejected RTSP motion-stream request (%s)",
                    type(err).__name__,
                )
                try:
                    await websocket.send_json(
                        {"type": "error", "error": "invalid_motion_stream_request"}
                    )
                    await websocket.close(code=1008)
                except Exception:
                    pass
                return

            loop = asyncio.get_running_loop()
            event_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=4)

            def publish(event: dict) -> None:
                """Forward worker events without blocking its RTSP-reader thread."""
                def enqueue() -> None:
                    if event_queue.full():
                        try:
                            event_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        event_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        pass

                try:
                    loop.call_soon_threadsafe(enqueue)
                except RuntimeError:
                    stop_event.set()

            worker = MotionStreamWorker(
                request,
                gate,
                stop_event,
                publish,
                face_analyzer=self.face_analyzer,
            )

            def run_worker() -> None:
                try:
                    worker.run()
                except Exception as err:
                    self.logger.warning(
                        "RTSP motion reader stopped unexpectedly (%s)",
                        type(err).__name__,
                    )
                    publish(
                        {
                            "type": "status",
                            "source_id": source_id,
                            "state": "error",
                        }
                    )

            worker_thread = threading.Thread(
                target=run_worker,
                name=f"doods-rtsp-motion-{source_id[:32]}",
                daemon=True,
            )
            worker_thread.start()
            receive_task = asyncio.create_task(websocket.receive())
            event_task = None

            try:
                while True:
                    event_task = asyncio.create_task(event_queue.get())
                    done, _pending = await asyncio.wait(
                        {event_task, receive_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receive_task in done:
                        try:
                            incoming = receive_task.result()
                        except WebSocketDisconnect:
                            break
                        if incoming.get("type") == "websocket.disconnect":
                            break
                        if (
                            incoming.get("type") == "websocket.receive"
                            and incoming.get("text") == "stop"
                        ):
                            break
                        receive_task = asyncio.create_task(websocket.receive())
                    if event_task in done:
                        event = event_task.result()
                        await websocket.send_json(event)
                    else:
                        event_task.cancel()
                        await asyncio.gather(event_task, return_exceptions=True)
            except (WebSocketDisconnect, RuntimeError):
                pass
            finally:
                stop_event.set()
                if event_task is not None:
                    if not event_task.done():
                        event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                if receive_task is not None and not receive_task.done():
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                if worker_thread is not None:
                    await asyncio.to_thread(worker_thread.join, 6.0)

        @self.api.get("/memory")
        async def memory():
            if not self.config.trace:
                return "Not Enabled"
            current_mem, peak_mem = tracemalloc.get_traced_memory()
            overhead = tracemalloc.get_tracemalloc_memory()
            ret = [ "traced memory: %d KiB  peak: %d KiB  overhead: %d KiB" % (
                int(current_mem // 1024), int(peak_mem // 1024), int(overhead // 1024)
            ) ]
            snapshot = tracemalloc.take_snapshot()
            stats = snapshot.statistics('lineno')            
            for trace in stats[:20]:
                ret.append("%s" % (trace))

            data = {}
            data['traceback'] = ret
            return data

        # Mount the UI directory - must be last
        self.api.mount("/", StaticFiles(directory="html", html=True), name="static")
        
    def run(self):
        log_config = uvicorn.config.LOGGING_CONFIG
        log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        log_config["loggers"]["uvicorn"]["propagate"] = False # Fix a bug in logging
        uvicorn.run(self.api, host=self.config.host, port=self.config.port, log_config=log_config) 

