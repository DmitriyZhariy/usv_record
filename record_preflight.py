#!/usr/bin/env python3
"""Предпусковые проверки перед записью обеих камер.

Проверки не зависят от PyAV и от HTTP-панели: их вызывает wrapper
record_dual_session.py, поэтому прямой запуск из CLI не обходит их.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

MIB = 1024 * 1024
EXPOSURE_CONTROL = "exposure_dynamic_framerate"
DEFAULT_V4L2_CTL = "v4l2-ctl"
COMMAND_TIMEOUT_S = 5.0


class PreflightError(RuntimeError):
    """Проверка не пройдена; запись запускать нельзя."""


def _default_runner(argv, timeout):
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
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


def verify_exposure(device, v4l2_ctl=DEFAULT_V4L2_CTL, runner=None):
    """Читает exposure_dynamic_framerate и требует 0.

    Настройка камеры не изменяется: при ненулевом значении preflight
    завершается ошибкой с командой для оператора.
    """
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
    if value != 0:
        raise PreflightError(
            f"{device}: {EXPOSURE_CONTROL}={value}, требуется 0. "
            f"Отключите: {v4l2_ctl} -d {device} "
            f"--set-ctrl={EXPOSURE_CONTROL}=0")
    return {"device": str(device), EXPOSURE_CONTROL: value}


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
                  runner=None, usage=None, proc_root="/proc"):
    """Полная проверка перед созданием сессии и запуском рекордеров."""
    if critical_free_mib is not None:
        if critical_free_mib <= 0:
            raise PreflightError("критический резерв должен быть > 0")
        if critical_free_mib >= min_free_mib:
            raise PreflightError(
                "критический резерв должен быть меньше порога запуска")

    space = ensure_output_root(output_root, min_free_mib, usage=usage)

    checked = []
    for device in devices:
        holders = find_device_holders(device, proc_root=proc_root)
        if holders:
            raise PreflightError(
                f"{device}: устройство уже открыто процессами "
                f"{holders}; остановите просмотр/запись")
        checked.append(verify_exposure(
            device, v4l2_ctl=v4l2_ctl, runner=runner))

    return {
        "space": space,
        "cameras": checked,
        "min_free_mib": min_free_mib,
        "critical_free_mib": critical_free_mib,
    }
