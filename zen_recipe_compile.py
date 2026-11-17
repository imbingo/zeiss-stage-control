"""Compile an identified recipe revision to a frozen SI execution plan."""

from copy import deepcopy
from math import isfinite
from zen_sequence import SequencePlan, Station
from zen_marks import MarkRecipe, MarkLayer
from zen_workflow import Recipe
from zen_fit import FIELD_CONVENTION, DEMO_CONVENTION, validate_recipe

TEMPLATES = {"测角 → 判定 → 上下孔采图": True, "仅上下孔采图": False}


def number(value, label, positive=False):
    if isinstance(value, bool):
        raise ValueError(label + " 必须为数字")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(label + " 尚未填写或不是数字") from None
    if not isfinite(result) or (positive and result <= 0):
        raise ValueError(label + " 必须为有限" + ("正数" if positive else "数字"))
    return result


def compile_body(body, *, simulated=False, identity=None):
    if "program_steps" in body:
        from zen_steps import validate_program
        return validate_program(body,identity)
    if body.get("units") != {"length": "um", "angle": "urad", "time": "s"}:
        raise ValueError("配方单位必须为 µm / µrad / s")
    if body.get("coordinate_system") != "设备绝对坐标（装夹基准须人工核对）":
        raise ValueError("当前只支持设备绝对坐标；旧样品坐标配置须重新核对")
    template = body.get("template")
    if template not in TEMPLATES:
        raise ValueError("不支持的流程模板")
    measured = TEMPLATES[template]
    expected = (
        ["common"]
        + (["tilt", "gate"] if measured else [])
        + ["safe_transfer", "group", "return_z", "archive"]
    )
    nodes = body.get("step_tree", [])
    if [n.get("kind") for n in nodes] != expected:
        raise ValueError("步骤顺序或依赖无效，请重新选择流程模板")
    ids = [n.get("step_id") for n in nodes]
    common = body.get("common_params", {})
    tilt = body.get("tilt", {})

    def n(key, positive=False):
        return number(common.get(key), key, positive)

    safe, low, high = n("safe_z_um"), n("z_min_um"), n("z_max_um")
    tolerance = n("xy_tolerance_um", True) * 1e-6
    arrival = number(tilt.get("arrival_timeout_s", 15), "移动超时", True)
    af_timeout = number(tilt.get("af_timeout_s", 30), "AF 超时", True)
    zseed = number(tilt.get("z_um"), "测角起始 Z") if measured else safe
    targets = []
    seeds = []
    for row in tilt.get("points_um", []) if measured else []:
        if len(row) not in (2, 3):
            raise ValueError("测角点必须为 XY，另可填写该点预期 Z")
        targets.append(tuple(number(v, "测角坐标") * 1e-6 for v in row[:2]))
        seeds.append(
            number(row[2], "逐点预期 Z") * 1e-6
            if len(row) > 2 and str(row[2]).strip()
            else zseed * 1e-6
        )
    exp = str(tilt.get("experiment", "")).strip()
    if measured and not exp:
        raise ValueError("请选择测角实验")
    recipe = Recipe(
        body.get("product_family", "配方"),
        exp or "未启用测角",
        tuple(targets),
        tolerance,
        af_timeout,
        arrival,
        DEMO_CONVENTION if simulated else FIELD_CONVENTION,
    )
    if measured:
        validate_recipe(recipe)
    half = number(tilt.get("af_half_um", 5), "测角 AF 半范围", True) * 1e-6
    if measured and any(
        not low * 1e-6 <= z - half < z + half <= high * 1e-6 for z in seeds
    ):
        raise ValueError("测角 AF 范围超出任务 Z 范围")
    stations = []
    group = next(n for n in nodes if n["kind"] == "group")
    for node in group.get("children", []):
        kind = node.get("kind")
        p = node.get("params", {})
        sid = node.get("step_id", "")
        if kind not in ("hole", "transit"):
            raise ValueError("不支持的孔位步骤类型")
        ids.append(sid)
        mark = None
        if kind == "hole":

            def layer(prefix):
                experiment = str(
                    p.get(prefix + "_experiment")
                    or common.get(prefix + "_experiment", "")
                ).strip()
                if not experiment:
                    raise ValueError(sid + " 缺少" + prefix + "实验")
                return MarkLayer(
                    experiment,
                    number(p.get(prefix + "_z_um"), sid + " Z") * 1e-6,
                    number(p.get(prefix + "_half_um", 5), sid + " AF 半范围", True)
                    * 1e-6,
                )

            mark = MarkRecipe(
                layer("upper"),
                layer("lower"),
                xy_tolerance_m=tolerance,
                z_tolerance_m=n("z_tolerance_um", True) * 1e-6,
                af_timeout_s=af_timeout,
                arrival_timeout_s=arrival,
                capture_timeout_s=n("capture_timeout_s", True),
                settle_s=n("settle_s", True),
                capture_kind=common.get("capture_kind", "confocal"),
                use_autofocus=p.get(
                    "use_autofocus", common.get("use_autofocus", False)
                ),
            )
        stations.append(
            Station(
                sid,
                number(p.get("x_um"), sid + " X") * 1e-6,
                number(p.get("y_um"), sid + " Y") * 1e-6,
                mark,
            )
        )
    if any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(
        set(ids)
    ):
        raise ValueError("步骤编号必须非空且全局唯一")
    plan = SequencePlan(
        recipe.name,
        recipe,
        tuple(stations),
        safe * 1e-6,
        zseed * 1e-6,
        low * 1e-6,
        high * 1e-6,
        n("max_abs_angle_urad", True),
        n("max_rms_um", True) * 1e-6,
        n("z_tolerance_um", True) * 1e-6,
        n("settle_s", True),
        bool(body.get("example")),
        measure_enabled=measured,
        recipe_identity=deepcopy(identity or {}),
        max_rx_urad=n("max_rx_urad", True),
        max_ry_urad=n("max_ry_urad", True),
        tilt_af_half_m=half,
        tilt_seed_z_m=tuple(seeds),
    )
    plan.validate(validate_recipe)
    return plan


def compile_revision(library, recipe_id, revision, *, mode, simulated):
    from zen_records import canonical_sha256

    version = library.get(recipe_id, revision)
    if not version:
        raise ValueError("配方版本不存在")
    if version["status"] == "archived":
        raise ValueError("归档版本不可执行")
    if mode == "production" and version["status"] != "released":
        raise ValueError("正式运行只接受已放行版本")
    body = deepcopy(version["body"])
    if body.get("example") and not simulated:
        raise ValueError("演示配方禁止在现场执行")
    if version["content_sha256"] != canonical_sha256(body):
        raise ValueError("配方内容哈希不一致")
    return compile_body(
        body,
        simulated=simulated,
        identity={
            "recipe_id": recipe_id,
            "revision": revision,
            "content_sha256": version["content_sha256"],
            "execution_mode": mode,
            "status": version["status"],
            "experiment_fingerprints": version.get("validation", {})
            .get("trial", {})
            .get("experiment_fingerprints", {}),
        },
    )
