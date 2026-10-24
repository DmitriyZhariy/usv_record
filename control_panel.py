#!/usr/bin/env python3
"""Веб-панель управления просмотром и записью двух камер.

Панель сама не захватывает видео и не пишет файлы:
- просмотр выполняет preview_dual_mjpeg.PreviewApp в этом же процессе;
- запись выполняет record_dual_session.py отдельным процессом;
- предпусковые проверки и контроль свободного места находятся в wrapper.

После загрузки панель запускается в состоянии idle: камеры не открываются,
запись не начинается. Состояние меняется только командами оператора.
"""

from __future__ import annotations

import argparse
import errno
import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import preview_dual_mjpeg as preview
from panel_page import PANEL_HTML

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8088
DEFAULT_OUTPUT_ROOT = "~/recordings"
DEFAULT_TOKEN_FILE = "~/.config/usv-panel/token"
DEFAULT_RECORD_LOCK_FILE = "~/.cache/usv_record/record_dual.lock"
DEFAULT_PANEL_LOCK_FILE = "~/.cache/usv_record/panel.lock"
DEFAULT_MIN_FREE_MIB = 4096
DEFAULT_CRITICAL_FREE_MIB = 1024
PREVIEW_JOIN_TIMEOUT = 5.0
READY_TIMEOUT = 30.0
PREVIEW_READY_TIMEOUT = 15.0
STATUS_INTERVAL = 1.0
DISK_INTERVAL = 5.0
STOP_WAIT_SECONDS = 110.0
MAX_REQUEST_BODY = 4096
MIB = 1024 * 1024

# Каталоги камер задаёт контракт wrapper (record_dual_session.CAMERAS).
CAMERA_DIRS = {"left": "cam-left", "right": "cam-right"}


class PanelError(RuntimeError):
    pass


class CommandError(PanelError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


@dataclass
class PanelConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    fps: float = preview.DEFAULT_FPS
    max_clients: int = preview.DEFAULT_MAX_CLIENTS
    fake_cameras: bool = False
    fake_capture_fps: float = 60.0
    device_left: str = "/dev/video2"
    device_right: str = "/dev/video4"
    output_root: str = DEFAULT_OUTPUT_ROOT
    record_seconds: float = 0.0
    segment_seconds: float = 10.0
    queue_frames: int = 120
    queue_mib: int = 32
    min_free_mib: int = DEFAULT_MIN_FREE_MIB
    critical_free_mib: int = DEFAULT_CRITICAL_FREE_MIB
    record_lock_file: str = DEFAULT_RECORD_LOCK_FILE
    panel_lock_file: str = DEFAULT_PANEL_LOCK_FILE
    token_file: str = DEFAULT_TOKEN_FILE
    dual_script: str = ""
    preview_join_timeout: float = PREVIEW_JOIN_TIMEOUT
    ready_timeout: float = READY_TIMEOUT
    preview_ready_timeout: float = PREVIEW_READY_TIMEOUT
    status_interval: float = STATUS_INTERVAL
    disk_interval: float = DISK_INTERVAL
    stop_wait_seconds: float = STOP_WAIT_SECONDS
    use_lock: bool = True


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_or_create_token(path):
    """Читает существующий токен или создаёт новый с правами 0600."""
    path = Path(path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise PanelError(
            f"не удалось создать каталог токена {path.parent}: "
            f"{type(exc).__name__}: {exc}")
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise PanelError(
            f"не удалось прочитать токен {path}: {type(exc).__name__}: {exc}")
    if existing:
        return existing

    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            raise PanelError(f"файл токена пуст: {path}")
        return text
    except OSError as exc:
        raise PanelError(
            f"не удалось создать токен {path}: {type(exc).__name__}: {exc}")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def probe_record_lock(path):
    """Проверяет, занят ли record-lock. Это диагностика, не mutual exclusion."""
    path = Path(path).expanduser()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+")
    except OSError as exc:
        return {"available": False, "busy": None,
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        import fcntl  # Linux/Unix only
    except ImportError:
        handle.close()
        return {"available": False, "busy": None, "error": None}
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return {"available": True, "busy": True, "error": None}
        return {"available": True, "busy": None,
                "error": f"{type(exc).__name__}: {exc}"}
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
    return {"available": True, "busy": False, "error": None}


def find_leftover_processes(proc_root="/proc"):
    """Диагностический поиск wrapper/рекордеров; ничего не завершает."""
    markers = ("record_dual_session.py", "record_camera_pyav_queue.py")
    found = []
    root = Path(proc_root)
    if not root.is_dir():
        return found
    try:
        entries = list(root.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        if command and any(marker in command for marker in markers):
            found.append({"pid": int(entry.name), "cmdline": command})
    return found


def read_metadata(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class SegmentTracker:
    """Кэширует закрытые сегменты и обновляет только текущий."""

    def __init__(self, directory, segment_seconds):
        self.directory = Path(directory)
        self.segment_seconds = segment_seconds
        self.known = {}
        self.current = None
        self.total = 0
        self.last_change = None
        self.warning_seconds = max(2.0 * segment_seconds, 30.0)
        self._lock = threading.Lock()

    def _stat(self, name):
        try:
            size = (self.directory / name).stat().st_size
        except OSError:
            return
        self.known[name] = size

    def scan(self, now):
        with self._lock:
            names = []
            try:
                with os.scandir(self.directory) as entries:
                    for entry in entries:
                        if entry.name.endswith(".mkv") and entry.is_file(
                                follow_symlinks=False):
                            names.append(entry.name)
            except OSError:
                return
            names.sort()
            if not names:
                return

            latest = names[-1]
            if self.current and latest != self.current:
                self._stat(self.current)
            for name in names:
                if name not in self.known:
                    self._stat(name)
            self.current = latest
            self._stat(latest)

            total = sum(self.known.values())
            if self.total != total or self.last_change is None:
                self.last_change = now
            self.total = total

    def rescan(self, now):
        with self._lock:
            names = []
            try:
                with os.scandir(self.directory) as entries:
                    for entry in entries:
                        if entry.name.endswith(".mkv") and entry.is_file(
                                follow_symlinks=False):
                            names.append(entry.name)
            except OSError:
                return
            for name in names:
                self._stat(name)
            self.current = max(names) if names else None
            total = sum(self.known.values())
            if self.total != total or self.last_change is None:
                self.last_change = now
            self.total = total

    def snapshot(self, now):
        with self._lock:
            age = None
            if self.last_change is not None:
                age = max(0.0, now - self.last_change)
            return {
                "segments": len(self.known),
                "bytes": self.total,
                "last_change_age_s": age,
                "warning_seconds": self.warning_seconds,
            }


class PreviewFacade:
    """Отдаёт PreviewHandler текущий PreviewApp через тот же интерфейс."""

    def __init__(self):
        self._lock = threading.Lock()
        self._app = None
        self.stop_event = threading.Event()

    def install(self, app):
        with self._lock:
            self._app = app
            self.stop_event = threading.Event()

    def begin_stop(self):
        self.stop_event.set()

    def clear(self):
        with self._lock:
            self._app = None
        self.stop_event.set()

    def current(self):
        with self._lock:
            return self._app

    @property
    def cameras(self):
        app = self.current()
        return app.cameras if app is not None else {}

    def acquire_client(self, key):
        app = self.current()
        return bool(app is not None and app.acquire_client(key))

    def release_client(self, key):
        app = self.current()
        if app is not None:
            app.release_client(key)


class SubprocessRecordRunner:
    """Обёртка wrapper-процесса; строки читаются отдельным потоком."""

    def __init__(self, argv, on_line, cwd=None):
        self.argv = [str(item) for item in argv]
        self.on_line = on_line
        self.cwd = cwd
        self.process = None
        self._reader = None
        self._closed = False

    def start(self):
        self.process = subprocess.Popen(
            self.argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=self.cwd,
            start_new_session=(os.name == "posix"),
        )
        self._reader = threading.Thread(
            target=self._read_lines, name="record-output", daemon=True)
        self._reader.start()

    def _read_lines(self):
        try:
            for line in self.process.stdout:
                self.on_line(line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.process.stdout.close()
            except (OSError, ValueError):
                pass

    def poll(self):
        return self.process.poll() if self.process is not None else None

    def finish_reading(self, timeout=2.0):
        """Дожидается разбора последних строк после выхода процесса."""
        if self._reader is not None:
            self._reader.join(timeout=timeout)

    def terminate(self):
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
            except (OSError, ProcessLookupError):
                pass

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.join(timeout=2.0)
        if self.process is not None and self.process.stdout is not None:
            try:
                self.process.stdout.close()
            except (OSError, ValueError):
                pass


class PanelController:
    def __init__(self, config, token, preview_factory=None,
                 runner_factory=None, probe_lock=None, find_leftovers=None,
                 disk_usage=None, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.token = token
        self.preview_factory = preview_factory or preview.PreviewApp
        self.runner_factory = runner_factory or SubprocessRecordRunner
        self.probe_lock = probe_lock or probe_record_lock
        self.find_leftovers = find_leftovers or find_leftover_processes
        self.disk_usage = disk_usage or shutil.disk_usage
        self.clock = clock
        self.sleep = sleep

        self.preview_facade = PreviewFacade()

        self._lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._finalize_lock = threading.Lock()
        self._shutdown = threading.Event()
        self._monitor = None

        self._state = "starting"
        self._message = "Инициализация"
        self._operation = None
        self._last_error = None
        self._conflicts = []

        self._preview_app = None
        self._stuck_preview = None
        self._stuck_preview_keys = []

        self._record_runner = None
        self._record_session = None
        self._record_session_path = None
        self._record_started = None
        self._record_exit = None
        self._stop_requested = False
        self._stop_reason = None
        self._forced = False
        self._wrapper_error = None
        self._readiness_failed = False
        self._result = None

        self._metadata = {}
        self._trackers = {}
        self._last_metadata_read = 0.0
        self._last_tracker_scan = 0.0
        self._last_disk_read = 0.0
        self._last_conflict_check = 0.0
        self._disk = {"free_mib": None, "error": None}
        self._tail = deque(maxlen=80)
        self._snapshot = {}

    # ---------- состояние и публикация ----------

    def _set_state(self, state, message=None, error=None):
        with self._lock:
            self._state = state
            if message is not None:
                self._message = message
            if error is not None:
                self._last_error = error
            elif state != "error":
                self._last_error = None

    def _fail(self, message, state="error"):
        print(f"panel: {message}", file=sys.stderr, flush=True)
        self._set_state(state, message, error=message)
        self._publish()

    def _preview_snapshot(self, now):
        app = self._preview_app
        if app is None:
            return {
                "active": False,
                "cameras": {},
                "clients_total": 0,
                "fake_cameras": self.config.fake_cameras,
            }
        try:
            cameras = {
                key: runtime.snapshot(now)
                for key, runtime in app.cameras.items()
            }
            clients = getattr(app, "clients_total", 0)
        except Exception:
            cameras, clients = {}, 0
        return {
            "active": self._state in ("preview", "starting", "stopping")
            or self._stuck_preview is not None,
            "cameras": cameras,
            "clients_total": clients,
            "fake_cameras": self.config.fake_cameras,
        }

    def _record_cameras_snapshot(self, now):
        cameras = {}
        for spec in preview.CAMERA_SPECS:
            key = spec["key"]
            meta = self._metadata.get(key) or {}
            tracker = self._trackers.get(key)
            file_info = tracker.snapshot(now) if tracker is not None else {
                "segments": 0, "bytes": 0, "last_change_age_s": None,
                "warning_seconds": max(2.0 * self.config.segment_seconds, 30.0),
            }
            status = meta.get("status")
            error = meta.get("capture_error") or meta.get("verification_error")
            if meta.get("queue_overflows"):
                error = error or (
                    f"переполнение очереди: {meta['queue_overflows']}")
            warning = None
            if (status == "recording"
                    and file_info["last_change_age_s"] is not None
                    and file_info["last_change_age_s"]
                    > file_info["warning_seconds"]):
                warning = (f"нет роста сегментов "
                           f"{file_info['last_change_age_s']:.0f} с")
            cameras[key] = {
                "label": spec["label"],
                "device": getattr(self.config, spec["device_arg"]),
                "status": status,
                "stop_reason": meta.get("stop_reason"),
                "error": error,
                "warning": warning,
                "segments": file_info["segments"],
                "bytes": file_info["bytes"],
                "last_change_age_s": file_info["last_change_age_s"],
            }
        return cameras

    def _build_snapshot(self):
        now = self.clock()
        with self._lock:
            state = self._state
            message = self._message
            operation = self._operation
            last_error = self._last_error
            conflicts = list(self._conflicts)
            session = self._record_session_path
            started = self._record_started
            exit_code = self._record_exit
            stop_reason = self._stop_reason
            forced = self._forced
            result = self._result
            runner = self._record_runner
            preview_info = self._preview_snapshot(now)
            cameras = self._record_cameras_snapshot(now)
            disk = dict(self._disk)
            tail = list(self._tail)
            stuck = self._stuck_preview is not None

        active = runner is not None and exit_code is None
        if active and started is not None:
            elapsed = max(0.0, now - started)
        elif result is not None:
            elapsed = result.get("duration_s")
        else:
            elapsed = None

        return {
            "app": "usv-panel",
            "state": state,
            "operation": operation,
            "message": message,
            "last_error": last_error,
            "conflicts": conflicts,
            "preview": preview_info,
            "recording": {
                "active": active,
                "session": str(session) if session else None,
                "elapsed_s": round(elapsed, 1) if elapsed is not None else None,
                "stop_reason": stop_reason,
                "forced": bool(forced),
                "exit_code": exit_code,
                "cameras": cameras,
            },
            "disk": disk,
            "limits": {
                "record_seconds": self.config.record_seconds,
                "segment_seconds": self.config.segment_seconds,
                "min_free_mib": self.config.min_free_mib,
                "critical_free_mib": self.config.critical_free_mib,
            },
            "result": result,
            "stuck_preview": stuck,
            "log_tail": tail,
            "note": (
                "Прогресс строится по метаданным и размерам MKV; "
                "frames.csv не читается."
            ),
        }

    def _publish(self):
        with self._publish_lock:
            snapshot = self._build_snapshot()
            with self._lock:
                self._snapshot = snapshot

    def status(self):
        with self._lock:
            return self._snapshot

    # ---------- служебные проверки ----------

    def _detect_conflicts(self):
        conflicts = []
        with self._lock:
            runner_active = (
                self._record_runner is not None and self._record_exit is None)
        if runner_active:
            conflicts.append("wrapper записи ещё работает")
        probe = self.probe_lock(self.config.record_lock_file)
        if probe.get("busy"):
            conflicts.append(
                "record-lock занят: возможно, работает wrapper записи")
        if probe.get("error"):
            conflicts.append(f"record-lock: {probe['error']}")
        for item in self.find_leftovers():
            conflicts.append(
                f"найден процесс {item['pid']}: {item['cmdline']}")
        return conflicts

    def _stuck_preview_still_alive(self):
        app = self._stuck_preview
        if app is None:
            return False
        keys = self._stuck_preview_keys
        threads = getattr(app, "_threads", {})
        if threads:
            return any(
                key in keys and thread.is_alive()
                for key, thread in threads.items())
        return bool(keys)

    def _clear_finished_stuck(self):
        with self._lock:
            app = self._stuck_preview
            self._stuck_preview = None
            self._stuck_preview_keys = []
            if app is not None and self._preview_app is app:
                self._preview_app = None
            if app is not None:
                self.preview_facade.clear()

    def _stop_preview_app(self):
        with self._lock:
            app = self._preview_app
        if app is None:
            return []
        self.preview_facade.begin_stop()
        try:
            alive = app.stop(join_timeout=self.config.preview_join_timeout)
        except Exception as exc:
            alive = ["unknown"]
            print(f"panel: ошибка остановки просмотра: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        with self._lock:
            if alive:
                self._stuck_preview = app
                self._stuck_preview_keys = list(alive)
                print("panel: потоки просмотра не завершились: "
                      + ", ".join(alive), file=sys.stderr, flush=True)
            else:
                self.preview_facade.clear()
                if self._preview_app is app:
                    self._preview_app = None
        return list(alive)

    # ---------- команды ----------

    def _guard_no_stuck(self):
        if self._stuck_preview_still_alive():
            raise PanelError(
                "потоки просмотра не завершились; новый захват запрещён. "
                "Перезапустите службу панели.")
        if self._stuck_preview is not None:
            self._clear_finished_stuck()

    def handle_command(self, command):
        with self._lock:
            if self._shutdown.is_set():
                raise CommandError(503, "панель завершает работу")
            state = self._state
            if command == "preview/start":
                if state != "idle":
                    raise CommandError(409, f"недоступно в состоянии {state}")
                self._state = "starting"
                self._operation = "preview"
                self._message = "Запуск просмотра обеих камер…"
                self._last_error = None
                action = self._run_preview_start
            elif command == "preview/stop":
                if state != "preview":
                    raise CommandError(409, f"недоступно в состоянии {state}")
                self._state = "stopping"
                self._operation = "preview"
                self._message = "Остановка просмотра…"
                action = self._run_preview_stop
            elif command == "record/start":
                if state not in ("idle", "preview"):
                    raise CommandError(409, f"недоступно в состоянии {state}")
                self._state = "starting"
                self._operation = "record"
                self._message = "Подготовка записи обеих камер…"
                self._last_error = None
                self._stop_requested = False
                self._stop_reason = None
                self._forced = False
                self._readiness_failed = False
                self._result = None
                self._record_exit = None
                self._record_started = None
                action = self._run_record_start
            elif command == "record/stop":
                if state not in ("starting", "recording"):
                    if state == "stopping":
                        return {"ok": True, "message": "остановка уже идёт"}
                    raise CommandError(409, f"недоступно в состоянии {state}")
                self._stop_requested = True
                if self._stop_reason is None:
                    self._stop_reason = "остановка оператором"
                if state == "starting":
                    self._message = "Остановка запрошена…"
                    runner = self._record_runner
                    action = None
                else:
                    self._state = "stopping"
                    self._operation = "record"
                    self._message = "Остановка и проверка…"
                    runner = self._record_runner
                    action = None
            elif command == "error/ack":
                if state != "error":
                    return {"ok": True, "message": "ошибки нет"}
                self._state = "starting"
                self._operation = "ack"
                self._message = "Проверка занятости…"
                runner = None
                action = self._run_ack
            else:
                raise CommandError(404, f"неизвестная команда: {command}")

        self._publish()
        if command == "record/stop" and runner is not None:
            runner.terminate()
        if action is not None:
            thread = threading.Thread(
                target=self._run_worker, args=(action,),
                name=f"panel-{command}", daemon=True)
            thread.start()
        return {"ok": True, "state": self._state}

    def _run_worker(self, action):
        try:
            action()
        except PanelError as exc:
            self._abort_after_worker_error()
            self._fail(str(exc))
        except Exception as exc:
            traceback.print_exc()
            self._abort_after_worker_error()
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            self._publish()

    def _abort_after_worker_error(self):
        with self._lock:
            runner = self._record_runner
            active = runner is not None and self._record_exit is None
            if active:
                self._readiness_failed = True
                self._state = "stopping"
                self._operation = "record"
                self._message = "Ошибка запуска; остановка wrapper…"
        if active:
            runner.terminate()

    # ---------- просмотр ----------

    def _run_preview_start(self):
        self._guard_no_stuck()
        conflicts = self._detect_conflicts()
        if conflicts:
            with self._lock:
                self._conflicts = conflicts
            raise PanelError("занятость: " + "; ".join(conflicts))
        with self._lock:
            if self._shutdown.is_set():
                raise PanelError("панель завершает работу")
            if self._record_runner is not None and self._record_exit is None:
                raise PanelError("запись ещё работает")
            self._conflicts = []

        app = self.preview_factory(
            devices={"left": self.config.device_left,
                     "right": self.config.device_right},
            fps=self.config.fps,
            max_clients=self.config.max_clients,
            fake_cameras=self.config.fake_cameras,
            fake_capture_fps=self.config.fake_capture_fps,
        )
        with self._lock:
            if self._shutdown.is_set():
                raise PanelError("панель завершает работу")
            self._preview_app = app
            self.preview_facade.install(app)
        try:
            app.start()
        except BaseException:
            self._stop_preview_app()
            raise
        if self._shutdown.is_set():
            self._stop_preview_app()
            raise PanelError("панель завершает работу")

        deadline = self.clock() + self.config.preview_ready_timeout
        while self.clock() < deadline:
            if self._shutdown.is_set():
                raise PanelError("панель завершает работу")
            cameras = app.cameras
            errors = []
            for spec in preview.CAMERA_SPECS:
                runtime = cameras.get(spec["key"])
                if runtime is None:
                    errors.append(f"{spec['label']}: нет состояния")
                elif runtime.error:
                    errors.append(f"{spec['label']}: {runtime.error}")
                elif runtime.state == "error":
                    errors.append(f"{spec['label']}: ошибка камеры")
            if errors:
                self._stop_preview_app()
                raise PanelError("просмотр не запущен: " + "; ".join(errors))
            if all(
                    cameras[spec["key"]].state == "running"
                    and cameras[spec["key"]].published_frames > 0
                    for spec in preview.CAMERA_SPECS):
                with self._lock:
                    self._state = "preview"
                    self._operation = "preview"
                    self._message = "Просмотр обеих камер работает"
                return
            self.sleep(0.1)

        self._stop_preview_app()
        raise PanelError(
            "камеры не подтвердили готовность просмотра за "
            f"{self.config.preview_ready_timeout:g} с")

    def _run_preview_stop(self):
        alive = self._stop_preview_app()
        if alive:
            raise PanelError(
                "потоки просмотра не завершились: " + ", ".join(alive)
                + "; новый захват запрещён до перезапуска службы")
        with self._lock:
            self._state = "idle"
            self._operation = None
            self._message = "Просмотр остановлен"

    # ---------- запись ----------

    def _build_record_argv(self):
        script = self.config.dual_script
        if not script:
            script = str(Path(__file__).resolve().with_name(
                "record_dual_session.py"))
        return [
            sys.executable, str(Path(script).resolve()),
            "--output-root", str(Path(self.config.output_root).expanduser()),
            "--device-left", self.config.device_left,
            "--device-right", self.config.device_right,
            "--seconds", f"{self.config.record_seconds:g}",
            "--segment-seconds", f"{self.config.segment_seconds:g}",
            "--queue-frames", str(self.config.queue_frames),
            "--queue-mib", str(self.config.queue_mib),
            "--min-free-mib", str(self.config.min_free_mib),
            "--critical-free-mib", str(self.config.critical_free_mib),
            "--lock-file", str(Path(self.config.record_lock_file).expanduser()),
        ]

    def _on_record_line(self, line):
        with self._lock:
            self._tail.append(line)
        print(f"wrapper: {line}", flush=True)
        if line.startswith("Output: "):
            path = Path(line[len("Output: "):].strip()).expanduser()
            with self._lock:
                self._record_session_path = path
                self._ensure_trackers(path)
        elif line.startswith("Stop reason: "):
            with self._lock:
                self._stop_reason = line[len("Stop reason: "):].strip()
        elif line.startswith("Остановка оператором:"):
            with self._lock:
                if self._stop_reason is None:
                    self._stop_reason = "остановка оператором"
        elif line.startswith("Остановка по сигналу:"):
            with self._lock:
                if self._stop_reason is None:
                    self._stop_reason = "остановка по сигналу"
        if "Принудительное завершение процессов" in line:
            with self._lock:
                self._forced = True
        if "Preflight failed:" in line:
            with self._lock:
                self._wrapper_error = line.strip()

    def _ensure_trackers(self, session):
        if not session:
            return
        for spec in preview.CAMERA_SPECS:
            key = spec["key"]
            if key not in self._trackers:
                self._trackers[key] = SegmentTracker(
                    session / CAMERA_DIRS[key], self.config.segment_seconds)

    def _read_record_metadata(self):
        session = self._record_session_path
        if not session:
            return {}
        metadata = {}
        for spec in preview.CAMERA_SPECS:
            path = session / CAMERA_DIRS[spec["key"]] / "session_metadata.json"
            data = read_metadata(path)
            if data is not None:
                metadata[spec["key"]] = data
        return metadata

    def _readiness_reached(self):
        with self._lock:
            session = self._record_session_path
        if not session:
            return False
        metadata = self._read_record_metadata()
        if len(metadata) != 2:
            return False
        if any(meta.get("status") != "recording"
               for meta in metadata.values()):
            return False
        for spec in preview.CAMERA_SPECS:
            directory = session / CAMERA_DIRS[spec["key"]]
            try:
                with os.scandir(directory) as entries:
                    has_segment = any(
                        entry.name.endswith(".mkv") and entry.is_file()
                        and entry.stat().st_size > 0
                        for entry in entries
                    )
            except OSError:
                has_segment = False
            if not has_segment:
                return False
        return True

    def _run_record_start(self):
        self._guard_no_stuck()
        conflicts = self._detect_conflicts()
        if conflicts:
            with self._lock:
                self._conflicts = conflicts
            raise PanelError("занятость: " + "; ".join(conflicts))
        with self._lock:
            self._conflicts = []

        with self._lock:
            preview_app = self._preview_app
        if preview_app is not None:
            alive = self._stop_preview_app()
            if alive:
                raise PanelError(
                    "просмотр не остановлен, запись не запущена: "
                    + ", ".join(alive))

        with self._lock:
            if self._stop_requested:
                self._state = "idle"
                self._operation = None
                self._message = "Запуск отменён"
                self._stop_requested = False
                return

        argv = self._build_record_argv()
        runner = self.runner_factory(argv, self._on_record_line)
        with self._lock:
            if self._shutdown.is_set() or self._stop_requested:
                self._record_runner = None
                self._state = "idle"
                self._operation = None
                self._message = ("Запуск отменён: панель завершает работу"
                                 if self._shutdown.is_set()
                                 else "Запуск отменён")
                self._stop_requested = False
                return
            self._record_runner = runner
            self._record_exit = None
            self._record_session = None
            self._record_session_path = None
            self._record_started = None
            self._metadata = {}
            self._trackers = {}
            self._tail.clear()
            self._forced = False
            self._wrapper_error = None
            self._stop_reason = None if not self._stop_requested else (
                "остановка оператором")
        try:
            runner.start()
        except Exception as exc:
            with self._lock:
                self._record_runner = None
            raise PanelError(
                f"не удалось запустить wrapper: {type(exc).__name__}: {exc}")

        deadline = self.clock() + self.config.ready_timeout
        while self.clock() < deadline:
            code = runner.poll()
            if code is not None:
                with self._lock:
                    self._readiness_failed = True
                self._finalize_record(code)
                with self._lock:
                    message = self._last_error or (
                        f"wrapper завершился до готовности (код {code})")
                raise PanelError(message)
            with self._lock:
                stop_now = self._stop_requested
                if stop_now:
                    self._state = "stopping"
                    self._operation = "record"
                    self._message = "Остановка и проверка…"
            if stop_now:
                runner.terminate()
                return
            if self._readiness_reached():
                with self._lock:
                    self._state = "recording"
                    self._operation = "record"
                    self._message = "Запись обеих камер идёт"
                    self._record_started = self.clock()
                    self._ensure_trackers(self._record_session_path)
                    self._metadata = self._read_record_metadata()
                return
            self.sleep(0.2)

        with self._lock:
            self._readiness_failed = True
            self._state = "stopping"
            self._operation = "record"
            self._message = "Готовность не подтверждена; остановка wrapper…"
            self._stop_reason = self._stop_reason or "готовность не подтверждена"
        runner.terminate()

    def _refresh_metadata(self, now):
        with self._lock:
            session = self._record_session_path
            active = (self._record_runner is not None
                      and self._record_exit is None)
        if not session or not active:
            return
        if now - self._last_metadata_read < 2.0:
            return
        self._last_metadata_read = now
        metadata = self._read_record_metadata()
        if metadata:
            with self._lock:
                self._metadata.update(metadata)

    def _refresh_trackers(self, now):
        with self._lock:
            session = self._record_session_path
            trackers = dict(self._trackers)
            active = (self._record_runner is not None
                      and self._record_exit is None)
        if not session or not trackers or not active:
            return
        if now - self._last_tracker_scan < 2.0:
            return
        self._last_tracker_scan = now
        for tracker in trackers.values():
            tracker.scan(now)

    def _refresh_disk(self, now):
        if now - self._last_disk_read < self.config.disk_interval:
            return
        self._last_disk_read = now
        try:
            stats = self.disk_usage(
                str(Path(self.config.output_root).expanduser()))
            self._disk = {
                "free_mib": round(stats.free / MIB, 1),
                "error": None,
            }
        except Exception as exc:
            with self._lock:
                free_mib = self._disk.get("free_mib")
            self._disk = {
                "free_mib": free_mib,
                "error": f"{type(exc).__name__}: {exc}",
            }

    def _finalize_record(self, code):
        with self._finalize_lock:
            with self._lock:
                runner = self._record_runner
            if runner is not None:
                finish_reading = getattr(runner, "finish_reading", None)
                if finish_reading is not None:
                    finish_reading()
            with self._lock:
                if self._record_exit is not None:
                    return
                session = self._record_session_path
                readiness_failed = self._readiness_failed
                stop_reason = self._stop_reason
                forced = self._forced
                wrapper_error = self._wrapper_error
                started = self._record_started

            now = self.clock()
            metadata = self._read_record_metadata()
            with self._lock:
                self._metadata.update(metadata)
                self._ensure_trackers(session)
                trackers = dict(self._trackers)
            for tracker in trackers.values():
                tracker.rescan(now)

            verification = {}
            for spec in preview.CAMERA_SPECS:
                meta = metadata.get(spec["key"]) or {}
                verification[spec["key"]] = meta.get("status") or "missing"
            verified = all(
                status == "verified" for status in verification.values())

            if stop_reason is None:
                reasons = [
                    (metadata.get(spec["key"]) or {}).get("stop_reason")
                    for spec in preview.CAMERA_SPECS
                ]
                if reasons and all(reason == "duration_limit"
                                   for reason in reasons):
                    stop_reason = "duration_limit"
                elif any(reason == "user_stop" for reason in reasons):
                    stop_reason = "остановка оператором"

            outcome = "ok"
            error = None
            if readiness_failed:
                outcome = "error"
                error = wrapper_error or "готовность записи не подтверждена"
            elif forced:
                outcome = "error"
                error = "wrapper принудительно завершил процессы"
            elif code != 0:
                outcome = "error"
                error = wrapper_error or f"wrapper завершился с кодом {code}"
            elif not verified:
                outcome = "error"
                error = ("проверка файлов не подтверждена: "
                         + ", ".join(f"{key}={value}"
                                     for key, value in verification.items()))

            finished = self.clock()
            duration = None
            if started is not None:
                duration = max(0.0, finished - started)
            result = {
                "outcome": outcome,
                "verification": verification,
                "stop_reason": stop_reason,
                "forced": bool(forced),
                "exit_code": code,
                "session": str(session) if session else None,
                "error": error,
                "duration_s": round(duration, 1) if duration is not None else None,
                "finished_at_utc": _now_iso(),
            }
            with self._lock:
                self._record_exit = code
                self._result = result
                if outcome == "ok":
                    self._state = "idle"
                    self._operation = None
                    prefix = (f"Остановлено: {stop_reason}; "
                              if stop_reason else "Запись завершена; ")
                    self._message = prefix + "проверка файлов: verified"
                    self._last_error = None
                else:
                    self._state = "error"
                    self._operation = None
                    self._message = error
                    self._last_error = error

    def _run_ack(self):
        if self._stuck_preview_still_alive():
            raise PanelError(
                "потоки просмотра всё ещё живы; сброс ошибки невозможен. "
                "Перезапустите службу панели.")
        if self._stuck_preview is not None:
            self._clear_finished_stuck()
        conflicts = self._detect_conflicts()
        if conflicts:
            with self._lock:
                self._conflicts = conflicts
            raise PanelError("занятость не устранена: " + "; ".join(conflicts))
        with self._lock:
            self._conflicts = []
            self._state = "idle"
            self._operation = None
            self._message = "Ошибка сброшена; панель ожидает команды"
            self._last_error = None

    # ---------- монитор и завершение ----------

    def _monitor_loop(self):
        while not self._shutdown.wait(self.config.status_interval):
            try:
                now = self.clock()
                self._refresh_metadata(now)
                self._refresh_trackers(now)
                self._refresh_disk(now)
                with self._lock:
                    runner = self._record_runner
                    exit_code = self._record_exit
                if runner is not None and exit_code is None:
                    code = runner.poll()
                    if code is not None:
                        self._finalize_record(code)
                with self._lock:
                    state = self._state
                if state == "idle" and now - self._last_conflict_check >= 5.0:
                    self._last_conflict_check = now
                    conflicts = self._detect_conflicts()
                    if conflicts:
                        with self._lock:
                            self._conflicts = conflicts
                            self._state = "error"
                            self._operation = None
                            self._message = (
                                "Обнаружена занятость: "
                                + "; ".join(conflicts)
                                + "; новые просмотр и запись запрещены")
                            self._last_error = "; ".join(conflicts)
                self._publish()
            except Exception as exc:
                print(f"panel: ошибка монитора: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                with self._lock:
                    self._last_error = f"монитор: {type(exc).__name__}: {exc}"
                self._publish()

    def initialize(self):
        conflicts = self._detect_conflicts()
        with self._lock:
            self._conflicts = conflicts
            if conflicts:
                self._state = "error"
                self._operation = None
                self._message = ("Обнаружена занятость: "
                                 + "; ".join(conflicts)
                                 + "; новые просмотр и запись запрещены")
                self._last_error = "; ".join(conflicts)
            else:
                self._state = "idle"
                self._operation = None
                self._message = "Ожидание команды оператора"
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="panel-monitor", daemon=True)
        self._monitor.start()
        self._publish()

    def shutdown(self, reason="остановка панели"):
        """Останавливает просмотр и штатно ждёт wrapper до общего бюджета."""
        success = True

        with self._lock:
            self._shutdown.set()
            self._message = "Завершение панели: остановка просмотра"
            self._operation = "shutdown"
        self._publish()

        alive = self._stop_preview_app()
        if alive:
            success = False
            self._set_state(
                "error",
                "Потоки просмотра не завершились при остановке панели",
                error="потоки просмотра не завершились: " + ", ".join(alive))

        with self._lock:
            runner = self._record_runner
            exit_code = self._record_exit
            self._stop_requested = True
            if runner is not None and exit_code is None:
                self._state = "stopping"
                self._operation = "shutdown"
                self._message = "Остановка записи и проверка…"
                self._stop_reason = self._stop_reason or reason
                stop_reason = self._stop_reason
            else:
                stop_reason = None
        self._publish()

        if runner is not None and exit_code is None:
            print(f"panel: {reason}: отправляю SIGTERM wrapper и жду",
                  flush=True)
            runner.terminate()
            deadline = self.clock() + self.config.stop_wait_seconds
            while self.clock() < deadline:
                code = runner.poll()
                if code is not None:
                    self._finalize_record(code)
                    break
                self.sleep(0.1)
            else:
                code = runner.poll()
                if code is not None:
                    self._finalize_record(code)
                else:
                    success = False
                    message = (
                        f"wrapper не завершился за "
                        f"{self.config.stop_wait_seconds:g} с; "
                        "принудительное завершение панелью не выполнялось. "
                        "Проверьте журнал и завершите службу.")
                    with self._lock:
                        self._state = "error"
                        self._operation = "shutdown"
                        self._message = message
                        self._last_error = message
                        self._result = {
                            "outcome": "timeout",
                            "verification": {},
                            "stop_reason": stop_reason,
                            "forced": False,
                            "exit_code": None,
                            "session": (str(self._record_session_path)
                                        if self._record_session_path else None),
                            "error": message,
                            "duration_s": None,
                            "finished_at_utc": _now_iso(),
                        }
                    self._publish()

            with self._lock:
                result = self._result
            if result is not None and result.get("outcome") != "ok":
                success = False

        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        if runner is not None:
            runner.close()
        self._publish()
        return success

    def close(self):
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        if self._record_runner is not None:
            self._record_runner.close()


class PanelHTTPServer(preview.PreviewHTTPServer):
    def __init__(self, address, panel):
        self.panel = panel
        self.app = panel.preview_facade
        ThreadingHTTPServer.__init__(self, address, PanelHandler)


class PanelHandler(preview.PreviewHandler):
    server_version = "usv-panel/1"

    def _token_ok(self):
        supplied = self.headers.get("X-USV-Token")
        if supplied is None:
            return False
        try:
            return hmac.compare_digest(
                supplied.encode("utf-8"),
                self.server.panel.token.encode("utf-8"))
        except (UnicodeError, TypeError):
            return False

    def _require_token(self):
        if not self._token_ok():
            self._send_json(401, {"ok": False, "message": "требуется токен"})
            return False
        return True

    def _check_origin(self):
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host", "")
            allowed = {"http://" + host, "https://" + host}
            if origin not in allowed:
                self._send_json(403, {"ok": False,
                                      "message": "недопустимый Origin"})
                return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._send_json(403, {"ok": False,
                                  "message": "cross-site запрос запрещён"})
            return False
        return True

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_body(code, "application/json; charset=utf-8", body)

    def _send_panel_html(self):
        cameras = [
            {"key": spec["key"], "label": spec["label"],
             "device": getattr(self.server.panel.config, spec["device_arg"])}
            for spec in preview.CAMERA_SPECS
        ]
        body = PANEL_HTML.replace(
            "__CAMERAS_JSON__", json.dumps(cameras, ensure_ascii=False))
        self._send_body(200, "text/html; charset=utf-8", body.encode("utf-8"))

    def _send_panel_status(self):
        status = self.server.panel.status()
        body = json.dumps(status, ensure_ascii=False).encode("utf-8")
        self._send_body(200, "application/json; charset=utf-8", body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._send_panel_html()
        if path == "/api/status":
            if not self._require_token():
                return
            return self._send_panel_status()
        if (path.startswith("/stream/") and path.endswith(".mjpg")) or \
                path == "/diagnostics/frame.jpg":
            if not self._require_token():
                return
            with self.server.panel._lock:
                state = self.server.panel._state
                app = self.server.panel._preview_app
            if state != "preview" or app is None:
                return self._send_body(
                    503, "text/plain; charset=utf-8",
                    b"preview is not running")
            return super().do_GET()
        self._send_body(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._send_json(404, {"ok": False, "message": "not found"})
        if not self._require_token():
            return
        if not self._check_origin():
            return
        if self.headers.get("Transfer-Encoding"):
            return self._send_json(
                400, {"ok": False, "message": "chunked body не поддерживается"})
        if self.headers.get_content_type() != "application/json":
            return self._send_json(
                400, {"ok": False, "message": "требуется application/json"})
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return self._send_json(400, {"ok": False, "message": "bad length"})
        if length < 0 or length > MAX_REQUEST_BODY:
            return self._send_json(
                413, {"ok": False, "message": "слишком большое тело запроса"})
        raw = self.rfile.read(length) if length else b""
        if raw.strip():
            try:
                body = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return self._send_json(
                    400, {"ok": False, "message": "некорректный JSON"})
            if not isinstance(body, dict):
                return self._send_json(
                    400, {"ok": False, "message": "ожидался JSON-объект"})

        command = path[len("/api/"):]
        try:
            result = self.server.panel.handle_command(command)
        except CommandError as exc:
            return self._send_json(
                exc.status, {"ok": False, "message": str(exc)})
        except Exception as exc:
            return self._send_json(
                500, {"ok": False,
                      "message": f"{type(exc).__name__}: {exc}"})
        payload = {"ok": True, "state": self.server.panel.status().get("state")}
        payload.update(result)
        return self._send_json(202, payload)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Веб-панель просмотра и записи двух камер.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--fps", type=float, default=preview.DEFAULT_FPS)
    parser.add_argument("--max-clients", type=int,
                        default=preview.DEFAULT_MAX_CLIENTS)
    parser.add_argument("--device-left", default="/dev/video2")
    parser.add_argument("--device-right", default="/dev/video4")
    parser.add_argument("--fake-cameras", action="store_true",
                        help="тестовые JPEG без обращения к устройствам")
    parser.add_argument("--fake-capture-fps", type=float, default=60.0)
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--record-seconds", type=float, default=0.0,
                        help="0 — запись до остановки оператором")
    parser.add_argument("--segment-seconds", type=float, default=10.0)
    parser.add_argument("--queue-frames", type=int, default=120)
    parser.add_argument("--queue-mib", type=int, default=32)
    parser.add_argument("--min-free-mib", type=int,
                        default=DEFAULT_MIN_FREE_MIB)
    parser.add_argument("--critical-free-mib", type=int,
                        default=DEFAULT_CRITICAL_FREE_MIB)
    parser.add_argument("--record-lock-file", default=DEFAULT_RECORD_LOCK_FILE)
    parser.add_argument("--panel-lock-file", default=DEFAULT_PANEL_LOCK_FILE)
    parser.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--dual-script", default="",
                        help="путь к record_dual_session.py (по умолчанию рядом)")
    parser.add_argument("--preview-ready-timeout", type=float,
                        default=PREVIEW_READY_TIMEOUT)
    parser.add_argument("--ready-timeout", type=float, default=READY_TIMEOUT)
    parser.add_argument("--status-interval", type=float,
                        default=STATUS_INTERVAL)
    parser.add_argument("--disk-interval", type=float, default=DISK_INTERVAL)
    parser.add_argument("--stop-wait-seconds", type=float,
                        default=STOP_WAIT_SECONDS)
    parser.add_argument("--no-lock", action="store_true",
                        help="не брать lock второго экземпляра панели")
    return parser


def config_from_args(args):
    return PanelConfig(
        host=args.host,
        port=args.port,
        fps=args.fps,
        max_clients=args.max_clients,
        fake_cameras=args.fake_cameras,
        fake_capture_fps=args.fake_capture_fps,
        device_left=args.device_left,
        device_right=args.device_right,
        output_root=args.output_root,
        record_seconds=args.record_seconds,
        segment_seconds=args.segment_seconds,
        queue_frames=args.queue_frames,
        queue_mib=args.queue_mib,
        min_free_mib=args.min_free_mib,
        critical_free_mib=args.critical_free_mib,
        record_lock_file=args.record_lock_file,
        panel_lock_file=args.panel_lock_file,
        token_file=args.token_file,
        dual_script=args.dual_script,
        preview_ready_timeout=args.preview_ready_timeout,
        ready_timeout=args.ready_timeout,
        status_interval=args.status_interval,
        disk_interval=args.disk_interval,
        stop_wait_seconds=args.stop_wait_seconds,
        use_lock=not args.no_lock,
    )


def validate_config(config):
    if not (0 <= config.port <= 65535):
        raise PanelError("--port вне диапазона 0..65535")
    if config.fps <= 0 or config.fake_capture_fps <= 0:
        raise PanelError("частоты должны быть > 0")
    if config.max_clients <= 0:
        raise PanelError("--max-clients должен быть > 0")
    if config.record_seconds < 0:
        raise PanelError("--record-seconds должен быть >= 0")
    if min(config.segment_seconds, config.queue_frames, config.queue_mib) <= 0:
        raise PanelError("segment-seconds, queue-frames и queue-mib должны быть > 0")
    if (config.min_free_mib <= 0 or config.critical_free_mib <= 0
            or config.critical_free_mib >= config.min_free_mib):
        raise PanelError("требуется 0 < --critical-free-mib < --min-free-mib")
    for name in ("preview_ready_timeout", "ready_timeout", "status_interval",
                 "disk_interval", "stop_wait_seconds"):
        if getattr(config, name) <= 0:
            raise PanelError(f"{name} должен быть > 0")


def main(argv=None):
    args = build_parser().parse_args(argv)
    config = config_from_args(args)
    try:
        validate_config(config)
        token = load_or_create_token(config.token_file)
    except PanelError as exc:
        print(f"panel: {exc}", file=sys.stderr)
        return 2

    lock_handle = None
    if config.use_lock:
        try:
            lock_handle, lock_status = preview.acquire_preview_lock(
                config.panel_lock_file)
        except OSError as exc:
            print(f"lock панели недоступен: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 1
        if lock_handle is None:
            if lock_status == "busy":
                print("Другой экземпляр панели уже работает "
                      f"(lock: {config.panel_lock_file}).", file=sys.stderr)
            else:
                print(f"lock панели недоступен: {lock_status}",
                      file=sys.stderr)
            return 1

    controller = PanelController(config, token)
    server = None
    server_thread = None
    exit_code = 0
    try:
        controller.initialize()
        try:
            server = PanelHTTPServer((config.host, config.port), controller)
        except OSError as exc:
            print(f"Не удалось занять {config.host}:{config.port}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            controller.shutdown("ошибка запуска HTTP")
            return 1
        server_thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="panel-http", daemon=True)
        server_thread.start()

        host, port = server.server_address[0], server.server_address[1]
        print("Панель управления камерами запущена.", flush=True)
        print(f"Страница: http://{host}:{port}/  "
              f"(с ноутбука/телефона: http://<IP-Repka>:{port}/)", flush=True)
        print(f"Токен: файл {Path(config.token_file).expanduser()} "
              "(значение не выводится)", flush=True)
        print(f"Камеры: cam_left={config.device_left}, "
              f"cam_right={config.device_right}", flush=True)
        print("Запись не начинается автоматически.", flush=True)
        if config.fake_cameras:
            print("РЕЖИМ ТЕСТОВЫХ КАДРОВ: устройства не открываются.",
                  flush=True)

        shutdown_event = threading.Event()

        def handle_signal(signum, frame):
            shutdown_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        while not shutdown_event.wait(0.2):
            pass
        print("Панель останавливается: просмотр и активная запись "
              "завершаются штатно.", flush=True)
        if not controller.shutdown("остановка панели"):
            exit_code = 1
    finally:
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if server_thread is not None:
            server_thread.join(timeout=2.0)
        controller.close()
        if lock_handle is not None:
            lock_handle.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
