"""Validate additional request types against pinned official protobuf descriptors."""
import importlib
import unittest
from zen_hardware.controls import GatewayControlsMixin


class Controls(GatewayControlsMixin):
    _timeout=1
    def __init__(self):self.calls=[];self.camera=None
    def _mark_stub(self,module,service):
        owner=self
        class Stub:
            def __getattr__(self,method):
                def call(request,**kwargs):
                    owner.calls.append((method,request))
                    response_class=getattr(importlib.import_module(module.replace('_pb2_grpc','_pb2')),type(request).__name__.replace('Request','Response'))
                    return response_class(camera=owner.camera) if method=='GetCamera' else response_class()
                return call
        return Stub()
    def _rpc(self,method,call,request,**kwargs):return call(request,**kwargs)


class ControlsTests(unittest.TestCase):
    def test_zstack_metres_no_acquisition(self):
        d=Controls();d.call_hardware_operation('set_zstack',{'experiment_id':'e','first_um':100,'last_um':110,'interval_um':1})
        self.assertEqual([m for m,_ in d.calls],['ModifyZStackFirstLast'])
        r=d.calls[0][1];self.assertAlmostEqual(r.first.value,100e-6);self.assertAlmostEqual(r.interval.value,1e-6)

    def test_exposure_preserves_camera_roi_binning(self):
        from zen_api.lm.hardware.v2.get_camera_data_pb2 import GetCameraData
        d=Controls();d.camera=GetCameraData(id='c',binning=2,min_exposure_time=1,max_exposure_time=100,exposure_time=5)
        d.camera.frame.size.width=100;d.camera.frame.size.height=80
        d.call_hardware_operation('set_exposure',{'camera_id':'c','exposure_value':12})
        self.assertEqual([m for m,_ in d.calls],['GetCamera','ConfigureCamera'])
        data=d.calls[-1][1].data
        self.assertEqual(data.binning,2);self.assertEqual(data.frame,d.camera.frame);self.assertEqual(data.exposure_time,12)

    def test_exposure_rejects_missing_roi(self):
        from zen_api.lm.hardware.v2.get_camera_data_pb2 import GetCameraData
        d=Controls();d.camera=GetCameraData(id='c',binning=2,min_exposure_time=1,max_exposure_time=100)
        with self.assertRaises(ValueError):d.call_hardware_operation('set_exposure',{'camera_id':'c','exposure_value':12})
        self.assertEqual(len(d.calls),1)

    def test_capture_deadline_matches_user_input_without_hidden_floor(self):
        class DeadlineControls(Controls):
            def _rpc(self, method, call, request, **kwargs):
                self.deadline = kwargs['timeout']
                return call(request, **kwargs)
        for op in ('snap','run_experiment'):
            device = DeadlineControls()
            device._timeout = 60.0
            device.call_hardware_operation(op, {'experiment_id':'e','output_name':'unique'})
            self.assertEqual(device.deadline, 60.0)
