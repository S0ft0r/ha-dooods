"""Legacy camera streams with bounded, cooperative capture cleanup."""
import asyncio
import logging
import threading
import time

import cv2
import odrpc

from fresh_frame import FreshestFrame


# Limits remain held until blocked native operations actually return. A daemon
# capture can outlive its request, so capture and generator have separate slots.
_CAPTURE_SLOTS = threading.BoundedSemaphore(4)
_STREAM_SLOTS = threading.BoundedSemaphore(4)


class Streamer:
    def __init__(self, doods):
        self.doods = doods
        self.logger = logging.getLogger("doods.streamer")
        self._stop = threading.Event()
        self._capture_lock = threading.Lock()
        self._capture = None

    def stop(self):
        """Wake a waiting frame consumer; never release a capture during read."""
        self._stop.set()
        with self._capture_lock:
            capture = self._capture
        if capture is not None:
            capture.release(timeout=0)

    def start_stream(self, detect_request: odrpc.DetectRequest):
        if not _STREAM_SLOTS.acquire(blocking=False):
            self.logger.warning("Stream capacity exhausted")
            return
        url = detect_request.data
        last_frame_time = 0.0
        vcap = None
        try:
            while not self._stop.is_set():
                if vcap is None:
                    vcap = self.create_vcap(url)
                    with self._capture_lock:
                        self._capture = vcap
                    if self._stop.is_set():
                        break
                delay = detect_request.throttle - (time.monotonic() - last_frame_time)
                if delay > 0 and self._stop.wait(delay):
                    break
                last_frame_time = time.monotonic()
                _, detect_request.data = vcap.read()
                if self._stop.is_set():
                    break
                if detect_request.data is None:
                    self.logger.error("No stream frame; reconnecting")
                    with self._capture_lock:
                        self._capture = None
                    vcap.release()
                    vcap = None
                    # An immediate failure must not become a busy loop.
                    if self._stop.wait(1.0):
                        break
                    continue
                detect_response = self.doods.detect(detect_request)
                if self._stop.is_set() or (yield detect_response):
                    return
        finally:
            with self._capture_lock:
                self._capture = None
            try:
                if vcap is not None:
                    vcap.release()
            finally:
                _STREAM_SLOTS.release()

    def create_vcap(self, url):
        if not _CAPTURE_SLOTS.acquire(blocking=False):
            raise RuntimeError("Capture capacity exhausted")
        vcap = None
        try:
            vcap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            return FreshestFrame(vcap, on_release=_CAPTURE_SLOTS.release)
        except BaseException:
            try:
                if vcap is not None:
                    vcap.release()
            finally:
                _CAPTURE_SLOTS.release()
            raise

    async def mjpeg_streamer(self, detect_request):
        """Advance the blocking iterator off-loop and signal it on disconnect."""
        gen = self.start_stream(detect_request)
        pending = None
        sentinel = object()
        try:
            while True:
                pending = asyncio.create_task(asyncio.to_thread(next, gen, sentinel))
                result = await asyncio.shield(pending)
                pending = None
                if result is sentinel:
                    return
                image = bytes(result.image)
                headers = (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                           + str(len(image)).encode("ascii") + b'\r\n\r\n')
                yield headers + image + b'\r\n' 
        finally:
            self.stop()
            if pending is not None and not pending.done():
                def finish_read(task):
                    if not task.cancelled():
                        task.exception()
                    asyncio.create_task(asyncio.to_thread(gen.close))
                pending.add_done_callback(finish_read)
            else:
                if pending is not None and not pending.cancelled():
                    pending.exception()
                await asyncio.to_thread(gen.close)
