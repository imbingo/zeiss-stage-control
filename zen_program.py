"""Ordered step execution. All device writes are owned by Workflow's gate."""

from copy import deepcopy
from math import isfinite, hypot
from pathlib import Path
from time import monotonic
import json
from zen_records import (
    RunRecords,
    write_json,
    utc_now,
    canonical_sha256,
    write_manifest,
    software_version,
)
from zen_images import write_tiff
from zen_steps import numeric, capability_issues
from zen_hardware import Motion
from zen_hardware.positioning import wait_for_arrival
from zen_acquisition import PointRecord


class ProcessRejected(ValueError):
    pass


class ProgramDevice:
    def __init__(self, workflow, common, command_log, persist=lambda: None):
        self.w = workflow
        self.d = workflow.device
        self.c = common
        self.log = command_log
        self.persist = persist
        self.optics = self.d.read_optics()

    def checkpoint(self):
        if self.w._stop.is_set():
            raise InterruptedError("已停止后续步骤；同步设备调用可能尚未结束")

    def position(self):
        self.checkpoint()
        z = self.d.read_z()
        if not isfinite(z) or not self.c["z_min_um"] <= z * 1e6 <= self.c["z_max_um"]:
            raise ValueError("Z 读回无效或越界")
        if self.d.read_optics() != self.optics:
            raise ValueError("物镜/倍率读回变化")
        xy = None
        if self.w.xy_enabled:
            if self.d.motion_state() is not Motion.IDLE:
                raise ValueError("XY 未空闲")
            xy = self.d.read_xy()
            if not all(isfinite(v) for v in xy):
                raise ValueError("XY 读回无效")
        return xy, z

    def target(self, z):
        if not isfinite(z) or not self.c["z_min_um"] <= z * 1e6 <= self.c["z_max_um"]:
            raise ValueError("目标 Z 超出允许范围")

    def same(self, before, *, z=True):
        after = self.position()
        if (
            before[0] is not None
            and hypot(after[0][0] - before[0][0], after[0][1] - before[0][1])
            > self.c["xy_tolerance_um"] * 1e-6
        ):
            raise ValueError("XY 意外变化")
        if z and abs(before[1] - after[1]) > self.c["z_tolerance_um"] * 1e-6:
            raise ValueError("Z 意外变化")
        return after

    def action(self, name, args, fn, settings=False, motion=False):
        before = self.position()
        self.checkpoint()
        entry = {
            "at": utc_now(),
            "command": name,
            "arguments": deepcopy(args),
            "state": "issued",
        }
        self.log.append(entry)
        self.persist()
        try:
            result = fn()
            after = (
                before if motion else self.same(before) if settings else self.position()
            )
            entry.update(
                state="accepted" if motion else "confirmed",
                readback={"xy_m": after[0], "z_m": after[1]},
            )
            return result
        except Exception as e:
            entry.update(state="failed", error=str(e))
            raise

    def move_z(self, z, timeout=15):
        self.target(z)
        before = self.position()
        self.action("move_z", {"z_m": z}, lambda: self.d.move_z(z), motion=True)
        end = monotonic() + timeout
        stable = 0
        while True:
            xy, actual = self.same(before, z=False)
            stable = (
                stable + 1 if abs(actual - z) <= self.c["z_tolerance_um"] * 1e-6 else 0
            )
            if stable >= 3:
                break
            if monotonic() >= end:
                raise TimeoutError("Z 到位超时")
            self.w._stop.wait(0.02)
        if self.w._stop.wait(self.c["settle_s"]):
            self.checkpoint()
        _, actual = self.same((xy, z))
        self.log[-1].update(state="confirmed", readback={"xy_m": xy, "z_m": actual})
        return actual * 1e6

    def move_xy(self, x, y, safe, timeout=15):
        if not self.w.xy_enabled:
            raise ValueError("无电动 XY")
        if not all(isfinite(v) for v in (x, y)):
            raise ValueError("目标 XY 无效")
        self.move_z(safe, timeout)
        self.action(
            "move_xy",
            {"x_m": x, "y_m": y, "safe_z_m": safe},
            lambda: self.d.move_xy(x, y),
            motion=True,
        )
        wait_for_arrival(
            self.d, (x, y), self.c["xy_tolerance_um"] * 1e-6, timeout, self.w._stop
        )
        xy, z = self.position()
        if abs(z - safe) > self.c["z_tolerance_um"] * 1e-6:
            raise ValueError("XY 转场期间偏离安全 Z")
        self.log[-1].update(state="confirmed", readback={"xy_m": xy, "z_m": z})
        return xy

    def load(self, name):
        def load():
            self.d.call_hardware_operation("load_experiment", {"experiment_name": name})
            if not self.w.simulated:
                digest = self.d.call_hardware_operation(
                    "export_experiment", {"experiment_id": self.d._experiment_id}
                ).get("xml_sha256")
                if not digest:
                    raise ValueError("无法读取实验内容指纹")
            else:
                digest = canonical_sha256({"simulated_experiment": name})
            self.d.call_hardware_operation(
                "clone_experiment", {"experiment_id": self.d._experiment_id}
            )
            return digest

        return self.action(
            "load_and_clone_experiment", {"name": name}, load, settings=True
        )

    def autofocus(self, name, center, half, timeout):
        before = self.position()
        requested_center = center
        center = before[1] * 1e6 if center is None else center
        low = (center - half) * 1e-6
        high = (center + half) * 1e-6
        self.target(low)
        self.target(high)
        if requested_center is not None:
            self.move_z(center * 1e-6)
        before = self.position()
        result = self.action(
            "prepare_af",
            {"experiment": name, "low_m": low, "high_m": high},
            lambda: self.d.prepare_loaded_autofocus(low, high),
            settings=True,
        )
        if (
            not isinstance(result, dict)
            or abs(result.get("lower_m", float("inf")) - low)
            > self.c["z_tolerance_um"] * 1e-6
            or abs(result.get("upper_m", float("inf")) - high)
            > self.c["z_tolerance_um"] * 1e-6
        ):
            raise ValueError("AF 范围未有效读回")
        value = self.action(
            "autofocus", {"timeout_s": timeout}, lambda: self.d.autofocus(timeout)
        )
        _, actual = self.same(before, z=False)
        if (
            not low <= value <= high
            or abs(actual - value) > self.c["z_tolerance_um"] * 1e-6
        ):
            raise ValueError("AF 结果越界或与 Z 不一致")
        return actual * 1e6


def execute_program(w, plan, output, session, preflight):
    parent = None
    device = None
    active = None
    issued = False
    result = None
    report = {
        "kind": "sequence",
        "model_version": "2",
        "state": "running",
        "simulated": w.simulated,
        "plan": plan.to_dict(),
        "recipe_identity": plan.recipe_identity,
        "steps": [],
        "images": [],
        "captures": [],
        "measurements": [],
        "experiment_fingerprints": {},
        "disposition": "PENDING",
    }
    if hasattr(w.device, "clear_rpc_trace"):
        w.device.clear_rpc_trace()
    w.sequence_report = report
    w.sequence_run_path = None
    w.result = None
    w.result_path = None
    commands = []
    outputs = {}
    experiment = None
    expected = plan.recipe_identity.get("experiment_fingerprints", {})
    pair_frames = {}

    def persist():
        if hasattr(w.device, "rpc_trace"):
            report["rpc_trace"] = w.device.rpc_trace()
        if parent:
            write_json(parent.path / "sequence.json", report)
            write_json(parent.path / "command_log.json", {"commands": commands})
        w.sequence_report = report

    def event():
        persist()
        w.events.put(("program_progress", deepcopy(report)))

    def checkpoint():
        if w._stop.is_set():
            raise InterruptedError("停止后续步骤，不自动返回或重试")

    def resolve(value, point):
        if isinstance(value, str) and value.startswith("@"):
            sid, key = value[1:].split(".")
            if sid == "point":
                return numeric(point.get(key), "当前点引用", allow_ref=False)
            if sid not in outputs or key not in outputs[sid]:
                raise ValueError("缺少本次运行的前置结果 " + value)
            return outputs[sid][key]
        return value

    def fingerprint(name):
        nonlocal issued, experiment
        issued = True
        digest = device.load(name)
        if (
            plan.recipe_identity.get("execution_mode") == "production"
            and expected.get(name) != digest
        ):
            raise ValueError("实验内容与放行版本不同：" + name)
        previous_digest = report["experiment_fingerprints"].get(name)
        if previous_digest is not None and previous_digest != digest:
            raise ValueError("同名实验在本次任务中已被修改，请重新验证：" + name)
        report["experiment_fingerprints"][name] = digest
        experiment = name

    def run(nodes, point=None, context=""):
        nonlocal active, issued, result
        for node in nodes:
            checkpoint()
            sid = node["step_id"]
            kind = node["kind"]
            from zen_steps import CATALOG

            reference_fields = {
                k
                for k, _, typ, _ in CATALOG[kind][2]
                if typ in ("number", "positive", "optional")
            }
            p = {
                k: resolve(v, point or {}) if k in reference_fields else v
                for k, v in node["params"].items()
            }
            for key, label, typ, _ in CATALOG[kind][2]:
                if typ in ("number", "positive", "optional") and p.get(key) not in (
                    "",
                    None,
                ):
                    p[key] = numeric(
                        p[key], label, positive=typ == "positive", allow_ref=False
                    )
            path = context + sid
            active = {
                "step_id": sid,
                "path": path,
                "title": node["label"],
                "kind": kind,
                "state": "running",
                "started_at": utc_now(),
            }
            report["steps"].append(active)
            event()
            w._state("自动任务", detail=node["label"])
            out = {}
            if kind == "wait":
                if w._stop.wait(float(p["seconds"])):
                    checkpoint()
            elif kind == "load_experiment":
                fingerprint(p["experiment"])
                out = {"experiment": experiment}
            elif kind == "move_z":
                target = float(p["z_um"]) + (
                    device.position()[1] * 1e6 if p.get("relative") else 0
                )
                issued = True
                out = {"z_um": device.move_z(target * 1e-6, float(p["timeout_s"]))}
            elif kind == "move_xy":
                safe = float(
                    p.get("safe_z_um")
                    if p.get("safe_z_um") not in ("", None)
                    else plan.common["safe_z_um"]
                )
                issued = True
                device.move_xy(
                    float(p["x_um"]) * 1e-6,
                    float(p["y_um"]) * 1e-6,
                    safe * 1e-6,
                    float(p["timeout_s"]),
                )
            elif kind == "autofocus":
                if not experiment:
                    raise ValueError("请先加载实验")
                issued = True
                out = {
                    "z_um": device.autofocus(
                        experiment,
                        (
                            None
                            if p.get("center_um") in ("", None)
                            else float(p["center_um"])
                        ),
                        float(p["half_um"]),
                        float(p["timeout_s"]),
                    )
                }
            elif kind == "measure_angle":
                fingerprint(p["experiment"])
                rows = []

                w.events.put(
                    ("measurement_begin", {"step_id": sid, "points_um": p["points_um"]})
                )
                for index, pointrow in enumerate(p["points_um"], 1):
                    checkpoint()
                    safe = float(plan.common["safe_z_um"]) * 1e-6
                    before_xy = device.move_xy(
                        float(pointrow[0]) * 1e-6, float(pointrow[1]) * 1e-6, safe
                    )
                    z = (
                        float(pointrow[2])
                        if len(pointrow) > 2 and pointrow[2] != ""
                        else float(p["z_um"])
                    )
                    z_um = device.autofocus(
                        experiment, z, float(p["half_um"]), float(p["timeout_s"])
                    )
                    xy, actual = device.position()
                    row = PointRecord(
                        index,
                        (float(pointrow[0]) * 1e-6, float(pointrow[1]) * 1e-6),
                        before_xy,
                        xy,
                        actual,
                        "ok",
                    )
                    rows.append(row)
                    parent.append(row)
                    item = {
                        "step_id": sid,
                        "path": path,
                        "index": index,
                        "x_um": xy[0] * 1e6,
                        "y_um": xy[1] * 1e6,
                        "z_um": z_um,
                    }
                    report["measurements"].append(item)
                    w.events.put(("measurement_point", item))
                    event()
                result = w.fit(tuple(rows))
                out = {
                    "rx_urad": result["rx_urad"],
                    "ry_urad": result["ry_urad"],
                    "rms_um": result["rms_m"] * 1e6,
                }
                report["result"] = result
                report.setdefault("angle_results", {})[path] = deepcopy(result)
                write_json(parent.path / "angle_result.json", result)
                device.move_z(float(plan.common["safe_z_um"]) * 1e-6)
            elif kind == "angle_gate":
                source = outputs.get(p["source"])
                if not source:
                    raise ValueError("缺少本次测角结果")
                if (
                    abs(source["rx_urad"]) > float(p["max_rx_urad"])
                    or abs(source["ry_urad"]) > float(p["max_ry_urad"])
                    or source["rms_um"] > float(p["max_rms_um"])
                ):
                    raise ProcessRejected("角度结果不合格，停止后续步骤")
            elif kind in ("capture_upper", "capture_lower"):
                if not experiment:
                    raise ValueError("请先加载实验")
                layer = "upper" if kind == "capture_upper" else "lower"
                before = device.position()
                issued = True
                frame = device.action(
                    "capture_frame",
                    {"layer": layer, "kind": p["capture_kind"]},
                    lambda: w.device.capture_frame(
                        float(p["timeout_s"]),
                        w._stop,
                        "lsm_" + parent.path.name + "_" + path.replace("/", "_"),
                        kind=p["capture_kind"],
                    ),
                )
                device.same(before)
                frame.array()
                if (
                    abs(frame.xyz_m[2] - before[1])
                    > plan.common["z_tolerance_um"] * 1e-6
                ):
                    raise ValueError("图像 Z 与已确认位置不符")
                if w.xy_enabled and (
                    not frame.xy_verified
                    or hypot(
                        frame.xyz_m[0] - before[0][0], frame.xyz_m[1] - before[0][1]
                    )
                    > plan.common["xy_tolerance_um"] * 1e-6
                ):
                    raise ValueError("图像 XY 与已确认位置不符")
                key = (context, p["pair_id"])
                upper = pair_frames.get(key)
                if layer == "lower" and upper:
                    if not frame.matches_geometry(upper):
                        raise ValueError("上下 mark 图像尺寸或标定不一致")
                    if (
                        w.xy_enabled
                        and hypot(
                            frame.xyz_m[0] - upper.xyz_m[0],
                            frame.xyz_m[1] - upper.xyz_m[1],
                        )
                        > plan.common["xy_tolerance_um"] * 1e-6
                    ):
                        raise ValueError("配对上下 mark 不在同一 XY")
                filename = path.replace("/", "_") + "_" + layer + ".tif"
                file = image_root / filename
                write_tiff(
                    file,
                    frame,
                    {
                        "run_id": parent.path.name,
                        "step_id": sid,
                        "layer": layer,
                        "pair_id": p["pair_id"],
                    },
                )
                if layer == "upper":
                    pair_frames[key] = frame
                item = {
                    "step_id": sid,
                    "context": context,
                    "pair_id": p["pair_id"],
                    "layer": layer,
                    "path": str(file),
                    "z_um": before[1] * 1e6,
                    "experiment": experiment,
                    "at": utc_now(),
                }
                report["images"].append(item)
                w.events.put(("mark_image", deepcopy(item)))
                out = {"path": str(file)}
            elif kind == "zstack":
                if not experiment:
                    raise ValueError("请先加载实验")
                first, last, interval = (
                    float(p["first_um"]),
                    float(p["last_um"]),
                    float(p["interval_um"]),
                )
                device.target(first * 1e-6)
                device.target(last * 1e-6)
                before = device.position()
                issued = True
                frames = device.action(
                    "capture_zstack",
                    p,
                    lambda: w.device.capture_stack(
                        first * 1e-6,
                        last * 1e-6,
                        interval * 1e-6,
                        float(p["timeout_s"]),
                        w._stop,
                        str(p["output_name"])
                        + "_"
                        + parent.path.name
                        + "_"
                        + path.replace("/", "_"),
                    ),
                )
                device.same(before, z=False)
                expected_count = round(abs(last - first) / interval) + 1
                if len(frames) != expected_count:
                    raise ValueError("Z-stack 层数不完整")
                folder = image_root / (path.replace("/", "_") + "_stack")
                folder.mkdir()
                geometry = None
                metadata = []
                for index, frame in enumerate(frames):
                    frame.array()
                    target = (
                        first + (1 if last > first else -1) * interval * index
                    ) * 1e-6
                    if (
                        abs(frame.xyz_m[2] - target)
                        > plan.common["z_tolerance_um"] * 1e-6
                    ):
                        raise ValueError("Z-stack 层位置不一致")
                    if geometry and not frame.matches_geometry(geometry):
                        raise ValueError("Z-stack 层图像几何不一致")
                    if w.xy_enabled and (
                        not frame.xy_verified
                        or hypot(
                            frame.xyz_m[0] - before[0][0], frame.xyz_m[1] - before[0][1]
                        )
                        > plan.common["xy_tolerance_um"] * 1e-6
                    ):
                        raise ValueError("Z-stack 图像 XY 不一致")
                    geometry = frame
                    file = folder / f"z_{index:04d}.tif"
                    write_tiff(
                        file, frame, {"run_id": parent.path.name, "z_index": index}
                    )
                    metadata.append(
                        {"index": index, "z_m": frame.xyz_m[2], "path": file.name}
                    )
                write_json(
                    folder / "stack.json",
                    {"axes": "ZYX", "planes": metadata, "complete": True},
                )
                out = {"planes": len(frames), "path": str(folder)}
                report.setdefault("stacks", []).append(
                    {"step_id": sid, "path": str(folder), "planes": len(frames)}
                )
            elif kind == "point_loop":
                for index, row in enumerate(p["points_um"], 1):
                    checkpoint()
                    pt = {"x_um": float(row[0]), "y_um": float(row[1])}
                    if len(row) > 2 and row[2] != "":
                        pt["z_um"] = float(row[2])
                    run(node.get("children", []), pt, path + f"[{index}]/")
                active = next(s for s in reversed(report["steps"]) if s["path"] == path)
            else:
                raise ValueError("不支持的可执行步骤：" + kind)
            if device:
                xy, z = device.position()
                active["readback"] = {"xy_m": xy, "z_m": z}
            outputs[sid] = out
            active.update(state="complete", finished_at=utc_now(), output=deepcopy(out))
            event()

    try:
        parent = RunRecords(
            w.root,
            name=plan.name,
            experiment="工程步骤",
            targets=(),
            tolerance_m=plan.common.get("xy_tolerance_um", 1) * 1e-6,
            af_timeout=30,
            arrival_timeout=15,
            sign_convention="",
            simulated=w.simulated,
            confirmed_start=(preflight or {}).get("xy"),
        )
        w.sequence_run_path = parent.path
        image_root = (
            Path(output)
            / ("demo_program_" if w.simulated else "program_")
            / parent.path.name
        )
        image_root.mkdir(parents=True)
        report["output_directory"] = str(image_root)
        snapshot = {
            "kind": "program_snapshot",
            "plan": plan.to_dict(),
            "session": session,
            "preflight": preflight,
            "software_version": software_version(),
            "frozen_at": utc_now(),
        }
        snapshot["snapshot_sha256"] = canonical_sha256(snapshot)
        write_json(parent.path / "snapshot.json", snapshot)
        write_json(
            parent.path / "session.json",
            {**session, "snapshot_sha256": snapshot["snapshot_sha256"]},
        )
        write_json(
            parent.path / "recipe.json",
            {**plan.to_dict(), "kind": "sequence", "recipe_name": plan.name},
        )
        event()
        if all(n["kind"] == "wait" for n in plan.steps):
            device = None
        else:
            device = ProgramDevice(w, plan.common, commands, persist)
            xy, z = device.position()
            if preflight.get("device") is not None and preflight["device"] != id(
                w.device
            ):
                raise ValueError("检查后设备连接已变化")
            if (
                preflight.get("z_m") is not None
                and abs(z - preflight["z_m"]) > plan.common["z_tolerance_um"] * 1e-6
            ):
                raise ValueError("检查后 Z 已变化，请重新检查")
            if (
                xy is not None
                and preflight.get("xy") is not None
                and hypot(xy[0] - preflight["xy"][0], xy[1] - preflight["xy"][1])
                > plan.common["xy_tolerance_um"] * 1e-6
            ):
                raise ValueError("检查后 XY 已变化，请重新检查")
            if (
                preflight.get("optics") is not None
                and tuple(preflight["optics"]) != device.optics
            ):
                raise ValueError("检查后物镜/倍率读回变化")
        run(plan.steps)
        checkpoint()
        manifest = write_manifest(
            parent.path,
            run_id=parent.path.name,
            files=[f for f in image_root.rglob("*") if f.is_file()],
            base=image_root,
        )
        report.update(
            state="complete",
            disposition="AWAITING_EXTERNAL_REVIEW",
            recovery_required=False,
            finished_at=utc_now(),
            manifest_sha256=manifest["manifest_sha256"],
        )
        persist()
        write_json(image_root / "sequence.json", report)
        parent.finish("complete", recovery_required=False, result=result)
        w.result = result
        w.result_path = parent.path
        event()
        w.events.put(("result", result, parent.path))
        w._state("完成", detail="步骤流程完成，未自动添加返回运动")
    except Exception as error:
        recovery = issued and not isinstance(error, ProcessRejected)
        state = "stopped" if isinstance(error, InterruptedError) else "failed"
        if active and active["state"] == "running":
            active.update(state=state, error=str(error))
        report.update(
            state=state,
            recovery_required=recovery,
            error={"code": type(error).__name__, "message": str(error)},
            disposition=(
                "MEASUREMENT_REJECTED"
                if isinstance(error, ProcessRejected)
                else "EQUIPMENT_FAULT"
            ),
        )
        try:
            persist()
            if parent:
                parent.finish(state, recovery_required=recovery, stage=str(error))
            if "image_root" in locals():
                write_json(image_root / "sequence.json", report)
        except OSError:
            report["recovery_required"] = True
            recovery = True
        w.sequence_report = report
        w.events.put(("program_progress", deepcopy(report)))
        w._state("设备状态待确认" if recovery else "已中止", detail=str(error))
    finally:
        w._save_capabilities()
