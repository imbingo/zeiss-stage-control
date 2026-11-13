import csv
import json
import tempfile
import time
import unittest
from dataclasses import replace
from math import atan
from pathlib import Path
from threading import Event

from zen_fit import DEMO_CONVENTION, FIELD_CONVENTION, fit_demo, fit_field, validate_recipe
from zen_acquisition import AcquisitionFailed, acquire
from zen_hardware import Motion
from zen_hardware.simulator import SimulatedDevice
from zen_records import pending_recovery
from zen_workflow import Recipe, Workflow


RECIPE = Recipe("演示", "Demo AF", ((0, 0), (.001, 0), (.001, .001), (0, .001)),
                1e-6, 2, 2, DEMO_CONVENTION)


class BlockingAutofocus(SimulatedDevice):
    def __init__(self):
        super().__init__(delay_seconds=0)
        self.entered = Event()
        self.release = Event()

    def autofocus(self, timeout_seconds):
        self.entered.set()
        if not self.release.wait(3):
            raise AssertionError("Test did not release AF")
        return super().autofocus(timeout_seconds)


class WorkflowTest(unittest.TestCase):
    def test_arrival_timeout_retains_last_readback(self):
        class OffsetStage(SimulatedDevice):
            def move_xy(self, x, y):
                super().move_xy(x + 10e-6, y)

        device = OffsetStage(delay_seconds=0)
        workflow = self.connected(device)
        workflow.start(replace(RECIPE, arrival_timeout=.01), path_confirmed=True)
        self.wait(workflow)
        run, = pending_recovery(self.root, simulated=True)
        with (run / "points.csv").open(encoding="utf-8-sig") as stream:
            row, = csv.DictReader(stream)
        self.assertEqual(float(row["before_af_x_m"]), 10e-6)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(device.af_attempts, 0)

    def test_external_motion_between_points_prevents_next_move(self):
        device = SimulatedDevice(delay_seconds=0)
        device.connect()
        rows = []

        def persist(row):
            rows.append(row)
            device.motion = Motion.MOVING

        with self.assertRaises(AcquisitionFailed) as failure:
            acquire(device, RECIPE.targets, RECIPE.experiment, RECIPE.tolerance_m,
                    RECIPE.af_timeout, RECIPE.arrival_timeout, Event(), persist,
                    lambda stage, index: None)
        self.assertEqual(failure.exception.index, 2)
        self.assertTrue(failure.exception.recovery_required)
        self.assertEqual(device.moves, [RECIPE.targets[0]])
        self.assertEqual([row.status for row in rows], ["ok", "failed"])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def wait(self, workflow):
        deadline = time.monotonic() + 4
        while workflow.busy and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(workflow.busy)

    def connected(self, device, root=None):
        workflow = Workflow(device, root or self.root, simulated=True,
                            validate=validate_recipe, fit=fit_demo)
        workflow.connect()
        self.wait(workflow)
        self.assertEqual(workflow.state, "就绪")
        return workflow

    def test_field_batch_rejects_changed_confirmed_start(self):
        device = SimulatedDevice(delay_seconds=0)
        workflow = Workflow(device, self.root, simulated=False,
                            validate=validate_recipe, fit=fit_field)
        workflow.connect()
        self.wait(workflow)
        recipe = replace(RECIPE, sign_convention=FIELD_CONVENTION)
        with self.assertRaises(ValueError):
            workflow.start(recipe, path_confirmed=True)
        device.xy = (.002, 0)
        workflow.start(recipe, path_confirmed=True, confirmed_start=(0, 0))
        self.wait(workflow)
        self.assertEqual(workflow.state, "设备状态待确认")
        self.assertEqual(device.moves, [])
        self.assertEqual(device.af_attempts, 0)
        run, = pending_recovery(self.root, simulated=False)
        status = json.loads((run / "status.json").read_text(encoding="utf-8"))
        snapshot = json.loads((run / "recipe.json").read_text(encoding="utf-8"))
        self.assertEqual(status["failure_stage"], "路径起点复核")
        self.assertFalse(status["result_valid"])
        self.assertEqual(snapshot["confirmed_start_xy_m"], [0, 0])

    def test_fit_and_invalidation(self):
        device = SimulatedDevice(delay_seconds=0)
        workflow = self.connected(device)
        with self.assertRaises(ValueError):
            workflow.start(replace(RECIPE, targets=((0, 0), (1, 1), (2, 2))),
                           path_confirmed=True)
        self.assertEqual(device.moves, [])
        workflow.start(RECIPE, path_confirmed=True)
        self.wait(workflow)
        self.assertEqual(workflow.state, "完成")
        self.assertAlmostEqual(workflow.result["rx_urad"], atan(-.001) * 1e6)
        self.assertAlmostEqual(workflow.result["ry_urad"], -atan(.002) * 1e6)
        self.assertLess(workflow.result["rms_m"], 1e-15)
        self.assertLess(workflow.result["pv_m"], 1e-15)
        self.assertEqual(workflow.result["angle_unit"], "urad")
        saved = json.loads((workflow.result_path / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["rx_urad"], workflow.result["rx_urad"])
        self.assertEqual(saved["angle_unit"], "urad")
        self.assertNotIn("rx_deg", saved)
        workflow.invalidate()
        self.assertIsNone(workflow.result)
        self.assertEqual(len(list(self.root.rglob("result.json"))), 1)

    def test_faults_preserve_rows_and_block_restart(self):
        for fault, stage in (("af_timeout", "软件 AF"), ("move", "定位"),
                             ("disconnect", "软件 AF")):
            with self.subTest(fault=fault):
                root = self.root / fault
                device = SimulatedDevice(fault=fault, delay_seconds=0)
                workflow = self.connected(device, root)
                workflow.start(RECIPE, path_confirmed=True)
                self.wait(workflow)
                self.assertEqual(workflow.state, "设备状态待确认")
                self.assertEqual(len(device.moves), 3)
                self.assertIsNone(workflow.result)
                run, = pending_recovery(root, simulated=True)
                with (run / "points.csv").open(encoding="utf-8-sig") as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual([r["status"] for r in rows], ["ok", "ok", "failed"])
                self.assertEqual(rows[-1]["failure_stage"], stage)
                status = json.loads((run / "status.json").read_text(encoding="utf-8"))
                self.assertFalse(status["result_valid"])
                self.assertTrue(status["recovery_required"])
                self.assertFalse((run / "result.json").exists())
                workflow.connect()
                self.wait(workflow)
                with self.assertRaises(RuntimeError):
                    workflow.start(RECIPE, path_confirmed=True)
                restarted_device = SimulatedDevice(delay_seconds=0)
                restarted = Workflow(restarted_device, root, simulated=True,
                                     validate=validate_recipe, fit=fit_demo)
                restarted.connect()
                self.wait(restarted)
                self.assertEqual(restarted.state, "设备状态待确认")
                with self.assertRaises(RuntimeError):
                    restarted.start(RECIPE, path_confirmed=True)
                self.assertEqual(restarted_device.moves, [])

    def test_stop_during_af_and_duplicate_start(self):
        with self.subTest(scenario="stop_during_af"):
            device = BlockingAutofocus()
            workflow = self.connected(device)
            workflow.start(RECIPE, path_confirmed=True)
            try:
                self.assertTrue(device.entered.wait(2))
                with self.assertRaises(RuntimeError):
                    workflow.start(RECIPE, path_confirmed=True)
                with self.assertRaises(RuntimeError):
                    workflow.invalidate()
                workflow.stop()
            finally:
                device.release.set()
                self.wait(workflow)
            self.assertEqual(len(device.moves), 1)
            self.assertEqual(workflow.state, "已中止")
            self.assertIsNone(workflow.result)
        with self.subTest(scenario="stop_after_last_point_keeps_result"):
            class StopAtLastPoint(SimulatedDevice):
                owner = None

                def autofocus(self, timeout_seconds):
                    z = super().autofocus(timeout_seconds)
                    if self.af_attempts == len(RECIPE.targets) and self.owner is not None:
                        self.owner.stop()
                    return z

            device = StopAtLastPoint(delay_seconds=0)
            workflow = self.connected(device, self.root / "last")
            device.owner = workflow
            workflow.start(RECIPE, path_confirmed=True)
            self.wait(workflow)
            self.assertEqual(workflow.state, "完成")
            self.assertIsNotNone(workflow.result)
            run, = (run for run in (self.root / "last").rglob("status.json"))
            status = json.loads(run.read_text(encoding="utf-8"))
            self.assertEqual(status["state"], "complete")
            self.assertTrue(status["result_valid"])
            self.assertFalse(status["recovery_required"])

    def test_single_point_uses_current_xy_without_move_or_fit(self):
        device = SimulatedDevice(delay_seconds=0)
        device.xy = (.002, .001)
        workflow = self.connected(device)
        workflow.start(RECIPE, path_confirmed=True, single_point=True)
        self.wait(workflow)
        self.assertEqual(workflow.state, "就绪")
        self.assertEqual(device.moves, [])
        self.assertEqual(device.af_attempts, 1)
        self.assertIsNone(workflow.result)
        points, = self.root.rglob("points.csv")
        with points.open(encoding="utf-8-sig") as stream:
            row, = csv.DictReader(stream)
        self.assertEqual(float(row["target_x_m"]), .002)
        self.assertAlmostEqual(float(row["af_z_m"]), 53e-6)
