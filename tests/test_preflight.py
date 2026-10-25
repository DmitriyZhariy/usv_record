"""Проверки preflight без камер, PyAV и v4l2-ctl."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import record_preflight as preflight  # noqa: E402

MIB = preflight.MIB


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.calls = []

    def _answer(self, kind, device):
        default = get_answer(0) if kind == "get" else FakeCompleted()
        answer = self.answers.get(kind, default)
        if isinstance(answer, dict):
            answer = answer.get(device, default)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def __call__(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        device = argv[argv.index("-d") + 1]
        if "--list-formats-ext" in argv:
            return FakeCompleted(stdout=(
                "[0]: 'MJPG' (Motion-JPEG)\n"
                "    Size: Discrete 1280x720\n"
                "        Interval: Discrete 0.017s (60.000 fps)\n"))
        if f"--set-ctrl={preflight.EXPOSURE_CONTROL}=0" in argv:
            kind = "set"
        elif f"--get-ctrl={preflight.EXPOSURE_CONTROL}" in argv:
            kind = "get"
        else:
            raise AssertionError(f"unexpected command: {argv}")
        return self._answer(kind, device)


def get_answer(value):
    return FakeCompleted(
        0, f"{preflight.EXPOSURE_CONTROL}: {value}\n")


def usage_with_free(free_mib):
    return SimpleNamespace(
        total=10000 * MIB, used=10000 * MIB - free_mib * MIB,
        free=free_mib * MIB)


class ExposureTests(unittest.TestCase):
    def test_set_changes_only_exposure_control(self):
        runner = FakeRunner()
        result = preflight.set_exposure("/dev/video2", runner=runner)
        self.assertEqual(result["exposure_dynamic_framerate"], 0)
        self.assertEqual(len(runner.calls), 1)
        argv, timeout = runner.calls[0]
        self.assertIn("--set-ctrl=exposure_dynamic_framerate=0", argv)
        self.assertEqual(
            [item for item in argv
             if str(item).startswith("--set-ctrl")],
            ["--set-ctrl=exposure_dynamic_framerate=0"])
        self.assertEqual(timeout, preflight.COMMAND_TIMEOUT_S)

    def test_set_failure_is_rejected(self):
        runner = FakeRunner({
            "set": FakeCompleted(1, "", "cannot set control"),
        })
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.set_exposure("/dev/video2", runner=runner)
        self.assertIn("cannot set control", str(caught.exception))

    def test_set_timeout_is_rejected(self):
        runner = FakeRunner({"set": subprocess.TimeoutExpired("v4l2-ctl", 5)})
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.set_exposure("/dev/video2", runner=runner)
        self.assertIn("таймаут", str(caught.exception))

    def test_read_back_zero_is_accepted(self):
        runner = FakeRunner({"get": get_answer(0)})
        result = preflight.verify_exposure("/dev/video2", runner=runner)
        self.assertEqual(result["exposure_dynamic_framerate"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("--get-ctrl=exposure_dynamic_framerate",
                      runner.calls[0][0])
        self.assertFalse(any(
            any(str(item).startswith("--set-ctrl")
                for item in argv) for argv, _ in runner.calls))

    def test_read_back_nonzero_is_rejected(self):
        runner = FakeRunner({"get": get_answer(1)})
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.verify_exposure("/dev/video2", runner=runner)
        self.assertIn("после установки", str(caught.exception))
        self.assertIn("требуется 0", str(caught.exception))

    def test_command_failure_is_rejected(self):
        runner = FakeRunner({
            "get": FakeCompleted(1, "", "control not found"),
        })
        with self.assertRaises(preflight.PreflightError):
            preflight.verify_exposure("/dev/video2", runner=runner)

    def test_missing_v4l2_ctl_is_rejected(self):
        runner = FakeRunner({"set": FileNotFoundError("v4l2-ctl")})
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.set_exposure(
                "/dev/video2", v4l2_ctl="/missing/v4l2-ctl", runner=runner)
        self.assertIn("v4l2-ctl", str(caught.exception))

    def test_unparseable_read_back_is_rejected(self):
        runner = FakeRunner({"get": FakeCompleted(0, "something else\n")})
        with self.assertRaises(preflight.PreflightError):
            preflight.verify_exposure("/dev/video2", runner=runner)


class DiskTests(unittest.TestCase):
    def test_enough_space_and_directory_is_created(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "recordings"
            info = preflight.ensure_output_root(
                root, 100, usage=lambda path: usage_with_free(200))
            self.assertTrue(root.is_dir())
            self.assertEqual(info["free_mib"], 200.0)

    def test_low_space_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(preflight.PreflightError) as caught:
                preflight.ensure_output_root(
                    temp, 4096,
                    usage=lambda path: usage_with_free(100))
            self.assertIn("требуется", str(caught.exception))

    def test_usage_error_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            def broken(path):
                raise OSError("disk disappeared")
            with self.assertRaises(preflight.PreflightError):
                preflight.ensure_output_root(temp, 1, usage=broken)

    def test_file_instead_of_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "not-a-dir"
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(preflight.PreflightError):
                preflight.ensure_output_root(
                    path, 1, usage=lambda value: usage_with_free(100))


@unittest.skipUnless(os.name == "posix", "fake /proc symlinks need POSIX")
class HolderTests(unittest.TestCase):
    def test_holder_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            device = Path(temp) / "video2"
            device.write_text("", encoding="utf-8")
            proc = Path(temp) / "proc"
            fd_dir = proc / "123" / "fd"
            fd_dir.mkdir(parents=True)
            os.symlink(str(device), str(fd_dir / "3"))
            holders = preflight.find_device_holders(device, proc_root=proc)
            self.assertEqual(holders, [123])

    def test_empty_proc_has_no_holders(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp) / "proc"
            (proc / "1").mkdir(parents=True)
            self.assertEqual(
                preflight.find_device_holders("/dev/video2", proc_root=proc),
                [])


class RunPreflightTests(unittest.TestCase):
    def _runner(self):
        return FakeRunner()

    def test_success_sets_then_reads_both_cameras(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self._runner()
            report = preflight.run_preflight(
                temp, ["/dev/video2", "/dev/video4"],
                min_free_mib=100, critical_free_mib=10,
                runner=runner, usage=lambda path: usage_with_free(500),
                proc_root=str(Path(temp) / "empty-proc"))
            self.assertEqual(len(report["cameras"]), 2)
            commands = [
                (("set" if any(str(item).startswith("--set-ctrl")
                               for item in argv) else "get"),
                 argv[argv.index("-d") + 1])
                for argv, _ in runner.calls
                if "--list-formats-ext" not in argv
            ]
            self.assertEqual(commands, [
                ("set", "/dev/video2"), ("set", "/dev/video4"),
                ("get", "/dev/video2"), ("get", "/dev/video4"),
            ])
            self.assertTrue(all(
                timeout == preflight.COMMAND_TIMEOUT_S
                for _, timeout in runner.calls))
            self.assertEqual(report["space"]["free_mib"], 500.0)

    def test_set_failure_blocks_both_cameras_before_read_back(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner({
                "set": {
                    "/dev/video2": FakeCompleted(),
                    "/dev/video4": FakeCompleted(1, "", "device busy"),
                },
            })
            with self.assertRaises(preflight.PreflightError):
                preflight.run_preflight(
                    temp, ["/dev/video2", "/dev/video4"],
                    min_free_mib=100, critical_free_mib=10,
                    runner=runner, usage=lambda path: usage_with_free(500),
                    proc_root=str(Path(temp) / "empty-proc"))
            kinds = [
                "set" if any(str(item).startswith("--set-ctrl")
                             for item in argv) else "get"
                for argv, _ in runner.calls
                if "--list-formats-ext" not in argv
            ]
            self.assertEqual(kinds, ["set", "set"])

    def test_nonzero_read_back_blocks_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = FakeRunner({
                "get": {
                    "/dev/video2": get_answer(0),
                    "/dev/video4": get_answer(1),
                },
            })
            with self.assertRaises(preflight.PreflightError) as caught:
                preflight.run_preflight(
                    temp, ["/dev/video2", "/dev/video4"],
                    min_free_mib=100, critical_free_mib=10,
                    runner=runner, usage=lambda path: usage_with_free(500),
                    proc_root=str(Path(temp) / "empty-proc"))
            self.assertIn("/dev/video4", str(caught.exception))

    def test_busy_device_blocks_before_exposure(self):
        with tempfile.TemporaryDirectory() as temp:
            device = Path(temp) / "video2"
            device.write_text("", encoding="utf-8")
            proc = Path(temp) / "proc"
            fd_dir = proc / "77" / "fd"
            fd_dir.mkdir(parents=True)
            if os.name == "posix":
                os.symlink(str(device), str(fd_dir / "9"))
            else:
                self.skipTest("symlink diag test is POSIX-only")
            runner = self._runner()
            with self.assertRaises(preflight.PreflightError) as caught:
                preflight.run_preflight(
                    temp, [str(device)], min_free_mib=1,
                    runner=runner, usage=lambda path: usage_with_free(100),
                    proc_root=str(proc))
            self.assertIn("открыто", str(caught.exception))
            self.assertEqual(runner.calls, [])

    def test_critical_must_be_below_start_threshold(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(preflight.PreflightError):
                preflight.run_preflight(
                    temp, [], min_free_mib=100, critical_free_mib=100,
                    runner=self._runner(),
                    usage=lambda path: usage_with_free(1000))


if __name__ == "__main__":
    unittest.main()
