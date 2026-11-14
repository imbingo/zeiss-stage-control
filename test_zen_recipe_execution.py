"""Protocol-level production evidence and fault cases, without a microscope."""

import tempfile, time, json, unittest
from pathlib import Path
from dataclasses import replace
from zen_recipe_compile import compile_body
from zen_recipe_store import default_body
from zen_hardware.mark_simulator import SimulatedSequenceDevice
from zen_workflow import Workflow
from zen_fit import validate_recipe, fit_demo


class RecipeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.d = SimulatedSequenceDevice()
        self.d.connected = True

    def run_plan(self, body=None, identity=None, mutate=None, simulated=True):
        body = body or default_body(example=True)
        p = compile_body(body, simulated=simulated, identity=identity)
        w = Workflow(
            self.d,
            self.root / "data",
            simulated=simulated,
            validate=validate_recipe,
            fit=fit_demo,
        )
        w.state = "就绪"
        if mutate:
            p = mutate(p)
        w.start_sequence(
            p,
            self.root / "images",
            confirmed=True,
            experiment_loading_confirmed=True,
            confirmed_start=self.d.read_xy(),
        )
        end = time.monotonic() + 30
        while w.busy and time.monotonic() < end:
            time.sleep(0.01)
        self.assertFalse(w.busy)
        return w

    def test_changed_production_experiment_blocks_before_xy(self):
        b = default_body(example=True)
        b["example"] = False
        self.d.call_hardware_operation = lambda op, params: {"xml_sha256": "changed"}
        w = self.run_plan(
            b,
            {
                "execution_mode": "production",
                "experiment_fingerprints": {"Demo AF": "old"},
            },
            simulated=False,
        )
        self.assertEqual(w.sequence_report["state"], "failed")
        self.assertEqual(self.d.capture_count, 0)
        self.assertEqual(len(self.d.moves), 0)
        self.assertTrue(w.sequence_report["recovery_required"])

    def test_unverified_af_range_aborts_before_capture(self):
        original = self.d.prepare_mark_experiment

        def invalid(*a, **k):
            original(*a, **k)
            return {"af_range": "unverified"}

        self.d.prepare_mark_experiment = invalid
        w = self.run_plan()
        self.assertEqual(w.sequence_report["state"], "failed")
        self.assertEqual(self.d.af_attempts, 0)
        self.assertEqual(self.d.capture_count, 0)

    def test_per_point_seed_and_bounds_are_applied(self):
        b = default_body(example=True)
        b["tilt"]["points_um"][0].append(101)
        w = self.run_plan(b)
        self.assertEqual(w.sequence_report["state"], "complete")
        self.assertTrue(
            any(
                abs(low - 96e-6) < 1e-12 and abs(high - 106e-6) < 1e-12
                for _, low, high in self.d.prepared
            )
        )

    def test_single_measurement_scope_never_visits_holes(self):
        w = self.run_plan(
            mutate=lambda p: replace(p, stations=(), execution_scope="tilt")
        )
        self.assertEqual(w.sequence_report["state"], "complete")
        self.assertEqual(self.d.capture_count, 0)
        self.assertTrue((w.sequence_run_path / "angle_result.json").exists())

    def test_partial_capture_failure_keeps_upper_and_full_identity(self):
        self.d.mark_fault = "lower_capture"
        w = self.run_plan(
            identity={
                "recipe_id": "TEST",
                "revision": 1,
                "execution_mode": "simulation",
            }
        )
        self.assertEqual(w.sequence_report["state"], "failed")
        image = Path(w.sequence_report["captures"][0]["output_directory"])
        self.assertTrue((image / "upper.tif").exists())
        self.assertFalse((image / "lower.tif").exists())
        self.assertEqual(
            json.loads((w.sequence_run_path / "snapshot.json").read_text())["plan"][
                "recipe_identity"
            ]["revision"],
            1,
        )
