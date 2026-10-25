#!/usr/bin/env python3
"""Предпусковые проверки перед записью обеих камер.

Проверки не зависят от PyAV и от HTTP-панели: их вызывает wrapper
record_dual_session.py, поэтому прямой запуск из CLI не обходит их.
Перед созданием сессии preflight проверяет поддержку выбранного режима
MJPG обеими камерами, устанавливает выбранное значение
exposure_dynamic_framerate на обеих камерах и читает его обратно.
Остальные настройки камер не изменяются.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import record_profiles

MIB = 1024 * 1024
EXPOSURE_CONTROL = "exposure_dynamic_framerate"
DEFAULT_V4L2_CTL = "v4l2-ctl"
COMMAND_TIMEOUT_S = 5.0
MJPEG_FOURCC = record_profiles.MJPEG_FOURCC
FPS_TOLERANCE = record_profiles.FPS_TOLERANCE

_FORMAT_RE = re.compile(r"^\s*\[\d+\]:\s*'([^']+)'")
_SIZE_RE = re.compile(
    r"^\s*Size:\s*(Discrete|Stepwise|Continuous)\s+(\d+)x(\d+)")
_INTERVAL_RE = re.compile(
    r"^\s*Interval:\s*(Discrete|Stepwise|Continuous)\b(.*)$")
_FPS_RE = re.compile(r"\(\s*([0-9.]+)\s*fps\s*\)")


class PreflightError(RuntimeError):
    """Проверка не пройдена; запись запускать нельзя."""


def _default_runner(argv, timeout):
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )


def _invoke(argv, runner, timeout=COMMAND_TIMEOUT_S):
    try:
        completed = runner(argv, timeout=timeout)
    except FileNotFoundError:
        raise PreflightError(
            f"не найдена команда {argv[0]!r}; установите v4l2-utils")
    except subprocess.TimeoutExpired:
        raise PreflightError(f"таймаут команды: {' '.join(argv)}")
    except OSError as exc:
        raise PreflightError(
            f"не удалось выполнить {' '.join(argv)}: "
            f"{type(exc).__name__}: {exc}")

    code = getattr(completed, "returncode", 1)
    stdout = getattr(completed, "stdout", "") or ""
    stderr = getattr(completed, "stderr", "") or ""
    if code != 0:
        detail = (stderr or stdout).strip()
        if detail:
            detail = f": {detail}"
        raise PreflightError(
            f"команда завершилась с кодом {code}: {' '.join(argv)}{detail}")
    return stdout + stderr


def set_exposure(device, value=0, v4l2_ctl=DEFAULT_V4L2_CTL, runner=None):
    """Устанавливает выбранное exposure_dynamic_framerate.

    Другие настройки камеры не изменяются. value — только 0 или 1.
    """
    if type(value) is not int or value not in (0, 1):
        raise PreflightError(
            f"{device}: exposure_dynamic_framerate должен быть 0 или 1")
    runner = runner or _default_runner
    set_argv = [
        str(v4l2_ctl), "-d", str(device),
        f"--set-ctrl={EXPOSURE_CONTROL}={value}",
    ]
    _invoke(set_argv, runner)
    return {"device": str(device), EXPOSURE_CONTROL: value}


def verify_exposure(device, expected=0, v4l2_ctl=DEFAULT_V4L2_CTL,
                    runner=None):
    """Читает exposure_dynamic_framerate обратно и требует expected."""
    if type(expected) is not int or expected not in (0, 1):
        raise PreflightError(
            f"{device}: ожидаемое значение должно быть 0 или 1")
    runner = runner or _default_runner
    get_argv = [
        str(v4l2_ctl), "-d", str(device),
        f"--get-ctrl={EXPOSURE_CONTROL}",
    ]
    output = _invoke(get_argv, runner)
    match = re.search(
        rf"{re.escape(EXPOSURE_CONTROL)}\s*:\s*(-?\d+)", output)
    if match is None:
        raise PreflightError(
            f"{device}: не удалось разобрать ответ v4l2-ctl: {output.strip()!r}")
    value = int(match.group(1))
    if value != expected:
        raise PreflightError(
            f"{device}: после установки {EXPOSURE_CONTROL}={value}, "
            f"требуется {expected}")
    return {"device": str(device), EXPOSURE_CONTROL: value}


def prepare_exposure(devices, value, v4l2_ctl=DEFAULT_V4L2_CTL, runner=None):
    """Устанавливает value на всех камерах и читает обратно на всех.

    Любая ошибка или несовпадение запрещает запуск обеих камер.
    """
    devices = tuple(devices)
    if not devices:
        raise PreflightError("не указаны камеры для подготовки exposure")
    for device in devices:
        set_exposure(device, value, v4l2_ctl=v4l2_ctl, runner=runner)
    checked = [
        verify_exposure(device, expected=value, v4l2_ctl=v4l2_ctl,
                        runner=runner)
        for device in devices
    ]
    return {"value": value, "cameras": checked}


def parse_formats_ext(output):
    """Разбирает вывод v4l2-ctl --list-formats-ext.

    Возвращает {fourcc: {"sizes": {(width, height): {
        "concrete": bool, "fps": [float], "non_discrete_intervals": bool,
    }}}}.

    Поддерживаются только Discrete-размеры и Discrete-интервалы.
    Stepwise/Continuous помечаются, но не считаются подтверждением
    режима: это ограничение парсера, а не доказательство отсутствия
    поддержки камерой.
    """
    formats = {}
    current = None
    current_size = None
    for line in output.splitlines():
        format_match = _FORMAT_RE.match(line)
        if format_match:
            fourcc = format_match.group(1).upper()
            current = formats.setdefault(fourcc, {"sizes": {}})
            current_size = None
            continue
        if current is None:
            continue
        size_match = _SIZE_RE.match(line)
        if size_match:
            kind, width, height = size_match.groups()
            key = (int(width), int(height))
            current_size = current["sizes"].setdefault(
                key,
                {"concrete": kind == "Discrete", "fps": [],
                 "non_discrete_intervals": False},
            )
            if kind != "Discrete":
                current_size["concrete"] = False
            continue
        interval_match = _INTERVAL_RE.match(line)
        if interval_match and current_size is not None:
            kind, rest = interval_match.groups()
            if kind != "Discrete":
                current_size["non_discrete_intervals"] = True
                continue
            fps_match = _FPS_RE.search(rest)
            if fps_match is None:
                current_size["non_discrete_intervals"] = True
                continue
            try:
                fps = float(fps_match.group(1))
            except ValueError:
                current_size["non_discrete_intervals"] = True
                continue
            if fps > 0:
                current_size["fps"].append(fps)
    return formats


def _mode_reason(parsed, mode):
    """Причина, почему режим не подтверждён, либо None."""
    section = parsed.get(MJPEG_FOURCC)
    if section is None:
        return ("камера не заявила MJPG; возможности YUYV не считаются "
                "поддержкой MJPG")
    size = section["sizes"].get((mode["width"], mode["height"]))
    if size is None:
        if any(not entry["concrete"] for entry in section["sizes"].values()):
            return ("формат перечисления размера Stepwise/Continuous "
                    "не поддерживается; поддержка не подтверждена")
        return f"MJPG {mode['width']}x{mode['height']} не заявлен"
    if not size["concrete"]:
        return ("формат перечисления размера Stepwise/Continuous "
                "не поддерживается; поддержка не подтверждена")
    if size["non_discrete_intervals"]:
        return ("формат перечисления интервалов Stepwise/Continuous "
                "не поддерживается; поддержка не подтверждена")
    if not any(abs(fps - mode["fps"]) <= FPS_TOLERANCE
               for fps in size["fps"]):
        listed = ", ".join(f"{fps:g}" for fps in size["fps"]) or "нет"
        return (f"заявлены интервалы {listed} к/с, "
                f"требуется {mode['fps']:g} к/с")
    return None


def list_formats(device, v4l2_ctl=DEFAULT_V4L2_CTL, runner=None):
    """Read-only перечисление режимов. Режим камеры не меняется."""
    runner = runner or _default_runner
    argv = [str(v4l2_ctl), "-d", str(device), "--list-formats-ext"]
    return _invoke(argv, runner)


def probe_modes(devices, modes=None, v4l2_ctl=DEFAULT_V4L2_CTL,
                runner=None):
    """Проверяет поддержку режимов MJPG на каждой камере.

    Режим доступен, только если подтверждён на всех переданных камерах.
    Ошибка команды, таймаут или неразбираемый вывод означают
    «поддержка не подтверждена», а не доказанное отсутствие режима.
    """
    devices = tuple(devices)
    if not devices:
        raise PreflightError("не указаны камеры для проверки режимов")
    modes = tuple(record_profiles.MODES if modes is None else modes)
    device_results = {}
    parsed = {}
    for device in devices:
        key = str(device)
        try:
            output = list_formats(device, v4l2_ctl=v4l2_ctl, runner=runner)
            formats = parse_formats_ext(output)
            if not formats:
                raise PreflightError(
                    "вывод --list-formats-ext не распознан")
        except PreflightError as exc:
            device_results[key] = {
                "ok": False,
                "error": f"поддержка не подтверждена: {exc}",
            }
            parsed[key] = None
        else:
            device_results[key] = {"ok": True, "error": None}
            parsed[key] = formats

    mode_results = {}
    for mode in modes:
        reasons = []
        for device in devices:
            key = str(device)
            info = device_results[key]
            if not info["ok"]:
                reasons.append(f"{key}: {info['error']}")
                continue
            reason = _mode_reason(parsed[key], mode)
            if reason is not None:
                reasons.append(f"{key}: {reason}")
        mode_results[mode["mode"]] = {
            "supported": not reasons,
            "reasons": reasons,
        }
    return {
        "ok": all(info["ok"] for info in device_results.values()),
        "devices": device_results,
        "modes": mode_results,
    }


def require_mode(devices, mode, v4l2_ctl=DEFAULT_V4L2_CTL, runner=None):
    """Бросает PreflightError, если режим не подтверждён всеми камерами."""
    report = probe_modes(devices, [mode], v4l2_ctl=v4l2_ctl, runner=runner)
    result = report["modes"][mode["mode"]]
    if not result["supported"]:
        raise PreflightError(
            f"режим {mode['mode']} не подтверждён обеими камерами: "
            + "; ".join(result["reasons"]))
    return report


def find_device_holders(device, proc_root="/proc"):
    """Диагностика Linux: PID, у которых открыт device.

    Это не гарантия взаимного исключения: процесс может открыть устройство
    сразу после проверки. Ошибки чтения /proc игнорируются.
    """
    holders = []
    proc = Path(proc_root)
    if not proc.is_dir():
        return holders
    target = os.path.realpath(str(device))
    try:
        entries = list(proc.iterdir())
    except OSError:
        return holders
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.path.realpath(os.readlink(fd)) == target:
                    holders.append(int(entry.name))
                    break
            except OSError:
                continue
    return sorted(holders)


def ensure_output_root(root, min_free_mib, usage=None):
    """Проверяет каталог, запись в него и минимальный свободный объём."""
    if min_free_mib <= 0:
        raise PreflightError("минимальный свободный объём должен быть > 0")

    root = Path(root).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError(
            f"не удалось создать каталог {root}: {type(exc).__name__}: {exc}")
    if not root.is_dir():
        raise PreflightError(f"не каталог: {root}")

    probe = None
    try:
        probe = tempfile.NamedTemporaryFile(
            mode="wb", prefix=".usv_preflight_", dir=str(root), delete=False)
        probe.write(b"preflight")
        probe.flush()
        os.fsync(probe.fileno())
        probe.close()
        probe = None
    except OSError as exc:
        raise PreflightError(
            f"каталог {root} недоступен для записи: "
            f"{type(exc).__name__}: {exc}")
    finally:
        if probe is not None:
            try:
                probe.close()
            except OSError:
                pass
        for item in root.glob(".usv_preflight_*"):
            try:
                item.unlink()
            except OSError:
                pass

    usage = usage or shutil.disk_usage
    try:
        stats = usage(str(root))
    except OSError as exc:
        raise PreflightError(
            f"не удалось получить свободное место для {root}: "
            f"{type(exc).__name__}: {exc}")

    free_mib = stats.free / MIB
    if free_mib < min_free_mib:
        raise PreflightError(
            f"свободно {free_mib:.0f} MiB, требуется не меньше "
            f"{min_free_mib} MiB в {root}")
    return {
        "root": str(root),
        "free_mib": round(free_mib, 1),
        "total_mib": round(stats.total / MIB, 1),
        "min_free_mib": min_free_mib,
    }


def run_preflight(output_root, devices, min_free_mib,
                  critical_free_mib=None, v4l2_ctl=DEFAULT_V4L2_CTL,
                  runner=None, usage=None, proc_root="/proc",
                  mode=None, exposure_dynamic_framerate=0):
    """Полная проверка перед созданием сессии и запуском рекордеров.

    Порядок: путь и место, доступность устройств, поддержка выбранного
    режима (по умолчанию 1280x720@60), установка выбранного
    exposure_dynamic_framerate на обеих камерах, чтение обратно.
    Любая ошибка запрещает запуск записи обеих камер.
    """
    if critical_free_mib is not None:
        if critical_free_mib <= 0:
            raise PreflightError("критический резерв должен быть > 0")
        if critical_free_mib >= min_free_mib:
            raise PreflightError(
                "критический резерв должен быть меньше порога запуска")

    devices = tuple(devices)
    if not devices:
        raise PreflightError("не указаны камеры для preflight")
    if mode is None:
        mode = record_profiles.require_mode(record_profiles.DEFAULT_MODE)
    space = ensure_output_root(output_root, min_free_mib, usage=usage)

    for device in devices:
        holders = find_device_holders(device, proc_root=proc_root)
        if holders:
            raise PreflightError(
                f"{device}: устройство уже открыто процессами "
                f"{holders}; остановите просмотр/запись")

    mode_support = require_mode(
        devices, mode, v4l2_ctl=v4l2_ctl, runner=runner)

    exposure = prepare_exposure(
        devices, exposure_dynamic_framerate,
        v4l2_ctl=v4l2_ctl, runner=runner)

    return {
        "space": space,
        "cameras": exposure["cameras"],
        "exposure_dynamic_framerate": exposure["value"],
        "mode": dict(mode),
        "mode_support": mode_support,
        "min_free_mib": min_free_mib,
        "critical_free_mib": critical_free_mib,
    }
