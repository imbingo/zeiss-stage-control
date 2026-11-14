"""Manual controls share task ownership and durable recovery records."""

import os, tempfile, unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_qt.pages.records import scan_runs


class ManualTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.p = self.w.pages["manual"]
        self.addCleanup(self.w.close)

    def fill(self):
        for k, v in dict(
            x="1000", y="2000", z="150", safe="20", low="0", high="300"
        ).items():
            self.p.fields[k].setText(v)
        self.p.approved.setChecked(True)

    def test_xy_move_goes_through_safe_z_and_is_archived(self):
        self.fill()
        self.p.move("xy")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.assertAlmostEqual(self.w.workflow.device.xy[0], 0.001)
        self.assertAlmostEqual(self.w.workflow.device.z, 20e-6)
        self.assertEqual(scan_runs(self.w.data_root)[0]["state"], "complete")

    def test_invalid_range_does_not_issue_motion(self):
        self.fill()
        self.p.fields["low"].setText("500")
        self.p.approved.setChecked(True)
        self.p.move("xy")
        self.assertEqual(self.w.workflow.device.moves, [])

    def test_parameter_edit_consumes_confirmation(self):
        self.fill()
        self.p.fields["x"].setText("2000")
        self.assertFalse(self.p.approved.isChecked())
        self.assertFalse(self.p.xy.isEnabled())

    def test_settings_save_updates_output_without_changing_recipe(self):
        settings = self.w.pages["settings"]
        settings.image_path.setText(str(Path(self.tmp.name) / "images"))
        settings.save()
        self.assertEqual(
            self.w.pages["sequence"].output.text(), str(Path(self.tmp.name) / "images")
        )
        self.assertEqual(len(self.w.pages["recipes"].library.recipes()), 0)
