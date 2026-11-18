"""Real offscreen dialog: async import/export, stale-data invalidation, no hardware."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_zen_qt_smoke import _app, _wait
from zen_qt.pages.stack_data import StackDataDialog
from zen_stack_data import write_stack_xml
import numpy as np
import tifffile


class StackDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.d = StackDataDialog()
        self.d.show()
        self.addCleanup(self.d.close)
        for i in range(3):
            tifffile.imwrite(self.root / f"{i}.tif", np.full((3, 4), i, dtype="u2"))
        self.source = self.root / "stack.xml"
        write_stack_xml(
            self.source,
            [dict(file=f"{i}.tif", z_um=100 + i * 0.4) for i in range(3)],
            width=4,
            height=3,
            scale_x_um=0.2,
            scale_y_um=0.2,
            z_source="metadata",
        )

    def test_load_export_and_parameter_change_clears_old_result(self):
        self.d.read_path(self.source)
        self.assertTrue(_wait(lambda: not self.d.busy))
        self.assertIsNotNone(self.d.stack, self.d.status.text())
        self.assertEqual(self.d.table.item(1, 1).text(), "100.4")
        self.assertTrue(self.d.export_button.isEnabled())
        self.d.export_to(self.root / "out")
        self.assertTrue(_wait(lambda: not self.d.busy))
        self.assertTrue((self.root / "out" / "z_um.npy").is_file())
        self.d.indices[1].setValue(1)
        self.assertIsNone(self.d.stack)
        self.assertEqual(self.d.table.rowCount(), 0)
        self.assertFalse(self.d.export_button.isEnabled())

    def test_failed_new_input_does_not_keep_previous_data(self):
        self.d.read_path(self.source)
        self.assertTrue(_wait(lambda: not self.d.busy))
        bad = self.root / "bad.xml"
        bad.write_text("<Experiment/>")
        self.d.read_path(bad)
        self.assertTrue(_wait(lambda: not self.d.busy))
        self.assertIsNone(self.d.stack)
        self.assertFalse(self.d.export_button.isEnabled())
        self.assertIn("失败", self.d.status.text())

    def test_height_generation_export_and_parameter_invalidation(self):
        from test_zen_topography import fixture

        stack, _ = fixture()
        self.d.stack = stack
        self.d.set_busy(False)
        self.d.generate_height()
        self.assertTrue(_wait(lambda: not self.d.busy))
        self.assertIsNotNone(self.d.height_result, self.d.status.text())
        self.assertIn("µrad", self.d.height_view.summary.text())
        self.d.export_height_to(self.root / "height")
        self.assertTrue(_wait(lambda: not self.d.busy))
        self.assertTrue((self.root / "height" / "height_um.tif").is_file())
        self.d.height_view.grid.setValue(4)
        self.assertIsNone(self.d.height_result)
        self.assertFalse(self.d.height_view.export.isEnabled())

    def test_cancelled_late_height_reply_cannot_publish_result(self):
        from test_zen_topography import fixture

        self.d.stack = fixture()[0]
        with patch.object(self.d, "launch"):
            self.d.generate_height()
            self.d.cancel_height()
            self.d.done_result(self.d.tag + ":height", {"old": "result"})
        self.assertIsNone(self.d.height_result)
        self.assertFalse(self.d.height_view.export.isEnabled())
        self.assertIn("取消", self.d.status.text())

    def test_busy_blocks_duplicate_jobs_and_close(self):
        with patch.object(self.d, "launch") as run:
            self.d.read_path(self.source)
            self.d.read_path(self.source)
            self.assertEqual(run.call_count, 1)
            self.d.reject()
            self.assertTrue(self.d.isVisible())
            self.d.failed(self.d.tag + ":read", ValueError("test failure"))
        self.assertFalse(self.d.busy)


if __name__ == "__main__":
    unittest.main()
