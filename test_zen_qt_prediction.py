"""Real widget/model contracts for editable four-point prediction paths."""

import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_scan_path import generate_path


class PredictionUiTests(unittest.TestCase):
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
        self.node = next(
            n for n in self.p._body["program_steps"] if n["kind"] == "measure_angle"
        )
        self.p._selected_step_id = self.node["step_id"]
        self.p.refresh_params()
        self.addCleanup(self.close)

    def close(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.p._dirty = False
        self.w.close()

    def test_generate_select_four_corners_and_reload(self):
        self.p.replace_points(self.node, "points_um", generate_path(0, 0, 30000, 30000))
        self.p.select_corners(self.node, "points_um")
        self.assertEqual(self.node["params"]["prescan_indices"], [0, 4, 15, 19])
        self.assertEqual(self.p.point_table.columnCount(), 4)
        self.p.point_table.item(4, 3).setCheckState(Qt.Unchecked)
        self.assertEqual(self.node["params"]["prescan_indices"], [0, 15, 19])
        self.assertTrue(self.p.save_new_version())
        self.assertEqual(
            self.p.library.get(self.p.current_id, self.p.current_rev)["body"][
                "program_steps"
            ][0]["params"]["prescan_indices"],
            [0, 15, 19],
        )

    def test_paste_replaces_path_clears_selection_and_rejects_duplicates_atomically(
        self,
    ):
        self.p.select_corners(self.node, "points_um")
        with patch(
            "zen_qt.pages.recipe_steps.QInputDialog.getMultiLineText",
            return_value=("1\t2\n3\t4", True),
        ):
            self.p.paste_points(self.node, "points_um")
        self.assertEqual(self.node["params"]["points_um"], [[1, 2, ""], [3, 4, ""]])
        self.assertEqual(self.node["params"]["prescan_indices"], [])
        with self.assertRaises(ValueError):
            self.p.replace_points(self.node, "points_um", [[1, 2, ""]], append=True)
        self.assertEqual(len(self.node["params"]["points_um"]), 2)

    def test_generator_never_moves_and_invalid_preview_cannot_apply(self):
        from zen_qt.pages.path_tools import PathDialog

        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        dialog = PathDialog(self.p)
        dialog.show()
        self.p.path_dialog = dialog
        self.assertIsNone(dialog.points)
        for edit, value in zip(dialog.coords, ("0", "0", "30000", "30000")):
            edit.setText(value)
        self.assertEqual(len(dialog.points), 20)
        dialog.read_endpoint(0)
        self.assertTrue(_wait(lambda: dialog.coords[0].text() == "0.000000"))
        self.assertEqual(self.w.workflow.device.moves, [])
        self.assertEqual(self.w.workflow.device.z_moves, [])
        dialog.close()

    def test_initial_z_teach_uses_only_z_and_rejects_stale_response(self):
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))

        def forbidden():
            raise AssertionError("Z teach must not read XY")

        self.w.workflow.device.read_xy = forbidden
        self.p.teach_initial_z()
        self.assertTrue(_wait(lambda: self.node["params"]["z_um"] == 100))
        self.assertEqual(self.w.workflow.device.z_moves, [])

        import json

        token = (
            self.p.current_id,
            self.p.current_rev,
            self.node["step_id"],
            json.dumps(self.node["params"], sort_keys=True),
        )
        self.node["params"]["z_um"] = 123
        self.p.on_rpc_result(
            "prediction_initial_z",
            dict(
                target=token,
                device=id(self.w.workflow.device),
                generation=getattr(self.w.workflow, "action_generation", 0),
                z=9e-6,
            ),
        )
        self.assertEqual(self.node["params"]["z_um"], 123)

    def test_prediction_checkbox_and_two_phase_parameters_save_as_draft(self):
        from PySide6.QtWidgets import QCheckBox

        box = next(
            c
            for c in self.p.findChildren(QCheckBox)
            if c.text() == "启用四点预扫描平面预测"
        )
        box.click()
        self.assertTrue(self.node["params"]["predict_plane"])
        self.p.select_corners(self.node, "points_um")
        self.p.param_edits["prescan_half_um"].setText("20")
        self.p.param_edits["half_um"].setText("10")
        self.assertTrue(self.p.save_new_version())
        self.p.check_recipe()
        v = self.p.library.get(self.p.current_id, self.p.current_rev)
        self.assertEqual(v["checks"]["result"], "passed")
        params = v["body"]["program_steps"][0]["params"]
        self.assertTrue(params["predict_plane"])
        self.assertEqual((params["prescan_half_um"], params["half_um"]), (20, 10))
