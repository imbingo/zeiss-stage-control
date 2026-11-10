"""Sequential acquisition shared by interactive and automated callers."""

from dataclasses import dataclass, replace
from math import isfinite
from threading import Event

from zen_hardware import Device, Motion, MoveFailed
from zen_hardware.positioning import (
    PositionCheckInterrupted,
    check_xy,
    wait_for_arrival,
)


@dataclass(frozen=True)
class PointRecord:
    index: int
    target: tuple[float, float]
    before_af: tuple[float, float] | None = None
    after_af: tuple[float, float] | None = None
    z: float | None = None
    status: str = "pending"
    failure_stage: str = ""


class AcquisitionFailed(RuntimeError):
    def __init__(self, stage, index, *, stopped=False, recovery_required=True):
        super().__init__(stage, index)
        self.stage = stage
        self.index = index
        self.stopped = stopped
        self.recovery_required = recovery_required


def acquire(
    device: Device,
    targets: tuple[tuple[float, float], ...],
    experiment: str,
    tolerance_m: float,
    af_timeout: float,
    arrival_timeout: float,
    stop: Event,
    persist,
    progress,
    *,
    move=True,
    confirmed_start=None,
):
    """Acquire validated targets. Persist each attempted row before advancing."""
    def checkpoint():
        if stop.is_set():
            raise PositionCheckInterrupted("已请求停止后续采集")

    stage = "启动检查"
    try:
        checkpoint()
        if device.motion_state() is not Motion.IDLE:
            raise MoveFailed("XY 未处于空闲状态")
        stage = "加载实验"
        progress(stage, 0)
        checkpoint()
        device.load_experiment(experiment)
        if confirmed_start is not None:
            stage = "路径起点复核"
            progress(stage, 0)
            checkpoint()
            _, matches = check_xy(device, confirmed_start, tolerance_m)
            if not matches or device.motion_state() is not Motion.IDLE:
                raise MoveFailed("当前位置与确认的路径起点不一致")
    except Exception as exc:
        raise AcquisitionFailed(
            stage, 0, stopped=isinstance(exc, PositionCheckInterrupted),
            recovery_required=not isinstance(exc, PositionCheckInterrupted),
        ) from exc

    for index, target in enumerate(targets, 1):
        if stop.is_set():
            raise AcquisitionFailed("点间停止", index, stopped=True, recovery_required=False)
        row = PointRecord(index, target)
        def save_readback(xy):
            nonlocal row
            row = replace(row, before_af=xy)

        stage = "定位" if move else "当前位置检查"
        try:
            progress(stage, index)
            checkpoint()
            if move:
                if device.motion_state() is not Motion.IDLE:
                    raise MoveFailed("XY 未空闲，已阻止下一点定位")
                checkpoint()
                device.move_xy(*target)
            stage = "到位确认"
            progress(stage, index)
            wait_for_arrival(device, target, tolerance_m, arrival_timeout, stop,
                             on_read=save_readback)
            stage = "软件 AF"
            progress(stage, index)
            checkpoint()
            z = device.autofocus(af_timeout)
            row = replace(row, z=z if isfinite(z) else None)
            if row.z is None:
                raise ValueError("AF 返回无效 Z")
            stage = "AF 后 XY 校验"
            progress(stage, index)
            xy, within_tolerance = check_xy(device, target, tolerance_m)
            row = replace(row, after_af=xy)
            if not within_tolerance or device.motion_state() is not Motion.IDLE:
                raise MoveFailed("AF 后 XY 未通过到位校验")
        except Exception as exc:
            stopped = isinstance(exc, PositionCheckInterrupted)
            persist(replace(row, status="stopped" if stopped else "failed", failure_stage=stage))
            raise AcquisitionFailed(stage, index, stopped=stopped) from exc
        persist(replace(row, status="ok"))
        if stop.is_set():
            raise AcquisitionFailed(stage, index, stopped=True, recovery_required=False)
