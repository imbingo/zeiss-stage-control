"""Height reconstruction adapter for calibrated stacks, with explicit validity and µrad."""

from dataclasses import asdict, dataclass
from math import atan, ceil, isfinite
from pathlib import Path
import json
import shutil
import tempfile
import threading
import numpy as np
import tifffile
import cv2
from zen_topography_core import (
    _score_image_for_topography,
    _fit_height_maps_vectorized,
    _spatial_spike_mask,
)

ALGORITHM_SOURCE = "imbingo/z-stack-analysis@e52d1211d794d6daa920c3e4df364900e47706c5"
MODES = {
    "peak": "快速峰值Z图",
    "parabolic": "快速峰值Z+抛物线插值",
    "gaussian": "共聚焦亮度高斯拟合（逐点）",
    "clarity": "清晰度高斯拟合-峰值（逐点）",
}


class AnalysisCancelled(InterruptedError):
    pass


def checkpoint(stop):
    if stop is not None and stop.is_set():
        raise AnalysisCancelled("已取消高度图计算")


@dataclass(frozen=True)
class HeightParameters:
    method: str = "gaussian"
    grid_px: int = 8
    signal_fraction: float = 0.08
    r2_min: float = 0.0
    residual_sigma: float = 3.0
    spike_sigma: float = 0.0


def build_height_map(stack, parameters=HeightParameters(), *, stop=None, progress=None):
    p = parameters
    if p.method not in MODES or type(p.grid_px) is not int or p.grid_px < 1:
        raise ValueError("高度图算法或网格尺寸无效")
    if not all(
        isfinite(v)
        for v in (p.signal_fraction, p.r2_min, p.residual_sigma, p.spike_sigma)
    ) or not (
        0 <= p.signal_fraction <= 1
        and 0 <= p.r2_min <= 1
        and p.residual_sigma >= 0
        and p.spike_sigma >= 0
    ):
        raise ValueError("高度图质量参数无效")
    images, z = stack.algorithm_input()
    z = np.asarray(z, dtype="f8")
    if (
        images.ndim != 3
        or len(images) != len(z)
        or len(z) < 3
        or not np.isfinite(z).all()
    ):
        raise ValueError("至少需要3层完整灰度图像及逐层有限Z")
    if min(stack.scale_x_um, stack.scale_y_um) <= 0 or not all(
        isfinite(v) for v in (stack.scale_x_um, stack.scale_y_um)
    ):
        raise ValueError("缺少有效XY像素标定")
    order = np.argsort(z)
    z = z[order]
    dz = np.diff(z)
    if not (dz > 0).all():
        raise ValueError("Z必须无重复且严格单调")
    if p.method != "peak" and not np.allclose(dz, np.median(dz), rtol=1e-4, atol=1e-8):
        raise ValueError(
            "原算法亚层拟合要求等间距Z；当前数据不等距，请选择离散峰值或重新采集"
        )
    if p.method in ("peak", "parabolic") and p.r2_min > 0:
        raise ValueError("离散峰值/抛物线不提供R²，请将R²门限设为0")
    nz, h, w = images.shape
    ny, nx = ceil(h / p.grid_px), ceil(w / p.grid_px)
    if min(nx, ny) < 2:
        raise ValueError("网格过大，至少需要2×2采样网格")
    # Bound vectorized float64 curves, Gaussian temporaries, output maps and input.
    if images.nbytes + nz * nx * ny * 8 * 18 + nx * ny * 256 > 512 * 1024 * 1024:
        raise ValueError("计算工作内存估算超过512MiB，请增大网格尺寸")
    curves = np.empty((nz, ny, nx), dtype="f4")
    mip = None
    for i, src in enumerate(order):
        checkpoint(stop)
        image = images[src]
        mip = image.copy() if mip is None else np.maximum(mip, image)
        score = _score_image_for_topography(image, MODES[p.method])
        curves[i] = cv2.resize(score, (nx, ny), interpolation=cv2.INTER_AREA)
        if progress:
            progress(i + 1, nz)
    checkpoint(stop)
    dynamic = np.ptp(curves, axis=0)
    global_range = float(np.max(curves) - np.min(curves))
    valid = np.isfinite(curves).all(axis=0) & (
        dynamic > max(global_range * p.signal_fraction, 1e-9)
    )
    peak = np.argmax(curves, axis=0)
    boundary = (peak == 0) | (peak == nz - 1)
    valid &= ~boundary
    if p.method == "peak":
        height = np.where(valid, z[peak], np.nan)
        r2 = np.full((ny, nx), np.nan)
        confidence = np.where(valid, 0.6, np.nan)
    else:
        result = _fit_height_maps_vectorized(
            curves,
            z,
            valid,
            use_log=p.method in ("gaussian", "clarity"),
            polarity="peak",
            want_fused=False,
        )
        height = result["height"]
        r2 = result["r2"]
        confidence = result["confidence"]
    if p.r2_min > 0:
        valid &= np.isfinite(r2) & (r2 >= p.r2_min)
    valid &= np.isfinite(height) & (height >= z[0]) & (height <= z[-1])
    valid &= ~_spatial_spike_mask(height, valid, p.spike_sigma, 6)
    height = np.where(valid, height, np.nan)
    x = (np.arange(nx) + 0.5) * w / nx * stack.scale_x_um
    y = (np.arange(ny) + 0.5) * h / ny * stack.scale_y_um
    xx, yy = np.meshgrid(x, y)

    def fit(mask):
        if np.count_nonzero(mask) < 3:
            raise ValueError("有效高度点不足3个，请检查扫描范围、信号与质量门限")
        A = np.column_stack((xx[mask], yy[mask], np.ones(np.count_nonzero(mask))))
        coef, _, rank, _ = np.linalg.lstsq(A, height[mask], rcond=None)
        if rank < 3:
            raise ValueError("有效点共线，无法拟合物料平面")
        return coef, coef[0] * xx + coef[1] * yy + coef[2]

    inliers = valid.copy()
    for _ in range(8 if p.residual_sigma > 0 else 0):
        checkpoint(stop)
        _, plane = fit(inliers)
        res = height - plane
        rv = res[inliers]
        median = float(np.median(rv))
        mad = float(np.median(np.abs(rv - median)))
        sigma = 1.4826 * mad if mad > 1e-12 else float(np.std(rv))
        if sigma <= 1e-12:
            break
        candidate = valid & (np.abs(res - median) <= p.residual_sigma * sigma)
        if np.array_equal(candidate, inliers):
            break
        # A filter that removes geometry is a failure, not a hidden filter fallback.
        fit(candidate)
        inliers = candidate
    coef, plane = fit(inliers)
    residual = height - plane
    rv = residual[inliers]
    hv = height[inliers]
    stats = {
        "valid_count": int(np.count_nonzero(valid)),
        "plane_inlier_count": int(np.count_nonzero(inliers)),
        "total_count": int(nx * ny),
        "boundary_count": int(np.count_nonzero(boundary)),
        "rx_urad": atan(float(coef[1])) * 1e6,
        "ry_urad": -atan(float(coef[0])) * 1e6,
        "pv_um": float(np.ptp(rv)),
        "rms_um": float(np.sqrt(np.mean(rv**2))),
        "ttv_um": float(np.ptp(hv)),
        "z_min_um": float(np.min(hv)),
        "z_max_um": float(np.max(hv)),
        "coordinate_system": "图像局部左手系：X向右、Y向图像下方、Z向下；Rx=atan(dZ/dY)，Ry=-atan(dZ/dX)；未标定到载台坐标",
        "plane_coefficients": coef.tolist(),
    }
    checkpoint(stop)
    return dict(
        height_um=height,
        plane_um=plane,
        residual_um=residual,
        valid_mask=valid,
        inlier_mask=inliers,
        boundary_mask=boundary,
        r2=r2,
        confidence=confidence,
        x_um=x,
        y_um=y,
        mip=mip,
        statistics=stats,
        parameters=asdict(p),
        algorithm_source=ALGORITHM_SOURCE,
        source=stack.source,
        z_source=stack.z_source,
        metadata_sha256=stack.metadata_sha256,
        source_planes=stack.planes,
        z_um=z,
        simulation=stack.z_source == "simulation",
    )


def export_height_map(result, destination):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("结果目录已存在，拒绝覆盖")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=".height-", dir=destination.parent))
    try:
        for key in ("height_um", "plane_um", "residual_um", "r2", "confidence"):
            tifffile.imwrite(
                temp / (key + ".tif"),
                result[key].astype("f8"),
                photometric="minisblack",
                metadata={"unit": "µm" if key.endswith("_um") else "dimensionless"},
            )
        for key in ("valid_mask", "inlier_mask", "boundary_mask"):
            tifffile.imwrite(
                temp / (key + ".tif"),
                result[key].astype("u1"),
                photometric="minisblack",
            )
        tifffile.imwrite(
            temp / "mip.tif", result["mip"], photometric="minisblack", metadata=None
        )
        np.savez_compressed(
            temp / "height_data.npz",
            **{
                k: result[k]
                for k in (
                    "height_um",
                    "plane_um",
                    "residual_um",
                    "valid_mask",
                    "inlier_mask",
                    "boundary_mask",
                    "x_um",
                    "y_um",
                    "z_um",
                    "r2",
                    "confidence",
                )
            }
        )
        report = {k: v for k, v in result.items() if not isinstance(v, np.ndarray)}
        report.update(
            schema_version=1,
            height_map_computed=True,
            complete=True,
            missing_value="NaN",
            height_unit="µm",
            angle_unit="µrad",
            height_image_axes="YX",
        )
        (temp / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temp.rename(destination)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return destination
