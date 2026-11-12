"""Exercise official protobuf wire types over a local gRPC test server."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
from pathlib import Path
import tempfile
from threading import Event, Thread
import time
import unittest

import grpc
import numpy as np
from google.protobuf.message_factory import GetMessageClass

from zen_hardware.gateway import GatewayDevice
from zen_hardware.mark_gateway import MarkCapabilityError

try:
    from zen_api.acquisition.v1beta.frame_data_pb2 import FrameData
except ImportError:
    FrameData = None


@unittest.skipIf(FrameData is None, 'Generate official Gateway stubs before running wire tests')
class MarkGatewayTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.xy_missing = self.optics_missing = self.omit_xy = self.omit_z = False
        self.reject_confocal = False
        self.images_count = 1
        self.z = 100e-6
        self.xy = (0., 0.)
        self.z_moves, self.params, self.set_ids = [], {}, []
        self.subscribed, self.exposed = {}, {}
        self.snaps = self.stops = 0
        self.partial = self.stale = self.missing_frame = False
        self.reject_bounds = self.block_snap = self.no_focus = False
        self.snap_entered = Event()
        self.server = grpc.server(ThreadPoolExecutor(max_workers=10))
        modules = ('hardware.v1.simple_stage_service', 'hardware.v1.stage_service',
                   'lm.hardware.v2.focus_service', 'lm.hardware.v2.objective_changer_service',
                   'lm.hardware.v2.optovar_service', 'acquisition.v1beta.experiment_service',
                   'lm.acquisition.v1.experiment_sw_autofocus_service',
                   'acquisition.v1beta.experiment_streaming_service')
        for name in modules:
            module = importlib.import_module('zen_api.'+name+'_pb2')
            for service in module.DESCRIPTOR.services_by_name.values():
                handlers = {}
                for method in service.methods:
                    request = GetMessageClass(method.input_type)
                    response = GetMessageClass(method.output_type)
                    if method.name == 'MonitorExperiment':
                        def stream(req, ctx, response=response):
                            ctx.send_initial_metadata(())
                            self.subscribed.setdefault(req.experiment_id, Event()).set()
                            ready = self.exposed.setdefault(req.experiment_id, Event())
                            while ctx.is_active() and not ready.wait(.01):pass
                            if not ctx.is_active():return
                            if not self.missing_frame:
                                frame = self.frame(req.experiment_id)
                                yield response(frame_data=frame)
                            while ctx.is_active():time.sleep(.01)
                        handlers[method.name] = grpc.unary_stream_rpc_method_handler(
                            stream, request_deserializer=request.FromString,
                            response_serializer=lambda x:x.SerializeToString())
                    else:
                        def handler(req, ctx, method=method, response=response, service=service):
                            return self.handle(service.full_name, method.name, response, req, ctx)
                        handlers[method.name] = grpc.unary_unary_rpc_method_handler(
                            handler, request_deserializer=request.FromString,
                            response_serializer=lambda x:x.SerializeToString())
                self.server.add_generic_rpc_handlers((grpc.method_handlers_generic_handler(service.full_name, handlers),))
        port = self.server.add_insecure_port('127.0.0.1:0')
        self.server.start()
        self.address = f"127.0.0.1:{port}"
        # Test server only: production always uses secure_channel with TLS/token.
        self.device = GatewayDevice(lambda:grpc.insecure_channel(f'127.0.0.1:{port}'), rpc_timeout=.5)
        self.device.connect()
        self.addCleanup(lambda:self.server.stop(0).wait())
        self.addCleanup(self.device.close)

    def handle(self, service, method, response, req, ctx):
        self.calls.append((service, method))
        if self.xy_missing and service.endswith((".SimpleStageService", ".StageService")):
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "No XY installed")
        if service.endswith('.SimpleStageService') and method == 'GetPosition':
            return response(x=self.xy[0], y=self.xy[1])
        if method == 'GetStageMotionState':return response(state=3)
        if service.endswith('.FocusService'):
            if self.no_focus:ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'no v2 focus')
            if method == 'GetPosition':return response(value=self.z)
            if method == 'MoveTo':self.z=req.value; self.z_moves.append(self.z);return response(position_changed=True)
        if service.endswith(('.ObjectiveChangerService', '.OptovarService')) and method == 'GetPosition':
            if self.optics_missing:ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'Fixed optics')
            return response(value=1)
        if method == 'Load':
            identity = 'original:'+req.experiment_name
            self.params[identity] = dict(auto_focus_mode=1, contrast_measure=1, autofocus_sampling=1,
                search_strategy='Smart', relative_range_is_automatic=True,
                relative_search_range=50e-6, lower_limit=0, upper_limit=.001)
            return response(experiment_id=identity)
        if method == 'Clone':
            identity = 'clone:'+str(len(self.params))
            self.params[identity] = deepcopy(self.params[req.experiment_id])
            return response(experiment_id=identity)
        if method == 'GetAutofocusParameters':return response(**self.params[req.experiment_id])
        if method == 'SetAutofocusParameters':
            self.set_ids.append(req.experiment_id)
            if not self.reject_bounds:
                self.params[req.experiment_id].update(
                    search_strategy=req.search_strategy,
                    relative_range_is_automatic=req.relative_range_is_automatic.value,
                    relative_search_range=req.relative_search_range.value,
                    lower_limit=req.lower_limit.value, upper_limit=req.upper_limit.value)
            return response()
        if method == 'FindAutoFocus':
            params = self.params[req.experiment_id]
            self.z=(params['lower_limit']+params['upper_limit'])/2
            return response(focus_position=self.z)
        if method == 'GetStatus':
            return response(status={'images_count':self.images_count, 'channels_count':1, 'zstack_slices_count':1})
        if method in ('RunSnap', 'RunExperiment'):
            if method == 'RunExperiment' and self.reject_confocal:ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'No acquisition service')
            self.snaps += 1
            self.snap_entered.set()
            ready = self.subscribed.setdefault(req.experiment_id, Event())
            if not ready.wait(1):ctx.abort(grpc.StatusCode.DEADLINE_EXCEEDED, 'not subscribed')
            while self.block_snap and ctx.is_active():time.sleep(.01)
            if not ctx.is_active():ctx.abort(grpc.StatusCode.CANCELLED, 'cancelled')
            self.exposed.setdefault(req.experiment_id, Event()).set()
            return response(output_name=req.output_name)
        if method == 'Stop':self.stops += 1;return response()
        ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'not part of this test')

    def frame(self, identity):
        frame = FrameData(experiment_id='old-experiment' if self.stale else identity, frame_number=1)
        frame.frame_size.width, frame.frame_size.height = 4, 3
        frame.scaling.x = frame.scaling.y = .2e-6
        frame.frame_stage_position.x.value = self.xy[0]
        frame.frame_stage_position.y.value = self.xy[1]
        frame.frame_stage_position.z.value = self.z
        if self.omit_xy:
            frame.frame_stage_position.ClearField("x");frame.frame_stage_position.ClearField("y")
        if self.omit_z:frame.frame_stage_position.ClearField("z")
        frame.pixel_data.size.width, frame.pixel_data.size.height = 4, 3
        if self.partial:frame.pixel_data.size.height = 1
        frame.pixel_data.pixel_type = 2
        frame.pixel_data.raw_data = np.arange(12, dtype='<u2').tobytes()
        return frame

    def prepare(self):
        return self.device.prepare_mark_experiment('upper', 95e-6, 105e-6)

    def test_focus_v2_uses_metres_and_does_not_use_v1(self):
        self.assertEqual(self.device.read_z(), 100e-6)
        self.device.move_z(200e-6)
        self.assertEqual(self.z_moves, [200e-6])
        self.assertEqual(self.device.read_optics(), (1, 1))
        self.no_focus=True
        with self.assertRaises(MarkCapabilityError):self.device.read_z()

    def test_af_limits_apply_only_to_clone_and_are_read_back(self):
        result=self.prepare()
        identity=result['experiment_id']
        self.assertTrue(identity.startswith('clone:'))
        self.assertEqual(self.set_ids, [identity])
        self.assertEqual(self.params['original:upper']['search_strategy'], 'Smart')
        self.assertEqual(self.params[identity]['lower_limit'], 95e-6)
        self.assertFalse(self.params[identity]['relative_range_is_automatic'])
        self.assertAlmostEqual(self.device.autofocus(1), 100e-6)
        self.reject_bounds=True
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(self.z_moves, [])

    def test_single_snap_receives_matching_full_raw_frame(self):
        af_id=self.prepare()["experiment_id"]
        frame=self.device.capture_frame(2, Event(), 'unique-name')
        self.assertEqual(frame.xyz_m, (0, 0, 100e-6))
        self.assertEqual(frame.experiment_id, self.device._experiment_id)
        self.assertNotEqual(frame.experiment_id, af_id)
        np.testing.assert_array_equal(frame.array(), np.arange(12, dtype='<u2').reshape(3, 4))
        self.assertEqual(self.snaps, 1)
        self.assertEqual(self.stops, 0)

    def test_partial_frame_is_rejected_without_retry(self):
        self.prepare();self.partial=True
        with self.assertRaises(ValueError):self.device.capture_frame(1, Event(), 'partial')
        self.assertEqual(self.snaps, 1)
        self.assertEqual(self.stops, 1)

    def test_missing_or_old_frame_times_out_without_reexposure(self):
        for missing, stale in ((True, False), (False, True)):
            with self.subTest(missing=missing, stale=stale):
                self.prepare();self.missing_frame=missing;self.stale=stale
                before=self.snaps
                with self.assertRaises(TimeoutError):self.device.capture_frame(.2, Event(), 'missing')
                self.assertEqual(self.snaps-before, 1)

    def test_stop_cancels_snap_and_requests_experiment_stop(self):
        self.prepare();self.block_snap=True
        stop=Event();errors=[]
        def capture():
            try:self.device.capture_frame(3, stop, 'cancel')
            except Exception as error:errors.append(error)
        thread=Thread(target=capture)
        thread.start()
        try:
            self.assertTrue(self.snap_entered.wait(2))
            stop.set()
        finally:
            stop.set();thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIsInstance(errors[0], InterruptedError)
        self.assertEqual(self.stops, 1)

    def test_no_xy_connect_and_confocal_never_call_stage_or_snap(self):
        self.device.close();self.calls.clear()
        self.xy_missing=self.optics_missing=self.omit_xy=self.omit_z=True
        self.device=GatewayDevice(lambda:grpc.insecure_channel(self.address), rpc_timeout=.5, xy_enabled=False)
        self.addCleanup(self.device.close)
        self.assertEqual(self.device.connect(), 100e-6)
        self.assertEqual(self.device.read_optics(), (None, None))
        self.device.prepare_mark_experiment('confocal-upper', 95e-6, 105e-6, use_autofocus=False)
        frame=self.device.capture_frame(2, Event(), 'confocal', kind='confocal')
        self.assertEqual(frame.xyz_m, (None, None, 100e-6))
        self.assertFalse(frame.xy_verified)
        self.assertEqual(frame.z_source, 'focus_readback_after_capture')
        self.assertEqual(frame.capture_kind, 'confocal')
        self.assertFalse(any(service.endswith(('.SimpleStageService', '.StageService')) for service, method in self.calls))
        self.assertNotIn('RunSnap', [method for service, method in self.calls])
        self.assertIn('RunExperiment', [method for service, method in self.calls])
        self.assertNotIn('SetAutofocusParameters', [method for service, method in self.calls])

    def test_confocal_unavailable_never_falls_back_to_camera(self):
        self.prepare();self.calls.clear();self.reject_confocal=True
        with self.assertRaises(grpc.RpcError):self.device.capture_frame(2, Event(), 'confocal', kind='confocal')
        methods=[method for service,method in self.calls]
        self.assertIn('RunExperiment', methods)
        self.assertNotIn('RunSnap', methods)

    def test_multiplane_confocal_experiment_is_rejected(self):
        self.prepare();self.images_count=2
        with self.assertRaises(ValueError):self.device.capture_frame(2, Event(), 'stack', kind='confocal')
        self.assertEqual(self.stops, 1)
