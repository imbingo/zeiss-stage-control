"""Recipe compiler and release evidence regressions."""

import json, tempfile, unittest
from pathlib import Path
from copy import deepcopy
from zen_recipe_store import RecipeLibrary, default_body
from zen_recipe_compile import compile_revision, compile_body
from zen_records import write_json


def valid_body():
    b = default_body("现场测试", example=True)
    b["example"] = False
    return b


class RecipeLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = RecipeLibrary(self.root / "recipes.json")
        self.rid, self.rev = self.lib.create("现场测试", "tester", body=valid_body())

    def trial(self, **overrides):
        v = self.lib.get(self.rid, self.rev)
        report = {
            "state": "complete",
            "simulated": False,
            "plan": {
                "recipe_identity": {
                    "recipe_id": self.rid,
                    "revision": self.rev,
                    "content_sha256": v["content_sha256"],
                }
            },
            "experiment_fingerprints": {
                "Demo AF": "a" * 64,
                "Upper_mark": "b" * 64,
                "Lower_mark": "c" * 64,
            },
        }
        report.update(overrides)
        path = self.root / "trial"
        path.mkdir(exist_ok=True)
        write_json(path / "sequence.json", report)
        return path

    def test_save_draft_keeps_revision_and_persists_changed_coordinates(self):
        b = valid_body()
        b["step_tree"][4]["children"][0]["params"]["x_um"] = 4321
        self.lib.save_draft(self.rid, self.rev, b, by="tester", note="改坐标")
        b["step_tree"][4]["children"][0]["params"]["x_um"] = 1
        reloaded = RecipeLibrary(self.lib.path)
        p = compile_revision(
            reloaded, self.rid, self.rev, mode="engineering", simulated=False
        )
        self.assertAlmostEqual(p.stations[0].x_m, 0.004321)
        self.assertEqual(len(reloaded.find(self.rid)["revisions"]), 1)

    def test_structural_check_cannot_release_without_field_trial(self):
        self.assertEqual(self.lib.check(self.rid, self.rev)["result"], "passed")
        with self.assertRaises(ValueError):
            self.lib.release(self.rid, self.rev, by="tester")

    def test_release_requires_current_successful_real_complete_run(self):
        self.lib.check(self.rid, self.rev)
        for override in [
            {"state": "failed"},
            {"simulated": True},
            {"experiment_fingerprints": {}},
        ]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.lib.record_trial(
                    self.rid, self.rev, self.trial(**override), by="tester"
                )
        self.lib.record_trial(self.rid, self.rev, self.trial(), by="tester")
        self.lib.release(self.rid, self.rev, by="tester")
        p = compile_revision(
            self.lib, self.rid, self.rev, mode="production", simulated=False
        )
        self.assertEqual(len(p.recipe_identity["experiment_fingerprints"]), 3)
        with self.assertRaises(ValueError):
            self.lib.save_draft(self.rid, self.rev, valid_body(), by="tester", note="")

    def test_old_trial_and_single_step_cannot_qualify(self):
        path = self.trial()
        body = valid_body()
        body["common_params"]["safe_z_um"] = 25
        self.lib.save_draft(self.rid, self.rev, body, by="tester", note="new")
        with self.assertRaises(ValueError):
            self.lib.record_trial(self.rid, self.rev, path, by="tester")
        path = self.trial()
        report = json.loads((path / "sequence.json").read_text(encoding="utf-8"))
        report["plan"]["recipe_identity"]["execution_scope"] = "H01"
        write_json(path / "sequence.json", report)
        with self.assertRaises(ValueError):
            self.lib.record_trial(self.rid, self.rev, path, by="tester")

    def test_example_never_runs_on_real_device_or_releases(self):
        rid, rev = self.lib.create("demo", "tester", body=default_body(example=True))
        self.lib.check(rid, rev)
        with self.assertRaises(ValueError):
            compile_revision(self.lib, rid, rev, mode="engineering", simulated=False)
        with self.assertRaises(ValueError):
            self.lib.release(rid, rev, by="tester")

    def test_invalid_values_and_reversed_steps_fail(self):
        for key, val in [
            ("capture_timeout_s", 0),
            ("z_tolerance_um", 0),
            ("safe_z_um", 999),
            ("settle_s", float("nan")),
        ]:
            b = valid_body()
            b["common_params"][key] = val
            with self.subTest(key=key), self.assertRaises(ValueError):
                compile_body(b)
        b = valid_body()
        b["step_tree"].reverse()
        with self.assertRaises(ValueError):
            compile_body(b)

    def test_capture_only_template_does_not_require_af_or_tilt_points(self):
        b = valid_body()
        b["template"] = "仅上下孔采图"
        b["tilt"] = {}
        b["step_tree"] = [
            n for n in b["step_tree"] if n["kind"] not in ("tilt", "gate")
        ]
        p = compile_body(b)
        self.assertFalse(p.measure_enabled)
        self.assertEqual(len(p.stations), 1)

    def test_new_real_recipe_has_no_teaching_defaults(self):
        b = default_body()
        self.assertEqual(b["common_params"]["safe_z_um"], "")
        self.assertEqual(b["tilt"]["points_um"], [])
        with self.assertRaises(ValueError):
            compile_body(b)

    def test_revision_copy_compare_and_archive(self):
        b = valid_body()
        b["common_params"]["safe_z_um"] = 30
        rev = self.lib.new_revision(self.rid, b, by="tester", change_note="new")
        self.assertEqual(rev, 2)
        self.assertGreater(self.lib.compare(self.rid, 1, 2)["change_count"], 0)
        self.lib.archive(self.rid, rev, by="tester")
        with self.assertRaises(ValueError):
            compile_revision(
                self.lib, self.rid, rev, mode="engineering", simulated=False
            )

    def test_corrupt_library_is_not_overwritten(self):
        self.lib.path.write_text("{bad")
        lib = RecipeLibrary(self.lib.path)
        with self.assertRaises(ValueError):
            lib.create("new", "tester")
        self.assertEqual(lib.path.read_text(encoding="utf-8"), "{bad")
