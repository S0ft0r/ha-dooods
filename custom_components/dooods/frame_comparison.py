"""Perceptual comparison of camera source frames for archive deduplication."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

_MAX_THUMBNAIL_SIZE = (96, 96)
_MAX_SOURCE_PIXELS = 100_000_000
_SIGNIFICANT_CHANNEL_DELTA = 20
_MAX_MEAN_CHANNEL_DELTA = 4.0
_MAX_SIGNIFICANT_PIXEL_RATIO = 0.01


def source_frame_signature(image_data: bytes | None) -> bytes | None:
    """Create a small RGB signature from a source JPEG/PNG without its metadata."""
    if not isinstance(image_data, bytes) or not image_data:
        return None

    try:
        with Image.open(BytesIO(image_data)) as image:
            width, height = image.size
            if width < 1 or height < 1 or width * height > _MAX_SOURCE_PIXELS:
                return None
            image.thumbnail(_MAX_THUMBNAIL_SIZE, Image.Resampling.BILINEAR)
            rgb_image = image.convert("RGB")
            thumbnail_width, thumbnail_height = rgb_image.size
            if thumbnail_width < 1 or thumbnail_height < 1:
                return None
            return bytes((thumbnail_width, thumbnail_height)) + rgb_image.tobytes()
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError):
        return None


def source_frames_are_similar(
    first_signature: bytes | None,
    second_signature: bytes | None,
) -> bool:
    """Treat small compression/noise changes as unchanged, but detect scene changes.

    The signatures are low-resolution RGB thumbnails. A pair is considered the
    same scene only when both the average color delta and the share of materially
    changed pixels remain below conservative thresholds.
    """
    if (
        not isinstance(first_signature, bytes)
        or not isinstance(second_signature, bytes)
        or len(first_signature) < 2
        or len(second_signature) < 2
        or first_signature[:2] != second_signature[:2]
    ):
        return False

    width, height = first_signature[0], first_signature[1]
    pixel_count = width * height
    expected_size = 2 + pixel_count * 3
    if (
        pixel_count < 1
        or len(first_signature) != expected_size
        or len(second_signature) != expected_size
    ):
        return False

    total_delta = 0
    significant_pixels = 0
    for offset in range(2, expected_size, 3):
        channel_delta = max(
            abs(first_signature[offset] - second_signature[offset]),
            abs(first_signature[offset + 1] - second_signature[offset + 1]),
            abs(first_signature[offset + 2] - second_signature[offset + 2]),
        )
        total_delta += channel_delta
        if channel_delta >= _SIGNIFICANT_CHANNEL_DELTA:
            significant_pixels += 1

    mean_delta = total_delta / pixel_count
    significant_ratio = significant_pixels / pixel_count
    return (
        mean_delta <= _MAX_MEAN_CHANNEL_DELTA
        and significant_ratio <= _MAX_SIGNIFICANT_PIXEL_RATIO
    )
