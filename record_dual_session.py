#!/usr/bin/env python3
"""Один запуск записи обеих камер в общую сессию.

Wrapper создаёт общий каталог сессии и логи, запускает два существующих
рекордера и управляет их завершением. Каталоги камер, захват, очередь,
метаданные, CSV и проверка остаются в record_camera_pyav_queue.py.
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import record_preflight as preflight
import record_profiles

DEFAULT_OUTPUT_ROOT = "~/recordings"
DEFAULT_LOCK_FILE = "~/.cache/usv_record/record_dual.lock"
STOP_GRACE_SECONDS = 3600.0
EXIT_GRACE_SECONDS = 3600.0
POLL_INTERVAL = 0.05
DEFAULT_MIN_FREE_MIB = 4096
DEFAULT_CRITICAL_FREE_MIB = 1024
DISK_CHECK_INTERVAL = 5.0

CAMERAS = (
    {
        "id": "cam_left",
        "dir": "cam-left",
        "label": "Левая камера",
        "device_arg": "device_left",
        "default_device": "/dev/video2",
    },
    {
        "id": "cam_right",
        "dir": "cam-right",
        "label": "Правая камера",
        "device_arg": "device_right",
        "default_device": "/dev/video4",
    },
)


class StopFlag:
    def __init__(self):
        self._set = False
        self.reason = None
        self.kind = None

    def set(self, reason, kind="operator"):
        if not self._set:
            self.reason = reason
            self.kind = kind
        self._set = True

    def is_set(self):
        return self._set


class Child:
    __slots__ = ("key", "process", "log_path")

    def __init__(self, key, process, log_path):
        self.key = key
        self.process = process
        self.log_path = log_path


def create_session(root, now=None):
    """Создаёт только корень сессии; каталоги камер создают рекордеры."""
    root = Path(root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise NotADirectoryError(f"not a directory: {root}")
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S_%f")
    for attempt in range(100):
        name = stamp if attempt == 0 else f"{stamp}_{attempt:02d}"
        session = root / name
        try:
            session.mkdir()
        except FileExistsError:
            continue
        return session
    raise RuntimeError(f"cannot create unique session dir under {root}")


def build_recorder_command(python, recorder, spec, device, output_dir, options):
    command = [
        str(python),
        str(recorder),
        "--camera-id", spec["id"],
        "--device", device,
        "--output-dir", str(output_dir),
        "--seconds", f"{options.seconds:g}",
        "--width", str(options.width),
        "--height", str(options.height),
        "--fps", f"{options.fps:g}",
        "--exposure-dynamic-framerate",
        str(options.exposure_dynamic_framerate),
        "--v4l2-ctl", str(options.v4l2_ctl),
        "--segment-seconds", f"{options.segment_seconds:g}",
        "--queue-frames", str(options.queue_frames),
        "--queue-mib", str(options.queue_mib),
    ]

    if getattr(options, "preview_root", None):
        command.extend(["--preview-root", str(options.preview_root)])
    return command


def _alive(children):
    return [child for child in children if child.process.poll() is None]


def _terminate(process):
    try:
        process.terminate()
    except (ProcessLookupError, OSError):
        pass


def _kill(process):
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass


def _wait(process):
    try:
        process.wait()
    except BaseException:
        pass


def shutdown_children(children, stop_grace=STOP_GRACE_SECONDS,
                      clock=time.monotonic, sleep=time.sleep):
    """SIGTERM всем живым, ожидание stop_grace, затем SIGKILL и wait().

    Возвращает True, если потребовалось принудительное завершение.
    """
    alive = _alive(children)
    for child in alive:
        _terminate(child.process)
    deadline = clock() + stop_grace
    while clock() < deadline and _alive(children):
        sleep(min(POLL_INTERVAL, max(0.0, deadline - clock())))
    forced = bool(_alive(children))
    for child in _alive(children):
        _kill(child.process)
    for child in children:
        _wait(child.process)
    return forced


def start_children(commands, session, popen=subprocess.Popen,
                   stop_grace=STOP_GRACE_SECONDS,
                   clock=time.monotonic, sleep=time.sleep):
    """Запускает рекордеры; при ошибке запуска гасит уже запущенные."""
    children = []
    handles = []
    try:
        for item in commands:
            log_path = Path(session) / item["log_name"]
            handle = log_path.open("w", encoding="utf-8")
            handles.append(handle)
            process = popen(
                item["argv"],
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name == "posix"),
            )
            children.append(Child(item["key"], process, log_path))
    except BaseException:
        shutdown_children(children, stop_grace=stop_grace,
                          clock=clock, sleep=sleep)
        raise
    finally:
        for handle in handles:
            handle.close()
    return children


class DualSupervisor:
    """Опрашивает оба процесса, не блокируясь на одном из них."""

    def __init__(self, stop_flag, clock=time.monotonic, sleep=time.sleep,
                 stop_grace=STOP_GRACE_SECONDS,
                 exit_grace=EXIT_GRACE_SECONDS,
                 poll_interval=POLL_INTERVAL,
                 disk_check=None, critical_free_mib=None,
                 disk_interval=DISK_CHECK_INTERVAL):
        self.stop_flag = stop_flag
        self.clock = clock
        self.sleep = sleep
        self.stop_grace = stop_grace
        self.exit_grace = exit_grace
        self.poll_interval = poll_interval
        self.disk_check = disk_check
        self.critical_free_mib = critical_free_mib
        self.disk_interval = disk_interval
        self.last_disk_check = None
        self.disk_error = None

    def _check_disk(self, now):
        if self.disk_check is None or self.stop_flag.is_set():
            return
        if (self.last_disk_check is not None
                and now - self.last_disk_check < self.disk_interval):
            return
        self.last_disk_check = now
        try:
            free_mib = float(self.disk_check())
        except Exception as exc:
            self.disk_error = f"{type(exc).__name__}: {exc}"
            self.stop_flag.set(
                f"disk_check_failed: {self.disk_error}", kind="disk")
            return
        if (self.critical_free_mib is not None
                and free_mib < self.critical_free_mib):
            self.stop_flag.set(
                "low_disk_reserve "
                f"free_mib={free_mib:.0f} "
                f"critical_mib={self.critical_free_mib}",
                kind="disk",
            )

    def _emergency_shutdown(self, children):
        alive = _alive(children)
        for child in alive:
            _terminate(child.process)
        deadline = self.clock() + self.stop_grace
        while self.clock() < deadline and _alive(children):
            self.sleep(min(self.poll_interval, max(0.0, deadline - self.clock())))
        for child in _alive(children):
            _kill(child.process)
        for child in children:
            _wait(child.process)

    def run(self, children):
        error = None
        forced = False
        shutdown_started = None
        first_clean_exit = None

        try:
            while True:
                codes = {child.key: child.process.poll() for child in children}
                alive = [child for child in children if codes[child.key] is None]
                if not alive:
                    break
                now = self.clock()
                self._check_disk(now)

                if shutdown_started is None:
                    if self.stop_flag.is_set():
                        for child in alive:
                            _terminate(child.process)
                        shutdown_started = now
                    else:
                        failed = [
                            child for child in children
                            if codes[child.key] not in (None, 0)
                        ]
                        if failed:
                            error = "рекордер завершился с ошибкой: " + ", ".join(
                                f"{child.key}={codes[child.key]}"
                                for child in failed
                            )
                            for child in alive:
                                _terminate(child.process)
                            shutdown_started = now
                        else:
                            if first_clean_exit is None and any(
                                    codes[child.key] == 0 for child in children):
                                first_clean_exit = now
                            if (first_clean_exit is not None
                                    and now - first_clean_exit >= self.exit_grace):
                                for child in alive:
                                    _kill(child.process)
                                forced = True
                                error = error or (
                                    "таймаут: один рекордер завершился, второй "
                                    "не завершился за отведённое время")
                                break
                elif now - shutdown_started >= self.stop_grace:
                    for child in alive:
                        _kill(child.process)
                    forced = True
                    error = error or "таймаут остановки рекордеров"
                    break

                self.sleep(self.poll_interval)
        except BaseException:
            self._emergency_shutdown(children)
            raise

        if _alive(children):
            for child in _alive(children):
                _kill(child.process)
            forced = True
            error = error or "остались незавершённые процессы"
        for child in children:
            _wait(child.process)

        return {
            "codes": {child.key: child.process.poll() for child in children},
            "forced": forced,
            "error": error,
            "operator_stop": self.stop_flag.is_set(),
            "stop_reason": self.stop_flag.reason,
            "stop_kind": self.stop_flag.kind,
            "disk_error": self.disk_error,
        }


def exit_code_for(result):
    if result["forced"] or result["error"]:
        return 1
    return 0 if all(code == 0 for code in result["codes"].values()) else 1


def acquire_wrapper_lock(path):
    """Linux-блокировка второго экземпляра wrapper.

    Возвращает (handle, status): None/"unavailable"/"busy"/текст ошибки.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        import fcntl
    except ImportError:
        return handle, "unavailable"
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None, "busy"
        return None, f"{type(exc).__name__}: {exc}"
    return handle, None


def build_parser():
    parser = argparse.ArgumentParser(
        description="Запись обеих камер в одну сессию (два рекордера).")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT,
                        help="корень сессий (default: ~/recordings)")
    parser.add_argument("--device-left", default="/dev/video2")
    parser.add_argument("--device-right", default="/dev/video4")
    parser.add_argument("--width", type=int, default=1280,
                        help="ширина MJPG-кадра (по умолчанию 1280)")
    parser.add_argument("--height", type=int, default=720,
                        help="высота MJPG-кадра (по умолчанию 720)")
    parser.add_argument("--fps", type=float, default=60,
                        help="запрашиваемый FPS (по умолчанию 60)")
    parser.add_argument("--exposure-dynamic-framerate", type=int,
                        choices=(0, 1), default=0,
                        help="0 — снижение частоты запрещено, 1 — разрешено")
    parser.add_argument("--preview-root", default=None)
    parser.add_argument("--seconds", type=float, default=35,
                        help="жёсткий предел, 0 — до остановки оператором")
    parser.add_argument("--segment-seconds", type=float, default=10)
    parser.add_argument("--queue-frames", type=int, default=120)
    parser.add_argument("--queue-mib", type=int, default=32)
    parser.add_argument("--min-free-mib", type=int,
                        default=DEFAULT_MIN_FREE_MIB,
                        help="отказ в запуске ниже этого запаса (default: 4096)")
    parser.add_argument("--critical-free-mib", type=int,
                        default=DEFAULT_CRITICAL_FREE_MIB,
                        help="штатная остановка ниже этого резерва "
                             "(default: 1024)")
    parser.add_argument("--v4l2-ctl", default=preflight.DEFAULT_V4L2_CTL,
                        help="путь к v4l2-ctl (для проверок и тестов)")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE,
                        help="lock-файл второго экземпляра wrapper")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.seconds < 0:
        print("--seconds должен быть >= 0 "
              "(0 — до остановки оператором)", file=sys.stderr)
        return 1
    if min(args.segment_seconds, args.queue_frames, args.queue_mib) <= 0:
        print("Все ограничения должны быть > 0", file=sys.stderr)
        return 1
    if (args.min_free_mib <= 0 or args.critical_free_mib <= 0
            or args.critical_free_mib >= args.min_free_mib):
        print("Требуется 0 < --critical-free-mib < --min-free-mib",
              file=sys.stderr)
        return 1
    try:
        mode = record_profiles.resolve_mode(args.width, args.height, args.fps)
        profile = record_profiles.make_profile(
            mode["mode"], args.exposure_dynamic_framerate, args.seconds)
    except record_profiles.ProfileError as exc:
        print(f"Недопустимый профиль: {exc}", file=sys.stderr)
        return 1

    recorder = Path(__file__).resolve().with_name("record_camera_pyav_queue.py")
    if not recorder.is_file():
        print(f"Не найден рекордер рядом с wrapper: {recorder}", file=sys.stderr)
        return 1

    lock_handle = None
    try:
        lock_handle, lock_status = acquire_wrapper_lock(args.lock_file)
    except OSError as exc:
        print(f"lock-файл недоступен: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    if lock_handle is None:
        if lock_status == "busy":
            print("Другой экземпляр wrapper уже работает "
                  f"(lock: {args.lock_file}). Запуск отменён.", file=sys.stderr)
        else:
            print(f"Не удалось взять lock {args.lock_file}: {lock_status}",
                  file=sys.stderr)
        return 1
    if lock_status == "unavailable":
        print("fcntl недоступен: блокировка второго экземпляра не работает.",
              file=sys.stderr)

    stop_flag = StopFlag()

    def handle_signal(signum, frame):
        stop_flag.set(signal.Signals(signum).name, kind="signal")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    exit_code = 1
    try:
        devices = [args.device_left, args.device_right]
        try:
            report = preflight.run_preflight(
                args.output_root, devices,
                min_free_mib=args.min_free_mib,
                critical_free_mib=args.critical_free_mib,
                v4l2_ctl=args.v4l2_ctl,
                mode=mode,
                exposure_dynamic_framerate=(
                    profile["exposure_dynamic_framerate"]),
            )
        except (preflight.PreflightError, OSError) as exc:
            print(f"Preflight failed: {exc}", file=sys.stderr)
            return 1

        space = report["space"]
        print(f"Profile: {profile['mode']} "
              f"dynamic={profile['exposure_dynamic_framerate']} "
              f"duration={profile['duration_seconds']:g}s", flush=True)
        print("Preflight: режим подтверждён обеими камерами; "
              f"{preflight.EXPOSURE_CONTROL}="
              f"{profile['exposure_dynamic_framerate']} установлен и "
              "подтверждён на "
              + ", ".join(item["device"] for item in report["cameras"]),
              flush=True)
        print(f"Preflight: свободно {space['free_mib']:.0f} MiB "
              f"(порог запуска {args.min_free_mib} MiB, "
              f"критический резерв {args.critical_free_mib} MiB)",
              flush=True)

        try:
            session = create_session(args.output_root)
        except OSError as exc:
            print(f"Не удалось создать каталог сессии: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1

        print(f"Output: {session}", flush=True)
        print(f"Сессия: {session}", flush=True)

        commands = []
        for spec in CAMERAS:
            device = getattr(args, spec["device_arg"])
            output_dir = session / spec["dir"]
            commands.append({
                "key": spec["id"],
                "log_name": f"{spec['dir']}.log",
                "argv": build_recorder_command(
                    sys.executable, recorder, spec, device, output_dir, args),
            })
            print(f"{spec['id']}: {device} -> {output_dir}", flush=True)

        try:
            children = start_children(commands, session)
        except BaseException as exc:
            print(f"Ошибка запуска рекордеров: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1

        output_root = Path(args.output_root).expanduser()
        supervisor = DualSupervisor(
            stop_flag,
            disk_check=lambda: (
                shutil.disk_usage(str(output_root)).free / preflight.MIB),
            critical_free_mib=args.critical_free_mib,
        )
        result = supervisor.run(children)
        exit_code = exit_code_for(result)

        for child in children:
            print(f"{child.key}: код {result['codes'].get(child.key)}; "
                  f"лог: {child.log_path}", flush=True)
        if result.get("stop_kind") == "disk":
            print(f"Stop reason: {result['stop_reason']}", flush=True)
        elif result["operator_stop"]:
            label = ("Остановка по сигналу" if result.get("stop_kind") == "signal"
                     else "Остановка оператором")
            print(f"{label}: {result['stop_reason']}", flush=True)
        if result["forced"]:
            print("Принудительное завершение процессов.", file=sys.stderr)
        if result["error"]:
            print(result["error"], file=sys.stderr)
        if exit_code == 0:
            print("Обе записи завершены и прошли проверку.", flush=True)
        else:
            print("Запись завершена с ошибкой; смотрите метаданные и логи.",
                  file=sys.stderr)
    except BaseException as exc:
        print(f"Ошибка wrapper: {type(exc).__name__}: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if lock_handle is not None:
            lock_handle.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
