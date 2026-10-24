#!/usr/bin/env python3
"""Просмотр двух USB-камер (MJPEG) для настройки фокуса и положения.

Первый этап: только просмотр. Рекордер и формат записей не затрагиваются.

Архитектура:
- по одному потоку захвата на камеру (PyAV/V4L2);
- слот "последний кадр" на камеру, без очереди приложения;
- HTTP-сервер stdlib раздаёт MJPEG и страницу с двумя изображениями;
- медленный клиент не блокирует захват (запись в сокет вне блокировки кадра).

Импорт PyAV и fcntl ленивый: модуль импортируется и тестируется на Windows.
"""

from __future__ import annotations

import argparse
import base64
import errno
import json
import os
import signal
import socket
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CAPTURE_OPTIONS = {"input_format": "mjpeg", "video_size": "1280x720", "framerate": "60"}
STREAM_BOUNDARY = "usvframe"
STREAM_WRITE_TIMEOUT_S = 5.0
STREAM_SNDBUF_BYTES = 64 * 1024
DEFAULT_FPS = 8.0
DEFAULT_PORT = 8088
DEFAULT_MAX_CLIENTS = 8

# Тестовые кадры: настоящие JPEG (SOI/EOI и таблицы), только для --fake-cameras.
FAKE_JPEGS = {
    "lower": (
        "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"
        "t27U6sgyLp/p2nWOOXqn/9k="
    ),
    "upper": (
        "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"
    ),
}

CAMERA_SPECS = (
    {"key": "upper", "label": "Верхняя камера", "device_arg": "device_upper",
     "default_device": "/dev/video4"},
    {"key": "lower", "label": "Нижняя камера", "device_arg": "device_lower",
     "default_device": "/dev/video2"},
)

PREVIEW_HTML = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>USV: просмотр камер</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         background: #101418; color: #e6edf3; }
  header { padding: 12px 16px; border-bottom: 1px solid #2a333d; display: flex;
           flex-wrap: wrap; gap: 8px 24px; align-items: baseline; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  #summary { color: #9fb0c0; font-size: 13px; }
  main { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
         gap: 12px; padding: 12px; }
  .cam { border: 1px solid #2a333d; border-radius: 8px; overflow: hidden;
         background: #161b21; display: flex; flex-direction: column; min-width: 0; }
  .cam-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
              padding: 8px 10px; border-bottom: 1px solid #2a333d; }
  .cam-title { font-weight: 600; }
  .cam-sub { color: #8b9bab; font-size: 12px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #7a8794; flex: none; }
  .dot.live { background: #35c46a; }
  .dot.error { background: #e05252; }
  .dot.retry { background: #d8a13a; }
  .dot.connecting { background: #4a90d9; }
  .spacer { flex: 1 1 auto; }
  select, button { background: #1e252d; color: #e6edf3; border: 1px solid #3a4754;
                   border-radius: 6px; padding: 4px 6px; font-size: 13px; }
  .viewport { overflow: auto; max-height: 70vh; background: #000;
              display: flex; align-items: flex-start; justify-content: center;
              touch-action: none; cursor: grab; }
  .viewport.dragging { cursor: grabbing; }
  .viewport img { display: block; width: 100%; height: auto; max-width: 100%; user-select: none; }
  .viewport img.pixelated { image-rendering: pixelated; }
  .details { padding: 6px 10px; font-size: 12px; color: #9fb0c0;
             display: flex; flex-wrap: wrap; gap: 4px 16px; }
  .error { margin: 0 10px 8px; padding: 6px 8px; border-radius: 6px;
           background: #44201f; color: #ffc9c6; font-size: 12px; }
  footer { padding: 10px 16px 20px; color: #8b9bab; font-size: 12px; line-height: 1.5; }
</style>
</head>
<body>
<header>
  <h1>Просмотр камер</h1>
  <div id="summary">подключение…</div>
</header>
<main id="grid"></main>
<footer>
  «Сервер» — возраст последнего кадра на Repka. «Браузер» — сколько прошло с момента,
  когда этот кадр получила страница. Визуальная задержка включает серверную свежесть,
  передачу по сети и отрисовку браузером; эти числа её не гарантируют.
  Режим просмотра несовместим с одновременной записью на тех же камерах.
</footer>
<script>
const CAMERAS = __CAMERAS_JSON__;
const STALL_MS = 5000;
const MAX_BUFFER = 8 * 1024 * 1024;
const CRLFCRLF = new Uint8Array([13, 10, 13, 10]);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function indexOfBytes(data, needle, from) {
  const start = from || 0;
  outer: for (let i = start; i <= data.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (data[i + j] !== needle[j]) continue outer;
    }
    return i;
  }
  return -1;
}

class MultipartParser {
  constructor(onFrame) {
    this.onFrame = onFrame;
    this.buf = new Uint8Array(0);
    this.boundary = new TextEncoder().encode("--" + "__BOUNDARY__");
  }
  push(chunk) {
    const merged = new Uint8Array(this.buf.length + chunk.length);
    merged.set(this.buf, 0);
    merged.set(chunk, this.buf.length);
    this.buf = merged;
    if (this.buf.length > MAX_BUFFER) this.buf = new Uint8Array(0);
    for (;;) {
      const start = indexOfBytes(this.buf, this.boundary, 0);
      if (start < 0) return;
      const headerEnd = indexOfBytes(this.buf, CRLFCRLF, start + this.boundary.length);
      if (headerEnd < 0) return;
      const head = new TextDecoder().decode(
        this.buf.subarray(start + this.boundary.length, headerEnd));
      const match = /content-length:\\s*(\\d+)/i.exec(head);
      if (!match) { this.buf = this.buf.subarray(headerEnd + CRLFCRLF.length); continue; }
      const size = parseInt(match[1], 10);
      const dataStart = headerEnd + CRLFCRLF.length;
      if (this.buf.length < dataStart + size) return;
      const jpeg = this.buf.slice(dataStart, dataStart + size);
      this.buf = this.buf.subarray(dataStart + size);
      if (jpeg.length && jpeg[0] === 0xff && jpeg[1] === 0xd8) this.onFrame(jpeg);
    }
  }
}

function fmtAge(ms) {
  if (ms === null || ms === undefined) return "—";
  return (ms / 1000).toFixed(ms < 10000 ? 2 : 1) + " с";
}

function fmtRate(value) {
  return (value === null || value === undefined) ? "—" : value.toFixed(1) + " к/с";
}

const cams = CAMERAS.map((spec) => {
  const root = el("section", "cam");
  const head = el("div", "cam-head");
  const dot = el("span", "dot connecting");
  const title = el("span", "cam-title", spec.label);
  const sub = el("span", "cam-sub", "cam_" + spec.key + " · " + spec.device);
  const spacer = el("span", "spacer");
  const scale = el("select");
  [["fit", "Fit"], ["1", "1:1"], ["2", "200%"], ["4", "400%"]].forEach(([value, text]) => {
    const option = el("option", "", text);
    option.value = value;
    scale.appendChild(option);
  });
  const diag = el("a", "", "кадр");
  diag.href = "/diagnostics/frame.jpg?camera=" + spec.key;
  diag.target = "_blank";
  diag.rel = "noopener";
  diag.style.color = "#7fb6e8";
  head.append(dot, title, sub, spacer, scale, diag);
  const viewport = el("div", "viewport");
  const img = el("img");
  img.alt = spec.label;
  viewport.appendChild(img);
  const details = el("div", "details");
  const serverDetail = el("span", "", "сервер: —");
  const clientDetail = el("span", "", "браузер: —");
  details.append(serverDetail, clientDetail);
  const errorBox = el("div", "error", "");
  errorBox.hidden = true;
  root.append(head, viewport, details, errorBox);
  return {
    key: spec.key, label: spec.label, root, img, viewport, dot, scale,
    serverDetail, clientDetail, errorBox,
    controller: null, timer: null, attempt: 0, lastFrameAt: 0,
    objectUrl: null, stopping: false,
  };
});
document.getElementById("grid").append(...cams.map((cam) => cam.root));

function setState(cam, state, text) {
  cam.dot.className = "dot " + state;
  if (text) cam.clientDetail.textContent = "браузер: " + text;
}

function applyScale(cam) {
  const mode = cam.scale.value;
  if (mode === "fit") {
    cam.img.style.width = "100%";
    cam.img.style.maxWidth = "100%";
    cam.img.classList.remove("pixelated");
    return;
  }
  const zoom = parseFloat(mode);
  const width = cam.img.naturalWidth || 1280;
  cam.img.style.width = Math.round(width * zoom) + "px";
  cam.img.style.maxWidth = "none";
  cam.img.classList.toggle("pixelated", zoom > 1);
}
cams.forEach((cam) => cam.scale.addEventListener("change", () => applyScale(cam)));

cams.forEach((cam) => {
  let drag = null;
  cam.viewport.addEventListener("pointerdown", (event) => {
    drag = { x: event.clientX, y: event.clientY,
             left: cam.viewport.scrollLeft, top: cam.viewport.scrollTop };
    cam.viewport.classList.add("dragging");
    cam.viewport.setPointerCapture(event.pointerId);
  });
  cam.viewport.addEventListener("pointermove", (event) => {
    if (!drag) return;
    cam.viewport.scrollLeft = drag.left - (event.clientX - drag.x);
    cam.viewport.scrollTop = drag.top - (event.clientY - drag.y);
  });
  const release = (event) => {
    drag = null;
    cam.viewport.classList.remove("dragging");
    if (cam.viewport.hasPointerCapture(event.pointerId)) {
      cam.viewport.releasePointerCapture(event.pointerId);
    }
  };
  cam.viewport.addEventListener("pointerup", release);
  cam.viewport.addEventListener("pointercancel", release);
});

function stopStream(cam) {
  const controller = cam.controller;
  cam.controller = null;
  if (controller) controller.abort();
}

function scheduleReconnect(cam, reason) {
  if (cam.timer) return;
  cam.attempt = Math.min(cam.attempt + 1, 8);
  const delay = Math.min(10000, 2000 * Math.pow(1.6, cam.attempt - 1));
  setState(cam, "retry", (reason ? reason + "; " : "") +
    "переподключение через " + (delay / 1000).toFixed(1) + " с");
  cam.timer = setTimeout(() => { cam.timer = null; connectStream(cam); }, delay);
}

function onFrame(cam, bytes) {
  const url = URL.createObjectURL(new Blob([bytes], { type: "image/jpeg" }));
  const previous = cam.objectUrl;
  cam.img.src = url;
  cam.objectUrl = url;
  if (previous) setTimeout(() => URL.revokeObjectURL(previous), 3000);
  cam.lastFrameAt = Date.now();
  cam.attempt = 0;
  if (cam.timer) { clearTimeout(cam.timer); cam.timer = null; }
  setState(cam, "live", "получен кадр");
  applyScale(cam);
}

function connectStream(cam) {
  stopStream(cam);
  const controller = new AbortController();
  cam.controller = controller;
  cam.lastFrameAt = Date.now();
  setState(cam, "connecting", "подключение…");
  fetch("/stream/" + cam.key + ".mjpg?t=" + Date.now(),
        { cache: "no-store", signal: controller.signal })
    .then((response) => {
      if (!response.ok) throw new Error("HTTP " + response.status);
      if (!response.body) throw new Error("браузер не поддерживает потоковое чтение");
      const reader = response.body.getReader();
      const parser = new MultipartParser((bytes) => onFrame(cam, bytes));
      const pump = () => reader.read().then(({ done, value }) => {
        if (done) throw new Error("поток закрыт сервером");
        parser.push(value);
        pump();
      }).catch((error) => {
        if (cam.controller === controller) {
          cam.controller = null;
          scheduleReconnect(cam, error && error.message ? error.message : String(error));
        }
      });
      pump();
    })
    .catch((error) => {
      if (controller.signal.aborted) return;
      if (cam.controller === controller) cam.controller = null;
      scheduleReconnect(cam, error && error.message ? error.message : String(error));
    });
}

cams.forEach((cam) => connectStream(cam));

function refreshStatus() {
  fetch("/status.json", { cache: "no-store" })
    .then((response) => response.json())
    .then((data) => {
      const summary = document.getElementById("summary");
      summary.textContent = "сервер: " + data.host + ":" + data.port +
        " · камеры: " + Object.keys(data.cameras).length +
        " · потоков: " + data.clients_total + "/" + data.max_clients +
        (data.fake_cameras ? " · ТЕСТОВЫЕ КАДРЫ" : "");
      for (const cam of cams) {
        const info = data.cameras[cam.key];
        if (!info) continue;
        cam.serverDetail.textContent =
          "сервер: " + fmtAge(info.last_frame_age_s === null ? null : info.last_frame_age_s * 1000) +
          " · " + (info.mode ? info.mode.codec + " " + info.mode.width + "×" + info.mode.height : info.state) +
          " · захват " + fmtRate(info.capture_fps_measured) +
          " · публикация " + fmtRate(info.published_fps_measured) +
          " · клиентов " + info.stream_clients;
        if (info.error) {
          cam.errorBox.textContent = "Ошибка камеры: " + info.error;
          cam.errorBox.hidden = false;
        } else if (info.state === "error") {
          cam.errorBox.textContent = "Камера не работает; подробности в журнале сервера.";
          cam.errorBox.hidden = false;
        } else {
          cam.errorBox.hidden = true;
        }
        if (info.state !== "running" && info.state !== "opening" && !info.error) {
          cam.serverDetail.textContent += " · " + info.state;
        }
      }
    })
    .catch(() => {
      document.getElementById("summary").textContent = "нет связи с сервером";
    });
}

setInterval(() => {
  const now = Date.now();
  for (const cam of cams) {
    if (cam.controller && now - cam.lastFrameAt > STALL_MS) {
      stopStream(cam);
      scheduleReconnect(cam, "нет кадров " + fmtAge(now - cam.lastFrameAt));
      continue;
    }
    cam.clientDetail.textContent = cam.controller
      ? "браузер: " + fmtAge(now - cam.lastFrameAt)
      : cam.clientDetail.textContent;
  }
  refreshStatus();
}, 1000);
refreshStatus();
</script>
</body>
</html>
"""


class Frame:
    __slots__ = ("seq", "payload", "mono_ns")

    def __init__(self, seq, payload, mono_ns):
        self.seq = seq
        self.payload = payload
        self.mono_ns = mono_ns


class LatestFrame:
    """Слот последнего кадра: старый кадр затирается, очереди нет."""

    def __init__(self):
        self._condition = threading.Condition()
        self._seq = 0
        self._payload = None
        self._mono_ns = None

    @property
    def seq(self):
        with self._condition:
            return self._seq

    def publish(self, payload, mono_ns=None):
        with self._condition:
            self._seq += 1
            self._payload = payload
            self._mono_ns = time.monotonic_ns() if mono_ns is None else mono_ns
            self._condition.notify_all()
            return self._seq

    def snapshot(self):
        with self._condition:
            if self._payload is None:
                return None
            return Frame(self._seq, self._payload, self._mono_ns)

    def wait_next(self, after_seq, timeout):
        """Ждёт кадр новее after_seq. Возвращает последний на момент пробуждения."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._seq <= after_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)
            return Frame(self._seq, self._payload, self._mono_ns)


def publish_due(now, last_publish, fps):
    """Частота публикации: не чаще fps. Первый кадр публикуется сразу."""
    if fps <= 0:
        raise ValueError("fps must be positive")
    return last_publish is None or (now - last_publish) >= (1.0 / fps)


def has_jpeg_markers(payload):
    """Только SOI/EOI. Это не проверка корректности изображения и не декодирование."""
    return len(payload) >= 4 and payload[:2] == b"\xff\xd8" and payload[-2:] == b"\xff\xd9"


def _rate(times):
    if len(times) < 2:
        return None
    span = times[-1] - times[0]
    if span <= 0:
        return None
    return (len(times) - 1) / span


class CameraRuntime:
    def __init__(self, key, label, device):
        self.key = key
        self.label = label
        self.device = device
        self.latest = LatestFrame()
        self._lock = threading.Lock()
        self.state = "starting"
        self.error = None
        self.last_error = None
        self.mode = None
        self.received_packets = 0
        self.published_frames = 0
        self.rejected_packets = 0
        self._packet_times = deque(maxlen=120)
        self._publish_times = deque(maxlen=40)
        self._last_publish_mono = None
        self.stream_clients = 0

    def set_state(self, state):
        with self._lock:
            self.state = state

    def set_error(self, message):
        with self._lock:
            self.state = "error"
            self.error = message

    def note_error(self, message):
        with self._lock:
            self.last_error = message

    def note_packet(self, now):
        with self._lock:
            self.received_packets += 1
            self._packet_times.append(now)

    def note_publish(self, now):
        with self._lock:
            self.published_frames += 1
            self._publish_times.append(now)
            self._last_publish_mono = now

    def note_rejected(self, message):
        with self._lock:
            self.rejected_packets += 1
            self.last_error = message

    def snapshot(self, now):
        with self._lock:
            age = None
            if self._last_publish_mono is not None:
                age = max(0.0, now - self._last_publish_mono)
            capture_rate = _rate(self._packet_times)
            publish_rate = _rate(self._publish_times)
            return {
                "key": self.key,
                "label": self.label,
                "device": self.device,
                "state": self.state,
                "error": self.error,
                "last_error": self.last_error,
                "mode": self.mode,
                "received_packets": self.received_packets,
                "published_frames": self.published_frames,
                "rejected_packets": self.rejected_packets,
                "capture_fps_measured": None if capture_rate is None
                else round(capture_rate, 2),
                "published_fps_measured": None if publish_rate is None
                else round(publish_rate, 2),
                "last_frame_age_s": None if age is None else round(age, 3),
                "has_frame": self._last_publish_mono is not None,
                "stream_clients": self.stream_clients,
            }


class PreviewApp:
    def __init__(self, devices, fps, max_clients, fake_cameras, fake_capture_fps):
        self.fps = fps
        self.max_clients = max_clients
        self.fake_cameras = fake_cameras
        self.fake_capture_fps = fake_capture_fps
        self.stop_event = threading.Event()
        self.stop_reason = None
        self.started_at_utc = datetime.now(timezone.utc).isoformat()
        self.started_mono = time.monotonic()
        self.cameras = {
            spec["key"]: CameraRuntime(spec["key"], spec["label"], devices[spec["key"]])
            for spec in CAMERA_SPECS
        }
        self._threads = {}
        self._client_lock = threading.Lock()
        self.clients_total = 0

    def request_stop(self, reason):
        if self.stop_reason is None:
            self.stop_reason = reason
        self.stop_event.set()

    def start(self):
        for key, runtime in self.cameras.items():
            target = fake_capture if self.fake_cameras else v4l2_capture
            thread = threading.Thread(
                target=target,
                args=(self, runtime),
                name=f"capture-{key}",
                daemon=True,
            )
            self._threads[key] = thread
            thread.start()

    def stop(self, join_timeout=4.0):
        """Просит потоки остановиться и ждёт ограниченное время.

        Возвращает список ключей камер, чьи потоки не завершились.
        """
        self.request_stop(self.stop_reason or "shutdown")
        deadline = time.monotonic() + join_timeout
        alive = []
        for key, thread in self._threads.items():
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
            if thread.is_alive():
                alive.append(key)
        return alive

    def acquire_client(self, key):
        with self._client_lock:
            if self.clients_total >= self.max_clients:
                return False
            self.clients_total += 1
            self.cameras[key].stream_clients += 1
            return True

    def release_client(self, key):
        with self._client_lock:
            self.clients_total = max(0, self.clients_total - 1)
            self.cameras[key].stream_clients = max(
                0, self.cameras[key].stream_clients - 1)

    def status(self, host, port):
        now = time.monotonic()
        return {
            "app": "usv-preview",
            "started_at_utc": self.started_at_utc,
            "uptime_s": round(now - self.started_mono, 1),
            "host": host,
            "port": port,
            "fps_target": self.fps,
            "fake_cameras": self.fake_cameras,
            "max_clients": self.max_clients,
            "clients_total": self.clients_total,
            "note": (
                "last_frame_age_s — свежесть последнего опубликованного кадра "
                "на сервере, а не визуальная задержка в браузере"
            ),
            "cameras": {key: runtime.snapshot(now)
                        for key, runtime in self.cameras.items()},
        }


def find_device_holders(device):
    """Необязательная диагностика Linux: PID, у которых открыт device.

    Не гарантирует отсутствие конфликта: другой процесс может открыть
    устройство сразу после проверки. Ошибки чтения /proc игнорируются.
    """
    holders = []
    proc = Path("/proc")
    if not proc.is_dir():
        return holders
    try:
        entries = list(proc.iterdir())
    except OSError:
        return holders
    target = str(Path(device))
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    holders.append(int(entry.name))
                    break
            except OSError:
                continue
    return holders


def acquire_preview_lock(path):
    """Блокировка только от второго экземпляра просмотрщика.

    Возвращает (handle, status), где status: None, "unavailable", "busy"
    или текст ошибки. При "unavailable" работа продолжается без блокировки.
    """
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+")
    try:
        import fcntl  # Linux/Unix only; на Windows блокировка недоступна
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


def fake_capture(app, runtime):
    """Тестовый генератор: настоящие JPEG, устройства камер не открываются."""
    payload = base64.b64decode(FAKE_JPEGS[runtime.key])
    interval = 1.0 / app.fake_capture_fps
    last_publish = None
    next_at = time.monotonic()
    runtime.set_state("running")
    runtime.mode = {"codec": "fake-jpeg", "width": 256, "height": 144}
    try:
        while not app.stop_event.is_set():
            now = time.monotonic()
            if now < next_at:
                app.stop_event.wait(min(next_at - now, 0.1))
                continue
            next_at = max(next_at + interval, now)
            runtime.note_packet(now)
            if publish_due(now, last_publish, app.fps):
                runtime.latest.publish(payload)
                runtime.note_publish(now)
                last_publish = now
    except BaseException as exc:
        runtime.set_error(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        if runtime.state != "error":
            runtime.set_state("stopped")


def v4l2_capture(app, runtime):
    """Захват камеры. Контейнер открывает и закрывает только этот поток."""
    container = None
    try:
        runtime.set_state("opening")
        if sys.platform.startswith("linux"):
            holders = find_device_holders(runtime.device)
            if holders:
                runtime.note_error(
                    f"устройство уже открыто процессами {holders}; "
                    "вероятно, работает рекордер или другой просмотр"
                )
        import av  # лениво: модуль импортируем без PyAV

        container = av.open(runtime.device, format="v4l2",
                            options=dict(CAPTURE_OPTIONS))
        stream = container.streams.video[0]
        context = stream.codec_context
        runtime.mode = {
            "codec": context.name,
            "width": context.width,
            "height": context.height,
        }
        if context.name != "mjpeg":
            raise RuntimeError(
                f"камера согласовала {context.name!r}, ожидался mjpeg")
        runtime.set_state("running")
        print(f"{runtime.key}: {runtime.device} открыта, "
              f"{context.name} {context.width}x{context.height}", flush=True)

        last_publish = None
        for packet in container.demux(stream):
            now = time.monotonic()
            if packet.size == 0:
                continue
            runtime.note_packet(now)
            if app.stop_event.is_set():
                break
            if packet.is_corrupt:
                runtime.note_rejected("повреждённый пакет пропущен")
                packet = None
                continue
            if not publish_due(now, last_publish, app.fps):
                packet = None
                continue
            payload = bytes(packet)
            if not has_jpeg_markers(payload):
                runtime.note_rejected(
                    "пакет не похож на самостоятельный JPEG (SOI/EOI)")
                packet = None
                continue
            runtime.latest.publish(payload)
            runtime.note_publish(now)
            last_publish = now
            packet = None
        else:
            if not app.stop_event.is_set():
                raise RuntimeError(
                    "поток камеры завершился неожиданно (камера отключена?)")
    except BaseException as exc:
        if not app.stop_event.is_set():
            runtime.set_error(f"{type(exc).__name__}: {exc}")
            print(f"{runtime.key}: ошибка захвата: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            traceback.print_exc()
    finally:
        if runtime.state != "error":
            runtime.set_state("stopped")
        if container is not None:
            try:
                container.close()
            except BaseException:
                print(f"{runtime.key}: ошибка закрытия камеры", file=sys.stderr)
                traceback.print_exc()


class PreviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    block_on_close = False

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, PreviewHandler)


class PreviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "usv-preview/1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._send_html()
        if path == "/status.json":
            return self._send_status()
        if path == "/diagnostics/frame.jpg":
            key = parse_qs(parsed.query).get("camera", ["lower"])[0]
            return self._send_diagnostic_frame(key)
        if path.startswith("/stream/") and path.endswith(".mjpg"):
            key = path[len("/stream/"):-len(".mjpg")]
            return self._send_stream(key)
        self._send_body(404, "text/plain; charset=utf-8", b"not found")

    def _send_body(self, code, content_type, body, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _send_html(self):
        body = PREVIEW_HTML.replace("__CAMERAS_JSON__", json.dumps(
            [{"key": spec["key"], "label": spec["label"],
              "device": self.server.app.cameras[spec["key"]].device}
             for spec in CAMERA_SPECS], ensure_ascii=False))
        body = body.replace("__BOUNDARY__", STREAM_BOUNDARY)
        self._send_body(200, "text/html; charset=utf-8", body.encode("utf-8"))

    def _send_status(self):
        app = self.server.app
        host, port = self.server.server_address[0], self.server.server_address[1]
        body = json.dumps(app.status(host, port), ensure_ascii=False).encode("utf-8")
        self._send_body(200, "application/json; charset=utf-8", body)

    def _send_diagnostic_frame(self, key):
        runtime = self.server.app.cameras.get(key)
        if runtime is None:
            return self._send_body(404, "text/plain; charset=utf-8",
                                   b"unknown camera")
        frame = runtime.latest.snapshot()
        if frame is None:
            return self._send_body(503, "text/plain; charset=utf-8",
                                   b"no frame published yet")
        age_ms = (time.monotonic_ns() - frame.mono_ns) / 1e6
        self._send_body(
            200, "image/jpeg", frame.payload,
            {"Content-Disposition":
             f'attachment; filename="{key}_preview_frame.jpg"',
             "X-Frame-Age-Ms": f"{age_ms:.1f}"},
        )

    def _send_stream(self, key):
        app = self.server.app
        runtime = app.cameras.get(key)
        if runtime is None:
            return self._send_body(404, "text/plain; charset=utf-8",
                                   b"unknown camera")
        if not app.acquire_client(key):
            return self._send_body(503, "text/plain; charset=utf-8",
                                   b"too many clients")
        try:
            self.send_response(200)
            self.send_header(
                "Content-Type",
                f"multipart/x-mixed-replace; boundary={STREAM_BOUNDARY}")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(STREAM_WRITE_TIMEOUT_S)
            try:
                # Ограничивает объём уже отправленных, но не прочитанных кадров
                # в буфере сокета: медленный клиент получает более свежие кадры.
                self.connection.setsockopt(
                    socket.SOL_SOCKET, socket.SO_SNDBUF, STREAM_SNDBUF_BYTES)
            except OSError:
                pass
            last_seq = 0
            while not app.stop_event.is_set():
                frame = runtime.latest.wait_next(last_seq, timeout=1.0)
                if frame is None:
                    continue
                last_seq = frame.seq
                header = (
                    f"--{STREAM_BOUNDARY}\r\n"
                    f"Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(frame.payload)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(header + frame.payload + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                socket.timeout, TimeoutError):
            pass
        except OSError:
            pass
        finally:
            app.release_client(key)


def create_server(host, port, app):
    return PreviewHTTPServer((host, port), app)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Просмотр двух камер (MJPEG) без записи.")
    parser.add_argument("--host", default="0.0.0.0",
                        help="адрес прослушивания (по умолчанию 0.0.0.0)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"порт HTTP (по умолчанию {DEFAULT_PORT})")
    parser.add_argument("--fps", type=float, default=DEFAULT_FPS,
                        help="частота ПУБЛИКАЦИИ кадров на камеру (по умолчанию 8)")
    parser.add_argument("--device-upper", default="/dev/video4",
                        help="устройство верхней камеры")
    parser.add_argument("--device-lower", default="/dev/video2",
                        help="устройство нижней камеры")
    parser.add_argument("--max-clients", type=int, default=DEFAULT_MAX_CLIENTS,
                        help="максимум одновременных MJPEG-потоков")
    parser.add_argument("--fake-cameras", action="store_true",
                        help="тестовые JPEG без обращения к устройствам")
    parser.add_argument("--fake-capture-fps", type=float, default=60.0,
                        help="частота генерации в режиме --fake-cameras")
    parser.add_argument("--lock-file", default="~/.cache/usv_record/preview.lock",
                        help="lock-файл второго экземпляра просмотрщика")
    parser.add_argument("--no-lock", action="store_true",
                        help="не брать lock-файл")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.fps <= 0:
        print("--fps должен быть > 0", file=sys.stderr)
        return 2
    if args.fake_capture_fps <= 0:
        print("--fake-capture-fps должен быть > 0", file=sys.stderr)
        return 2
    if args.max_clients <= 0:
        print("--max-clients должен быть > 0", file=sys.stderr)
        return 2
    if not (0 <= args.port <= 65535):
        print("--port вне диапазона 0..65535", file=sys.stderr)
        return 2

    lock_handle = None
    if not args.no_lock:
        try:
            lock_handle, lock_status = acquire_preview_lock(args.lock_file)
        except OSError as exc:
            print(f"lock-файл недоступен: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            lock_handle, lock_status = None, "error"
        if lock_handle is None and lock_status == "busy":
            print("Другой экземпляр просмотрщика уже работает "
                  f"(lock: {args.lock_file}). Запуск отменён.", file=sys.stderr)
            return 1
        if lock_status == "unavailable":
            print("fcntl недоступен: блокировка второго экземпляра не работает.",
                  file=sys.stderr)

    devices = {
        "upper": args.device_upper,
        "lower": args.device_lower,
    }
    app = PreviewApp(
        devices=devices,
        fps=args.fps,
        max_clients=args.max_clients,
        fake_cameras=args.fake_cameras,
        fake_capture_fps=args.fake_capture_fps,
    )

    def handle_signal(signum, frame):
        app.request_stop(f"signal {signal.Signals(signum).name}")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    server = None
    server_thread = None
    exit_code = 0
    try:
        app.start()
        try:
            server = create_server(args.host, args.port, app)
        except OSError as exc:
            print(f"Не удалось занять {args.host}:{args.port}: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            app.stop(join_timeout=2.0)
            return 1
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.2},
            name="http-server", daemon=True)
        server_thread.start()

        host, port = server.server_address[0], server.server_address[1]
        print("Просмотр камер запущен.", flush=True)
        print(f"Страница: http://{host}:{port}/  "
              f"(с ноутбука/телефона: http://<IP-Repka>:{port}/)", flush=True)
        print("Камеры: cam_upper=" + args.device_upper +
              ", cam_lower=" + args.device_lower, flush=True)
        if args.fake_cameras:
            print(f"РЕЖИМ ТЕСТОВЫХ КАДРОВ: устройства не открываются; "
                  f"генерация {args.fake_capture_fps:g} к/с.", flush=True)
        print(f"Публикация: {args.fps:g} к/с на камеру. "
              f"Частота захвата зависит от камеры и видна в /status.json.",
              flush=True)
        print("Остановка: Ctrl+C (SIGINT) или SIGTERM.", flush=True)

        try:
            while not app.stop_event.wait(0.2):
                pass
        except KeyboardInterrupt:
            app.request_stop("KeyboardInterrupt")

        print(f"Остановка: {app.stop_reason}", flush=True)
        # shutdown() вызывается из главного потока, не из serve_forever().
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2.0)

        alive = app.stop(join_timeout=4.0)
        if alive:
            exit_code = 1
            print("ВНИМАНИЕ: потоки захвата не завершились за 4 с: "
                  + ", ".join(alive), file=sys.stderr, flush=True)
            print("Ограничения: блокирующий demux() может не реагировать на "
                  "stop-событие; контейнер закроет владеющий поток, если demux "
                  "вернёт управление. Процесс завершится, устройства освободит ОС.",
                  file=sys.stderr, flush=True)
        else:
            print("Потоки захвата остановлены, контейнеры закрыты.", flush=True)
        print("Просмотр остановлен.", flush=True)
    finally:
        if server is not None:
            try:
                server.server_close()
            except Exception:
                pass
        if lock_handle is not None:
            lock_handle.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
