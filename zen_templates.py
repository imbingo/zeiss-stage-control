"""User flow templates, independent of immutable released recipe revisions."""

from copy import deepcopy
from uuid import uuid4
import json
from pathlib import Path
from zen_records import write_json, utc_now
from zen_steps import flatten


class TemplateStore:
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return []
        doc = json.loads(self.path.read_text(encoding="utf-8"))
        if doc.get("schema_version") != 1 or not isinstance(doc.get("templates"), list):
            raise ValueError("模板库格式无效，未覆盖原文件")
        return doc["templates"]

    def save(self, name, body):
        if not name.strip() or not body.get("program_steps"):
            raise ValueError("模板名称和执行步骤不能为空")
        rows = self.load()
        entry = {
            "id": uuid4().hex,
            "name": name.strip(),
            "saved_at": utc_now(),
            "coordinate_system": body.get("coordinate_system"),
            "example": bool(body.get("example")),
            "steps": deepcopy(body["program_steps"]),
            "common_params": deepcopy(body.get("common_params", {})),
        }
        rows.append(entry)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, {"schema_version": 1, "templates": rows})
        return entry

    @staticmethod
    def instantiate(entry):
        nodes = deepcopy(entry["steps"])
        mapping = {
            n["step_id"]: "S" + uuid4().hex[:7].upper() for _, n in flatten(nodes)
        }
        for _, n in flatten(nodes):
            n["step_id"] = mapping[n["step_id"]]
            for key, value in n.get("params", {}).items():
                if isinstance(value, str) and value.startswith("@"):
                    old, sep, field = value[1:].partition(".")
                    if old in mapping:
                        n["params"][key] = "@" + mapping[old] + sep + field
                elif key == "source" and value in mapping:
                    n["params"][key] = mapping[value]
        return nodes
