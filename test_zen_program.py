"""Composable programs: measured data, independent capture, limits and recovery."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import json, time, unittest
from unittest.mock import patch
from zen_steps import (
    make_step,
    validate_program,
    migrate_program,
    capability_issues,
    ProgramPlan,
)
from zen_recipe_store import default_body, RecipeLibrary
from zen_hardware.mark_simulator import SimulatedSequenceDevice, SimulatedMarkDevice
from zen_fit import fit_demo, validate_recipe
from zen_workflow import Workflow
from zen_records import pending_recovery


def body(steps=None):
    b = default_body("工程流程", example=True)
    b = migrate_program(b)
    b["common_params"]["settle_s"] = 0.001
    if steps is not None:
        b["program_steps"] = steps
    return b


class ProgramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.d = SimulatedSequenceDevice()
        self.d.connect()
        self.w = Workflow(
            self.d,
            self.base / "data",
            simulated=True,
            validate=validate_recipe,
            fit=fit_demo,
        )
        self.w.state = "就绪"

    def execute(self, b, **kw):
        self.w.start_program(
            validate_program(b),
            self.base / "images",
            session={"sample": "W1", "operator": "E1"},
            preflight=kw.get("preflight", {}),
            confirmed=True,
        )
        deadline = time.monotonic() + 10
        while self.w.busy and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertFalse(self.w.busy)
        return self.w.sequence_report

    def test_measure_gate_and_pair_across_independent_regions(self):
        trace = []
        move = self.d.move_xy

        def tracked(x, y):
            trace.append((x, y, self.d.z))
            return move(x, y)

        self.d.move_xy = tracked
        r = self.execute(body())
        self.assertEqual(r["state"], "complete", r.get("error"))
        self.assertEqual(len(r["measurements"]), 4)
        self.assertEqual(len(r["images"]), 2)
        self.assertAlmostEqual(r["result"]["rx_urad"], -1000, places=2)
        self.assertTrue(all(abs(z - 20e-6) < 1e-12 for _, _, z in trace))
        self.assertAlmostEqual(trace[-1][0], 0.004)
        events = []
        while not self.w.events.empty():
            events.append(self.w.events.get_nowait())
        self.assertEqual(sum(e[0] == "measurement_point" for e in events), 4)
        from zen_qt.pages.records import scan_runs

        self.assertEqual(scan_runs(self.w.root)[0]["n_images"], 2)
        self.assertEqual(
            json.loads(
                (self.w.sequence_run_path / "snapshot.json").read_text(encoding="utf-8")
            )["session"]["operator"],
            "E1",
        )

    def test_no_xy_capture_is_independent_of_angle_and_af(self):
        self.d.xy_enabled = False
        steps = [
            make_step("load_experiment", experiment="mark"),
            make_step("move_z", z_um=100),
            make_step("capture_upper"),
            make_step("move_z", z_um=200),
            make_step("capture_lower"),
        ]
        r = self.execute(body(steps))
        self.assertEqual(r["state"], "complete", r.get("error"))
        self.assertEqual(self.d.af_attempts, 0)
        self.assertEqual(self.d.moves, [])

    def test_capture_does_not_implicitly_move_or_autofocus(self):
        r = self.execute(
            body(
                [
                    make_step("load_experiment", experiment="mark"),
                    make_step("capture_upper"),
                ]
            )
        )
        self.assertEqual(r["state"], "complete")
        self.assertEqual(self.d.z_moves, [])
        self.assertEqual(self.d.af_attempts, 0)

    def test_loop_and_runtime_result_reference(self):
        z = make_step("move_z", z_um=100)
        loop = make_step("point_loop", points_um=[[1000, 1000], [2000, 2000]])
        loop["children"] = [
            make_step("move_xy", x_um="@point.x_um", y_um="@point.y_um"),
            make_step("move_z", z_um="@" + z["step_id"] + ".z_um"),
        ]
        r = self.execute(body([z, loop]))
        self.assertEqual(r["state"], "complete", r.get("error"))
        self.assertEqual(self.d.moves, [(0.001, 0.001), (0.002, 0.002)])
        self.assertAlmostEqual(self.d.z, 100e-6)
        self.assertEqual(len({n["path"] for n in r["steps"]}), 6)

    def test_stack_keeps_all_planes_and_metadata(self):
        r = self.execute(
            body(
                [
                    make_step("load_experiment", experiment="stack"),
                    make_step("zstack", first_um=100, last_um=102, interval_um=1),
                ]
            )
        )
        self.assertEqual(r["state"], "complete", r.get("error"))
        folder = Path(r["steps"][-1]["output"]["path"])
        meta = json.loads((folder / "stack.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["axes"], "ZYX")
        self.assertEqual(len(list(folder.glob("*.tif"))), 3)
        self.assertEqual(
            [round(n["z_m"] * 1e6) for n in meta["planes"]], [100, 101, 102]
        )

    def test_gate_rejection_preserves_measurements_without_recovery(self):
        b = body()
        b["program_steps"][1]["params"]["max_rx_urad"] = 1
        r = self.execute(b)
        self.assertEqual(r["state"], "failed")
        self.assertEqual(r["disposition"], "MEASUREMENT_REJECTED")
        self.assertFalse(r["recovery_required"])
        self.assertEqual(self.d.capture_count, 0)

    def test_failed_lower_preserves_upper_and_blocks_restart(self):
        self.d.mark_fault = "lower_capture"
        b = body(
            [
                make_step("load_experiment", experiment="mark"),
                make_step("capture_upper"),
                make_step("capture_lower"),
            ]
        )
        r = self.execute(b)
        self.assertEqual(r["state"], "failed")
        self.assertTrue(Path(r["images"][0]["path"]).is_file())
        self.assertTrue(pending_recovery(self.w.root, simulated=True))
        with self.assertRaises(RuntimeError):
            self.execute(b)

    def test_changed_position_after_preflight_never_issues_write(self):
        r = self.execute(
            body([make_step("move_z", z_um=200)]), preflight={"z_m": 50e-6}
        )
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.d.z_moves, [])
        self.assertFalse(r["recovery_required"])

    def test_z_timeout_cannot_start_xy(self):
        self.d.move_z = lambda z: None
        r = self.execute(
            body([make_step("move_xy", x_um=100, y_um=100, timeout_s=0.05)])
        )
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.d.moves, [])
        self.assertTrue(r["recovery_required"])

    def test_stop_does_not_start_next_step_or_return(self):
        original = self.d.move_z

        def stop(z):
            original(z)
            self.w.stop()

        self.d.move_z = stop
        r = self.execute(
            body([make_step("move_z", z_um=200), make_step("move_z", z_um=20)])
        )
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(len(self.d.z_moves), 1)

    def test_compile_rejects_missing_load_gain_forward_reference_and_unsafe_z(self):
        invalid = [
            body([make_step("capture_upper")]),
            body([make_step("set_gain", channel="1", value=500)]),
            body([make_step("move_z", z_um="@future.z_um")]),
            body([make_step("move_z", z_um=400)]),
        ]
        for b in invalid:
            with self.subTest(b=b), self.assertRaises(ValueError):
                validate_program(b)
        self.assertEqual(self.d.z_moves, [])

    def test_no_xy_capability_blocks_xy_only(self):
        self.d.xy_enabled = False
        self.assertFalse(
            capability_issues(
                validate_program(body([make_step("move_z", z_um=100)])), self.d
            )
        )
        self.assertTrue(
            capability_issues(
                validate_program(body([make_step("move_xy", x_um=1, y_um=1)])), self.d
            )
        )

    def test_forbidden_children_unsafe_ids_and_missing_loop_z_rejected(self):
        n = make_step("wait")
        n["children"] = [make_step("wait")]
        bad = make_step("wait")
        bad["step_id"] = "../../outside"
        loop = make_step("point_loop", points_um=[[0, 0]])
        loop["children"] = [make_step("move_z", z_um="@point.z_um")]
        for steps in ([n], [bad], [loop]):
            with self.assertRaises(ValueError):
                validate_program(body(steps))

    def test_public_start_revalidates_forged_plan(self):
        plan = ProgramPlan(
            "forged", (make_step("move_z", z_um=999),), body()["common_params"], {}
        )
        with self.assertRaises(ValueError):
            self.w.start_program(
                plan, self.base / "images", session={}, preflight={}, confirmed=True
            )

    def test_motion_only_recipe_can_qualify_but_simulation_cannot(self):
        lib = RecipeLibrary(self.base / "recipes.json")
        b = body([make_step("wait")])
        b["example"] = False
        rid, rev = lib.create("wait", "E1", body=b)
        v = lib.get(rid, rev)
        run = self.base / "trial"
        run.mkdir()
        r = {
            "state": "complete",
            "simulated": True,
            "plan": {
                "recipe_identity": {
                    "recipe_id": rid,
                    "revision": rev,
                    "content_sha256": v["content_sha256"],
                }
            },
        }
        (run / "sequence.json").write_text(json.dumps(r), encoding="utf-8")
        with self.assertRaises(ValueError):
            lib.record_trial(rid, rev, run, by="E1")
        r["simulated"] = False
        (run / "sequence.json").write_text(json.dumps(r), encoding="utf-8")
        lib.check(rid, rev)
        lib.record_trial(rid, rev, run, by="E1")
        lib.release(rid, rev, by="E1")
        self.assertEqual(lib.get(rid, rev)["status"], "released")

    def test_af_range_is_validated_before_moving_to_center(self):
        from zen_program import ProgramDevice

        device = ProgramDevice(self.w, body()["common_params"], [])
        with self.assertRaises(ValueError):
            device.autofocus("Upper", 299, 5, 1)
        self.assertEqual(self.d.z_moves, [])
        self.assertEqual(self.d.af_attempts, 0)

    def test_af_uses_explicit_loaded_experiment_without_hidden_load(self):
        r = self.execute(
            body(
                [
                    make_step("load_experiment", experiment="Upper"),
                    make_step("autofocus", center_um=100, half_um=5),
                ]
            )
        )
        self.assertEqual(r["state"], "complete", r.get("error"))
        self.assertEqual(self.d.lab_calls.count("load_experiment"), 1)

    def test_experiment_changes_within_trial_cannot_be_qualified(self):
        original = self.d.call_hardware_operation
        digests = iter(["a" * 64, "b" * 64])

        def control(operation, params):
            if operation == "export_experiment":
                return {"xml_sha256": next(digests)}
            return original(operation, params)

        self.d.call_hardware_operation = control
        self.w.simulated = False
        b = body(
            [
                make_step("load_experiment", experiment="Upper"),
                make_step("load_experiment", experiment="Upper"),
                make_step("move_z", z_um=200),
            ]
        )
        b["example"] = False
        report = self.execute(b)
        self.assertEqual(report["state"], "failed")
        self.assertIn("已被修改", report["error"]["message"])
        self.assertEqual(self.d.z_moves, [])

class PredictiveProgramTests(unittest.TestCase):
    setUp = ProgramTests.setUp
    execute = ProgramTests.execute
    # Reuse the deterministic device fixture, not the inherited test cases.
    def predictive_body(self):
        from zen_scan_path import generate_path
        points = generate_path(0,0,3000,3000,rows=4,columns=5)
        step = make_step('measure_angle', experiment='Demo AF', points_um=points,z_um=100,
            predict_plane=True,prescan_indices=[0,4,15,19],prescan_half_um=30,
            half_um=10,prescan_timeout_s=20,timeout_s=5,max_prediction_residual_um=.5)
        return body([step])

    def test_twenty_points_four_first_no_duplicate_af_and_model_separate(self):
        b = self.predictive_body()
        r = self.execute(b)
        self.assertEqual(r['state'],'complete',r.get('error'))
        points = r['measurements']
        self.assertEqual([p['index'] for p in points[:4]],[1,5,16,20])
        self.assertEqual(len({p['index'] for p in points}),20)
        self.assertEqual(self.d.af_attempts,20)
        self.assertEqual([p['phase'] for p in points[:4]],['prescan']*4)
        self.assertTrue(all(p['af_half_um']==10 and p['af_timeout_s']==5 for p in points[4:]))
        self.assertTrue(all(p['af_half_um']==30 and p['af_timeout_s']==20 for p in points[:4]))
        self.assertTrue(all(abs(p['prediction_error_um'])<1e-6 for p in points[4:]))
        self.assertIn('prediction_models',r)
        self.assertEqual(len(r['result']['residuals_m']),20)
        self.assertEqual(len(json.loads((self.w.sequence_run_path/'sequence.json').read_text())['measurements']),20)

    def test_final_result_uses_actual_fine_z_without_updating_prediction(self):
        original=self.d.autofocus
        def biased(timeout):
            original(timeout)
            if self.d.af_attempts == 5: self.d.z += 2e-6
            return self.d.z
        self.d.autofocus=biased
        r=self.execute(self.predictive_body())
        self.assertEqual(r['state'],'complete',r.get('error'))
        self.assertAlmostEqual(r['measurements'][4]['prediction_error_um'],2)
        self.assertAlmostEqual(r['measurements'][5]['prediction_error_um'],0)
        self.assertGreater(r['result']['rms_m'],0)

    def test_bad_four_point_residual_blocks_fine_scan(self):
        original = self.d.autofocus
        def warped(timeout):
            z = original(timeout)
            if self.d.af_attempts == 4:
                self.d.z += 4e-6
            return self.d.z
        self.d.autofocus = warped
        r = self.execute(self.predictive_body())
        self.assertEqual(r['state'],'failed')
        self.assertEqual(self.d.af_attempts,4)
        self.assertEqual(len(r['measurements']),4)
        self.assertIsNone(r.get('result'))

    def test_prescan_failure_never_enters_fine_or_retries(self):
        from zen_hardware import AutofocusTimeout
        original = self.d.autofocus
        def fail(timeout):
            if self.d.af_attempts == 1: raise AutofocusTimeout('unknown AF state')
            return original(timeout)
        self.d.autofocus = fail
        r = self.execute(self.predictive_body())
        self.assertEqual(r['state'],'failed')
        self.assertEqual(len(r['measurements']),1)
        self.assertNotIn('prediction_models',r)

    def test_fine_failure_preserves_four_points_and_stops_without_retry(self):
        from zen_hardware import AutofocusTimeout
        original = self.d.autofocus
        def fail(timeout):
            if self.d.af_attempts == 4: raise AutofocusTimeout('fine timeout')
            return original(timeout)
        self.d.autofocus = fail
        r = self.execute(self.predictive_body())
        self.assertEqual(r['state'],'failed')
        self.assertEqual(self.d.af_attempts,4)
        self.assertEqual(len(r['measurements']),4)
        self.assertIn('prediction_models',r)

    def test_predicted_search_interval_outside_limits_blocks_fine_movement(self):
        b = self.predictive_body()
        # Four low-X points define the plane; a remaining distant point extrapolates above limit.
        p=b['program_steps'][0]['params']
        p['points_um']=[[0,0],[1000,0],[0,1000],[1000,1000],[200000,0]]
        p['prescan_indices']=[0,1,2,3]
        r=self.execute(b)
        self.assertEqual(r['state'],'failed')
        self.assertEqual(self.d.af_attempts,4)
        self.assertEqual(len(self.d.moves),4)
        self.assertNotIn('result',r)

    def test_stop_after_prescan_does_not_visit_fine_points(self):
        original = self.d.autofocus
        def stop(timeout):
            result=original(timeout)
            if self.d.af_attempts == 4: self.w.stop()
            return result
        self.d.autofocus=stop
        r=self.execute(self.predictive_body())
        self.assertEqual(r['state'],'stopped')
        self.assertEqual(self.d.af_attempts,4)
        self.assertEqual(len(self.d.moves),4)

    def test_invalid_selected_points_rejected_before_any_motion(self):
        for indices in ([0,1,2,3], [0,4,15], [0,4,15,99], [0,4,15,15]):
            b=self.predictive_body();b['program_steps'][0]['params']['prescan_indices']=indices
            with self.subTest(indices=indices), self.assertRaises(ValueError): validate_program(b)
        self.assertEqual(self.d.moves,[])
