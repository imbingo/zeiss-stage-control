"""Explicitly simulated two-plane capture; never connects to hardware."""

from dataclasses import replace
from .simulator import SimulatedDevice
from . import AutofocusTimeout
from zen_images import RawFrame


class SimulatedMarkDevice(SimulatedDevice):
    def __init__(self, *, fault=None):
        super().__init__(delay_seconds=0)
        self.mark_fault = fault
        self.z = 100e-6
        self.z_moves = []
        self.capture_count = 0
        self.prepared = []
        self.optics = (1, 1)
        self.bounds = (90e-6, 110e-6)

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

    def prepare_mark_experiment(self, name, low, high):
        self.load_experiment(name)
        self.bounds = low, high
        self.prepared.append((name, low, high))
        if self.mark_fault == 'optics':
            self.optics = (2, 1)
        return {"lower_m": low, "upper_m": high, "experiment_id": f"demo-{len(self.prepared)}"}

    def autofocus(self, timeout_seconds):
        self.af_attempts += 1
        if self.mark_fault == 'af_timeout':
            raise AutofocusTimeout("模拟局部 AF 超时")
        self.z = sum(self.bounds) / 2
        if self.mark_fault == 'wrong_plane':
            self.z = self.bounds[1] + 50e-6
        return self.z

    def capture_frame(self, timeout_s, stop, output_name):
        import numpy as np
        if stop.is_set():
            raise InterruptedError("已请求停止拍摄")
        self.capture_count += 1
        if self.mark_fault == 'lower_capture' and self.capture_count == 2:
            raise TimeoutError("模拟下层拍摄超时")
        if self.mark_fault == 'xy':
            self.xy = (self.xy[0] + 10e-6, self.xy[1])
        y, x = np.indices((96, 128))
        offset = 2 if self.capture_count == 2 else 0
        image = np.where((abs(x - 64-offset) < 3) | (abs(y-48) < 3), 50000, 1000).astype('<u2')
        frame = RawFrame(128, 96, 2, image.tobytes(), .2e-6, .2e-6,
                         (*self.xy, self.z), f"demo-{self.capture_count}", self.capture_count)
        if self.mark_fault == 'scale' and self.capture_count == 2:
            frame = replace(frame, scale_x_m=.4e-6)
        return frame
