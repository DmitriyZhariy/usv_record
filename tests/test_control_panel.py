"""Проверки панели управления без камер, PyAV и реальной записи.

Wrapper и рекордеры заменены управляемыми заглушками. Проверяются
состояния, команды, HTTP-контракт, токен, остановка и диагностика
занятости. Реальный V4L2/PyAV проверяется только на Repka.
"""

import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import control_panel as panel  # noqa: E402

MIB = panel.MIB


def wait_until(predicate, timeout=4.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def make_config(temp, **overrides):
    values = dict(
        output_root=str(Path(temp) / "recordings"),
        token_file=str(Path(temp) / "token"),
        record_lock_file=str(Path(temp) / "record.lock"),
        panel_lock_file=str(Path(temp) / "panel.lock"),
        fake_cameras=True,
        fps=20.0,
        max_clients=4,
        record_seconds=0,
        segment_seconds=10,
        min_free_mib=4096,
        critical_free_mib=1024,
        preview_ready_timeout=2.0,
        ready_timeout=2.0,
        status_interval=0.05,
        disk_interval=0.1,
        stop_wait_seconds=2.0,
        use_lock=False,
    )
    values.update(overrides)
    return panel.PanelConfig(**values)


def fake_disk(free_mib=10000):
    return SimpleNamespace(
        total=10000 * MIB, used=10000 * MIB - free_mib * MIB,
        free=free_mib * MIB)


class FakeRunner:
    counter = 0

    def __init__(self, argv, on_line, stop_delay=0.0,
                 final_status="verified", stop_reason=None,
                 forced=False, exit_code=0, preflight_fail=None,
                 duration_reason=None, started_event=None):
        self.argv = list(argv)
        self.on_line = on_line
        self.stop_delay = stop_delay
        self.final_status = final_status
        self.stop_reason = stop_reason
        self.forced = forced
        self.exit_code = exit_code
        self.preflight_fail = preflight_fail
        self.duration_reason = duration_reason
        self.started_event = started_event
        self.returncode = None
        self.terminated = False
        self.session = None
        self._timer = None

    def start(self):
        if self.preflight_fail:
            self.on_line("Preflight failed: " + self.preflight_fail)
            self.returncode = 1
            return
        root = Path(self.argv[self.argv.index("--output-root") + 1])
        FakeRunner.counter += 1
        self.session = root / f"session_{FakeRunner.counter:04d}"
        for name in ("cam-left", "cam-right"):
            directory = self.session / name
            directory.mkdir(parents=True)
            camera_id = "cam_left" if name == "cam-left" else "cam_right"
            (directory / "session_metadata.json").write_text(
                json.dumps({"status": "recording", "camera_id": camera_id}),
                encoding="utf-8")
            (directory / f"{camera_id}_segment_0000.mkv").write_bytes(
                b"x" * 1024)
        self.on_line(f"Output: {self.session}")
        if self.started_event is not None:
            self.started_event.set()

    def _finish(self):
        if self.returncode is not None:
            return
        if self.session is not None:
            for name in ("cam-left", "cam-right"):
                camera_id = "cam_left" if name == "cam-left" else "cam_right"
                metadata = {
                    "status": self.final_status,
                    "camera_id": camera_id,
                    "stop_reason": self.duration_reason or "user_stop",
                }
                (self.session / name / "session_metadata.json").write_text(
                    json.dumps(metadata), encoding="utf-8")
        if self.stop_reason:
            self.on_line("Stop reason: " + self.stop_reason)
        if self.forced:
            self.on_line("Принудительное завершение процессов.")
        self.returncode = self.exit_code

    def terminate(self):
        if self.terminated:
            return
        self.terminated = True
        if self.stop_delay:
            self._timer = threading.Timer(self.stop_delay, self._finish)
            self._timer.daemon = True
            self._timer.start()
        else:
            self._finish()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self._finish() or self.returncode

    def close(self):
        if self._timer is not None:
            self._timer.cancel()


class LiveThread:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


class FakeRuntime:
    def __init__(self, key, state="running", error=None, published=1):
        self.key = key
        self.state = state
        self.error = error
        self.published_frames = published
        self.stream_clients = 0

    def snapshot(self, now):
        return {
            "key": self.key,
            "state": self.state,
            "error": self.error,
            "published_frames": self.published_frames,
            "stream_clients": self.stream_clients,
        }


class FakePreviewApp:
    def __init__(self, states=None, error_camera=None, alive_keys=None):
        states = states or {"left": "running", "right": "running"}
        self.cameras = {
            key: FakeRuntime(
                key, state=states[key],
                error=(f"камера {key} не открылась"
                       if error_camera == key else None))
            for key in ("left", "right")
        }
        self.stop_event = threading.Event()
        self.clients_total = 0
        self.alive_keys = list(alive_keys or [])
        self.stopped = False
        self._threads = {
            key: LiveThread(alive=(key in self.alive_keys))
            for key in ("left", "right")
        }

    def start(self):
        pass

    def stop(self, join_timeout=4.0):
        self.stopped = True
        return list(self.alive_keys)

    def acquire_client(self, key):
        self.clients_total += 1
        return True

    def release_client(self, key):
        self.clients_total = max(0, self.clients_total - 1)


def make_controller(temp, runner_options=None, **config_overrides):
    config = make_config(temp, **config_overrides)
    runners = []
    options = dict(runner_options or {})

    def runner_factory(argv, on_line):
        runner = FakeRunner(argv, on_line, **options)
        runners.append(runner)
        return runner

    controller = panel.PanelController(
        config,
        token="test-token",
        runner_factory=runner_factory,
        probe_lock=lambda path: {"available": True, "busy": False,
                                 "error": None},
        find_leftovers=lambda: [],
        disk_usage=lambda path: fake_disk(),
    )
    return controller, runners


class PanelTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controllers = []
        self.servers = []

    def tearDown(self):
        for controller in reversed(self.controllers):
            try:
                controller.shutdown()
            except Exception:
                pass
            controller.close()
        for server, thread in reversed(self.servers):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)

    def start_controller(self, **kwargs):
        runner_options = kwargs.pop("runner_options", None)
        controller, runners = make_controller(
            self.temp.name, runner_options=runner_options, **kwargs)
        controller.initialize()
        self.controllers.append(controller)
        return controller, runners

    def start_server(self, controller):
        server = panel.PanelHTTPServer(("127.0.0.1", 0), controller)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05},
            daemon=True)
        thread.start()
        self.servers.append((server, thread))
        return server.server_address[1]

    def wait_state(self, controller, states, timeout=4.0):
        if isinstance(states, str):
            states = (states,)
        return wait_until(
            lambda: controller.status().get("state") in states, timeout)


class LifecycleTests(PanelTestBase):
    def test_initial_state_is_idle_without_cameras(self):
        controller, _ = self.start_controller()
        self.assertEqual(controller.status()["state"], "idle")
        self.assertFalse(controller.status()["preview"]["active"])
        self.assertEqual(controller.status()["preview"]["cameras"], {})

    def test_preview_starts_both_and_stops(self):
        controller, _ = self.start_controller()
        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "preview"))
        cameras = controller.status()["preview"]["cameras"]
        self.assertEqual(set(cameras), {"left", "right"})
        for camera in cameras.values():
            self.assertEqual(camera["state"], "running")
            self.assertGreater(camera["published_frames"], 0)

        controller.handle_command("preview/stop")
        self.assertTrue(self.wait_state(controller, "idle"))
        self.assertFalse(controller.status()["preview"]["active"])

    def test_preview_failure_shows_error(self):
        config = make_config(self.temp.name)
        controller = panel.PanelController(
            config,
            token="test-token",
            preview_factory=lambda **kwargs: FakePreviewApp(
                error_camera="right"),
            runner_factory=lambda argv, line: FakeRunner(argv, line),
            probe_lock=lambda path: {"available": True, "busy": False,
                                     "error": None},
            find_leftovers=lambda: [],
            disk_usage=lambda path: fake_disk(),
        )
        controller.initialize()
        self.controllers.append(controller)
        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "error"))
        self.assertIn("Правая", controller.status()["message"])
        self.assertFalse(controller.status()["preview"]["active"])

    def test_record_start_stops_preview_before_runner(self):
        started = threading.Event()
        controller, runners = self.start_controller(
            runner_options={"started_event": started})
        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "preview"))
        preview_app = controller.preview_facade.current()
        self.assertIsNotNone(preview_app)

        controller.handle_command("record/start")
        self.assertTrue(wait_until(lambda: bool(runners), timeout=4.0))
        self.assertIsNone(controller.preview_facade.current())
        self.assertTrue(started.wait(2.0))
        self.assertTrue(self.wait_state(controller, "recording"))

        controller.handle_command("record/stop")
        self.assertTrue(self.wait_state(controller, "idle"))
        result = controller.status()["result"]
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["verification"],
                         {"left": "verified", "right": "verified"})
        self.assertEqual(runners[0].argv[runners[0].argv.index("--seconds") + 1],
                         "0")

    def test_parallel_and_repeated_commands_do_not_duplicate(self):
        controller, runners = self.start_controller()
        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "preview"))

        accepted = []
        errors = []

        def start():
            try:
                controller.handle_command("record/start")
                accepted.append(True)
            except panel.CommandError as exc:
                errors.append(exc)

        first = threading.Thread(target=start)
        second = threading.Thread(target=start)
        first.start()
        second.start()
        first.join(timeout=2.0)
        second.join(timeout=2.0)

        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)
        self.assertTrue(self.wait_state(controller, "recording"))
        self.assertEqual(len(runners), 1)

        with self.assertRaises(panel.CommandError) as caught:
            controller.handle_command("record/start")
        self.assertEqual(caught.exception.status, 409)


class StatusTests(PanelTestBase):
    def test_status_is_fast_while_stopping(self):
        controller, runners = self.start_controller(
            runner_options={"stop_delay": 0.8})
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))
        port = self.start_server(controller)

        controller.handle_command("record/stop")
        self.assertTrue(self.wait_state(controller, "stopping"))
        started = time.monotonic()
        for _ in range(20):
            connection = http.client.HTTPConnection(
                "127.0.0.1", port, timeout=2)
            connection.request("GET", "/api/status",
                               headers={"X-USV-Token": "test-token"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertTrue(self.wait_state(controller, "idle", timeout=3.0))

    def test_closed_http_client_does_not_stop_recording(self):
        controller, runners = self.start_controller()
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))
        port = self.start_server(controller)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request("GET", "/api/status",
                           headers={"X-USV-Token": "test-token"})
        response = connection.getresponse()
        response.read()
        connection.close()
        time.sleep(0.2)
        self.assertEqual(controller.status()["state"], "recording")
        self.assertFalse(runners[0].terminated)

    def test_low_disk_reason_survives_verified_result(self):
        controller, _ = self.start_controller(
            runner_options={
                "stop_reason": "low_disk_reserve free_mib=100 critical_mib=1024",
            })
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))
        controller.handle_command("record/stop")
        self.assertTrue(self.wait_state(controller, "idle"))
        result = controller.status()["result"]
        self.assertEqual(result["outcome"], "ok")
        self.assertEqual(result["verification"],
                         {"left": "verified", "right": "verified"})
        self.assertIn("low_disk_reserve", result["stop_reason"])
        self.assertIn("low_disk_reserve", controller.status()["message"])

    def test_disk_usage_exception_is_visible_and_record_stops_with_reason(self):
        config = make_config(self.temp.name)
        runner_options = {
            "stop_reason": "disk_check_failed: OSError: disk disappeared",
        }
        runners = []

        def factory(argv, line):
            runner = FakeRunner(argv, line, **runner_options)
            runners.append(runner)
            return runner

        def broken_disk(path):
            raise OSError("disk disappeared")

        controller = panel.PanelController(
            config, token="test-token", runner_factory=factory,
            probe_lock=lambda path: {"available": True, "busy": False,
                                     "error": None},
            find_leftovers=lambda: [], disk_usage=broken_disk)
        controller.initialize()
        self.controllers.append(controller)

        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))
        self.assertTrue(wait_until(
            lambda: controller.status()["disk"]["error"] is not None,
            timeout=2.0))
        self.assertIn("disk disappeared",
                      controller.status()["disk"]["error"])

        controller.handle_command("record/stop")
        self.assertTrue(self.wait_state(controller, "idle"))
        result = controller.status()["result"]
        self.assertEqual(result["verification"],
                         {"left": "verified", "right": "verified"})
        self.assertIn("disk_check_failed", result["stop_reason"])

    def test_preflight_failure_is_reported(self):
        controller, _ = self.start_controller(
            runner_options={
                "preflight_fail": "свободно 10 MiB, требуется 4096 MiB",
            })
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "error"))
        status = controller.status()
        self.assertIn("Preflight failed", status["message"])
        self.assertIn("4096", status["message"])
        self.assertEqual(status["result"]["outcome"], "error")


class StuckPreviewTests(PanelTestBase):
    def test_live_preview_blocks_ack_and_new_capture(self):
        config = make_config(self.temp.name)
        app = FakePreviewApp(alive_keys=["left"])

        def factory(**kwargs):
            return app

        controller = panel.PanelController(
            config, token="test-token", preview_factory=factory,
            runner_factory=lambda argv, line: FakeRunner(argv, line),
            probe_lock=lambda path: {"available": True, "busy": False,
                                     "error": None},
            find_leftovers=lambda: [],
            disk_usage=lambda path: fake_disk())
        controller.initialize()
        self.controllers.append(controller)

        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "preview"))
        controller.handle_command("preview/stop")
        self.assertTrue(self.wait_state(controller, "error"))
        self.assertTrue(controller.status()["stuck_preview"])

        with self.assertRaises(panel.CommandError):
            controller.handle_command("record/start")
        with self.assertRaises(panel.CommandError):
            controller.handle_command("preview/start")

        controller.handle_command("error/ack")
        time.sleep(0.2)
        self.assertEqual(controller.status()["state"], "error")
        self.assertIn("потоки просмотра",
                      controller.status()["message"])

        app._threads["left"].alive = False
        controller.handle_command("error/ack")
        self.assertTrue(self.wait_state(controller, "idle"))


class ConflictTests(PanelTestBase):
    def test_busy_record_lock_blocks_idle_and_new_capture(self):
        config = make_config(self.temp.name)
        controller = panel.PanelController(
            config, token="test-token",
            runner_factory=lambda argv, line: FakeRunner(argv, line),
            probe_lock=lambda path: {"available": True, "busy": True,
                                     "error": None},
            find_leftovers=lambda: [],
            disk_usage=lambda path: fake_disk())
        controller.initialize()
        self.controllers.append(controller)

        self.assertEqual(controller.status()["state"], "error")
        self.assertIn("record-lock", controller.status()["message"])
        for command in ("preview/start", "record/start"):
            with self.assertRaises(panel.CommandError):
                controller.handle_command(command)

        controller.handle_command("error/ack")
        time.sleep(0.2)
        self.assertEqual(controller.status()["state"], "error")

    def test_leftover_process_blocks_idle(self):
        config = make_config(self.temp.name)
        controller = panel.PanelController(
            config, token="test-token",
            runner_factory=lambda argv, line: FakeRunner(argv, line),
            probe_lock=lambda path: {"available": True, "busy": False,
                                     "error": None},
            find_leftovers=lambda: [
                {"pid": 4242, "cmdline": "python record_dual_session.py"}
            ],
            disk_usage=lambda path: fake_disk())
        controller.initialize()
        self.controllers.append(controller)
        self.assertEqual(controller.status()["state"], "error")
        self.assertIn("4242", controller.status()["message"])


class ShutdownTests(PanelTestBase):
    def test_shutdown_waits_for_wrapper(self):
        controller, runners = self.start_controller(
            runner_options={"stop_delay": 0.4})
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))

        started = time.monotonic()
        success = controller.shutdown("остановка панели")
        elapsed = time.monotonic() - started

        self.assertTrue(success)
        self.assertGreaterEqual(elapsed, 0.35)
        self.assertTrue(runners[0].terminated)
        result = controller.status()["result"]
        self.assertEqual(result["outcome"], "ok")
        self.assertFalse(result["forced"])
        self.assertEqual(result["verification"],
                         {"left": "verified", "right": "verified"})

    def test_shutdown_timeout_is_not_success(self):
        controller, runners = self.start_controller(
            runner_options={"stop_delay": 10.0},
            stop_wait_seconds=0.3)
        controller.handle_command("record/start")
        self.assertTrue(self.wait_state(controller, "recording"))

        success = controller.shutdown("остановка панели")
        self.assertFalse(success)
        result = controller.status()["result"]
        self.assertEqual(result["outcome"], "timeout")
        self.assertFalse(result["forced"])
        self.assertIn("не завершился", result["error"])


class HttpTests(PanelTestBase):
    def test_page_and_token_protection(self):
        controller, _ = self.start_controller()
        port = self.start_server(controller)

        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/", timeout=2) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Начать запись обеих камер", page)
        self.assertIn("Левая камера", page)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/status", timeout=2)
        self.assertEqual(caught.exception.code, 401)

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/status",
            headers={"X-USV-Token": "test-token"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["state"], "idle")

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST", "/api/preview/start", body="{}",
            headers={"X-USV-Token": "test-token",
                     "Content-Type": "application/json",
                     "Origin": "http://evil.example"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        connection.request(
            "POST", "/api/preview/start", body="{}",
            headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        response.read()
        connection.close()

    def test_token_required_for_streams_and_diagnostic_jpeg(self):
        controller, _ = self.start_controller()
        controller.handle_command("preview/start")
        self.assertTrue(self.wait_state(controller, "preview"))
        port = self.start_server(controller)

        for path in ("/stream/left.mjpg",
                     "/diagnostics/frame.jpg?camera=left"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}{path}", timeout=2)
            self.assertEqual(caught.exception.code, 401)

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/diagnostics/frame.jpg?camera=left",
            headers={"X-USV-Token": "test-token"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get("Content-Type"), "image/jpeg")
            self.assertTrue(response.read().startswith(b"\xff\xd8"))

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request(
            "GET", "/stream/left.mjpg",
            headers={"X-USV-Token": "test-token"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("multipart/x-mixed-replace",
                      response.getheader("Content-Type"))
        connection.close()


class TokenFileTests(unittest.TestCase):
    def test_existing_token_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "token"
            path.parent.mkdir()
            path.write_text("existing-token\n", encoding="utf-8")
            self.assertEqual(panel.load_or_create_token(path),
                             "existing-token")

    def test_new_token_is_created_with_0600(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "nested" / "token"
            token = panel.load_or_create_token(path)
            self.assertTrue(token)
            self.assertEqual(path.read_text(encoding="utf-8").strip(), token)
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class SegmentTrackerTests(unittest.TestCase):
    def test_new_segment_is_detected_without_rescanning_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            first = directory / "cam_left_segment_0000.mkv"
            first.write_bytes(b"a" * 100)
            tracker = panel.SegmentTracker(directory, segment_seconds=10)
            tracker.scan(1.0)
            self.assertEqual(tracker.total, 100)
            first.write_bytes(b"a" * 200)
            tracker.scan(2.0)
            self.assertEqual(tracker.total, 200)
            second = directory / "cam_left_segment_0001.mkv"
            second.write_bytes(b"b" * 50)
            tracker.scan(3.0)
            self.assertEqual(tracker.snapshot(4.0)["segments"], 2)
            self.assertEqual(tracker.snapshot(4.0)["bytes"], 250)
            self.assertIsNotNone(tracker.snapshot(4.0)["last_change_age_s"])


if __name__ == "__main__":
    unittest.main()
