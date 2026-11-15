"""Available entry points, clear feedback and cancellation-safe automatic checks."""

import tempfile, unittest
from pathlib import Path
from threading import Event
from PySide6.QtTest import QTest
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from zen_steps import make_step


class ActionFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.w.z_timer.stop()
        self.c = self.w.pages["sequence"]
        self.addCleanup(self.close)

    def close(self):
        from zen_qt.action_feedback import cancel_pending

        for page in self.w.pages.values():
            cancel_pending(page)
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.w.pages["recipes"]._dirty = False
        self.w.close()

    def recipe(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        p._body["program_steps"] = [make_step("wait", seconds=0.02)]
        p.mark_dirty()
        self.assertTrue(p.save_new_version())
        self.w.connect_demo("sequence")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.c.select_recipe(p.current_id, p.current_rev, mode="simulation")
        self.w.sample_edit.setText("W1")
        self.w.operator_edit.setText("E1")
        self.c.path_confirm.setChecked(True)
        self.c.load_confirm.setChecked(True)
        return self.w.workflow

    def hold(self, w, read=True):
        entered = Event()
        release = Event()
        self.addCleanup(release.set)

        def wait(*_):
            entered.set()
            release.wait(3)
            return {}

        if read:
            w.read_only("held_read", wait)
        else:
            w._launch(wait)
        self.assertTrue(entered.wait(1))
        return release

    def test_disconnected_checkboxes_and_start_are_clickable_with_next_step(self):
        self.c.refresh_enabled()
        self.assertTrue(self.c.path_confirm.isEnabled())
        self.assertTrue(self.c.load_confirm.isEnabled())
        self.assertTrue(self.c.start_button.isEnabled())
        self.c.start_button.click()
        self.assertIn("配方", self.c.status.text())
        self.assertFalse(self.c.next_action.isHidden())
        self.assertIsNone(self.w.workflow)

    def test_background_read_does_not_disable_or_clear_confirmations(self):
        w = self.recipe()
        release = self.hold(w)
        self.c.refresh_enabled()
        self.assertFalse(self.w.action_busy())
        self.assertTrue(self.c.path_confirm.isEnabled())
        self.assertTrue(self.c.path_confirm.isChecked())
        self.assertTrue(self.c.output.isEnabled())
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))

    def test_start_auto_checks_and_runs_once_without_extra_preflight_click(self):
        w = self.recipe()
        self.assertIsNone(self.c._checked)
        self.c.start_button.click()
        self.c.start_button.click()
        self.assertTrue(
            _wait(
                lambda: getattr(w, "sequence_report", {}).get("state") == "complete"
                and not w.busy
            )
        )
        self.assertEqual(len(w.sequence_report["steps"]), 1)
        self.assertEqual(len(list((w.root / "demo").iterdir())), 1)
        self.assertTrue(self.c.path_confirm.isChecked())
        self.assertTrue(self.c.load_confirm.isChecked())

    def test_stop_cancels_start_waiting_behind_read(self):
        w = self.recipe()
        release = self.hold(w)
        self.c.start()
        self.assertIsNotNone(self.c._pending_click)
        self.w.stop()
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))
        QTest.qWait(120)
        self.assertIsNone(getattr(w, "sequence_report", None))
        self.assertEqual(w.device.z_moves, [])

    def test_editing_metadata_cancels_queued_start_without_resetting_path(self):
        w = self.recipe()
        release = self.hold(w)
        self.c.start()
        self.w.operator_edit.setText("E2")
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))
        QTest.qWait(120)
        self.assertIsNone(getattr(w, "sequence_report", None))
        self.assertTrue(self.c.path_confirm.isChecked())
        self.assertTrue(self.c.load_confirm.isChecked())

    def test_actual_device_task_rejects_duplicate_start_with_reason(self):
        w = self.recipe()
        release = self.hold(w, read=False)
        self.c.start()
        self.assertIn("不能叠加", self.c.status.text())
        self.assertIsNone(getattr(self.c, "_pending_click", None))
        self.assertIsNone(getattr(w, "sequence_report", None))
        release.set()

    def test_transport_only_connection_can_be_checked_from_run_center(self):
        w = self.recipe()
        w.state = "通信就绪（硬件单步）"
        self.c.preflight()
        self.assertTrue(_wait(lambda: self.c._checked is not None and not w.busy))
        self.assertEqual(w.state, "就绪")
        self.assertIsNone(getattr(w, "sequence_report", None))

    def test_metadata_retains_confirmations_and_new_sample_rechecks_clamping(self):
        self.recipe()
        self.c.output.setText(str(Path(self.tmp.name) / "other-images"))
        self.w.operator_edit.setText("E2")
        self.assertTrue(self.c.path_confirm.isChecked())
        self.assertTrue(self.c.load_confirm.isChecked())
        self.w.sample_edit.setText("W2")
        self.assertFalse(self.c.path_confirm.isChecked())
        self.assertTrue(self.c.load_confirm.isChecked())

    def test_unchecking_manual_confirmation_cannot_auto_start_after_read(self):
        w = self.recipe()
        release = self.hold(w)
        self.c.start()
        self.c.path_confirm.setChecked(False)
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))
        QTest.qWait(120)
        self.assertIsNone(getattr(w, "sequence_report", None))
        self.assertIn("人工核对", self.c.status.text())

    def test_mark_capture_button_reports_missing_confirmation_without_motion(self):
        self.recipe()
        m = self.w.pages["marks"]
        m.refresh_enabled()
        self.assertTrue(m.capture_buttons["upper"].isEnabled())
        m.capture_buttons["upper"].click()
        self.assertIn("核对", m.capture_hint.text())
        self.assertEqual(self.w.workflow.device.capture_count, 0)
        self.assertFalse(m.fix_button.isHidden())

    def test_release_entry_lists_missing_requirements_without_releasing(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        p.release_button.click()
        self.assertIn("放行尚缺条件", p.validation_hint.text())
        self.assertEqual(p.library.get(p.current_id, p.current_rev)["status"], "draft")

    def test_manual_and_device_entries_explain_instead_of_silently_disabling(self):
        p = self.w.pages["manual"]
        p.refresh_enabled()
        self.assertTrue(p.z.isEnabled())
        p.z.click()
        self.assertIn("连接", p.message.text())
        d = self.w.pages["device"]
        d.refresh_enabled()
        self.assertTrue(d.preflight_button.isEnabled())
        d.preflight_button.click()
        self.assertIn("连接", d.preflight_summary.text())
        h = self.w.pages["hardware"]
        h.refresh_enabled()
        h.run_button.click()
        self.assertIn("连接", h.result_info.text())

    def test_periodic_refresh_yields_to_pending_user_click(self):
        from unittest.mock import patch

        w = self.recipe()
        self.c._pending_click = {"explicit_intent": True}
        with patch.object(w, "read_only") as reader:
            self.w._poll_position()
            reader.assert_not_called()
        self.c._pending_click = None

    def test_readonly_preflight_does_not_require_operator_but_start_does(self):
        w = self.recipe()
        self.w.operator_edit.setText("")
        self.c.preflight()
        self.assertTrue(_wait(lambda: self.c._checked is not None and not w.busy))
        self.c.start()
        self.assertIn("操作员", self.c.status.text())
        self.assertIsNone(getattr(w, "sequence_report", None))

    def test_fast_ready_check_still_continues_preflight(self):
        from unittest.mock import patch

        w = self.recipe()
        w.state = "通信就绪（硬件单步）"
        with patch.object(w, "verify_ready", side_effect=lambda: w._state("就绪")):
            self.c.preflight()
        self.assertTrue(_wait(lambda: self.c._checked is not None and not w.busy))
        self.assertIsNone(getattr(w, "sequence_report", None))

    def test_device_action_after_preflight_triggers_fresh_check_automatically(self):
        import json

        w = self.recipe()
        self.c.preflight()
        self.assertTrue(_wait(lambda: self.c._checked is not None and not w.busy))
        w._launch(lambda: w.device.move_z(120e-6))
        self.assertTrue(_wait(lambda: not w.busy))
        self.c.start()
        self.assertTrue(
            _wait(
                lambda: getattr(w, "sequence_report", {}).get("state") == "complete"
                and not w.busy
            )
        )
        snapshot = json.loads(
            (w.sequence_run_path / "snapshot.json").read_text(encoding="utf-8")
        )
        self.assertAlmostEqual(snapshot["preflight"]["z_m"], 120e-6)

    def test_leaving_page_cancels_start_during_preflight(self):
        w = self.recipe()
        entered = Event()
        release = Event()
        self.addCleanup(release.set)
        read = w.device.read_z

        def hold():
            entered.set()
            release.wait(3)
            return read()

        w.device.read_z = hold
        self.c.start()
        self.assertTrue(entered.wait(1))
        self.w.navigate("recipes")
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))
        QTest.qWait(120)
        self.assertIsNone(getattr(w, "sequence_report", None))

    def test_parameter_dialog_cancels_start_during_preflight(self):
        from PySide6.QtWidgets import QDialog

        w = self.recipe()
        entered = Event()
        release = Event()
        self.addCleanup(release.set)
        read = w.device.read_z

        def hold():
            entered.set()
            release.wait(3)
            return read()

        w.device.read_z = hold
        self.c.start()
        self.assertTrue(entered.wait(1))
        dialog = QDialog(self.w)
        dialog.setModal(True)
        dialog.show()
        self.addCleanup(dialog.close)
        _app().processEvents()
        release.set()
        self.assertTrue(_wait(lambda: not w.busy))
        QTest.qWait(120)
        self.assertIsNone(getattr(w, "sequence_report", None))
        dialog.close()
