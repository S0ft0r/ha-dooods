"""Experimental, observation-only RTSP artifact scoring and log aggregation.

This deliberately does not decide whether a frame is usable. The threshold is
calibrated on a small set of annotated stills and must not be used as a filter.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import cv2
import numpy as np

THRESHOLD = 2.5
REPORT_INTERVAL = 30.0


def artifact_score(frame: np.ndarray) -> float:
    """Measure concentration of horizontal high-frequency image content.

    A row-window peak is divided by the median across the image. Ignore outer
    margins because the initial experimental samples had UI overlays there.
    This is not proof of a decoder error and can flag valid picture content.
    """
    if not isinstance(frame, np.ndarray) or frame.dtype != np.uint8 or frame.size == 0:
        raise ValueError("expected a non-empty uint8 image")
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("expected a BGR image")
    height, width = frame.shape[:2]
    if height < 32 or width < 32:
        raise ValueError("image is too small")
    image = frame[int(height * 0.065):int(height * 0.98), int(width * 0.01):int(width * 0.99)]
    target_height = max(8, round(image.shape[0] * 640 / image.shape[1]))
    if target_height > 4096:
        raise ValueError("image aspect ratio is unsupported")
    image = cv2.resize(image, (640, target_height), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.int16)
    edge_rows = (np.abs(np.diff(gray, axis=1)) > 25).mean(axis=1)
    windows = np.convolve(edge_rows, np.ones(8) / 8, mode="valid")
    return float(windows.max() / (np.median(windows) + 0.005))


class ArtifactDiagnostics:
    """Aggregate observations per stream; no image or URL is retained."""

    def __init__(self, source_id: str, logger: logging.Logger, now: float) -> None:
        # Never include arbitrary strings, URLs, credentials, or line breaks in logs.
        self.source_id = source_id if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", source_id) else "source-hidden"
        self.logger = logger
        self.started = now
        self.sampled = 0
        self.suspicious = 0
        self.max_score = 0.0
        self.first_at: str | None = None
        self.last_at: str | None = None

    def observe(self, frame: np.ndarray, now: float) -> None:
        """Check one sampled frame; never change it or the motion gate."""
        score = artifact_score(frame)
        self.sampled += 1
        if score >= THRESHOLD:
            timestamp = datetime.now(UTC).isoformat(timespec="milliseconds")
            self.suspicious += 1
            self.first_at = self.first_at or timestamp
            self.last_at = timestamp
            self.max_score = max(self.max_score, score)
        if now - self.started >= REPORT_INTERVAL:
            self.flush(now)

    def flush(self, now: float) -> None:
        """Log only intervals with suspicious observations, then reset counts."""
        if self.suspicious:
            self.logger.warning(
                "RTSP artifact diagnostic (observation only): camera=%s "
                "suspicious=%d sampled=%d max_score=%.3f threshold=%.2f "
                "first_utc=%s last_utc=%s; frames were not dropped",
                self.source_id, self.suspicious, self.sampled, self.max_score,
                THRESHOLD, self.first_at, self.last_at,
            )
        self.started = now
        self.sampled = 0
        self.suspicious = 0
        self.max_score = 0.0
        self.first_at = None
        self.last_at = None
