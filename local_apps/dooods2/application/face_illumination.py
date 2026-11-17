"""Optional, bounded shadow correction for SFace's aligned BGR face crop."""

from __future__ import annotations

from functools import lru_cache
import math

import cv2
import numpy as np

# These initial experimental values are not a substitute for camera-specific
# validation against known and unknown faces.
DEFAULT_DARK_MEDIAN_LIGHTNESS = 60
DEFAULT_GAMMA = 0.45


def validate_parameters(threshold: int, gamma: float) -> tuple[int, float]:
    """Reject unsupported or non-finite settings without silently coercing them."""
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not 0 <= threshold <= 255
    ):
        raise ValueError("Brightness threshold must be an integer from 0 to 255")
    if (
        isinstance(gamma, bool)
        or not isinstance(gamma, (int, float))
        or not math.isfinite(gamma)
        or not 0.3 <= gamma <= 0.9
    ):
        raise ValueError("Gamma must be a finite number from 0.3 to 0.9")
    return threshold, float(gamma)


@lru_cache(maxsize=16)
def _gamma_lut(gamma: float) -> np.ndarray:
    """Cache a bounded number of correction lookup tables."""
    return np.array(
        [round(255.0 * (value / 255.0) ** gamma) for value in range(256)],
        dtype=np.uint8,
    )


def compensate_face_illumination(
    image: np.ndarray,
    *,
    threshold: int = DEFAULT_DARK_MEDIAN_LIGHTNESS,
    gamma: float = DEFAULT_GAMMA,
    mode: str = "gamma",
) -> np.ndarray:
    """Brighten a dark uint8 BGR crop; leave source and bright crops untouched.

    The caller supplies the aligned SFace crop, not an arbitrary whole camera
    frame. No normalization relative to the frame maximum is performed.
    """
    if not isinstance(image, np.ndarray):
        raise TypeError("Expected an OpenCV image")
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
        raise ValueError("Expected a uint8 BGR face crop")
    if image.shape[0] < 16 or image.shape[1] < 16:
        raise ValueError("Face crop is too small")

    threshold, gamma = validate_parameters(threshold, gamma)
    if mode not in {"gamma", "clahe", "gamma_clahe"}:
        raise ValueError("Invalid illumination mode")
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a_channel, b_channel = cv2.split(lab)
    if float(np.median(lightness)) >= threshold:
        return image
    if mode == "gamma":
        smoothed = cv2.bilateralFilter(lightness, 7, 25, 25)
        corrected = cv2.LUT(smoothed, _gamma_lut(gamma))
    else:
        # Experimental local contrast: same bounded parameters as the preview.
        source = cv2.LUT(lightness, _gamma_lut(gamma)) if mode == "gamma_clahe" else lightness
        corrected = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(4, 4)).apply(source)
    return cv2.cvtColor(
        cv2.merge((corrected, a_channel, b_channel)), cv2.COLOR_LAB2BGR
    )
