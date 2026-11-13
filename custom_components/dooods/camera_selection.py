"""Resolve independent object/face camera selections, including legacy options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SELECTION_VERSION = 2
SELECTION_VERSION_KEY = "camera_selection_version"
SOURCE = "source"
OBJECTS = "object_detection_cameras"
FACES = "face_detection_cameras"


def _camera_list(value: Any) -> list[str]:
    """Return deduplicated camera identifiers without changing their order."""
    if not isinstance(value, (list, tuple)):
        return []
    return list(
        dict.fromkeys(
            item for item in value if isinstance(item, str) and item.startswith("camera.")
        )
    )


def resolve_camera_selection(
    options: Mapping[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Return the union, object cameras and face cameras without mutating options.

    Older configurations used `source` as a master list and an empty object
    selection to mean all sources. New configurations use two explicit lists.
    """
    if options.get(SELECTION_VERSION_KEY) == SELECTION_VERSION:
        objects = _camera_list(options.get(OBJECTS))
        faces = _camera_list(options.get(FACES))
    else:
        source = _camera_list(options.get(SOURCE))
        allowed = set(source)
        raw_objects = options.get(OBJECTS)
        if not isinstance(raw_objects, list) or not raw_objects:
            objects = source
        else:
            objects = [camera for camera in _camera_list(raw_objects) if camera in allowed]
        faces = [camera for camera in _camera_list(options.get(FACES)) if camera in allowed]
    union = list(dict.fromkeys(objects + faces))
    return union, objects, faces
