"""Последний JPEG из рекордера: отдельная очередь просмотра, без PyAV/V4L2.

В Linux panel создаёт приватный каталог в /dev/shm. Файлы атомарно
заменяются и имеют ограниченный размер. Ошибки этого канала не меняют
результат записи. Предпросмотр допускает пропуск кадров; MKV/CSV — нет.
"""
from __future__ import annotations
import math
import os
import shutil
import struct
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

FPS = 2.0
MAX_JPEG = 2 * 1024 * 1024
HEADER = struct.Struct("!8sQQ")
MAGIC = b"USVP0001"
CAMERAS = {"left": "cam_left", "right": "cam_right"}
STALE_SECONDS = 3.0

@dataclass(frozen=True)
class Frame:
    seq: int
    mono_ns: int
    payload: bytes


def frame_path(root, camera_id):
    if camera_id not in CAMERAS.values():
        raise ValueError("unknown camera")
    return Path(root) / (camera_id + ".frame")


def decode_frame(data):
    if not HEADER.size + 4 <= len(data) <= HEADER.size + MAX_JPEG:
        raise ValueError("invalid preview size")
    magic, seq, mono = HEADER.unpack(data[:HEADER.size])
    payload = data[HEADER.size:]
    if magic != MAGIC or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise ValueError("invalid preview JPEG/header")
    return Frame(seq, mono, payload)


class SnapshotPublisher:
    def __init__(self, root, camera_id, fps=FPS, clock=time.monotonic):
        if not math.isfinite(fps) or not 0 < fps <= FPS:
            raise ValueError("preview FPS must be in (0, 2]")
        self.path = frame_path(root, camera_id)
        self.clock = clock
        self.interval = 1.0 / fps
        self._next = 0.0
        self._lock = threading.Lock()
        self._pending = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.error = None
        self.frames_published = 0
        self.frames_skipped = 0
        self._thread = threading.Thread(target=self._run, name="record-preview-writer", daemon=True)

    def start(self):
        if not self.path.parent.is_dir():
            raise OSError("preview directory does not exist")
        self._thread.start()

    def offer(self, payload, mono_ns, seq):
        """Не ждёт I/O/браузер. При занятости теряется только preview-копия."""
        if self._stop.is_set() or self.error is not None:
            return False
        now = self.clock()
        if now < self._next:
            return False
        if not 4 <= len(payload) <= MAX_JPEG or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
            self.frames_skipped += 1
            return False
        if not self._lock.acquire(blocking=False):
            self.frames_skipped += 1
            return False
        try:
            if self._pending is not None:
                self.frames_skipped += 1
            self._pending = Frame(seq, mono_ns, payload)
            self._next = now + self.interval
        finally:
            self._lock.release()
        self._wake.set()
        return True

    def _write(self, frame):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("wb") as handle:
            handle.write(HEADER.pack(MAGIC, frame.seq, frame.mono_ns))
            handle.write(frame.payload)
        os.replace(temporary, self.path)

    def _run(self):
        try:
            while not self._stop.is_set():
                self._wake.wait(0.2)
                self._wake.clear()
                with self._lock:
                    frame, self._pending = self._pending, None
                if frame is not None:
                    self._write(frame)
                    self.frames_published += 1
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"

    def close(self, timeout=1.0):
        self._stop.set()
        self._wake.set()
        if self._thread.ident is not None:
            self._thread.join(timeout)
        if self._thread.is_alive() and self.error is None:
            self.error = "preview writer did not stop within timeout"
        with self._lock:
            self._pending = None

    def snapshot(self):
        return {"enabled": True, "fps_limit": 1.0 / self.interval,
                "frames_published": self.frames_published,
                "frames_skipped": self.frames_skipped, "error": self.error,
                "thread_alive": self._thread.is_alive()}


class RecordPreview:
    """Reader принадлежит одному запуску. Не открывает видеоустройства."""
    def __init__(self, max_clients=8, directory=None):
        if directory is None:
            parent = "/dev/shm" if sys_is_linux() else None
            directory = tempfile.mkdtemp(prefix="usv-record-preview-", dir=parent)
        self.directory = Path(directory)
        self.stop_event = threading.Event()
        self._lock = threading.Lock()
        self.max_clients = max_clients
        self.clients_total = 0
        self._clients = {key: 0 for key in CAMERAS}
        self._errors = {}

    def acquire_client(self, key):
        with self._lock:
            if key not in CAMERAS or self.stop_event.is_set() or self.clients_total >= self.max_clients:
                return False
            self.clients_total += 1
            self._clients[key] += 1
            return True

    def release_client(self, key):
        with self._lock:
            if self._clients.get(key, 0):
                self._clients[key] -= 1
                self.clients_total = max(0, self.clients_total - 1)

    def read_frame(self, key):
        if key not in CAMERAS:
            raise KeyError(key)
        if self.stop_event.is_set():
            return None
        try:
            with frame_path(self.directory, CAMERAS[key]).open("rb") as handle:
                frame = decode_frame(handle.read(HEADER.size + MAX_JPEG + 1))
            with self._lock:
                self._errors.pop(key, None)
            return frame
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            with self._lock:
                self._errors[key] = f"{type(exc).__name__}: {exc}"
            return None

    def snapshot(self):
        cameras = {}
        now = time.monotonic_ns()
        for key in CAMERAS:
            age = None
            seq = None
            try:
                with frame_path(self.directory, CAMERAS[key]).open("rb") as handle:
                    data = handle.read(HEADER.size)
                magic, seq, mono = HEADER.unpack(data)
                if magic != MAGIC:
                    raise ValueError("bad preview header")
                age = max(0.0, (now - mono) / 1e9)
            except FileNotFoundError:
                pass
            except (OSError, ValueError, struct.error) as exc:
                with self._lock:
                    self._errors[key] = f"{type(exc).__name__}: {exc}"
            with self._lock:
                error = self._errors.get(key)
                clients = self._clients[key]
            cameras[key] = {"has_frame": age is not None,
                "last_frame_age_s": None if age is None else round(age, 2),
                "source_frame_id": seq, "stream_clients": clients, "error": error,
                "warning": error or ("нет свежих кадров просмотра" if age is None or age > STALE_SECONDS else None)}
        with self._lock:
            clients = self.clients_total
        return {"active": not self.stop_event.is_set(), "source": "recorder",
                "fps_limit": FPS, "clients_total": clients, "cameras": cameras}

    def close(self):
        self.stop_event.set()
        shutil.rmtree(self.directory, ignore_errors=True)


def sys_is_linux():
    import sys
    return sys.platform.startswith("linux")
