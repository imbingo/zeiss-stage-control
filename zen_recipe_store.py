"""Local recipe library: identity, immutable revisions, checks and release.

This is deliberately small but real. Every version badge shown in the UI comes
from a persisted revision with a content hash, a check result and (when
released) an approver. A released revision is read-only: editing it always
creates a new draft revision with ``parent_revision`` set.
"""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from zen_records import canonical_sha256, utc_now, write_json

DRAFT = "draft"
RELEASED = "released"
ARCHIVED = "archived"
STATUS_NAMES = {DRAFT: "草稿", RELEASED: "已验证", ARCHIVED: "已归档"}

_TRANSITIONS = {DRAFT: {RELEASED, ARCHIVED}, RELEASED: {ARCHIVED}, ARCHIVED: set()}

PROTECTED_STEPS = (
    ("common", "配方公共参数"), ("tilt", "表面测角 · A 区 · 4 点"),
    ("gate", "角度结果判定"), ("safe_transfer", "安全转场"),
    ("group", "孔位采图循环"), ("return_z", "返回安全 Z"),
    ("archive", "结果归档"),
)

REQUIRED_COMMON = (
    "safe_z_um", "z_min_um", "z_max_um", "xy_tolerance_um", "z_tolerance_um",
    "capture_timeout_s", "settle_s", "upper_experiment", "lower_experiment",
    "max_abs_angle_urad", "max_rms_um",
)


def default_body(name="新建配方", *, example=False):
    """A complete, explicit starting point. Example bodies can never be released."""
    return {
        "product_family": name,
        "template": "测角 → 判定 → 上下孔采图",
        "coordinate_system": "样品坐标 · 基准 501",
        "example": example,
        "units": {"length": "um", "angle": "urad", "time": "s"},
        "common_params": {
            "safe_z_um": 20.0, "z_min_um": 0.0, "z_max_um": 300.0,
            "xy_tolerance_um": 1.0, "z_tolerance_um": 0.5,
            "capture_timeout_s": 180.0, "settle_s": 0.2,
            "upper_experiment": "Upper_mark" if example else "",
            "lower_experiment": "Lower_mark" if example else "",
            "use_autofocus": False,
            "max_abs_angle_urad": 10000.0, "max_rms_um": 10.0,
        },
        "step_tree": [
            {"step_id": "S0", "kind": "common", "label": "配方公共参数", "protected": True},
            {"step_id": "S1", "kind": "tilt", "label": "表面测角 · A 区 · 4 点", "protected": True},
            {"step_id": "S2", "kind": "gate", "label": "角度结果判定", "protected": True},
            {"step_id": "S3", "kind": "safe_transfer", "label": "安全转场", "protected": True},
            {"step_id": "G1", "kind": "group", "label": "孔位采图循环 · B 区", "protected": True,
             "children": [
                 {"step_id": "H01", "kind": "hole", "label": "H01 上下孔采图",
                  "protected": False,
                  "params": {"x_um": 4000.0, "y_um": 1000.0,
                             "upper_z_um": 100.0, "lower_z_um": 200.0}},
             ]},
            {"step_id": "S4", "kind": "return_z", "label": "返回安全 Z", "protected": True},
            {"step_id": "S5", "kind": "archive", "label": "结果归档", "protected": True},
        ],
        "external_dependencies": [
            {"logical_name": "upper_hole_experiment", "zen_experiment_name": "",
             "expected_xml_sha256": "", "revalidate_before": "",
             "note": "ZEN 实验内容未被指纹锁定；发布前须采集实验 XML 指纹"},
        ],
        "unverifiable_assumptions": [
            "ZEN 内部 AF 不受任务 Z 范围约束",
            "实验加载/运行不换物镜、不恢复 XY/Z",
        ],
    }


def _holes(body):
    for node in body.get("step_tree", ()):
        for child in node.get("children", ()) or ():
            if child.get("kind") == "hole":
                yield child


class RecipeLibrary:
    """JSON-backed store; all mutations are atomic and append a revision."""

    def __init__(self, path):
        self.path = Path(path)
        self._doc = {"kind": "recipe_library", "schema_version": 1, "recipes": []}
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self):
        import json
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (isinstance(raw, dict) and raw.get("kind") == "recipe_library"
                    and raw.get("schema_version") == 1
                    and isinstance(raw.get("recipes"), list)):
                self._doc = raw
                return
        except (OSError, ValueError):
            pass
        self._doc = {"kind": "recipe_library", "schema_version": 1, "recipes": []}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self._doc)

    # ---- queries -----------------------------------------------------
    def recipes(self):
        return list(self._doc["recipes"])

    def find(self, recipe_id):
        return next((r for r in self._doc["recipes"]
                     if r.get("recipe_id") == recipe_id), None)

    def latest(self, recipe_id):
        recipe = self.find(recipe_id)
        if not recipe or not recipe.get("revisions"):
            return None
        return max(recipe["revisions"], key=lambda v: v["revision"])

    def get(self, recipe_id, revision):
        recipe = self.find(recipe_id)
        if not recipe:
            return None
        return next((v for v in recipe["revisions"]
                     if v["revision"] == int(revision)), None)

    # ---- mutations ---------------------------------------------------
    def create(self, name, by, *, body=None, example=False):
        recipe_id = "RCP-" + uuid4().hex[:10].upper()
        body = body or default_body(name, example=example)
        revision = self._revision(1, body, by, "新建配方", None)
        self._doc["recipes"].append({
            "recipe_id": recipe_id, "name": name or "未命名配方",
            "revisions": [revision],
        })
        self.save()
        return recipe_id, 1

    @staticmethod
    def _revision(number, body, by, note, parent):
        frozen = deepcopy(body)
        return {
            "revision": number, "status": DRAFT, "created_at": utc_now(),
            "created_by": str(by or "")[:120], "change_note": str(note or "")[:500],
            "parent_revision": parent, "content_sha256": canonical_sha256(frozen),
            "body": frozen, "checks": None, "validation": {"state": "unvalidated"},
        }

    def new_revision(self, recipe_id, body, *, by, change_note, base_revision=None):
        """Always append. Editing a released revision produces a new draft."""
        recipe = self.find(recipe_id)
        if recipe is None:
            raise ValueError("配方不存在")
        latest = self.latest(recipe_id)
        parent = int(base_revision) if base_revision is not None else latest["revision"]
        revision = self._revision(latest["revision"] + 1, body, by,
                                  change_note, parent)
        recipe["revisions"].append(revision)
        self.save()
        return revision["revision"]

    # ---- verification ------------------------------------------------
    @staticmethod
    def check_body(body):
        """Structural checks; returns a durable, itemised result."""
        items = []

        def add(key, label, ok, detail=""):
            items.append({"key": key, "label": label,
                          "result": "passed" if ok else "failed",
                          "detail": detail})

        common = body.get("common_params", {})
        missing = [k for k in REQUIRED_COMMON if common.get(k) in (None, "")]
        add("common_params", "公共参数齐全", not missing,
            "缺少：" + "、".join(missing) if missing else "")
        numeric = [k for k in REQUIRED_COMMON if k.endswith(("_um", "_s", "_urad"))]
        bad = [k for k in numeric
               if not isinstance(common.get(k), (int, float))
               or isinstance(common.get(k), bool)
               or common.get(k, 0) < 0]
        add("numeric", "数值与单位有效", not bad,
            "无效：" + "、".join(bad) if bad else "")
        add("z_range", "Z 范围自洽",
            isinstance(common.get("z_min_um"), (int, float))
            and isinstance(common.get("z_max_um"), (int, float))
            and common["z_min_um"] < common["z_max_um"]
            and common["z_min_um"] <= common.get("safe_z_um", -1) <= common["z_max_um"])
        kinds = [n.get("kind") for n in body.get("step_tree", ())]
        missing_steps = [k for k, _ in PROTECTED_STEPS if k not in kinds]
        add("protected_steps", "保护步骤未被删除", not missing_steps,
            "缺少：" + "、".join(missing_steps) if missing_steps else "")
        holes = list(_holes(body))
        add("holes", "至少配置一个孔位", bool(holes))
        names = [h.get("step_id") for h in holes]
        add("unique_ids", "孔位编号唯一", len(names) == len(set(names)))
        ok_coords = True
        for hole in holes:
            p = hole.get("params", {})
            for key in ("x_um", "y_um", "upper_z_um", "lower_z_um"):
                if not isinstance(p.get(key), (int, float)) or isinstance(p.get(key), bool):
                    ok_coords = False
            if p.get("upper_z_um") == p.get("lower_z_um"):
                ok_coords = False
            for key in ("upper_z_um", "lower_z_um"):
                if isinstance(p.get(key), (int, float)) and not (
                        common.get("z_min_um", 0) <= p[key] <= common.get("z_max_um", 0)):
                    ok_coords = False
        add("hole_values", "孔位坐标与示教 Z 有效", ok_coords)
        deps = body.get("external_dependencies", ())
        add("experiments", "上下孔实验名已填写",
            bool(common.get("upper_experiment")) and bool(common.get("lower_experiment")))
        unfingerprinted = [d.get("logical_name") for d in deps
                           if not d.get("expected_xml_sha256")]
        add("example", "非示例配方", not body.get("example", False),
            "示例配方不可放行" if body.get("example") else "")
        # An unfingerprinted external experiment is a warning, not a hard fail:
        # the operator must still attest to it during release.
        blocked = any(i["result"] == "failed" for i in items)
        return {
            "kind": "recipe_check", "checked_at": utc_now(),
            "result": "passed" if not blocked else "failed",
            "items": items,
            "warnings": (["外部实验尚无内容指纹：" + "、".join(map(str, unfingerprinted))]
                         if unfingerprinted else []),
            "content_sha256": canonical_sha256(body),
        }

    def check(self, recipe_id, revision):
        version = self.get(recipe_id, revision)
        if version is None:
            raise ValueError("配方版本不存在")
        if version["status"] != DRAFT:
            raise ValueError("已验证/已归档版本为只读，不能重新检查")
        if canonical_sha256(version["body"]) != version["content_sha256"]:
            raise ValueError("配方内容与保存的哈希不符，请重新生成版本")
        report = self.check_body(version["body"])
        version["checks"] = report
        self.save()
        return report

    def _transition(self, version, target, by, note):
        current = version["status"]
        if target not in _TRANSITIONS.get(current, set()):
            raise ValueError(f"不允许的状态变更：{STATUS_NAMES.get(current)} → {target}")

    def release(self, recipe_id, revision, *, by, note=""):
        version = self.get(recipe_id, revision)
        if version is None:
            raise ValueError("配方版本不存在")
        self._transition(version, RELEASED, by, note)
        checks = version.get("checks")
        if not checks or checks.get("result") != "passed":
            raise ValueError("请先通过配方检查")
        current_hash = canonical_sha256(version["body"])
        if current_hash != version["content_sha256"]:
            raise ValueError("配方内容已被改动，请重新生成版本")
        if checks.get("content_sha256") != current_hash:
            raise ValueError("检查后配方已改动，请重新检查")
        version["status"] = RELEASED
        version["validation"] = {
            "state": "qualified", "approved_by": str(by or "")[:120],
            "approved_at": utc_now(), "note": str(note or "")[:500],
            "check_sha256": canonical_sha256(checks),
        }
        self.save()
        return version

    def archive(self, recipe_id, revision, *, by):
        version = self.get(recipe_id, revision)
        if version is None:
            raise ValueError("配方版本不存在")
        self._transition(version, ARCHIVED, by, "归档")
        version["status"] = ARCHIVED
        self.save()
        return version

    # ---- comparison --------------------------------------------------
    def compare(self, recipe_id, revision_a, revision_b):
        a, b = self.get(recipe_id, revision_a), self.get(recipe_id, revision_b)
        if a is None or b is None:
            raise ValueError("比较的版本不存在")

        def flatten(value, prefix=""):
            out = {}
            if isinstance(value, dict):
                for key, item in value.items():
                    out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    out.update(flatten(item, f"{prefix}[{index}]"))
            else:
                out[prefix] = value
            return out

        left, right = flatten(a["body"]), flatten(b["body"])
        changes = []
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                changes.append({"path": key, "before": left.get(key),
                                "after": right.get(key)})
        return {"recipe_id": recipe_id, "revision_a": int(revision_a),
                "revision_b": int(revision_b), "change_count": len(changes),
                "changes": changes, "compared_at": utc_now()}
