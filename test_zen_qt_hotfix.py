"""Exercise the visible controls behind the four reported V2.8 regressions."""

import tempfile, unittest
from pathlib import Path
from PySide6.QtWidgets import QPushButton
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow


class HotfixUiTests(unittest.TestCase):
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

    def test_add_button_opens_actual_menu_and_choice_inserts_step(self):
        p = self.w.pages["recipes"]
        p.seed_demo()
        before = len(p._body["program_steps"])
        button = next(
            b
            for b in p.step_tree.parentWidget().findChildren(QPushButton)
            if b.text() == "＋步骤"
        )
        button.click()
        self.assertTrue(p.add_menu.isVisible())
        action = next(
            a
            for group in p.add_menu.actions()
            for a in group.menu().actions()
            if a.data() == "wait"
        )
        action.trigger()
        p.add_menu.close()
        self.assertEqual(len(p._body["program_steps"]), before + 1)
        self.assertEqual(p._find_step(p._selected_step_id)["kind"], "wait")

    def test_catalog_single_selection_has_explicit_add_control(self):
        from PySide6.QtCore import Qt

        p = self.w.pages["recipes"]
        p.seed_demo()
        before = len(p._body["program_steps"])
        item = next(
            p.catalog.topLevelItem(i).child(j)
            for i in range(p.catalog.topLevelItemCount())
            for j in range(p.catalog.topLevelItem(i).childCount())
            if p.catalog.topLevelItem(i).child(j).data(0, Qt.UserRole) == "move_z"
        )
        p.catalog.setCurrentItem(item)
        p.add_catalog_button.click()
        self.assertEqual(len(p._body["program_steps"]), before + 1)

    def test_top_exit_demo_returns_to_disconnected_device_page(self):
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        self.w.refresh_ui()
        self.assertFalse(self.w.exit_demo_button.isHidden())
        self.w.exit_demo_button.click()
        self.assertIsNone(self.w.workflow)
        self.assertTrue(self.w.exit_demo_button.isHidden())
        self.assertEqual(self.w.pages["hardware"].current_experiment_id, "")

    def test_marks_promotes_transport_connection_via_read_only_ready_check(self):
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        w = self.w.workflow
        w.state = "通信就绪（硬件单步）"
        w.device._experiment_id = "preserved"
        m = self.w.pages["marks"]
        m.on_show()
        self.assertTrue(_wait(lambda: not w.busy))
        self.assertEqual(w.state, "就绪")
        self.assertEqual(w.device._experiment_id, "preserved")
        m.refresh_enabled()
        self.assertEqual(m.check_items["connected"].icon.text(), "✓")

    def test_mark_readiness_failure_shows_actual_state(self):
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        w = self.w.workflow
        w.device.read_z = lambda: float("nan")
        w.state = "通信就绪（硬件单步）"
        m = self.w.pages["marks"]
        m.ready_button.click()
        self.assertTrue(_wait(lambda: not w.busy))
        self.assertNotEqual(w.state, "就绪")
