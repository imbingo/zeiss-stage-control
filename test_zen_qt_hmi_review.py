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
        self.assertIn("未填写", self.w.session_summary.text())
        self.assertTrue(self.c.isAncestorOf(self.w.sample_edit))
        self.assertTrue(self.c.isAncestorOf(self.w.operator_edit))
        self.w.sample_edit.setText("W1")
        self.assertIn("W1", self.w.session_summary.text())
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


class RecordsHmiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def test_record_detail_leads_with_outcome_and_folds_paths_away(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        w = WorkbenchWindow(Path(tmp.name) / "data")
        w.z_timer.stop()
        recipes, c = w.pages["recipes"], w.pages["sequence"]

        def cleanup():
            recipes._dirty = False
            w.close()

        self.addCleanup(cleanup)
        w.show()
        recipes.seed_demo()
        c.demo()
        self.assertTrue(_wait(lambda: w.device_ready()))
        w.sample_edit.setText("W1")
        w.operator_edit.setText("E1")
        c.path_confirm.setChecked(True)
        c.load_confirm.setChecked(True)
        c.start()
        self.assertTrue(_wait(lambda: c._verdict_kind in ("ok", "bad", "warn"), timeout=30))
        self.assertTrue(_wait(lambda: not w.workflow.busy))
        w.navigate("records")
        p = w.pages["records"]
        self.assertEqual(p.table.item(0, 7).text(), "已完成·演示")
        verdict = p.detail_verdict.text()
        self.assertIn("离线演示结果 · 非现场数据", verdict)
        self.assertIn("待 SOMA", verdict)
        self.assertIn("Rx", p.detail_metrics.text())
        self.assertIn("RMS", p.detail_metrics.text())
        # Paths, ids and raw state names are one click away, not in the summary.
        for noise in (str(w.data_root), "complete", "False", "simulation"):
            self.assertNotIn(noise, p.detail.text())
        self.assertFalse(p.detail_more.isVisible())
        p.more_button.click()
        self.assertTrue(p.detail_more.isVisible())
        self.assertIn(str(w.data_root), p.detail_more.text())

    def test_verdict_separates_rejected_failed_stopped_and_unjudged(self):
        from zen_qt.pages.records import run_verdict

        def run(**kw):
            base = dict(state="complete", kind="tilt", valid=True, recovery=False,
                        stage="", sequence={})
            base.update(kw)
            return base

        self.assertEqual(run_verdict(run()), ("ok", "测角结果有效"))
        self.assertEqual(run_verdict(run(valid=False))[0], "warn")
        self.assertEqual(run_verdict(run(state="stopped"))[0], "warn")
        self.assertEqual(run_verdict(run(recovery=True))[0], "warn")
        self.assertEqual(
            run_verdict(run(kind="sequence", sequence={"disposition": "MEASUREMENT_REJECTED"})),
            ("bad", "角度不合格"),
        )
        kind, text = run_verdict(run(state="failed", stage="af"))
        self.assertEqual(kind, "bad")
        self.assertIn("af", text)


class RecipeHmiTests(unittest.TestCase):
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
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p._dirty = False
        self.w.close()

    def select(self, sid):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QTreeWidgetItemIterator

        it = QTreeWidgetItemIterator(self.p.step_tree)
        while it.value():
            if it.value().data(0, Qt.UserRole)[0] == sid:
                self.p.step_tree.setCurrentItem(it.value())
                return
            it += 1
        self.fail(sid)

    def kinds(self):
        return [n["kind"] for n in self.p._body["program_steps"]]

    def test_editing_a_checked_recipe_withdraws_the_tick_until_saved(self):
        self.p.check_recipe()
        self.assertIn("参数检查 ✓", self.p.meta_parent.text())
        self.assertIn("参数检查：通过", self.p.tests.toPlainText())
        self.assertNotIn("{", self.p.tests.toPlainText())
        # A note-only edit does not touch the checked content.
        self.p.change_note.setText("仅改说明")
        self.p.mark_dirty()
        self.assertIn("参数检查 ✓", self.p.meta_parent.text())
        xy = next(n for n in self.p._body["program_steps"] if n["kind"] == "move_xy")
        self.select(xy["step_id"])
        self.p.param_edits["x_um"].setText("4321")
        self.assertNotIn("✓", self.p.meta_parent.text())
        self.assertIn("需重做", self.p.meta_parent.text())
        self.assertIn("重新检查", self.p.validation_hint.text())
        self.assertTrue(self.p.save_new_version())
        self.assertIn("参数检查 待完成", self.p.meta_parent.text())
        self.assertIn("尚未检查", self.p.tests.toPlainText())

    def test_deleted_step_can_be_restored_once_in_place(self):
        before = self.kinds()
        target = self.p._body["program_steps"][2]
        self.select(target["step_id"])
        self.assertFalse(self.p.undo_button.isEnabled())
        self.p.delete_hole()
        self.assertEqual(len(self.kinds()), len(before) - 1)
        self.assertTrue(self.p.undo_button.isEnabled())
        self.assertIn("撤销删除", self.p.validation_hint.text())
        # Nothing selected any more: the old step's title must not linger.
        self.select("__common")
        self.p.undo_button.click()
        self.assertEqual(self.kinds(), before)
        self.assertEqual(self.p._body["program_steps"][2]["step_id"], target["step_id"])
        self.assertFalse(self.p.undo_button.isEnabled())

    def test_deleting_a_loop_with_children_asks_first(self):
        from unittest.mock import patch
        from PySide6.QtWidgets import QMessageBox

        self.p.add_kind("point_loop")
        loop = self.p._find_step(self.p._selected_step_id)
        self.p.add_kind("wait")
        self.assertEqual(len(loop["children"]), 1)
        self.select(loop["step_id"])
        count = len(self.kinds())
        with patch(
            "zen_qt.pages.recipe_steps.QMessageBox.question",
            return_value=QMessageBox.Cancel,
        ) as ask:
            self.p.delete_hole()
        ask.assert_called_once()
        self.assertEqual(len(self.kinds()), count)
        with patch(
            "zen_qt.pages.recipe_steps.QMessageBox.question",
            return_value=QMessageBox.Yes,
        ):
            self.p.delete_hole()
        self.assertEqual(len(self.kinds()), count - 1)
        self.p.undo_delete()
        self.assertEqual(len(self.p._find_step(loop["step_id"])["children"]), 1)

    def test_param_panel_is_cleared_when_no_step_is_selected(self):
        xy = next(n for n in self.p._body["program_steps"] if n["kind"] == "move_xy")
        self.select(xy["step_id"])
        self.assertEqual(self.p.step_title.text(), xy["label"])
        self.p._selected_step_id = None
        self.p.refresh_params()
        self.assertEqual(self.p.step_title.text(), "请选择步骤")
        self.assertEqual(self.p.tests.toPlainText(), "")

    def test_device_driving_actions_are_grouped_and_styled_apart(self):
        for button in (self.p.test_step_button, self.p.from_here_button, self.p.trial_button):
            self.assertEqual(button.objectName(), "moving")
            self.assertIn("现场设备", button.toolTip())
        for button in (self.p.check_button, self.p.simulate_button):
            self.assertNotEqual(button.objectName(), "moving")
        bar = self.p.check_button.parentWidget().layout()
        self.assertLess(bar.indexOf(self.p.simulate_button), bar.indexOf(self.p.device_group_label))
        self.assertLess(bar.indexOf(self.p.device_group_label), bar.indexOf(self.p.test_step_button))

    def test_standard_dialog_buttons_are_chinese(self):
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self.w)
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        self.assertEqual(
            {b.text() for b in box.buttons()}, {"保存", "不保存", "取消"}
        )



class AuditFollowupTests(unittest.TestCase):
    setUpClass = classmethod(lambda cls: _app())
    setUp = HmiReviewTests.setUp
    cleanup = HmiReviewTests.cleanup
    run_demo = HmiReviewTests.run_demo

    def test_new_task_identity_clears_old_verdict(self):
        self.run_demo()
        self.w.sample_edit.setText('NEXT-SAMPLE')
        self.assertEqual(self.c._verdict_kind, 'idle')
        self.assertNotIn('Rx', self.c.verdict.text())
        self.assertEqual(self.c.marks.frames, {})
        self.c.set_verdict('ok', '旧结果')
        self.p.seed_demo()
        self.c.select_recipe(self.p.current_id, self.p.current_rev, mode='simulation')
        self.assertEqual(self.c._verdict_kind, 'idle')
        self.assertNotIn('完成', self.c.result_label.text())

    def test_running_verdict_survives_identity_edit(self):
        from unittest.mock import patch
        self.c.set_verdict('run', '任务执行中')
        with patch.object(self.w, 'action_busy', return_value=True):
            self.w.sample_edit.setText('NEXT-SAMPLE')
        self.assertEqual(self.c._verdict_kind, 'run')


class PairSummaryTests(unittest.TestCase):
    def test_pairs_require_both_layers_and_same_context_and_id(self):
        from zen_qt.pages.records import completed_pairs
        def image(context, pair, layer):
            return dict(context=context, pair_id=pair, layer=layer, path='saved.tif')
        upper = image('loop/1', 'A', 'upper')
        cases = [([upper], 0), ([upper, image('loop/1', 'B', 'lower')], 0),
                 ([upper, image('loop/2', 'A', 'lower')], 0),
                 ([upper, image('loop/1', 'A', 'lower')], 1),
                 ([upper, image('loop/1', 'A', 'lower'), image('loop/2', 'A', 'upper'), image('loop/2', 'A', 'lower')], 2)]
        for images, expected in cases:
            with self.subTest(images=images):
                self.assertEqual(completed_pairs({'images': images}), expected)
        self.assertEqual(completed_pairs({'captures': [{'state':'complete'}, {'state':'failed'}]}), 1)
        self.assertEqual(completed_pairs({'images': [], 'captures': [{'state':'complete'}]}), 0)

    def test_motion_only_sequence_is_not_reported_as_capture(self):
        from zen_qt.pages.records import run_verdict
        run = dict(state='complete',kind='sequence',sequence={},stage='',recovery=False,n_images=0)
        self.assertEqual(run_verdict(run), ('ok','任务完成'))


if __name__ == "__main__":
    unittest.main()
