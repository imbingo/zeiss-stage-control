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
        self.assertTrue(self.p.xy.isEnabled())
        self.p.move('xy')
        self.assertEqual(self.w.workflow.device.moves,[])
        self.assertIn('确认',self.p.message.text())

    def test_read_position_can_seed_target_and_previews_the_displacement(self):
        self.p.nudge(1)
        self.assertIn("读取当前位置", self.p.message.text())
        self.p.read()
        self.assertTrue(_wait(lambda: self.p._position is not None))
        z = self.w.workflow.device.z * 1e6
        self.p.fill_current()
        self.assertEqual(self.p.fields["z"].text(), f"{z:.3f}")
        self.p.step.setText("5")
        self.p.nudge(1)
        self.assertEqual(self.p.fields["z"].text(), f"{z + 5:.3f}")
        self.assertIn("+5.000 µm", self.p.preview.text())
        # Seeding and nudging the target never moves or confirms anything.
        self.assertEqual(self.w.workflow.device.moves, [])
        self.assertFalse(self.p.approved.isChecked())

    def test_limits_are_remembered_but_confirmation_is_not(self):
        self.fill()
        self.p.move("z")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.w.close()
        again = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.addCleanup(again.close)
        p = again.pages["manual"]
        self.assertEqual(
            [p.fields[k].text() for k in ("safe", "low", "high")], ["20", "0", "300"]
        )
        self.assertEqual(p.fields["z"].text(), "")
        self.assertFalse(p.approved.isChecked())

    def test_settings_save_updates_output_without_changing_recipe(self):
        settings = self.w.pages["settings"]
        settings.image_path.setText(str(Path(self.tmp.name) / "images"))
        settings.save()
        self.assertEqual(
            self.w.pages["sequence"].output.text(), str(Path(self.tmp.name) / "images")
        )
        self.assertEqual(len(self.w.pages["recipes"].library.recipes()), 0)
