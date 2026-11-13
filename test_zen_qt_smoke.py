"""Offscreen smoke tests for the Qt workbench (not part of the CI count)."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from zen_qt.app import WorkbenchWindow


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    return app


def _wait(predicate, timeout=30):
    app = _app()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class TestQtWorkbench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _app()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.window = WorkbenchWindow(Path(self.tmp.name) / "data")
        self.addCleanup(self.window.close)

    def test_demo_tilt_run(self):
        window = self.window
        window.connect_demo("normal")
        self.assertTrue(_wait(lambda: window.workflow and window.workflow.state == "完成"))
        self.assertIsNotNone(window.workflow.result)
        self.assertTrue(window.field_recipe is not None)
        self.assertIn("rx_urad", window.workflow.result)

    def test_demo_mark_capture(self):
        window = self.window
        window.connect_demo("marks")
        self.assertTrue(_wait(lambda: window.workflow and window.workflow.state == "就绪"))
        page = window.pages["marks"]
        page.check_items["zpath"].check.setChecked(True)
        page.check_items["loading"].check.setChecked(True)
        page.capture(("upper", "lower"))
        self.assertTrue(_wait(
            lambda: window.workflow.state == "就绪"
            and getattr(window.workflow, "mark_output_path", None) is not None))
        output = Path(window.workflow.mark_output_path)
        self.assertTrue((output / "upper.tif").exists())
        self.assertTrue((output / "lower.tif").exists())
        self.assertTrue((output / "capture.json").exists())

    def test_recovery_lock_blocks_capture(self):
        window = self.window
        window.connect_demo("marks")
        self.assertTrue(_wait(lambda: window.workflow and window.workflow.state == "就绪"))
        page = window.pages["marks"]
        # Simulate a pending-recovery record appearing after connect.
        from zen_records import pending_recovery, write_json, utc_now
        run = Path(self.tmp.name) / "data" / "demo" / "stale-run"
        run.mkdir(parents=True)
        write_json(run / "status.json", {"state": "running", "recovery_required": True,
                                         "result_valid": False, "started_at": utc_now()})
        window.recovery_runs = pending_recovery(window.data_root, simulated=True)
        window.refresh_ui()
        self.assertTrue(window.locked())
        for keys, button in page.capture_buttons.items():
            self.assertFalse(button.isEnabled())
        # Unlock via the same path the recovery dialog uses.
        window.confirm_recovery()
        self.assertFalse(window.locked())


if __name__ == "__main__":
    unittest.main()
