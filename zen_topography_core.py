"""Numerical functions adapted from imbingo/z-stack-analysis.
Source: e52d1211d794d6daa920c3e4df364900e47706c5, zstack_focus_analyzer.py.
Retained numerical bodies; GUI/file loading excluded. See docs/height-map/README.md.
"""

from __future__ import annotations
import numpy as np
import cv2


def is_zen_mip_mode(mode):
    return mode in {"ZEN兼容：最大强度投影 MIP", "快速最大亮度投影"}


def normalize_to_255(img: np.ndarray) -> np.ndarray:
    """把单张图归一化到 0-255，用于纹理/频域焦度和显示。"""
    img = img.astype(np.float32)
    lo, hi = float(np.min(img)), float(np.max(img))
    if hi > lo:
        return (img - lo) / (hi - lo) * 255.0
    return np.zeros_like(img, dtype=np.float32)


def _score_image_for_topography(raw_crop: np.ndarray, mode: str) -> np.ndarray:
    """把单层图像转换为每个 XY 点/网格的 Z 向评分图。"""
    # 亮度投影 / 快速投影：直接用原始灰度做 Z 向亮度评分。
    # 共聚焦黑背景下，焦面对应亮度峰值，因此所有“快速投影/亮度”类算法都按亮度处理，
    # 而不是清晰度高频能量（修正历史上快速模式名义亮度、实际跑清晰度的问题）。
    if (
        is_zen_mip_mode(mode)
        or mode.startswith("共聚焦亮度")
        or mode.startswith("快速")
        or "亮度" in mode
    ):
        return raw_crop.astype(np.float32)

    # 清晰度高度图：用局部高频能量图，而不是整块 variance。再对网格做面积平均。
    img = normalize_to_255(raw_crop).astype(np.float32)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    lap = cv2.Laplacian(img, cv2.CV_32F, ksize=3)
    gx = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=3)
    focus_energy = 0.45 * (lap * lap) + 0.55 * (gx * gx + gy * gy)
    return focus_energy.astype(np.float32)


def _fit_height_maps_vectorized(
    curves: np.ndarray,
    z_values: np.ndarray,
    signal_mask: np.ndarray,
    use_log: bool,
    polarity: str,
    want_fused: bool,
) -> Dict[str, np.ndarray]:
    """全向量化逐像素亚层峰/谷定位，取代逐像素 scipy curve_fit。

    对每个像素的 Z 向曲线，在峰值层附近取 3 点做二次（抛物线）顶点插值：
    - use_log=True 时拟合 ln(曲线-基线)，等价于高斯峰，可同时得到 σ；这与代码里
      已有的 “log-Gaussian” 退化算法同源，但一次性对整幅网格向量化，速度快几个数量级。
    - use_log=False 时直接抛物线峰，仅定位亚层 Z（无 σ）。
    polarity='valley' 时对 -曲线 求峰。want_fused 仅在亮度峰值下有意义，输出高斯加权
    融合灰度(EDF)与峰值灰度。signal_mask=False 的像素全部置 NaN（背景/弱信号不拟合）。
    返回 dict：height/sigma/r2/fused_peak/fused_weighted/confidence，形状均为 (ny, nx)。
    """
    Zn, ny, nx = curves.shape
    order = np.argsort(np.asarray(z_values, dtype=np.float64))
    z = np.asarray(z_values, dtype=np.float64)[order]
    C = np.asarray(curves, dtype=np.float64)[order]  # (Z, ny, nx) 按 z 升序
    dz = float(np.median(np.diff(z))) if Zn > 1 else 1.0
    if not np.isfinite(dz) or dz <= 0:
        dz = 1.0

    def nanmap():
        return np.full((ny, nx), np.nan, dtype=np.float64)

    def take(a3, idx2):
        return np.take_along_axis(a3, idx2[None, :, :], axis=0)[0]

    S = C if polarity == "peak" else -C  # 统一成找“峰”
    base = np.nanpercentile(S, 10, axis=0)  # 稳健基线 (ny, nx)
    pos = np.maximum(S - base[None, :, :], 1e-12)
    yv = np.log(pos) if use_log else pos  # 抛物线拟合对象

    pk = np.argmax(S, axis=0)  # 峰层索引 (ny, nx)
    pkm = np.clip(pk - 1, 0, Zn - 1)
    pkp = np.clip(pk + 1, 0, Zn - 1)
    ym = take(yv, pkm)
    y0 = take(yv, pk)
    yp = take(yv, pkp)
    z0 = z[pk]

    denom = ym - 2.0 * y0 + yp
    interior = (pk > 0) & (pk < Zn - 1)
    concave = denom < -1e-12
    good = interior & concave & signal_mask

    delta = np.zeros((ny, nx), dtype=np.float64)
    np.divide(0.5 * (ym - yp), denom, out=delta, where=good)
    delta = np.clip(delta, -1.0, 1.0)
    mu = np.where(good, z0 + delta * dz, z0)
    vertex = y0 + 0.5 * (yp - ym) * delta + 0.5 * denom * delta * delta  # 顶点处 yv 值

    sigma_m = nanmap()
    if use_log:
        with np.errstate(invalid="ignore", divide="ignore"):
            sig = dz / np.sqrt(-denom)
        sigma_m = np.where(good & np.isfinite(sig) & (sig > 0), sig, np.nan)

    height = np.where(signal_mask, mu, np.nan)
    peak_gray = take(C, pk)  # 峰层灰度（C 域）

    fused_peak = nanmap()
    fused_weighted = nanmap()
    if want_fused:
        amp = np.exp(vertex) if use_log else vertex  # 峰值高出基线的量
        peak_val = base + amp  # 顶点处 S(=C) 值
        fused_peak = np.where(signal_mask, np.where(good, peak_val, peak_gray), np.nan)
        if use_log:
            # 高斯加权融合(EDF)：以各像素 μ/σ 为权重沿 Z 加权平均原始灰度。
            sig_w = np.where(np.isfinite(sigma_m) & (sigma_m > 1e-9), sigma_m, dz)
            zc = z[:, None, None]
            w = np.exp(-((zc - mu[None, :, :]) ** 2) / (2.0 * sig_w[None, :, :] ** 2))
            sw = np.sum(w, axis=0)
            fw = np.sum(w * C, axis=0) / np.where(sw > 1e-12, sw, np.nan)
            fw = np.where(np.isfinite(fw), fw, peak_gray)
            fused_weighted = np.where(signal_mask, fw, np.nan)
        else:
            # 非高斯（抛物线/峰值）模式：融合灰度退化为峰层灰度。
            fused_weighted = np.where(signal_mask, peak_gray, np.nan)

    r2_m = nanmap()
    conf = nanmap()
    if use_log:
        amp_eff = np.where(good, np.exp(vertex), np.nan)
        sig_eff = np.where(np.isfinite(sigma_m) & (sigma_m > 1e-9), sigma_m, np.nan)
        zc = z[:, None, None]
        with np.errstate(invalid="ignore", divide="ignore"):
            fitted = base[None, :, :] + amp_eff[None, :, :] * np.exp(
                -((zc - mu[None, :, :]) ** 2) / (2.0 * sig_eff[None, :, :] ** 2)
            )
            ss_res = np.sum((S - fitted) ** 2, axis=0)
            mean_s = np.mean(S, axis=0)
            ss_tot = np.sum((S - mean_s[None, :, :]) ** 2, axis=0)
            r2 = 1.0 - ss_res / np.where(ss_tot > 1e-12, ss_tot, np.nan)
        r2_m = np.where(good, r2, np.nan)
        conf = np.clip(np.where(np.isfinite(r2_m), r2_m, 0.0), 0.0, 1.0)
        conf = np.where(signal_mask, conf, np.nan)
    else:
        conf = np.where(signal_mask & np.isfinite(height), 0.6, np.nan)

    return {
        "height": height,
        "sigma": sigma_m,
        "r2": r2_m,
        "fused_peak": fused_peak,
        "fused_weighted": fused_weighted,
        "confidence": conf,
    }


def _spatial_spike_mask(
    height: np.ndarray, valid_mask: np.ndarray, k_sigma: float, max_cluster: int
) -> np.ndarray:
    """识别“孤立噪声尖刺”：相对局部中值显著偏离、且连通域很小的点。

    孤立坏点(噪声)是小连通域；mark/孔等真实特征是大连通域，不会被当成尖刺。
    返回需要剔除的尖刺布尔掩码。k_sigma<=0 或 max_cluster<=0 时不剔除。
    """
    finite = valid_mask & np.isfinite(height)
    if k_sigma <= 0 or max_cluster <= 0 or int(np.count_nonzero(finite)) < 9:
        return np.zeros_like(finite, dtype=bool)
    try:
        from scipy.ndimage import median_filter, label
    except Exception:
        return np.zeros_like(finite, dtype=bool)
    fillv = float(np.nanmedian(height[finite]))
    hf = np.where(finite, height, fillv).astype(np.float64)
    med = median_filter(hf, size=3, mode="nearest")
    dev = np.abs(height - med)
    dv = dev[finite]
    mdev = float(np.nanmedian(dv))
    robust = 1.4826 * mdev if mdev > 1e-12 else float(np.nanstd(dv))
    if not np.isfinite(robust) or robust <= 1e-12:
        return np.zeros_like(finite, dtype=bool)
    outlier = finite & (dev > k_sigma * robust)
    if not np.any(outlier):
        return np.zeros_like(finite, dtype=bool)
    lbl, n = label(outlier)
    if n <= 0:
        return np.zeros_like(finite, dtype=bool)
    sizes = np.bincount(lbl.ravel())
    spike = np.zeros_like(outlier, dtype=bool)
    for comp in range(1, n + 1):
        if sizes[comp] <= max_cluster:  # 只剔除小连通域(孤立尖刺)，大连通域(特征)保留
            spike |= lbl == comp
    return spike
