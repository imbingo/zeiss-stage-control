"""Actual widget edits must change the selected recipe, not stale copies."""

import os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_recipe_store import RecipeLibrary


class TestRecipesPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.p = self.w.pages["recipes"]
        self.p.seed_demo()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p._dirty = False
        if self.w.workflow and self.w.workflow.busy:
            self.w.workflow.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.w.close()

    def select(self, sid):
        root = self.p.step_tree.invisibleRootItem()

        def walk(n):
            for i in range(n.childCount()):
                c = n.child(i)
                v = c.data(0, Qt.UserRole)
                if v and v[0] == sid:
                    return c
                result = walk(c)
                if result:
                    return result

        self.p.step_tree.setCurrentItem(walk(root))
        _app().processEvents()

    def test_change_save_reload_preserves_coordinate_and_revision(self):
        self.select("H01")
        self.p.param_edits["x_um"].setText("4321")
        self.assertTrue(self.p.save_new_version())
        v = RecipeLibrary(self.p.library.path).get(self.p.current_id, 1)
        self.assertEqual(
            v["body"]["step_tree"][4]["children"][0]["params"]["x_um"], 4321
        )

    def test_switch_step_preserves_edit_and_displays_correct_fields(self):
        self.select("H01")
        self.p.param_edits["x_um"].setText("8765")
        self.select("S1")
        self.assertIn("af_half_um", self.p.param_edits)
        self.assertNotIn("upper_z_um", self.p.param_edits)
        self.select("H01")
        self.assertEqual(self.p.param_edits["x_um"].text(), "8765")
        self.w.connect_demo("sequence")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.select("S1")
        self.p.point_table.selectRow(0)
        self.p.teach_tilt()
        self.assertTrue(_wait(lambda: len(self.p._body["tilt"]["points_um"][0]) == 3))
        self.assertEqual(self.p._body["tilt"]["points_um"][0], [0.0, 0.0, 100.0])

    def test_check_saves_current_edits_and_never_fakes_qualification(self):
        self.select("H01")
        self.p.param_edits["x_um"].setText("7654")
        self.p.check_recipe()
        v = self.p.library.get(self.p.current_id, 1)
        self.assertEqual(
            v["body"]["step_tree"][4]["children"][0]["params"]["x_um"], 7654
        )
        self.assertFalse(self.p.release_button.isEnabled())

    def test_invalid_numeric_save_reports_and_keeps_last_saved_value(self):
        self.select("H01")
        self.p.param_edits["x_um"].setText("invalid")
        with patch("zen_qt.pages.recipes.QMessageBox.warning") as warning:
            self.assertFalse(self.p.save_new_version())
            warning.assert_called_once()
        self.assertEqual(
            self.p.library.get(self.p.current_id, 1)["body"]["step_tree"][4][
                "children"
            ][0]["params"]["x_um"],
            4000,
        )

    def test_template_updates_step_dependencies(self):
        self.p.template.setCurrentText("仅上下孔采图")
        self.assertNotIn("tilt", [n["kind"] for n in self.p._body["step_tree"]])
        self.p.check_recipe()
        self.assertEqual(
            self.p.library.get(self.p.current_id, 1)["checks"]["result"], "passed"
        )
        self.w.preferences.update(
            sequence={
                "entry_unit": "um",
                "fields": {
                    "name": "旧自动配置",
                    "safe_z_um": "25",
                    "z_min_um": "0",
                    "z_max_um": "300",
                    "tilt_z_um": "100",
                    "max_angle_urad": "123",
                },
                "rows": [["H01", "孔位", "4321", "1000", "100", "200"]],
            }
        )
        self.p.import_legacy(str(self.w.preferences.path))
        self.assertEqual(self.p._body["common_params"]["safe_z_um"], "25")
        self.assertEqual(self.p._body["common_params"]["max_rx_urad"], "123")
        self.assertEqual(
            self.p._body["step_tree"][4]["children"][0]["params"]["x_um"], "4321"
        )

    def test_cancel_switch_and_close_retains_unsaved_draft(self):
        self.select("H01")
        self.p.param_edits["x_um"].setText("999")
        with patch(
            "zen_qt.pages.recipes.QMessageBox.question", return_value=QMessageBox.Cancel
        ):
            self.assertFalse(self.p._resolve_dirty())
        self.assertTrue(self.p._dirty)

    def test_no_automatic_released_example(self):
        self.assertFalse(
            any(
                v["status"] == "released"
                for r in self.p.library.recipes()
                for v in r["revisions"]
            )
        )

    def test_add_reorder_delete_holes_keeps_protected_steps(self):
        before = [n["kind"] for n in self.p._body["step_tree"]]
        self.p.add_hole()
        self.p.move_hole(-1)
        self.p.delete_hole()
        self.assertEqual(before, [n["kind"] for n in self.p._body["step_tree"]])
        self.assertEqual(len(self.p._body["step_tree"][4]["children"]), 1)
