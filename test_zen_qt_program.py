"""UI contracts for preview, explicit step scope, live charts and window chrome."""

import tempfile, unittest
from pathlib import Path
from PySide6.QtCore import Qt
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_steps import make_step


class ProgramUiTests(unittest.TestCase):
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
        self.c = self.w.pages["sequence"]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.p._dirty = False
        self.w.close()

    def select(self, **kw):
        self.c.select_recipe(self.p.current_id, self.p.current_rev, **kw)

    def test_actual_window_is_frameless_and_brand_is_lsm(self):
        self.assertTrue(self.w.windowFlags() & Qt.FramelessWindowHint)
        self.assertIn("LSM测量工作站", self.w.windowTitle())
        self.assertEqual(self.c.tabs.count(), 2)
        self.assertTrue(
            self.w.chrome.edges(
                __import__("PySide6.QtCore", fromlist=["QPoint"]).QPoint(1, 1)
            )
            & Qt.LeftEdge
        )

    def test_invalid_draft_preview_is_visible_without_connection(self):
        self.p._body["program_steps"][0]["params"]["experiment"] = ""
        self.p.mark_dirty()
        self.assertTrue(self.p.save_new_version())
        self.select(mode="production")
        self.assertEqual(
            self.c.steps.topLevelItemCount(), len(self.p._body["program_steps"])
        )
        self.c.preflight()
        self.assertIsNone(self.c._checked)
        self.assertGreater(self.c.steps.topLevelItemCount(), 0)

    def test_single_move_never_implicitly_loads_experiment(self):
        target = next(n for n in self.p._body["program_steps"] if n["kind"] == "move_z")
        self.select(step=target["step_id"])
        plan = self.c.build_plan()
        self.assertEqual([n["kind"] for n in plan.steps], ["move_z"])
        self.assertEqual(self.c.steps.topLevelItemCount(), 1)

    def test_single_capture_displays_its_explicit_load_and_nothing_else(self):
        target = next(
            n for n in self.p._body["program_steps"] if n["kind"] == "capture_upper"
        )
        self.select(step=target["step_id"])
        self.assertEqual(
            [n["kind"] for n in self.c.build_plan().steps],
            ["load_experiment", "capture_upper"],
        )
        self.assertEqual(self.c.steps.topLevelItemCount(), 2)
        load = next(
            n
            for n in reversed(self.p._body["program_steps"])
            if n["kind"] == "load_experiment"
        )
        self.select(step=load["step_id"], from_here=True)
        plan = self.c.build_plan()
        self.assertEqual(plan.steps[0]["step_id"], load["step_id"])
        self.assertEqual(sum(n["kind"] == "load_experiment" for n in plan.steps), 1)

    def test_from_here_revalidates_missing_results_and_shows_actual_scope(self):
        target = next(
            n for n in self.p._body["program_steps"] if n["kind"] == "angle_gate"
        )
        self.select(step=target["step_id"], from_here=True)
        with self.assertRaises(ValueError):
            self.c.build_plan()
        self.assertEqual(
            self.c.steps.topLevelItemCount(), len(self.p._body["program_steps"]) - 1
        )

    def test_measured_scatter_uses_real_event_z_and_new_recipe_clears_it(self):
        self.select()
        sid = self.p._body["program_steps"][0]["step_id"]
        self.c.on_workflow_event("measurement_begin", [{"step_id": sid}], None)
        self.c.on_workflow_event(
            "measurement_point",
            [{"step_id": sid, "x_um": 3, "y_um": 4, "z_um": 123.456}],
            None,
        )
        self.assertEqual(self.c.measure.samples[sid][0]["z_um"], 123.456)
        self.p.seed_demo()
        self.select()
        self.assertEqual(self.c.measure.samples, {})

    def test_no_xy_configuration_allows_pair_program_and_blocks_xy(self):
        self.w.xy_mode = "no_xy"
        self.w.connect_demo("sequence")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.p._body["program_steps"] = [
            make_step("load_experiment", experiment="Upper"),
            make_step("capture_upper"),
        ]
        self.p.mark_dirty()
        self.assertTrue(self.p.save_new_version())
        self.select(mode="simulation")
        self.w.sample_edit.setText("W1")
        self.w.operator_edit.setText("E1")
        self.c.preflight()
        self.assertTrue(_wait(lambda: self.c._checked is not None))
        self.assertFalse(self.w.workflow.xy_enabled)

    def test_gain_is_configurable_but_not_executable(self):
        self.p.add_kind("set_gain")
        step = self.p._body["program_steps"][-1]
        self.assertEqual(step["kind"], "set_gain")
        self.assertIn("value", self.p.param_edits)
        step["params"].update(channel="1", value=500)
        self.p.mark_dirty()
        self.assertTrue(self.p.save_new_version())
        self.select()
        with self.assertRaisesRegex(ValueError, "主增益"):
            self.c.build_plan()
