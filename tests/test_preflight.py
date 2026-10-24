"""Проверки preflight без камер, PyAV и v4l2-ctl."""

import os
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
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, timeout=None):
        self.calls.append((list(argv), timeout))
        if "--get-ctrl=exposure_dynamic_framerate" not in argv:
            raise AssertionError(f"unexpected command: {argv}")
        answer = self.answers.get("get", FakeCompleted())
        if isinstance(answer, Exception):
            raise answer
        return answer


def usage_with_free(free_mib):
    return SimpleNamespace(
        total=10000 * MIB, used=10000 * MIB - free_mib * MIB,
        free=free_mib * MIB)


class ExposureTests(unittest.TestCase):
    def test_reads_zero_without_changing_camera(self):
        runner = FakeRunner({
            "get": FakeCompleted(0, "exposure_dynamic_framerate: 0\n"),
        })
        result = preflight.verify_exposure("/dev/video2", runner=runner)
        self.assertEqual(result["exposure_dynamic_framerate"], 0)
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("--get-ctrl=exposure_dynamic_framerate",
                      runner.calls[0][0])
        self.assertFalse(any(
            any(str(item).startswith("--set-ctrl")
                for item in argv) for argv, _ in runner.calls))

    def test_read_back_nonzero_is_rejected_with_command(self):
        runner = FakeRunner({
            "get": FakeCompleted(0, "exposure_dynamic_framerate: 1\n"),
        })
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.verify_exposure("/dev/video2", runner=runner)
        self.assertIn("требуется 0", str(caught.exception))
        self.assertIn("--set-ctrl=exposure_dynamic_framerate=0",
                      str(caught.exception))

    def test_command_failure_is_rejected(self):
        runner = FakeRunner({
            "get": FakeCompleted(1, "", "control not found"),
        })
        with self.assertRaises(preflight.PreflightError):
            preflight.verify_exposure("/dev/video2", runner=runner)

    def test_missing_v4l2_ctl_is_rejected(self):
        runner = FakeRunner({"get": FileNotFoundError("v4l2-ctl")})
        with self.assertRaises(preflight.PreflightError) as caught:
            preflight.verify_exposure(
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
        return FakeRunner({
            "get": FakeCompleted(0, "exposure_dynamic_framerate: 0\n"),
        })

    def test_success_checks_both_cameras_without_changing_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = self._runner()
            report = preflight.run_preflight(
                temp, ["/dev/video2", "/dev/video4"],
                min_free_mib=100, critical_free_mib=10,
                runner=runner, usage=lambda path: usage_with_free(500),
                proc_root=str(Path(temp) / "empty-proc"))
            self.assertEqual(len(report["cameras"]), 2)
            self.assertEqual(len(runner.calls), 2)
            self.assertEqual(report["space"]["free_mib"], 500.0)

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
