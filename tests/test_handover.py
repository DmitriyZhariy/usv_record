"""Проверки параметров передачи: таймауты, unit и состав runtime.

Тест не открывает камеры и не требует PyAV. Он фиксирует значения,
которые были увеличены после полевого отказа проверки на длинной записи.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import control_panel  # noqa: E402
import record_dual_session  # noqa: E402


RUNTIME_MODULES = (
    "control_panel.py",
    "panel_page.py",
    "preview_dual_mjpeg.py",
    "record_profiles.py",
    "record_preflight.py",
    "record_dual_session.py",
    "record_camera_pyav_queue.py",
    "recording_preview.py",
)


class TimeoutPreservationTests(unittest.TestCase):
    def test_long_verification_timeouts_are_preserved(self):
        self.assertEqual(record_dual_session.STOP_GRACE_SECONDS, 3600.0)
        self.assertEqual(record_dual_session.EXIT_GRACE_SECONDS, 3600.0)
        self.assertEqual(control_panel.STOP_WAIT_SECONDS, 3660.0)


class DeploymentFilesTests(unittest.TestCase):
    def test_unit_and_dropin_use_verified_release_path(self):
        unit = (ROOT / "deploy" / "usv-panel.service").read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=/root/usv_record", unit)
        self.assertIn(
            "ExecStart=/root/recorder/venv-pyav14/bin/python "
            "/root/usv_record/control_panel.py",
            unit,
        )
        self.assertNotIn("/root/recorder-profile-test-20261025-080008", unit)
        dropin = (ROOT / "deploy" / "usv-panel.service.d" /
                  "20-verification-timeout.conf").read_text(encoding="utf-8")
        self.assertIn("TimeoutStopSec=3720s", dropin)

    def test_all_runtime_modules_exist(self):
        missing = [name for name in RUNTIME_MODULES
                   if not (ROOT / name).is_file()]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
