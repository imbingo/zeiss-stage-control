"""Run centre uses selected recipe revisions, with no cross-page configuration."""

import os, tempfile, unittest, json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_recipe_store import RecipeLibrary
from zen_qt.pages.records import scan_runs


class QtSequenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.w.z_timer.stop()
        self.p = self.w.pages["recipes"]
        self.p.seed_demo()
        self.w.sample_edit.setText("W-001")
        self.w.operator_edit.setText("tester")
        self.w.connect_demo("sequence")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.c = self.w.pages["sequence"]
        self.c.select_recipe(self.p.current_id, self.p.current_rev, mode="simulation")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p._dirty = False
        if self.w.workflow.busy:
            self.w.workflow.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.w.close()

    def start(self):
        self.c.preflight()
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.assertIsNotNone(self.c._checked, self.c.status.text())
        self.c.path_confirm.setChecked(True)
        self.c.load_confirm.setChecked(True)
        self.c.start()
        self.assertTrue(_wait(lambda: not self.w.workflow.busy, 30))
        _app().processEvents()

    def test_changed_recipe_controls_move_and_records_same_revision(self):
        b = self.p._body
        b["step_tree"][4]["children"][0]["params"]["x_um"] = 4321
        self.p.mark_dirty()
        self.assertTrue(self.p.save_new_version())
        self.c.select_recipe(self.p.current_id, 1, mode="simulation")
        self.start()
        w = self.w.workflow
        self.assertEqual(w.sequence_report["state"], "complete")
        self.assertAlmostEqual(w.device.xy[0], 0.004321)
        report = json.loads(
            (w.sequence_run_path / "snapshot.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            report["plan"]["recipe_identity"]["content_sha256"],
            self.p.library.get(self.p.current_id, 1)["content_sha256"],
        )
        self.assertEqual(scan_runs(self.w.data_root)[0]["n_images"], 2)

    def test_revisiting_center_keeps_execution_progress(self):
        self.start()
        before = [
            self.c.steps.item(r, 1).text() for r in range(self.c.steps.rowCount())
        ]
        self.w.navigate("recipes")
        self.w.navigate("sequence")
        self.assertEqual(
            before,
            [self.c.steps.item(r, 1).text() for r in range(self.c.steps.rowCount())],
        )
        self.assertIn("完成", before)

    def test_capture_only_no_af_calls(self):
        self.p.template.setCurrentText("仅上下孔采图")
        self.p.save_new_version()
        self.c.select_recipe(self.p.current_id, 1, mode="simulation")
        self.start()
        self.assertEqual(self.w.workflow.device.af_attempts, 0)
        self.assertEqual(self.w.workflow.sequence_report["state"], "complete")
        self.assertIsNone(self.w.workflow.sequence_report["result"])

    def test_edit_sample_invalidates_readonly_check(self):
        self.c.preflight()
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.assertIsNotNone(self.c._checked)
        self.w.sample_edit.setText("W-002")
        self.assertIsNone(self.c._checked)
        self.assertFalse(self.c.start_button.isEnabled())

    def test_production_rejects_draft_and_real_mode_rejects_simulator(self):
        self.c.mode.setCurrentIndex(0)
        self.c.preflight()
        self.assertIsNone(self.c._checked)
        self.assertIn("已放行", self.c.status.text())
        self.c.mode.setCurrentIndex(1)
        self.c.preflight()
        self.assertIn("模拟设备", self.c.status.text())

    def test_frozen_plan_survives_library_edits_after_start(self):
        self.c.preflight()
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.c.path_confirm.setChecked(True)
        self.c.load_confirm.setChecked(True)
        self.c.start()
        b = self.p._body
        b["step_tree"][4]["children"][0]["params"]["x_um"] = 9876
        self.p.library.save_draft(self.p.current_id, 1, b, by="tester", note="next run")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy, 30))
        self.assertAlmostEqual(self.w.workflow.device.xy[0], 0.004)
        self.assertNotEqual(
            self.w.workflow.sequence_report["recipe_identity"]["content_sha256"],
            self.p.library.get(self.p.current_id, 1)["content_sha256"],
        )
