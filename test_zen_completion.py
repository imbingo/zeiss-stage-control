"""Normal verified operations remain ready; unknown completion remains locked."""

import tempfile, time, unittest
from pathlib import Path
from threading import Event
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_hardware.library import HardwareTestSession
from zen_workflow import Workflow
from zen_records import pending_recovery
from zen_fit import fit_demo, validate_recipe


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.d = SimulatedMarkDevice(xy_enabled=False)
        self.d.connect_transport()
        self.s = HardwareTestSession(self.root)
        self.s.bind(self.d, simulated=True)

    def run_op(self, key, p=None, **kw):
        return self.s.execute(key, p, confirmed=True, timeout_s=0.2, **kw)

    def workflow(self):
        w = Workflow(
            self.d, self.root, simulated=True, validate=validate_recipe, fit=fit_demo
        )
        w.state = "通信就绪（硬件单步）"
        return w

    def wait(self, w):
        end = time.monotonic() + 3
        while w.busy and time.monotonic() < end:
            time.sleep(0.005)
        self.assertFalse(w.busy)

    def test_verified_z_can_be_followed_by_next_operation_without_manual_recovery(self):
        for z in (101, 102):
            report = self.run_op("move_z", {"z_um": z})
            self.assertTrue(report["completion"]["verified"])
            self.assertFalse(report["recovery_required"])
        self.assertFalse(pending_recovery(self.root, simulated=True))

    def test_not_arrived_z_never_retries_action_and_stays_locked(self):
        calls = []
        self.d.move_z = lambda z: calls.append(z)
        report = self.run_op("move_z", {"z_um": 200})
        self.assertEqual(len(calls), 1)
        self.assertFalse(report["completion"]["verified"])
        self.assertTrue(report["recovery_required"])
        self.assertTrue(pending_recovery(self.root, simulated=True))

    def test_running_experiment_not_misreported_complete(self):
        original = self.d.call_hardware_operation

        def operation(key, p):
            if key == "experiment_status":
                return {"is_experiment_running": True, "is_acquisition_running": True}
            return original(key, p)

        self.d.call_hardware_operation = operation
        report = self.run_op("load_experiment", {"experiment_name": "Upper"})
        self.assertTrue(report["recovery_required"])
        self.assertEqual(self.d.lab_calls.count("load_experiment"), 1)

    def test_stop_request_still_requires_recovery(self):
        report = self.run_op("stop_z")
        self.assertTrue(report["recovery_required"])

    def test_readiness_from_transport_preserves_experiment_id_and_never_reads_xy(self):
        self.d._experiment_id = "kept-id"
        self.d.read_xy = lambda: (_ for _ in ()).throw(AssertionError("no XY"))
        w = self.workflow()
        w.verify_ready()
        self.wait(w)
        self.assertEqual(w.state, "就绪")
        self.assertEqual(self.d._experiment_id, "kept-id")
        self.assertEqual(self.d.lab_calls, [])

    def test_failed_readiness_does_not_claim_ready(self):
        self.d.read_z = lambda: float("nan")
        w = self.workflow()
        w.verify_ready()
        self.wait(w)
        self.assertNotEqual(w.state, "就绪")

    def test_successful_parameter_write_preserves_business_readiness(self):
        w = self.workflow()
        w.state = "就绪"
        self.d._experiment_id = "demo-loaded"
        w.run_hardware_test(
            self.s,
            "clone_experiment",
            {"experiment_id": "demo-loaded"},
            confirmed=True,
            timeout_s=1,
        )
        self.wait(w)
        self.assertEqual(w.state, "就绪")
