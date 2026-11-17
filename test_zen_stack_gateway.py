"""Full Z-stack over ZEISS official protobufs, including 3.12 package variants."""

from threading import Event
import unittest
import test_zen_mark_gateway as wire

FrameData = wire.FrameData


@unittest.skipIf(FrameData is None, "Generate official Gateway stubs")
class StackGatewayTests(unittest.TestCase):
    setUp = wire.MarkGatewayTest.setUp
    frame = wire.MarkGatewayTest.frame

    def handle(self, service, method, response, req, ctx):
        if method == "GetZStackInfo":
            first, last, interval = self.zstack_writes.get(
                req.experiment_id, (100e-6, 102e-6, 1e-6)
            )
            return response(
                first_slice=first, last_slice=last, interval=interval, num_slices=3
            )
        if method == "GetStatus":
            return response(
                status={
                    "images_count": 3,
                    "channels_count": 1,
                    "zstack_slices_count": 3,
                }
            )
        return wire.MarkGatewayTest.handle(self, service, method, response, req, ctx)

    def frames_for(self, identity):
        frames = []
        for i in range(3):
            f = self.frame(identity)
            f.frame_position.z = i
            f.frame_stage_position.z.value = (100 + i) * 1e-6
            frames.append(f)
        if getattr(self, "missing_layer", False):
            return frames[:2]
        if getattr(self, "duplicate_layer", False):
            frames[2].frame_position.z = 1
        if getattr(self, "extra_channel", False):
            frames[1].frame_position.c = 1
        return frames

    def run_stack(self):
        self.zstack_enabled = True
        self.device.load_experiment("Upper")
        return self.device.capture_stack(
            100e-6, 102e-6, 1e-6, 0.35, Event(), "unique-stack"
        )

    def test_stack_preserves_three_planes_with_actual_z_and_never_collapses(self):
        frames = self.run_stack()
        self.assertEqual(len(frames), 3)
        self.assertEqual([round(f.xyz_m[2] * 1e6) for f in frames], [100, 101, 102])
        self.assertEqual(self.snaps, 1)
        self.assertTrue(
            all(first != last for first, last, _ in self.zstack_writes.values())
        )

    def test_missing_plane_stops_and_does_not_report_complete(self):
        self.missing_layer = True
        with self.assertRaises((TimeoutError, wire.GatewayCallError)):
            self.run_stack()
        self.assertEqual(self.stops, 1)

    def test_duplicate_plane_fails_instead_of_silently_overwriting(self):
        self.duplicate_layer = True
        with self.assertRaises(ValueError):
            self.run_stack()
        self.assertEqual(self.stops, 1)

    def test_extra_channel_fails_without_flattening(self):
        self.extra_channel = True
        with self.assertRaises(ValueError):
            self.run_stack()

    def test_no_xy_stack_keeps_z_metadata_without_stage_calls(self):
        self.device.xy_enabled = False
        self.omit_xy = True
        frames = self.run_stack()
        self.assertEqual(frames[0].xyz_m[:2], (None, None))
        self.assertFalse(frames[0].xy_verified)

    def test_loaded_af_clones_current_experiment_without_loading_again(self):
        self.device.load_experiment("Upper")
        before = sum(method == "Load" for _, method in self.calls)
        result = self.device.prepare_loaded_autofocus(95e-6, 105e-6)
        self.assertEqual(sum(method == "Load" for _, method in self.calls), before)
        self.assertAlmostEqual(result["lower_m"], 95e-6)
        self.assertNotEqual(self.device._experiment_id, "original:Upper")
