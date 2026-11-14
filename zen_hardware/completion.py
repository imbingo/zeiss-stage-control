"""Read-only completion evidence for successful hardware single-step commands."""

from math import isfinite, hypot
from time import monotonic
from threading import Event
from . import Motion


def verify_completion(device, operation, parameters, timeout_s, stop):
    deadline = monotonic() + timeout_s
    waiter = stop or Event()
    previous = None
    stable = 0
    observations = {}
    exp = parameters.get("experiment_id") or getattr(device, "_experiment_id", None)
    needs_status = operation in ("load_experiment", "af", "snap", "run_experiment")
    while True:
        if stop is not None and stop.is_set():
            raise InterruptedError("停止后完成状态尚未确认")
        if monotonic() >= deadline:
            raise TimeoutError("动作完成读回超时")
        # These are reads only. Never resend the original action.
        device._timeout = min(timeout_s, max(0.01, deadline - monotonic()))
        z = device.read_z()
        if not isfinite(z):
            raise ValueError("Z 读回无效")
        xy = None
        if getattr(device, "xy_enabled", True):
            state = device.motion_state()
            if state not in (Motion.IDLE, Motion.MOVING):
                raise ValueError("XY 状态未知")
            if state is not Motion.IDLE:
                stable = 0
                waiter.wait(0.02)
                continue
            xy = device.read_xy()
            if not all(isfinite(v) for v in xy):
                raise ValueError("XY 读回无效")
        idle = True
        if needs_status:
            if not exp:
                raise ValueError("缺少用于确认采集结束的实验 ID")
            reply = device.call_hardware_operation(
                "experiment_status", {"experiment_id": exp}
            )
            status = reply.get("status", reply)
            if (
                type(status.get("is_experiment_running")) is not bool
                or type(status.get("is_acquisition_running")) is not bool
            ):
                raise ValueError("实验状态未明确提供运行标志")
            idle = (
                not status["is_experiment_running"]
                and not status["is_acquisition_running"]
            )
            observations["experiment_status"] = status
        target_ok = True
        if operation == "move_z":
            target_ok = abs(z - parameters["z_um"] * 1e-6) <= 0.5e-6
        if operation == "move_xy":
            target_ok = (
                xy is not None
                and hypot(
                    xy[0] - parameters["x_um"] * 1e-6, xy[1] - parameters["y_um"] * 1e-6
                )
                <= 1e-6
            )
        unchanged = (
            previous is not None
            and abs(z - previous[1]) <= 0.5e-6
            and (
                xy is None
                or hypot(xy[0] - previous[0][0], xy[1] - previous[0][1]) <= 1e-6
            )
        )
        stable = stable + 1 if idle and target_ok and unchanged else 0
        previous = (xy, z)
        if stable >= 2:
            return {
                **observations,
                "verified": True,
                "source": "read_only_completion",
                "xy_m": xy,
                "z_m": z,
                "samples": 3,
                "note": "确认目标/稳定读回及所需实验空闲状态；不等于锁定转盘或图像有效性判定",
            }
        waiter.wait(0.02)
