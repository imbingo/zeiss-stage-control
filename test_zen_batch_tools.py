"""Selectable checks, zero targets, continued reporting and durable templates."""

import json, tempfile, unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch
import grpc
from zen_hardware.batch import execute_batch
from zen_hardware.library import HardwareTestSession
from zen_hardware.mark_simulator import SimulatedMarkDevice
from zen_hardware.rpc_diagnostics import GatewayCallError
from zen_device_policy import use_live_checks
from zen_templates import TemplateStore
from zen_recipe_store import RecipeLibrary, default_body
from zen_steps import make_step, flatten


def row(key, selected=True, **p):
    return {"operation": key, "selected": selected, "parameters": p}


class BatchToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        use_live_checks(self.root)
        self.d = SimulatedMarkDevice(xy_enabled=False)
        self.d.connect_transport()
        self.s = HardwareTestSession(self.root)
        self.s.bind(self.d, simulated=True)

    def run_batch(self, plan, **kw):
        return execute_batch(
            self.s, plan, timeout_s=0.08, stop=kw.pop("stop", Event()), **kw
        )

    def test_unselected_never_calls_api_and_no_xy_never_reads_stage(self):
        self.d.read_xy = lambda: (_ for _ in ()).throw(AssertionError("XY forbidden"))
        report = self.run_batch([row("snap", False), row("read_xy"), row("read_z")])
        self.assertEqual(self.d.lab_calls, ["read_z"])
        self.assertEqual(
            [r["result"] for r in report["items"]], ["未勾选", "不适用", "OK"]
        )

    def test_failed_method_records_advice_and_continues_next_check(self):
        original = self.d.call_hardware_operation

        def api(key, p):
            if key == "list_cameras":
                raise GatewayCallError(
                    "Camera.GetCameras", grpc.StatusCode.UNIMPLEMENTED
                )
            return original(key, p)

        self.d.call_hardware_operation = api
        r = self.run_batch([row("list_cameras"), row("read_z")])
        self.assertEqual(r["state"], "complete")
        self.assertEqual(r["counts"]["不OK"], 1)
        self.assertEqual(r["items"][1]["result"], "OK")
        self.assertIn("命名空间", r["items"][0]["advice"])
        self.assertTrue((Path(r["directory"]) / "report.json").exists())
        self.assertIn(
            "不OK", (Path(r["directory"]) / "report.md").read_text(encoding="utf-8")
        )

    def test_loaded_id_is_resolved_for_subsequent_checks(self):
        r = self.run_batch(
            [
                row("connect"),
                row("load_experiment", experiment_name="Upper"),
                row("export_experiment", experiment_id="@experiment_id"),
            ]
        )
        self.assertEqual(r["items"][2]["parameters"]["experiment_id"], "demo-loaded")
        self.assertEqual(r["items"][2]["result"], "OK")

    def test_missing_dependency_fails_only_that_item(self):
        r = self.run_batch(
            [row("export_experiment", experiment_id="@experiment_id"), row("read_z")]
        )
        self.assertEqual(r["items"][0]["result"], "不OK")
        self.assertEqual(r["items"][1]["result"], "OK")

    def test_stop_prevents_next_check_and_final_zero(self):
        stop = Event()

        def progress(report):
            if report["items"] and report["items"][0].get("result"):
                stop.set()

        r = self.run_batch(
            [row("read_z"), row("load_experiment", experiment_name="Upper")],
            stop=stop,
            progress=progress,
            zero_z=True,
        )
        self.assertEqual(r["state"], "stopped")
        self.assertEqual(self.d.lab_calls, ["read_z"])
        self.assertEqual(self.d.z_moves, [])

    def test_zero_is_absolute_zero_on_installed_axes_and_never_homing(self):
        self.d.xy_enabled = True
        self.d.xy = (0.001, 0.002)
        r = self.run_batch([], zero_z=True, zero_xy=True)
        self.assertEqual(r["counts"]["OK"], 2)
        self.assertAlmostEqual(self.d.z, 0)
        self.assertEqual(self.d.read_xy(), (0, 0))
        self.assertEqual(r["zero_mode"], "absolute_coordinate_0_not_homing")

    def test_uncertain_motion_blocks_new_writes_but_read_checks_continue(self):
        self.d._experiment_id = "running"
        original = self.d.call_hardware_operation

        def api(key, p):
            if key == "af":
                raise TimeoutError("still running")
            if key == "experiment_status":
                return {"is_experiment_running": True, "is_acquisition_running": True}
            return original(key, p)

        self.d.call_hardware_operation = api
        r = self.run_batch(
            [
                row("af", experiment_id="running", af_timeout_s=1),
                row("move_z", z_um=0),
                row("read_z"),
            ]
        )
        self.assertEqual(self.d.z_moves, [])
        self.assertEqual(r["items"][-1]["result"], "OK")
        self.assertEqual(r["counts"]["不OK"], 2)

    def test_templates_persist_and_remap_internal_references(self):
        m = make_step("measure_angle")
        g = make_step("angle_gate", source=m["step_id"])
        store = TemplateStore(self.root / "templates.json")
        store.save("自定义流程", {"program_steps": [m, g]})
        entry = TemplateStore(store.path).load()[0]
        nodes = store.instantiate(entry)
        self.assertNotEqual(nodes[0]["step_id"], m["step_id"])
        self.assertEqual(nodes[1]["params"]["source"], nodes[0]["step_id"])

    def test_corrupt_template_store_is_not_overwritten(self):
        path = self.root / "templates.json"
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            TemplateStore(path).save("test", {"program_steps": [make_step("wait")]})
        self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_delete_hides_recipe_but_preserves_immutable_version_evidence(self):
        lib = RecipeLibrary(self.root / "recipes.json")
        rid, rev = lib.create("Test", "E1", body=default_body())
        digest = lib.get(rid, rev)["content_sha256"]
        lib.delete_recipe(rid, by="E1")
        self.assertEqual(lib.recipes(), [])
        self.assertEqual(lib.get(rid, rev)["content_sha256"], digest)

    def test_stop_interface_can_pass_live_checks_without_questionnaire(self):
        report = self.run_batch([row("stop_z"), row("read_z")])
        self.assertEqual([r["result"] for r in report["items"]], ["OK", "OK"])
        self.assertFalse(self.s.uncertain)

    def test_success_without_position_evidence_is_not_falsely_ok(self):
        self.d.optics = (None, None)
        self.d.read_z = lambda: float("nan")
        report = self.run_batch([row("read_optics"), row("read_z")])
        self.assertEqual(report["counts"]["不OK"], 2)
        self.assertIn("未完整核验", report["items"][0]["message"])
