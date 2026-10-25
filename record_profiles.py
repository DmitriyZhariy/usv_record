#!/usr/bin/env python3
"""Профили записи: единый список режимов MJPG.

Модуль не импортирует PyAV и не обращается к оборудованию, поэтому его
используют и панель, и wrapper, и рекордер. Один профиль применяется к
обеим камерам.
"""

from __future__ import annotations

import math

MJPEG_FORMAT = "mjpeg"
MJPEG_FOURCC = "MJPG"

DEFAULT_MODE = "1280x720@60"
DEFAULT_EXPOSURE_DYNAMIC_FRAMERATE = 0
DEFAULT_DURATION_SECONDS = 0.0

# Допуск сравнения округлённого FPS из вывода драйвера.
# Драйвер может печатать 59.940 вместо 60; 0.5 к/с не смешивает
# соседние режимы 30 и 60.
FPS_TOLERANCE = 0.5

# Согласованный список режимов. Порядок отображается в интерфейсе.
MODES = (
    {"mode": "1920x1080@60", "width": 1920, "height": 1080, "fps": 60.0,
     "format": MJPEG_FORMAT},
    {"mode": "1280x720@60", "width": 1280, "height": 720, "fps": 60.0,
     "format": MJPEG_FORMAT},
    {"mode": "1280x720@30", "width": 1280, "height": 720, "fps": 30.0,
     "format": MJPEG_FORMAT},
    {"mode": "800x600@60", "width": 800, "height": 600, "fps": 60.0,
     "format": MJPEG_FORMAT},
    {"mode": "800x600@30", "width": 800, "height": 600, "fps": 30.0,
     "format": MJPEG_FORMAT},
    {"mode": "640x480@60", "width": 640, "height": 480, "fps": 60.0,
     "format": MJPEG_FORMAT},
    {"mode": "640x480@30", "width": 640, "height": 480, "fps": 30.0,
     "format": MJPEG_FORMAT},
)

MODE_INDEX = {item["mode"]: item for item in MODES}

# Длительности, предлагаемые в интерфейсе (секунды).
UI_DURATIONS = (0, 10, 30, 300, 600, 1800, 3600)

PROFILE_KEYS = frozenset(
    ("mode", "exposure_dynamic_framerate", "duration_seconds"))


class ProfileError(ValueError):
    """Профиль или режим недопустим."""


def get_mode(name):
    """Возвращает описание режима по имени или None."""
    if not isinstance(name, str):
        return None
    return MODE_INDEX.get(name)


def require_mode(name):
    mode = get_mode(name)
    if mode is None:
        raise ProfileError(f"неизвестный режим: {name!r}")
    return mode


def resolve_mode(width, height, fps):
    """Находит точный запрошенный режим без скрытой подмены FPS.

    Допуск FPS_TOLERANCE нужен только для округлённого ответа драйвера,
    а не для нормализации пользовательского запроса.
    """
    if (isinstance(width, bool) or not isinstance(width, int)
            or isinstance(height, bool) or not isinstance(height, int)
            or isinstance(fps, bool) or not isinstance(fps, (int, float))
            or width <= 0 or height <= 0 or fps <= 0
            or (isinstance(fps, float) and not math.isfinite(fps))):
        raise ProfileError("width/height должны быть целыми > 0, FPS — числом > 0")
    for item in MODES:
        if (item["width"] == width and item["height"] == height
                and item["fps"] == fps):
            return item
    raise ProfileError(
        f"неизвестная комбинация {width}x{height}@{fps}; "
        "разрешены только режимы MJPG из списка")


def capture_options(mode):
    """Options для av.open(..., format="v4l2")."""
    return {
        "input_format": mode["format"],
        "video_size": f"{mode['width']}x{mode['height']}",
        "framerate": f"{mode['fps']:g}",
    }


def make_profile(mode, exposure_dynamic_framerate, duration_seconds):
    """Создаёт и проверяет профиль из уже разобранных значений."""
    mode_info = require_mode(mode)
    if (isinstance(exposure_dynamic_framerate, bool)
            or not isinstance(exposure_dynamic_framerate, int)
            or exposure_dynamic_framerate not in (0, 1)):
        raise ProfileError(
            "exposure_dynamic_framerate должен быть целым 0 или 1")
    if isinstance(duration_seconds, bool) or not isinstance(
            duration_seconds, (int, float)):
        raise ProfileError("duration_seconds должен быть числом")
    try:
        duration = float(duration_seconds)
    except (OverflowError, ValueError):
        raise ProfileError("duration_seconds должен быть конечным числом >= 0")
    if not math.isfinite(duration) or duration < 0:
        raise ProfileError("duration_seconds должен быть >= 0")
    return {
        "mode": mode_info["mode"],
        "exposure_dynamic_framerate": exposure_dynamic_framerate,
        "duration_seconds": int(duration) if duration.is_integer()
        else duration,
    }


def validate_profile(payload):
    """Строгая проверка тела POST /api/profile/apply.

    Принимаются только известные ключи, все три обязательны.
    bool не считается int.
    """
    if not isinstance(payload, dict):
        raise ProfileError("ожидался JSON-объект профиля")
    unknown = set(payload) - PROFILE_KEYS
    if unknown:
        raise ProfileError(
            "неизвестные ключи профиля: " + ", ".join(sorted(unknown)))
    missing = PROFILE_KEYS - set(payload)
    if missing:
        raise ProfileError(
            "отсутствуют ключи профиля: " + ", ".join(sorted(missing)))
    return make_profile(
        payload["mode"],
        payload["exposure_dynamic_framerate"],
        payload["duration_seconds"],
    )


def profile_details(profile):
    """Развёрнутое представление профиля для метаданных и статуса."""
    mode = require_mode(profile["mode"])
    return {
        "mode": mode["mode"],
        "format": mode["format"],
        "width": mode["width"],
        "height": mode["height"],
        "fps": mode["fps"],
        "exposure_dynamic_framerate":
            profile["exposure_dynamic_framerate"],
        "duration_seconds": profile["duration_seconds"],
    }


def recorder_fields(profile):
    """Поля метаданных рекордера: requested_mode и profile.

    Существующее имя requested_mode сохраняется; поля внутри прежние.
    """
    mode = require_mode(profile["mode"])
    return {
        "requested_mode": {
            "format": mode["format"],
            "width": mode["width"],
            "height": mode["height"],
            "fps": mode["fps"],
        },
        "profile": profile_details(profile),
    }


def mode_matches(mode, codec, width, height):
    """Совпадают ли подтверждённые codec/размер с запрошенным режимом."""
    return (mode is not None and codec == mode["format"]
            and width == mode["width"] and height == mode["height"])


def measured_rate(first_mono_ns, last_mono_ns, count):
    """Темп приёма пакетов: (N - 1) / (t_last - t_first).

    Используется монотонное время. При недостаточных данных — None.
    Это не время экспозиции и не синхронизация камер.
    """
    if count < 2 or first_mono_ns is None or last_mono_ns is None:
        return None
    span_s = (last_mono_ns - first_mono_ns) / 1_000_000_000
    if span_s <= 0:
        return None
    return round((count - 1) / span_s, 2)
