"""The single write-command gateway for stage motion and experiment loading.

Raw adapters (GatewayDevice, simulators) execute commands but do not enforce
safety. Every flow that moves the stage, loads an experiment or exposes a frame
must go through MotionInterlock, which owns:

  * safe Z before any XY transfer,
  * command + readback confirmation for each move,
  * optics / motion-state / Z-range guards,
  * an append-only command log that is copied into the run record.

``enforces_safe_z`` is the marker other layers use to tell whether a device
passed through this gate. New executors must obtain their device from
``Workflow.motion_device(...)`` instead of calling a raw adapter directly.
"""
from datetime import datetime, timezone
from math import hypot, isfinite
from time import monotonic

from . import Motion
from .positioning import wait_for_arrival


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe(value):
    """Keep the command log JSON-serialisable and free of device internals."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if value is None or type(value) in (int, float, str, bool):
        return value
    return repr(value)[:200]


class SequenceStopped(InterruptedError):
    """The operator requested a stop; no further command may be issued."""


def enforces_safe_z(device):
    """True only when a device has passed through MotionInterlock."""
    return getattr(device, "enforces_safe_z", False) is True


class MotionInterlock:
    """Safe-Z transfers and command/readback pairs around a raw device adapter.

    The configured Z limits check commands and readbacks; they are not a
    hardware travel limit and cannot constrain ZEN's own autofocus motion.
    """

    enforces_safe_z = True

    def __init__(self, device, *, safe_z_m, z_min_m, z_max_m, z_tolerance_m,
                 xy_tolerance_m, arrival_timeout_s, settle_s, stop,
                 tilt_z_m=None):
        self.device = device
        self.safe_z_m = float(safe_z_m)
        self.z_min_m = float(z_min_m)
        self.z_max_m = float(z_max_m)
        self.z_tolerance_m = float(z_tolerance_m)
        self.xy_tolerance_m = float(xy_tolerance_m)
        self.arrival_timeout_s = float(arrival_timeout_s)
        self.settle_s = float(settle_s)
        self.tilt_z_m = None if tilt_z_m is None else float(tilt_z_m)
        self.stop = stop
        self.angle_mode = False
        self.optics = device.read_optics()
        self.command_log = []

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "device"), name)

    # ---- command log -------------------------------------------------
    def _command(self, kind, arguments):
        entry = {"seq": len(self.command_log) + 1, "at": _now(),
                 "command": kind, "arguments": _safe(arguments),
                 "outcome": "issued", "readback": {}}
        self.command_log.append(entry)
        return entry

    def _confirmed(self, entry, **readback):
        entry["outcome"] = "confirmed"
        entry["readback"] = _safe(readback)
        entry["confirmed_at"] = _now()

    def _aborted(self, entry, error):
        entry["outcome"] = "aborted"
        entry["error"] = type(error).__name__
        entry["confirmed_at"] = _now()

    def command_log_snapshot(self):
        """Copy for the run record; never hand out the live list."""
        return [dict(entry) for entry in self.command_log]

    # ---- guards ------------------------------------------------------
    def checkpoint(self):
        if self.stop.is_set():
            raise SequenceStopped('已停止后续任务；不自动移动或返回')

    def guard(self):
        self.checkpoint()
        if self.device.read_optics() != self.optics:
            raise ValueError('物镜/倍率读回变化，已停止 Sequence')
        z = self.device.read_z()
        if not isfinite(z) or not self.z_min_m <= z <= self.z_max_m:
            raise ValueError('实际 Z 超出任务核对范围，已停止后续动作')
        if self.device.motion_state() is not Motion.IDLE:
            raise ValueError('XY 未空闲，已停止后续动作')
        xy=self.device.read_xy()
        if len(xy)!=2 or not all(isfinite(v) for v in xy):
            raise ValueError('XY 位置回读无效，未发送后续运动')
        return xy, z

    def same_xy(self, expected):
        xy = self.device.read_xy()
        if (not all(isfinite(v) for v in (*xy,*expected))
                or hypot(xy[0] - expected[0], xy[1] - expected[1]) > self.xy_tolerance_m):
            raise ValueError('XY 与已确认位置不一致，已停止 Sequence')

    # ---- the only write paths ----------------------------------------
    def move_z(self, z):
        xy, _ = self.guard()
        if not isfinite(z) or not self.z_min_m <= z <= self.z_max_m:
            raise ValueError('目标 Z 超出任务范围')
        entry = self._command('move_z', {'z_m': z})
        try:
            self.checkpoint()
            self.device.move_z(z)
            deadline = monotonic() + self.arrival_timeout_s
            stable = 0
            while True:
                self.checkpoint()
                self.same_xy(xy)
                if (self.device.motion_state() is not Motion.IDLE
                        or self.device.read_optics() != self.optics):
                    raise ValueError('Z 转场期间 XY 或光学状态变化')
                actual = self.device.read_z()
                stable = stable + 1 if isfinite(actual) and abs(actual - z) <= self.z_tolerance_m else 0
                if stable >= 3:
                    break
                if monotonic() >= deadline:
                    raise TimeoutError('Sequence Z 到位确认超时')
                self.stop.wait(.02)
            if self.stop.wait(self.settle_s):
                self.checkpoint()
            xy_after, z_after = self.guard()
            self.same_xy(xy)
            if abs(z_after - z) > self.z_tolerance_m:
                raise ValueError('Z 稳定等待后偏离目标')
        except Exception as error:
            self._aborted(entry, error)
            raise
        self._confirmed(entry, z_m=z_after, xy_m=list(xy_after))
        return z_after

    def move_xy(self, x, y):
        if not all(isfinite(v) for v in (x,y)):raise ValueError('目标 XY 必须为有限坐标')
        # The safe-Z transfer is unconditional: no caller can reach the raw
        # MoveTo without going through this method.
        self.move_z(self.safe_z_m)
        entry = self._command('move_xy', {'x_m': x, 'y_m': y})
        try:
            self.checkpoint()
            self.device.move_xy(x, y)
            actual_xy=wait_for_arrival(self.device, (x, y), self.xy_tolerance_m,
                             self.arrival_timeout_s, self.stop)
            _, z = self.guard()
            if abs(z - self.safe_z_m) > self.z_tolerance_m:
                raise ValueError('XY 转场期间 Z 偏离安全位置')
        except Exception as error:
            self._aborted(entry, error)
            raise
        self._confirmed(entry, x_m=actual_xy[0], y_m=actual_xy[1], safe_z_m=z)
        return x, y

    def _settings(self, action, name, arguments):
        xy, z = self.guard()
        entry = self._command(name, arguments)
        try:
            result = action()
            _, after = self.guard()
            self.same_xy(xy)
            if abs(after - z) > self.z_tolerance_m:
                raise ValueError('实验加载/准备改变了 Z，停止后续任务，请检查实验设置')
        except Exception as error:
            self._aborted(entry, error)
            raise
        self._confirmed(entry, z_m=after)
        return result

    def load_experiment(self, name):
        return self._settings(lambda: self.device.load_experiment(name),
                              'load_experiment', {'experiment': name})

    def prepare_mark_experiment(self, *a, **kw):
        return self._settings(lambda: self.device.prepare_mark_experiment(*a, **kw),
                              'prepare_mark_experiment',
                              {'args': [str(v)[:80] for v in a], 'kwargs': sorted(kw)})

    def autofocus(self, timeout):
        if self.angle_mode and self.tilt_z_m is not None:
            self.move_z(self.tilt_z_m)
        xy, _ = self.guard()
        entry = self._command('autofocus', {'timeout_s': timeout})
        try:
            self.checkpoint()
            value = self.device.autofocus(timeout)
            _, z = self.guard()
            self.same_xy(xy)
            if not isfinite(value) or abs(z - value) > self.z_tolerance_m:
                raise ValueError('AF 返回焦面与 Z 回读不一致')
        except Exception as error:
            self._aborted(entry, error)
            raise
        self._confirmed(entry, focus_z_m=value, readback_z_m=z)
        return value

    def capture_frame(self, *a, **kw):
        self.checkpoint()
        entry = self._command('capture_frame',
                              {'args': [str(v)[:80] for v in a], 'kwargs': sorted(kw)})
        try:
            frame = self.device.capture_frame(*a, **kw)
        except Exception as error:
            self._aborted(entry, error)
            raise
        self._confirmed(entry, frame_received=True)
        return frame
