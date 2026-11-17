"""Multi-surface real-height acquisition. No material-thickness subtraction."""

import csv
import os
from hashlib import sha256
from math import hypot
from pathlib import Path
from time import monotonic
from zen_records import write_json, utc_now
from zen_scan_path import (
    checked_prediction,
    prediction_order,
    predict_z,
    validate_prescan,
)

AF_DEFAULTS = dict(prescan_half_um=30, prescan_timeout_s=60, half_um=10, timeout_s=30)


def default_surfaces():
    return [dict(experiment="", z_um="", override=False) for _ in range(3)]


def face_parameters(p, face):
    return {
        key: face[key] if face.get("override") and key in face else p.get(key, value)
        for key, value in AF_DEFAULTS.items()
    }


def validate_heights(p, common):
    from zen_steps import numeric

    if type(p.get("surface_count")) is not int or p["surface_count"] not in (2, 3):
        raise ValueError("测量面数必须为两面或三面")
    if p.get("xy_mode") not in ("motorized", "current"):
        raise ValueError("请选择 XY 路径或固定当前位置")
    faces = p.get("surfaces")
    if not isinstance(faces, list) or len(faces) != 3:
        raise ValueError("面配置须保存三个位置槽，按面数启用前两个或三个")
    low, high = float(common["z_min_um"]), float(common["z_max_um"])
    for index, face in enumerate(faces[: p["surface_count"]]):
        if not isinstance(face, dict) or not str(face.get("experiment", "")).strip():
            raise ValueError(f"Z{index+1} 面须填写 ZEN 实验名称")
        face["experiment"] = str(face["experiment"]).strip()
        face["z_um"] = numeric(face.get("z_um"), f"Z{index+1} 初始 Z", allow_ref=False)
        if type(face.get("override", False)) is not bool:
            raise ValueError("独立 AF 参数必须为开关")
        af = face_parameters(p, face)
        for key, value in af.items():
            af[key] = numeric(value, key, positive=True, allow_ref=False)
        if face.get("override"):
            face.update(af)
        half = af["prescan_half_um"] if p["xy_mode"] == "motorized" else af["half_um"]
        if not low <= face["z_um"] - half < face["z_um"] + half <= high:
            raise ValueError(f"Z{index+1} 面的完整初始 AF 搜索区间超出允许 Z 范围")
    if p["xy_mode"] == "motorized":
        validate_prescan(p.get("points_um", []), p.get("prescan_indices"))
    numeric(
        p.get("max_prediction_residual_um"),
        "预测残差上限",
        positive=True,
        allow_ref=False,
    )
    for key in AF_DEFAULTS:
        p[key] = numeric(p[key], key, positive=True, allow_ref=False)
    numeric(common.get("safe_z_um"), "安全转场 Z", allow_ref=False)
    if not low <= float(common["safe_z_um"]) <= high:
        raise ValueError("安全转场 Z 超出允许范围")


def height_experiments(p):
    return {
        str(f["experiment"]).strip()
        for f in p.get("surfaces", [])[: p.get("surface_count", 3)]
    }


def csv_fields(count):
    return (
        ["point_no", "X_um", "Y_um"]
        + [f"Z{i}_um" for i in range(1, count + 1)]
        + ["dZ21_um"]
        + (["dZ32_um"] if count == 3 else [])
        + ["status"]
    )


def save_height_scan(scan):
    """Both destinations are incremental; a failed write prevents more actions."""
    fields = csv_fields(scan["surface_count"])
    for folder in (scan["record_directory"], scan["output_directory"]):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "heights.json", scan)
        target = folder / "heights.csv"
        temporary = target.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(scan["rows"])
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)


def execute_heights(
    w, device, p, common, path, report, record_root, output_root, fingerprint, event
):
    count = p["surface_count"]
    faces = p["surfaces"][:count]
    motorized = p["xy_mode"] == "motorized"
    points = p["points_um"] if motorized else [None]
    pre, order = prediction_order(len(points), p.get("prescan_indices", []), motorized)
    key = sha256(path.encode("utf-8")).hexdigest()[:20]
    scan = dict(
        schema_version=1,
        unit="um",
        kind="height_scan",
        surface_count=count,
        step_path=path,
        state="running",
        data_complete=False,
        return_complete=False,
        simulated=w.simulated,
        xy_verified=bool(w.xy_enabled),
        rows=[],
        models={},
        face_samples=[[] for _ in faces],
        record_directory=str(Path(record_root) / "height_scans" / key),
        output_directory=str(Path(output_root) / ("heights_" + key)),
    )
    report.setdefault("height_scans", {})[path] = scan
    row = None

    def publish():
        save_height_scan(scan)
        event()

    def check_xy(anchor):
        xy, z = device.position()
        if anchor is not None and (
            xy is None
            or hypot(xy[0] - anchor[0], xy[1] - anchor[1])
            > common["xy_tolerance_um"] * 1e-6
        ):
            raise ValueError("同点各面累计 XY 偏移超限")
        return xy, z

    def build_models():
        remaining = [points[i][:2] for i in order[4:]]
        for face_index, face in enumerate(faces):
            af = face_parameters(p, face)
            scan["models"][str(face_index + 1)] = checked_prediction(
                scan["face_samples"][face_index],
                remaining,
                float(af["half_um"]),
                float(p["max_prediction_residual_um"]),
                device.target,
            )
        publish()

    try:
        publish()
        for sequence_index, index in enumerate(order, 1):
            device.checkpoint()
            if motorized and sequence_index == 5:
                w._state(
                    "自动任务", detail="建立各面独立预测平面，检查所有剩余 AF 区间"
                )
                build_models()
            phase = (
                "prescan"
                if motorized and index in pre
                else "fine" if motorized else "single"
            )
            row = {name: None for name in csv_fields(count)}
            row.update(
                point_no=index + 1,
                execution_index=sequence_index,
                phase=phase,
                status="running",
                faces=[],
            )
            scan["rows"].append(row)
            publish()
            safe = float(common["safe_z_um"]) * 1e-6
            if motorized:
                device.move_xy(
                    float(points[index][0]) * 1e-6, float(points[index][1]) * 1e-6, safe
                )
            anchor, _ = device.position()
            row.update(
                X_um=anchor[0] * 1e6 if anchor else None,
                Y_um=anchor[1] * 1e6 if anchor else None,
            )
            for face_index, face in enumerate(faces):
                device.checkpoint()
                af = face_parameters(p, face)
                half = float(
                    af["prescan_half_um"] if phase == "prescan" else af["half_um"]
                )
                timeout = float(
                    af["prescan_timeout_s"] if phase == "prescan" else af["timeout_s"]
                )
                z = (
                    float(face["z_um"])
                    if phase != "fine"
                    else predict_z(
                        scan["models"][str(face_index + 1)], row["X_um"], row["Y_um"]
                    )
                )
                # Recheck against actual arrival XY before loading/positioning this face.
                device.target((z - half) * 1e-6)
                device.target((z + half) * 1e-6)
                check_xy(anchor)
                w._state(
                    "自动任务",
                    detail=f"多面高度 · {'预扫描' if phase=='prescan' else '精扫描' if phase=='fine' else '单点'} {sequence_index}/{len(order)} · Z{face_index+1}",
                )
                digest = fingerprint(face["experiment"])
                check_xy(anchor)
                sample = dict(
                    surface=face_index + 1,
                    experiment=face["experiment"],
                    experiment_sha256=digest,
                    loaded_experiment_id=getattr(w.device, "_experiment_id", None),
                    search_center_um=z,
                    half_um=half,
                    timeout_s=timeout,
                    state="running",
                    started_at=utc_now(),
                )
                row["faces"].append(sample)
                publish()
                started = monotonic()
                try:
                    device.autofocus(face["experiment"], z, half, timeout)
                finally:
                    # AF preparation may clone the loaded experiment again.
                    sample["experiment_id"] = getattr(w.device, "_experiment_id", None)
                    sample["elapsed_s"] = monotonic() - started
                actual_xy, actual_z = check_xy(anchor)
                value = actual_z * 1e6
                row[f"Z{face_index+1}_um"] = value
                sample.update(
                    state="complete",
                    actual_xyz_m=[
                        *(actual_xy if actual_xy else (None, None)),
                        actual_z,
                    ],
                    actual_z_um=value,
                    elapsed_s=monotonic() - started,
                    prediction_error_um=value - z if phase == "fine" else None,
                    finished_at=utc_now(),
                )
                if face_index >= 1:
                    row["dZ21_um" if face_index == 1 else "dZ32_um"] = (
                        value - row[f"Z{face_index}_um"]
                    )
                if phase == "prescan":
                    scan["face_samples"][face_index].append(
                        [actual_xy[0] * 1e6, actual_xy[1] * 1e6, value]
                    )
                publish()
                if face_index >= 1 and value <= row[f"Z{face_index}_um"]:
                    row["status"] = "invalid_order"
                    raise ValueError(
                        "实测面顺序异常：要求 Z1 < Z2 < Z3；保留有符号差值"
                    )
            row["status"] = "complete"
            publish()
        if motorized and not scan["models"]:
            build_models()
        scan["data_complete"] = True
        scan["state"] = "returning"
        publish()
        w._state("自动任务", detail="多面高度数据已采齐，返回安全 Z 并核验到位")
        device.move_z(float(common["safe_z_um"]) * 1e-6)
        scan.update(state="complete", return_complete=True, finished_at=utc_now())
        publish()
        return {"height_scan_path": path, "points": len(points), "surfaces": count}
    except Exception as error:
        if row and row["status"] == "running":
            row["status"] = (
                "stopped" if isinstance(error, InterruptedError) else "partial"
            )
        if row and row["faces"] and row["faces"][-1]["state"] == "running":
            row["faces"][-1].update(state="failed", error=str(error))
        scan.update(
            state="stopped" if isinstance(error, InterruptedError) else "failed",
            error=str(error),
            finished_at=utc_now(),
        )
        try:
            publish()
        except OSError:
            pass
        raise


def validate_height_trial(body, report):
    """Complete, same-revision field evidence only; every loop occurrence is required."""
    from collections import Counter
    from zen_steps import flatten

    nodes = {
        n["step_id"]: n
        for _, n in flatten(body["program_steps"])
        if n["kind"] == "measure_heights"
    }
    if not nodes:
        return
    required = set().union(*(height_experiments(n["params"]) for n in nodes.values()))
    if not required <= set(report.get("experiment_fingerprints", {})):
        raise ValueError("多面高度试运行缺少全部启用面的实验指纹")
    expected = Counter()

    def walk(items, multiplier=1):
        for node in items:
            if node["kind"] == "measure_heights":
                expected[node["step_id"]] += multiplier
            if node["kind"] == "point_loop":
                walk(node["children"], multiplier * len(node["params"]["points_um"]))

    walk(body["program_steps"])
    scans = list(report.get("height_scans", {}).values())
    actual = Counter()
    for scan in scans:
        sid = scan.get("step_path", "").split("/")[-1]
        if sid not in nodes:
            raise ValueError("多面高度试运行步骤不匹配")
        actual[sid] += 1
        p = nodes[sid]["params"]
        count = p.get("surface_count", 3)
        npoints = 1 if p.get("xy_mode") == "current" else len(p["points_um"])
        rows = scan.get("rows", [])
        valid = (
            scan.get("state") == "complete"
            and scan.get("data_complete") is True
            and scan.get("return_complete") is True
            and scan.get("surface_count") == count
            and sorted(r.get("point_no", 0) for r in rows)
            == list(range(1, npoints + 1))
        )
        for row in rows:
            fs = row.get("faces", [])
            valid = valid and row.get("status") == "complete" and len(fs) == count
            valid = valid and all(
                f.get("state") == "complete"
                and f.get("surface") == i + 1
                and f.get("experiment") == str(p["surfaces"][i]["experiment"]).strip()
                and f.get("experiment_sha256")
                == report["experiment_fingerprints"].get(f.get("experiment"))
                and row.get(f"Z{i+1}_um") is not None
                for i, f in enumerate(fs)
            )
        if not valid:
            raise ValueError("多面高度试运行未完整采集或未确认结束定位")
    if actual != expected:
        raise ValueError("多面高度试运行未覆盖全部步骤/循环实例")
