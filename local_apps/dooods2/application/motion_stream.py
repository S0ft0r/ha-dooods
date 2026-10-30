"""Background RTSP reader that emits JPEG candidates when frames change."""

from __future__ import annotations

import base64
import logging
import threading
import time
from datetime import UTC, datetime
from typing import Any, Callable

import cv2

from motion_gate import MotionGate
from odrpc import MotionStreamRequest

_LOGGER = logging.getLogger("doods.motion_stream")
_MAX_RECONNECT_DELAY = 30.0
_JPEG_QUALITY = 88


class MotionStreamWorker:
    """Read an RTSP stream continuously and publish only motion candidates.

    The worker deliberately does not call the object detector. Candidate JPEGs
    are sent to Home Assistant, which uses the existing authenticated /detect
    request path and existing per-camera profile/label filtering.
    """

    def __init__(
        self,
        request: MotionStreamRequest,
        gate: MotionGate,
        stop_event: threading.Event,
        publish: Callable[[dict[str, Any]], None],
    ) -> None:
        self.request = request
        self.gate = gate
        self.stop_event = stop_event
        self.publish = publish

    def run(self) -> None:
        """Connect, sample frames, and reconnect until the websocket closes."""
        reconnect_delay = 1.0
        last_candidate_at = 0.0
        last_status_at = 0.0
        connected = False

        while not self.stop_event.is_set():
            capture = self._open_capture()
            if capture is None:
                if connected:
                    self._emit_status("reconnecting")
                else:
                    self._emit_status("connecting")
                connected = False
                self.gate.reset()
                self.stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)
                continue

            if not connected:
                self._emit_status("connected")
            connected = True
            reconnect_delay = 1.0
            last_sample_at = 0.0
            self.gate.reset()

            try:
                while not self.stop_event.is_set():
                    ok, frame = capture.read()
                    if not ok or frame is None:
                        break

                    now = time.monotonic()
                    if now - last_sample_at < self.request.sample_interval:
                        continue
                    last_sample_at = now

                    update = self.gate.update(frame)
                    if update.started:
                        self.publish(
                            {
                                "type": "motion",
                                "source_id": self.request.source_id,
                                "state": "active",
                                "score": round(update.changed_fraction, 6),
                            }
                        )
                    elif update.ended:
                        self.publish(
                            {
                                "type": "motion",
                                "source_id": self.request.source_id,
                                "state": "idle",
                                "score": round(update.changed_fraction, 6),
                            }
                        )

                    if update.active and (
                        update.started
                        or now - last_candidate_at >= self.request.candidate_interval
                    ):
                        self._publish_candidate(frame, update.changed_fraction)
                        last_candidate_at = now

                    if now - last_status_at >= 5.0:
                        self._emit_status("connected")
                        last_status_at = now
            except Exception as err:
                # A decode failure should reconnect, not kill HA's stream task.
                _LOGGER.debug(
                    "RTSP motion reader failed for source %s (%s)",
                    self.request.source_id,
                    type(err).__name__,
                )
            finally:
                capture.release()

            if not self.stop_event.is_set():
                self._emit_status("reconnecting")
                connected = False
                self.gate.reset()
                self.stop_event.wait(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, _MAX_RECONNECT_DELAY)

        self._emit_status("stopped")

    def _open_capture(self) -> cv2.VideoCapture | None:
        """Open with bounded FFmpeg connection/read timeouts where supported."""
        timeout_params: list[int] = []
        open_timeout = getattr(cv2, "CAP_PROP_OPEN_TIMEOUT_MSEC", None)
        read_timeout = getattr(cv2, "CAP_PROP_READ_TIMEOUT_MSEC", None)
        if open_timeout is not None:
            timeout_params.extend((int(open_timeout), 5000))
        if read_timeout is not None:
            timeout_params.extend((int(read_timeout), 5000))

        capture = cv2.VideoCapture()
        opened = False
        try:
            if timeout_params:
                try:
                    opened = capture.open(
                        self.request.stream_source,
                        cv2.CAP_FFMPEG,
                        timeout_params,
                    )
                except (TypeError, cv2.error):
                    capture.release()
                    capture = cv2.VideoCapture()
            if not opened:
                opened = capture.open(self.request.stream_source, cv2.CAP_FFMPEG)
        except (TypeError, cv2.error):
            capture.release()
            return None

        if not opened or not capture.isOpened():
            capture.release()
            return None

        buffer_size = getattr(cv2, "CAP_PROP_BUFFERSIZE", None)
        if buffer_size is not None:
            capture.set(buffer_size, 1)
        return capture

    def _publish_candidate(self, frame, score: float) -> None:
        """Encode the original-resolution frame; never log or persist its source URL."""
        encoded_ok, encoded = cv2.imencode(
            ".jpg",
            frame,
            [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
        )
        if not encoded_ok:
            self._emit_status("frame_encode_failed")
            return
        self.publish(
            {
                "type": "candidate",
                "source_id": self.request.source_id,
                "captured_at": datetime.now(UTC).isoformat(),
                "motion_score": round(score, 6),
                "image": base64.b64encode(encoded.tobytes()).decode("ascii"),
            }
        )

    def _emit_status(self, state: str) -> None:
        """Publish a status that contains no RTSP URL or camera credentials."""
        self.publish(
            {
                "type": "status",
                "source_id": self.request.source_id,
                "state": state,
            }
        )
