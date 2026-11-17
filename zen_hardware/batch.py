"""Selected hardware checks, complete reporting and no blind write retries."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from math import isfinite, hypot
from zen_records import write_json, utc_now, software_version
from .library import OPERATIONS

ADVICE = {
    "UNIMPLEMENTED": "当前 ZEN 未提供该方法；核对已注册服务命名空间和官方版本适配，不将其视为整机离线。",
    "INVALID_ARGUMENT": "核对参数单位及数值；实验 ID 应来自本次 Load/Clone，不能使用会话 ID。",
    "NOT_FOUND": "核对实验名称、相机 ID 及本次连接中对象是否仍有效。",
    "ALREADY_EXISTS": "输出名已存在，改用唯一输出名，不覆盖已有数据。",
    "UNAUTHENTICATED": "核对 Gateway 令牌、地址及 CA 证书。",
    "PERMISSION_DENIED": "核对 ZEN API 许可与 Unsupervised API Mode。",
    "DEADLINE_EXCEEDED": "在 ZEN 检查动作是否仍运行，并按实际实验时长设置超时；不自动重发动作。",
    "TimeoutError": "检查实际位置/实验状态及接口读回；动作未核验结束时后续写入会跳过，其他检查继续。",
    "ValueError": "核对参数、连接模式和前置测试；需要实验或相机的项目须先取得有效 ID。",
}


def execute_batch(
    session,
    plan,
    *,
    timeout_s,
    stop,
    progress=lambda report: None,
    zero_z=False,
    zero_xy=False,
):
    identity = uuid4().hex
    folder = session.root / "hardware_batches" / identity
    folder.mkdir(parents=True)
    report = {
        "kind": "hardware_batch",
        "schema_version": 1,
        "software_version": software_version(),
        "batch_id": identity,
        "simulated": session.simulated,
        "started_at": utc_now(),
        "state": "running",
        "zero_mode": "absolute_coordinate_0_not_homing",
        "completion_tolerance_um": {"z": 0.5, "xy": 1},
        "items": [],
        "directory": str(folder),
    }
    context = {
        "experiment_id": getattr(session.library.device, "_experiment_id", None),
        "camera_id": None,
    }
    rows = deepcopy(plan)
    # Disconnect is deliberately the last selected check, after optional zero moves.
    tail = [r for r in rows if r["operation"] == "disconnect"]
    rows = [r for r in rows if r["operation"] != "disconnect"]
    for key, selected, p in [
        ("move_z", zero_z, {"z_um": 0}),
        ("move_xy", zero_xy, {"x_um": 0, "y_um": 0}),
    ]:
        if selected:
            rows.append(
                {
                    "operation": key,
                    "selected": True,
                    "parameters": p,
                    "zero": True,
                    "label": "结束后移到绝对坐标 0 · "
                    + ("Z" if key == "move_z" else "XY"),
                }
            )
    rows += tail

    def save():
        write_json(folder / "report.json", report)
        lines = [
            "# 硬件一键测试报告",
            "",
            f"批次：{identity}",
            f"状态：{report['state']}",
            f"模拟：{session.simulated}",
            "",
            "回零表示移动至绝对坐标 0 µm，不是设备寻原点。",
            "",
            "| 项目 | 结果 | 原因 / 建议 |",
            "|---|---|---|",
        ]
        for row in report["items"]:
            detail = (
                (row.get("message", "") + " " + row.get("advice", ""))
                .replace("|", "/")
                .replace("\n", " ")
            )
            lines.append(f"| {row['title']} | {row['result']} | {detail} |")
        (folder / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        progress(deepcopy(report))

    save()
    for index, row in enumerate(rows):
        key = row["operation"]
        op = OPERATIONS[key]
        item = {
            "operation": key,
            "title": row.get("label") or op.title,
            "selected": bool(row.get("selected")),
        }
        report["items"].append(item)
        if not item["selected"]:
            item["result"] = "未勾选"
            save()
            continue
        if stop.is_set():
            item["result"] = "已停止，未执行"
            save()
            continue
        if op.xy_required and not getattr(session.library.device, "xy_enabled", True):
            item.update(result="不适用", message="本机无电动 XY，未发送 XY 请求")
            save()
            continue
        params = deepcopy(row.get("parameters", {}))
        try:
            if row.get("zero") and not session.uncertain:
                d = session.library.device
                if key == "move_z":
                    current = d.read_z()
                    if not isfinite(current):
                        raise ValueError("Z 读回无效，未执行回零")
                    already = abs(current) <= 0.5e-6
                else:
                    current = d.read_xy()
                    if not all(isfinite(v) for v in current):
                        raise ValueError("XY 读回无效，未执行回零")
                    already = hypot(*current) <= 1e-6
                if already:
                    item.update(result="OK", message="已在绝对零坐标，无需发送重复移动")
                    save()
                    continue
            for field in op.fields:
                if field.name not in params:
                    params[field.name] = (
                        ""
                        if field.optional
                        else (
                            "@" + field.name
                            if field.name in ("experiment_id", "camera_id")
                            else ""
                        )
                    )
            for name, value in list(params.items()):
                if value in ("@experiment_id", "@camera_id"):
                    resolved = context[value[1:]]
                    if not resolved:
                        raise ValueError(
                            "缺少本批次已加载实验/相机 ID，请勾选对应前置项目并填写参数"
                        )
                    params[name] = resolved
            if "output_name" in params:
                params["output_name"] = (
                    (params["output_name"] or "test")
                    + "_"
                    + identity[:8]
                    + "_"
                    + str(index)
                )
            raw = session.execute(
                key, params, confirmed=True, timeout_s=timeout_s, stop=stop
            )
            item["report"] = raw
            item["parameters"] = params
            error = raw.get("error") or raw.get("completion", {}).get("error") or {}
            ok = raw["status"] == "success" and not raw.get("recovery_required")
            result = raw.get("result") or {}
            if ok and key == "read_optics":
                positions = result.get(
                    "positions",
                    [result.get("objective_position"), result.get("optovar_position")],
                )
                if len(positions) != 2 or any(v is None for v in positions):
                    ok = False
                    error = {
                        "code": "UNIMPLEMENTED",
                        "message": "转盘/倍率位置未完整核验；接口返回不等于实际位置已读取。",
                    }
            if ok and key == "read_z":
                value = result.get("z_m")
                if not isinstance(value, (int, float)) or not isfinite(value):
                    ok = False
                    error = {
                        "code": "ValueError",
                        "message": "Z 未返回有限位置，不能判为 OK。",
                    }
            item["result"] = "OK" if ok else "不OK"
            if not ok:
                item["message"] = error.get("message") or raw.get(
                    "note", "完成状态未核验"
                )
                item["advice"] = ADVICE.get(
                    error.get("code"),
                    "查看本项 RPC 记录和 ZEN 设备状态；后续只读检查继续，不盲目重试。",
                )
            if raw["status"] == "success":
                result = raw.get("result") or {}
                if key in ("connect", "disconnect"):
                    context = {"experiment_id": None, "camera_id": None}
                if key in ("load_experiment", "clone_experiment"):
                    context["experiment_id"] = result.get("experiment_id")
                if key == "list_cameras" and result.get("cameras"):
                    context["camera_id"] = result["cameras"][0].get("id")
        except Exception as error:
            item.update(
                result="不OK",
                message=str(error),
                advice=ADVICE.get(
                    type(error).__name__, "检查参数及本批次前置项目；查看详细日志。"
                ),
            )
        save()
    report["state"] = "stopped" if stop.is_set() else "complete"
    report["finished_at"] = utc_now()
    report["counts"] = {
        label: sum(r["result"] == label for r in report["items"])
        for label in ("OK", "不OK", "不适用", "未勾选", "已停止，未执行")
    }
    save()
    return report
