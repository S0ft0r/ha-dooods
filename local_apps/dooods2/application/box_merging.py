"""Объединение перекрывающихся рамок только для отображения JPEG."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
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


def pad_display_box(box: DisplayBox, padding_percent: object) -> DisplayBox:
    """Расширить рамку только для изображения, не выходя за границы кадра.

    Процент применяется к ширине и высоте исходной рамки отдельно с каждой
    стороны. Невалидное значение трактуется как нулевой отступ; диапазон
    ограничен 0–100 процентов.
    """
    try:
        percent = float(padding_percent)
    except (TypeError, ValueError, OverflowError):
        percent = 0.0
    if not math.isfinite(percent):
        percent = 0.0
    ratio = min(100.0, max(0.0, percent)) / 100.0

    width = max(0.0, box.right - box.left)
    height = max(0.0, box.bottom - box.top)
    left = max(0.0, box.left - width * ratio)
    top = max(0.0, box.top - height * ratio)
    right = min(1.0, box.right + width * ratio)
    bottom = min(1.0, box.bottom + height * ratio)
    return DisplayBox(
        label=box.label,
        confidence=box.confidence,
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        count=box.count,
    )


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
