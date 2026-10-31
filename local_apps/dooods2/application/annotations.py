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
    ) -> None:
        """Нарисовать Unicode-строку в цвете и размере, близких к настройкам DOODS2."""
        if not text or thickness <= 0:
            return
        font_size = max(10, round(_FONT_SIZE_SCALE * max(0.5, float(font_scale))))
        font = _load_font(font_size)
        stroke_width = max(0, int(thickness) - 1)
        bbox = self._draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)
        x, baseline = origin
        y = max(0, baseline - bbox[3])
        rgb_color = tuple(
            max(0, min(255, int(channel)))
            for channel in reversed(color_bgr[:3])
        )
        self._draw.text(
            (x, y),
            text,
            font=font,
            fill=rgb_color,
            stroke_width=stroke_width,
            stroke_fill=rgb_color,
        )
        self._changed = True

    def to_bgr(self) -> np.ndarray:
        """Вернуть BGR-кадр с надписями, не меняя входное изображение."""
        if not self._changed:
            return self._original
        rgb_image = np.asarray(self._image, dtype=np.uint8)
        return np.ascontiguousarray(rgb_image[:, :, ::-1])
