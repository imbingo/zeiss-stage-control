"""Declarative engineering step catalog and compiler; no device I/O in previews."""

from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from uuid import uuid4
import re
from zen_records import canonical_sha256

# name, label, kind, default. Units are in the label and JSON keys.
CATALOG = {
    "load_experiment": ("实验", "加载实验", [("experiment", "ZEN 实验", "text", "")]),
    "move_z": (
        "运动",
        "Z 调焦 MoveTo",
        [
            ("z_um", "目标 Z · µm / 引用", "number", ""),
            ("relative", "相对当前 Z", "bool", False),
            ("timeout_s", "到位超时 · s", "positive", 15),
        ],
    ),
    "move_xy": (
        "运动",
        "XY MoveTo",
        [
            ("x_um", "目标 X · µm / 引用", "number", ""),
            ("y_um", "目标 Y · µm / 引用", "number", ""),
            ("safe_z_um", "转场 Z · µm（空则继承）", "optional", ""),
            ("timeout_s", "到位超时 · s", "positive", 15),
        ],
    ),
    "autofocus": (
        "对焦与采集",
        "自动聚焦 AF",
        [
            ("center_um", "搜索中心 Z · µm（空=当前）", "optional", ""),
            ("half_um", "AF 半范围 · µm", "positive", 5),
            ("timeout_s", "AF 超时 · s", "positive", 30),
        ],
    ),
    "zstack": (
        "对焦与采集",
        "Z-stack 扫描",
        [
            ("first_um", "起始 Z · µm / 引用", "number", ""),
            ("last_um", "结束 Z · µm / 引用", "number", ""),
            ("interval_um", "层间距 · µm", "positive", 0.5),
            ("timeout_s", "采集超时 · s", "positive", 180),
            ("output_name", "输出名称", "text", "stack"),
        ],
    ),
    "capture_upper": (
        "对焦与采集",
        "拍摄上 mark",
        [
            ("capture_kind", "采集方式", "capture", "confocal"),
            ("timeout_s", "采集超时 · s", "positive", 180),
            ("pair_id", "上下图像配对编号", "text", "mark"),
        ],
    ),
    "capture_lower": (
        "对焦与采集",
        "拍摄下 mark",
        [
            ("capture_kind", "采集方式", "capture", "confocal"),
            ("timeout_s", "采集超时 · s", "positive", 180),
            ("pair_id", "上下图像配对编号", "text", "mark"),
        ],
    ),
    "measure_angle": (
        "测量与判断",
        "共聚焦扫描物料角度",
        [
            ("experiment", "测角实验", "text", ""),
            ("z_um", "统一预期焦面 Z · µm", "number", ""),
            ("half_um", "AF 半范围 · µm", "positive", 5),
            ("timeout_s", "每点 AF 超时 · s", "positive", 30),
            ("points_um", "有序 XY / 可选预期 Z · µm", "points", []),
        ],
    ),
    "angle_gate": (
        "测量与判断",
        "角度结果判定",
        [
            ("source", "测角步骤编号", "text", ""),
            ("max_rx_urad", "最大 |Rx| · µrad", "positive", 100),
            ("max_ry_urad", "最大 |Ry| · µrad", "positive", 100),
            ("max_rms_um", "最大 RMS · µm", "positive", 1),
        ],
    ),
    "set_gain": (
        "参数设置",
        "设置实验主增益",
        [
            ("channel", "通道 / 探测器", "text", ""),
            ("value", "主增益（API 原始单位）", "number", ""),
        ],
    ),
    "point_loop": (
        "流程控制",
        "按点位循环",
        [("points_um", "有序 XY / 可选 Z · µm", "points", [])],
    ),
    "wait": ("流程控制", "等待稳定", [("seconds", "等待时间 · s", "positive", 0.2)]),
}
COMMON_FIELDS = [
    ("z_min_um", "允许 Z 下限 · µm"),
    ("z_max_um", "允许 Z 上限 · µm"),
    ("safe_z_um", "默认转场 Z · µm"),
    ("xy_tolerance_um", "XY 到位容差 · µm"),
    ("z_tolerance_um", "Z 到位容差 · µm"),
    ("settle_s", "稳定等待 · s"),
]
OUTPUTS = {
    "move_z": {"z_um"},
    "autofocus": {"z_um"},
    "measure_angle": {"rx_urad", "ry_urad", "rms_um"},
}


def make_step(kind, **params):
    if kind not in CATALOG:
        raise ValueError("未知步骤")
    return {
        "step_id": "S" + uuid4().hex[:7].upper(),
        "kind": kind,
        "label": CATALOG[kind][1],
        "params": {**{k: deepcopy(d) for k, _, _, d in CATALOG[kind][2]}, **params},
        **({"children": []} if kind == "point_loop" else {}),
    }


def flatten(nodes, prefix=""):
    for n in nodes:
        yield prefix + n.get("step_id", ""), n
        yield from flatten(n.get("children", []), prefix + n.get("step_id", "") + "/")


def migrate_program(body):
    """Copy old v2.7 process values into explicit editable actions; no device calls."""
    b = deepcopy(body)
    if "program_steps" in b:
        return b
    c = b.get("common_params", {})
    t = b.get("tilt", {})
    nodes = []
    if any(n.get("kind") == "tilt" for n in b.get("step_tree", [])):
        m = make_step(
            "measure_angle",
            experiment=t.get("experiment", ""),
            z_um=t.get("z_um", ""),
            half_um=t.get("af_half_um", 5),
            timeout_s=t.get("af_timeout_s", 30),
            points_um=t.get("points_um", []),
        )
        nodes.append(m)
        nodes.append(
            make_step(
                "angle_gate",
                source=m["step_id"],
                max_rx_urad=c.get("max_rx_urad", c.get("max_abs_angle_urad", 100)),
                max_ry_urad=c.get("max_ry_urad", c.get("max_abs_angle_urad", 100)),
                max_rms_um=c.get("max_rms_um", 1),
            )
        )
    for parent in b.get("step_tree", []):
        if parent.get("kind") != "group":
            continue
        for old in parent.get("children", []):
            p = old.get("params", {})
            nodes.append(
                make_step("move_xy", x_um=p.get("x_um", ""), y_um=p.get("y_um", ""))
            )
            if old.get("kind") == "transit":
                continue
            pair = old.get("step_id", "mark")
            for layer in ("upper", "lower"):
                nodes.append(
                    make_step(
                        "load_experiment",
                        experiment=p.get(layer + "_experiment")
                        or c.get(layer + "_experiment", ""),
                    )
                )
                nodes.append(make_step("move_z", z_um=p.get(layer + "_z_um", "")))
                if p.get("use_autofocus", c.get("use_autofocus", False)):
                    nodes.append(
                        make_step("autofocus", half_um=p.get(layer + "_half_um", 5))
                    )
                nodes.append(
                    make_step(
                        "capture_" + layer,
                        pair_id=pair,
                        capture_kind=c.get("capture_kind", "confocal"),
                        timeout_s=c.get("capture_timeout_s", 180),
                    )
                )
    if c.get("safe_z_um") not in ("", None):
        nodes.append(make_step("move_z", z_um=c["safe_z_um"]))
    b["program_steps"] = nodes
    b["program_schema"] = 1
    # Old UI claimed sample coordinates without implementing a transform. Require declaration.
    b.setdefault("coordinate_system", "设备绝对坐标（装夹基准须人工核对）")
    return b


def insert_template(name, body):
    b = deepcopy(body)
    c = b.get("common_params", {})
    t = b.get("tilt", {})
    if name == "空白流程":
        return []
    if name == "多点物料测角":
        m = make_step(
            "measure_angle",
            experiment=t.get("experiment", ""),
            z_um=t.get("z_um", ""),
            half_um=t.get("af_half_um", 5),
            points_um=t.get("points_um", []),
        )
        return [m, make_step("angle_gate", source=m["step_id"])]
    if name == "单点上下 mark":
        result = []
        for layer in ("upper", "lower"):
            result += [
                make_step(
                    "load_experiment", experiment=c.get(layer + "_experiment", "")
                ),
                make_step("move_z"),
                make_step("capture_" + layer),
            ]
        return result
    raise ValueError("未知模板")


def numeric(value, label, positive=False, allow_ref=True):
    if isinstance(value, str) and value.startswith("@") and allow_ref:
        return value
    if isinstance(value, bool):
        raise ValueError(label + " 必须为数字")
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise ValueError(label + " 尚未填写有效数字") from None
    if not isfinite(n) or (positive and n <= 0):
        raise ValueError(label + " 必须为有限" + ("正数" if positive else "数字"))
    return n


@dataclass(frozen=True)
class ProgramPlan:
    name: str
    steps: tuple
    common: dict
    recipe_identity: dict
    simulated_configuration: bool = False
    execution_scope: str | None = None

    def to_dict(self):
        return {
            "kind": "step_program",
            "schema_version": 1,
            "coordinate_unit": "um",
            "name": self.name,
            "steps": deepcopy(list(self.steps)),
            "common": deepcopy(self.common),
            "recipe_identity": deepcopy(self.recipe_identity),
            "simulated_configuration": self.simulated_configuration,
            "execution_scope": self.execution_scope,
        }

    def validate(self, *_):
        return self


def validate_program(body, identity=None, scope=None):
    errors = []
    nodes = deepcopy(body.get("program_steps", []))
    common = deepcopy(body.get("common_params", {}))
    seen = {}
    ids = set()
    loaded = False
    if body.get("coordinate_system") != "设备绝对坐标（装夹基准须人工核对）":
        errors.append("请在公共参数中核对并声明设备绝对坐标；不执行自动坐标变换")
    if not nodes:
        errors.append("请添加至少一个步骤")
    if len(list(flatten(nodes))) > 1000:
        errors.append("步骤数量超过 1000")

    def check(items, depth=0, available=None, has_experiment=False):
        local = dict(available or {})
        active = has_experiment
        if depth > 2:
            errors.append("最多支持两层点位循环")
            return
        for node in items:
            sid = node.get("step_id", "")
            kind = node.get("kind")
            p = node.get("params", {})
            if (
                not isinstance(sid, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", sid)
                or sid in ids
            ):
                errors.append("步骤编号为空或重复：" + sid)
            ids.add(sid)
            if kind not in CATALOG:
                errors.append(sid + " 未知步骤类型")
                continue
            if kind != "point_loop" and node.get("children"):
                errors.append(sid + " 仅点位循环可包含子步骤")
            if kind == "set_gain":
                errors.append(sid + " 实验主增益尚无已验证接口，禁止写入")
            for key, label, typ, default in CATALOG[kind][2]:
                value = p.setdefault(key, deepcopy(default))
                try:
                    if typ in ("number", "positive", "optional"):
                        if typ == "optional" and value in ("", None):
                            continue
                        n = numeric(value, label, typ == "positive")
                        if isinstance(n, str):
                            parts = n[1:].split(".")
                            if (
                                len(parts) != 2
                                or (
                                    parts[0] == "point"
                                    and (
                                        depth == 0
                                        or parts[1] not in ("x_um", "y_um", "z_um")
                                    )
                                )
                                or (
                                    parts[0] != "point"
                                    and parts[1] not in local.get(parts[0], set())
                                )
                            ):
                                raise ValueError("无效或尚未产生的结果引用 " + n)
                        else:
                            p[key] = n
                    elif typ == "text" and not str(value).strip():
                        raise ValueError(label + " 不能为空")
                    elif typ == "bool" and type(value) is not bool:
                        raise ValueError(label + " 必须为开关")
                    elif typ == "capture" and value not in ("confocal", "camera_snap"):
                        raise ValueError("未知采集方式")
                    elif typ == "points":
                        if not isinstance(value, list) or not 1 <= len(value) <= 1000:
                            raise ValueError("请填写点位表（1–1000 行）")
                        for row in value:
                            if len(row) not in (2, 3):
                                raise ValueError("点位须为 XY 或 XYZ")
                            for v in row:
                                if v != "":
                                    numeric(v, "点位", allow_ref=False)
                            for v in row[:2]:
                                numeric(v, "XY 点位", allow_ref=False)
                except (ValueError, TypeError) as e:
                    errors.append(sid + "：" + str(e))
            if kind in ("load_experiment", "measure_angle"):
                active = True
            if (
                kind in ("autofocus", "capture_upper", "capture_lower", "zstack")
                and not active
            ):
                errors.append(sid + " 前面需要加载实验")
            if kind == "angle_gate" and "rx_urad" not in local.get(
                p.get("source"), set()
            ):
                errors.append(sid + " 必须引用此前测角步骤的结果")
            if kind == "measure_angle":
                try:
                    from zen_fit import _design

                    _design(
                        [
                            (float(r[0]) * 1e-6, float(r[1]) * 1e-6)
                            for r in p["points_um"]
                        ]
                    )
                except (KeyError, TypeError, ValueError) as e:
                    errors.append(sid + "：" + str(e))
            if kind == "zstack" and (
                any(c in str(p.get("output_name", "")) for c in ("/", "\\"))
                or len(str(p.get("output_name", ""))) > 64
            ):
                errors.append(sid + " 输出名称请使用不超过 64 字的名称，不含路径分隔符")
            if kind == "zstack":
                try:
                    first, last, interval = (
                        p[k] for k in ("first_um", "last_um", "interval_um")
                    )
                    if not isinstance(first, str) and not isinstance(last, str):
                        count = abs(last - first) / interval
                        if (
                            count == 0
                            or count > 4095
                            or abs(count - round(count)) > 1e-6
                        ):
                            raise ValueError("Z-stack 起止与间距须形成 2–4096 个等距层")
                except (TypeError, ValueError) as e:
                    errors.append(sid + "：" + str(e))
            if kind == "point_loop":
                if any(
                    v == "@point.z_um"
                    for _, child in flatten(node.get("children", []))
                    for v in child.get("params", {}).values()
                    if isinstance(v, str)
                ) and any(
                    len(row) < 3 or row[2] == "" for row in p.get("points_um", [])
                ):
                    errors.append(sid + " 循环子步骤引用 Z，但点位表存在未填写的 Z")
                if not node.get("children"):
                    errors.append(sid + " 循环内没有动作")
                check(node.get("children", []), depth + 1, local, active)
            local[sid] = OUTPUTS.get(kind, set())
        return local

    check(nodes)
    if any(n.get("kind") != "wait" for _, n in flatten(nodes)):
        for key in (
            "z_min_um",
            "z_max_um",
            "z_tolerance_um",
            "xy_tolerance_um",
            "settle_s",
        ):
            try:
                common[key] = numeric(
                    common.get(key), key, key not in ("z_min_um", "z_max_um"), False
                )
            except ValueError as e:
                errors.append(str(e))
        try:
            low, high = common["z_min_um"], common["z_max_um"]
            if float(low) >= float(high):
                errors.append("Z 下限必须小于上限")
            for sid, n in flatten(nodes):
                p = n.get("params", {})
                k = n["kind"]
                values = []
                if k == "move_z" and not p.get("relative"):
                    values = [p.get("z_um")]
                elif k == "move_xy" or k == "measure_angle":
                    values = [
                        (
                            p.get("safe_z_um")
                            if p.get("safe_z_um") not in ("", None)
                            else common.get("safe_z_um")
                        )
                    ]
                elif k == "zstack":
                    values = [p.get("first_um"), p.get("last_um")]
                elif (
                    k == "autofocus"
                    and p.get("center_um") not in ("", None)
                    and not isinstance(p.get("center_um"), str)
                    and not isinstance(p.get("half_um"), str)
                ):
                    values = [
                        p["center_um"] - p["half_um"],
                        p["center_um"] + p["half_um"],
                    ]
                for v in values:
                    if isinstance(v, str) and v.startswith("@"):
                        continue
                    if (
                        not float(low)
                        <= numeric(v, "目标 Z", allow_ref=False)
                        <= float(high)
                    ):
                        errors.append(sid + " Z 超出任务范围")
                if k == "measure_angle":
                    for row in p.get("points_um", []):
                        seed = numeric(
                            row[2] if len(row) > 2 and row[2] != "" else p.get("z_um"),
                            "测角焦面",
                            allow_ref=False,
                        )
                        if (
                            not float(low)
                            <= seed - float(p["half_um"])
                            < seed + float(p["half_um"])
                            <= float(high)
                        ):
                            errors.append(sid + " 测角 AF 范围越界")
        except (ValueError, TypeError, KeyError) as e:
            errors.append(str(e))

    def work(items):
        return sum(
            (
                len(n.get("params", {}).get("points_um", []))
                * work(n.get("children", []))
                if n.get("kind") == "point_loop"
                else 1
            )
            for n in items
        )

    if work(nodes) > 10000:
        errors.append("展开循环后超过 10000 个动作，请拆分任务")
    if errors:
        raise ValueError("\n".join(dict.fromkeys(errors)))
    return ProgramPlan(
        body.get("product_family", "配方"),
        tuple(nodes),
        common,
        deepcopy(identity or {}),
        bool(body.get("example")),
        scope,
    )


def preview_program(body):
    rows = []
    for path, node in flatten(body.get("program_steps", [])):
        rows.append(
            {
                "path": path,
                "step_id": node.get("step_id", ""),
                "kind": node.get("kind", ""),
                "title": node.get("label")
                or CATALOG.get(node.get("kind"), ("", "未知步骤", []))[1],
            }
        )
    return rows


def capability_issues(plan, device):
    errors = []
    caps = getattr(device, "capabilities", None)
    services = getattr(caps, "services", None)
    for path, n in flatten(plan.steps):
        kind = n["kind"]
        if kind in ("move_xy", "measure_angle") and not getattr(
            device, "xy_enabled", True
        ):
            errors.append(path + "：当前设备没有电动 XY")
        if kind == "set_gain":
            errors.append(path + "：主增益接口待适配")
        if kind == "zstack" and not callable(getattr(device, "capture_stack", None)):
            errors.append(path + "：缺少已验证 Z-stack 采集适配")
        if services is not None:
            if kind in ("autofocus", "measure_angle") and not any(
                "ExperimentSwAutofocusService" in s for s in services
            ):
                errors.append(path + "：未提供软件 AF 服务")
            if (
                kind
                in (
                    "load_experiment",
                    "capture_upper",
                    "capture_lower",
                    "zstack",
                    "measure_angle",
                )
                and "zen_api.acquisition.v1beta.ExperimentService" not in services
            ):
                errors.append(path + "：缺少实验服务")
            if (
                kind in ("capture_upper", "capture_lower", "zstack")
                and "zen_api.acquisition.v1beta.ExperimentStreamingService"
                not in services
            ):
                errors.append(path + "：缺少图像流服务")
    return errors
