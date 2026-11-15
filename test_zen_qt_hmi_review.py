"""Operator-facing contracts from the V2.9.1 HMI review: no stale verdicts, no preselected motion."""

import tempfile, unittest
from pathlib import Path
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow


class HmiReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.w.z_timer.stop()
        self.p = self.w.pages["recipes"]
        self.c = self.w.pages["sequence"]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.p._dirty = False
        self.w.close()

    def run_demo(self):
        self.p.seed_demo()
        self.c.demo()
        self.assertTrue(_wait(lambda: self.w.device_ready()))
        self.w.sample_edit.setText("W1")
        self.w.operator_edit.setText("E1")
        self.c.path_confirm.setChecked(True)
        self.c.load_confirm.setChecked(True)
        self.c.start()
        self.assertTrue(_wait(lambda: self.c._verdict_kind in ("ok", "bad", "warn"), timeout=30))
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))

    def test_first_screen_names_the_next_step(self):
        self.assertEqual(self.c._verdict_kind, "idle")
        self.assertIn("连接设备", self.c.verdict.text())
        self.assertIn("未填写", self.c.identity.text())
        self.assertEqual(self.w.conn_chip.text(), "未连接设备")
        self.assertFalse(self.w.mode_chip.isVisibleTo(self.w))

    def test_finished_demo_is_labelled_and_restart_drops_the_old_verdict(self):
        self.run_demo()
        self.assertEqual(self.c._verdict_kind, "ok")
        self.assertIn("离线演示结果", self.c.verdict.text())
        self.assertIn("Rx", self.c.verdict.text())
        self.c.path_confirm.setChecked(True)
        self.c.load_confirm.setChecked(True)
        self.c.start()
        self.assertTrue(_wait(lambda: self.c._verdict_kind == "run", timeout=10))
        self.assertNotIn("完成", self.c.result_label.text())
        self.assertIn("执行中", self.c.result_label.text())
        self.assertTrue(_wait(lambda: self.c._verdict_kind == "ok", timeout=30))

    def test_leaving_the_demo_clears_its_results_from_the_run_page(self):
        self.run_demo()
        self.assertGreater(len(self.c.marks.frames), 0)
        self.w.exit_demo()
        self.assertTrue(_wait(lambda: self.w.workflow is None))
        self.assertEqual(self.c._verdict_kind, "idle")
        self.assertEqual(self.c.marks.frames, {})
        self.assertEqual(self.c.measure.samples, {})
        self.assertNotIn("完成", self.c.result_label.text())
        self.assertIn("结果与追溯", self.c.verdict.text())
        self.assertTrue(
            all(
                self.c.steps.topLevelItem(i).text(1) == "待检查"
                for i in range(self.c.steps.topLevelItemCount())
            )
        )

    def test_stop_control_says_it_is_not_an_emergency_stop(self):
        for button in (self.w.stop_button, self.c.stop_button):
            self.assertIn("停止后续步骤", button.text())
            self.assertIn("不是硬件急停", button.toolTip())
        self.assertTrue(self.w.stop_button.property("idle"))


if __name__ == "__main__":
    unittest.main()
