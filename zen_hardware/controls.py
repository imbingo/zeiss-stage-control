"""Individually callable hardware operations; no Tk or measurement workflow."""

import hashlib
from google.protobuf.json_format import MessageToDict


def as_dict(reply):
    return MessageToDict(reply, preserving_proto_field_name=True, always_print_fields_with_no_presence=True)


class GatewayControlsMixin:
    def _control_rpc(self, package, service, method, request, timeout_s=None, **values):
        from importlib import import_module
        module = import_module('zen_api.'+package+'_pb2')
        stub = self._mark_stub('zen_api.'+package+'_pb2_grpc', service+'Stub')
        request_type = getattr(module, request)
        return self._rpc('zen_api.'+package.rsplit('.', 1)[0]+'.'+service+'.'+method,
                         getattr(stub, method), request_type(**values),
                         timeout=self._timeout if timeout_s is None else timeout_s)

    def call_hardware_operation(self, operation, p):
        """Arguments already validated by HardwareLibrary; each branch is explicit."""
        from google.protobuf.wrappers_pb2 import DoubleValue, BoolValue
        experiment = 'acquisition.v1beta.experiment_service'
        if operation == 'read_z':return {'z_m':self.read_z()}
        if operation == 'move_z':
            self.move_z(p['z_um']*1e-6)
            return {'target_z_m':p['z_um']*1e-6, 'motion_completion_verified':False}
        if operation == 'stop_z':
            return as_dict(self._control_rpc('lm.hardware.v2.focus_service','FocusService','Stop','FocusServiceStopRequest'))
        if operation == 'read_xy':
            x,y=self.read_xy();return {'x_m':x,'y_m':y}
        if operation == 'read_xy_motion':return {'state':self.motion_state().value}
        if operation == 'move_xy':
            self.move_xy(p['x_um']*1e-6,p['y_um']*1e-6)
            return {'target_xy_m':[p['x_um']*1e-6,p['y_um']*1e-6], 'motion_completion_verified':False}
        if operation == 'read_optics':
            value=self.read_optics();return {'objective_position':value[0], 'optovar_position':value[1],
                                            'positions_verified':[v is not None for v in value], 'turret_locked':False}
        if operation == 'list_experiments':
            return as_dict(self._control_rpc(experiment,'ExperimentService','GetAvailableExperiments','ExperimentServiceGetAvailableExperimentsRequest'))
        if operation == 'load_experiment':
            self.load_experiment(p['experiment_name']);return {'experiment_id':self._experiment_id}
        if operation == 'clone_experiment':
            reply=self._control_rpc(experiment,'ExperimentService','Clone','ExperimentServiceCloneRequest',experiment_id=p['experiment_id'])
            if not reply.experiment_id or reply.experiment_id==p['experiment_id']:raise ValueError('Clone 未返回独立实验 ID，拒绝在原实验上继续操作')
            self._experiment_id=reply.experiment_id
            return as_dict(reply)
        if operation == 'export_experiment':
            reply=self._control_rpc(experiment,'ExperimentService','Export','ExperimentServiceExportRequest',experiment_id=p['experiment_id'])
            return {'xml_characters':len(reply.xml), 'xml_sha256':hashlib.sha256(reply.xml.encode('utf-8')).hexdigest(),
                    'note':'XML 内容不进入共享日志；此测试仅验证读取接口'}
        if operation in ('snap','run_experiment','stop_experiment','experiment_status','image_output_path'):
            method,request={'snap':('RunSnap','ExperimentServiceRunSnapRequest'),
                            'run_experiment':('RunExperiment','ExperimentServiceRunExperimentRequest'),
                            'stop_experiment':('Stop','ExperimentServiceStopRequest'),
                            'experiment_status':('GetStatus','ExperimentServiceGetStatusRequest'),
                            'image_output_path':('GetImageOutputPath','ExperimentServiceGetImageOutputPathRequest')}[operation]
            values={} if operation=='image_output_path' else {'experiment_id':p['experiment_id']}
            if operation in ('snap','run_experiment'):values['output_name']=p['output_name']
            return as_dict(self._control_rpc(experiment,'ExperimentService',method,request,**values))
        if operation in ('get_af','set_af_range','af'):
            package='lm.acquisition.v1.experiment_sw_autofocus_service'
            module = __import__('importlib').import_module('zen_api.'+package+'_pb2')
            if operation=='af':
                request=getattr(module,'ExperimentSwAutofocusServiceFindAutoFocusRequest')(
                    experiment_id=p['experiment_id'], timeout=DoubleValue(value=p['af_timeout_s']))
                return as_dict(self._af_rpc('FindAutoFocus', request, p['af_timeout_s']+self._timeout))
            reply=self._af_rpc('GetAutofocusParameters',
                getattr(module,'ExperimentSwAutofocusServiceGetAutofocusParametersRequest')(
                    experiment_id=p['experiment_id']), self._timeout)
            if operation=='get_af':return as_dict(reply)
            if reply.auto_focus_mode not in (1,2,3):raise ValueError('只支持软件 AF 的范围设置，不修改反射式 AF')
            return as_dict(self._af_rpc('SetAutofocusParameters',
                getattr(module,'ExperimentSwAutofocusServiceSetAutofocusParametersRequest')(
                    experiment_id=p['experiment_id'],
                    autofocus_mode=reply.auto_focus_mode, contrast_measure=reply.contrast_measure,
                    autofocus_sampling=reply.autofocus_sampling, search_strategy='Full',
                    relative_range_is_automatic=BoolValue(value=False),
                    relative_search_range=DoubleValue(value=(p['high_um']-p['low_um'])*1e-6),
                    lower_limit=DoubleValue(value=p['low_um']*1e-6),upper_limit=DoubleValue(value=p['high_um']*1e-6)),
                self._timeout))
        if operation in ('get_zstack','is_zstack','set_zstack'):
            method,request={'get_zstack':('GetZStackInfo','ZStackServiceGetZStackInfoRequest'),
                            'is_zstack':('IsZStackExperiment','ZStackServiceIsZStackExperimentRequest'),
                            'set_zstack':('ModifyZStackFirstLast','ZStackServiceModifyZStackFirstLastRequest')}[operation]
            values={'experiment_id':p['experiment_id']}
            if operation=='set_zstack':
                values.update(first=DoubleValue(value=p['first_um']*1e-6),last=DoubleValue(value=p['last_um']*1e-6),
                              interval=DoubleValue(value=p['interval_um']*1e-6))
            return as_dict(self._control_rpc('lm.acquisition.v1.zstack_service','ZStackService',method,request,**values))
        if operation in ('list_cameras','get_camera','set_exposure'):
            package='lm.hardware.v2.camera_service'
            if operation=='list_cameras':
                return as_dict(self._control_rpc(package,'CameraService','GetCameras','CameraServiceGetCamerasRequest'))
            camera=self._control_rpc(package,'CameraService','GetCamera','CameraServiceGetCameraRequest',id=p['camera_id']).camera
            if operation=='get_camera':return as_dict(camera)
            from zen_api.lm.hardware.v2.set_camera_data_pb2 import SetCameraData
            if camera.id!=p['camera_id'] or camera.binning<=0 or not camera.HasField('frame'):
                raise ValueError('相机返回的 ID/binning/ROI 不完整，拒绝覆盖其他相机设置')
            if not 0<=camera.min_exposure_time<camera.max_exposure_time or not camera.min_exposure_time<=p['exposure_value']<=camera.max_exposure_time:
                raise ValueError('曝光值超出相机当前返回的范围，或范围未提供')
            data=SetCameraData(id=camera.id,exposure_time=p['exposure_value'],binning=camera.binning)
            data.frame.CopyFrom(camera.frame)
            return as_dict(self._control_rpc(package,'CameraService','ConfigureCamera','CameraServiceConfigureCameraRequest',data=data))
        raise ValueError('未知硬件操作')
