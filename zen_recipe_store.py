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
    ("common", "配方公共参数"),
    ("tilt", "共聚焦扫描物料角度"),
    ("gate", "角度结果判定"),
    ("safe_transfer", "安全转场"),
    ("group", "孔位采图循环"),
    ("return_z", "返回安全 Z"),
    ("archive", "结果归档"),
)

REQUIRED_COMMON = (
    "safe_z_um",
    "z_min_um",
    "z_max_um",
    "xy_tolerance_um",
    "z_tolerance_um",
    "capture_timeout_s",
    "settle_s",
    "upper_experiment",
    "lower_experiment",
    "max_abs_angle_urad",
    "max_rms_um",
)


def default_body(name="新建配方", *, example=False):
    """A complete, explicit starting point. Example bodies can never be released."""
    return {
        "product_family": name,
        "template": "测角 → 判定 → 上下孔采图",
        "coordinate_system": "设备绝对坐标（装夹基准须人工核对）",
        "example": example,
        "tilt": {
            "experiment": "Demo AF" if example else "",
            "points_um": [[0, 0], [500, 0], [500, 500], [0, 500]] if example else [],
            "z_um": 100 if example else "",
            "af_half_um": 5,
            "af_timeout_s": 30,
            "arrival_timeout_s": 15,
        },
        "units": {"length": "um", "angle": "urad", "time": "s"},
        "common_params": {
            "safe_z_um": 20.0 if example else "",
            "z_min_um": 0.0 if example else "",
            "z_max_um": 300.0 if example else "",
            "xy_tolerance_um": 1.0,
            "z_tolerance_um": 0.5,
            "capture_timeout_s": 180.0,
            "settle_s": 0.2,
            "upper_experiment": "Upper_mark" if example else "",
            "lower_experiment": "Lower_mark" if example else "",
            "use_autofocus": False,
            "max_abs_angle_urad": 10000.0,
            "max_rx_urad": 10000.0,
            "max_ry_urad": 10000.0,
            "max_rms_um": 10.0,
        },
        "step_tree": [
            {
                "step_id": "S0",
                "kind": "common",
                "label": "配方公共参数",
                "protected": True,
            },
            {
                "step_id": "S1",
                "kind": "tilt",
                "label": "共聚焦扫描物料角度",
                "protected": True,
            },
            {
                "step_id": "S2",
                "kind": "gate",
                "label": "角度结果判定",
                "protected": True,
            },
            {
                "step_id": "S3",
                "kind": "safe_transfer",
                "label": "安全转场",
                "protected": True,
            },
            {
                "step_id": "G1",
                "kind": "group",
                "label": "孔位采图循环 · B 区",
                "protected": True,
                "children": [
                    {
                        "step_id": "H01",
                        "kind": "hole",
                        "label": "H01 上下孔采图",
                        "protected": False,
                        "params": {
                            "x_um": 4000.0 if example else "",
                            "y_um": 1000.0 if example else "",
                            "upper_z_um": 100.0 if example else "",
                            "lower_z_um": 200.0 if example else "",
                        },
                    },
                ],
            },
            {
                "step_id": "S4",
                "kind": "return_z",
                "label": "返回安全 Z",
                "protected": True,
            },
            {
                "step_id": "S5",
                "kind": "archive",
                "label": "结果归档",
                "protected": True,
            },
        ],
        "external_dependencies": [],
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
        self.error = ""
        self.load()

    # ---- persistence -------------------------------------------------
    def load(self):
        import json

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                isinstance(raw, dict)
                and raw.get("kind") == "recipe_library"
                and raw.get("schema_version") == 1
                and isinstance(raw.get("recipes"), list)
            ):
                for recipe in raw["recipes"]:
                    if (
                        not isinstance(recipe, dict)
                        or not isinstance(recipe.get("recipe_id"), str)
                        or not isinstance(recipe.get("revisions"), list)
                    ):
                        raise ValueError("配方条目结构无效")
                    for version in recipe["revisions"]:
                        if (
                            not isinstance(version, dict)
                            or version.get("status") not in STATUS_NAMES
                            or not isinstance(version.get("body"), dict)
                            or not isinstance(version.get("revision"), int)
                        ):
                            raise ValueError("配方修订结构无效")
                self._doc = raw
                return
            raise ValueError("不支持的配方库格式")
        except (OSError, ValueError) as error:
            if self.path.exists():
                self.error = "配方库不可读，已保留原文件并禁止覆盖：" + str(error)
        self._doc = {"kind": "recipe_library", "schema_version": 1, "recipes": []}

    def save(self):
        if self.error:
            raise ValueError(self.error)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self._doc)

    # ---- queries -----------------------------------------------------
    def recipes(self):
        return list(self._doc["recipes"])

    def find(self, recipe_id):
        return next(
            (r for r in self._doc["recipes"] if r.get("recipe_id") == recipe_id), None
        )

    def latest(self, recipe_id):
        recipe = self.find(recipe_id)
        if not recipe or not recipe.get("revisions"):
            return None
        return max(recipe["revisions"], key=lambda v: v["revision"])

    def get(self, recipe_id, revision):
        recipe = self.find(recipe_id)
        if not recipe:
            return None
        return next(
            (v for v in recipe["revisions"] if v["revision"] == int(revision)), None
        )

    # ---- mutations ---------------------------------------------------
    def create(self, name, by, *, body=None, example=False):
        recipe_id = "RCP-" + uuid4().hex[:10].upper()
        body = body or default_body(name, example=example)
        revision = self._revision(1, body, by, "新建配方", None)
        self._doc["recipes"].append(
            {
                "recipe_id": recipe_id,
                "name": name or "未命名配方",
                "revisions": [revision],
            }
        )
        self.save()
        return recipe_id, 1

    @staticmethod
    def _revision(number, body, by, note, parent):
        frozen = deepcopy(body)
        return {
            "revision": number,
            "status": DRAFT,
            "created_at": utc_now(),
            "created_by": str(by or "")[:120],
            "change_note": str(note or "")[:500],
            "parent_revision": parent,
            "content_sha256": canonical_sha256(frozen),
            "body": frozen,
            "checks": None,
            "validation": {"state": "unvalidated"},
        }

    def new_revision(self, recipe_id, body, *, by, change_note, base_revision=None):
        """Always append. Editing a released revision produces a new draft."""
        recipe = self.find(recipe_id)
        if recipe is None:
            raise ValueError("配方不存在")
        latest = self.latest(recipe_id)
        parent = int(base_revision) if base_revision is not None else latest["revision"]
        revision = self._revision(latest["revision"] + 1, body, by, change_note, parent)
        recipe["revisions"].append(revision)
        self.save()
        return revision["revision"]

    # ---- verification ------------------------------------------------
    @staticmethod
    def check_body(body):
        from zen_recipe_compile import compile_body

        errors = []
        try:
            compile_body(body, simulated=bool(body.get("example")))
        except (ValueError, TypeError, KeyError) as error:
            errors.append(str(error))
        return {
            "kind": "recipe_check",
            "checked_at": utc_now(),
            "result": "failed" if errors else "passed",
            "items": [
                {
                    "key": "executable_plan",
                    "label": "参数、步骤依赖与运动范围",
                    "result": "failed" if errors else "passed",
                    "detail": "；".join(errors),
                }
            ],
            "warnings": ["正式运行前须核对所有 ZEN 实验内容指纹"],
            "content_sha256": canonical_sha256(body),
        }

    def save_draft(self, recipe_id, revision, body, *, by, note):
        version = self.get(recipe_id, revision)
        if not version or version["status"] != DRAFT:
            raise ValueError("仅草稿可以保存")
        if canonical_sha256(body) == version["content_sha256"]:
            version.update(
                created_by=str(by), change_note=str(note), updated_at=utc_now()
            )
            self.save()
            return
        version.update(
            body=deepcopy(body),
            content_sha256=canonical_sha256(body),
            checks=None,
            validation={"state": "unvalidated"},
            created_by=str(by),
            change_note=str(note),
            updated_at=utc_now(),
        )
        self.save()

    def record_trial(self, recipe_id, revision, run_path, *, by):
        import json

        version = self.get(recipe_id, revision)
        if not version or version["status"] != DRAFT:
            raise ValueError("仅草稿可记录验证")
        report = json.loads(
            (Path(run_path) / "sequence.json").read_text(encoding="utf-8")
        )
        identity = report.get("plan", {}).get("recipe_identity", {})
        if report.get("state") != "complete" or report.get("simulated") is not False:
            raise ValueError("现场验证须关联成功的真实设备任务")
        if (
            identity.get("content_sha256") != version["content_sha256"]
            or identity.get("recipe_id") != recipe_id
            or identity.get("revision") != revision
        ):
            raise ValueError("试运行与当前配方版本不一致")
        if identity.get("execution_scope"):
            raise ValueError("单步试运行不能放行完整配方")
        fingerprints = report.get("experiment_fingerprints", {})
        required_experiments = True
        if "program_steps" in version["body"]:
            from zen_steps import flatten
            required_experiments = any(n["kind"] in ("load_experiment", "measure_angle", "autofocus", "zstack", "capture_upper", "capture_lower") for _, n in flatten(version["body"]["program_steps"]))
        if required_experiments and not fingerprints:
            raise ValueError("试运行没有完整实验指纹")
        version["validation"] = {
            "state": "field_trial_passed",
            "run_path": str(run_path),
            "by": by,
            "at": utc_now(),
            "experiment_fingerprints": fingerprints,
        }
        self.save()

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
            raise ValueError(
                f"不允许的状态变更：{STATUS_NAMES.get(current)} → {target}"
            )

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
        if version["body"].get("example"):
            raise ValueError("演示配方不能放行到现场")
        if version.get("validation", {}).get("state") != "field_trial_passed":
            raise ValueError("须关联本版本成功的现场试运行，再由工程师确认放行")
        trial = deepcopy(version["validation"])
        version["status"] = RELEASED
        version["validation"] = {
            "trial": trial,
            "state": "qualified",
            "approved_by": str(by or "")[:120],
            "approved_at": utc_now(),
            "note": str(note or "")[:500],
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
                changes.append(
                    {"path": key, "before": left.get(key), "after": right.get(key)}
                )
        return {
            "recipe_id": recipe_id,
            "revision_a": int(revision_a),
            "revision_b": int(revision_b),
            "change_count": len(changes),
            "changes": changes,
            "compared_at": utc_now(),
        }
