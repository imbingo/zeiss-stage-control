"""Offline, calibrated Z-stack input. Never infer a plane Z from acquisition settings.

Supported: OME-XML with explicit Plane PositionZ and TIFF mappings, embedded
OME-TIFF, and the versioned ZenSurfaceStack manifest written by this application.
Z values are acquisition metadata, not independent encoder measurements.
"""

from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET

import numpy as np
import tifffile

MAX_BYTES = 512 * 1024 * 1024
MAX_XML_BYTES = 16 * 1024 * 1024


def _xml(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("XML 超过 16 MiB 上限")
    # Decode before checking: also rejects DTD/entity declarations in UTF-16/32.
    if isinstance(data, bytes):
        if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
            data = data.decode("utf-32")
        elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
            data = data.decode("utf-16")
        else:
            data = data.decode("utf-8-sig")
    if "\x00" in data or "<!DOCTYPE" in data.upper() or "<!ENTITY" in data.upper():
        raise ValueError("XML 不允许 DTD 或实体声明")
    return ET.fromstring(data)


def _tag(e):
    return e.tag.rsplit("}", 1)[-1]


def _children(e, name):
    return [c for c in e if _tag(c) == name]


def _um(value, unit):
    units = {"m": 1e6, "mm": 1e3, "µm": 1.0, "μm": 1.0, "um": 1.0, "nm": 1e-3}
    if unit not in units:
        raise ValueError(f"缺少或不支持的长度单位：{unit!r}")
    result = float(value) * units[unit]
    if not isfinite(result):
        raise ValueError("Z 或像素标定不是有限数值")
    return result


def _relative_file(root, name):
    # A manifest may only reference its own directory tree, on every OS.
    if not name or "\\" in name or ":" in name:
        raise ValueError("图像引用必须是 XML 目录内的相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"图像不存在或不在 XML 目录内：{name}")
    return path


@dataclass
class StackData:
    pixels: np.ndarray  # ZYX, original integer pixel type
    z_um: np.ndarray
    scale_x_um: float
    scale_y_um: float
    planes: list
    source: str
    z_source: str
    metadata_sha256: str

    def algorithm_input(self):
        """The existing reconstruction algorithm can consume these exact two arrays."""
        return self.pixels, self.z_um.copy()


def _read_planes(specs, size, scale, source, z_source, digest):
    if not 2 <= len(specs) <= 4096:
        raise ValueError("需要 2 至 4096 个完整 Z 平面")
    z = np.array([s["z_um"] for s in specs], dtype=np.float64)
    dz = np.diff(z)
    if not np.isfinite(z).all() or not ((dz > 0).all() or (dz < 0).all()):
        raise ValueError("逐层 Z 必须严格递增或递减，不能重复或混序")
    pixels, dtype, seen, hashes, count = [], None, set(), {}, 0
    for spec in specs:
        path = Path(spec["file"])
        key = (str(path), spec["page"])
        if key in seen:
            raise ValueError("同一图像页被重复映射")
        seen.add(key)
        if path not in hashes:
            h = sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(block)
            hashes[path] = h.hexdigest()
        with tifffile.TiffFile(path) as tif:
            if not 0 <= spec["page"] < len(tif.pages):
                raise ValueError("TIFF 图像页缺失")
            page = tif.pages[spec["page"]]
            if len(page.shape) != 2 or tuple(page.shape) != size:
                raise ValueError("只支持同尺寸的二维灰度平面，拒绝颜色或隐式展平")
            if page.dtype.kind != "u" or page.dtype.itemsize not in (1, 2):
                raise ValueError("仅支持无损 uint8/uint16 灰度数据")
            if dtype is not None and page.dtype != dtype:
                raise ValueError("各层位深不一致")
            dtype = page.dtype
            count += int(np.prod(size)) * dtype.itemsize
            # Reserve room for the final contiguous array as well as individual planes.
            if count * 2 > MAX_BYTES:
                raise ValueError("堆栈工作内存超过 512 MiB 上限")
            pixels.append(page.asarray())
        spec["sha256"] = hashes[path]
    return StackData(np.stack(pixels), z, *scale, specs, str(source), z_source, digest)


def load_stack(path, *, channel=0, timepoint=0, image_index=0):
    """Load actual per-plane metadata. No first+step fallback, no automatic acquisition."""
    path = Path(path).resolve()
    if any(type(v) is not int or v < 0 for v in (channel, timepoint, image_index)):
        raise ValueError("图像、通道、时间索引必须是非负整数")
    embedded = path.suffix.lower() in (".tif", ".tiff")
    if embedded:
        with tifffile.TiffFile(path) as tif:
            data = tif.ome_metadata
        if not data:
            raise ValueError("TIFF 没有 OME 元数据；请选择配套 XML")
        data = data.encode("utf-8")
    else:
        if path.stat().st_size > MAX_XML_BYTES:
            raise ValueError("XML 超过 16 MiB 上限")
        data = path.read_bytes()
    root = _xml(data)
    digest = sha256(data).hexdigest()
    specs = []
    if _tag(root) == "ZenSurfaceStack":
        if root.get("version") != "1" or root.get("complete") != "true":
            raise ValueError("不是受支持的完整堆栈清单")
        if channel or timepoint or image_index:
            raise ValueError("工作站清单只包含已选择的单通道、单时刻数据")
        size = (int(root.attrib["height"]), int(root.attrib["width"]))
        scale = tuple(
            _um(root.attrib[k], root.get("unit")) for k in ("scale_x", "scale_y")
        )
        planes = _children(root, "Plane")
        if int(root.attrib["count"]) != len(planes):
            raise ValueError("清单层数不完整")
        for index, p in enumerate(planes):
            if int(p.attrib["index"]) != index:
                raise ValueError("清单层号不连续或重复")
            specs.append(
                dict(
                    index=index,
                    file=str(_relative_file(path.parent, p.get("file"))),
                    page=int(p.get("page", "0")),
                    z_um=_um(p.attrib["z"], root.get("unit")),
                )
            )
        z_source = root.get("z_source", "metadata")
    elif _tag(root) == "OME":
        images = _children(root, "Image")
        if image_index >= len(images):
            raise ValueError("所选 Image 不存在")
        blocks = _children(images[image_index], "Pixels")
        if len(blocks) != 1:
            raise ValueError("缺少唯一 Pixels 定义")
        p = blocks[0]
        sizes = {a: int(p.attrib["Size" + a]) for a in "XYZCT"}
        if sizes["C"] <= channel or sizes["T"] <= timepoint:
            raise ValueError("所选通道或时间点不存在")
        if not 2 <= sizes["Z"] <= 4096 or min(sizes.values()) < 1:
            raise ValueError("OME 维度无效或 Z 层数超限")
        size = (sizes["Y"], sizes["X"])
        scale = tuple(
            _um(p.attrib["PhysicalSize" + a], p.get("PhysicalSize" + a + "Unit", "µm"))
            for a in "XY"
        )
        if any(
            int(c.get("SamplesPerPixel", "1")) != 1 for c in _children(p, "Channel")
        ):
            raise ValueError("不支持 RGB/交错采样通道")
        order = p.get("DimensionOrder", "")
        if not order.startswith("XY") or set(order) != set("XYZCT") or len(order) != 5:
            raise ValueError("OME DimensionOrder 无效")
        axes = order[2:]
        positions = {}
        for plane in _children(p, "Plane"):
            c, t, z = (int(plane.attrib["The" + a]) for a in "CTZ")
            if not (
                0 <= c < sizes["C"] and 0 <= t < sizes["T"] and 0 <= z < sizes["Z"]
            ):
                raise ValueError("OME Plane 索引越界")
            if c != channel or t != timepoint:
                continue
            if z in positions:
                raise ValueError("OME Plane 层号重复")
            if "PositionZ" not in plane.attrib:
                raise ValueError("OME 缺少逐层 PositionZ，拒绝用步进补值")
            positions[z] = _um(
                plane.attrib["PositionZ"], plane.get("PositionZUnit", "µm")
            )
        mapping = {}
        for td in _children(p, "TiffData"):
            start = {a: int(td.get("First" + a, "0")) for a in axes}
            if any(not 0 <= start[a] < sizes[a] for a in axes):
                raise ValueError("OME TiffData 首层索引越界")
            uuids = _children(td, "UUID")
            if len(uuids) > 1:
                raise ValueError("OME 图像文件引用不唯一")
            name = uuids[0].get("FileName") if uuids else None
            file = (
                _relative_file(path.parent, name)
                if name
                else (path if embedded else None)
            )
            if file is None:
                raise ValueError("外置 OME-XML 缺少 TIFF FileName")
            with tifffile.TiffFile(file) as tif:
                n_pages = len(tif.pages)
            n = int(td.get("PlaneCount", "1" if "IFD" in td.attrib else str(n_pages)))
            if n < 1 or n > sizes["Z"] * sizes["C"] * sizes["T"]:
                raise ValueError("OME PlaneCount 无效")
            if int(td.get("IFD", "0")) < 0 or int(td.get("IFD", "0")) + n > n_pages:
                raise ValueError("TIFF 图像页缺失")
            if n > 262144:
                raise ValueError("OME 映射数量超出支持上限")
            offset, stride = 0, 1
            for a in axes:
                offset += start[a] * stride
                stride *= sizes[a]
            for j in range(n):
                remaining = offset + j
                coords = {}
                for a in axes:
                    coords[a] = remaining % sizes[a]
                    remaining //= sizes[a]
                if remaining:
                    raise ValueError("OME TiffData 映射超出维度")
                if coords["C"] != channel or coords["T"] != timepoint:
                    continue
                z = coords["Z"]
                if z in mapping:
                    raise ValueError("OME TIFF 层映射重复")
                mapping[z] = (file, int(td.get("IFD", "0")) + j)
        if set(positions) != set(range(sizes["Z"])) or set(mapping) != set(positions):
            raise ValueError("OME 缺层：图像映射与逐层 Z 元数据必须完整对应")
        for index in range(sizes["Z"]):
            file, page = mapping[index]
            specs.append(
                dict(
                    index=index,
                    file=str(file),
                    page=page,
                    z_um=positions[index],
                    channel=channel,
                    timepoint=timepoint,
                    image_index=image_index,
                )
            )
        z_source = "ome_plane_position_z"
    else:
        raise ValueError(
            "尚不支持此 XML 格式；支持 OME-XML 和工作站堆栈清单。实验配置不能代替采集元数据"
        )
    if min(size) < 1 or not all(v > 0 for v in scale):
        raise ValueError("尺寸或像素标定无效")
    return _read_planes(specs, size, scale, path, z_source, digest)


def write_stack_xml(path, planes, *, width, height, scale_x_um, scale_y_um, z_source):
    """Write app-owned schema, not a claimed ZEISS export format; inputs already verified."""
    root = ET.Element(
        "ZenSurfaceStack",
        version="1",
        complete="true",
        count=str(len(planes)),
        width=str(width),
        height=str(height),
        unit="µm",
        scale_x=str(scale_x_um),
        scale_y=str(scale_y_um),
        z_source=z_source,
    )
    for index, plane in enumerate(planes):
        ET.SubElement(
            root,
            "Plane",
            index=str(index),
            file=plane["file"],
            page=str(plane.get("page", 0)),
            z=repr(float(plane["z_um"])),
        )
    path = Path(path)
    tmp = path.with_suffix(".xml.tmp")
    created = False
    try:
        with tmp.open("xb") as stream:
            created = True
            ET.ElementTree(root).write(stream, encoding="utf-8", xml_declaration=True)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise FileExistsError("堆栈 XML 已存在，拒绝覆盖")
        tmp.rename(path)
    finally:
        if created:
            tmp.unlink(missing_ok=True)


def export_stack(stack, destination):
    """Portable TIFF+XML and algorithm arrays. Publish directory only after all saves succeed."""
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError("请选择不存在的输出子目录，拒绝覆盖已有结果")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".stack-", dir=destination.parent))
    try:
        planes = []
        for i, image in enumerate(stack.pixels):
            name = f"z_{i:04d}.tif"
            tifffile.imwrite(
                temporary / name, image, photometric="minisblack", metadata=None
            )
            planes.append(dict(file=name, z_um=float(stack.z_um[i])))
        write_stack_xml(
            temporary / "stack.xml",
            planes,
            width=stack.pixels.shape[2],
            height=stack.pixels.shape[1],
            scale_x_um=stack.scale_x_um,
            scale_y_um=stack.scale_y_um,
            z_source=stack.z_source,
        )
        np.save(temporary / "images_zyx.npy", stack.pixels, allow_pickle=False)
        np.save(temporary / "z_um.npy", stack.z_um, allow_pickle=False)
        provenance = dict(
            schema_version=1,
            source=stack.source,
            z_source=stack.z_source,
            metadata_sha256=stack.metadata_sha256,
            planes=stack.planes,
            axes="ZYX",
            z_unit="µm",
            height_map_computed=False,
        )
        (temporary / "source.json").write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return destination
