"""Проверка детекций по полигональным зонам профиля."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_EPSILON = 1e-9
Point = tuple[float, float]


class PolygonValidationError(ValueError):
    """Ошибка проверки координат или формы полигона."""


def _number(value: Any, name: str) -> float:
    """Проверить, что значение является конечным числом."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} должен быть числом")
    try:
        result = float(value)
    except (OverflowError, ValueError) as err:
        raise ValueError(f"{name} должен быть конечным числом") from err
    if not math.isfinite(result):
        raise ValueError(f"{name} должен быть конечным числом")
    return result


def _point(value: Any, index: int) -> Point:
    """Преобразовать одну точку из пары координат или записи left/top."""
    if isinstance(value, Mapping):
        left = value.get("left")
        top = value.get("top")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            raise PolygonValidationError(
                f"Точка {index} должна содержать две координаты"
            )
        left, top = value
    else:
        raise PolygonValidationError(
            f"Точка {index} должна быть парой координат или записью left/top"
        )

    try:
        x = _number(left, f"left точки {index}")
        y = _number(top, f"top точки {index}")
    except ValueError as err:
        raise PolygonValidationError(str(err)) from err

    if not 0.0 <= x <= 1.0 or not 0.0 <= y <= 1.0:
        raise PolygonValidationError(
            f"Координаты точки {index} должны быть в диапазоне от 0 до 1"
        )
    return x, y


def _cross(a: Point, b: Point) -> float:
    """Вернуть векторное произведение двух двумерных векторов."""
    return a[0] * b[1] - a[1] * b[0]


def _orientation(a: Point, b: Point, c: Point) -> int:
    """Определить взаимное положение точки и ориентированного отрезка."""
    value = _cross((b[0] - a[0], b[1] - a[1]), (c[0] - a[0], c[1] - a[1]))
    if abs(value) <= _EPSILON:
        return 0
    return 1 if value > 0 else -1


def _point_on_segment(point: Point, start: Point, end: Point) -> bool:
    """Проверить, лежит ли точка на отрезке, включая его концы."""
    if _orientation(start, end, point) != 0:
        return False
    return (
        min(start[0], end[0]) - _EPSILON
        <= point[0]
        <= max(start[0], end[0]) + _EPSILON
        and min(start[1], end[1]) - _EPSILON
        <= point[1]
        <= max(start[1], end[1]) + _EPSILON
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Проверить пересечение двух отрезков, включая касание границ."""
    first = _orientation(a, b, c)
    second = _orientation(a, b, d)
    third = _orientation(c, d, a)
    fourth = _orientation(c, d, b)

    if first != second and third != fourth:
        return True
    return (
        (first == 0 and _point_on_segment(c, a, b))
        or (second == 0 and _point_on_segment(d, a, b))
        or (third == 0 and _point_on_segment(a, c, d))
        or (fourth == 0 and _point_on_segment(b, c, d))
    )


def _signed_area(points: Sequence[Point]) -> float:
    """Вычислить ориентированную площадь простого полигона."""
    return sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    ) / 2.0


@dataclass(frozen=True, slots=True)
class Polygon:
    """Проверенный простой полигон с нормализованными координатами."""

    points: tuple[Point, ...]

    @classmethod
    def from_points(cls, values: Sequence[Any]) -> Polygon:
        """Создать полигон и отклонить вырожденную или самопересекающуюся форму."""
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise PolygonValidationError("Вершины полигона должны быть списком")

        points = [_point(value, index) for index, value in enumerate(values)]
        if len(points) > 1 and points[0] == points[-1]:
            points.pop()
        if len(points) < 3:
            raise PolygonValidationError("Полигон должен иметь не менее трёх вершин")

        for index, point in enumerate(points):
            next_point = points[(index + 1) % len(points)]
            if point == next_point:
                raise PolygonValidationError("Соседние вершины полигона совпадают")

        for index, point in enumerate(points):
            previous = points[index - 1]
            following = points[(index + 1) % len(points)]
            incoming = (point[0] - previous[0], point[1] - previous[1])
            outgoing = (following[0] - point[0], following[1] - point[1])
            if (
                _orientation(previous, point, following) == 0
                and incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
                < -_EPSILON
            ):
                raise PolygonValidationError(
                    "Соседние стороны полигона не должны идти назад по одной линии"
                )

        if abs(_signed_area(points)) <= _EPSILON:
            raise PolygonValidationError("Площадь полигона должна быть больше нуля")

        count = len(points)
        for first_index in range(count):
            first_start = points[first_index]
            first_end = points[(first_index + 1) % count]
            for second_index in range(first_index + 1, count):
                if second_index == first_index + 1:
                    continue
                if first_index == 0 and second_index == count - 1:
                    continue
                second_start = points[second_index]
                second_end = points[(second_index + 1) % count]
                if _segments_intersect(
                    first_start,
                    first_end,
                    second_start,
                    second_end,
                ):
                    raise PolygonValidationError(
                        "Стороны полигона не должны пересекаться"
                    )

        return cls(tuple(points))


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """Ограничивающая рамка детекции в нормализованных координатах."""

    left: float
    top: float
    right: float
    bottom: float

    @classmethod
    def from_detection(cls, detection: Mapping[str, Any]) -> BoundingBox:
        """Построить рамку из полей детекции и проверить порядок координат."""
        try:
            left = _number(detection.get("left"), "left рамки")
            top = _number(detection.get("top"), "top рамки")
            right = _number(detection.get("right"), "right рамки")
            bottom = _number(detection.get("bottom"), "bottom рамки")
        except ValueError as err:
            raise ValueError(str(err)) from err

        if left > right or top > bottom:
            raise ValueError("Границы рамки детекции заданы в неверном порядке")
        return cls(left=left, top=top, right=right, bottom=bottom)

    def corners(self) -> tuple[Point, Point, Point, Point]:
        """Вернуть углы рамки в обход по часовой стрелке."""
        return (
            (self.left, self.top),
            (self.right, self.top),
            (self.right, self.bottom),
            (self.left, self.bottom),
        )

    def edges(self) -> tuple[tuple[Point, Point], ...]:
        """Вернуть четыре стороны рамки."""
        corners = self.corners()
        return tuple(
            (corners[index], corners[(index + 1) % len(corners)])
            for index in range(len(corners))
        )


def _point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Проверить попадание точки внутрь полигона или на его границу."""
    inside = False
    points = polygon.points
    for index, start in enumerate(points):
        end = points[(index + 1) % len(points)]
        if _point_on_segment(point, start, end):
            return True
        if (start[1] > point[1]) != (end[1] > point[1]):
            intersection_x = start[0] + (point[1] - start[1]) * (
                end[0] - start[0]
            ) / (end[1] - start[1])
            if intersection_x > point[0]:
                inside = not inside
    return inside


def _point_in_box(point: Point, box: BoundingBox) -> bool:
    """Проверить попадание точки внутрь рамки или на её границу."""
    return (
        box.left - _EPSILON <= point[0] <= box.right + _EPSILON
        and box.top - _EPSILON <= point[1] <= box.bottom + _EPSILON
    )


def _segment_intersection_parameters(
    start: Point,
    end: Point,
    other_start: Point,
    other_end: Point,
) -> list[float]:
    """Найти параметры пересечения границы полигона с отрезком рамки."""
    direction = (end[0] - start[0], end[1] - start[1])
    other_direction = (
        other_end[0] - other_start[0],
        other_end[1] - other_start[1],
    )
    offset = (other_start[0] - start[0], other_start[1] - start[1])
    denominator = _cross(direction, other_direction)

    if abs(denominator) <= _EPSILON:
        if abs(_cross(offset, direction)) > _EPSILON:
            return []
        length_squared = direction[0] ** 2 + direction[1] ** 2
        if length_squared <= _EPSILON**2:
            return [0.0] if _point_on_segment(start, other_start, other_end) else []
        values = []
        for point in (other_start, other_end):
            parameter = (
                (point[0] - start[0]) * direction[0]
                + (point[1] - start[1]) * direction[1]
            ) / length_squared
            if -_EPSILON <= parameter <= 1.0 + _EPSILON:
                values.append(min(1.0, max(0.0, parameter)))
        return values

    parameter = _cross(offset, other_direction) / denominator
    other_parameter = _cross(offset, direction) / denominator
    if (
        -_EPSILON <= parameter <= 1.0 + _EPSILON
        and -_EPSILON <= other_parameter <= 1.0 + _EPSILON
    ):
        return [min(1.0, max(0.0, parameter))]
    return []


def _segment_inside_polygon(start: Point, end: Point, polygon: Polygon) -> bool:
    """Проверить, что отрезок целиком лежит внутри полигона или на границе."""
    if start == end:
        return _point_in_polygon(start, polygon)

    parameters = [0.0, 1.0]
    for index, edge_start in enumerate(polygon.points):
        edge_end = polygon.points[(index + 1) % len(polygon.points)]
        parameters.extend(
            _segment_intersection_parameters(
                start,
                end,
                edge_start,
                edge_end,
            )
        )

    ordered = sorted(parameters)
    unique: list[float] = []
    for parameter in ordered:
        if not unique or abs(parameter - unique[-1]) > _EPSILON:
            unique.append(parameter)

    direction = (end[0] - start[0], end[1] - start[1])
    for parameter in unique:
        point = (
            start[0] + direction[0] * parameter,
            start[1] + direction[1] * parameter,
        )
        if not _point_in_polygon(point, polygon):
            return False

    for start_parameter, end_parameter in zip(unique, unique[1:]):
        if end_parameter - start_parameter <= _EPSILON:
            continue
        parameter = (start_parameter + end_parameter) / 2.0
        point = (
            start[0] + direction[0] * parameter,
            start[1] + direction[1] * parameter,
        )
        if not _point_in_polygon(point, polygon):
            return False
    return True


def box_intersects_polygon(box: BoundingBox, polygon: Polygon) -> bool:
    """Проверить любое пересечение рамки детекции с полигоном."""
    corners = box.corners()
    if any(_point_in_polygon(point, polygon) for point in corners):
        return True
    if any(_point_in_box(point, box) for point in polygon.points):
        return True
    for box_start, box_end in box.edges():
        for index, polygon_start in enumerate(polygon.points):
            polygon_end = polygon.points[(index + 1) % len(polygon.points)]
            if _segments_intersect(box_start, box_end, polygon_start, polygon_end):
                return True
    return False


def box_inside_polygon(box: BoundingBox, polygon: Polygon) -> bool:
    """Проверить полное вхождение рамки в полигон, включая вогнутые части."""
    if not all(_point_in_polygon(point, polygon) for point in box.corners()):
        return False
    return all(
        _segment_inside_polygon(start, end, polygon)
        for start, end in box.edges()
    )


def box_matches_polygon(
    box: BoundingBox,
    polygon: Polygon,
    *,
    covers: bool = False,
) -> bool:
    """Применить семантику DOODS2: любое пересечение либо полное вхождение."""
    if covers:
        return box_inside_polygon(box, polygon)
    return box_intersects_polygon(box, polygon)


@dataclass(frozen=True, slots=True)
class PolygonZone:
    """Полигональная зона с порогами confidence по меткам."""

    zone_id: str
    polygon: Polygon
    labels: Mapping[str, float]
    covers: bool = False

    def __post_init__(self) -> None:
        """Проверить идентификатор зоны, режим покрытия и пороги меток."""
        if not isinstance(self.zone_id, str) or not self.zone_id.strip():
            raise ValueError("У зоны должен быть непустой идентификатор")
        if not isinstance(self.covers, bool):
            raise ValueError("covers должен быть логическим значением")
        if not isinstance(self.labels, Mapping):
            raise ValueError("labels зоны должны быть словарём порогов")

        normalized: dict[str, float] = {}
        for label, threshold in self.labels.items():
            if not isinstance(label, str) or not label:
                raise ValueError("Метка зоны должна быть непустой строкой")
            value = _number(threshold, f"Порог метки {label}")
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"Порог метки {label} должен быть от 0 до 100")
            normalized[label] = value
        object.__setattr__(self, "labels", MappingProxyType(normalized))


def _passes_thresholds(
    label: str,
    confidence: float,
    thresholds: Mapping[str, float],
) -> bool:
    """Проверить порог конкретной метки или общий wildcard-порог."""
    if label in thresholds:
        return confidence >= thresholds[label]
    if "*" in thresholds:
        return confidence >= thresholds["*"]
    return False


def filter_detections_by_zones(
    detections: Sequence[Mapping[str, Any]],
    global_thresholds: Mapping[str, float],
    zones: Sequence[PolygonZone],
) -> list[dict[str, Any]]:
    """Фильтровать объекты и назначать прошедшую детекцию первой подходящей зоне."""
    accepted: list[dict[str, Any]] = []
    for detection in detections:
        label = detection.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError("У детекции должна быть непустая метка")
        confidence = _number(detection.get("confidence"), "confidence")
        box = BoundingBox.from_detection(detection)

        if _passes_thresholds(label, confidence, global_thresholds):
            accepted.append(dict(detection))
            continue

        for zone in zones:
            if not box_matches_polygon(box, zone.polygon, covers=zone.covers):
                continue
            if not _passes_thresholds(label, confidence, zone.labels):
                continue
            accepted_detection = dict(detection)
            accepted_detection["region_id"] = zone.zone_id
            accepted.append(accepted_detection)
            break

    return accepted
