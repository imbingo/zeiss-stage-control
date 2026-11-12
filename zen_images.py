"""Lossless conversion of complete ZEN frames to calibrated TIFF files."""

from dataclasses import dataclass
from fractions import Fraction
import json
from math import isfinite, isclose
import os
from pathlib import Path


@dataclass(frozen=True)
class RawFrame:
    width: int
    height: int
    pixel_type: int  # Official PixelType: GRAY8=1, GRAY16=2, BGR24=4, BGR48=5.
    data: bytes
    scale_x_m: float
    scale_y_m: float
    xyz_m: tuple
    experiment_id: str = ""
    frame_number: int = 0
    origin: tuple = (0, 0)
    channel: int = 0

    def array(self):
        import numpy as np

        formats = {1: ("u1", 1), 2: ("<u2", 1), 4: ("u1", 3), 5: ("<u2", 3)}
        if self.pixel_type not in formats:
            raise ValueError("不支持的原始像素类型，未保存图像")
        if (type(self.width) is not int or type(self.height) is not int
                or self.width <= 0 or self.height <= 0):
            raise ValueError("图像尺寸无效")
        dtype, channels = formats[self.pixel_type]
        expected = self.width * self.height * np.dtype(dtype).itemsize * channels
        if len(self.data) != expected or expected > 256 * 1024 * 1024:
            raise ValueError("图像字节数与尺寸不符，或图像超过 256 MiB")
        if not all(isfinite(v) and v > 0 for v in (self.scale_x_m, self.scale_y_m)):
            raise ValueError("图像缺少有效的像素标定")
        if len(self.xyz_m) != 3 or not all(map(isfinite, self.xyz_m)):
            raise ValueError("图像缺少有效的 XYZ 拍摄位置")
        shape = (self.height, self.width) if channels == 1 else (self.height, self.width, channels)
        result = np.frombuffer(self.data, dtype=dtype).reshape(shape)
        # TIFF uses RGB, while the ZEN stream uses BGR. Only channel order changes.
        return np.ascontiguousarray(result if channels == 1 else result[..., ::-1])

    def matches_geometry(self, other):
        return ((self.width, self.height, self.pixel_type, self.origin, self.channel)
                == (other.width, other.height, other.pixel_type, other.origin, other.channel)
                and isclose(self.scale_x_m, other.scale_x_m, rel_tol=1e-6, abs_tol=0)
                and isclose(self.scale_y_m, other.scale_y_m, rel_tol=1e-6, abs_tol=0))

    def metadata(self):
        return {
            "width": self.width, "height": self.height, "source_pixel_type": self.pixel_type,
            "scale_x_m_per_pixel": self.scale_x_m, "scale_y_m_per_pixel": self.scale_y_m,
            "frame_xyz_m": self.xyz_m, "experiment_id": self.experiment_id,
            "frame_number": self.frame_number, "roi_origin_pixels": self.origin,
            "channel_index": self.channel,
        }


def write_tiff(path: Path, frame: RawFrame, metadata):
    import tifffile

    pixels = frame.array()
    if path.exists():
        raise FileExistsError("图像已存在，拒绝覆盖")
    def rational_resolution(scale):
        value = 1 / (scale * 100)
        if not isfinite(value) or not 0 < value <= 2**32-1:
            raise ValueError("像素标定超出 TIFF 分辨率标签的有效范围")
        fraction = Fraction(value).limit_denominator(max(1, min(1_000_000, int((2**32-1)/value))))
        if fraction.numerator == 0 or not isclose(float(fraction), value, rel_tol=1e-9):
            raise ValueError("无法精确保存 TIFF 像素标定")
        return fraction.numerator, fraction.denominator
    resolution = (rational_resolution(frame.scale_x_m), rational_resolution(frame.scale_y_m))
    temporary = path.with_suffix(".tif.tmp")
    try:
        with temporary.open("xb") as stream:
            tifffile.imwrite(
                stream, pixels, photometric="rgb" if pixels.ndim == 3 else "minisblack",
                compression=None, metadata=None,
                description=json.dumps({**frame.metadata(), **metadata}, ensure_ascii=False, allow_nan=False),
                resolution=resolution,
                resolutionunit="CENTIMETER",
            )
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
