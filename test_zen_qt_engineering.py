"""Visible engineering workflow improvements requested during real-machine use."""

import tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_templates import TemplateStore


class EngineeringUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.w.z_timer.stop()
        self.addCleanup(self.close)

    def close(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.w.pages["recipes"]._dirty = False
        self.w.close()

    def demo(self):
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        return self.w.workflow

    def test_z_teach_from_transport_ignores_even_misconfigured_xy(self):
        w = self.demo()
        w.state = "通信就绪（硬件单步）"
        w.device.xy_enabled = True
        w.device.z = 42e-6

        def forbidden():
            raise AssertionError("XY must not be read")

        w.device.read_xy = w.device.motion_state = forbidden
        self.w.pages["marks"].teach_z("upper")
        self.assertTrue(
            _wait(lambda: self.w.pages["marks"].layers["upper"]["z"].value() == 42)
        )
        self.assertAlmostEqual(self.w._live_z, 42e-6)

    def test_directory_has_no_elision_and_horizontal_scroll(self):
        page = self.w.pages["hardware"]
        self.assertEqual(page.tree.textElideMode(), Qt.ElideNone)
        self.assertEqual(page.tree.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertFalse(page.tree.header().stretchLastSection())

    def test_flow_drag_order_updates_actual_recipe(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        before = [n["step_id"] for n in p._body["program_steps"]]
        root = p.step_tree.invisibleRootItem()
        item = root.takeChild(1)
        root.insertChild(2, item)
        p.step_tree.setCurrentItem(item)
        p.accept_tree_order()
        after = [n["step_id"] for n in p._body["program_steps"]]
        self.assertEqual(after[:2], before[1::-1])
        p.param_edits['experiment'].setText('AfterDrag')
        self.assertEqual(p._body['program_steps'][1]['params']['experiment'],'AfterDrag')
        self.assertEqual(p.step_tree.dragDropMode(), QAbstractItemView.InternalMove)

    def test_saving_custom_template_is_available_after_restart(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        with patch(
            "zen_qt.pages.recipe_tools.QInputDialog.getText",
            return_value=("现场流程", True),
        ):
            p.save_template()
        rows = TemplateStore(self.w.data_root / "step_templates.json").load()
        self.assertEqual(rows[0]["name"], "现场流程")
        p._body["program_steps"] = []
        with patch(
            "zen_qt.pages.recipe_tools.QInputDialog.getItem",
            return_value=(f"自定义 · 现场流程 · {rows[0]['id'][:4]}", True),
        ):
            p.choose_template()
        self.assertGreater(len(p._body["program_steps"]), 0)

    def test_delete_recipe_refreshes_list_and_execution_preview(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        rid = p.current_id
        p.delete_recipe(rid)
        self.assertEqual(p.library.recipes(), [])
        self.assertEqual(self.w.pages["sequence"].recipes.count(), 0)
        self.assertEqual(p.step_tree.topLevelItemCount(), 0)
        self.assertEqual(p.param_edits, {})

    def test_help_is_specific_to_selected_action(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        p.add_kind("capture_upper")
        self.assertEqual(p.tabs.tabText(1), "步骤使用说明")
        self.assertIn("不会自动移动", p.conditions.toPlainText())
        p.add_kind("move_xy")
        self.assertIn("转场 Z", p.conditions.toPlainText())

    def test_recovery_questionnaire_is_replaced_by_live_check(self):
        w = self.demo()
        w.state = "设备状态待确认"
        with patch("zen_qt.app.RecoveryDialog") as dialog:
            self.w.open_recovery()
            self.assertTrue(_wait(lambda: not w.busy))
            dialog.assert_not_called()
        self.assertEqual(w.state, "就绪")

    def test_batch_dialog_selects_only_checked_and_has_stop_controls(self):
        from zen_qt.pages.hardware_batch import BatchDialog

        self.demo()
        p = self.w.pages["hardware"]
        p.bind_current()
        d = BatchDialog(p)
        self.assertFalse(d.empty.isChecked())
        self.assertTrue(d.zero_z.isChecked())
        self.assertTrue(hasattr(p, "batch_button"))
        self.assertTrue(hasattr(p, "stop_button"))
        d.close()
