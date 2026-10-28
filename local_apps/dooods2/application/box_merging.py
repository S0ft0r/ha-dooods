"""Объединение перекрывающихся рамок только для отображения JPEG."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DisplayBox:
    """Рамка для отрисовки; исходные детекции остаются отдельными."""

    label: str
    confidence: float
    left: float
    top: float
    right: float
    bottom: float
    count: int = 1


def build_display_boxes(
    detections: Sequence[Any],
    *,
    merge_overlapping: bool,
) -> list[DisplayBox]:
    """Сгруппировать транзитивно перекрывающиеся рамки одного класса."""
    boxes = [
        DisplayBox(
            label=detection.label,
            confidence=detection.confidence,
            left=detection.left,
            top=detection.top,
            right=detection.right,
            bottom=detection.bottom,
        )
        for detection in detections
    ]
    if not merge_overlapping or len(boxes) < 2:
        return boxes

    parents = list(range(len(boxes)))

    def root(index: int) -> int:
        """Найти корень компоненты и сжать путь дерева."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def unite(first: int, second: int) -> None:
        """Объединить компоненты двух пересекающихся рамок."""
        first_root = root(first)
        second_root = root(second)
        if first_root != second_root:
            parents[second_root] = first_root

    for first_index, first in enumerate(boxes):
        for second_index in range(first_index + 1, len(boxes)):
            second = boxes[second_index]
            if first.label != second.label:
                continue
            if (
                first.left < second.right
                and second.left < first.right
                and first.top < second.bottom
                and second.top < first.bottom
            ):
                unite(first_index, second_index)

    groups: dict[int, list[DisplayBox]] = {}
    for index, box in enumerate(boxes):
        groups.setdefault(root(index), []).append(box)

    merged: list[DisplayBox] = []
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(
            DisplayBox(
                label=group[0].label,
                confidence=max(box.confidence for box in group),
                left=min(box.left for box in group),
                top=min(box.top for box in group),
                right=max(box.right for box in group),
                bottom=max(box.bottom for box in group),
                count=len(group),
            )
        )
    return merged
