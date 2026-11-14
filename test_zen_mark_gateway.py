"""Exercise official protobuf wire types over a local gRPC test server."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib
import json
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
from zen_hardware.rpc_diagnostics import GatewayCallError

try:
    from zen_api.acquisition.v1beta.frame_data_pb2 import FrameData
except ImportError:
    FrameData = None


@unittest.skipIf(FrameData is None, 'Generate official Gateway stubs before running wire tests')
class MarkGatewayTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.reject_method = None
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
        self.reject_v1_af = self.reject_beta_af = False
        self.zstack_writes = {}
        self.zstack_enabled = False
        self.zstack_slices = 6
        self.snap_entered = Event()
        self.server = grpc.server(ThreadPoolExecutor(max_workers=10))
        self._service_names = []
        modules = ('hardware.v1.simple_stage_service', 'hardware.v1.stage_service',
                   'lm.hardware.v1.focus_service', 'lm.hardware.v2.focus_service',
                   'lm.hardware.v2.objective_changer_service',
                   'lm.hardware.v2.optovar_service', 'acquisition.v1beta.experiment_service',
                   'lm.acquisition.v1.experiment_sw_autofocus_service',
                   'lm.acquisition.v1.definite_focus_service',
                   'acquisition.v1beta.experiment_streaming_service')
        for name in modules:
            module = importlib.import_module('zen_api.'+name+'_pb2')
            for service in module.DESCRIPTOR.services_by_name.values():
                self._service_names.append(service.full_name)
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
        # Renamed-package service twins: identical wire schemas re-registered
        # under the package names ZEN 3.12 actually serves (colleague's path).
        from google.protobuf import descriptor_pb2
        from grpc_reflection.v1alpha import reflection_pb2
        self._variant_fds = {}
        def register_twin(module_name, new_package, rename=None):
            module = importlib.import_module('zen_api.'+module_name+'_pb2')
            old_package = module.DESCRIPTOR.package
            for service in module.DESCRIPTOR.services_by_name.values():
                twin = service.full_name.replace(old_package, new_package)
                if rename:twin=twin.rsplit('.',1)[0]+'.'+rename
                handlers = {}
                for method in service.methods:
                    request = GetMessageClass(method.input_type)
                    response = GetMessageClass(method.output_type)
                    def handler(req, ctx, method=method, response=response, twin=twin):
                        return self.handle(twin, method.name, response, req, ctx)
                    handlers[method.name] = grpc.unary_unary_rpc_method_handler(
                        handler, request_deserializer=request.FromString,
                        response_serializer=lambda x:x.SerializeToString())
                self.server.add_generic_rpc_handlers((
                    grpc.method_handlers_generic_handler(twin, handlers),))
                self._service_names.append(twin)
            twin_file = descriptor_pb2.FileDescriptorProto()
            twin_file.ParseFromString(module.DESCRIPTOR.serialized_pb)
            twin_file.package = new_package
            for service in twin_file.service:
                if rename:service.name=rename
                for method in service.method:
                    method.input_type = method.input_type.replace(old_package+'.', new_package+'.')
                    method.output_type = method.output_type.replace(old_package+'.', new_package+'.')
            fds = [twin_file.SerializeToString()]
            for dep in module.DESCRIPTOR.dependencies:
                dep_file = descriptor_pb2.FileDescriptorProto()
                dep_file.ParseFromString(dep.serialized_pb)
                fds.append(dep_file.SerializeToString())
            for service in module.DESCRIPTOR.services_by_name.values():
                twin = service.full_name.replace(old_package, new_package)
                if rename:twin=twin.rsplit('.',1)[0]+'.'+rename
                self._variant_fds[twin] = fds
        register_twin('hardware.v1.simple_stage_service','zen_api.lm.hardware.v2',rename='StageService')
        register_twin('lm.acquisition.v1.experiment_sw_autofocus_service',
                      'zen_api.lm.acquisition.v1beta')
        register_twin('lm.acquisition.v1.zstack_service',
                      'zen_api.lm.acquisition.v1beta')
        def reflection_info(requests, ctx):
            for req in requests:
                resp = reflection_pb2.ServerReflectionResponse(valid_host=req.host)
                resp.original_request.CopyFrom(req)
                if req.HasField('list_services'):
                    for service_name in self._service_names:
                        resp.list_services_response.service.add(name=service_name)
                elif req.HasField('file_containing_symbol') and \
                        req.file_containing_symbol in self._variant_fds:
                    resp.file_descriptor_response.file_descriptor_proto.extend(
                        self._variant_fds[req.file_containing_symbol])
                yield resp
        self.server.add_generic_rpc_handlers((
            grpc.method_handlers_generic_handler(
                'grpc.reflection.v1alpha.ServerReflection',
                {'ServerReflectionInfo': grpc.stream_stream_rpc_method_handler(
                    reflection_info,
                    request_deserializer=reflection_pb2.ServerReflectionRequest.FromString,
                    response_serializer=reflection_pb2.ServerReflectionResponse.SerializeToString)}),))
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
        if method == self.reject_method:
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "synthetic-private-detail-not-for-export")
        if self.reject_v1_af and service == 'zen_api.lm.acquisition.v1.ExperimentSwAutofocusService':
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "v1 af absent")
        if self.reject_beta_af and service == 'zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService':
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "beta af absent")
        if self.xy_missing and service.endswith((".SimpleStageService", ".StageService")):
            ctx.abort(grpc.StatusCode.UNIMPLEMENTED, "No XY installed")
        if service.endswith('.SimpleStageService') or service=='zen_api.lm.hardware.v2.StageService':
            if method=='GetPosition':return response(x=self.xy[0], y=self.xy[1])
            if method=='MoveTo':self.xy=(req.x.value,req.y.value);return response(position_changed=True)
        if method == 'GetStageMotionState':return response(state=3)
        if service.endswith('.FocusService'):
            v1 = '.v1.' in service  # v1 GetPosition/MoveTo use MICROMETRES; v2 uses metres.
            if self.no_focus and not v1:ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'no v2 focus')
            if method == 'GetPosition':return response(value=self.z*1e6 if v1 else self.z)
            if method == 'MoveTo':
                self.z=req.value/1e6 if v1 else req.value
                self.z_moves.append(self.z);return response(position_changed=True)
        if service.endswith(('.ObjectiveChangerService', '.OptovarService')) and method == 'GetPosition':
            if self.optics_missing:ctx.abort(grpc.StatusCode.UNIMPLEMENTED, 'Fixed optics')
            return response(value=1)
        if method == 'GetAvailableExperiments':
            return response(experiments=[{'name':'PLLOW扫描'}, {'name':'Upper'}])
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
        if method == 'FindSurface':
            # Definite Focus: hardware surface detect, Z in metres.
            self.z=98e-6
            return response(zposition=self.z)
        if method == 'IsZStackExperiment':
            return response(is_zstack_experiment=self.zstack_enabled)
        if method == 'GetZStackInfo':
            return response(interval=2e-6, first_slice=95e-6, last_slice=105e-6,
                            range=10e-6, num_slices=self.zstack_slices, is_center_mode=False, offset=0)
        if method == 'ModifyZStackFirstLast':
            self.zstack_writes[req.experiment_id] = (
                req.first.value, req.last.value, req.interval.value)
            if req.first.value == req.last.value:self.zstack_slices = 1
            return response()
        if method == 'ModifyZStackCenterRange':
            if req.HasField('range') and req.range.value == 0:self.zstack_slices = 1
            return response()
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
        with self.subTest('explicit v1 opt-in converts micrometres'):
            self.device.allow_focus_v1=True
            # v1 reports micrometres: the stage is at 200e-6 m from the move above.
            self.assertAlmostEqual(self.device.read_z(), 200e-6)
            self.device.move_z(250e-6)
            self.assertAlmostEqual(self.z, 250e-6)

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
        with self.subTest('v1beta service path when v1 unregistered'):
            # Reflection descriptor discovery needs headroom on busy CI hosts.
            self.device._timeout = 2
            from zen_hardware.capability import CapabilityMatrix
            self.device.capabilities=CapabilityMatrix()
            self.reject_bounds=False
            self.reject_v1_af=True
            result=self.prepare()
            self.assertEqual(result['lower_m'], 95e-6)
            self.assertTrue(any(service=='zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService'
                                for service,_ in self.calls))
            self.assertAlmostEqual(self.device.autofocus(1), 100e-6)
            self.reject_v1_af=False
        with self.subTest('definite focus fallback when software AF absent'):
            self.device.capabilities=CapabilityMatrix()
            self.reject_bounds=False
            self.reject_method='FindAutoFocus'
            self.device.allow_definite_focus=True
            self.assertAlmostEqual(self.device.autofocus(1), 98e-6)
            self.assertIn(('zen_api.lm.acquisition.v1.DefiniteFocusService','FindSurface'),self.calls)
            self.reject_method=None
            self.device.allow_definite_focus=False

    def test_single_snap_receives_matching_full_raw_frame(self):
        af_id=self.prepare()["experiment_id"]
        frame=self.device.capture_frame(2, Event(), 'unique-name')
        self.assertEqual(frame.xyz_m, (0, 0, 100e-6))
        self.assertEqual(frame.experiment_id, self.device._experiment_id)
        self.assertNotEqual(frame.experiment_id, af_id)
        np.testing.assert_array_equal(frame.array(), np.arange(12, dtype='<u2').reshape(3, 4))
        self.assertEqual(self.snaps, 1)
        self.assertEqual(self.stops, 0)
        with self.subTest('zstack-enabled clone is collapsed to one slice at current Z'):
            self.zstack_enabled=True
            frame=self.device.capture_frame(2, Event(), 'stacked')
            first, last, interval = self.zstack_writes[frame.experiment_id]
            self.assertAlmostEqual(first, last)
            self.assertAlmostEqual(first, self.z)
            self.assertEqual(self.zstack_slices, 1)
            self.assertEqual(self.snaps, 2)
            self.assertEqual(frame.z_source, 'frame')
            self.zstack_enabled=False

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

    def test_preparation_failure_identifies_exact_method_without_private_details(self):
        from zen_hardware.capability import CapabilityMatrix
        for method in ('Load', 'Clone', 'GetAutofocusParameters', 'SetAutofocusParameters'):
            with self.subTest(method=method):
                # A rejected method is cached as unimplemented; reset the matrix
                # so each subTest actually reaches its own target method.
                self.device.capabilities=CapabilityMatrix()
                self.reject_method=method
                if method == 'GetAutofocusParameters':
                    # No readback means no bounded AF without an explicit opt-in.
                    with self.assertRaises(MarkCapabilityError):self.prepare()
                    self.assertEqual(self.set_ids, [])
                    self.device.allow_unverified_af=True
                    result=self.prepare()
                    self.assertEqual(result['af_range'], 'written_unverified_readback')
                    self.device.allow_unverified_af=False
                    self.assertEqual(self.z_moves, [])
                    self.assertEqual(self.snaps, 0)
                    continue
                if method == 'SetAutofocusParameters':
                    with self.assertRaises(MarkCapabilityError) as caught:self.prepare()
                    self.assertIn('AF 参数写入接口不可用', str(caught.exception))
                    self.assertEqual(self.z_moves, [])
                    self.assertEqual(self.snaps, 0)
                    continue
                with self.assertRaises(GatewayCallError) as caught:self.prepare()
                error=caught.exception
                self.assertTrue(error.method.endswith('.'+method))
                self.assertEqual(error.code(), grpc.StatusCode.UNIMPLEMENTED)
                self.assertNotIn('synthetic-private-detail', json.dumps(error.to_dict()))
                self.assertEqual(self.z_moves, [])
                self.assertEqual(self.snaps, 0)
        with self.subTest('definite focus when software AF service unregistered'):
            self.reject_method=None
            self.reject_beta_af=True
            self.device.capabilities=CapabilityMatrix()
            self.device.capabilities.note_services((), ('zen_api.lm.acquisition.v1.ExperimentSwAutofocusService',))
            self.device.allow_definite_focus=True
            result=self.prepare()
            self.assertEqual(result['af_engine'], 'definite_focus')
            self.assertFalse(any(method=='FindSurface' for _,method in self.calls))
            self.device.allow_definite_focus=False
            self.reject_beta_af=False

    def test_read_only_inspection_does_not_load_clone_or_move(self):
        self.calls.clear()
        report=self.device.inspect_mark_experiments(('PLLOW扫描', 'missing'))
        self.assertEqual(report['requested_experiments'], [{'name':'PLLOW扫描','exists':True}, {'name':'missing','exists':False}])
        self.assertFalse(report['device_actions_performed'])
        self.assertEqual({method for service,method in self.calls}, {'GetAvailableExperiments', 'GetPosition'})
        self.assertEqual(self.z_moves, [])
        self.assertEqual(self.snaps, 0)

    def test_unsupported_read_only_listing_is_unknown_not_missing(self):
        self.reject_method='GetAvailableExperiments'
        report=self.device.inspect_mark_experiments(('PLLOW扫描',))
        self.assertIsNone(report['requested_experiments'][0]['exists'])
        self.assertEqual(report['errors'][0]['code'], 'UNIMPLEMENTED')
        self.assertTrue(report['errors'][0]['method'].endswith('.GetAvailableExperiments'))
        self.assertEqual(self.z_moves, [])

    def test_capture_failure_persists_rpc_method_and_status(self):
        from zen_workflow import Workflow
        from zen_fit import validate_recipe, fit_demo
        from test_zen_marks import MARKS
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            workflow=Workflow(self.device, base/'records', simulated=False, validate=validate_recipe, fit=fit_demo)
            def wait():
                deadline=time.monotonic()+4
                while workflow.busy and time.monotonic()<deadline:time.sleep(.01)
                self.assertFalse(workflow.busy)
            workflow.connect();wait()
            self.reject_method='Clone'
            workflow.start_mark_capture(MARKS, base/'images', confirmed=True, experiment_loading_confirmed=True);wait()
            self.assertEqual(workflow.state, '设备状态待确认')
            report=json.loads((workflow.mark_output_path/'capture.json').read_text(encoding='utf-8'))
            self.assertTrue(report['error']['method'].endswith('.Clone'))
            self.assertEqual(report['error']['code'], 'UNIMPLEMENTED')
            self.assertIn('ExperimentService.Clone', report['status']['failure_stage'])
            self.assertNotIn('synthetic-private-detail', json.dumps(report))
            self.assertEqual(self.z_moves, [])
            self.assertEqual(self.snaps, 0)

    def test_hardware_transport_connect_without_xy_or_z(self):
        self.xy_missing=self.no_focus=True
        self.calls.clear();self.device.connect_transport()
        self.assertEqual(self.calls,[])
        self.device.xy_enabled=False
        with self.assertRaises(MarkCapabilityError):self.device.call_hardware_operation('read_z',{})

    def test_hardware_z_move_is_only_move(self):
        self.calls.clear()
        self.device.call_hardware_operation('move_z',{'z_um':120})
        self.assertEqual([m for _,m in self.calls],['MoveTo'])
        self.assertAlmostEqual(self.z,120e-6)

    def test_hardware_af_does_not_clone_or_load(self):
        self.device.load_experiment('Upper')
        self.calls.clear()
        self.device.call_hardware_operation('af',{'experiment_id':'original:Upper','af_timeout_s':1})
        self.assertEqual([m for _,m in self.calls],['FindAutoFocus'])

        with self.subTest('ZStackService falls back to registered package variant'):
            # The v1 ZStackService is not registered on this mock (as on the
            # real ZEN 3.12); the v1beta twin must answer transparently.
            self.zstack_enabled=True
            self.assertTrue(self.device.call_hardware_operation(
                'is_zstack', {'experiment_id':'original:Upper'})['is_zstack_experiment'])
            self.zstack_enabled=False
            self.device.call_hardware_operation('set_zstack', {
                'experiment_id':'original:Upper','first_um':95,'last_um':105,'interval_um':2})
            first, last, interval = self.zstack_writes['original:Upper']
            for actual, expected in zip((first, last, interval), (95e-6, 105e-6, 2e-6)):
                self.assertAlmostEqual(actual, expected)
            self.assertTrue(any(service=='zen_api.lm.acquisition.v1beta.ZStackService'
                                for service,_ in self.calls))

    def test_legacy_312_xy_route_uses_metres_and_records_real_path(self):
        self.device._timeout=2
        self._service_names.remove('zen_api.hardware.v1.SimpleStageService')
        self.device.probe_services()
        self.device.move_xy(.002,.003)
        self.assertEqual(self.device.read_xy(),(.002,.003))
        self.assertIn(('zen_api.lm.hardware.v2.StageService','MoveTo'),self.calls)
        self.assertIn(('zen_api.lm.hardware.v2.StageService','GetPosition'),self.calls)

    def test_zstack_deadline_never_attempts_another_write(self):
        self.zstack_enabled=True
        original=self.device._versioned_rpc
        writes=[]
        def rpc(service,method,request,timeout,primary_call,template=None):
            if method.startswith('Modify'):
                writes.append(method)
                raise GatewayCallError(service+'.'+method,grpc.StatusCode.DEADLINE_EXCEEDED)
            return original(service,method,request,timeout,primary_call,template=template)
        self.device._versioned_rpc=rpc
        self.device.load_experiment('Upper')
        with self.assertRaises(grpc.RpcError):self.device._collapse_zstack(self.device._experiment_id)
        self.assertEqual(writes,['ModifyZStackFirstLast'])

    def test_version_fallback_never_crosses_lm_em_service_families(self):
        self.device.capabilities.services=('zen_api.em.hardware.v1.CameraService',
            'zen_api.lm.hardware.v1.CameraService','zen_api.lm.lls.acquisition.v1beta.ExperimentService')
        self.assertEqual(self.device._service_variants('zen_api.lm.hardware.v2.CameraService'),
                         ['zen_api.lm.hardware.v1.CameraService'])
        self.assertEqual(self.device._service_variants('zen_api.acquisition.v1beta.ExperimentService'),[])
