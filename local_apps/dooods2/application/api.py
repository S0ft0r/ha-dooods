import uvicorn
import odrpc
import json
import base64
import logging
import asyncio
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
from capabilities import capabilities_payload
from auth import api_key_matches, is_authenticated_ingress_request
from profiles import ProfileStoreError, ProfileValidationError
from preview import PreviewStore, PreviewTooLargeError, PreviewValidationError
from profile_sources import ProfileSourceStore, ProfileSourceStoreError, ProfileSourceValidationError
from concurrent.futures import ThreadPoolExecutor
from prometheus_fastapi_instrumentator import Instrumentator

import tracemalloc
from pathlib import Path


class API():
    def __init__(self, config, doods, preview_store=None, profile_source_store=None):
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
        self.auth_key = getattr(config, "auth_key", "") or ""
        self.logger = logging.getLogger("doods.api")
        if not self.auth_key:
            self.logger.warning(
                "API key is not configured; legacy API routes remain unauthenticated "
                "and profile features are disabled."
            )
        self._preview_cleanup_task = None

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

        def is_protected_api_path(path: str) -> bool:
            return path in {
                "/detectors",
                "/detect",
                "/image",
                "/stream",
                "/memory",
                "/metrics",
                "/docs",
                "/openapi.json",
                "/redoc",
                "/profiles",
                "/preview",
            } or path.startswith("/profiles/") or path.startswith("/preview/")

        @self.api.middleware("http")
        async def enforce_api_key(request: Request, call_next):
            if self.auth_key and is_protected_api_path(request.url.path):
                supplied_key = request.headers.get("doods-auth-key")
                client_host = request.client.host if request.client else None
                ingress_user_id = request.headers.get("x-remote-user-id")
                ingress_authenticated = is_authenticated_ingress_request(
                    client_host,
                    ingress_user_id,
                )
                if not api_key_matches(self.auth_key, supplied_key) and not ingress_authenticated:
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"detail": "API authentication required."},
                    )
            return await call_next(request)

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
                localized_annotations_enabled=True,
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

