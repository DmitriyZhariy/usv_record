"""Аппаратно-независимые проверки просмотрщика.

Запуск из корня репозитория:
    python -m unittest discover -s tests -v

Тесты не открывают камеры и не требуют PyAV: используется --fake-cameras.
Они проверяют логику «последнего кадра», HTTP-контракт и вёрстку страницы,
но не частоту, задержку и работу V4L2 на Repka.
"""

import base64
import http.client
import json
import re
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import preview_dual_mjpeg as preview  # noqa: E402


class LatestFrameTests(unittest.TestCase):
    def test_wait_returns_newest_frame_and_skips_intermediate(self):
        slot = preview.LatestFrame()
        slot.publish(b"first")
        slot.publish(b"second")
        slot.publish(b"third")
        frame = slot.wait_next(0, timeout=0.2)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.seq, 3)
        self.assertEqual(frame.payload, b"third")

    def test_wait_timeout_returns_none(self):
        slot = preview.LatestFrame()
        slot.publish(b"only")
        self.assertIsNone(slot.wait_next(slot.seq, timeout=0.05))

    def test_publish_does_not_block_and_keeps_no_backlog(self):
        slot = preview.LatestFrame()
        started = time.monotonic()
        for index in range(500):
            slot.publish(b"frame-%d" % index)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(slot.snapshot().seq, 500)
        self.assertEqual(slot.snapshot().payload, b"frame-499")

    def test_wait_wakes_on_new_frame(self):
        slot = preview.LatestFrame()
        slot.publish(b"old")
        result = {}

        def waiter():
            result["frame"] = slot.wait_next(slot.seq, timeout=1.0)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        slot.publish(b"new")
        thread.join(timeout=1.0)
        self.assertEqual(result["frame"].payload, b"new")


class PublishGateTests(unittest.TestCase):
    def test_first_frame_is_due(self):
        self.assertTrue(preview.publish_due(100.0, None, 8.0))

    def test_interval_is_respected(self):
        self.assertFalse(preview.publish_due(100.0, 100.0, 8.0))
        self.assertFalse(preview.publish_due(100.124, 100.0, 8.0))
        self.assertTrue(preview.publish_due(100.125, 100.0, 8.0))

    def test_invalid_fps(self):
        with self.assertRaises(ValueError):
            preview.publish_due(1.0, None, 0)


class JpegMarkerTests(unittest.TestCase):
    def test_markers_accept_fake_frames(self):
        for key in ("left", "right"):
            payload = base64.b64decode(preview.FAKE_JPEGS[key])
            self.assertTrue(preview.has_jpeg_markers(payload), key)

    def test_markers_reject_truncated_payload(self):
        payload = base64.b64decode(preview.FAKE_JPEGS["left"])
        self.assertFalse(preview.has_jpeg_markers(payload[:-2]))
        self.assertFalse(preview.has_jpeg_markers(b"not a jpeg"))

    def test_fake_frames_have_baseline_structure(self):
        """Структурная проверка (SOI/EOI/SOF), не декодирование изображения."""
        for key in ("left", "right"):
            payload = base64.b64decode(preview.FAKE_JPEGS[key])
            self.assertTrue(payload.startswith(b"\xff\xd8"), key)
            self.assertTrue(payload.endswith(b"\xff\xd9"), key)
            self.assertTrue(
                any(marker in payload for marker in
                    (b"\xff\xc0", b"\xff\xc1", b"\xff\xc2")),
                key,
            )


class StatusTests(unittest.TestCase):
    def test_snapshot_reports_errors_and_age(self):
        runtime = preview.CameraRuntime("left", "Левая камера", "/dev/video2")
        runtime.note_packet(10.0)
        runtime.note_publish(10.0)
        runtime.set_state("running")
        runtime.set_error("Camera buffer error")
        snapshot = runtime.snapshot(now=10.5)
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["error"], "Camera buffer error")
        self.assertEqual(snapshot["last_frame_age_s"], 0.5)
        self.assertTrue(snapshot["has_frame"])
        self.assertEqual(snapshot["published_frames"], 1)

    def test_snapshot_without_frames(self):
        runtime = preview.CameraRuntime("right", "Правая камера", "/dev/video4")
        snapshot = runtime.snapshot(now=1.0)
        self.assertFalse(snapshot["has_frame"])
        self.assertIsNone(snapshot["last_frame_age_s"])
        self.assertIsNone(snapshot["capture_fps_measured"])


class FakeServerCase(unittest.TestCase):
    def setUp(self):
        self.app = preview.PreviewApp(
            devices={"left": "/dev/video2", "right": "/dev/video4"},
            fps=20.0,
            max_clients=2,
            fake_cameras=True,
            fake_capture_fps=60.0,
        )
        self.app.start()
        self.server = preview.create_server("127.0.0.1", 0, self.app)
        self.port = self.server.server_address[1]
        self.server_thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self.server_thread.start()

    def tearDown(self):
        self.app.request_stop("test teardown")
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2.0)
        alive = self.app.stop(join_timeout=2.0)
        self.assertEqual(alive, [], "потоки захвата не завершились")

    def wait_for_frames(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.get_status()
            cameras = status["cameras"]
            if all(camera["published_frames"] > 0 for camera in cameras.values()):
                return status
            time.sleep(0.05)
        self.fail("фейковые кадры не появились за отведённое время")

    def get_status(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/status.json", timeout=3) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def get_page(self):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/", timeout=3) as resp:
            return resp.read().decode("utf-8")

    def test_page_has_two_labeled_cameras(self):
        body = self.get_page()
        self.assertIn("Левая камера", body)
        self.assertIn("Правая камера", body)
        self.assertIn('"key": "left"', body)
        self.assertIn('"key": "right"', body)
        self.assertIn('"device": "/dev/video2"', body)
        self.assertIn('"device": "/dev/video4"', body)
        self.assertIn("const CAMERAS", body)

    def test_page_order_is_left_then_right(self):
        match = re.search(r"const CAMERAS = (\[.*?\]);", self.get_page(), re.DOTALL)
        self.assertIsNotNone(match)
        cameras = json.loads(match.group(1))
        self.assertEqual([camera["key"] for camera in cameras],
                         ["left", "right"])
        self.assertEqual([camera["label"] for camera in cameras],
                         ["Левая камера", "Правая камера"])
        self.assertEqual([camera["device"] for camera in cameras],
                         ["/dev/video2", "/dev/video4"])

    def test_status_contract(self):
        status = self.wait_for_frames()
        self.assertEqual(status["app"], "usv-preview")
        self.assertTrue(status["fake_cameras"])
        self.assertIn("не визуальная задержка", status["note"])
        self.assertEqual(set(status["cameras"]), {"left", "right"})
        self.assertEqual(status["cameras"]["left"]["device"], "/dev/video2")
        self.assertEqual(status["cameras"]["right"]["device"], "/dev/video4")
        for camera in status["cameras"].values():
            self.assertEqual(camera["state"], "running")
            self.assertGreater(camera["published_frames"], 0)
            self.assertIsNotNone(camera["last_frame_age_s"])
            self.assertLess(camera["last_frame_age_s"], 2.0)
            self.assertIsNotNone(camera["mode"])

    def test_stream_returns_multipart_jpeg(self):
        self.wait_for_frames()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/stream/left.mjpg")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
        data = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            chunk = response.read(4096)
            if not chunk:
                break
            data += chunk
            if b"--" + preview.STREAM_BOUNDARY.encode() in data and b"\xff\xd9" in data:
                break
        connection.close()
        self.assertIn(b"--" + preview.STREAM_BOUNDARY.encode(), data)
        self.assertIn(b"Content-Type: image/jpeg", data)
        start = data.find(b"\xff\xd8")
        self.assertGreaterEqual(start, 0)
        self.assertIn(b"\xff\xd9", data[start:])

    def test_diagnostic_frame_endpoint(self):
        self.wait_for_frames()
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/diagnostics/frame.jpg?camera=right",
                timeout=3) as resp:
            self.assertEqual(resp.headers.get("Content-Type"), "image/jpeg")
            body = resp.read()
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertTrue(body.endswith(b"\xff\xd9"))

    def test_unknown_camera_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/stream/side.mjpg", timeout=3)
        self.assertEqual(caught.exception.code, 404)

    def test_client_limit(self):
        self.wait_for_frames()
        first = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        first.request("GET", "/stream/left.mjpg")
        first_response = first.getresponse()
        self.assertEqual(first_response.status, 200)
        second = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        second.request("GET", "/stream/right.mjpg")
        second_response = second.getresponse()
        self.assertEqual(second_response.status, 200)
        third = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        third.request("GET", "/stream/left.mjpg")
        third_response = third.getresponse()
        self.assertEqual(third_response.status, 503)
        first.close()
        second.close()
        third.close()


class FakeModeIsolationTests(unittest.TestCase):
    def _app(self):
        return preview.PreviewApp(
            devices={"left": "/dev/video2", "right": "/dev/video4"},
            fps=20.0,
            max_clients=2,
            fake_cameras=True,
            fake_capture_fps=60.0,
        )

    def test_fake_mode_does_not_open_devices_and_does_not_import_av(self):
        app = self._app()
        app.start()
        try:
            time.sleep(0.2)
            self.assertNotIn("av", sys.modules,
                             "тестовый режим не должен импортировать PyAV")
        finally:
            self.assertEqual(app.stop(join_timeout=2.0), [])

    def test_stop_marks_cameras_stopped(self):
        app = self._app()
        app.start()
        time.sleep(0.1)
        self.assertEqual(app.stop(join_timeout=2.0), [])
        now = time.monotonic()
        for runtime in app.cameras.values():
            self.assertEqual(runtime.snapshot(now)["state"], "stopped")


if __name__ == "__main__":
    unittest.main()
