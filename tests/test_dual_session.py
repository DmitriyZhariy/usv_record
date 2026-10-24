"""Аппаратно-независимые проверки wrapper записи двух камер.

Запуск из корня репозитория:
    python -m unittest discover -s tests -v

Тесты не запускают рекордер, не импортируют PyAV и не открывают камеры:
дочерние процессы заменены управляемыми заглушками. Реальный захват,
V4L2 и проверка сегментов проверяются только на Repka.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import record_dual_session as dual  # noqa: E402

FIXED_NOW = datetime(2026, 10, 24, 12, 30, 45, 123456, tzinfo=timezone.utc)


class FakeClock:
    """Часы, которые двигаются только в sleep()."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(0.0, seconds)


class FakeProcess:
    def __init__(self, returncode=None, exit_after_polls=None,
                 on_terminate=None, on_kill=None):
        self.returncode = returncode
        self.exit_after_polls = exit_after_polls
        self.on_terminate = on_terminate
        self.on_kill = on_kill
        self.poll_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        self.poll_calls += 1
        if (self.returncode is None and self.exit_after_polls is not None
                and self.poll_calls >= self.exit_after_polls):
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.on_terminate is not None:
            self.on_terminate(self)

    def kill(self):
        self.kill_calls += 1
        if self.on_kill is not None:
            self.on_kill(self)
        else:
            self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def make_child(key, process):
    return dual.Child(key, process, Path(f"{key}.log"))


def make_supervisor(stop_flag, clock, **kwargs):
    kwargs.setdefault("sleep", clock.sleep)
    kwargs.setdefault("poll_interval", 0.05)
    return dual.DualSupervisor(stop_flag, clock=clock, **kwargs)


class SessionLayoutTests(unittest.TestCase):
    def test_creates_only_session_root(self):
        with tempfile.TemporaryDirectory() as temp:
            session = dual.create_session(temp, now=FIXED_NOW)
            self.assertEqual(session.name, "20261024_123045_123456")
            self.assertTrue(session.is_dir())
            self.assertEqual(list(session.iterdir()), [])

    def test_second_camera_dirs_are_not_precreated(self):
        with tempfile.TemporaryDirectory() as temp:
            session = dual.create_session(temp, now=FIXED_NOW)
            self.assertFalse((session / "cam-left").exists())
            self.assertFalse((session / "cam-right").exists())

    def test_remembers_custom_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "custom" / "root"
            session = dual.create_session(root, now=FIXED_NOW)
            self.assertEqual(session.parent, root)
            self.assertTrue(root.is_dir())

    def test_timestamp_collision_adds_suffix_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as temp:
            first = dual.create_session(temp, now=FIXED_NOW)
            marker = first / "keep.txt"
            marker.write_text("previous session", encoding="utf-8")

            second = dual.create_session(temp, now=FIXED_NOW)

            self.assertEqual(second.name, "20261024_123045_123456_01")
            self.assertNotEqual(first, second)
            self.assertEqual(marker.read_text(encoding="utf-8"),
                             "previous session")

    def test_collision_loop_gives_up_after_many_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stamp = "20261024_123045_123456"
            (root / stamp).mkdir()
            for index in range(1, 100):
                (root / f"{stamp}_{index:02d}").mkdir()
            with self.assertRaises(RuntimeError):
                dual.create_session(root, now=FIXED_NOW)


class RecorderCommandTests(unittest.TestCase):
    def setUp(self):
        self.options = dual.build_parser().parse_args([])

    def _command(self, index, output_dir="/tmp/session/cam-left"):
        spec = dual.CAMERAS[index]
        return dual.build_recorder_command(
            "/venv/bin/python", "/opt/recorder.py", spec,
            spec["default_device"], output_dir, self.options)

    def test_camera_order_and_mapping(self):
        self.assertEqual([spec["id"] for spec in dual.CAMERAS],
                         ["cam_left", "cam_right"])
        self.assertEqual([spec["dir"] for spec in dual.CAMERAS],
                         ["cam-left", "cam-right"])
        self.assertEqual([spec["default_device"] for spec in dual.CAMERAS],
                         ["/dev/video2", "/dev/video4"])

    def test_left_command_targets_video2_and_cam_left_dir(self):
        command = self._command(0)
        self.assertIn("--camera-id", command)
        self.assertEqual(command[command.index("--camera-id") + 1], "cam_left")
        self.assertEqual(command[command.index("--device") + 1], "/dev/video2")
        self.assertEqual(command[command.index("--output-dir") + 1],
                         "/tmp/session/cam-left")
        self.assertEqual(command[0], "/venv/bin/python")
        self.assertEqual(command[1], "/opt/recorder.py")

    def test_right_command_targets_video4_and_cam_right_dir(self):
        command = self._command(1, "/tmp/session/cam-right")
        self.assertEqual(command[command.index("--camera-id") + 1], "cam_right")
        self.assertEqual(command[command.index("--device") + 1], "/dev/video4")
        self.assertEqual(command[command.index("--output-dir") + 1],
                         "/tmp/session/cam-right")

    def test_limits_are_forwarded(self):
        options = dual.build_parser().parse_args(
            ["--seconds", "12", "--segment-seconds", "3",
             "--queue-frames", "7", "--queue-mib", "9"])
        command = dual.build_recorder_command(
            "python", "recorder.py", dual.CAMERAS[0], "/dev/video2",
            "/tmp/cam", options)
        self.assertEqual(command[command.index("--seconds") + 1], "12")
        self.assertEqual(command[command.index("--segment-seconds") + 1], "3")
        self.assertEqual(command[command.index("--queue-frames") + 1], "7")
        self.assertEqual(command[command.index("--queue-mib") + 1], "9")


class StartChildrenTests(unittest.TestCase):
    def _commands(self):
        return [
            {"key": "cam_left", "log_name": "cam-left.log",
             "argv": ["left"]},
            {"key": "cam_right", "log_name": "cam-right.log",
             "argv": ["right"]},
        ]

    def test_starts_both_and_writes_logs(self):
        started = []

        def popen(argv, **kwargs):
            process = FakeProcess(returncode=0)
            started.append((argv, kwargs, process))
            return process

        with tempfile.TemporaryDirectory() as temp:
            children = dual.start_children(
                self._commands(), temp, popen=popen, stop_grace=1.0,
                clock=FakeClock(), sleep=lambda seconds: None)

        self.assertEqual([child.key for child in children],
                         ["cam_left", "cam_right"])
        self.assertEqual([item[0] for item in started], [["left"], ["right"]])
        self.assertEqual(Path(children[0].log_path).name, "cam-left.log")
        self.assertEqual(children[0].process.returncode, 0)

    def test_failure_on_second_launch_stops_first_and_reraises(self):
        calls = []
        first = FakeProcess(on_terminate=lambda proc: setattr(proc, "returncode", 0))

        def popen(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return first
            raise OSError("cannot start second recoder")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(OSError):
                dual.start_children(
                    self._commands(), temp, popen=popen, stop_grace=1.0,
                    clock=FakeClock(), sleep=lambda seconds: None)
            self.assertTrue((Path(temp) / "cam-left.log").exists())
            self.assertTrue((Path(temp) / "cam-right.log").exists())

        self.assertEqual(first.terminate_calls, 1)
        self.assertEqual(first.wait_calls, 1)


class SupervisorTests(unittest.TestCase):
    def test_both_success_exit_zero(self):
        left = FakeProcess(returncode=0)
        right = FakeProcess(returncode=0)
        clock = FakeClock()
        stop = dual.StopFlag()

        result = make_supervisor(stop, clock).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertEqual(result["codes"], {"cam_left": 0, "cam_right": 0})
        self.assertFalse(result["forced"])
        self.assertIsNone(result["error"])
        self.assertFalse(result["operator_stop"])
        self.assertEqual(dual.exit_code_for(result), 0)
        self.assertEqual((left.wait_calls, right.wait_calls), (1, 1))

    def test_polls_both_processes_without_blocking_on_one(self):
        left = FakeProcess(exit_after_polls=2)
        right = FakeProcess(exit_after_polls=6)
        clock = FakeClock()
        stop = dual.StopFlag()

        result = make_supervisor(stop, clock).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertEqual(dual.exit_code_for(result), 0)
        self.assertGreaterEqual(left.poll_calls, 2)
        self.assertGreaterEqual(right.poll_calls, 6)

    def test_one_camera_failure_stops_the_other(self):
        left = FakeProcess(returncode=1)

        def stop_right(proc):
            proc.returncode = 1

        right = FakeProcess(on_terminate=stop_right)
        clock = FakeClock()
        stop = dual.StopFlag()

        result = make_supervisor(stop, clock).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertEqual(right.terminate_calls, 1)
        self.assertEqual(right.wait_calls, 1)
        self.assertIsNotNone(result["error"])
        self.assertIn("cam_left", result["error"])
        self.assertFalse(result["forced"])
        self.assertEqual(dual.exit_code_for(result), 1)

    def test_operator_stop_with_verified_recorders_exits_zero(self):
        left = FakeProcess(on_terminate=lambda proc: setattr(proc, "returncode", 0))
        right = FakeProcess(on_terminate=lambda proc: setattr(proc, "returncode", 0))
        clock = FakeClock()
        stop = dual.StopFlag()
        stop.set("SIGINT")

        result = make_supervisor(stop, clock).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertTrue(result["operator_stop"])
        self.assertEqual(result["stop_reason"], "SIGINT")
        self.assertFalse(result["forced"])
        self.assertEqual(dual.exit_code_for(result), 0)

    def test_stop_timeout_requires_kill_and_returns_error(self):
        left = FakeProcess()
        right = FakeProcess()
        clock = FakeClock()
        stop = dual.StopFlag()
        stop.set("SIGTERM")

        result = make_supervisor(stop, clock, stop_grace=1.0).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertTrue(result["forced"])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["codes"]["cam_left"], -9)
        self.assertEqual(left.kill_calls, 1)
        self.assertEqual(right.kill_calls, 1)
        self.assertEqual(left.wait_calls, 1)
        self.assertEqual(right.wait_calls, 1)
        self.assertEqual(dual.exit_code_for(result), 1)

    def test_one_clean_exit_then_hang_is_killed_and_reported(self):
        left = FakeProcess(returncode=0)
        right = FakeProcess()
        clock = FakeClock()
        stop = dual.StopFlag()

        result = make_supervisor(stop, clock, exit_grace=1.0).run(
            [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertEqual(left.terminate_calls, 0)
        self.assertEqual(right.kill_calls, 1)
        self.assertTrue(result["forced"])
        self.assertIn("таймаут", result["error"])
        self.assertEqual(dual.exit_code_for(result), 1)

    def test_unexpected_exception_shuts_down_running_children(self):
        class ExplodingProcess(FakeProcess):
            def __init__(self):
                super().__init__()
                self.exploded = False

            def poll(self):
                if not self.exploded:
                    self.exploded = True
                    raise RuntimeError("poll failed")
                return super().poll()

        left = ExplodingProcess()
        right = FakeProcess()
        stop = dual.StopFlag()

        with self.assertRaises(RuntimeError):
            make_supervisor(stop, FakeClock()).run(
                [make_child("cam_left", left), make_child("cam_right", right)])

        self.assertEqual(left.terminate_calls, 1)
        self.assertEqual(right.terminate_calls, 1)
        self.assertEqual(left.wait_calls, 1)
        self.assertEqual(right.wait_calls, 1)


class ExitCodeTests(unittest.TestCase):
    def test_only_two_zero_codes_give_zero(self):
        self.assertEqual(dual.exit_code_for(
            {"codes": {"cam_left": 0, "cam_right": 0},
             "forced": False, "error": None}), 0)
        self.assertEqual(dual.exit_code_for(
            {"codes": {"cam_left": 0, "cam_right": 1},
             "forced": False, "error": None}), 1)
        self.assertEqual(dual.exit_code_for(
            {"codes": {"cam_left": 0, "cam_right": 0},
             "forced": True, "error": None}), 1)
        self.assertEqual(dual.exit_code_for(
            {"codes": {"cam_left": 0, "cam_right": 0},
             "forced": False, "error": "timeout"}), 1)


@unittest.skipUnless(os.name == "posix", "Linux-only: start_new_session и flock")
class LinuxProcessTests(unittest.TestCase):
    def test_children_start_in_new_session(self):
        seen = []

        def popen(argv, **kwargs):
            seen.append(kwargs.get("start_new_session"))
            return FakeProcess(returncode=0)

        commands = [
            {"key": "cam_left", "log_name": "cam-left.log", "argv": ["left"]},
            {"key": "cam_right", "log_name": "cam-right.log", "argv": ["right"]},
        ]
        with tempfile.TemporaryDirectory() as temp:
            dual.start_children(commands, temp, popen=popen, stop_grace=1.0,
                                clock=FakeClock(), sleep=lambda seconds: None)

        self.assertEqual(seen, [True, True])

    def test_second_wrapper_lock_is_busy_until_released(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "record.lock"
            first, status = dual.acquire_wrapper_lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(status)
            try:
                second, second_status = dual.acquire_wrapper_lock(path)
                self.assertIsNone(second)
                self.assertEqual(second_status, "busy")
            finally:
                first.close()

            third, third_status = dual.acquire_wrapper_lock(path)
            self.assertIsNotNone(third)
            self.assertIsNone(third_status)
            third.close()


if __name__ == "__main__":
    unittest.main()
