"""Pure path construction and four-point AF prediction, all coordinates in µm."""

from math import isfinite
import numpy as np


def generate_path(x0, y0, x1, y1, *, kind="grid", rows=4, columns=5, count=20):
    values = tuple(float(v) for v in (x0, y0, x1, y1))
    if not all(isfinite(v) for v in values):
        raise ValueError("坐标必须为有限数字（µm）")
    x0, y0, x1, y1 = values
    if kind == "line":
        if not 2 <= count <= 1000 or (x0, y0) == (x1, y1):
            raise ValueError("直线需不同起终点，点数为 2–1000；共线点不能用于平面测角")
        return [
            [x0 + (x1 - x0) * i / (count - 1), y0 + (y1 - y0) * i / (count - 1), ""]
            for i in range(count)
        ]
    if kind != "grid" or not (
        2 <= rows <= 1000 and 2 <= columns <= 1000 and rows * columns <= 1000
    ):
        raise ValueError("网格行列均至少为 2，总点数不得超过 1000")
    if x0 == x1 or y0 == y1:
        raise ValueError("矩形必须具有非零宽度和高度")
    return [
        [x0 + (x1 - x0) * c / (columns - 1), y0 + (y1 - y0) * r / (rows - 1), ""]
        for r in range(rows)
        for c in (range(columns) if r % 2 == 0 else range(columns - 1, -1, -1))
    ]


def parse_points(text):
    """Excel TSV or CSV; optional Z, explicit µm, no silent header/row skipping."""
    result = []
    for line_number, line in enumerate(text.strip().splitlines()):
        cells = line.split("\t") if "\t" in line else line.split(",")
        if line_number == 0 and [c.strip() for c in cells] in (
            ["X_um", "Y_um"],
            ["X_um", "Y_um", "Z_um"],
        ):
            continue
        if len(cells) not in (2, 3) or not all(c.strip() for c in cells[:2]):
            raise ValueError("每行需两列 X/Y 或三列 X/Y/Z，单位 µm；不要包含表头")
        row = [float(c.strip()) if c.strip() else "" for c in cells]
        if any(v != "" and not isfinite(v) for v in row):
            raise ValueError("点位必须为有限数字")
        result.append(row + ([""] if len(row) == 2 else []))
    if not 1 <= len(result) <= 1000:
        raise ValueError("请粘贴 1–1000 行点位")
    if len({tuple(r[:2]) for r in result}) != len(result):
        raise ValueError("存在重复 XY 点位")
    return result


def validate_prescan(points, indices):
    if (
        not isinstance(indices, list)
        or len(indices) != 4
        or any(type(i) is not int for i in indices)
    ):
        raise ValueError("请在路径中勾选恰好四个预扫描点")
    if len(set(indices)) != 4 or any(i < 0 or i >= len(points) for i in indices):
        raise ValueError("预扫描点编号重复或超出路径范围")
    xy = [(float(points[i][0]), float(points[i][1])) for i in indices]
    if len(set(xy)) != 4:
        raise ValueError("预扫描四点的 XY 不得重复")
    from zen_fit import _design

    _design(xy)


def fit_prediction(samples, max_residual_um):
    """Centred/scaled least squares; model deliberately separate from final metrology."""
    if len(samples) != 4 or not isfinite(max_residual_um) or max_residual_um <= 0:
        raise ValueError("预测平面需要四个实测点及有效的残差上限")
    xyz = np.asarray(samples, dtype=float)
    if xyz.shape != (4, 3) or not np.isfinite(xyz).all():
        raise ValueError("预扫描 XYZ 数据无效")
    origin = xyz[:, :2].mean(axis=0)
    scale = np.max(np.linalg.norm(xyz[:, :2] - origin, axis=1))
    from zen_fit import _design

    _design([tuple(row[:2]) for row in xyz])
    design = np.column_stack(((xyz[:, :2] - origin) / scale, np.ones(4)))
    coefficients, _, rank, _ = np.linalg.lstsq(design, xyz[:, 2], rcond=None)
    residuals = xyz[:, 2] - design @ coefficients
    if rank != 3 or np.max(np.abs(residuals)) > max_residual_um:
        raise ValueError("四点预测平面残差超限，请检查点位、装夹或增大经验证的残差上限")
    return dict(
        samples_xyz_um=xyz.tolist(),
        origin_um=origin.tolist(),
        scale_um=float(scale),
        coefficients=coefficients.tolist(),
        residuals_um=residuals.tolist(),
        max_abs_residual_um=float(np.max(np.abs(residuals))),
    )


def predict_z(model, x, y):
    a, b, c = model["coefficients"]
    return (
        a * (x - model["origin_um"][0]) / model["scale_um"]
        + b * (y - model["origin_um"][1]) / model["scale_um"]
        + c
    )


def prediction_order(count, indices, enabled=True):
    """Shared original-row ordering for angle and multi-surface scans."""
    prescan = sorted(indices) if enabled else []
    return prescan, prescan + [i for i in range(count) if i not in prescan]


def checked_prediction(samples, remaining_xy_um, half_um, max_residual_um, target):
    model = fit_prediction(samples, max_residual_um)
    for x, y in remaining_xy_um:
        z = predict_z(model, float(x), float(y))
        target((z-half_um)*1e-6)
        target((z+half_um)*1e-6)
    return model
