"""Локализация надписей для размеченных изображений DOOODS2."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = Path(__file__).with_name("fonts") / "DejaVuSans.ttf"
_FONT_SIZE_SCALE = 16
_ADAPTIVE_COLOR_PALETTE_BGR = (
    (0, 0, 0),        # black
    (255, 255, 255),  # white
    (255, 0, 255),    # magenta
    (255, 255, 0),    # cyan
    (0, 255, 255),    # yellow
    (0, 0, 255),      # red
    (0, 255, 0),      # green
    (255, 0, 0),      # blue
)


def _normalized_bgr_color(value: Sequence[int]) -> tuple[int, int, int]:
    """Clamp an RGB-like three-channel color to the OpenCV BGR byte range."""
    channels = list(value[:3])
    if len(channels) < 3:
        channels.extend([0] * (3 - len(channels)))
    return tuple(max(0, min(255, int(channel))) for channel in channels)


def _relative_luminance_bgr(colors: np.ndarray) -> np.ndarray:
    """Calculate WCAG relative luminance for one or more BGR colors."""
    rgb = np.clip(np.asarray(colors, dtype=np.float32)[..., ::-1] / 255.0, 0.0, 1.0)
    linear_rgb = np.where(
        rgb <= 0.04045,
        rgb / 12.92,
        ((rgb + 0.055) / 1.055) ** 2.4,
    )
    return (
        0.2126 * linear_rgb[..., 0]
        + 0.7152 * linear_rgb[..., 1]
        + 0.0722 * linear_rgb[..., 2]
    )


def _local_background_samples(
    image_bgr: np.ndarray,
    bounds: Sequence[int],
) -> np.ndarray | None:
    """Sample a narrow ring outside a detection box, falling back to the frame edge."""
    if (
        not isinstance(image_bgr, np.ndarray)
        or getattr(image_bgr, "ndim", 0) != 3
        or image_bgr.shape[2] < 3
        or image_bgr.shape[0] < 1
        or image_bgr.shape[1] < 1
    ):
        return None

    height, width = image_bgr.shape[:2]
    try:
        left, top, right, bottom = (int(round(float(value))) for value in bounds[:4])
    except (TypeError, ValueError, OverflowError):
        left, top, right, bottom = 0, 0, width, height
    left = min(width - 1, max(0, left))
    top = min(height - 1, max(0, top))
    right = min(width, max(left + 1, right))
    bottom = min(height, max(top + 1, bottom))
    margin = max(2, min(12, round(max(right - left, bottom - top) * 0.02)))

    strips: list[np.ndarray] = []
    if top > 0:
        strips.append(image_bgr[max(0, top - margin):top, left:right, :3])
    if bottom < height:
        strips.append(image_bgr[bottom:min(height, bottom + margin), left:right, :3])
    if left > 0:
        strips.append(image_bgr[top:bottom, max(0, left - margin):left, :3])
    if right < width:
        strips.append(image_bgr[top:bottom, right:min(width, right + margin), :3])

    if not any(strip.size for strip in strips):
        border = max(1, min(8, round(min(height, width) * 0.02)))
        strips = [
            image_bgr[:border, :, :3],
            image_bgr[-border:, :, :3],
            image_bgr[:, :border, :3],
            image_bgr[:, -border:, :3],
        ]

    nonempty = [strip.reshape(-1, 3) for strip in strips if strip.size]
    if not nonempty:
        return None
    samples = np.concatenate(nonempty, axis=0).astype(np.float32, copy=False)
    if len(samples) > 1024:
        sample_indices = np.linspace(0, len(samples) - 1, 1024, dtype=np.int32)
        samples = samples[sample_indices]
    return samples


def _adaptive_color_score(
    candidate_bgr: tuple[int, int, int],
    samples_bgr: np.ndarray,
    sample_luminance: np.ndarray,
    minimum_contrast_ratio: float,
) -> float:
    """Score a color by its lower-tail luminance contrast and median color distance."""
    candidate = np.asarray(candidate_bgr, dtype=np.float32)
    candidate_luminance = float(_relative_luminance_bgr(candidate))
    lighter = np.maximum(candidate_luminance, sample_luminance)
    darker = np.minimum(candidate_luminance, sample_luminance)
    contrast = float(np.percentile((lighter + 0.05) / (darker + 0.05), 10))

    median_background_rgb = np.median(samples_bgr[:, ::-1], axis=0)
    candidate_rgb = candidate[::-1]
    color_distance = float(
        np.linalg.norm(candidate_rgb - median_background_rgb)
        / np.sqrt(3.0 * 255.0 * 255.0)
    )
    return min(contrast / minimum_contrast_ratio, color_distance / 0.35)


def adaptive_annotation_color_bgr(
    image_bgr: np.ndarray,
    bounds: Sequence[int],
    preferred_color_bgr: Sequence[int],
    *,
    minimum_contrast_ratio: float = 3.0,
) -> tuple[int, int, int]:
    """Keep a configured color when legible; otherwise choose a high-contrast color.

    Contrast is evaluated against pixels in a narrow ring around the detection,
    rather than against one global day/night estimate. The 10th-percentile score
    makes the choice less sensitive to a few unusually bright or dark pixels.
    """
    preferred = _normalized_bgr_color(preferred_color_bgr)
    samples_bgr = _local_background_samples(image_bgr, bounds)
    if samples_bgr is None:
        return preferred
    try:
        target_contrast = float(minimum_contrast_ratio)
    except (TypeError, ValueError, OverflowError):
        target_contrast = 3.0
    if not np.isfinite(target_contrast):
        target_contrast = 3.0
    target_contrast = max(1.0, target_contrast)

    sample_luminance = _relative_luminance_bgr(samples_bgr)
    preferred_score = _adaptive_color_score(
        preferred,
        samples_bgr,
        sample_luminance,
        target_contrast,
    )
    if preferred_score >= 1.0:
        return preferred

    return max(
        _ADAPTIVE_COLOR_PALETTE_BGR,
        key=lambda candidate: _adaptive_color_score(
            candidate,
            samples_bgr,
            sample_luminance,
            target_contrast,
        ),
    )


def _single_color_luminance_bgr(color_bgr: Sequence[int]) -> float:
    """Calculate relative luminance for one color without allocating an image array."""
    rgb = [max(0, min(255, int(channel))) / 255.0 for channel in reversed(color_bgr[:3])]
    linear_rgb = [
        channel / 12.92
        if channel <= 0.04045
        else ((channel + 0.055) / 1.055) ** 2.4
        for channel in rgb
    ]
    return (
        0.2126 * linear_rgb[0]
        + 0.7152 * linear_rgb[1]
        + 0.0722 * linear_rgb[2]
    )


def opposite_light_dark_color_bgr(color_bgr: Sequence[int]) -> tuple[int, int, int]:
    """Return black or white, whichever contrasts more with the supplied color."""
    color = _normalized_bgr_color(color_bgr)
    luminance = _single_color_luminance_bgr(color)
    black_contrast = (luminance + 0.05) / 0.05
    white_contrast = 1.05 / (luminance + 0.05)
    return (0, 0, 0) if black_contrast >= white_contrast else (255, 255, 255)


_RUSSIAN_LABELS = {
    "person": "человек",
    "bicycle": "велосипед",
    "car": "автомобиль",
    "motorcycle": "мотоцикл",
    "airplane": "самолёт",
    "bus": "автобус",
    "train": "поезд",
    "truck": "грузовик",
    "boat": "лодка",
    "traffic light": "светофор",
    "fire hydrant": "пожарный гидрант",
    "stop sign": "знак «Стоп»",
    "parking meter": "парковочный автомат",
    "bench": "скамейка",
    "bird": "птица",
    "cat": "кошка",
    "dog": "собака",
    "horse": "лошадь",
    "sheep": "овца",
    "cow": "корова",
    "elephant": "слон",
    "bear": "медведь",
    "zebra": "зебра",
    "giraffe": "жираф",
    "backpack": "рюкзак",
    "umbrella": "зонт",
    "handbag": "сумка",
    "tie": "галстук",
    "suitcase": "чемодан",
    "frisbee": "фрисби",
    "skis": "лыжи",
    "ski": "лыжи",
    "snowboard": "сноуборд",
    "sports ball": "спортивный мяч",
    "kite": "воздушный змей",
    "baseball bat": "бейсбольная бита",
    "baseball glove": "бейсбольная перчатка",
    "skateboard": "скейтборд",
    "surfboard": "доска для сёрфинга",
    "tennis racket": "теннисная ракетка",
    "bottle": "бутылка",
    "wine glass": "бокал",
    "cup": "чашка",
    "fork": "вилка",
    "knife": "нож",
    "spoon": "ложка",
    "bowl": "миска",
    "banana": "банан",
    "apple": "яблоко",
    "sandwich": "сэндвич",
    "orange": "апельсин",
    "broccoli": "брокколи",
    "carrot": "морковь",
    "hot dog": "хот-дог",
    "pizza": "пицца",
    "donut": "пончик",
    "cake": "торт",
    "chair": "стул",
    "couch": "диван",
    "potted plant": "растение в горшке",
    "bed": "кровать",
    "dining table": "обеденный стол",
    "toilet": "унитаз",
    "tv": "телевизор",
    "laptop": "ноутбук",
    "mouse": "мышь",
    "remote": "пульт",
    "keyboard": "клавиатура",
    "cell phone": "мобильный телефон",
    "cellphone": "мобильный телефон",
    "microwave": "микроволновка",
    "oven": "духовка",
    "toaster": "тостер",
    "sink": "раковина",
    "refrigerator": "холодильник",
    "book": "книга",
    "clock": "часы",
    "vase": "ваза",
    "scissors": "ножницы",
    "teddy bear": "плюшевый мишка",
    "hair drier": "фен",
    "hair dryer": "фен",
    "toothbrush": "зубная щётка",
    "egg": "яйцо",
    "flower": "цветок",
    "plant": "растение",
    "van": "фургон",
}


def normalize_annotation_locale(value: Any) -> str:
    """Свести локаль HA к поддерживаемому языку подписей: ru или en."""
    if not isinstance(value, str) or not value.strip():
        return "en"
    language = value.strip().replace("_", "-").split("-", 1)[0].casefold()
    return "ru" if language == "ru" else "en"


def _locale_key(value: str) -> str | None:
    """Вернуть поддерживаемый язык для пользовательского словаря или None."""
    language = value.strip().replace("_", "-").split("-", 1)[0].casefold()
    return language if language in {"ru", "en"} else None


def normalize_annotation_label_overrides(value: Any) -> dict[str, dict[str, str]]:
    """Проверить пользовательский JSON-словарь отображаемых меток ru/en."""
    payload = value
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(payload, Mapping):
        return {}

    normalized: dict[str, dict[str, str]] = {}
    for raw_locale, raw_labels in payload.items():
        if not isinstance(raw_locale, str) or not isinstance(raw_labels, Mapping):
            continue
        locale = _locale_key(raw_locale)
        if locale is None:
            continue
        labels = {
            source.strip().casefold(): display.strip()
            for source, display in raw_labels.items()
            if isinstance(source, str)
            and source.strip()
            and isinstance(display, str)
            and display.strip()
        }
        if labels:
            normalized[locale] = labels
    return normalized


class AnnotationTranslations:
    """Локализовать только надписи на изображении, не меняя метки API."""

    def __init__(self, overrides: Any = None) -> None:
        """Подготовить встроенные и пользовательские отображаемые имена."""
        self.overrides = normalize_annotation_label_overrides(overrides)

    def display_label(self, label: str, locale: Any) -> str:
        """Вернуть текст подписи для JPEG, сохраняя исходное имя неизвестной метки."""
        language = normalize_annotation_locale(locale)
        normalized_label = label.strip().casefold()
        if display := self.overrides.get(language, {}).get(normalized_label):
            return display
        if language == "ru":
            return _RUSSIAN_LABELS.get(normalized_label, label)
        return label


@lru_cache(maxsize=32)
def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Загрузить поставляемый шрифт с поддержкой кириллицы."""
    return ImageFont.truetype(str(_FONT_PATH), size=size)


class AnnotationCanvas:
    """Добавлять текст с поддержкой Unicode к изображению BGR из OpenCV."""

    def __init__(self, image_bgr: np.ndarray) -> None:
        """Создать одну PIL-поверхность для всех надписей кадра."""
        self._original = image_bgr
        rgb_image = np.ascontiguousarray(image_bgr[:, :, ::-1])
        self._image = Image.fromarray(rgb_image)
        self._draw = ImageDraw.Draw(self._image)
        self._changed = False

    def draw_text(
        self,
        text: str,
        origin: tuple[int, int],
        color_bgr: Sequence[int],
        font_scale: float,
        thickness: int,
        stroke_color_bgr: Sequence[int] | None = None,
    ) -> None:
        """Нарисовать Unicode-строку в цвете и размере, близких к настройкам DOODS2."""
        if not text or thickness <= 0:
            return
        font_size = max(10, round(_FONT_SIZE_SCALE * max(0.5, float(font_scale))))
        font = _load_font(font_size)
        stroke_width = (
            max(1, int(thickness) - 1)
            if stroke_color_bgr is not None
            else max(0, int(thickness) - 1)
        )
        bbox = self._draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        x, baseline = origin
        y = max(0, baseline - bbox[3])
        rgb_color = tuple(
            max(0, min(255, int(channel)))
            for channel in reversed(color_bgr[:3])
        )
        stroke_rgb = (
            tuple(
                max(0, min(255, int(channel)))
                for channel in reversed(stroke_color_bgr[:3])
            )
            if stroke_color_bgr is not None
            else rgb_color
        )
        self._draw.text(
            (x, y),
            text,
            font=font,
            fill=rgb_color,
            stroke_width=stroke_width,
            stroke_fill=stroke_rgb,
        )
        self._changed = True

    def to_bgr(self) -> np.ndarray:
        """Вернуть BGR-кадр с надписями, не меняя входное изображение."""
        if not self._changed:
            return self._original
        rgb_image = np.asarray(self._image, dtype=np.uint8)
        return np.ascontiguousarray(rgb_image[:, :, ::-1])
