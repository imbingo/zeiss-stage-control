"""Simulate 3.12 documented service paths using existing wire fixture.

This is a protocol-routing test, not ZEN software or microscope execution.
Latest fixture message fields were separately checked against the archived
3.12 generated classes by audit_contracts.py. No credentials or hardware.
"""
import argparse
import json
from threading import Event
from unittest.mock import patch
import grpc
from test_zen_mark_gateway import MarkGatewayTest
from zen_hardware.gateway import GatewayDevice

parser=argparse.ArgumentParser()
parser.add_argument('--contracts',required=True)
parser.add_argument('--output',required=True)
args=parser.parse_args()
contracts=json.load(open(args.contracts))
allowed={row['versions']['ZEN3.12_20250509']['service'] for row in contracts['methods'] if row['versions']['ZEN3.12_20250509']['documented']}
allowed.add('grpc.reflection.v1alpha.ServerReflection')
class OldSurface(grpc.ServerInterceptor):
    def intercept_service(self,continuation,details):
        service=details.method.split('/')[1]
        if service in allowed:return continuation(details)
        def absent(request,context):context.abort(grpc.StatusCode.UNIMPLEMENTED,'not in archived 3.12 surface')
        return grpc.unary_unary_rpc_method_handler(absent)
original_server=grpc.server
original_connect=GatewayDevice.connect
def test_server(*a,**kw):return original_server(*a,interceptors=(OldSurface(),),**kw)
def no_xy_connect(device):
    device.xy_enabled=False
    device._timeout=2
    return original_connect(device)
fixture=MarkGatewayTest('test_focus_v2_uses_metres_and_does_not_use_v1')
checks=[]
try:
    with patch('grpc.server',test_server),patch.object(GatewayDevice,'connect',no_xy_connect):fixture.setUp()
    device=fixture.device
    fixture._service_names[:]=[s for s in fixture._service_names if s in allowed]
    device.probe_services()
    device.move_z(100e-6)
    assert abs(device.read_z()-100e-6)<1e-12
    checks.append({'check':'Focus v2 read/move in metres','pass':True})
    device.prepare_mark_experiment('Upper',95e-6,105e-6)
    z=device.autofocus(2)
    assert abs(z-100e-6)<1e-12
    assert device._last_af_path=='zen_api.lm.acquisition.v1beta.ExperimentSwAutofocusService'
    checks.append({'check':'AF Get/Set/readback/Find via v1beta','pass':True})
    fixture.zstack_enabled=True
    frame=device.capture_frame(3,Event(),'audit_upper',kind='confocal')
    assert frame.array().shape==(3,4)
    assert any(service=='zen_api.lm.acquisition.v1beta.ZStackService' for service,method in fixture.calls)
    checks.append({'check':'ZStack v1beta collapse + confocal complete frame','pass':True})
    device.prepare_mark_experiment('Upper',95e-6,105e-6,use_autofocus=False)
    frame=device.capture_frame(3,Event(),'audit_snap',kind='camera_snap')
    assert frame.array().shape==(3,4)
    checks.append({'check':'No-AF camera SNAP complete frame','pass':True})
    assert device.read_optics()==(None,None)
    try:device.call_hardware_operation('list_cameras',{})
    except grpc.RpcError as error:assert error.code()==grpc.StatusCode.UNIMPLEMENTED
    else:raise AssertionError('camera enumeration must not succeed on this surface')
    checks.append({'check':'Unavailable optics/camera remain unavailable','pass':True})
    device.close();device.xy_enabled=True
    try:original_connect(device)
    except grpc.RpcError as error:assert error.code()==grpc.StatusCode.UNIMPLEMENTED
    else:raise AssertionError('Current motorized route unexpectedly accepted old-only surface')
    checks.append({'check':'Confirmed current motorized connect lacks old XY path','pass':True,'expected_incompatibility':True})
    result={'kind':'local_protocol_mock_only','application_commit':contracts['application_commit'],'checks':checks,
            'limitations':['Not ZEN execution; mock only.','Message fixture fields checked statically against official 3.12 archive.','Does not test hardware, authentication, licensing, AF quality or ZStack acceptance.'],
            'calls':[{'service':s,'method':m} for s,m in fixture.calls]}
    with open(args.output,'w') as f:json.dump(result,f,indent=2)
    print(json.dumps(checks,indent=2))
finally:fixture.doCleanups()
