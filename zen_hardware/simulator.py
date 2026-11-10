"""Deterministic, in-memory device for workflow verification."""

from time import sleep

from . import AutofocusTimeout, DeviceDisconnected, Motion, MoveFailed


class SimulatedDevice:
    def __init__(self, *, fault=None, fault_at=3, delay_seconds=0.1):
        self.fault = fault
        self.fault_at = fault_at
        self.delay_seconds = delay_seconds
        self.connected = False
        self.xy = (0.0, 0.0)
        self.experiment = None
        self.moves = []
        self.af_attempts = 0
        self.motion = Motion.IDLE
        self.af_pending = False

    def connect(self):
        self.connected = True

    def _require_connection(self):
        if not self.connected:
            raise DeviceDisconnected("模拟 Gateway 连接已中断")

    def read_xy(self):
        self._require_connection()
        return self.xy

    def motion_state(self):
        self._require_connection()
        return self.motion

    def move_xy(self, x, y):
        self._require_connection()
        self.moves.append((x, y))
        self.motion = Motion.MOVING
        sleep(self.delay_seconds)
        if self.fault == "move" and len(self.moves) == self.fault_at:
            self.motion = Motion.UNKNOWN
            raise MoveFailed("模拟 XY 定位失败")
        self.xy = (x, y)
        self.motion = Motion.IDLE

    def load_experiment(self, name):
        self._require_connection()
        if not name.strip():
            raise ValueError("实验名不能为空")
        self.experiment = name

    def autofocus(self, timeout_seconds):
        self._require_connection()
        if self.experiment is None:
            raise RuntimeError("请先加载实验")
        self.af_attempts += 1
        self.af_pending = True
        sleep(min(timeout_seconds, self.delay_seconds))
        if self.af_attempts == self.fault_at and self.fault == "disconnect":
            self.connected = False
            raise DeviceDisconnected("模拟 AF 期间 Gateway 断线")
        if timeout_seconds < self.delay_seconds or (
            self.af_attempts == self.fault_at and self.fault == "af_timeout"
        ):
            raise AutofocusTimeout("模拟 AF 超时，AF/Z 状态待确认")
        self.af_pending = False
        x, y = self.xy
        return 50e-6 + 0.002 * x - 0.001 * y

    def confirm_simulated_recovery(self):
        """Model the external intervention, independently of reconnect."""
        self.af_pending = False
        self.motion = Motion.IDLE
