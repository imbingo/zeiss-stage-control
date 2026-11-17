"""Multi-face editing, guarded Z teaching and actual runtime/archive display."""

import tempfile, unittest, json
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QComboBox, QCheckBox
from test_zen_qt_smoke import _app, _wait
from zen_qt.app import WorkbenchWindow
from test_zen_heights import height_step
from zen_heights import validate_height_trial


class HeightUiTests(unittest.TestCase):
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
        self.n = height_step(current=True)
        self.p._body["program_steps"] = [self.n]
        self.p._selected_step_id = self.n["step_id"]
        self.p.mark_dirty()
        self.p.refresh_step_tree()
        self.p.refresh_params()
        self.addCleanup(self.close)

    def close(self):
        if self.w.workflow and self.w.workflow.busy:
            self.w.stop()
            _wait(lambda: not self.w.workflow.busy)
        self.p._dirty = False
        self.w.close()

    def test_two_three_faces_and_override_controls_save(self):
        combo = next(
            c for c in self.p.form_host.findChildren(QComboBox) if c.findData(2) >= 0
        )
        combo.setCurrentIndex(combo.findData(2))
        _app().processEvents()
        self.assertEqual(len(self.p.height_z_edits), 2)
        box = next(
            b
            for b in self.p.form_host.findChildren(QCheckBox)
            if b.objectName() == "height_override_0"
        )
        box.click()
        _app().processEvents()
        self.assertTrue(self.n["params"]["surfaces"][0]["override"])
        self.assertTrue(self.p.save_new_version())
        self.p.check_recipe()
        version = self.p.library.get(self.p.current_id, self.p.current_rev)
        self.assertEqual(version["checks"]["result"], "passed")
        self.assertEqual(
            version["body"]["program_steps"][0]["params"]["surface_count"], 2
        )
        self.assertTrue(
            version["body"]["program_steps"][0]["params"]["surfaces"][0]["override"]
        )

    def test_no_xy_z_teach_and_late_response_rejected(self):
        self.w.set_xy_mode("no_xy")
        self.w.connect_demo("marks")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))

        def forbidden():
            raise AssertionError("must not read XY")

        self.w.workflow.device.read_xy = forbidden
        self.w.workflow.device.z = 42e-6
        self.p.teach_height_z(1)
        self.assertTrue(_wait(lambda: self.n["params"]["surfaces"][1]["z_um"] == 42))
        token = (
            self.p.current_id,
            self.p.current_rev,
            self.n["step_id"],
            1,
            json.dumps(self.n["params"], sort_keys=True),
        )
        self.n["params"]["surfaces"][1]["z_um"] = 50
        self.p.on_rpc_result(
            "heights_teach",
            dict(
                target=token,
                device=id(self.w.workflow.device),
                generation=getattr(self.w.workflow, "action_generation", 0),
                z=10e-6,
            ),
        )
        self.assertEqual(self.n["params"]["surfaces"][1]["z_um"], 50)
        self.assertEqual(self.w.workflow.device.z_moves, [])

    def run_current(self, fault=False):
        self.w.set_xy_mode("no_xy")
        self.w.connect_demo("sequence")
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        if fault:
            original = self.w.workflow.device.autofocus

            def fail(timeout):
                if self.w.workflow.device.experiment == "Demo Z3":
                    raise TimeoutError("third face timeout")
                return original(timeout)

            self.w.workflow.device.autofocus = fail
        self.assertTrue(self.p.save_new_version())
        c = self.w.pages["sequence"]
        c.select_recipe(self.p.current_id, self.p.current_rev, mode="simulation")
        self.w.sample_edit.setText("HEIGHT1")
        self.w.operator_edit.setText("E1")
        c.path_confirm.setChecked(True)
        c.load_confirm.setChecked(True)
        c.start()
        self.assertTrue(_wait(lambda: c._verdict_kind in ("ok", "bad", "warn"), 30))
        self.assertTrue(_wait(lambda: not self.w.workflow.busy))
        return c

    def test_current_scan_table_archive_and_new_task_reset(self):
        c = self.run_current()
        self.assertEqual(self.w.workflow.sequence_report["state"], "complete")
        self.assertEqual(c.heights.table.rowCount(), 1)
        self.assertIn("XY 未自动核验", c.heights.summary.text())
        self.assertIn("采集及安全 Z 返回完成", c.heights.summary.text())
        self.assertNotIn("待 SOMA", c.verdict.text())
        self.w.navigate("records")
        records = self.w.pages["records"]
        records.table.selectRow(0)
        _app().processEvents()
        self.assertEqual(records.height_view.table.rowCount(), 1)
        self.assertIn("多面高度", records.detail.text())
        records.height_view.update_report({})
        self.assertEqual(records.height_view.table.rowCount(), 0)
        self.w.sample_edit.setText("NEXT")
        self.assertEqual(c.heights.table.rowCount(), 0)

    def test_failed_surface_table_keeps_blanks_and_partial_status(self):
        c = self.run_current(fault=True)
        self.assertEqual(c._verdict_kind, "bad")
        fields = [
            c.heights.table.horizontalHeaderItem(i).text()
            for i in range(c.heights.table.columnCount())
        ]
        self.assertEqual(c.heights.table.item(0, fields.index("Z3 · µm")).text(), "")
        self.assertEqual(c.heights.table.item(0, fields.index("Z3−Z2 · µm")).text(), "")
        self.assertEqual(
            c.heights.table.item(0, fields.index("状态")).text(), "部分完成"
        )


class HeightEvidenceTests(unittest.TestCase):
    def test_incomplete_empty_missing_fingerprint_or_loop_trial_cannot_release(self):
        s = height_step(current=True)
        body = {"program_steps": [s]}
        report = {"experiment_fingerprints": {}, "height_scans": {}}
        with self.assertRaises(ValueError):
            validate_height_trial(body, report)
        report["experiment_fingerprints"] = {f"Demo Z{i}": "hash" for i in (1, 2, 3)}
        with self.assertRaises(ValueError):
            validate_height_trial(body, report)
        scan = dict(
            step_path=s["step_id"],
            state="complete",
            data_complete=True,
            return_complete=True,
            surface_count=3,
            rows=[],
        )
        report["height_scans"] = {s["step_id"]: scan}
        with self.assertRaises(ValueError):
            validate_height_trial(body, report)
        row = {
            "point_no": 1,
            "status": "complete",
            "faces": [],
            "Z1_um": 100,
            "Z2_um": 160,
            "Z3_um": 230,
        }
        for i in (1, 2, 3):
            row["faces"].append(
                dict(
                    state="complete",
                    surface=i,
                    experiment=f"Demo Z{i}",
                    experiment_sha256="hash",
                )
            )
        scan["rows"] = [row]
        validate_height_trial(body, report)
        loop = dict(
            step_id="LOOP",
            kind="point_loop",
            params={"points_um": [[0, 0], [1, 1]]},
            children=[s],
        )
        with self.assertRaises(ValueError):
            validate_height_trial({"program_steps": [loop]}, report)
        scan["return_complete"] = False
        with self.assertRaises(ValueError):
            validate_height_trial(body, report)
