"""Explicitly simulated two-plane capture; never connects to hardware."""

from dataclasses import replace
from .simulator import SimulatedDevice
from . import AutofocusTimeout
from zen_images import RawFrame


class SimulatedMarkDevice(SimulatedDevice):
    def __init__(self, *, fault=None, xy_enabled=True):
        super().__init__(delay_seconds=0)
        self.xy_enabled = xy_enabled
        self.capture_kinds = []
        self.mark_fault = fault
        self.z = 100e-6
        self.z_moves = []
        self.capture_count = 0
        self.prepared = []
        self.optics = (1, 1)
        self.bounds = (90e-6, 110e-6)
        self._experiment_id = None
        self.lab_calls = []
        self.lab_exposure = 10.0
        self.lab_stack = {"first_slice":100e-6,"last_slice":110e-6,"interval":1e-6,"num_slices":11}

    def read_xy(self):
        if not self.xy_enabled:
            raise AssertionError("无 XY 设备不应收到 XY 读取")
        return super().read_xy()

    def motion_state(self):
        if not self.xy_enabled:
            raise AssertionError("无 XY 设备不应收到 XY 状态查询")
        return super().motion_state()

    def move_xy(self, x, y):
        if not self.xy_enabled:
            raise AssertionError("无 XY 设备不应收到 XY 移动")
        return super().move_xy(x, y)

    def read_z(self):
        self._require_connection()
        return self.z

    def read_optics(self):
        self._require_connection()
        return self.optics

    def move_z(self, z):
        self._require_connection()
        self.z_moves.append(z)
        self.z = z

    def prepare_mark_experiment(self, name, low, high, *, use_autofocus=True):
        self.load_experiment(name)
        self.bounds = low, high
        self.prepared.append((name, low, high))
        if self.mark_fault == 'optics':
            self.optics = (2, 1)
        return {"lower_m": low, "upper_m": high, "experiment_id": f"demo-{len(self.prepared)}"}

    def autofocus(self, timeout_seconds):
        self._require_connection()
        self.af_attempts += 1
        if self.mark_fault == 'af_timeout':
            raise AutofocusTimeout("模拟局部 AF 超时")
        self.z = sum(self.bounds) / 2
        if self.mark_fault == 'wrong_plane':
            self.z = self.bounds[1] + 50e-6
        return self.z

    def capture_frame(self, timeout_s, stop, output_name, *, kind="camera_snap"):
        import numpy as np
        self._require_connection()
        if stop.is_set():
            raise InterruptedError("已请求停止拍摄")
        self.capture_kinds.append(kind)
        self.capture_count += 1
        if self.mark_fault == 'lower_capture' and self.capture_count == 2:
            raise TimeoutError("模拟下层拍摄超时")
        if self.mark_fault == 'xy':
            self.xy = (self.xy[0] + 10e-6, self.xy[1])
        y, x = np.indices((96, 128))
        offset = 2 if self.capture_count == 2 else 0
        image = np.where((abs(x - 64-offset) < 3) | (abs(y-48) < 3), 50000, 1000).astype('<u2')
        frame = RawFrame(128, 96, 2, image.tobytes(), .2e-6, .2e-6,
                         (*(self.xy if self.xy_enabled else (None, None)), self.z),
                         f"demo-{self.capture_count}", self.capture_count,
                         xy_verified=self.xy_enabled, capture_kind=kind)
        if self.mark_fault == 'scale' and self.capture_count == 2:
            frame = replace(frame, scale_x_m=.4e-6)
        return frame

    def inspect_mark_experiments(self, names):
        return {"kind": "read_only_experiment_check", "device_actions_performed": False,
                "available_experiments": ["Demo upper", "Demo lower"],
                "requested_experiments": [{"name": name, "exists": name in ("Demo upper", "Demo lower")} for name in names if name],
                "z_m": self.read_z(), "optics_positions": self.read_optics(), "rpc_trace": [],
                "limitations": ["离线模拟报告不证明现场接口可用，不检查或锁定真实转盘"]}

    def connect_transport(self):
        self.connected = True
        self._experiment_id = None

    def close(self):
        self.connected = False
        self._experiment_id = None

    def call_hardware_operation(self, operation, p):
        self._require_connection()
        self.lab_calls.append(operation)
        if operation == 'read_z':return {'z_m':self.read_z()}
        if operation == 'move_z':self.move_z(p['z_um']*1e-6);return {'target_z_m':self.z,'motion_completion_verified':False}
        if operation == 'stop_z':return {'simulated':True}
        if operation == 'read_xy':return {'xy_m':self.read_xy()}
        if operation == 'read_xy_motion':return {'state':self.motion_state().value}
        if operation == 'move_xy':self.move_xy(p['x_um']*1e-6,p['y_um']*1e-6);return {'motion_completion_verified':False}
        if operation == 'read_optics':return {'positions':self.read_optics(),'turret_locked':False}
        if operation == 'list_experiments':return {'experiments':[{'name':'Demo upper'},{'name':'Demo lower'}]}
        if operation == 'list_services':return {'services':['demo.FocusService','demo.ExperimentService','simulated-only']}
        if operation == 'load_experiment':
            self.load_experiment(p['experiment_name']);self._experiment_id='demo-loaded'
            return {'experiment_id':self._experiment_id}
        if operation == 'clone_experiment':self._experiment_id='demo-clone';return {'experiment_id':self._experiment_id}
        if operation == 'export_experiment':return {'xml_characters':0,'note':'模拟 XML 摘要'}
        if operation == 'get_af':return {'lower_limit':self.bounds[0],'upper_limit':self.bounds[1],'units':'m'}
        if operation == 'set_af_range':self.bounds=(p['low_um']*1e-6,p['high_um']*1e-6);return {}
        if operation == 'af':return {'focus_position':self.autofocus(p['af_timeout_s']),'units':'m'}
        if operation == 'is_zstack':return {'is_zstack_experiment':True}
        if operation == 'get_zstack':return dict(self.lab_stack)
        if operation == 'set_zstack':
            self.lab_stack={'first_slice':p['first_um']*1e-6,'last_slice':p['last_um']*1e-6,'interval':p['interval_um']*1e-6}
            return {}
        if operation in ('snap','run_experiment'):
            self.capture_count+=1;return {'output_name':p['output_name'] or 'simulated-image','simulated':True}
        if operation == 'stop_experiment':return {}
        if operation == 'experiment_status':return {'is_experiment_running':False,'images_count':self.capture_count}
        if operation == 'image_output_path':return {'image_output_path':'SIMULATED-NOT-A-REAL-FOLDER'}
        if operation == 'list_cameras':return {'cameras':[{'id':'demo-camera','name':'模拟相机'}]}
        if operation == 'get_camera':return {'id':p['camera_id'],'exposure_time':self.lab_exposure,'min_exposure_time':1,'max_exposure_time':100,'exposure_unit':'API raw'}
        if operation == 'set_exposure':
            if not 1<=p['exposure_value']<=100:raise ValueError('模拟相机曝光范围为 1–100（原始单位）')
            self.lab_exposure=p['exposure_value'];return {}
        raise ValueError('未知模拟硬件操作')


class SimulatedSequenceDevice(SimulatedMarkDevice):
    """Combined tilt-plane and paired-frame demo; never accesses a microscope."""
    def autofocus(self, timeout_seconds):
        if self.experiment == 'Demo AF':
            self._require_connection()
            self.af_attempts += 1
            self.z = 100e-6 + .002*self.xy[0] - .001*self.xy[1]
            return self.z
        return super().autofocus(timeout_seconds)
