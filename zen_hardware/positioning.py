"""Read-only XY checks shared by real and simulated devices."""

from math import hypot, isfinite
from threading import Event
from time import monotonic

from . import Device, Motion, MoveFailed


class PositionCheckInterrupted(RuntimeError):
    """The caller requested that positioning checks stop."""


def check_xy(device: Device, target: tuple[float, float], tolerance_m: float):
    """Read XY and report whether the radial error is within tolerance."""
    if not isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError("XY 容差必须为有限正数")
    if not all(map(isfinite, target)):
        raise ValueError("目标 XY 必须为有限坐标")
    x, y = device.read_xy()
    if not (isfinite(x) and isfinite(y)):
        raise MoveFailed("载物台返回无效 XY")
    return (x, y), hypot(x - target[0], y - target[1]) <= tolerance_m


def wait_for_arrival(
    device: Device,
    target: tuple[float, float],
    tolerance_m: float,
    timeout_seconds: float,
    stop: Event,
    *,
    on_read=None,
) -> tuple[float, float]:
    """Verify XY idle and position after a move, without issuing commands."""
    if not isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("到位超时必须为有限正数")
    deadline = monotonic() + timeout_seconds
    while True:
        if stop.is_set():
            raise PositionCheckInterrupted("已请求停止后续采集，设备状态需独立确认")
        state = device.motion_state()
        if state not in (Motion.IDLE, Motion.MOVING):
            raise MoveFailed("XY 运动状态无法确认")
        if state is Motion.IDLE:
            xy, within_tolerance = check_xy(device, target, tolerance_m)
            if on_read is not None:
                on_read(xy)
            if stop.is_set():
                raise PositionCheckInterrupted("已请求停止后续采集，设备状态需独立确认")
            if within_tolerance:
                # A fresh in-tolerance read is a valid confirmation even if the
                # deadline passed between polls; the timeout only bounds the wait.
                return xy
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise MoveFailed("XY 到位确认超时")
        stop.wait(min(remaining, 0.05))
