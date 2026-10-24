"""Проверки выбора каталога рекордера без PyAV и камер.

Модуль рекордера импортирует `av`; для теста подставляется минимальная
заглушка, чтобы проверить только логику каталогов. Захват, запись,
очередь и верификация здесь не проверяются.
"""

import sys
import tempfile
import types
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAKE_AV = types.ModuleType("av")
with mock.patch.dict(sys.modules, {"av": FAKE_AV}):
    import record_camera_pyav_queue as recorder  # noqa: E402


class OutputFolderTests(unittest.TestCase):
    def _args(self, **overrides):
        values = {
            "camera_id": "cam_left",
            "output_root": None,
            "output_dir": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_output_dir_is_used_exactly(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / "session"
            session.mkdir()
            target = session / "cam-left"
            folder = recorder.prepare_output_folder(
                self._args(output_dir=str(target)))
            self.assertEqual(folder, target)
            self.assertTrue(target.is_dir())

    def test_existing_output_dir_is_rejected_even_if_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "cam-left"
            target.mkdir()
            with self.assertRaises(FileExistsError):
                recorder.prepare_output_folder(
                    self._args(output_dir=str(target)))
            self.assertEqual(list(target.iterdir()), [])

    def test_output_root_is_used_for_queued_dir(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "recordings"
            folder = recorder.prepare_output_folder(
                self._args(output_root=str(root)))
            self.assertEqual(folder.parent, root)
            self.assertTrue(folder.name.startswith("queued_cam_left_"))
            self.assertTrue(folder.is_dir())

    def test_camera_id_is_part_of_queued_name(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "recordings"
            folder = recorder.prepare_output_folder(
                self._args(camera_id="cam_right", output_root=str(root)))
            self.assertTrue(folder.name.startswith("queued_cam_right_"))


if __name__ == "__main__":
    unittest.main()
