import uvicorn
import odrpc
import json
import base64
import copy
from collections import deque
from functools import wraps
import logging
import asyncio
import math
import os
import secrets
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
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from streamer import Streamer
from motion_gate import MotionGate
from motion_stream import MotionStreamWorker
from capabilities import capabilities_payload
from cooperative_json import loads as cooperative_json_loads, dumps as cooperative_json_dumps
from auth import (
    api_key_matches,
    check_supervisor_version,
    csrf_token,
    csrf_token_matches,
    ingress_user_id,
    is_authenticated_ingress_request,
)
from profiles import ProfileStoreError, ProfileValidationError
from preview import PreviewStore, PreviewTooLargeError, PreviewValidationError
from profile_sources import ProfileSourceStore, ProfileSourceStoreError, ProfileSourceValidationError
from face_gallery import (
    FaceGalleryCleanupError,
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
from prometheus_fastapi_instrumentator import Instrumentator

import tracemalloc
from pathlib import Path

_STREAM_FIRST_FRAME_TIMEOUT = 10.0


class _MotionEventRelay:
    """Bound both cross-thread pending events and loop-side queue to four."""

    def __init__(self, loop, queue, stop_event, max_pending=4):
        self._loop = loop
        self._queue = queue
        self._stop = stop_event
        self._pending = deque(maxlen=max_pending)
        self._lock = threading.Lock()
        self._scheduled = False
        self._closed = False

    def publish(self, event):
        with self._lock:
            if self._closed:
                return
            # As with the loop-side queue, keep the newest events under load.
            self._pending.append(event)
            if self._scheduled:
                return
            self._scheduled = True
            try:
                self._loop.call_soon_threadsafe(self._deliver)
            except RuntimeError:
                self._scheduled = False
                self._pending.clear()
                self._closed = True
                self._stop.set()

    def _deliver(self):
        with self._lock:
            if self._closed:
                self._pending.clear()
                self._scheduled = False
                return
            pending = list(self._pending)
            self._pending.clear()
            self._scheduled = False
        for event in pending:
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()


class _AccessQueryRedaction(logging.Filter):
    """Keep request paths but do not log potentially sensitive query strings."""

    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path = args[2].split("?", 1)[0]
            record.args = (args[0], args[1], path, args[3], args[4])
        return True


def _map_rematch_faces(raw_results, capture):
    """Map redetected boxes only when their overlap is unambiguous."""
    # YuNet can return faces in a different order on re-detection.
    # Map only an unambiguous spatial overlap; otherwise do not
    # associate a saved crop with the new result.
    faces = capture.get("faces", []) or []
    candidates = []
    for result_index, result in enumerate(raw_results):
        box = result.get("yunet_bbox") or {}
        for face in faces:
            old = face.get("bbox") or {}
            try:
                left = max(float(box["x"]), float(old["x"]))
                top = max(float(box["y"]), float(old["y"]))
                right = min(float(box["x"])+float(box["width"]), float(old["x"])+float(old["width"]))
                bottom = min(float(box["y"])+float(box["height"]), float(old["y"])+float(old["height"]))
                intersection = max(0.0, right-left)*max(0.0, bottom-top)
                union = float(box["width"])*float(box["height"]) + float(old["width"])*float(old["height"]) - intersection
                iou = intersection/union if union > 0 else 0
            except (KeyError, TypeError, ValueError):
                continue
            if iou >= 0.5:
                candidates.append((iou, result_index, face.get("face_index")))
    used_results, used_faces = set(), set()
    mapped = [dict(result, detected_face_index=result.get("face_index"), face_index=None) for result in raw_results]
    for _, result_index, face_index in sorted(candidates, key=lambda item: item[0], reverse=True):
        # Competing overlaps are unsafe: show the full frame instead.
        if (not isinstance(face_index, int)
            or result_index in used_results or face_index in used_faces
            or sum(item[1] == result_index for item in candidates) != 1
            or sum(item[2] == face_index for item in candidates) != 1):
            continue
        mapped[result_index]["face_index"] = face_index
        used_results.add(result_index)
        used_faces.add(face_index)
    return mapped


_FACE_DISABLED_PAGE = """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Определение лиц отключено — DOOODS2</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; }
    body { margin: 0; background: #111820; color: #eef4f8; }
    main { max-width: 680px; margin: 8vh auto; padding: 24px; }
    section { background: #1a242e; border: 1px solid #354554;
      border-radius: 12px; padding: 24px; }
    h1 { margin-top: 0; font-size: 1.5rem; }
    li { margin: 0.75rem 0; }
    p, li { line-height: 1.5; }
    .muted { color: #a7b6c4; }
  </style>
</head>
<body><main><section>
  <h1>Определение лиц отключено</h1>
  <p>Обработка лиц и галерея сейчас недоступны. Сохранённые данные не удалены.</p>
  <h2>Как включить</h2>
  <ol>
    <li>В Home Assistant откройте <strong>Настройки → Дополнения → DOOODS2 → Конфигурация</strong>.</li>
    <li>В разделе <strong>«Настройки распознавания лиц»</strong> включите <strong>«Определение лиц»</strong> и сохраните настройки.</li>
    <li>Перезапустите App DOOODS2, затем откройте веб-интерфейс снова.</li>
  </ol>
  <p class="muted">Пока функция отключена, API лиц, экспорт и удаление данных недоступны.</p>
</section></main></body></html>"""

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
        face_enabled=True,
        face_diagnostics_enabled=True,
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
        self.face_enabled = bool(face_enabled)
        self.face_diagnostics_enabled = bool(face_diagnostics_enabled and self.face_enabled)
        self.face_gallery_store = face_gallery_store if self.face_enabled else None
        self.face_analyzer = face_analyzer if self.face_enabled else None
        self.face_debug_store = (
            face_debug_store if self.face_diagnostics_enabled else None
        )
        self.face_settings_store = face_settings_store if self.face_enabled else None
        self.auth_key = getattr(config, "auth_key", "") or ""
        self.logger = logging.getLogger("doods.api")
        if not self.auth_key:
            self.logger.warning(
                "API key is not configured; legacy API routes remain unauthenticated "
                "and profile features are disabled."
            )
        self._preview_cleanup_task = None
        self._face_model_task = None
        self._csrf_secret = secrets.token_bytes(32)
        self._ingress_checks = asyncio.Semaphore(4)
        self._http_detection_slots = asyncio.Semaphore(2)
        self._detect_websocket_connections = asyncio.Semaphore(16)
        self._stream_websocket_connections = asyncio.Semaphore(8)
        self._motion_websocket_connections = asyncio.Semaphore(32)
        self._motion_worker_slots = threading.BoundedSemaphore(32)
        self._rematch_slot = asyncio.Semaphore(1)
        self._face_camera_settings_slot = asyncio.Semaphore(1)
        self._profile_operation_slot = asyncio.Semaphore(1)
        self._face_backup_operation_slot = asyncio.Semaphore(1)
        self._face_gallery_operation_slot = asyncio.Semaphore(1)
        self._face_debug_operation_slot = asyncio.Semaphore(1)
        self._face_inference_operation_slot = asyncio.Semaphore(1)
        self._memory_snapshot_slot = asyncio.Semaphore(1)
        self._rematch_task = None

        async def run_http_detection(detect_request):
            # Admission is bounded. Once started, cancellation does not release
            # the slot until the underlying synchronous work actually finishes.
            try:
                async with asyncio.timeout(5.0):
                    await self._http_detection_slots.acquire()
            except TimeoutError as err:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Detector busy; retry later.",
                ) from err
            try:
                task = asyncio.create_task(asyncio.to_thread(self.doods.detect, detect_request))
            except BaseException:
                self._http_detection_slots.release()
                raise
            def detection_finished(completed):
                self._http_detection_slots.release()
                # A disconnected client no longer awaits the task. Retrieve its
                # exception without logging potentially sensitive request data.
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(detection_finished)
            return await asyncio.shield(task)

        async def run_storage_operation(slot, label, function, *args, **kwargs):
            # Lock waiters remain outside the shared thread pool. A cancelled
            # request keeps its slot until its actual disk work has returned.
            try:
                async with asyncio.timeout(5.0):
                    await slot.acquire()
            except TimeoutError:
                raise HTTPException(status_code=503, detail=f"{label} busy; retry later.") from None
            try:
                task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
            except BaseException:
                slot.release()
                raise

            def finished(completed):
                slot.release()
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finished)
            return await asyncio.shield(task)

        async def run_gallery_operation(function, *args, **kwargs):
            return await run_storage_operation(
                self._face_gallery_operation_slot, "Face gallery", function, *args, **kwargs
            )

        async def run_debug_operation(function, *args, **kwargs):
            return await run_storage_operation(
                self._face_debug_operation_slot, "Face diagnostics", function, *args, **kwargs
            )

        async def run_face_inference(function, *args, **kwargs):
            return await run_storage_operation(
                self._face_inference_operation_slot, "Face inference", function, *args, **kwargs
            )

        def serialize_profile_operation(handler):
            # Keep the complete multi-store operation ordered even if the
            # requesting client disconnects while a disk worker is running.
            @wraps(handler)
            async def wrapped(*args, **kwargs):
                try:
                    async with asyncio.timeout(5.0):
                        await self._profile_operation_slot.acquire()
                except TimeoutError:
                    raise HTTPException(status_code=503, detail="Profile storage busy; retry later.") from None
                try:
                    task = asyncio.create_task(handler(*args, **kwargs))
                except BaseException:
                    self._profile_operation_slot.release()
                    raise

                def finished(completed):
                    self._profile_operation_slot.release()
                    if not completed.cancelled():
                        completed.exception()

                task.add_done_callback(finished)
                return await asyncio.shield(task)

            return wrapped

        def serialize_face_backup_operation(handler):
            """Bound backup parsing, serialization and restore I/O per process."""
            @wraps(handler)
            async def wrapped(*args, **kwargs):
                try:
                    async with asyncio.timeout(5.0):
                        await self._face_backup_operation_slot.acquire()
                except TimeoutError:
                    raise HTTPException(status_code=503, detail="Face backup is busy; retry later.") from None
                try:
                    task = asyncio.create_task(handler(*args, **kwargs))
                except BaseException:
                    self._face_backup_operation_slot.release()
                    raise

                def finished(completed):
                    self._face_backup_operation_slot.release()
                    if not completed.cancelled():
                        completed.exception()

                task.add_done_callback(finished)
                return await asyncio.shield(task)

            return wrapped

        async def supervisor_is_supported():
            """Check every Ingress request; an unavailable Supervisor fails closed."""
            token = os.environ.get("SUPERVISOR_TOKEN", "")
            if not token:
                return False
            try:
                async with asyncio.timeout(5.0):
                    await self._ingress_checks.acquire()
                    try:
                        task = asyncio.create_task(
                            asyncio.to_thread(check_supervisor_version, token)
                        )
                    except BaseException:
                        self._ingress_checks.release()
                        raise
                    # A timed-out thread keeps running. Hold the slot until it
                    # actually finishes, rather than allowing unbounded work.
                    def ingress_check_done(completed):
                        self._ingress_checks.release()
                        if not completed.cancelled():
                            completed.exception()

                    task.add_done_callback(ingress_check_done)
                    return await asyncio.shield(task)
            except Exception:
                # Unknown or malformed Supervisor responses never grant access.
                return False

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
                async def stop_face_store():
                    try:
                        await asyncio.to_thread(self.face_debug_store.stop, 10.0)
                    except Exception as err:
                        self.logger.warning(
                            "Face-debug writer shutdown failed (%s)",
                            type(err).__name__,
                        )

                rematch = self._rematch_task
                if rematch is not None and not rematch.done():
                    done, _ = await asyncio.wait({rematch}, timeout=1.0)
                    if not done:
                        self.logger.warning("Deferring face-debug shutdown until rematch finishes")
                        rematch.add_done_callback(lambda _: asyncio.create_task(stop_face_store()))
                        return
                await stop_face_store()

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
            return bool(self.face_enabled and self.auth_key and self.face_gallery_store is not None)

        def require_face_gallery_enabled():
            if self.face_gallery_store is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Face gallery storage is unavailable.",
                )

        def face_debug_enabled():
            return bool(
                self.face_enabled
                and self.face_debug_store is not None
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
                if len(chunk) > max_bytes - len(body):
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="JSON request exceeds the configured size limit.",
                    )
                body.extend(chunk)
            try:
                if max_bytes > _MAX_FACE_JSON_REQUEST_BYTES:
                    return await cooperative_json_loads(body)
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
            # Scope is decoded by ASGI. Reject traversal rather than letting
            # StaticFiles normalize it to a different, potentially private path.
            raw_path = request.scope.get("path", "")
            if any(part in {".", ".."} for part in raw_path.split("/")):
                return JSONResponse(status_code=400, content={"detail": "Invalid path."})
            path = "/" + "/".join(part for part in raw_path.split("/") if part)
            root_path = "/" + "/".join(
                part for part in request.scope.get("root_path", "").split("/") if part
            )
            if root_path != "/" and (path == root_path or path.startswith(root_path + "/")):
                path = path[len(root_path):] or "/"
            is_face_api = path == "/faces" or path.startswith("/faces/")
            is_face_page = path == "/faces.html"
            is_editor_api = (
                path in {"/detectors", "/detect", "/image", "/profiles", "/preview", "/ingress/csrf"}
                or (path == "/stream" and request.method == "POST")
                or path.startswith("/profiles/")
                or path.startswith("/preview/")
            )
            if not self.face_enabled and is_face_page and request.method in {"GET", "HEAD"}:
                return HTMLResponse(
                    content=_FACE_DISABLED_PAGE,
                    headers={
                        "Cache-Control": "no-store, must-revalidate",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            if not self.face_enabled and (is_face_api or is_face_page):
                return JSONResponse(status_code=404, content={"detail": "Not found."})

            key_ok = api_key_matches(self.auth_key, request.headers.get("doods-auth-key"))
            remote_host = request.client.host if request.client else None
            identity = ingress_user_id(remote_host, request.headers)
            ingress_ok = False
            if (is_face_api or is_face_page or is_editor_api) and not key_ok and self.auth_key and identity:
                supervisor_ok = await supervisor_is_supported()
                ingress_ok = is_authenticated_ingress_request(
                    remote_host, request.headers, supervisor_verified=supervisor_ok,
                )
            if is_face_api or is_face_page or is_editor_api:
                if not (key_ok or ingress_ok):
                    # Show an actionable but non-sensitive page to authenticated
                    # Ingress users while the Supervisor check is unavailable.
                    if is_face_page and identity and request.method in {"GET", "HEAD"}:
                        return HTMLResponse(
                            content="<!doctype html><html lang='ru'><meta charset='utf-8'><title>Лица недоступны</title>"
                            "<body><h1>Лица временно недоступны</h1><p>Проверьте API-ключ и Supervisor "
                            "версии 2026.10.2 или новее. После проверки обновите страницу.</p></body></html>",
                            status_code=503,
                            headers={"Cache-Control": "no-store"},
                        )
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "Authentication required."},
                        headers={"Cache-Control": "no-store"},
                    )
                if ingress_ok:
                    request.state.faces_ingress_identity = identity
                if (is_face_api or is_editor_api) and request.method not in {"GET", "HEAD", "OPTIONS"} and not key_ok:
                    if not identity or not csrf_token_matches(
                        self._csrf_secret, identity, request.headers.get("x-doods-csrf")
                    ):
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "CSRF validation failed."},
                            headers={"Cache-Control": "no-store"},
                        )
                if is_face_page and not self.face_diagnostics_enabled and request.method in {"GET", "HEAD"}:
                    page = await run_gallery_operation(self._face_summary_html)
                    return HTMLResponse(
                        content=page,
                        headers={
                            "Cache-Control": "no-store, must-revalidate",
                            "X-Content-Type-Options": "nosniff",
                        },
                    )
                if not self.face_diagnostics_enabled and (
                    path in {"/faces/debug", "/faces/people", "/faces/backup", "/faces/restore"}
                    or path.startswith("/faces/debug/") or path.startswith("/faces/people/")
                ):
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "Not found."},
                        headers={"Cache-Control": "no-store"},
                    )
            elif (self.auth_key and is_protected_api_path(path) and not key_ok) or (
                path == "/stream" and not key_ok
            ):
                # Other routes keep their existing key-only behaviour. Never
                # grant broad ingress access without equivalent CSRF protection.
                return JSONResponse(
                    status_code=401,
                    content={"detail": "API authentication required."},
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
                )
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
                await websocket.close(code=1008)
                return False
            supplied_key = websocket.headers.get("doods-auth-key")
            if api_key_matches(self.auth_key, supplied_key):
                return True
            await websocket.close(code=1008)
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
                # Kept for existing HA integrations: this flag also controls
                # face camera settings, even when gallery routes are blocked.
                face_gallery_enabled=face_gallery_enabled(),
                localized_annotations_enabled=True,
                motion_stream_enabled=bool(self.auth_key),
                face_pipeline_diagnostics_enabled=bool(
                    self.face_enabled and self.auth_key and self.face_analyzer is not None
                ),
                face_debug_enabled=face_debug_enabled(),
                face_summary_enabled=bool(self.face_enabled and not self.face_diagnostics_enabled),
            )

        @self.api.get("/health")
        async def health():
            """Вернуть минимальный публичный ответ о доступности процесса."""
            return {"status": "ok"}

        @self.api.get("/profiles")
        @serialize_profile_operation
        async def list_profiles():
            """Получить упорядоченные по ID профили для редактора."""
            require_profiles_enabled()
            return await asyncio.to_thread(self.profile_store.list_profiles)

        @self.api.get("/profiles/{profile_id}")
        @serialize_profile_operation
        async def get_profile(profile_id: str):
            """Получить один профиль зон."""
            require_profiles_enabled()
            profile = await asyncio.to_thread(self.profile_store.get, profile_id)
            if profile is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Profile not found.",
                )
            return profile.as_dict()

        @self.api.get("/profiles/{profile_id}/preview-source")
        @serialize_profile_operation
        async def get_profile_preview_source(profile_id: str, response: Response):
            """Получить только непрозрачный источник preview, связанный с профилем."""
            response.headers["Cache-Control"] = "no-store"
            require_profiles_enabled()
            if await asyncio.to_thread(self.profile_store.get, profile_id) is None:
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
        @serialize_profile_operation
        async def put_profile_preview_source(
            profile_id: str,
            source_data: dict,
            response: Response,
        ):
            """Запомнить opaque source_id отдельно от JSON профиля и JPEG-кадров."""
            response.headers["Cache-Control"] = "no-store"
            require_profiles_enabled()
            if await asyncio.to_thread(self.profile_store.get, profile_id) is None:
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
        @serialize_profile_operation
        async def delete_profile_preview_source(profile_id: str):
            """Убрать связь источника; сам профиль и временные кадры не меняются."""
            require_profiles_enabled()
            if await asyncio.to_thread(self.profile_store.get, profile_id) is None:
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
        @serialize_profile_operation
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
        @serialize_profile_operation
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

        @self.api.get("/ingress/csrf")
        @self.api.get("/faces/csrf")
        async def face_csrf(request: Request):
            """Issue an in-memory, per-Ingress-user token; never expose API keys."""
            identity = getattr(request.state, "faces_ingress_identity", None)
            if not identity:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ingress required.")
            return JSONResponse(
                content={"token": csrf_token(self._csrf_secret, identity)},
                headers={"Cache-Control": "no-store"},
            )

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
            # The persisted and live values are one ordered operation. The
            # slot remains held when a request is cancelled until disk I/O ends.
            try:
                async with asyncio.timeout(5.0):
                    await self._face_camera_settings_slot.acquire()
            except TimeoutError:
                raise HTTPException(status_code=503, detail="Face camera settings are busy.") from None

            def save_and_apply():
                selected = unique_cameras
                if self.face_settings_store is not None:
                    selected = self.face_settings_store.save_cameras(selected)
                self.face_analyzer.face_detection_cameras = set(selected)
                return selected

            try:
                task = asyncio.create_task(asyncio.to_thread(save_and_apply))
            except BaseException:
                self._face_camera_settings_slot.release()
                raise

            def settings_done(completed):
                self._face_camera_settings_slot.release()
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(settings_done)
            try:
                unique_cameras = await asyncio.shield(task)
            except FaceSettingsError as err:
                self.logger.error("Face detection cameras were not persisted: %s", err)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Could not persist face detection cameras.",
                ) from err
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
                "model": await asyncio.to_thread(self.face_analyzer.model_status),
                "storage": await run_debug_operation(self.face_debug_store.status),
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
                    "entries": await run_debug_operation(
                        self.face_debug_store.list_logs,
                        limit,
                    ),
                    "storage": await run_debug_operation(self.face_debug_store.status),
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
                    "captures": await run_debug_operation(
                        self.face_debug_store.list_captures,
                        limit,
                    ),
                    "storage": await run_debug_operation(self.face_debug_store.status),
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
                image = await run_debug_operation(
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
                capture = await run_debug_operation(
                    self.face_debug_store.get_capture,
                    capture_id,
                )
                if capture is None:
                    raise FaceDebugNotFoundError("Capture not found")
                faces = capture.get("faces", [])
                if not 0 <= face_index < len(faces):
                    raise FaceDebugNotFoundError("Face crop not found")
                jpeg_bytes = await run_debug_operation(
                    self.face_debug_store.get_capture_frame,
                    capture_id,
                )
                embedding = await run_face_inference(
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
                return await run_gallery_operation(
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
            return await run_gallery_operation(self.face_gallery_store.list_people)

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
                return await run_gallery_operation(
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
                person = await run_gallery_operation(
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
                person = await run_gallery_operation(
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
                deleted = await run_gallery_operation(
                    self.face_gallery_store.delete_person,
                    person_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryCleanupError as err:
                self.logger.error("Face gallery image cleanup failed; metadata remains unchanged")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Private face image cleanup failed; gallery metadata remains unchanged.",
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
                jpeg_bytes = await run_gallery_operation(
                    self.face_gallery_store.get_reference_face_image,
                    person_id,
                )
                if jpeg_bytes is None:
                    reference = await run_gallery_operation(
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
                    jpeg_bytes = await run_debug_operation(
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
                person = await run_gallery_operation(
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
                        has_image = await run_gallery_operation(
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
                jpeg_bytes = await run_gallery_operation(
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
                matches = await run_debug_operation(
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
                jpeg_bytes = await run_debug_operation(
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
                "frames": await run_debug_operation(
                    self.face_debug_store.has_new_frames
                ),
                "matches": await run_debug_operation(
                    self.face_debug_store.has_new_matches
                ),
            }

        async def rematch_existing_captures_worker():
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
                captures = await run_debug_operation(
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
                    jpeg_bytes = await run_debug_operation(
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

                    frame = await asyncio.to_thread(cv2.imdecode, encoded, cv2.IMREAD_COLOR)
                    if frame is None:
                        errors += 1
                        continue
                    raw_results = await run_face_inference(
                        self.face_analyzer.match_frame_all, frame, source_id,
                    )
                    faces = capture.get("faces", []) or []
                    mapped = _map_rematch_faces(raw_results, capture)
                    for result in mapped:
                        result["sface_input_origin"] = "rematch_unavailable"
                    match_result = self.face_analyzer._best_match(mapped)
                    updated = await run_debug_operation(
                        self.face_debug_store.update_capture_match,
                        capture_id, match_result, mapped,
                    )
                    if not updated:
                        continue
                    if any(result.get("state") == "matched" for result in mapped):
                        face_images = []
                        for face in faces:
                            index = face.get("face_index")
                            try:
                                image = await run_debug_operation(
                                    self.face_debug_store.get_capture_image,
                                    capture_id, kind="face", face_index=index,
                                )
                            except Exception:
                                image = b""
                            face_images.append(image)
                        capture["match"] = match_result
                        capture["matches"] = mapped
                        await run_debug_operation(
                            self.face_debug_store._write_match_capture,
                            capture_id, jpeg_bytes, capture, face_images=face_images,
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

        @self.api.post("/faces/debug/rematch")
        async def rematch_existing_captures():
            require_face_debug_enabled()
            if self._rematch_slot.locked():
                raise HTTPException(status_code=409, detail="Rematch is already running.")
            await self._rematch_slot.acquire()
            try:
                task = asyncio.create_task(rematch_existing_captures_worker())
                self._rematch_task = task
            except BaseException:
                self._rematch_slot.release()
                raise

            def finish_rematch(completed):
                self._rematch_slot.release()
                self._rematch_task = None
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finish_rematch)
            # A disconnected client does not abort a synchronous analysis in
            # progress. Keep the admission slot until the entire job finishes.
            return await asyncio.shield(task)

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
                return await run_gallery_operation(
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
                deleted = await run_gallery_operation(
                    self.face_gallery_store.delete_sample,
                    person_id,
                    sample_id,
                )
            except FaceGalleryValidationError as err:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(err),
                ) from err
            except FaceGalleryCleanupError as err:
                self.logger.error("Face gallery image cleanup failed; metadata remains unchanged")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Private face image cleanup failed; gallery metadata remains unchanged.",
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
        @serialize_face_backup_operation
        async def export_face_gallery_backup():
            """Скачать embeddings только по защищённому API и с no-store."""
            require_face_gallery_enabled()
            backup = await run_gallery_operation(
                self.face_gallery_store.export_backup
            )
            content = await cooperative_json_dumps(backup)
            return Response(
                content=content,
                media_type="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="dooods2f-face-gallery.json"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        @self.api.post("/faces/restore")
        @serialize_face_backup_operation
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
                return await run_gallery_operation(
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
            detect_response = await run_http_detection(detect_request)
            if detect_response.error:
                response.status_code = status.HTTP_400_BAD_REQUEST
            # If we requested an image, base64 encode it back to the user.
            if detect_request.image and detect_response.image is not None:
                detect_response.image = base64.b64encode(detect_response.image)
            return detect_response
        
        async def detect_stream_session(websocket: WebSocket):
            await websocket.accept()
            pending = set()
            send_lock = asyncio.Lock()

            async def send_response(result):
                async with send_lock:
                    await websocket.send_json(result.asdict(include_none=False))

            async def process_request(detect_request):
                try:
                    result = await run_http_detection(detect_request)
                    if detect_request.image and result.image is not None:
                        result.image = base64.b64encode(result.image).decode("ascii")
                    await send_response(result)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    # Do not log request payloads, images, or stream URLs.
                    self.logger.warning("WebSocket detection failed (%s)", type(err).__name__)
                    try:
                        await send_response(odrpc.DetectResponse(
                            id=detect_request.id, error="Detection failed or detector busy"
                        ))
                    except Exception:
                        pass

            try:
                while True:
                    # Keep receiving while busy so a disconnect is observable.
                    # Reject excess work rather than building an unbounded queue.
                    detect_config = await websocket.receive_json()
                    try:
                        detect_request = odrpc.DetectRequest(**detect_config)
                    except (TypeError, ValueError):
                        await send_response(odrpc.DetectResponse(error="could not parse request body"))
                        continue
                    if len(pending) >= 2:
                        await send_response(odrpc.DetectResponse(
                            id=detect_request.id, error="Detector busy; retry later."
                        ))
                        continue
                    if detect_request.profile_id and not profiles_enabled():
                        await send_response(odrpc.DetectResponse(
                            id=detect_request.id,
                            error="Profile features require configured API authentication.",
                        ))
                        continue
                    task = asyncio.create_task(process_request(detect_request))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
            except WebSocketDisconnect:
                pass
            except Exception as err:
                self.logger.warning("WebSocket detection stopped (%s)", type(err).__name__)
            finally:
                for task in tuple(pending):
                    task.cancel()
                if pending:
                    await asyncio.gather(*tuple(pending), return_exceptions=True)

        @self.api.websocket("/detect")
        async def detect_stream(websocket: WebSocket):
            if not await authorize_websocket(websocket):
                return
            # This runs on one event loop. An uncontended acquire cannot yield,
            # so the locked check and reservation are atomic between tasks.
            if self._detect_websocket_connections.locked():
                await websocket.close(code=1013)
                return
            await self._detect_websocket_connections.acquire()
            try:
                await detect_stream_session(websocket)
            finally:
                self._detect_websocket_connections.release()

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
            detect_response = await run_http_detection(detect_request)
            if detect_response.error:
                return Response(status_code=status.HTTP_400_BAD_REQUEST, content=detect_response.error)
            return Response(content=detect_response.image, media_type="image/jpeg")

        async def open_http_stream(detect_request: odrpc.DetectRequest, request: Request):
            """Wait for the first frame or disconnect before committing a status."""
            if detect_request.profile_id and not profiles_enabled():
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Profile features require configured API authentication.",
                )
            detect_request.image = ".jpg"
            handler = Streamer(self.doods)
            frames = handler.mjpeg_streamer(detect_request)
            async def wait_for_disconnect():
                while True:
                    message = await request.receive()
                    if message["type"] == "http.disconnect":
                        return

            frame_task = asyncio.create_task(frames.__anext__())
            disconnect_task = asyncio.create_task(wait_for_disconnect())
            first = None
            try:
                done, _ = await asyncio.wait(
                    {frame_task, disconnect_task},
                    timeout=_STREAM_FIRST_FRAME_TIMEOUT,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_task in done:
                    disconnect_task.result()
                    raise HTTPException(status_code=499, detail="Client disconnected.")
                if frame_task not in done:
                    raise HTTPException(status_code=504, detail="Stream did not produce a frame in time.")
                first = frame_task.result()
            except StopAsyncIteration:
                raise HTTPException(status_code=503, detail="Stream capacity unavailable or stream ended.") from None
            except HTTPException:
                raise
            except Exception:
                # Native exceptions can contain a source URL and credentials.
                self.logger.warning("Could not start camera stream")
                raise HTTPException(status_code=502, detail="Could not start camera stream.") from None
            finally:
                for task in (frame_task, disconnect_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(frame_task, disconnect_task, return_exceptions=True)
                if first is None:
                    await frames.aclose()

            async def body():
                try:
                    yield first
                    async for frame in frames:
                        yield frame
                finally:
                    await frames.aclose()

            return StreamingResponse(
                body(),
                media_type="multipart/x-mixed-replace;boundary=frame",
                headers={"Cache-Control": "no-store"},
            )

        @self.api.get("/stream")
        async def stream(request: Request, detect_request: str = '{}'):
            detect_request_dict = json.loads(detect_request)
            return await open_http_stream(odrpc.DetectRequest(**detect_request_dict), request)

        @self.api.post("/stream")
        async def stream_from_body(detect_request: odrpc.DetectRequest, request: Request):
            """Start the browser stream without exposing its source in the URL."""
            return await open_http_stream(detect_request, request)

        async def websocket_stream_session(websocket: WebSocket):
            await websocket.accept()
            streamer = None
            stream_handler = None
            stream_read = None
            receive_task = None
            try:
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

                stream_handler = Streamer(self.doods)
                streamer = stream_handler.start_stream(detect_request)
                sentinel = object()
                receive_task = asyncio.create_task(websocket.receive())
                while True:
                    if stream_read is None:
                        stream_read = asyncio.create_task(asyncio.to_thread(next, streamer, sentinel))
                    done, _ = await asyncio.wait(
                        {stream_read, receive_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive_task in done:
                        message = receive_task.result()
                        if message.get("type") == "websocket.disconnect":
                            break
                        receive_task = asyncio.create_task(websocket.receive())
                    if stream_read in done:
                        detect_response = stream_read.result()
                        stream_read = None
                        if detect_response is sentinel:
                            break
                        if detect_request.image and detect_response.image is not None:
                            detect_response.image = base64.b64encode(detect_response.image).decode("ascii")
                        await websocket.send_json(detect_response.asdict(include_none=False))
            except WebSocketDisconnect:
                pass
            except Exception as err:
                self.logger.warning("WebSocket stream stopped (%s)", type(err).__name__)
            finally:
                if stream_handler is not None:
                    stream_handler.stop()
                if receive_task is not None:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                if streamer is not None:
                    if stream_read is not None and not stream_read.done():
                        # Never close a generator while its worker is inside it.
                        def finish_read(task):
                            if not task.cancelled():
                                task.exception()
                            asyncio.create_task(asyncio.to_thread(streamer.close))
                        stream_read.add_done_callback(finish_read)
                    else:
                        if stream_read is not None and not stream_read.cancelled():
                            stream_read.exception()
                        await asyncio.to_thread(streamer.close)

        @self.api.websocket("/stream")
        async def websocket_stream(websocket: WebSocket):
            if not await authorize_websocket(websocket):
                return
            if self._stream_websocket_connections.locked():
                await websocket.close(code=1013)
                return
            await self._stream_websocket_connections.acquire()
            try:
                await websocket_stream_session(websocket)
            finally:
                self._stream_websocket_connections.release()

        async def motion_stream_session(websocket: WebSocket):
            """Постоянно читать RTSP и передавать HA только кадры-кандидаты."""
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

            relay = _MotionEventRelay(loop, event_queue, stop_event)
            publish = relay.publish

            if not self._motion_worker_slots.acquire(blocking=False):
                await websocket.send_json({"type": "error", "source_id": source_id, "error": "motion_stream_capacity"})
                await websocket.close(code=1013)
                return
            try:
                worker = MotionStreamWorker(
                    request,
                    gate,
                    stop_event,
                    publish,
                    face_analyzer=self.face_analyzer,
                    artifact_diagnostics=getattr(self.config, "rtsp_artifact_diagnostics", False),
                )
            except BaseException:
                self._motion_worker_slots.release()
                raise

            worker_finished = asyncio.Event()

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
                finally:
                    self._motion_worker_slots.release()
                    try:
                        loop.call_soon_threadsafe(worker_finished.set)
                    except RuntimeError:
                        # The event loop can already be closed at process shutdown.
                        pass

            worker_thread = threading.Thread(
                target=run_worker,
                name=f"doods-rtsp-motion-{source_id[:32]}",
                daemon=True,
            )
            try:
                worker_thread.start()
            except BaseException:
                self._motion_worker_slots.release()
                raise
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
                relay.close()
                if event_task is not None:
                    if not event_task.done():
                        event_task.cancel()
                    await asyncio.gather(event_task, return_exceptions=True)
                if receive_task is not None and not receive_task.done():
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                if worker_thread is not None:
                    try:
                        async with asyncio.timeout(6.0):
                            await worker_finished.wait()
                    except TimeoutError:
                        # The native worker keeps its own capacity slot until it exits.
                        pass

        @self.api.websocket("/motion/stream")
        async def motion_stream(websocket: WebSocket):
            if not await authorize_websocket(websocket):
                return
            if not self.auth_key:
                await websocket.close(code=1008)
                return
            if self._motion_websocket_connections.locked():
                await websocket.close(code=1013)
                return
            await self._motion_websocket_connections.acquire()
            try:
                await motion_stream_session(websocket)
            finally:
                self._motion_websocket_connections.release()

        @self.api.get("/memory")
        async def memory():
            if not self.config.trace:
                return "Not Enabled"

            def collect_snapshot():
                current_mem, peak_mem = tracemalloc.get_traced_memory()
                overhead = tracemalloc.get_tracemalloc_memory()
                lines = [
                    "traced memory: %d KiB  peak: %d KiB  overhead: %d KiB"
                    % (current_mem // 1024, peak_mem // 1024, overhead // 1024)
                ]
                snapshot = tracemalloc.take_snapshot()
                lines.extend(str(trace) for trace in snapshot.statistics("lineno")[:20])
                return {"traceback": lines}

            return await run_storage_operation(
                self._memory_snapshot_slot, "Memory diagnostics", collect_snapshot
            )

        # Mount the UI directory - must be last
        self.api.mount("/", StaticFiles(directory="html", html=True), name="static")
        
    def _face_summary_html(self) -> str:
        """Render aggregate-only face summary without images, names or secrets."""
        state = "Недоступно"
        if self.face_analyzer is not None:
            try:
                status_value = self.face_analyzer.model_status().get("state")
                state = {
                    "ready": "Модели готовы",
                    "loading": "Модели загружаются",
                    "error": "Ошибка загрузки моделей",
                }.get(status_value, "Неизвестно")
            except Exception:
                self.logger.warning("Face model status is unavailable")
        people_count = "Недоступно"
        sample_count = "Недоступно"
        if self.face_gallery_store is not None:
            try:
                people = self.face_gallery_store.list_people()
                people_count = str(len(people))
                sample_count = str(sum(int(person["sample_count"]) for person in people))
            except Exception:
                self.logger.warning("Face gallery summary is unavailable")
        return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Диагностика лиц отключена — DOOODS2</title>
<style>
:root {{ color-scheme: dark; font-family: system-ui, sans-serif; }}
body {{ margin: 0; background: #111820; color: #eef4f8; }}
main {{ max-width: 680px; margin: 8vh auto; padding: 24px; }}
section {{ background: #1a242e; border: 1px solid #354554; border-radius: 12px; padding: 24px; }}
h1 {{ margin-top: 0; font-size: 1.5rem; }}
li {{ margin: .75rem 0; }} p, li {{ line-height: 1.5; }}
</style></head><body><main><section>
<h1>Диагностика лиц отключена</h1>
<p>Распознавание лиц включено. Диагностические кадры, совпадения и журнал не записываются.</p>
<p><strong>Состояние моделей:</strong> {state}<br>
<strong>Зарегистрировано людей:</strong> {people_count}<br>
<strong>Эталонных образцов:</strong> {sample_count}</p>
<p>Сохранённые ранее данные не удалены. Управление эталонами и диагностические кадры доступны после включения диагностики.</p>
<h2>Как включить диагностику</h2><ol>
<li>Откройте в Home Assistant <strong>Настройки → Дополнения → DOOODS2 → Конфигурация</strong>.</li>
<li>В разделе <strong>«Настройки распознавания лиц»</strong> включите <strong>«Диагностика лиц»</strong> и сохраните настройки.</li>
<li>Перезапустите App DOOODS2 и снова откройте веб-интерфейс.</li>
</ol></section></main></body></html>"""

    def run(self):
        log_config = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
        log_config.setdefault("filters", {})["redact_query"] = {"()": _AccessQueryRedaction}
        access_handler = log_config.get("handlers", {}).get("access", {})
        access_handler["filters"] = [*access_handler.get("filters", []), "redact_query"]
        log_config["formatters"]["access"]["fmt"] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        log_config["formatters"]["default"]["fmt"] = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        log_config["loggers"]["uvicorn"]["propagate"] = False # Fix a bug in logging
        uvicorn.run(
            self.api,
            host=self.config.host,
            port=self.config.port,
            log_config=log_config,
            proxy_headers=False,
        )

