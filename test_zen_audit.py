"""Audit trail: interlock, recovery attestations, snapshots and artifacts."""
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event

from zen_fit import DEMO_CONVENTION
from zen_hardware.mark_simulator import SimulatedSequenceDevice
from zen_hardware.interlock import (MotionInterlock, SequenceStopped,
                                    enforces_safe_z)
from zen_marks import MarkLayer, MarkRecipe
from zen_records import (canonical_sha256, confirm_recovery, pending_recovery,
                         software_version, utc_now, write_json, write_manifest)
from zen_sequence import SequencePlan, Station, build_snapshot
from zen_workflow import Recipe


class TracedDevice(SimulatedSequenceDevice):
    def __init__(self):
        super().__init__()
        self.trace = []

    def move_xy(self, x, y):
        self.trace.append(("xy", x, y, self.z))
        return super().move_xy(x, y)

    def move_z(self, z):
        self.trace.append(("z", z))
        return super().move_z(z)


def plan():
    tilt = Recipe("A", "Demo AF", ((0., 0.), (.0005, 0.), (0., .0005)),
                  1e-6, 2, .5, DEMO_CONVENTION)
    capture = MarkRecipe(MarkLayer("Demo upper", 100e-6, 5e-6),
                         MarkLayer("Demo lower", 200e-6, 5e-6),
                         use_autofocus=False, capture_kind="confocal")
    return SequencePlan("A", tilt, (Station("H1", .002, .0015, capture),),
                        20e-6, 100e-6, 0., 300e-6, 10000., 10e-6,
                        settle_s=.001, simulated_configuration=True)


class InterlockTests(unittest.TestCase):
    def setUp(self):
        self.device = TracedDevice()
        self.device.connect()
        self.stop = Event()
        self.interlock = MotionInterlock(
            self.device, safe_z_m=20e-6, z_min_m=0., z_max_m=300e-6,
            z_tolerance_m=.5e-6, xy_tolerance_m=1e-6,
            arrival_timeout_s=.5, settle_s=.001, stop=self.stop)

    def test_every_xy_move_goes_through_safe_z(self):
        self.interlock.move_xy(.001, .001)
        kinds = [row[0] for row in self.device.trace]
        first_xy = kinds.index("xy")
        self.assertEqual(self.device.trace[first_xy - 1], ("z", 20e-6))
        self.assertTrue(enforces_safe_z(self.interlock))
        self.assertFalse(enforces_safe_z(self.device))

    def test_command_log_records_confirmed_pairs(self):
        self.interlock.move_xy(.001, .001)
        log = self.interlock.command_log_snapshot()
        self.assertIn("move_z", [e["command"] for e in log])
        self.assertIn("move_xy", [e["command"] for e in log])
        self.assertTrue(all(e["outcome"] == "confirmed" for e in log))
        self.assertTrue(all(e.get("confirmed_at") for e in log))

    def test_stop_blocks_further_commands(self):
        self.stop.set()
        with self.assertRaises(SequenceStopped):
            self.interlock.move_xy(.001, .001)
        self.assertEqual(self.device.trace, [])


class RecoveryAttestationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        self.run = self.root / "demo" / "run-1"
        self.run.mkdir(parents=True)
        write_json(self.run / "status.json",
                   {"state": "running", "recovery_required": True,
                    "result_valid": False, "started_at": utc_now()})

    def test_legacy_call_still_unlocks(self):
        self.assertTrue(pending_recovery(self.root, simulated=True))
        confirm_recovery(self.run, xy_confirmed=True, af_z_confirmed=True)
        self.assertFalse(pending_recovery(self.root, simulated=True))

    def test_items_operator_and_append_only_log(self):
        items = [
            {"key": "xy_idle", "label": "XY 静止", "confirmed": True},
            {"key": "af_z_idle", "label": "AF/Z 结束", "confirmed": True},
            {"key": "experiment_idle", "label": "实验已停止", "confirmed": True},
            {"key": "optics_unchanged", "label": "光路未变", "confirmed": True},
        ]
        record = confirm_recovery(self.run, operator="王工", items=items,
                                  reason="现场核对完成",
                                  readbacks={"z_m": 2e-5})
        self.assertEqual(record["operator"], "王工")
        self.assertEqual(len(record["items"]), 4)
        self.assertEqual(len(record["attestation_sha256"]), 64)
        lines = (self.run / "recovery.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["run_id"], "run-1")
        confirm_recovery(self.run, xy_confirmed=True, af_z_confirmed=True)
        lines = (self.run / "recovery.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertFalse(pending_recovery(self.root, simulated=True))

    def test_unchecked_item_is_refused(self):
        items = [{"key": "xy_idle", "confirmed": True},
                 {"key": "af_z_idle", "confirmed": False}]
        with self.assertRaises(ValueError):
            confirm_recovery(self.run, items=items)
        self.assertTrue(pending_recovery(self.root, simulated=True))


class RecordIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_canonical_hash_ignores_key_order(self):
        self.assertEqual(canonical_sha256({"a": 1, "b": 2}),
                         canonical_sha256({"b": 2, "a": 1}))
        self.assertNotEqual(canonical_sha256({"a": 1}), canonical_sha256({"a": 2}))

    def test_software_version_is_not_a_hardcoded_release(self):
        self.assertNotEqual(software_version(), "2.6.0")
        self.assertTrue(software_version())

    def test_manifest_hashes_artifact_content(self):
        first = self.root / "upper.tif"
        second = self.root / "lower.tif"
        first.write_bytes(b"image-a")
        second.write_bytes(b"image-b")
        document = write_manifest(self.root, run_id="run-1",
                                  files=[first, second], base=self.root)
        self.assertEqual(document["artifact_count"], 2)
        entry = next(a for a in document["artifacts"] if a["rel_path"] == "upper.tif")
        self.assertEqual(entry["sha256"], hashlib.sha256(b"image-a").hexdigest())
        first.write_bytes(b"tampered")
        again = write_manifest(self.root, run_id="run-1",
                               files=[first, second], base=self.root)
        self.assertNotEqual(again["manifest_sha256"], document["manifest_sha256"])

    def test_snapshot_hash_is_stable_and_sensitive(self):
        original = plan()
        a = build_snapshot(original, (0., 0.), {"sample": "S1"}, None, None)
        b = build_snapshot(original, (0., 0.), {"sample": "S1"}, None, None)
        self.assertEqual(a["snapshot_sha256"], b["snapshot_sha256"])
        changed = build_snapshot(replace(original, max_rms_m=5e-6),
                                 (0., 0.), {"sample": "S1"}, None, None)
        self.assertNotEqual(a["snapshot_sha256"], changed["snapshot_sha256"])
        self.assertEqual(a["plan"]["kind"], "sequence")


if __name__ == "__main__":
    unittest.main()
