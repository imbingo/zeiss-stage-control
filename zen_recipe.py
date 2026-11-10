"""V2 recipe import using explicit SI units and ordered XY."""

import csv
import json
from math import isfinite

from zen_workflow import Recipe


def load_points(path):
    """Read ordered V2 points with explicit metre or micrometre headers."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers):
            raise ValueError("点位 CSV 列名重复")
        pairs = [
            (x, y, scale) for x, y, scale in
            (("x_m", "y_m", 1.0), ("x_um", "y_um", 1e-6))
            if x in headers and y in headers
        ]
        if len(pairs) != 1:
            raise ValueError("点位 CSV 须包含 x_m,y_m 或 x_um,y_um")
        x, y, scale = pairs[0]
        points = []
        for row in reader:
            try:
                point = float(row[x]) * scale, float(row[y]) * scale
                if None in row or not all(map(isfinite, point)):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError(f"点位 CSV 第 {reader.line_num} 行坐标无效") from exc
            points.append(point)
    if not points:
        raise ValueError("点位 CSV 无记录")
    return tuple(points)


def load_recipe(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if data["schema_version"] != 1 or data["coordinate_unit"] != "m":
        raise ValueError("需要版本 1、坐标单位为 m 的配方")
    names = [data[key] for key in ("recipe_name", "experiment_name", "sign_convention")]
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("配方、实验和符号约定不能为空")
    targets = tuple(tuple(point) for point in data["point_order"])
    if any(len(p) != 2 or any(type(v) not in (float, int) or not isfinite(v) for v in p)
           for p in targets):
        raise ValueError("点位需为有序的有限 XY 坐标")
    values = [data[key] for key in ("xy_tolerance_m", "af_timeout_s", "arrival_timeout_s")]
    if any(type(v) not in (int, float) or not isfinite(v) or v <= 0 for v in values):
        raise ValueError("容差和超时须为有限正数")
    return Recipe(names[0], names[1], targets, *values, names[2])
