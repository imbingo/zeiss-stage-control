"""Two/three-face acquisition contracts including partial data and motion gates."""

import csv, json, time, unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from zen_steps import make_step, validate_program, capability_issues
from zen_heights import default_surfaces, face_parameters
from test_zen_program import body
import test_zen_program as program_fixture
from zen_hardware import AutofocusTimeout


def height_step(count=3, current=False):
    faces = default_surfaces()
    for i, face in enumerate(faces):
        face.update(experiment=f"Demo Z{i+1}", z_um=(100, 160, 230)[i])
    from zen_scan_path import generate_path

    return make_step(
        "measure_heights",
        surface_count=count,
        xy_mode="current" if current else "motorized",
        surfaces=faces,
        points_um=[] if current else generate_path(0, 0, 30000, 30000),
        prescan_indices=[] if current else [0, 4, 15, 19],
    )


class HeightTests(unittest.TestCase):
    setUp = program_fixture.ProgramTests.setUp

    def execute(self, b, **kw):
        self.w.start_program(
            validate_program(b),
            self.base / "images",
            session={"sample": "H1", "operator": "E1"},
            preflight=kw.get("preflight", {}),
            confirmed=True,
        )
        end = time.monotonic() + 60
        while self.w.busy and time.monotonic() < end:
            time.sleep(0.005)
        self.assertFalse(self.w.busy)
        return self.w.sequence_report

    def scan(self, report):
        return next(iter(report["height_scans"].values()))

    def test_three_faces_twenty_points_sixty_af_and_csv_units(self):
        r = self.execute(body([height_step()]))
        self.assertEqual(r["state"], "complete", r.get("error"))
        s = self.scan(r)
        self.assertTrue(s["data_complete"])
        self.assertTrue(s["return_complete"])
        self.assertEqual(self.d.af_attempts, 60)
        self.assertEqual([p["point_no"] for p in s["rows"][:4]], [1, 5, 16, 20])
        self.assertEqual(len(s["models"]), 3)
        for row in s["rows"]:
            self.assertLess(row["Z1_um"], row["Z2_um"])
            self.assertLess(row["Z2_um"], row["Z3_um"])
            self.assertAlmostEqual(row["dZ21_um"], row["Z2_um"] - row["Z1_um"])
            self.assertAlmostEqual(row["dZ32_um"], row["Z3_um"] - row["Z2_um"])
            self.assertEqual(len(row["faces"]), 3)
        self.assertAlmostEqual(self.d.z, 20e-6)
        with (Path(s["record_directory"]) / "heights.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            data = list(reader)
        self.assertEqual(len(data), 20)
        self.assertIn("dZ32_um", reader.fieldnames)
        self.assertEqual(len(r["experiment_fingerprints"]), 3)
        self.assertNotIn("result", r)  # no fake angle output

    def test_two_faces_and_fixed_no_xy_do_not_read_stage(self):
        self.d.xy_enabled = False

        def forbidden(*a):
            raise AssertionError("XY forbidden")

        self.d.read_xy = self.d.motion_state = self.d.move_xy = forbidden
        r = self.execute(body([height_step(2, current=True)]))
        self.assertEqual(r["state"], "complete", r.get("error"))
        s = self.scan(r)
        row = s["rows"][0]
        self.assertEqual(self.d.af_attempts, 2)
        self.assertIsNone(row["X_um"])
        self.assertIsNone(row["Y_um"])
        self.assertNotIn("Z3_um", row)
        self.assertNotIn("dZ32_um", row)
        self.assertFalse(s["xy_verified"])
        self.assertAlmostEqual(row["dZ21_um"], 60)
        self.assertFalse(s["models"])

    def test_third_face_failure_keeps_first_difference_and_blanks(self):
        original = self.d.autofocus

        def fail(timeout):
            if self.d.experiment == "Demo Z3":
                raise AutofocusTimeout("unknown AF state")
            return original(timeout)

        self.d.autofocus = fail
        r = self.execute(body([height_step(current=True)]))
        s = self.scan(r)
        row = s["rows"][0]
        self.assertEqual(r["state"], "failed")
        self.assertFalse(s["data_complete"])
        self.assertAlmostEqual(row["dZ21_um"], 60)
        self.assertIsNone(row["Z3_um"])
        self.assertIsNone(row["dZ32_um"])
        self.assertEqual(row["status"], "partial")
        self.assertEqual(self.d.af_attempts, 2)
        with (Path(s["record_directory"]) / "heights.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            csvrow = next(csv.DictReader(handle))
        self.assertEqual(csvrow["Z3_um"], "")
        self.assertEqual(csvrow["dZ32_um"], "")
        self.assertNotAlmostEqual(self.d.z, 20e-6)  # failure does not add return

    def test_wrong_order_keeps_negative_difference_and_stops(self):
        original = self.d.autofocus

        def reverse(timeout):
            if self.d.experiment == "Demo Z2":
                self.d.af_attempts += 1
                self.d.z = 90e-6
                return self.d.z
            return original(timeout)

        step = height_step(current=True)
        step["params"]["surfaces"][1]["z_um"] = 90
        self.d.autofocus = reverse
        r = self.execute(body([step]))
        row = self.scan(r)["rows"][0]
        self.assertEqual(r["state"], "failed")
        self.assertEqual(row["status"], "invalid_order")
        self.assertAlmostEqual(row["dZ21_um"], -10)
        self.assertIsNone(row["Z3_um"])
        self.assertEqual(self.d.af_attempts, 2)

    def test_shared_and_override_af_ranges_and_timeouts(self):
        step = height_step(2)
        p = step["params"]
        p["surfaces"][1].update(
            override=True,
            prescan_half_um=25,
            prescan_timeout_s=55,
            half_um=5,
            timeout_s=12,
        )
        r = self.execute(body([step]))
        self.assertEqual(r["state"], "complete", r.get("error"))
        s = self.scan(r)
        self.assertEqual(
            [(f["half_um"], f["timeout_s"]) for f in s["rows"][0]["faces"]],
            [(30, 60), (25, 55)],
        )
        self.assertEqual(
            [(f["half_um"], f["timeout_s"]) for f in s["rows"][4]["faces"]],
            [(10, 30), (5, 12)],
        )
        self.assertEqual(self.d.af_attempts, 40)

    def test_cumulative_xy_drift_is_checked_against_point_anchor(self):
        original = self.d.autofocus

        def drift(timeout):
            z = original(timeout)
            self.d.xy = (self.d.xy[0] + 0.4e-6, self.d.xy[1])
            return z

        self.d.autofocus = drift
        r = self.execute(body([height_step(current=True)]))
        s = self.scan(r)
        self.assertEqual(r["state"], "failed")
        self.assertIn("累计", s["error"])
        self.assertIsNone(s["rows"][0]["Z3_um"])

    def test_return_failure_not_complete_even_when_data_collected(self):
        original = self.d.move_z

        def fail(z):
            if self.d.af_attempts == 3 and abs(z - 20e-6) < 1e-12:
                raise TimeoutError("return failed")
            return original(z)

        self.d.move_z = fail
        r = self.execute(body([height_step(current=True)]))
        s = self.scan(r)
        self.assertEqual(r["state"], "failed")
        self.assertTrue(s["data_complete"])
        self.assertFalse(s["return_complete"])
        self.assertEqual(s["rows"][0]["status"], "complete")

    def test_prescan_model_failure_blocks_all_fine_af(self):
        original = self.d.autofocus

        def warp(timeout):
            z = original(timeout)
            if self.d.af_attempts == 12:
                self.d.z += 12e-6
            return self.d.z

        self.d.autofocus = warp
        r = self.execute(body([height_step()]))
        s = self.scan(r)
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.d.af_attempts, 12)
        self.assertEqual(len(s["rows"]), 4)
        self.assertFalse(s["data_complete"])

    def test_stop_does_not_start_next_face_or_return(self):
        original = self.d.autofocus

        def stop(timeout):
            z = original(timeout)
            self.w.stop()
            return z

        self.d.autofocus = stop
        r = self.execute(body([height_step(current=True)]))
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(self.d.af_attempts, 1)
        self.assertFalse(self.scan(r)["return_complete"])

    def test_invalid_configs_and_no_xy_path_rejected_before_motion(self):
        for change in (
            lambda p: p.update(prescan_indices=[0, 1, 2, 3]),
            lambda p: p["surfaces"][0].update(experiment=""),
            lambda p: p["surfaces"][1].update(z_um=299),
        ):
            s = height_step()
            change(s["params"])
            with self.assertRaises(ValueError):
                validate_program(body([s]))
        plan = validate_program(body([height_step()]))
        self.d.xy_enabled = False
        self.assertTrue(capability_issues(plan, self.d))
        self.assertEqual(self.d.moves, [])

    def test_any_face_prediction_outside_z_limits_blocks_fine_scan(self):
        s = height_step()
        p = s["params"]
        p["points_um"] = [[0, 0], [1000, 0], [0, 1000], [1000, 1000], [600000, 0]]
        p["prescan_indices"] = [0, 1, 2, 3]
        r = self.execute(body([s]))
        scan = self.scan(r)
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.d.af_attempts, 12)
        self.assertEqual(len(self.d.moves), 4)
        self.assertEqual(len(scan["rows"]), 4)

    def test_save_failure_stops_before_next_surface(self):
        import zen_heights

        save = zen_heights.save_height_scan

        def failing(scan):
            if scan["rows"] and scan["rows"][-1].get("Z2_um") is not None:
                raise OSError("disk full")
            return save(scan)

        with patch("zen_heights.save_height_scan", side_effect=failing):
            r = self.execute(body([height_step(current=True)]))
        self.assertEqual(r["state"], "failed")
        self.assertEqual(self.d.af_attempts, 2)
        self.assertFalse(self.scan(r)["data_complete"])
        self.assertNotAlmostEqual(self.d.z, 20e-6)

    def test_af_clone_id_is_recorded_separately_from_loaded_id(self):
        original = self.d.prepare_loaded_autofocus

        def clone(low, high):
            reply = original(low, high)
            self.d._experiment_id = f"af-clone-{self.d.af_attempts+1}"
            reply["experiment_id"] = self.d._experiment_id
            return reply

        self.d.prepare_loaded_autofocus = clone
        r = self.execute(body([height_step(2, current=True)]))
        self.assertEqual(r["state"], "complete", r.get("error"))
        samples = self.scan(r)["rows"][0]["faces"]
        self.assertEqual(
            [f["experiment_id"] for f in samples], ["af-clone-1", "af-clone-2"]
        )
        self.assertEqual(
            [f["loaded_experiment_id"] for f in samples], ["demo-clone", "demo-clone"]
        )

    def test_whitespace_in_experiment_name_has_consistent_trial_fingerprint(self):
        from zen_heights import validate_height_trial

        s = height_step(2, current=True)
        s["params"]["surfaces"][0]["experiment"] = " Demo Z1 "
        b = body([s])
        r = self.execute(b)
        self.assertEqual(r["state"], "complete", r.get("error"))
        validate_height_trial(b, r)

    def test_return_checks_xy_against_original_point_anchor(self):
        move = self.d.move_z

        def drift(z):
            move(z)
            # Small per-move XY drift accumulates across faces and the final return.
            self.d.xy = (self.d.xy[0] + 0.3e-6, self.d.xy[1])

        self.d.move_z = drift
        r = self.execute(body([height_step(current=True)]))
        scan = self.scan(r)
        self.assertEqual(r["state"], "failed")
        self.assertTrue(scan["data_complete"])
        self.assertFalse(scan["return_complete"])
        self.assertIn("累计", scan["error"])
