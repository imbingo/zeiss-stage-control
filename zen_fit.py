"""Geometry checks and a provisional convention for simulated measurements."""

from math import atan, fsum, hypot, isfinite, sqrt


DEMO_CONVENTION = "演示约定：Rx=atan(dZ/dY)，Ry=-atan(dZ/dX)，角度单位 µrad"
FIELD_CONVENTION = "联调约定：Rx=atan(dZ/dY)，Ry=-atan(dZ/dX)，角度单位 µrad"


def _design(points):
    if len(points) < 3 or any(len(p) != 2 or not all(map(isfinite, p)) for p in points):
        raise ValueError("至少需要三个有限 XY 点")
    n = len(points)
    x0 = fsum(p[0] / n for p in points)
    y0 = fsum(p[1] / n for p in points)
    centered = [(x - x0, y - y0) for x, y in points]
    scale = max(hypot(x, y) for x, y in centered)
    if not isfinite(scale) or scale == 0:
        raise ValueError("XY 点位没有有效跨度")
    xy = [(x / scale, y / scale) for x, y in centered]
    xx = fsum(x * x for x, y in xy)
    yy = fsum(y * y for x, y in xy)
    cross = fsum(x * y for x, y in xy)
    determinant = xx * yy - cross * cross
    if determinant <= 1e-12 * (xx + yy) ** 2:
        raise ValueError("XY 点位共线或过于接近共线")
    return xy, scale, xx, yy, cross, determinant


def validate_recipe(recipe):
    if not recipe.name.strip() or not recipe.experiment.strip():
        raise ValueError("配方与实验名称不能为空")
    for value in (recipe.tolerance_m, recipe.af_timeout, recipe.arrival_timeout):
        if not isfinite(value) or value <= 0:
            raise ValueError("容差和超时必须为有限正数")
    _design(recipe.targets)


def fit_plane(rows, *, sign_convention):
    if any(r.status != "ok" or r.before_af is None or r.z is None for r in rows):
        raise ValueError("采集记录不完整")
    xy, scale, xx, yy, cross, determinant = _design([r.before_af for r in rows])
    z0 = fsum(r.z / len(rows) for r in rows)
    z = [r.z - z0 for r in rows]
    xz = fsum(x * h for (x, y), h in zip(xy, z))
    yz = fsum(y * h for (x, y), h in zip(xy, z))
    a = (yy * xz - cross * yz) / determinant
    b = (xx * yz - cross * xz) / determinant
    residuals = [h - a * x - b * y for (x, y), h in zip(xy, z)]
    return {
        "rx_urad": atan(b / scale) * 1e6,
        "ry_urad": -atan(a / scale) * 1e6,
        "angle_unit": "urad",
        "rms_m": sqrt(fsum(r * r for r in residuals) / len(rows)),
        "pv_m": max(residuals) - min(residuals),
        "residuals_m": residuals,
        "sign_convention": sign_convention,
    }


def fit_demo(rows):
    return fit_plane(rows, sign_convention=DEMO_CONVENTION)


def fit_field(rows):
    return fit_plane(rows, sign_convention=FIELD_CONVENTION)
