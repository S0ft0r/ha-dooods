"""Lightweight frame-difference gate for continuous camera streams."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class MotionUpdate:
    """Motion state and normalized changed-pixel score for one sampled frame."""

    active: bool
    started: bool
    ended: bool
    changed_fraction: float


class MotionGate:
    """Detect sustained image changes without running the object detector per frame.

    Frames are blurred and downscaled before absolute-difference comparison. An
    optional union of normalized polygon ROIs can limit the comparison to the
    camera's configured detection zones.
    """

    def __init__(
        self,
        *,
        max_width: int = 320,
        pixel_threshold: int = 22,
        changed_fraction: float = 0.004,
        confirm_frames: int = 1,
        quiet_frames: int = 4,
        roi_polygons: Sequence[Sequence[Sequence[float]]] | None = None,
    ) -> None:
        if isinstance(max_width, bool) or not isinstance(max_width, int) or max_width < 64:
            raise ValueError("max_width must be an integer of at least 64")
        if (
            isinstance(pixel_threshold, bool)
            or not isinstance(pixel_threshold, int)
            or not 1 <= pixel_threshold <= 255
        ):
            raise ValueError("pixel_threshold must be an integer from 1 to 255")
        if (
            isinstance(changed_fraction, bool)
            or not isinstance(changed_fraction, (int, float))
            or not math.isfinite(changed_fraction)
            or not 0 < changed_fraction <= 1
        ):
            raise ValueError("changed_fraction must be greater than 0 and at most 1")
        if (
            isinstance(confirm_frames, bool)
            or not isinstance(confirm_frames, int)
            or confirm_frames < 1
        ):
            raise ValueError("confirm_frames must be a positive integer")
        if isinstance(quiet_frames, bool) or not isinstance(quiet_frames, int) or quiet_frames < 1:
            raise ValueError("quiet_frames must be a positive integer")

        self.max_width = max_width
        self.pixel_threshold = pixel_threshold
        self.changed_fraction = float(changed_fraction)
        self.confirm_frames = confirm_frames
        self.quiet_frames = quiet_frames
        self.roi_polygons = self._normalize_polygons(roi_polygons or ())
        self._previous_gray: np.ndarray | None = None
        self._roi_mask: np.ndarray | None = None
        self._candidate_streak = 0
        self._quiet_streak = 0
        self._active = False

    @staticmethod
    def _normalize_polygons(
        polygons: Sequence[Sequence[Sequence[float]]],
    ) -> tuple[tuple[tuple[float, float], ...], ...]:
        """Validate a sequence of normalized [x, y] polygons."""
        normalized: list[tuple[tuple[float, float], ...]] = []
        for polygon in polygons:
            if isinstance(polygon, (str, bytes)) or len(polygon) < 3:
                raise ValueError("each ROI polygon must contain at least three points")
            points: list[tuple[float, float]] = []
            for point in polygon:
                if isinstance(point, (str, bytes)) or len(point) != 2:
                    raise ValueError("ROI points must contain normalized x and y")
                x, y = point
                if (
                    isinstance(x, bool)
                    or isinstance(y, bool)
                    or not isinstance(x, (int, float))
                    or not isinstance(y, (int, float))
                    or not math.isfinite(x)
                    or not math.isfinite(y)
                    or not 0 <= x <= 1
                    or not 0 <= y <= 1
                ):
                    raise ValueError("ROI coordinates must be finite values from 0 to 1")
                points.append((float(x), float(y)))
            normalized.append(tuple(points))
        return tuple(normalized)

    def reset(self) -> None:
        """Forget the previous frame and re-arm the gate after a stream reconnect."""
        self._previous_gray = None
        self._roi_mask = None
        self._candidate_streak = 0
        self._quiet_streak = 0
        self._active = False

    def _prepare_frame(self, frame: np.ndarray) -> np.ndarray:
        """Convert a decoded frame to a small blurred grayscale image."""
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            raise ValueError("frame must be a non-empty numpy array")
        if frame.ndim == 2:
            gray = frame
        elif frame.ndim == 3 and frame.shape[2] == 3:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        elif frame.ndim == 3 and frame.shape[2] == 4:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
        else:
            raise ValueError("frame must be grayscale, BGR, or BGRA")

        height, width = gray.shape[:2]
        if width > self.max_width:
            target_height = max(1, round(height * self.max_width / width))
            gray = cv2.resize(
                gray,
                (self.max_width, target_height),
                interpolation=cv2.INTER_AREA,
            )
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        return gray

    def _mask_for_shape(self, height: int, width: int) -> np.ndarray | None:
        """Build a union mask for normalized ROI polygons at the sampled size."""
        if not self.roi_polygons:
            return None
        mask = np.zeros((height, width), dtype=np.uint8)
        for polygon in self.roi_polygons:
            points = np.asarray(
                [
                    [round(x * (width - 1)), round(y * (height - 1))]
                    for x, y in polygon
                ],
                dtype=np.int32,
            ).reshape((-1, 1, 2))
            cv2.fillPoly(mask, [points], 255)
        if cv2.countNonZero(mask) == 0:
            raise ValueError("ROI polygons do not cover any sampled pixels")
        return mask

    def update(self, frame: np.ndarray) -> MotionUpdate:
        """Compare a sampled frame and return state transitions plus change score."""
        current_gray = self._prepare_frame(frame)
        if self._previous_gray is None or self._previous_gray.shape != current_gray.shape:
            self._previous_gray = current_gray
            self._roi_mask = self._mask_for_shape(*current_gray.shape)
            self._candidate_streak = 0
            self._quiet_streak = 0
            self._active = False
            return MotionUpdate(False, False, False, 0.0)

        difference = cv2.absdiff(current_gray, self._previous_gray)
        _, changed = cv2.threshold(
            difference,
            self.pixel_threshold,
            255,
            cv2.THRESH_BINARY,
        )
        changed = cv2.morphologyEx(
            changed,
            cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)),
        )
        if self._roi_mask is not None:
            changed = cv2.bitwise_and(changed, self._roi_mask)
            roi_pixels = cv2.countNonZero(self._roi_mask)
        else:
            roi_pixels = changed.shape[0] * changed.shape[1]
        score = cv2.countNonZero(changed) / max(1, roi_pixels)
        candidate = score >= self.changed_fraction

        started = False
        ended = False
        if candidate:
            self._candidate_streak += 1
            self._quiet_streak = 0
            if not self._active and self._candidate_streak >= self.confirm_frames:
                self._active = True
                started = True
        else:
            self._candidate_streak = 0
            self._quiet_streak += 1
            if self._active and self._quiet_streak >= self.quiet_frames:
                self._active = False
                self._quiet_streak = 0
                ended = True

        self._previous_gray = current_gray
        return MotionUpdate(self._active, started, ended, score)
